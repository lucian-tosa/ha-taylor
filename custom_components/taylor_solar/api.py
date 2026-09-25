"""Client for the Taylor Solar cloud API."""

from __future__ import annotations

from asyncio import Lock, sleep
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
import json
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://clientapi.taylor.solar"
HEADERS = {
    "Client-Version": "1.0.0",
    "Client-Name": "end-user-api",
    "Content-Type": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=30)
MIN_REQUEST_INTERVAL = 7.5  # seconds; Taylor allows ~10 requests/minute
RATE_LIMIT_BACKOFF = 60  # seconds
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
# TODO(verify): token lifetime with persistUserSession; used only when `exp` is unusable.
FALLBACK_TOKEN_LIFETIME = timedelta(hours=1)


class TaylorError(Exception):
    """Base Taylor error."""


class TaylorApiError(TaylorError):
    """Unexpected response, connection error or timeout."""


class TaylorAuthError(TaylorError):
    """Invalid credentials or token rejected after re-authenticating."""


class TaylorApiVersionError(TaylorError):
    """HTTP 418: the API received backwards-incompatible changes."""


class TaylorRateLimitError(TaylorError):
    """Still rate limited after backing off."""


def parse_expiry(value: str) -> datetime:
    """Parse the `exp` field, e.g. 2025-06-16T22:23:12.2318992Z."""
    exp = datetime.fromisoformat(value)
    return exp if exp.tzinfo else exp.replace(tzinfo=UTC)


class TaylorClient:
    """Taylor API client with token handling and a shared rate limiter."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        min_interval: float = MIN_REQUEST_INTERVAL,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._username = username
        self._password = password
        self._min_interval = min_interval
        self._token: str | None = None
        self._expires = datetime.min.replace(tzinfo=UTC)
        self._lock = Lock()
        self._last_request = -float("inf")

    async def async_authenticate(self) -> None:
        """Obtain a new token."""
        status, data = await self._send(
            "POST",
            "/api/authenticate",
            {"Accept": "text/plain"},
            {
                "userName": self._username,
                "password": self._password,
                "persistUserSession": True,
            },
        )
        if status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise TaylorAuthError(f"Authentication failed ({status})")
        self._raise_for_status(status)
        try:
            self._token = data["token"]
        except (KeyError, TypeError) as err:
            raise TaylorApiError("Unexpected authentication response") from err
        try:
            self._expires = parse_expiry(data["exp"])
        except KeyError, TypeError, ValueError:
            self._expires = datetime.now(UTC) + FALLBACK_TOKEN_LIFETIME

    async def async_get_sites(self) -> list[dict[str, Any]]:
        """Return the sites of the account."""
        return await self._request("/api/public/sites") or []

    async def async_get_day(self, site_id: str, day: date) -> dict[str, Any] | None:
        """Return the day payload of a site, or None if there is no data."""
        # Days before installation return zero-filled padding, not 404; a 404 is
        # still treated as an empty day.
        return await self._request(
            f"/api/public/site/{site_id}/data/{day.year}/{day.month}/{day.day}",
            allow_404=True,
        )

    async def _request(self, path: str, *, allow_404: bool = False) -> Any:
        """GET an authenticated endpoint, re-authenticating once on 401."""
        for attempt in range(2):
            if self._token is None or self._expires - datetime.now(UTC) < TOKEN_REFRESH_MARGIN:
                await self.async_authenticate()
            status, data = await self._send("GET", path, {"Authorization": f"Taylor {self._token}"})
            if status != HTTPStatus.UNAUTHORIZED:
                break
            self._token = None
            if attempt:
                raise TaylorAuthError("Token rejected after re-authenticating")
        if status == HTTPStatus.NOT_FOUND and allow_404:
            return None
        self._raise_for_status(status)
        return data

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status == 418:
            raise TaylorApiVersionError("Taylor API changed incompatibly (HTTP 418)")
        if not 200 <= status < 300:
            raise TaylorApiError(f"Unexpected HTTP status {status}")

    async def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Send a rate-limited request, backing off once on 429."""
        async with self._lock:
            for attempt in range(2):
                if (wait := self._last_request + self._min_interval - time.monotonic()) > 0:
                    await sleep(wait)
                self._last_request = time.monotonic()
                status, data = await self._raw(method, path, headers, body)
                if status != HTTPStatus.TOO_MANY_REQUESTS:
                    return status, data
                if not attempt:
                    _LOGGER.warning("Rate limited by Taylor, retrying in %ss", RATE_LIMIT_BACKOFF)
                    await sleep(RATE_LIMIT_BACKOFF)
        raise TaylorRateLimitError("Rate limited by Taylor")

    async def _raw(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, Any]:
        try:
            async with self._session.request(
                method,
                BASE_URL + path,
                headers=HEADERS | headers,
                json=body,
                timeout=TIMEOUT,
            ) as resp:
                if not 200 <= resp.status < 300:
                    return resp.status, None
                # Responses are JSON regardless of content type (or Accept).
                text = await resp.text()
                return resp.status, json.loads(text) if text.strip() else None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TaylorApiError(f"Error communicating with Taylor: {err}") from err
        except ValueError as err:
            raise TaylorApiError("Invalid JSON from Taylor") from err

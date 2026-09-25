"""Tests for the Taylor API client."""

from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.taylor_solar.api import (
    BASE_URL,
    TaylorApiError,
    TaylorApiVersionError,
    TaylorAuthError,
    TaylorClient,
    TaylorRateLimitError,
    parse_expiry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import SITE_ID

AUTH_URL = f"{BASE_URL}/api/authenticate"
SITES_URL = f"{BASE_URL}/api/public/sites"
DAY_URL = f"{BASE_URL}/api/public/site/{SITE_ID}/data/2025/3/1"
NOW = datetime(2025, 3, 1, 12, tzinfo=UTC)


def _auth(exp: datetime = NOW + timedelta(days=7)) -> dict:
    return {"token": "fake-token", "exp": exp.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")}


def _sequence(*responses: tuple[int, object]):
    """Return a side effect replying with the given (status, json) in order."""
    queue = list(responses)

    async def side_effect(method, url, data):
        status, body = queue.pop(0)
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    return side_effect


@pytest.fixture
def sleep():
    with patch("custom_components.taylor_solar.api.sleep", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, sleep
) -> TaylorClient:
    freezer.move_to(NOW)
    return TaylorClient(async_get_clientsession(hass), "user@example.com", "pw", min_interval=0)


def test_parse_expiry_seven_fraction_digits() -> None:
    assert parse_expiry("2025-06-16T22:23:12.2318992Z") == datetime(
        2025, 6, 16, 22, 23, 12, 231899, tzinfo=UTC
    )


async def test_headers_and_requests(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, json=[{"id": SITE_ID, "siteName": "Home"}])
    aioclient_mock.get(DAY_URL, json={"systemMetrics": []})

    assert await client.async_get_sites() == [{"id": SITE_ID, "siteName": "Home"}]
    assert await client.async_get_day(SITE_ID, date(2025, 3, 1)) == {"systemMetrics": []}

    assert [str(url) for _, url, _, _ in aioclient_mock.mock_calls] == [
        AUTH_URL,
        SITES_URL,
        DAY_URL,
    ]
    for _, _, _, headers in aioclient_mock.mock_calls:
        assert headers["Client-Version"] == "1.0.0"
        assert headers["Client-Name"] == "end-user-api"
        assert headers["Content-Type"] == "application/json"
    auth_call, sites_call, _ = aioclient_mock.mock_calls
    assert auth_call[2] == {
        "userName": "user@example.com",
        "password": "pw",
        "persistUserSession": True,
    }
    assert auth_call[3]["Accept"] == "text/plain"
    assert sites_call[3]["Authorization"] == "Taylor fake-token"


async def test_token_refreshed_near_expiry(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth(NOW + timedelta(minutes=10)))
    aioclient_mock.get(SITES_URL, json=[])

    await client.async_get_sites()
    await client.async_get_sites()
    assert aioclient_mock.call_count == 3  # one auth

    freezer.tick(timedelta(minutes=6))
    await client.async_get_sites()
    assert aioclient_mock.call_count == 5  # re-authenticated


async def test_401_reauthenticates_once(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, side_effect=_sequence((401, None), (200, [{"id": SITE_ID}])))

    assert await client.async_get_sites() == [{"id": SITE_ID}]
    assert [m for m, *_ in aioclient_mock.mock_calls] == ["POST", "GET", "POST", "GET"]


async def test_401_after_reauth_raises(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, status=HTTPStatus.UNAUTHORIZED)

    with pytest.raises(TaylorAuthError):
        await client.async_get_sites()
    assert aioclient_mock.call_count == 4


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_invalid_credentials(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    aioclient_mock.post(AUTH_URL, status=status)
    with pytest.raises(TaylorAuthError):
        await client.async_authenticate()


async def test_418_is_version_error(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, status=418)
    with pytest.raises(TaylorApiVersionError):
        await client.async_get_sites()


async def test_429_retries_after_backoff(
    client: TaylorClient, aioclient_mock: AiohttpClientMocker, sleep: AsyncMock
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, side_effect=_sequence((429, None), (200, [])))

    assert await client.async_get_sites() == []
    sleep.assert_awaited_once_with(60)


async def test_429_twice_raises(client: TaylorClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, status=429)
    with pytest.raises(TaylorRateLimitError):
        await client.async_get_sites()


async def test_404_day_is_empty(client: TaylorClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(DAY_URL, status=404)
    assert await client.async_get_day(SITE_ID, date(2025, 3, 1)) is None


async def test_other_errors(client: TaylorClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth())
    aioclient_mock.get(SITES_URL, status=500)
    aioclient_mock.get(DAY_URL, exc=TimeoutError())
    with pytest.raises(TaylorApiError):
        await client.async_get_sites()
    with pytest.raises(TaylorApiError):
        await client.async_get_day(SITE_ID, date(2025, 3, 1))


async def test_rate_limiter_spaces_requests(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sleep: AsyncMock
) -> None:
    aioclient_mock.post(AUTH_URL, json=_auth(datetime.now(UTC) + timedelta(days=1)))
    aioclient_mock.get(SITES_URL, json=[])
    client = TaylorClient(async_get_clientsession(hass), "user@example.com", "pw")

    await client.async_get_sites()
    assert sleep.await_count == 1  # auth went first, sites waited
    assert 7 < sleep.await_args.args[0] <= 7.5

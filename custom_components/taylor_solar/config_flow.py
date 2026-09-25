"""Config flow for Taylor Solar."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TaylorApiVersionError, TaylorAuthError, TaylorClient, TaylorError
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    MAX_BACKFILL_DAYS,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_BACKFILL_DAYS, default=DEFAULT_BACKFILL_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=MAX_BACKFILL_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="days",
            )
        ),
    }
)


def _site_label(site: dict[str, Any]) -> str:
    address = " ".join(filter(None, (site.get("address"), site.get("houseNumber"))))
    return f"{site.get('siteName')} ({address})" if address else str(site.get("siteName"))


class TaylorSolarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Taylor Solar."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._sites: list[dict[str, Any]] = []

    async def _async_validate(
        self, username: str, password: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Authenticate and list sites; return (sites, error key)."""
        # Only two requests here, so skip the runtime rate limiter.
        client = TaylorClient(
            async_get_clientsession(self.hass), username, password, min_interval=0
        )
        try:
            await client.async_authenticate()
            sites = await client.async_get_sites()
        except TaylorAuthError:
            return [], "invalid_auth"
        except TaylorApiVersionError:
            return [], "api_changed"
        except TaylorError:
            return [], "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error")
            return [], "unknown"
        return sites, None if sites else "no_sites"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for credentials and the backfill period."""
        errors: dict[str, str] = {}
        if user_input is not None:
            sites, error = await self._async_validate(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                self._data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_BACKFILL_DAYS: int(user_input[CONF_BACKFILL_DAYS]),
                }
                self._sites = sites
                if len(sites) == 1:
                    return await self._async_create(sites[0])
                return await self.async_step_site()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a site when the account has several."""
        if user_input is not None:
            site = next(s for s in self._sites if s["id"] == user_input[CONF_SITE_ID])
            return await self._async_create(site)
        options = [
            SelectOptionDict(value=site["id"], label=_site_label(site)) for site in self._sites
        ]
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {vol.Required(CONF_SITE_ID): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def _async_create(self, site: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(site["id"])
        self._abort_if_unique_id_configured()
        name = site.get("siteName") or site["id"]
        return self.async_create_entry(
            title=name,
            data={**self._data, CONF_SITE_ID: site["id"], CONF_SITE_NAME: name},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, error = await self._async_validate(
                entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error and error != "no_sites":
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

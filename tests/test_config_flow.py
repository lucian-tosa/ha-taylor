"""Tests for the Taylor Solar config flow."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.taylor_solar.api import (
    TaylorApiError,
    TaylorApiVersionError,
    TaylorAuthError,
)
from custom_components.taylor_solar.const import (
    CONF_BACKFILL_DAYS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import SITE_ID

pytestmark = pytest.mark.usefixtures("setup_env")

OTHER_ID = "11111111-2222-3333-4444-555555555555"
SITE = {
    "id": SITE_ID,
    "siteName": "Home",
    "address": "Main St",
    "houseNumber": "1",
    "zipcode": "1234AB",
}
OTHER = {
    "id": OTHER_ID,
    "siteName": "Cabin",
    "address": "Lake Rd",
    "houseNumber": "7",
    "zipcode": "5678CD",
}
USER_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret", CONF_BACKFILL_DAYS: 30.0}


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    with patch("custom_components.taylor_solar.config_flow.TaylorClient", autospec=True) as cls:
        client = cls.return_value
        client.async_get_sites.return_value = [SITE]
        yield client


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch("custom_components.taylor_solar.async_setup_entry", return_value=True) as mock:
        yield mock


async def test_single_site(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["result"].unique_id == SITE_ID
    assert result["data"] == {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_BACKFILL_DAYS: 30,
        CONF_SITE_ID: SITE_ID,
        CONF_SITE_NAME: "Home",
    }


async def test_multiple_sites(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    mock_client.async_get_sites.return_value = [SITE, OTHER]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    options = result["data_schema"].schema[CONF_SITE_ID].config["options"]
    assert options[1] == {"value": OTHER_ID, "label": "Cabin (Lake Rd 7)"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: OTHER_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cabin"
    assert result["data"][CONF_SITE_ID] == OTHER_ID


@pytest.mark.parametrize(
    ("side_effect", "sites", "error"),
    [
        (TaylorAuthError, [SITE], "invalid_auth"),
        (TaylorApiError, [SITE], "cannot_connect"),
        (TaylorApiVersionError, [SITE], "api_changed"),
        (RuntimeError, [SITE], "unknown"),
        (None, [], "no_sites"),
    ],
)
async def test_errors_then_recover(
    hass: HomeAssistant, mock_client: AsyncMock, side_effect, sites, error: str
) -> None:
    mock_client.async_authenticate.side_effect = side_effect
    mock_client.async_get_sites.return_value = sites
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.async_authenticate.side_effect = None
    mock_client.async_get_sites.return_value = [SITE]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_aborts(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.async_authenticate.side_effect = TaylorAuthError
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.async_authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new"
    assert config_entry.data[CONF_USERNAME] == "user@example.com"

"""Tests for Taylor Solar setup and sensors."""

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.taylor_solar.api import TaylorApiVersionError, TaylorAuthError
from custom_components.taylor_solar.const import DOMAIN, ISSUE_API_CHANGED, UPDATE_INTERVAL
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import day_payload

pytestmark = pytest.mark.usefixtures("setup_env")

TODAY = date(2026, 6, 15)
PANELS = [
    {
        "panelId": 1234,
        "cellStringAProductionWh": 426,
        "cellStringBProductionWh": 430,
        "cellStringCProductionWh": 433,
    },
    {
        "panelId": 1235,
        "cellStringAProductionWh": 400,
        "cellStringBProductionWh": 400,
        "cellStringCProductionWh": 400,
    },
]


@pytest.fixture
def days() -> dict[date, dict[str, Any]]:
    return {
        TODAY: day_payload(
            TODAY,
            {"08:00": {0: 200, 1: 700}, "08:30": {0: 300, 1: 500}, "09:00": {0: 450}},
            panels=PANELS,
        )
    }


@pytest.fixture
def client(
    days: dict[date, dict[str, Any]], freezer: FrozenDateTimeFactory
) -> Generator[AsyncMock]:
    freezer.move_to(datetime(2026, 6, 15, 7, 40, tzinfo=UTC))  # 09:40 CEST
    with patch("custom_components.taylor_solar.TaylorClient", autospec=True) as cls:
        client = cls.return_value
        client.async_get_day.side_effect = lambda _site, day: days.get(day)
        yield client


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client: AsyncMock,
    entity_registry: er.EntityRegistry,
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.home_solar_production_today").state == "0.95"
    assert hass.states.get("sensor.home_consumption_today").state == "1.2"
    assert hass.states.get("sensor.home_grid_import_today") is None

    energy = hass.states.get("sensor.home_solar_production_today")
    assert energy.attributes["device_class"] == "energy"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert "state_class" not in energy.attributes

    power = hass.states.get("sensor.home_solar_power")
    assert power.state == "900"
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["state_class"] == "measurement"
    assert power.attributes["bucket_start"] == "2026-06-15T07:00:00+00:00"

    # No layout in this payload format: panels are named by Taylor's ID.
    panel = hass.states.get("sensor.home_panel_1234_energy_today")
    assert panel.state == "1.289"
    assert panel.attributes["taylor_panel_id"] == 1234
    assert panel.attributes["cell_string_a_wh"] == 426
    assert panel.attributes["cell_string_c_wh"] == 433
    assert hass.states.get("sensor.home_panel_1235_energy_today").state == "1.2"

    imported = hass.states.get("sensor.home_statistics_imported_through")
    assert imported.state == "2026-06-15"
    assert entity_registry.async_get(imported.entity_id).entity_category == "diagnostic"


async def test_new_types_are_added(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client: AsyncMock,
    days: dict[date, dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.home_grid_export_today") is None

    days[TODAY] = day_payload(TODAY, {"08:00": {0: 200, 3: 150}})
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.home_grid_export_today").state == "0.15"
    assert hass.states.get("sensor.home_panel_1234_energy_today").state == "unknown"


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.async_get_day.side_effect = TaylorAuthError
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_api_changed_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.async_get_day.side_effect = TaylorApiVersionError
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_API_CHANGED)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable


async def test_unload_and_remove(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client: AsyncMock,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, config_entry)
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    key = f"{DOMAIN}.{config_entry.entry_id}"
    assert hass_storage[key]["data"] == {"last_day": "2026-06-15"}

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_live_format_panels_named_by_layout_number(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client: AsyncMock,
    days: dict[date, dict[str, Any]],
    entity_registry: er.EntityRegistry,
) -> None:
    point = {
        "timestamp": "2026-06-15T09:00:00",
        "inverterEnergyData": {"quality": 0, "solarProduction": 400, "consumption": None},
        "panelEnergyData": [
            {
                "id": 9001,
                "cellStringAProductionWh": 10,
                "cellStringBProductionWh": 11,
                "cellStringCProductionWh": 12,
            },
            {
                "id": 9002,
                "cellStringAProductionWh": 9,
                "cellStringBProductionWh": 9,
                "cellStringCProductionWh": 9,
            },
        ],
    }
    days[TODAY] = {
        "systemMetrics": [
            {
                "dataPoints": [point],
                "panelLayout": {
                    "panelPositions": [{"id": 9001, "number": 1}, {"id": 9002, "number": 2}]
                },
            }
        ],
        "dayDataPointDurationSeconds": 900,
    }
    await _setup(hass, config_entry)

    assert hass.states.get("sensor.home_solar_production_today").state == "0.4"
    assert hass.states.get("sensor.home_solar_power").state == "1600"
    assert hass.states.get("sensor.home_consumption_today") is None

    panel = hass.states.get("sensor.home_panel_1_energy_today")
    assert panel.state == "0.033"
    assert panel.attributes["taylor_panel_id"] == 9001
    assert hass.states.get("sensor.home_panel_2_energy_today").state == "0.027"
    entry = entity_registry.async_get("sensor.home_panel_1_energy_today")
    assert entry.unique_id.endswith("_panel_9001_energy_today")

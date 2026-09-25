"""Tests for the statistics importer."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from itertools import accumulate
from typing import Any
from unittest.mock import AsyncMock, Mock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.taylor_solar.api import (
    TaylorApiError,
    TaylorApiVersionError,
    TaylorAuthError,
)
from custom_components.taylor_solar.const import DOMAIN, ISSUE_API_CHANGED
from custom_components.taylor_solar.coordinator import StatisticsImporter, statistic_id
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .conftest import SITE_HEX, SITE_ID, day_payload

pytestmark = pytest.mark.usefixtures("setup_env")

SOLAR = f"taylor_solar:{SITE_HEX}_solar_production"
EXPORT = f"taylor_solar:{SITE_HEX}_grid_export"
TODAY = date(2026, 6, 15)  # CEST, UTC+2
FULL_DAY = {"08:00": {0: 100, 3: 10}, "08:30": {0: 200}, "12:00": {0: 500, 3: 50}}
TODAY_DATA = {
    "08:00": {0: 100},
    "08:30": {0: 100},
    "09:00": {0: 300},
    "10:00": {0: 400},  # incomplete at 10:20
}


def local(d: date, hour: int) -> datetime:
    """Return the UTC start of a local (CEST) hour."""
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC) - timedelta(hours=2)


@pytest.fixture
def days() -> dict[date, dict[str, Any]]:
    """Day payloads served by the fake client; missing days return None (404)."""
    return {
        TODAY - timedelta(days=n): day_payload(TODAY - timedelta(days=n), FULL_DAY)
        for n in (1, 2, 3)
    } | {TODAY: day_payload(TODAY, TODAY_DATA)}


@pytest.fixture
def client(days: dict[date, dict[str, Any]]) -> Mock:
    client = Mock()
    client.async_get_day = AsyncMock(side_effect=lambda _site, day: days.get(day))
    return client


@pytest.fixture
def importer(
    hass: HomeAssistant, config_entry: MockConfigEntry, client: Mock, freezer: FrozenDateTimeFactory
) -> StatisticsImporter:
    freezer.move_to(datetime(2026, 6, 15, 10, 20, tzinfo=UTC) - timedelta(hours=2))
    return StatisticsImporter(hass, config_entry, client, Mock())


async def _stats(hass: HomeAssistant, stat_id: str) -> list[tuple[datetime, float, float]]:
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        (datetime.fromtimestamp(r["start"], UTC), round(r["state"], 6), round(r["sum"], 6))
        for r in rows.get(stat_id, [])
    ]


def _assert_consistent(rows: list[tuple[datetime, float, float]]) -> None:
    starts = [start for start, _, _ in rows]
    assert starts == sorted(starts)
    expected = list(accumulate(state for _, state, _ in rows))
    assert [s for _, _, s in rows] == pytest.approx(expected)


def test_statistic_id() -> None:
    assert statistic_id(SITE_ID, 0) == SOLAR
    assert statistic_id(SITE_ID, 5) == f"taylor_solar:{SITE_HEX}_battery_discharge"


async def test_backfill(hass: HomeAssistant, importer: StatisticsImporter, client: Mock) -> None:
    await importer.async_run()

    fetched = [c.args[1] for c in client.async_get_day.await_args_list]
    assert fetched == [TODAY - timedelta(days=n) for n in (3, 2, 1, 0)]
    assert importer.last_day == TODAY
    assert importer._on_progress.call_count == 4

    rows = await _stats(hass, SOLAR)
    _assert_consistent(rows)
    past = [
        (local(TODAY - timedelta(days=n), h), kwh)
        for n in (3, 2, 1)
        for h, kwh in ((8, 0.3), (12, 0.5))
    ]
    # Today: only complete hours (08:00 and 09:00 local; 10:00 still running).
    assert [(start, state) for start, state, _ in rows] == [
        *past,
        (local(TODAY, 8), 0.2),
        (local(TODAY, 9), 0.3),
    ]
    assert rows[-1][2] == pytest.approx(3 * 0.8 + 0.5)

    export = await _stats(hass, EXPORT)
    _assert_consistent(export)
    assert export[-1][2] == pytest.approx(3 * 0.06)

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={SOLAR})
    )
    meta = metadata[SOLAR][1]
    assert meta["name"] == "Taylor Home solar production"
    assert meta["source"] == DOMAIN
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["has_sum"]


async def test_revision_rewrites_window(
    hass: HomeAssistant,
    importer: StatisticsImporter,
    client: Mock,
    days: dict[date, dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    await importer.async_run()
    before = await _stats(hass, SOLAR)

    # Taylor revises yesterday's 12:00 bucket; an hour later, 10:00 is complete.
    yesterday = TODAY - timedelta(days=1)
    days[yesterday] = day_payload(yesterday, FULL_DAY | {"12:00": {0: 800}})
    freezer.tick(timedelta(hours=1))
    client.async_get_day.reset_mock()

    await importer.async_run()
    assert [c.args[1] for c in client.async_get_day.await_args_list] == [yesterday, TODAY]

    rows = await _stats(hass, SOLAR)
    _assert_consistent(rows)
    assert rows[:2] == before[:2]  # untouched rows before the window
    by_start = {start: state for start, state, _ in rows}
    assert by_start[local(yesterday, 12)] == 0.8
    assert by_start[local(TODAY, 10)] == 0.4
    assert rows[-1][2] == pytest.approx(3 * 0.8 + 0.3 + 0.9)


async def test_empty_days_advance_cursor(
    hass: HomeAssistant,
    importer: StatisticsImporter,
    client: Mock,
    days: dict[date, dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    # Before install: 404 (missing) and a payload without data points.
    del days[TODAY - timedelta(days=3)]
    days[TODAY - timedelta(days=2)] = day_payload(TODAY - timedelta(days=2), {})

    await importer.async_run()
    assert importer.last_day == TODAY
    rows = await _stats(hass, SOLAR)
    _assert_consistent(rows)
    assert rows[0][0] == local(TODAY - timedelta(days=1), 8)

    client.async_get_day.reset_mock()
    freezer.tick(timedelta(minutes=15))
    await importer.async_run()
    assert [c.args[1] for c in client.async_get_day.await_args_list] == [
        TODAY - timedelta(days=1),
        TODAY,
    ]
    assert await _stats(hass, SOLAR) == rows


async def test_cursor_persists(
    hass: HomeAssistant,
    importer: StatisticsImporter,
    config_entry: MockConfigEntry,
    client: Mock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await importer.async_run()
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    reloaded = StatisticsImporter(hass, config_entry, client, Mock())
    await reloaded.async_load()
    assert reloaded.last_day == TODAY


async def test_error_stops_and_resumes(
    hass: HomeAssistant,
    importer: StatisticsImporter,
    client: Mock,
    days: dict[date, dict[str, Any]],
) -> None:
    serve: Callable = client.async_get_day.side_effect

    async def fail_yesterday(site: str, day: date):
        if day == TODAY - timedelta(days=1):
            raise TaylorApiError("boom")
        return serve(site, day)

    client.async_get_day.side_effect = fail_yesterday
    await importer.async_run()
    assert importer.last_day == TODAY - timedelta(days=2)

    client.async_get_day.side_effect = serve
    client.async_get_day.reset_mock()
    await importer.async_run()
    assert [c.args[1] for c in client.async_get_day.await_args_list] == [
        TODAY - timedelta(days=3),
        TODAY - timedelta(days=2),
        TODAY - timedelta(days=1),
        TODAY,
    ]
    rows = await _stats(hass, SOLAR)
    _assert_consistent(rows)
    assert rows[-1][2] == pytest.approx(3 * 0.8 + 0.5)


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, importer: StatisticsImporter, client: Mock
) -> None:
    client.async_get_day.side_effect = TaylorAuthError
    await importer.async_run()
    await hass.async_block_till_done(wait_background_tasks=True)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert importer.last_day is None


async def test_api_version_error_creates_issue(
    hass: HomeAssistant, importer: StatisticsImporter, client: Mock
) -> None:
    client.async_get_day.side_effect = TaylorApiVersionError
    await importer.async_run()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_API_CHANGED)

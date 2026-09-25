"""Coordinator for today's data and the long-term statistics importer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
import logging
import re

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import TaylorApiVersionError, TaylorAuthError, TaylorClient, TaylorError
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    DATA_TYPES,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    ISSUE_API_CHANGED,
    TYPE_LABELS,
    UPDATE_INTERVAL,
)
from .parsing import TaylorDay, hourly_kwh, parse_day

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
HOUR = timedelta(hours=1)
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)

type TaylorConfigEntry = ConfigEntry[TaylorCoordinator]


def storage_key(entry_id: str) -> str:
    """Return the Store key of the importer cursor."""
    return f"{DOMAIN}.{entry_id}"


def statistic_id(site_id: str, type_: int) -> str:
    """Return the external statistic ID for a site and data type."""
    return f"{DOMAIN}:{re.sub(r'[^0-9a-z]', '', site_id.lower())}_{DATA_TYPES[type_]}"


@callback
def async_create_api_issue(hass: HomeAssistant) -> None:
    """Raise a repair issue for a backwards-incompatible API change."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_API_CHANGED,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_API_CHANGED,
    )


class TaylorCoordinator(DataUpdateCoordinator[TaylorDay]):
    """Polls today's data and kicks the statistics importer."""

    config_entry: TaylorConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TaylorConfigEntry, client: TaylorClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.site_id: str = entry.data[CONF_SITE_ID]
        self.importer = StatisticsImporter(hass, entry, client, self.async_update_listeners)

    async def _async_update_data(self) -> TaylorDay:
        try:
            payload = await self.client.async_get_day(self.site_id, dt_util.now().date())
        except TaylorAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TaylorApiVersionError as err:
            async_create_api_issue(self.hass)
            raise UpdateFailed(str(err)) from err
        except TaylorError as err:
            raise UpdateFailed(str(err)) from err
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_API_CHANGED)
        self.importer.async_schedule()
        return parse_day(payload)


class StatisticsImporter:
    """Imports hourly energy into external statistics, tracked by a day cursor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TaylorConfigEntry,
        client: TaylorClient,
        on_progress: Callable[[], None],
    ) -> None:
        """Initialize the importer."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self._on_progress = on_progress
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._lock = asyncio.Lock()
        self.last_day: date | None = None

    async def async_load(self) -> None:
        """Load the cursor."""
        if (data := await self._store.async_load()) and (last := data.get("last_day")):
            self.last_day = date.fromisoformat(last)

    @callback
    def async_schedule(self) -> None:
        """Start a run in the background unless one is in progress."""
        if not self._lock.locked():
            self.entry.async_create_background_task(
                self.hass, self.async_run(), f"{DOMAIN} statistics import"
            )

    async def async_run(self) -> None:
        """Import from the cursor through today; stop on the first error."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._async_import()
            except TaylorAuthError:
                self.entry.async_start_reauth(self.hass)
            except TaylorApiVersionError:
                async_create_api_issue(self.hass)
            except TaylorError as err:
                _LOGGER.warning(
                    "Statistics import stopped, resuming after %s next time: %s",
                    self.last_day,
                    err,
                )

    async def _async_import(self) -> None:
        now = dt_util.now()
        today = now.date()
        tz = dt_util.get_default_time_zone()
        if self.last_day is None:
            days = self.entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
            first = today - timedelta(days=days)
        else:
            first = min(self.last_day - timedelta(days=1), today)
        window_start = dt_util.as_utc(dt_util.start_of_local_day(first))
        site_id: str = self.entry.data[CONF_SITE_ID]
        site_name: str = self.entry.data[CONF_SITE_NAME]

        # Commit the previous run's rows before reading base sums.
        await get_instance(self.hass).async_block_till_done()
        sums: dict[int, float] = {}

        day = first
        while day <= today:
            parsed = parse_day(await self.client.async_get_day(site_id, day))
            # TODO(verify): whether types 1-3 are all zeros without a Taylor meter;
            # if so, they are still imported as flat statistics.
            for type_, series in parsed.series.items():
                hours = sorted(
                    (start, kwh)
                    for start, kwh in hourly_kwh(series, tz).items()
                    if start + HOUR <= now  # complete hours only
                )
                if not hours:
                    continue
                stat_id = statistic_id(site_id, type_)
                if type_ not in sums:
                    sums[type_] = await self._async_base_sum(stat_id, window_start)
                rows = []
                for start, kwh in hours:
                    sums[type_] += kwh
                    rows.append(StatisticData(start=start, state=kwh, sum=sums[type_]))
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        mean_type=StatisticMeanType.NONE,
                        has_sum=True,
                        name=f"Taylor {site_name} {TYPE_LABELS[type_]}",
                        source=DOMAIN,
                        statistic_id=stat_id,
                        unit_class=EnergyConverter.UNIT_CLASS,
                        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    ),
                    rows,
                )
            self.last_day = day
            self._store.async_delay_save(self._data_to_save, 1)
            self._on_progress()
            if (day - first).days % 30 == 29:
                _LOGGER.info("Taylor statistics imported through %s", day)
            day += timedelta(days=1)

    def _data_to_save(self) -> dict[str, str]:
        assert self.last_day is not None
        return {"last_day": self.last_day.isoformat()}

    async def _async_base_sum(self, stat_id: str, window_start: datetime) -> float:
        """Return the sum of the last row before the window, or 0."""
        for start in (window_start - timedelta(days=3), EPOCH):
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                window_start,
                {stat_id},
                "hour",
                None,
                {"sum"},
            )
            if rows := stats.get(stat_id):
                return rows[-1].get("sum") or 0.0
        return 0.0

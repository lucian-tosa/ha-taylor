"""Sensors for Taylor Solar."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_SITE_NAME, DATA_TYPES, DOMAIN, TYPE_SOLAR
from .coordinator import TaylorConfigEntry, TaylorCoordinator
from .parsing import PanelDay, localize


@dataclass(frozen=True, kw_only=True)
class TaylorSensorDescription(SensorEntityDescription):
    """Describes a Taylor sensor."""

    value_fn: Callable[[TaylorCoordinator], float | date | None]
    attr_fn: Callable[[TaylorCoordinator], dict[str, Any] | None] = lambda _: None


def _energy_today(type_: int) -> TaylorSensorDescription:
    # No state_class: the Energy dashboard should use the imported statistics.
    return TaylorSensorDescription(
        key=f"{DATA_TYPES[type_]}_today",
        translation_key=f"{DATA_TYPES[type_]}_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda c: sum(wh for _, wh in c.data.series.get(type_, ())) / 1000,
    )


def _latest_solar(coordinator: TaylorCoordinator) -> tuple[Any, float] | None:
    series = coordinator.data.series.get(TYPE_SOLAR)
    if not series:
        return None
    return max(localize(series, dt_util.get_default_time_zone()), key=lambda p: p[0])


def _panel(coordinator: TaylorCoordinator, panel_id: int) -> PanelDay | None:
    return next((p for p in coordinator.data.panels if p.panel_id == panel_id), None)


def _panel_energy(panel: PanelDay) -> TaylorSensorDescription:
    panel_id = panel.panel_id

    def attrs(c: TaylorCoordinator) -> dict[str, Any] | None:
        if (panel := _panel(c, panel_id)) is None:
            return None
        return {
            "taylor_panel_id": panel_id,
            "cell_string_a_wh": panel.a,
            "cell_string_b_wh": panel.b,
            "cell_string_c_wh": panel.c,
        }

    # The unique ID uses Taylor's panel ID, which never changes; the name uses
    # the panel's number in the site layout when the payload provides one.
    return TaylorSensorDescription(
        key=f"panel_{panel_id}_energy_today",
        translation_key="panel_energy_today",
        translation_placeholders={"panel": str(panel.number or panel_id)},
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda c: (p.total_wh / 1000) if (p := _panel(c, panel_id)) else None,
        attr_fn=attrs,
    )


SOLAR_POWER = TaylorSensorDescription(
    key="solar_power",
    translation_key="solar_power",
    device_class=SensorDeviceClass.POWER,
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement=UnitOfPower.WATT,
    value_fn=lambda c: (
        round(latest[1] * 3600 / c.data.interval_seconds) if (latest := _latest_solar(c)) else None
    ),
    attr_fn=lambda c: (
        {"bucket_start": latest[0].isoformat()} if (latest := _latest_solar(c)) else None
    ),
)

IMPORTED_THROUGH = TaylorSensorDescription(
    key="statistics_imported_through",
    translation_key="statistics_imported_through",
    device_class=SensorDeviceClass.DATE,
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=lambda c: c.importer.last_day,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TaylorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors, adding per-type and per-panel ones as they appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        types = {TYPE_SOLAR, *coordinator.data.series}
        descriptions = [SOLAR_POWER, IMPORTED_THROUGH]
        descriptions += [_energy_today(t) for t in sorted(types)]
        descriptions += [
            _panel_energy(p)
            for p in sorted(coordinator.data.panels, key=lambda p: (p.number or 0, p.panel_id))
        ]
        new = list({d.key: d for d in descriptions if d.key not in known}.values())
        known.update(d.key for d in new)
        if new:
            async_add_entities(TaylorSensor(coordinator, d) for d in new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class TaylorSensor(CoordinatorEntity[TaylorCoordinator], SensorEntity):
    """A Taylor sensor."""

    _attr_has_entity_name = True
    entity_description: TaylorSensorDescription

    def __init__(
        self, coordinator: TaylorCoordinator, description: TaylorSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        site_id = coordinator.site_id
        self._attr_unique_id = f"{site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)},
            name=coordinator.config_entry.data[CONF_SITE_NAME],
            manufacturer="Taylor",
        )

    @property
    def native_value(self) -> float | date | None:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        return self.entity_description.attr_fn(self.coordinator)

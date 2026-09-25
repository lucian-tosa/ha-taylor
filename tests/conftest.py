"""Fixtures for Taylor Solar tests."""

from __future__ import annotations

from datetime import date
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.taylor_solar.const import (
    CONF_BACKFILL_DAYS,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    DOMAIN,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

# The HA test plugin logs every SQL statement at INFO.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

SITE_ID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
SITE_HEX = SITE_ID.replace("-", "")


def day_payload(
    day: date,
    buckets: dict[str, dict[int, float]],
    *,
    panels: list[dict[str, Any]] | None = None,
    interval: int = 1800,
) -> dict[str, Any]:
    """Build a day payload from {"HH:MM": {type: wh}}."""
    return {
        "systemMetrics": [
            {
                "dataPoints": [
                    {
                        "timestamp": f"{day.isoformat()}T{hhmm}:00",
                        "data": [{"type": t, "wh": wh} for t, wh in data.items()],
                    }
                    for hhmm, data in buckets.items()
                ],
                "panelData": panels or [],
            }
        ],
        "dayDataPointDurationSeconds": interval,
    }


@pytest.fixture
async def setup_env(recorder_mock, enable_custom_integrations, hass: HomeAssistant) -> None:
    """Enable the recorder, custom integrations and the owner's time zone."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a Taylor config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=SITE_ID,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_SITE_ID: SITE_ID,
            CONF_SITE_NAME: "Home",
            CONF_BACKFILL_DAYS: 3,
        },
    )
    entry.add_to_hass(hass)
    return entry

"""Constants for the Taylor Solar integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "taylor_solar"

CONF_SITE_ID: Final = "site_id"
CONF_SITE_NAME: Final = "site_name"
CONF_BACKFILL_DAYS: Final = "backfill_days"

DEFAULT_BACKFILL_DAYS: Final = 365
MAX_BACKFILL_DAYS: Final = 1095

UPDATE_INTERVAL: Final = timedelta(minutes=15)
# Once a day, the importer re-fetches this many past days to pick up Taylor's
# later corrections and filled-in gaps; other runs re-fetch yesterday and today.
DAILY_REIMPORT_DAYS: Final = 7

# Taylor data point types -> statistic key suffix / translation key.
TYPE_SOLAR: Final = 0
DATA_TYPES: Final[dict[int, str]] = {
    0: "solar_production",
    1: "consumption",
    2: "grid_import",
    3: "grid_export",
    4: "battery_charge",
    5: "battery_discharge",
}
TYPE_LABELS: Final[dict[int, str]] = {
    0: "solar production",
    1: "consumption",
    2: "grid import",
    3: "grid export",
    4: "battery charge",
    5: "battery discharge",
}

ISSUE_API_CHANGED: Final = "api_changed"

"""Pure functions for turning Taylor day payloads into energy series."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from .const import DATA_TYPES

type Series = list[tuple[datetime, float]]

DEFAULT_INTERVAL_SECONDS = 900

# Live payloads report energy per data point in `inverterEnergyData`, in Wh
# per interval. `grid`, `battery` and `balance` are signed values whose sign
# convention is unknown (they are null on the systems seen so far), so they
# are not imported.
# TODO(verify): sign convention of grid/battery on a system with a Taylor meter.
INVERTER_FIELDS = {"solarProduction": 0, "consumption": 1}


@dataclass(frozen=True, slots=True)
class PanelDay:
    """Per-day cell-string production of one panel, in Wh."""

    panel_id: int
    a: float
    b: float
    c: float
    number: int | None = None  # position in the site layout, as numbered in the Taylor app

    @property
    def total_wh(self) -> float:
        """Total production of the panel."""
        return self.a + self.b + self.c


@dataclass(frozen=True, slots=True)
class TaylorDay:
    """Parsed day payload."""

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    # Taylor type -> [(naive site-local bucket start, Wh)] in payload order.
    series: dict[int, Series] = field(default_factory=dict)
    panels: list[PanelDay] = field(default_factory=list)


def _energy_items(point: dict[str, Any]) -> list[tuple[Any, Any]]:
    """Return (type, Wh) pairs of a data point in either payload format."""
    inverter = point.get("inverterEnergyData") or {}
    return [(t, inverter.get(name)) for name, t in INVERTER_FIELDS.items()] + [
        # Format from the vendor's API document; not seen in live responses.
        (item.get("type"), item.get("wh"))
        for item in point.get("data") or []
    ]


def parse_day(payload: dict[str, Any] | None) -> TaylorDay:
    """Parse a day payload, summing data points across all system metrics."""
    if not payload:
        return TaylorDay()

    # Keyed by (timestamp, occurrence) so a timestamp repeated on the DST
    # fall-back day stays two buckets; dict order preserves payload order.
    merged: dict[int, dict[tuple[datetime, int], float]] = defaultdict(dict)
    panels: defaultdict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    def add_panel(panel_id: int, panel: dict[str, Any]) -> None:
        totals = panels[panel_id]
        for i, cell in enumerate("ABC"):
            totals[i] += panel.get(f"cellString{cell}ProductionWh") or 0

    numbers: dict[int, int] = {}

    for metric in payload.get("systemMetrics") or []:
        layout = metric.get("panelLayout") or {}
        numbers |= {pos["id"]: pos["number"] for pos in layout.get("panelPositions") or []}
        seen: defaultdict[datetime, int] = defaultdict(int)
        for point in metric.get("dataPoints") or []:
            # A "Z" suffix marks zero-filled padding for intervals without data
            # (before installation, after sunset); real intervals are naive
            # site-local times.
            if point["timestamp"].endswith("Z"):
                continue
            ts = datetime.fromisoformat(point["timestamp"])
            key = (ts, seen[ts])
            seen[ts] += 1
            for type_, wh in _energy_items(point):
                if type_ not in DATA_TYPES or wh is None:
                    continue
                buckets = merged[type_]
                buckets[key] = buckets.get(key, 0.0) + wh
            for panel in point.get("panelEnergyData") or []:
                add_panel(panel["id"], panel)
        # Per-day totals, in the vendor document's format.
        for panel in metric.get("panelData") or []:
            add_panel(panel["panelId"], panel)

    return TaylorDay(
        interval_seconds=payload.get("dayDataPointDurationSeconds") or DEFAULT_INTERVAL_SECONDS,
        series={
            type_: [(ts, wh) for (ts, _), wh in buckets.items()]
            for type_, buckets in sorted(merged.items())
        },
        panels=[
            PanelDay(panel_id, *totals, number=numbers.get(panel_id))
            for panel_id, totals in panels.items()
        ],
    )


def localize(series: Series, tz: tzinfo) -> list[tuple[datetime, float]]:
    """Convert naive site-local bucket starts to UTC.

    A naive timestamp seen for the second time is the repeated hour of the
    DST fall-back day and gets fold=1.
    """
    # Live payloads only cover daylight intervals, so this rarely matters.
    seen: set[datetime] = set()
    result = []
    for ts, wh in series:
        if ts.tzinfo is None:
            naive, ts = ts, ts.replace(tzinfo=tz, fold=int(ts in seen))
            seen.add(naive)
        result.append((ts.astimezone(UTC), wh))
    return result


def hourly_kwh(series: Series, tz: tzinfo) -> dict[datetime, float]:
    """Aggregate a series into kWh per UTC hour start."""
    hours: defaultdict[datetime, float] = defaultdict(float)
    for ts, wh in localize(series, tz):
        hours[ts.replace(minute=0, second=0, microsecond=0)] += wh / 1000
    return dict(hours)

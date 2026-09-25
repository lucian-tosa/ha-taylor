"""Pure functions for turning Taylor day payloads into energy series."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from .const import DATA_TYPES

type Series = list[tuple[datetime, float]]

DEFAULT_INTERVAL_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class PanelDay:
    """Per-day cell-string production of one panel, in Wh."""

    panel_id: int
    a: float
    b: float
    c: float

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


def parse_day(payload: dict[str, Any] | None) -> TaylorDay:
    """Parse a day payload, summing data points across all system metrics."""
    if not payload:
        return TaylorDay()

    # Keyed by (timestamp, occurrence) so a timestamp repeated on the DST
    # fall-back day stays two buckets; dict order preserves payload order.
    merged: dict[int, dict[tuple[datetime, int], float]] = defaultdict(dict)
    panels: list[PanelDay] = []

    for metric in payload.get("systemMetrics") or []:
        seen: defaultdict[datetime, int] = defaultdict(int)
        for point in metric.get("dataPoints") or []:
            ts = datetime.fromisoformat(point["timestamp"])
            key = (ts, seen[ts])
            seen[ts] += 1
            for item in point.get("data") or []:
                type_, wh = item.get("type"), item.get("wh")
                if type_ not in DATA_TYPES or wh is None:
                    continue
                buckets = merged[type_]
                buckets[key] = buckets.get(key, 0.0) + wh
        # TODO(verify): whether panelData is present for past days; only today's is used.
        panels.extend(
            PanelDay(
                panel_id=panel["panelId"],
                a=panel.get("cellStringAProductionWh") or 0,
                b=panel.get("cellStringBProductionWh") or 0,
                c=panel.get("cellStringCProductionWh") or 0,
            )
            for panel in metric.get("panelData") or []
        )

    return TaylorDay(
        interval_seconds=payload.get("dayDataPointDurationSeconds") or DEFAULT_INTERVAL_SECONDS,
        series={
            type_: [(ts, wh) for (ts, _), wh in buckets.items()]
            for type_, buckets in sorted(merged.items())
        },
        panels=panels,
    )


def localize(series: Series, tz: tzinfo) -> list[tuple[datetime, float]]:
    """Convert naive site-local bucket starts to UTC.

    A naive timestamp seen for the second time is the repeated hour of the
    DST fall-back day and gets fold=1.
    """
    # TODO(verify): whether Taylor repeats 02:00/02:30 on the fall-back day.
    # Without repeats, the second 02:xx hour is simply absent.
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

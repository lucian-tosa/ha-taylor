"""Tests for the pure parsing functions."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from custom_components.taylor_solar.parsing import PanelDay, hourly_kwh, localize, parse_day

from .conftest import day_payload

AMS = ZoneInfo("Europe/Amsterdam")

SAMPLE = {
    "systemMetrics": [
        {
            "dataPoints": [
                {
                    "timestamp": "2025-06-10T08:00:00",
                    "data": [
                        {"type": 0, "wh": 200},
                        {"type": 4, "wh": 400},
                        {"type": 2, "wh": 800},
                        {"type": 5, "wh": 800},
                        {"type": 3, "wh": 700},
                        {"type": 1, "wh": 1700},
                    ],
                    "solarToGrid": 5,
                }
            ],
            "panelData": [
                {
                    "panelId": 1234,
                    "cellStringAProductionWh": 426,
                    "cellStringBProductionWh": 430,
                    "cellStringCProductionWh": 433,
                    "position": {},
                }
            ],
        }
    ],
    "dayDataPointDurationSeconds": 1800,
}


def test_sample_payload() -> None:
    day = parse_day(SAMPLE)
    ts = datetime(2025, 6, 10, 8)
    assert day.interval_seconds == 1800
    assert day.series == {
        0: [(ts, 200)],
        1: [(ts, 1700)],
        2: [(ts, 800)],
        3: [(ts, 700)],
        4: [(ts, 400)],
        5: [(ts, 800)],
    }
    assert day.panels == [PanelDay(1234, 426, 430, 433)]
    assert day.panels[0].total_wh == 1289
    # 08:00 CEST is 06:00 UTC.
    assert hourly_kwh(day.series[0], AMS) == {datetime(2025, 6, 10, 6, tzinfo=UTC): 0.2}


def test_multiple_system_metrics_are_summed() -> None:
    d = date(2025, 6, 10)
    a = day_payload(d, {"08:00": {0: 100}, "08:30": {0: 50}}, panels=[{"panelId": 1}])
    b = day_payload(d, {"08:00": {0: 10, 1: 5}}, panels=[{"panelId": 2}])
    payload = {"systemMetrics": a["systemMetrics"] + b["systemMetrics"]}
    day = parse_day(payload)
    assert day.series[0] == [(datetime(2025, 6, 10, 8), 110), (datetime(2025, 6, 10, 8, 30), 50)]
    assert day.series[1] == [(datetime(2025, 6, 10, 8), 5)]
    assert [p.panel_id for p in day.panels] == [1, 2]


def test_900_second_buckets() -> None:
    d = date(2025, 6, 10)
    payload = day_payload(
        d, {f"09:{m:02d}": {0: 100} for m in (0, 15, 30, 45)} | {"10:00": {0: 40}}, interval=900
    )
    day = parse_day(payload)
    assert day.interval_seconds == 900
    assert hourly_kwh(day.series[0], AMS) == {
        datetime(2025, 6, 10, 7, tzinfo=UTC): 0.4,
        datetime(2025, 6, 10, 8, tzinfo=UTC): 0.04,
    }


def _full_day(d: date, times: list[str]) -> dict:
    return day_payload(d, {}) | {
        "systemMetrics": [
            {
                "dataPoints": [
                    {"timestamp": f"{d.isoformat()}T{t}:00", "data": [{"type": 0, "wh": 1000}]}
                    for t in times
                ]
            }
        ]
    }


def test_dst_fall_back_repeats() -> None:
    d = date(2026, 10, 25)
    times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
    times[6:6] = ["02:00", "02:30"]  # repeated hour after 02:30 CEST
    assert times[4:8] == ["02:00", "02:30", "02:00", "02:30"]
    day = parse_day(_full_day(d, times))
    assert len(day.series[0]) == 50

    utc = [ts for ts, _ in localize(day.series[0], AMS)]
    assert utc[4:8] == [
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    ]
    assert len(set(utc)) == 50

    hours = hourly_kwh(day.series[0], AMS)
    assert len(hours) == 25
    assert set(hours.values()) == {2.0}
    assert min(hours) == datetime(2026, 10, 24, 22, tzinfo=UTC)
    assert max(hours) == datetime(2026, 10, 25, 22, tzinfo=UTC)


def test_dst_spring_forward() -> None:
    d = date(2026, 3, 29)
    times = [f"{h:02d}:{m:02d}" for h in range(24) if h != 2 for m in (0, 30)]
    hours = hourly_kwh(parse_day(_full_day(d, times)).series[0], AMS)
    assert len(hours) == 23
    assert set(hours.values()) == {2.0}
    assert min(hours) == datetime(2026, 3, 28, 23, tzinfo=UTC)
    assert max(hours) == datetime(2026, 3, 29, 21, tzinfo=UTC)


def test_empty_payloads() -> None:
    for payload in (None, {}, {"systemMetrics": []}, {"systemMetrics": [{"dataPoints": []}]}):
        day = parse_day(payload)
        assert day.series == {}
        assert day.panels == []
        assert day.interval_seconds == 1800

"""Unit tests for analyst helpers."""

from datetime import datetime, timezone

from egauge_mcp.analysis import (
    compare_periods,
    find_peak_demand,
    parse_iso_or_relative,
    period_summary,
    tariff_estimate,
)
from egauge_mcp.client import HistoricalRow


def _ts(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def _hourly(start_year: int = 2026, start_month: int = 1, days: int = 7,
            base_w: float = 1000.0, peak_at_hour: int = 18, peak_w: float = 5000.0) -> list[HistoricalRow]:
    """24×days hourly rows. Off-peak base power, peak at the given hour."""
    rows = []
    for d in range(days):
        for h in range(24):
            ts = _ts(start_year, start_month, 1 + d, h)
            w = peak_w if h == peak_at_hour else base_w
            rows.append(HistoricalRow(timestamp=ts, values={"Mains": w}))
    return rows


def test_period_summary_basic():
    rows = _hourly(days=1, base_w=1000, peak_at_hour=18, peak_w=5000)
    s = period_summary(rows)
    # 23h × 1000W + 1h × 5000W = 28 kWh
    assert s["total_kwh"] == 28.0
    assert s["peak_kw"] == 5.0
    assert s["average_kw"] == round(28.0 / 24, 2)


def test_find_peak_demand_returns_top_n():
    rows = _hourly(days=2, base_w=500, peak_at_hour=14, peak_w=8000)
    out = find_peak_demand(rows, top_n=3)
    assert len(out["top_peaks"]) == 3
    assert out["top_peaks"][0]["total_kw"] == 8.0


def test_compare_periods_computes_deltas():
    a = _hourly(start_month=1, days=1, base_w=1000, peak_at_hour=18, peak_w=4000)
    b = _hourly(start_month=2, days=1, base_w=1500, peak_at_hour=18, peak_w=6000)
    out = compare_periods(a, b, label_a="Jan", label_b="Feb")
    cmp = out["comparison"]
    assert cmp["total_kwh"]["delta_kwh"] > 0
    assert cmp["peak_kw"]["delta_kw"] == 2.0  # 6kW - 4kW


def test_tariff_estimate_splits_peak_offpeak():
    # 1 day, base 1kW always, peak 5kW at hour 14 (inside 7-22 peak window)
    rows = _hourly(days=1, base_w=1000, peak_at_hour=14, peak_w=5000)
    out = tariff_estimate(
        rows, peak_rate=20.0, offpeak_rate=10.0,
        peak_hours=(7, 22), demand_rate_per_kw=100.0,
        fixed_charge=50.0, currency="KES",
    )
    # Peak window 07:00-21:00 inclusive (15h): 14 hrs × 1kW + 1 hr × 5kW = 19 kWh
    # Off-peak (00-06 + 22-23): 9 hrs × 1kW = 9 kWh
    assert out["peak_kwh"] == 19.0
    assert out["offpeak_kwh"] == 9.0
    assert out["peak_demand_kw"] == 5.0
    assert out["energy_charge"] == 19.0 * 20.0 + 9.0 * 10.0
    assert out["demand_charge"] == 5.0 * 100.0
    assert out["fixed_charge"] == 50.0
    assert out["total"] == out["energy_charge"] + out["demand_charge"] + out["fixed_charge"]


def test_parse_relative_formats():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    assert parse_iso_or_relative("now", now=now) == int(now.timestamp())
    assert parse_iso_or_relative("24h ago", now=now) == int(now.timestamp()) - 86400
    assert parse_iso_or_relative("7d ago", now=now) == int(now.timestamp()) - 7 * 86400
    assert parse_iso_or_relative("6mo ago", now=now) == int(now.timestamp()) - 6 * 2629800
    assert parse_iso_or_relative("1y ago", now=now) == int(now.timestamp()) - 31557600
    assert parse_iso_or_relative("30 days ago", now=now) == int(now.timestamp()) - 30 * 86400


def test_parse_iso_format():
    assert parse_iso_or_relative("2026-04-01T00:00:00Z") == int(
        datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()
    )

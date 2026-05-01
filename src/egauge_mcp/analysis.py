"""
Analyst helpers — turn raw HistoricalRow lists into the kind of output an
energy analyst would ship in a report.

All functions are pure and pandas-backed. They accept a list[HistoricalRow]
plus inputs and return JSON-serializable dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import pandas as pd

from .client import HistoricalRow


def to_dataframe(rows: Sequence[HistoricalRow]) -> pd.DataFrame:
    """Wide DataFrame: index = UTC timestamp, columns = register names, values = avg watts."""
    if not rows:
        return pd.DataFrame()
    records = []
    for r in rows:
        rec = {"timestamp": pd.Timestamp(r.timestamp, unit="s", tz="UTC")}
        rec.update(r.values)
        records.append(rec)
    df = pd.DataFrame(records).set_index("timestamp").sort_index()
    return df


def _interval_hours(df: pd.DataFrame) -> float:
    """Average spacing between rows, in hours. Falls back to 0.25h (15min) if unknown."""
    if len(df) < 2:
        return 0.25
    diffs = df.index.to_series().diff().dropna().dt.total_seconds()
    return float(diffs.median() / 3600.0) if not diffs.empty else 0.25


def period_summary(rows: Sequence[HistoricalRow]) -> dict:
    """Totals + peak + load factor + per-register share."""
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data in range"}

    hours = _interval_hours(df)
    kwh_per_register = (df.sum() * hours / 1000.0).round(2).to_dict()
    total_kwh = round(sum(kwh_per_register.values()), 2)

    # Peak across all registers (sum at each timestamp)
    summed = df.sum(axis=1)
    peak_w = float(summed.max())
    peak_ts = summed.idxmax().isoformat()
    avg_w = float(summed.mean())
    load_factor = round(avg_w / peak_w, 3) if peak_w > 0 else 0.0

    share = {
        name: round((kwh_per_register[name] / total_kwh) * 100, 1) if total_kwh > 0 else 0.0
        for name in kwh_per_register
    }

    return {
        "start": df.index.min().isoformat(),
        "end": df.index.max().isoformat(),
        "interval_hours": round(hours, 4),
        "rows": int(len(df)),
        "total_kwh": total_kwh,
        "kwh_per_register": kwh_per_register,
        "register_share_pct": share,
        "peak_kw": round(peak_w / 1000.0, 2),
        "peak_at": peak_ts,
        "average_kw": round(avg_w / 1000.0, 2),
        "load_factor": load_factor,
    }


def find_peak_demand(rows: Sequence[HistoricalRow], top_n: int = 5) -> dict:
    """Top N peak intervals across the period, with per-register breakdown."""
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data"}
    summed = df.sum(axis=1).sort_values(ascending=False).head(top_n)
    peaks = []
    for ts, total_w in summed.items():
        breakdown = {col: round(float(df.loc[ts, col]) / 1000.0, 2) for col in df.columns}
        peaks.append({
            "at": ts.isoformat(),
            "total_kw": round(float(total_w) / 1000.0, 2),
            "by_register_kw": breakdown,
        })
    return {"top_peaks": peaks}


def find_phantom_loads(rows: Sequence[HistoricalRow], night_hours: tuple[int, int] = (1, 5)) -> dict:
    """
    Estimate always-on baseline per register.

    Method: take the 5th-percentile reading during overnight hours (default 01:00–05:00 local UTC).
    A high baseline relative to daytime average indicates phantom loads.
    """
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data"}
    h = df.index.hour
    start, end = night_hours
    mask = (h >= start) & (h < end)
    night = df[mask]
    if night.empty:
        return {"error": f"no data in night window {start:02d}-{end:02d}"}

    interval_h = _interval_hours(df)
    out: dict[str, dict] = {}
    for col in df.columns:
        baseline_w = float(np.percentile(night[col].dropna(), 5))
        avg_w = float(df[col].mean())
        share_pct = round((baseline_w / avg_w) * 100, 1) if avg_w > 0 else 0.0
        annual_kwh = round(baseline_w * 8760 / 1000.0, 1)
        out[col] = {
            "baseline_w": round(baseline_w, 1),
            "avg_w": round(avg_w, 1),
            "phantom_share_pct_of_total": share_pct,
            "implied_annual_kwh_if_constant": annual_kwh,
        }
    return {"night_window_utc": f"{start:02d}-{end:02d}", "interval_hours": round(interval_h, 4), "registers": out}


def compare_periods(
    period_a: Sequence[HistoricalRow],
    period_b: Sequence[HistoricalRow],
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """Side-by-side comparison: totals, peak, deltas in absolute and percentage terms."""
    a = period_summary(period_a)
    b = period_summary(period_b)
    if "error" in a or "error" in b:
        return {"a": a, "b": b}

    def pct(new: float, old: float) -> float:
        return round(((new - old) / old) * 100, 1) if old else 0.0

    delta = {
        "total_kwh": {
            label_a: a["total_kwh"],
            label_b: b["total_kwh"],
            "delta_kwh": round(b["total_kwh"] - a["total_kwh"], 2),
            "delta_pct": pct(b["total_kwh"], a["total_kwh"]),
        },
        "peak_kw": {
            label_a: a["peak_kw"],
            label_b: b["peak_kw"],
            "delta_kw": round(b["peak_kw"] - a["peak_kw"], 2),
            "delta_pct": pct(b["peak_kw"], a["peak_kw"]),
        },
        "average_kw": {
            label_a: a["average_kw"],
            label_b: b["average_kw"],
            "delta_kw": round(b["average_kw"] - a["average_kw"], 2),
            "delta_pct": pct(b["average_kw"], a["average_kw"]),
        },
        "load_factor": {label_a: a["load_factor"], label_b: b["load_factor"]},
    }
    return {"summary_a": a, "summary_b": b, "comparison": delta}


def detect_anomalies(rows: Sequence[HistoricalRow], sigma: float = 2.0) -> dict:
    """
    Flag intervals where total power exceeds the mean ± sigma·std for that hour-of-week.
    Needs at least 2 weeks of data to be meaningful.
    """
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data"}
    summed = df.sum(axis=1).rename("total_w").to_frame()
    summed["how"] = summed.index.dayofweek * 24 + summed.index.hour

    stats = summed.groupby("how")["total_w"].agg(["mean", "std"]).fillna(0)
    summed = summed.join(stats, on="how")
    summed["upper"] = summed["mean"] + sigma * summed["std"]
    summed["lower"] = (summed["mean"] - sigma * summed["std"]).clip(lower=0)
    summed["is_high"] = summed["total_w"] > summed["upper"]
    summed["is_low"] = summed["total_w"] < summed["lower"]

    anomalies = []
    for ts, row in summed[summed["is_high"] | summed["is_low"]].iterrows():
        anomalies.append({
            "at": ts.isoformat(),
            "total_kw": round(float(row["total_w"]) / 1000.0, 2),
            "expected_kw": round(float(row["mean"]) / 1000.0, 2),
            "kind": "high" if row["is_high"] else "low",
        })
    return {
        "sigma": sigma,
        "samples": int(len(summed)),
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[:50],  # cap to avoid flooding
    }


def solar_self_consumption(
    rows: Sequence[HistoricalRow],
    solar_register: str,
    grid_register: str,
) -> dict:
    """
    Compute solar self-consumption %.

    Assumes solar register reads positive when generating, and grid register
    reads negative when exporting (typical eGauge convention) — but we use
    absolute values defensively.
    """
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data"}
    if solar_register not in df.columns or grid_register not in df.columns:
        return {"error": f"need both '{solar_register}' and '{grid_register}' in data"}

    hours = _interval_hours(df)
    solar_kwh = float((df[solar_register].clip(lower=0).sum() * hours) / 1000.0)
    # Net export = grid reading negative → use clip(upper=0).abs()
    export_kwh = float((df[grid_register].clip(upper=0).abs().sum() * hours) / 1000.0)
    self_used_kwh = max(solar_kwh - export_kwh, 0.0)
    pct = round((self_used_kwh / solar_kwh) * 100, 1) if solar_kwh > 0 else 0.0

    return {
        "solar_generation_kwh": round(solar_kwh, 2),
        "exported_to_grid_kwh": round(export_kwh, 2),
        "self_consumed_kwh": round(self_used_kwh, 2),
        "self_consumption_pct": pct,
    }


def tariff_estimate(
    rows: Sequence[HistoricalRow],
    peak_rate: float,
    offpeak_rate: float | None = None,
    peak_hours: tuple[int, int] = (7, 22),
    demand_rate_per_kw: float = 0.0,
    fixed_charge: float = 0.0,
    currency: str = "KES",
) -> dict:
    """
    Apply a tariff to the consumption data.

    - peak_rate: per-kWh during peak window (peak_hours[0] ≤ hour < peak_hours[1])
    - offpeak_rate: per-kWh outside the peak window (defaults to peak_rate)
    - demand_rate_per_kw: charged on the maximum kW reading in the period (KPLC-style)
    - fixed_charge: flat addition (meter rent, taxes you treat as fixed, etc.)
    """
    if offpeak_rate is None:
        offpeak_rate = peak_rate
    df = to_dataframe(rows)
    if df.empty:
        return {"error": "no data"}

    hours = _interval_hours(df)
    summed_kw = df.sum(axis=1) / 1000.0
    in_peak = (df.index.hour >= peak_hours[0]) & (df.index.hour < peak_hours[1])

    peak_kwh = float((summed_kw[in_peak] * hours).sum())
    offpeak_kwh = float((summed_kw[~in_peak] * hours).sum())
    peak_demand_kw = float(summed_kw.max())

    energy_charge = peak_kwh * peak_rate + offpeak_kwh * offpeak_rate
    demand_charge = peak_demand_kw * demand_rate_per_kw
    total = energy_charge + demand_charge + fixed_charge

    return {
        "currency": currency,
        "peak_window": f"{peak_hours[0]:02d}-{peak_hours[1]:02d}",
        "peak_kwh": round(peak_kwh, 2),
        "offpeak_kwh": round(offpeak_kwh, 2),
        "peak_demand_kw": round(peak_demand_kw, 2),
        "energy_charge": round(energy_charge, 2),
        "demand_charge": round(demand_charge, 2),
        "fixed_charge": round(fixed_charge, 2),
        "total": round(total, 2),
    }


_UNIT_SECS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
    "mo": 2629800, "month": 2629800, "months": 2629800,  # 30.44 days avg
    "y": 31557600, "yr": 31557600, "year": 31557600, "years": 31557600,
}


def parse_iso_or_relative(value: str, now: datetime | None = None) -> int:
    """
    Convert ISO-8601 OR relative ('7d ago', '6mo ago', '5y ago', '24h ago', 'now')
    to a UNIX timestamp in UTC seconds.

    Note: 'mo' and 'm' are distinct — 'mo' is months, 'm' is minutes.
    """
    import re

    now = now or datetime.now(timezone.utc)
    v = value.strip().lower()
    if v in ("now", ""):
        return int(now.timestamp())
    if v.endswith(" ago"):
        v = v[:-4].strip()
    elif v.endswith("ago"):
        v = v[:-3].strip()

    # Match "<number><unit>" with optional space, longest unit first
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*", v)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        if unit in _UNIT_SECS:
            return int(now.timestamp() - n * _UNIT_SECS[unit])

    # Try ISO
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError as e:
        raise ValueError(
            f"cannot parse time: {value!r} (try ISO-8601 or '7d ago', '6mo ago', '1y ago')"
        ) from e

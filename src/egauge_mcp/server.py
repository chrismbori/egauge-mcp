"""
eGauge MCP server — turns Claude into a live energy analyst.

Exposes eGauge devices as MCP tools. Works with any eGauge device given
its public id (egaugeXXXXX) — config file just adds friendly names,
credentials, and register aliases.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .analysis import (
    compare_periods as _compare_periods,
    detect_anomalies as _detect_anomalies,
    find_peak_demand as _find_peak_demand,
    find_phantom_loads as _find_phantom_loads,
    parse_iso_or_relative,
    period_summary,
    solar_self_consumption as _solar_self_consumption,
    tariff_estimate as _tariff_estimate,
)
from .client import EGaugeClient, EGaugeError, HistoricalRow
from .config import Config, DeviceConfig, load_config

mcp = FastMCP("egauge-mcp")

_config: Config = load_config()


def _client_for(device: str) -> tuple[EGaugeClient, DeviceConfig]:
    cfg = _config.get(device)
    client = EGaugeClient(
        device_id=cfg.id,
        username=cfg.username,
        password=cfg.password,
    )
    return client, cfg


def _resolve_register_filter(cfg: DeviceConfig, names: list[str] | None) -> list[str] | None:
    if not names:
        return None
    return [cfg.resolve_register(n) for n in names]


def _fetch_window(
    device: str,
    start: str,
    end: str,
    register_aliases: list[str] | None = None,
) -> tuple[list[HistoricalRow], DeviceConfig, dict]:
    """Fetch rows covering [start, end] and trim to the window.

    Returns (rows, config, meta) where meta describes archive resolution and
    any warnings about coarse data — important because older eGauge firmware
    has 61-day archive buckets and short windows return zero rows.
    """
    client, cfg = _client_for(device)
    start_ts = parse_iso_or_relative(start)
    end_ts = parse_iso_or_relative(end)
    if start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts

    duration = end_ts - start_ts
    # Cap at 4320 — empirically some firmware returns empty XML for n>~4500.
    # The client's fallback chain handles short-archive devices anyway.
    rows_needed = min(max(int(duration // 60), 240), 4320)
    end_param = end_ts if end_ts < int(datetime.now(timezone.utc).timestamp()) - 60 else 0

    filt = _resolve_register_filter(cfg, register_aliases)
    raw_rows = client.get_historical(
        rows=rows_needed,
        step_seconds=60,
        end_timestamp=end_param,
        register_names=filt,
    )

    rows = [r for r in raw_rows if start_ts <= r.timestamp <= end_ts]
    rows.sort(key=lambda r: r.timestamp)

    meta: dict = {}
    if raw_rows:
        if len(raw_rows) >= 2:
            archive_interval = abs(raw_rows[0].timestamp - raw_rows[1].timestamp)
            meta["archive_interval_seconds"] = archive_interval
            meta["archive_interval_human"] = _humanize_seconds(archive_interval)
        if not rows:
            oldest = min(r.timestamp for r in raw_rows)
            newest = max(r.timestamp for r in raw_rows)
            meta["warning"] = (
                f"No data in requested window. Device archive only covers "
                f"{datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat()} to "
                f"{datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()} "
                f"at {meta.get('archive_interval_human', 'unknown')} resolution. "
                f"Try a wider window — older eGauge firmware stores fine-grained "
                f"data only in RAM (visible on the device dashboard, not the XML API)."
            )
    return rows, cfg, meta


def _humanize_seconds(s: int) -> str:
    if s >= 86400:
        return f"{s/86400:.1f} days"
    if s >= 3600:
        return f"{s/3600:.1f} hours"
    if s >= 60:
        return f"{s/60:.1f} minutes"
    return f"{s} seconds"


# ─── Discovery / config tools ────────────────────────────────────────────────


@mcp.tool()
def list_devices() -> dict[str, Any]:
    """List every eGauge device configured in ~/.egauge-mcp/config.toml.

    Returns id, friendly name, and any register aliases. Devices not in
    config can still be queried by raw id (e.g. `egauge12345`).
    """
    devices = []
    for d in _config.devices.values():
        devices.append({
            "id": d.id,
            "name": d.name,
            "registers": d.registers,
            "url": EGaugeClient._resolve_base_url(d.id),
            "has_credentials": bool(d.username and d.password),
        })
    return {"count": len(devices), "devices": devices, "config_path": str(_config.path)}


@mcp.tool()
def add_device(
    id: str,
    name: str | None = None,
    username: str | None = None,
    password: str | None = None,
    registers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Register a new eGauge device in the config file.

    Args:
        id: eGauge device id (e.g. "egauge12345") or full hostname/URL.
        name: Friendly label (e.g. "Wildfire Lake Pump").
        username: Optional Digest-auth username.
        password: Optional Digest-auth password.
        registers: Optional alias map, e.g. {"Lake Pump Grid": "Grid"}.
    """
    dev = DeviceConfig(
        id=id,
        name=name,
        username=username,
        password=password,
        registers=dict(registers or {}),
    )
    _config.add_device(dev)
    return {"ok": True, "id": id, "saved_to": str(_config.path)}


@mcp.tool()
def get_registers(device: str) -> dict[str, Any]:
    """Get the list of registers (channels) reported by a device, with current values.

    Args:
        device: eGauge id (e.g. "egauge12345") or friendly name from config.
    """
    client, cfg = _client_for(device)
    live = client.get_live()
    out = []
    for r in live.registers:
        out.append({
            "name": r.name,
            "type": r.type,
            "type_label": _type_label(r.type),
            "current_value": r.instantaneous,
            "cumulative": r.cumulative,
        })
    return {
        "device": cfg.display_name(),
        "device_id": cfg.id,
        "timestamp": datetime.fromtimestamp(live.timestamp, tz=timezone.utc).isoformat(),
        "register_count": len(out),
        "registers": out,
        "aliases": cfg.registers,
    }


def _type_label(t: str) -> str:
    return {
        "P": "power (watts)",
        "Pa": "apparent power (VA)",
        "S": "apparent power (VA)",
        "V": "voltage",
        "A": "current (amps)",
        "F": "frequency (Hz)",
        "Q": "reactive power (VAR)",
        "PF": "power factor",
        "T": "temperature",
    }.get(t, t)


# ─── Live data ────────────────────────────────────────────────────────────────


@mcp.tool()
def get_live(device: str) -> dict[str, Any]:
    """Get the current instantaneous power reading per register, right now.

    Args:
        device: eGauge id or friendly name.
    """
    client, cfg = _client_for(device)
    live = client.get_live()
    power_registers = [r for r in live.registers if r.type == "P"]
    total_kw = round(sum(r.instantaneous for r in power_registers) / 1000.0, 3)
    return {
        "device": cfg.display_name(),
        "at": datetime.fromtimestamp(live.timestamp, tz=timezone.utc).isoformat(),
        "total_kw": total_kw,
        "registers": {r.name: round(r.instantaneous, 1) for r in live.registers},
    }


# ─── Historical / analyst tools ──────────────────────────────────────────────


@mcp.tool()
def get_history(
    device: str,
    start: str,
    end: str = "now",
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Get raw historical rows (average watts per register per interval).

    Args:
        device: eGauge id or friendly name.
        start: ISO-8601 timestamp OR relative phrase like '24h ago', '7d ago', '30d ago'.
        end: Same format as start. Defaults to 'now'.
        registers: Optional list of register names or aliases to include.
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    return {
        "device": cfg.display_name(),
        "start": start,
        "end": end,
        "row_count": len(rows),
        **meta,
        "rows": [
            {
                "at": datetime.fromtimestamp(r.timestamp, tz=timezone.utc).isoformat(),
                **{name: round(v, 1) for name, v in r.values.items()},
            }
            for r in rows
        ],
    }


@mcp.tool()
def analyze_period(
    device: str,
    start: str,
    end: str = "now",
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Energy-analyst summary for a period: total kWh, peak kW, load factor, per-register share.

    Args:
        device: eGauge id or friendly name.
        start: e.g. '30d ago', '2026-04-01T00:00:00Z'.
        end: defaults to 'now'.
        registers: Optional filter — register names or aliases.
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    summary = period_summary(rows)
    return {"device": cfg.display_name(), **meta, **summary}


@mcp.tool()
def find_peak_demand(
    device: str,
    start: str,
    end: str = "now",
    top_n: int = 5,
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Find the highest-demand intervals in a period, with per-register breakdown.

    Useful for understanding what's driving demand charges on a KPLC bill.
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    return {"device": cfg.display_name(), **meta, **_find_peak_demand(rows, top_n=top_n)}


@mcp.tool()
def find_phantom_loads(
    device: str,
    start: str,
    end: str = "now",
    night_start_hour: int = 1,
    night_end_hour: int = 5,
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Estimate always-on baseline (phantom load) per register from overnight readings.

    Args:
        night_start_hour: UTC hour to start the night window (default 01).
        night_end_hour: UTC hour to end (default 05).
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    return {
        "device": cfg.display_name(),
        **meta,
        **_find_phantom_loads(rows, night_hours=(night_start_hour, night_end_hour)),
    }


@mcp.tool()
def compare_periods(
    device: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    label_a: str = "Period A",
    label_b: str = "Period B",
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Compare two time periods on the same device — totals, peaks, deltas.

    Examples: this week vs last week, this month vs same month last year.
    """
    rows_a, cfg, meta_a = _fetch_window(device, period_a_start, period_a_end, registers)
    rows_b, _, meta_b = _fetch_window(device, period_b_start, period_b_end, registers)
    notes = {k: v for k, v in {**meta_a, **meta_b}.items() if k.startswith("warning") or k == "archive_interval_human"}
    return {"device": cfg.display_name(), **notes, **_compare_periods(rows_a, rows_b, label_a, label_b)}


@mcp.tool()
def detect_anomalies(
    device: str,
    start: str,
    end: str = "now",
    sigma: float = 2.0,
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Flag intervals where total power deviates >sigma·std from the mean for that hour-of-week.

    Needs at least 2 weeks of data to be meaningful.
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    return {"device": cfg.display_name(), **meta, **_detect_anomalies(rows, sigma=sigma)}


@mcp.tool()
def solar_self_consumption(
    device: str,
    solar_register: str,
    grid_register: str,
    start: str,
    end: str = "now",
) -> dict[str, Any]:
    """Compute what % of solar generation was self-consumed vs exported to grid.

    Args:
        solar_register: Register name (or alias) measuring solar generation.
        grid_register: Register measuring grid import/export (negative = export).
    """
    _, cfg = _client_for(device)
    solar_real = cfg.resolve_register(solar_register)
    grid_real = cfg.resolve_register(grid_register)
    rows, _, meta = _fetch_window(device, start, end, [solar_real, grid_real])
    return {"device": cfg.display_name(), **meta, **_solar_self_consumption(rows, solar_real, grid_real)}


@mcp.tool()
def tariff_estimate(
    device: str,
    start: str,
    end: str,
    peak_rate: float,
    offpeak_rate: float | None = None,
    peak_hour_start: int = 7,
    peak_hour_end: int = 22,
    demand_rate_per_kw: float = 0.0,
    fixed_charge: float = 0.0,
    currency: str = "KES",
    registers: list[str] | None = None,
) -> dict[str, Any]:
    """Estimate a utility bill from metered consumption.

    Supports KPLC-style tariffs with energy + demand charges. peak_rate is
    applied during the peak hour window; offpeak_rate (defaults to peak_rate)
    is applied otherwise. demand_rate_per_kw is multiplied by the period's
    peak kW.
    """
    rows, cfg, meta = _fetch_window(device, start, end, registers)
    return {
        "device": cfg.display_name(),
        **meta,
        **_tariff_estimate(
            rows,
            peak_rate=peak_rate,
            offpeak_rate=offpeak_rate,
            peak_hours=(peak_hour_start, peak_hour_end),
            demand_rate_per_kw=demand_rate_per_kw,
            fixed_charge=fixed_charge,
            currency=currency,
        ),
    }


@mcp.tool()
def device_status(device: str) -> dict[str, Any]:
    """Quick health check: is the device online, what's its current total kW, and how many registers."""
    client, cfg = _client_for(device)
    try:
        live = client.get_live()
    except EGaugeError as e:
        return {"device": cfg.display_name(), "online": False, "error": str(e)}
    power = [r for r in live.registers if r.type == "P"]
    return {
        "device": cfg.display_name(),
        "device_id": cfg.id,
        "online": True,
        "at": datetime.fromtimestamp(live.timestamp, tz=timezone.utc).isoformat(),
        "register_count": len(live.registers),
        "power_register_count": len(power),
        "total_kw_now": round(sum(r.instantaneous for r in power) / 1000.0, 3),
    }


def main() -> None:
    """Entry point — runs the MCP server over stdio."""
    # Allow log level override
    if os.environ.get("EGAUGE_MCP_DEBUG"):
        import logging
        logging.basicConfig(level=logging.DEBUG)
    mcp.run()


if __name__ == "__main__":
    main()

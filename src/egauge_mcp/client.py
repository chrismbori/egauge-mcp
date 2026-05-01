"""
eGauge XML API client.

Devices use the older XML API (not the newer JSON WebAPI).
Authentication: HTTP Digest (MD5). Many read-only endpoints accept anonymous access.

Endpoints:
  GET /cgi-bin/egauge?tot&inst       → real-time cumulative + instantaneous values
  GET /cgi-bin/egauge-show?n=N&s=S   → historical stored data (columnar XML)

Critical parsing rules (verified against eagles-portal/apps/api/src/egauge/client.ts):
  - <data> may appear multiple times in one response; treat as list.
  - Each <data> may contain one or many <r> rows.
  - Column headers (<cname>) live on the first <data> block.
  - time_stamp is hex; time_delta is the interval BETWEEN consecutive rows
    (NOT the total archive span — the device ignores the requested `s` param
    and returns its native archive resolution).
  - Cumulative values are watt-seconds. Average watts = (curr - next) / time_delta.
  - Trailing rows may be duplicates from before device install — dedupe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from xml.etree import ElementTree as ET

import httpx


@dataclass(frozen=True)
class RegisterReading:
    name: str
    type: str  # P=power, V=voltage, A=current, F=frequency, S=apparent
    instantaneous: float  # current value (watts for P-type)
    cumulative: int  # watt-seconds (or appropriate unit-seconds)


@dataclass(frozen=True)
class LiveReadings:
    timestamp: int  # unix seconds
    registers: list[RegisterReading]


@dataclass(frozen=True)
class HistoricalRow:
    timestamp: int  # unix seconds
    values: dict[str, float]  # register_name -> average watts in interval


class EGaugeError(Exception):
    """Raised for any eGauge HTTP / parsing failure."""


class EGaugeClient:
    """Client for one eGauge device.

    `device_id` may be:
      - a short id like "egauge12345" (auto-resolves to https://egauge12345.egaug.es)
      - a full hostname like "egauge12345.egaug.es"
      - a full URL like "http://192.168.1.50" (for local-network devices)
    """

    def __init__(
        self,
        device_id: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.device_id = device_id
        self.username = username
        self.password = password
        self.timeout = timeout
        self._base_url = self._resolve_base_url(device_id)

    @staticmethod
    def _resolve_base_url(device_id: str) -> str:
        d = device_id.strip().rstrip("/")
        if d.startswith(("http://", "https://")):
            return d
        if "." not in d:
            # Bare device id like "egauge12345"
            return f"https://{d}.egaug.es"
        return f"https://{d}"

    def _auth(self) -> httpx.Auth | None:
        if self.username and self.password:
            return httpx.DigestAuth(self.username, self.password)
        return None

    def _fetch(self, path: str) -> str:
        """GET with HTTPS-then-HTTP fallback. No empty-body retry here —
        that lives in get_historical, which knows it's safe to alter the URL."""
        urls = [f"{self._base_url}{path}"]
        if self._base_url.startswith("https://"):
            urls.append(f"http://{self._base_url[len('https://'):]}{path}")

        last_err: Exception | None = None
        for url in urls:
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                    r = c.get(url, auth=self._auth())
                    if r.status_code == 401 and self._auth() is None:
                        raise EGaugeError(
                            f"{self.device_id} requires authentication; set username/password"
                        )
                    r.raise_for_status()
                    return r.text
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_err = e
                continue
            except httpx.HTTPStatusError as e:
                raise EGaugeError(f"{self.device_id} HTTP {e.response.status_code} on {path}") from e
        raise EGaugeError(f"{self.device_id} unreachable: {last_err}")

    # ── Public API ──────────────────────────────────────────────────────────

    def is_online(self) -> bool:
        try:
            self._fetch("/cgi-bin/egauge?tot")
            return True
        except EGaugeError:
            return False

    def get_live(self) -> LiveReadings:
        xml = self._fetch("/cgi-bin/egauge?tot&inst")
        return parse_live_xml(xml)

    # Some firmware returns empty XML for specific (n, s) combos.
    # These are ordered most-useful first; we fall through until one yields rows.
    _N_FALLBACKS: tuple[int, ...] = (4320, 2880, 960, 240, 60)

    def get_historical(
        self,
        rows: int = 4320,
        step_seconds: int = 60,
        end_timestamp: int = 0,
        register_names: Iterable[str] | None = None,
    ) -> list[HistoricalRow]:
        """
        Fetch time-series rows ordered newest→oldest, then convert to average watts.

        Note: the device IGNORES `step_seconds` on most firmware and returns its
        native archive resolution — we trust `time_delta` from the response.
        Some firmware also returns empty XML for specific `n` values; we retry
        with a series of known-good fallbacks if that happens.

        Do NOT add &c or &C — those switch the device to CSV output mode.
        """
        names_filter = list(register_names) if register_names else None

        candidates: list[int] = [rows]
        for n in self._N_FALLBACKS:
            if n != rows and n not in candidates:
                candidates.append(n)

        last_err: Exception | None = None
        for n in candidates:
            path = f"/cgi-bin/egauge-show?n={n}&s={step_seconds}"
            if end_timestamp > 0:
                path += f"&m={end_timestamp}"
            try:
                xml = self._fetch(path)
            except EGaugeError as e:
                last_err = e
                continue
            if "<r>" not in xml and "<r " not in xml:
                continue  # empty — try next candidate
            try:
                return parse_historical_xml(xml, names_filter)
            except EGaugeError as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return []


# ── XML parsers ─────────────────────────────────────────────────────────────


def parse_live_xml(xml: str) -> LiveReadings:
    root = ET.fromstring(xml)
    # Live response has <data> at root or as child; find first <data>.
    data = root if root.tag == "data" else root.find(".//data")
    if data is None:
        raise EGaugeError("live XML: missing <data>")

    # Live XML carries the timestamp as a <ts> child element (decimal seconds).
    # Older firmware variants put it in a `time_stamp` attribute (hex) or `ts` attribute.
    ts_text = (data.findtext("ts") or "").strip()
    ts_attr = data.get("time_stamp") or data.get("ts") or ""
    candidate = ts_text or ts_attr or "0"
    try:
        if candidate.lower().startswith("0x"):
            timestamp = int(candidate, 16)
        elif candidate.isdigit():
            timestamp = int(candidate)
        else:
            timestamp = int(candidate, 16)
    except ValueError:
        timestamp = 0

    registers: list[RegisterReading] = []
    for r in data.findall("r"):
        name = (r.get("n") or "").strip()
        rtype = r.get("t") or "P"
        if not name:
            continue
        v_text = (r.findtext("v") or "0").strip()
        i_text = (r.findtext("i") or "0").strip()
        try:
            cumulative = int(v_text)
        except ValueError:
            cumulative = 0
        try:
            instantaneous = float(i_text)
        except ValueError:
            instantaneous = 0.0
        registers.append(
            RegisterReading(name=name, type=rtype, instantaneous=instantaneous, cumulative=cumulative)
        )

    return LiveReadings(timestamp=timestamp, registers=registers)


def parse_historical_xml(xml: str, filter_names: list[str] | None = None) -> list[HistoricalRow]:
    """
    Parse egauge-show response.

    The first <data> block carries column headers (<cname>) and the most-recent
    row's timestamp. time_delta is the seconds between consecutive rows.
    """
    root = ET.fromstring(xml)
    group = root if root.tag == "group" else root.find(".//group")
    if group is None:
        raise EGaugeError("historical XML: missing <group>")

    data_blocks = group.findall("data")
    if not data_blocks:
        raise EGaugeError("historical XML: missing <data>")

    first = data_blocks[0]
    cnames = first.findall("cname")
    columns: list[tuple[str, str]] = []
    for c in cnames:
        col_name = (c.text or "").strip()
        col_type = c.get("t") or "P"
        columns.append((col_name, col_type))

    if not columns:
        raise EGaugeError("historical XML: missing <cname> columns")

    # Determine which columns to surface.
    filter_lower = {n.lower() for n in filter_names} if filter_names else None
    wanted_idx: list[int] = []
    for i, (name, ctype) in enumerate(columns):
        if filter_lower is not None:
            if name.lower() in filter_lower:
                wanted_idx.append(i)
        elif ctype == "P":
            wanted_idx.append(i)

    if not wanted_idx:
        # Fallback: include everything if filter excluded all
        wanted_idx = list(range(len(columns)))

    # Time bookkeeping from the first block
    ts_attr = first.get("time_stamp") or "0"
    try:
        end_ts = int(ts_attr, 16)
    except ValueError:
        end_ts = 0
    try:
        time_delta = int(first.get("time_delta") or "0")
    except ValueError:
        time_delta = 0
    if time_delta <= 0:
        # Fall back if device omitted it
        time_delta = 900

    # Flatten rows newest→oldest, deduping consecutive identical rows
    all_rows: list[list[int]] = []
    last_key: str | None = None
    for block in data_blocks:
        for r in block.findall("r"):
            cells = [int((c.text or "0").strip()) for c in r.findall("c")]
            key = ",".join(str(x) for x in cells[:5])
            if key != last_key:
                all_rows.append(cells)
                last_key = key

    rows: list[HistoricalRow] = []
    for i in range(len(all_rows) - 1):
        row_ts = end_ts - i * time_delta
        curr = all_rows[i]
        nxt = all_rows[i + 1]
        values: dict[str, float] = {}
        for ci in wanted_idx:
            if ci >= len(curr) or ci >= len(nxt):
                continue
            delta_ws = curr[ci] - nxt[ci]
            values[columns[ci][0]] = delta_ws / time_delta
        rows.append(HistoricalRow(timestamp=row_ts, values=values))

    return rows

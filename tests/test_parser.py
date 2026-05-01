"""Unit tests for the eGauge XML parser."""

from egauge_mcp.client import parse_historical_xml, parse_live_xml


LIVE_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<data serial="0x17">
 <ts>1777659081</ts>
 <r t="V" n="L1 Voltage" did="1"><v>11041738800845</v><i>235.971</i></r>
 <r t="P" n="Mains" did="0"><v>22929439571605</v><i>667135</i></r>
 <r t="P" n="HVAC" did="2"><v>123456789</v><i>4500</i></r>
</data>
"""


HISTORICAL_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<group serial="0x2001c7a5">
<data columns="3" time_stamp="0x69f3ed00" time_delta="900" epoch="0">
 <cname t="P">Mains</cname>
 <cname t="P">HVAC</cname>
 <cname t="V">L1 Voltage</cname>
 <r><c>1000000000</c><c>500000000</c><c>240000</c></r>
 <r><c>999000000</c><c>499500000</c><c>240000</c></r>
 <r><c>998000000</c><c>499000000</c><c>240000</c></r>
 <r><c>997000000</c><c>498500000</c><c>240000</c></r>
</data>
</group>
"""


def test_live_parses_ts_from_child_element():
    out = parse_live_xml(LIVE_XML)
    assert out.timestamp == 1777659081
    assert len(out.registers) == 3
    mains = next(r for r in out.registers if r.name == "Mains")
    assert mains.type == "P"
    assert mains.instantaneous == 667135.0
    assert mains.cumulative == 22929439571605


def test_historical_default_filters_to_p_type():
    rows = parse_historical_xml(HISTORICAL_XML)
    # 4 cells, deltas computed between consecutive, last row dropped → 3 rows
    assert len(rows) == 3
    # Default filter excludes V-type
    assert "L1 Voltage" not in rows[0].values
    assert "Mains" in rows[0].values
    # Average watts: (1_000_000_000 - 999_000_000) / 900 = 1111.11...
    assert abs(rows[0].values["Mains"] - 1111.111) < 0.1


def test_historical_explicit_filter_includes_voltage():
    rows = parse_historical_xml(HISTORICAL_XML, filter_names=["L1 Voltage"])
    assert "L1 Voltage" in rows[0].values
    assert "Mains" not in rows[0].values


def test_historical_timestamps_decrement_by_time_delta():
    rows = parse_historical_xml(HISTORICAL_XML)
    end_ts = int("0x69f3ed00", 16)
    assert rows[0].timestamp == end_ts
    assert rows[1].timestamp == end_ts - 900
    assert rows[2].timestamp == end_ts - 1800

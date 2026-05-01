# egauge-mcp

**An MCP server that turns Claude into a live energy analyst for any [eGauge](https://egauge.net) energy meter.**

Without MCP, Claude can only work with what you paste in. With this server, Claude pulls live data from your eGauge devices mid-conversation — fetches it, analyses it, and writes the report. No more CSV exports.

```
You:  "Summarize Lake Pump consumption vs grid for the last 30 days at Wildfire,
       and tell me what's driving the demand charge on the KPLC bill."

Claude:  [calls list_devices → analyze_period → find_peak_demand → tariff_estimate]

         "Over the last 30 days, the Lake Pump grid meter (egauge15539) drew
          14,210 kWh — 38% of total Wildfire consumption. Peak demand was
          47.3 kW on April 18 at 14:15 UTC, dominated by the pump itself
          (42.1 kW). Phantom load is 1.8 kW (always on). At your KPLC rate
          of KES 28.50/kWh and KES 1,400/kW demand, the period bills out
          at KES 471,255 — KES 66,220 of which is the demand charge.
          Shifting one pump cycle out of the 14:00 peak would save ~KES 12k/mo."
```

## What it does

11 MCP tools, each backed by the eGauge XML API:

| Tool | Purpose |
|------|---------|
| `list_devices` | Show configured devices |
| `add_device` | Register a new device (saves to config) |
| `get_registers` | List channels reported by a device |
| `device_status` | Quick health check + total kW now |
| `get_live` | Instantaneous watts per register, right now |
| `get_history` | Raw time-series rows for any window |
| `analyze_period` | Total kWh, peak kW, load factor, per-register share |
| `find_peak_demand` | Top-N peak intervals with breakdown |
| `find_phantom_loads` | Always-on baseline per register |
| `compare_periods` | Side-by-side: this week vs last week, etc. |
| `detect_anomalies` | Flag unusual intervals (per hour-of-week stats) |
| `solar_self_consumption` | Self-use % vs grid export |
| `tariff_estimate` | Estimate the bill from a tariff (KPLC-compatible) |

## Install

You don't need to clone anything — `uvx` will fetch it on demand:

```bash
# One-time: install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or install from PyPI (once published):

```bash
uvx egauge-mcp
```

Or from this repo:

```bash
uvx --from git+https://github.com/chrismbori/egauge-mcp egauge-mcp
```

## Connect to Claude

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "egauge": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chrismbori/egauge-mcp", "egauge-mcp"]
    }
  }
}
```

Restart Claude Desktop. You should see the eGauge tools appear in the tool icon.

### Claude Code

Add to `~/.claude/settings.json` or your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "egauge": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chrismbori/egauge-mcp", "egauge-mcp"]
    }
  }
}
```

Or via the CLI:

```bash
claude mcp add egauge -- uvx --from git+https://github.com/chrismbori/egauge-mcp egauge-mcp
```

### Cursor

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "egauge": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/chrismbori/egauge-mcp", "egauge-mcp"]
    }
  }
}
```

## Configure your devices

The server works **zero-config** for any public eGauge device — just give Claude an id like `egauge12345` and it'll fetch from `https://egauge12345.egaug.es`.

For friendly names, credentials, and register aliases, create `~/.egauge-mcp/config.toml`:

```toml
# Optional global default credentials
[defaults]
username = "owner"
password = "your-password"

[[device]]
id = "egauge15539"
name = "Wildfire Lake Pump"

[device.registers]
"Lake Pump Grid" = "Grid"

[[device]]
id = "egauge60394"
name = "Wildfire Main Farm"
# Per-device credentials override defaults
username = "owner"
password = "different-password"

[device.registers]
"Main Farm Grid" = "Grid"
"Solar" = "Solar PV"
"Backup Generator" = "Generator"
```

Now Claude can answer "what was the Solar generation at Wildfire Main Farm last week?" — it resolves "Wildfire Main Farm" to `egauge60394` and "Solar" to the actual register name `Solar PV`.

You can also let Claude add devices for you mid-conversation: "add my new meter `egauge99999`, call it 'Garage Solar'" — Claude will call `add_device` and persist it.

### Environment variables

```bash
export EGAUGE_MCP_CONFIG=/path/to/config.toml   # default: ~/.egauge-mcp/config.toml
export EGAUGE_USERNAME=owner                    # fallback if not in config
export EGAUGE_PASSWORD=secret                   # fallback if not in config
export EGAUGE_MCP_DEBUG=1                       # enable debug logging
```

## Time arguments

Any tool that accepts `start` / `end` understands:

- ISO-8601: `"2026-04-01T00:00:00Z"`, `"2026-04-15"`
- Relative: `"now"`, `"24h ago"`, `"7d ago"`, `"30d ago"`, `"2w ago"`

## eGauge quirks the parser handles

These are well-known traps for anyone integrating with eGauge devices:

- Multiple `<data>` blocks in one response (each can have one or many `<r>` rows)
- `time_delta` is the interval **between consecutive rows**, not the total span — and the device often ignores the requested `s` parameter, so we trust `time_delta` from the response
- Older devices have very coarse archive resolution (~61-day rows on some firmware); the parser computes average watts as `(curr - next) / time_delta` regardless
- Trailing duplicate rows (from before device install) are deduped by comparing column values
- Some firmware skips auth for read-only endpoints; we handle both anonymous and Digest-auth flows
- HTTPS-then-HTTP fallback for older firmware that doesn't support TLS

## Development

```bash
git clone https://github.com/chrismbori/egauge-mcp
cd egauge-mcp
uv sync
uv run egauge-mcp                       # runs the server on stdio
uv run python -m egauge_mcp.client      # smoke-test from a script
```

Test with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run egauge-mcp
```

## License

MIT — see [LICENSE](LICENSE).

## Contributing

PRs welcome. The eGauge ecosystem is full of older firmware and quirky devices; if you hit a parser edge case, open an issue with a minimal XML sample.

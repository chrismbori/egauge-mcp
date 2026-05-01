"""
Device configuration.

Loaded from $EGAUGE_MCP_CONFIG (default ~/.egauge-mcp/config.toml).
Devices not present in config are still queryable by raw eGauge id — config
just adds friendly names, credentials, and register aliases.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_DIR = Path.home() / ".egauge-mcp"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


@dataclass
class DeviceConfig:
    id: str
    name: str | None = None
    username: str | None = None
    password: str | None = None
    # alias -> actual register name on the device
    registers: dict[str, str] = field(default_factory=dict)

    def display_name(self) -> str:
        return self.name or self.id

    def resolve_register(self, alias_or_name: str) -> str:
        """Map a friendly alias to the device's actual register name (case-insensitive)."""
        lookup = {a.lower(): real for a, real in self.registers.items()}
        return lookup.get(alias_or_name.lower(), alias_or_name)


@dataclass
class Config:
    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    default_username: str | None = None
    default_password: str | None = None
    path: Path = DEFAULT_CONFIG_PATH

    def get(self, id_or_name: str) -> DeviceConfig:
        """Find a device by id or by friendly name. Falls back to a bare-id config."""
        key = id_or_name.lower()
        for d in self.devices.values():
            if d.id.lower() == key or (d.name and d.name.lower() == key):
                return self._with_defaults(d)
        # Bare id — synthesize an unconfigured device
        return self._with_defaults(DeviceConfig(id=id_or_name))

    def _with_defaults(self, d: DeviceConfig) -> DeviceConfig:
        if d.username is None and self.default_username:
            d = replace(d, username=self.default_username)
        if d.password is None and self.default_password:
            d = replace(d, password=self.default_password)
        return d

    def add_device(self, device: DeviceConfig) -> None:
        self.devices[device.id] = device
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if self.default_username or self.default_password:
            lines.append("[defaults]")
            if self.default_username:
                lines.append(f'username = "{_escape(self.default_username)}"')
            if self.default_password:
                lines.append(f'password = "{_escape(self.default_password)}"')
            lines.append("")
        for d in self.devices.values():
            lines.append("[[device]]")
            lines.append(f'id = "{_escape(d.id)}"')
            if d.name:
                lines.append(f'name = "{_escape(d.name)}"')
            if d.username:
                lines.append(f'username = "{_escape(d.username)}"')
            if d.password:
                lines.append(f'password = "{_escape(d.password)}"')
            if d.registers:
                lines.append("[device.registers]")
                for alias, real in d.registers.items():
                    lines.append(f'"{_escape(alias)}" = "{_escape(real)}"')
            lines.append("")
        self.path.write_text("\n".join(lines).rstrip() + "\n")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def load_config(path: Path | None = None) -> Config:
    p = path or Path(os.environ.get("EGAUGE_MCP_CONFIG", str(DEFAULT_CONFIG_PATH)))
    cfg = Config(path=p)

    # Env-var defaults
    cfg.default_username = os.environ.get("EGAUGE_USERNAME") or None
    cfg.default_password = os.environ.get("EGAUGE_PASSWORD") or None

    if not p.exists():
        return cfg

    raw = tomllib.loads(p.read_text())

    defaults = raw.get("defaults") or {}
    cfg.default_username = defaults.get("username") or cfg.default_username
    cfg.default_password = defaults.get("password") or cfg.default_password

    devices = raw.get("device") or []
    if isinstance(devices, dict):
        devices = [devices]
    for d in devices:
        if not d.get("id"):
            continue
        cfg.devices[d["id"]] = DeviceConfig(
            id=d["id"],
            name=d.get("name"),
            username=d.get("username"),
            password=d.get("password"),
            registers=dict(d.get("registers") or {}),
        )
    return cfg

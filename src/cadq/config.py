"""User-level configuration for cadq.

Persisted to ``%APPDATA%/cadq/config.json`` on Windows or
``~/.config/cadq/config.json`` on Linux/macOS.  Used to remember the path
to the ODA File Converter and any other per-user preferences without
forcing the user to set environment variables.

Resolution order for any setting is:

1. Explicit function argument (e.g. ``--oda-path`` on the CLI).
2. Environment variable (e.g. ``ODA_FILE_CONVERTER``).
3. User config file (this module).
4. Built-in auto-detection (PATH + common install locations).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "config.json"


def config_dir() -> Path:
    """Return the per-user config directory, creating it lazily on save."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "cadq"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict[str, Any]) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def set_(key: str, value: Any) -> Path:
    data = load()
    data[key] = value
    return save(data)


def unset(key: str) -> Path:
    data = load()
    data.pop(key, None)
    return save(data)

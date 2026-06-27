"""
Configuration management for duk (fafnir edition).

duk now reads from two possible sources:
  * ``live`` -- the FMP API directly (the original behaviour)
  * ``db``   -- the fafnir PostgreSQL warehouse (mart schema)

The active source is chosen per invocation with ``--source`` or defaults to
``[general].default_source`` (which itself defaults to ``db`` when a database DSN
is configured, else ``live``).

Config file: ``~/.dukrc`` (TOML). Reuses the original keys and adds:
  [database].dsn
  [general].default_source
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Optional


class DukConfig:
    """Configuration manager for the duk CLI."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.path.expanduser("~/.dukrc")
        self.config_path = config_path
        self._data: dict[str, Any] = {}
        path = Path(config_path)
        if path.exists():
            with path.open("rb") as fh:
                self._data = tomllib.load(fh)

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    # -- FMP (live mode) ----------------------------------------------------
    @property
    def fmp_key(self) -> str:
        env_key = os.environ.get("FMP_API_KEY", "").strip()
        if env_key:
            return env_key
        return (self._get("api", "fmp_key", "") or "").strip()

    # -- database (db mode) -------------------------------------------------
    @property
    def dsn(self) -> str:
        env_dsn = os.environ.get("FAFNIR_DSN", "").strip()
        if env_dsn:
            return env_dsn
        return (self._get("database", "dsn", "") or "").strip()

    @property
    def default_source(self) -> str:
        """'live' or 'db'. Defaults to db when a DSN is configured, else live."""
        configured = (self._get("general", "default_source", "") or "").strip().lower()
        if configured in ("live", "db"):
            return configured
        return "db" if self.dsn else "live"

    # -- output / logging ---------------------------------------------------
    @property
    def default_output_dir(self) -> str:
        return self._get("general", "default_output_dir", "var/duk")

    @property
    def default_output_type(self) -> str:
        return self._get("general", "default_output_type", "csv")

    @property
    def log_level(self) -> str:
        return self._get("general", "log_level", "info")

    @property
    def log_dir(self) -> str:
        return self._get("general", "log_dir", "var/duk/log")

    def is_loaded(self) -> bool:
        return bool(self._data)


def get_config(config_path: Optional[str] = None) -> DukConfig:
    return DukConfig(config_path)

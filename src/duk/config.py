"""
Configuration management for duk (fafnir edition).

duk reads from one of three sources:
  * ``db``       -- the fafnir PostgreSQL warehouse (mart schema); the default
  * ``fmp``      -- the FMP API directly (``live`` is the older spelling)
  * ``sharadar`` -- the Sharadar API directly

The active source is chosen per invocation with ``--source`` or defaults to
``[general].default_source``, which itself defaults to ``db``: duk reads fafnir
unless told otherwise. A vendor source needs that vendor's key.

Config file: ``~/.dukrc`` (TOML). Reuses the original keys and adds:
  [api].sharadar_key, [api].sharadar_base_url
  [database].dsn
  [general].default_source
  [general].console_format   ("auto" | "table" | "csv" | "json")
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

    # -- FMP (-S fmp) -------------------------------------------------------
    @property
    def fmp_key(self) -> str:
        env_key = os.environ.get("FMP_API_KEY", "").strip()
        if env_key:
            return env_key
        return (self._get("api", "fmp_key", "") or "").strip()

    # -- Sharadar (-S sharadar) ---------------------------------------------
    @property
    def sharadar_key(self) -> str:
        env_key = os.environ.get("SHARADAR_API_KEY", "").strip()
        if env_key:
            return env_key
        return (self._get("api", "sharadar_key", "") or "").strip()

    @property
    def sharadar_base_url(self) -> str:
        """Base of Sharadar's table API. Overridable because the endpoint shape is
        documented from the vendor's pages, not yet measured against a paid key
        (see duk.sharadar_api)."""
        env_url = os.environ.get("SHARADAR_BASE_URL", "").strip()
        if env_url:
            return env_url
        return (self._get("api", "sharadar_base_url", "") or "").strip()

    # -- database (db mode) -------------------------------------------------
    @property
    def dsn(self) -> str:
        env_dsn = os.environ.get("FAFNIR_DSN", "").strip()
        if env_dsn:
            return env_dsn
        return (self._get("database", "dsn", "") or "").strip()

    @property
    def default_source(self) -> str:
        """'db', 'fmp' or 'sharadar'. Defaults to db: duk reads the warehouse
        unless the configuration or -S says otherwise. 'live' is accepted as the
        older name for 'fmp'. An unknown value falls back to db rather than
        failing here, so a typo surfaces as a clear source error at dispatch."""
        configured = (self._get("general", "default_source", "") or "").strip().lower()
        if configured == "live":
            return "fmp"
        if configured in ("db", "fmp", "sharadar"):
            return configured
        return "db"

    # -- output / logging ---------------------------------------------------
    @property
    def default_output_dir(self) -> str:
        return self._get("general", "default_output_dir", "var/duk")

    @property
    def default_output_type(self) -> str:
        return self._get("general", "default_output_type", "csv")

    @property
    def console_format(self) -> str:
        """How a command prints when no --table/--csv/--json is given: ``auto``
        (the default: a table on a terminal, CSV in a pipe), ``table``, ``csv`` or
        ``json``. ``DUK_FORMAT`` in the environment overrides it."""
        return (self._get("general", "console_format", "") or "auto").strip().lower()

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

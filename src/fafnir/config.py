"""
Configuration management for fafnir.

Loads ``~/.fafnirrc`` (TOML) and layers environment-variable overrides on top.
Secrets (the database password and API keys) are sourced from the environment
or a secrets manager in preference to the file -- they must never live in code
or logs.

Config sections
---------------
``[database]``
    dsn        -- full libpq connection string/URL (takes precedence if set)
    host, port, dbname, user, password  -- assembled into a DSN if dsn is unset
``[api]``
    fmp_key, fred_key, bls_key, bea_key
``[general]``
    log_level, log_dir, universe, request_rate_per_min, overlap_days
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = "~/.fafnirrc"


class FafnirConfig:
    """Configuration manager for the fafnir warehouse tooling."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.path.expanduser(DEFAULT_CONFIG_PATH)
        self.config_path = config_path
        self._data: dict[str, Any] = {}
        path = Path(config_path)
        if path.exists():
            with path.open("rb") as fh:
                self._data = tomllib.load(fh)

    # -- internal helpers ---------------------------------------------------
    def _get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    # -- database -----------------------------------------------------------
    @property
    def dsn(self) -> str:
        """
        Return a libpq DSN.

        Resolution order:
          1. ``FAFNIR_DSN`` environment variable
          2. ``[database].dsn`` in the config file
          3. assembled from ``[database]`` host/port/dbname/user/password,
             with the password overridable by ``FAFNIR_DB_PASSWORD`` / ``PGPASSWORD``.
        """
        env_dsn = os.environ.get("FAFNIR_DSN", "").strip()
        if env_dsn:
            return env_dsn

        file_dsn = (self._get("database", "dsn", "") or "").strip()
        if file_dsn:
            return file_dsn

        host = self._get("database", "host", "localhost")
        port = self._get("database", "port", 5432)
        dbname = self._get("database", "dbname", "fafnir")
        user = self._get("database", "user", "fafnir_ingest")
        password = (
            os.environ.get("FAFNIR_DB_PASSWORD")
            or os.environ.get("PGPASSWORD")
            or self._get("database", "password", "")
        )
        parts = [f"host={host}", f"port={port}", f"dbname={dbname}", f"user={user}"]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    # -- api keys -----------------------------------------------------------
    @property
    def fmp_key(self) -> str:
        return (
            os.environ.get("FMP_API_KEY", "").strip()
            or (self._get("api", "fmp_key", "") or "").strip()
        )

    @property
    def fred_key(self) -> str:
        return (
            os.environ.get("FRED_API_KEY", "").strip()
            or (self._get("api", "fred_key", "") or "").strip()
        )

    @property
    def bls_key(self) -> str:
        return (
            os.environ.get("BLS_API_KEY", "").strip()
            or (self._get("api", "bls_key", "") or "").strip()
        )

    @property
    def bea_key(self) -> str:
        return (
            os.environ.get("BEA_API_KEY", "").strip()
            or (self._get("api", "bea_key", "") or "").strip()
        )

    # -- general ------------------------------------------------------------
    @property
    def log_level(self) -> str:
        return self._get("general", "log_level", "info")

    @property
    def log_dir(self) -> str:
        return self._get("general", "log_dir", "var/fafnir/log")

    @property
    def universe(self) -> str:
        """Target universe identifier, e.g. 'us-equity-etf'."""
        return self._get("general", "universe", "us-equity-etf")

    @property
    def request_rate_per_min(self) -> int:
        """Proactive FMP throttle ceiling (Professional plan ~300 req/min)."""
        return int(self._get("general", "request_rate_per_min", 280))

    @property
    def overlap_days(self) -> int:
        """Re-pull overlap window (days) to absorb late corrections/re-adjustments."""
        return int(self._get("general", "overlap_days", 5))

    @property
    def calendar_start_year(self) -> int:
        return int(self._get("general", "calendar_start_year", 2015))

    @property
    def calendar_end_year(self) -> int:
        return int(self._get("general", "calendar_end_year", 2027))

    def is_loaded(self) -> bool:
        return bool(self._data)


def get_config(config_path: Optional[str] = None) -> FafnirConfig:
    """Return a FafnirConfig instance."""
    return FafnirConfig(config_path)

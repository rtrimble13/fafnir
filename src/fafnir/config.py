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

Environment overrides
---------------------
``FAFNIR_DSN``, ``FAFNIR_DB_PASSWORD``/``PGPASSWORD``, ``FMP_API_KEY``,
``FRED_API_KEY``, ``BLS_API_KEY``, ``BEA_API_KEY``, ``FAFNIR_LOG_DIR``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = "~/.fafnirrc"

#: Named once so the connection error surface can describe precedence without
#: hard-coding the variable it is describing.
DSN_ENV_VAR = "FAFNIR_DSN"
PASSWORD_ENV_VARS = ("FAFNIR_DB_PASSWORD", "PGPASSWORD")


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
        env_dsn = os.environ.get(DSN_ENV_VAR, "").strip()
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

    @property
    def dsn_source(self) -> str:
        """Where :attr:`dsn` came from, in words, for diagnostics.

        A connection failure is nearly always a question about *which* of the
        three sources won, and the answer is invisible in the DSN itself: an
        assembled `host=localhost` and a `[database].dsn` of `host=localhost`
        look identical and are fixed in different files.
        """
        if os.environ.get(DSN_ENV_VAR, "").strip():
            return f"the {DSN_ENV_VAR} environment variable"
        if (self._get("database", "dsn", "") or "").strip():
            return f"[database].dsn in {self.config_path}"
        if not Path(self.config_path).exists():
            return f"built-in defaults ({self.config_path} does not exist)"
        return f"the [database] section of {self.config_path}"

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
        """Where the rotating ``fafnir.log`` is written.

        ``FAFNIR_LOG_DIR`` wins over the file, matching how ``FAFNIR_DSN`` and
        ``FAFNIR_SQL_DIR`` already behave. It exists because the deployment that
        most needs an absolute path -- the systemd units, which source
        ``/etc/fafnir/fafnir.env`` -- had no way to set one without editing the
        service user's ``~/.fafnirrc``.

        The default is **relative**, and deliberately kept that way: a developer
        running from the checkout gets ``./var/fafnir/log`` and nothing writes
        outside the tree. On a host that is the wrong answer, which is why
        install_hetzner.md §4.3 sets an absolute path and
        :func:`fafnir.logging_config.setup_logging` says so by name when the
        relative default cannot be created.
        """
        env = os.environ.get("FAFNIR_LOG_DIR", "").strip()
        if env:
            return env
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
    def actions_overlap_days(self) -> int:
        """Re-sweep window (days) on the corporate-actions calendar.

        Wider than ``overlap_days`` because a dividend is amended later than a price
        is corrected: the declared amount, the record date and the payment date can
        all move after the event first appears on the feed.
        """
        return int(self._get("general", "actions_overlap_days", 7))

    @property
    def actions_mode(self) -> str:
        """How `fafnir ingest actions` fetches: ``symbol``, ``calendar`` or ``auto``.

        Defaults to ``symbol`` -- the pre-ADR-0007 behaviour -- because adopting the
        market-wide calendar is gated on `fafnir source probe-actions` passing
        against a live key, which only the operator has. Flip to ``auto`` once it does.
        """
        value = str(self._get("general", "actions_mode", "symbol")).strip().lower()
        return value if value in ("symbol", "calendar", "auto") else "symbol"

    @property
    def actions_reconcile_buckets(self) -> int:
        """Reconcile 1/N of the universe per night against the per-symbol feed.

        0 disables the rotation. The default 30 reaches every security monthly at
        roughly 1.3% of a full nightly refresh.
        """
        return max(0, int(self._get("general", "actions_reconcile_buckets", 30)))

    @property
    def calendar_start_year(self) -> int:
        return int(self._get("general", "calendar_start_year", 2015))

    @property
    def calendar_end_year(self) -> int:
        """Minimum/initial horizon for the calendar + partitions. The daily job
        (`fafnir db ensure-horizon`) auto-extends beyond this to a rolling
        `current_year + horizon_extra_years`, so this rarely needs changing."""
        return int(self._get("general", "calendar_end_year", 2027))

    @property
    def horizon_extra_years(self) -> int:
        """How many years past the current year the rolling horizon stays ahead."""
        return int(self._get("general", "horizon_extra_years", 2))

    def is_loaded(self) -> bool:
        return bool(self._data)


def get_config(config_path: Optional[str] = None) -> FafnirConfig:
    """Return a FafnirConfig instance."""
    return FafnirConfig(config_path)

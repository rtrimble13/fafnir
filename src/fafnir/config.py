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
    sharadar_base_url -- shared with duk; the Sharadar *key* is never read here
``[general]``
    log_level, log_dir, universe, request_rate_per_min, overlap_days,
    exclude_instruments, primary_price_vendor, fmp_secondary
``[sharadar]``
    rate_per_min, bulk_dir -- see :mod:`fafnir.sources.sharadar`
``[prices]``
    restatement_max_ratio, restatement_max_volume_ratio, restatement_min_volume --
    see :mod:`fafnir.ingest.daily_price`
``[dq]``
    engine, dorq_path, dorq_config, dorq_shadow, dorq_shadow_dir,
    dorq_lookback_sessions, dorq_threads, dorq_backfill_max -- see
    :mod:`fafnir.dq.dorq`

Environment overrides
---------------------
``FAFNIR_DSN``, ``FAFNIR_DB_PASSWORD``/``PGPASSWORD``, ``FMP_API_KEY``,
``FRED_API_KEY``, ``BLS_API_KEY``, ``BEA_API_KEY``, ``FAFNIR_LOG_DIR``,
``FAFNIR_DORQ_PATH``, ``SHARADAR_API_KEY`` (the only source of that key),
``SHARADAR_BASE_URL``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Optional

from fafnir.instruments import INSTRUMENT_KINDS

DEFAULT_CONFIG_PATH = "~/.fafnirrc"

#: Named once so the connection error surface can describe precedence without
#: hard-coding the variable it is describing.
DSN_ENV_VAR = "FAFNIR_DSN"
PASSWORD_ENV_VARS = ("FAFNIR_DB_PASSWORD", "PGPASSWORD")

SHARADAR_KEY_ENV_VAR = "SHARADAR_API_KEY"
#: The vendors `[general] primary_price_vendor` may name.
PRICE_VENDORS = ("fmp", "sharadar")
#: What `fafnir config get` prints: the settings scripts branch on. None is a
#: secret; a key or a password is never printed.
SCRIPT_KEYS = (
    "primary_price_vendor",
    "fmp_secondary",
    "actions_mode",
    "dq_engine",
    "log_dir",
    "sharadar_bulk_dir",
)


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

    # -- sharadar -----------------------------------------------------------
    @property
    def sharadar_api_key(self) -> str:
        """The Sharadar key, from ``SHARADAR_API_KEY`` and nowhere else.

        Not from the rc file, unlike the FMP key: the Sharadar subscription is the
        warehouse's primary feed and its licence's, so it lives in the host's
        root-owned env file (``/etc/fafnir/fafnir.env``), never in a file a
        session or a backup might copy. ``[api].sharadar_key`` is duk's, for a
        laptop, and fafnir ignores it.
        """
        return os.environ.get(SHARADAR_KEY_ENV_VAR, "").strip()

    @property
    def sharadar_key_in_rc(self) -> bool:
        """Whether the rc file holds a Sharadar key fafnir will not use, so a
        missing-key error can say why."""
        return bool((self._get("api", "sharadar_key", "") or "").strip())

    @property
    def sharadar_base_url(self) -> str:
        """Base of Sharadar's table API: ``SHARADAR_BASE_URL``, then
        ``[api].sharadar_base_url`` (both shared with duk), then the client's
        default. Configurable because the endpoint is UNVERIFIED until the probe
        (#49) measures it."""
        from fafnir.sources.sharadar import DEFAULT_BASE_URL

        return (
            os.environ.get("SHARADAR_BASE_URL", "").strip()
            or (self._get("api", "sharadar_base_url", "") or "").strip()
            or DEFAULT_BASE_URL
        )

    @property
    def sharadar_rate_per_min(self) -> int:
        """Proactive throttle on Sharadar requests. The vendor's limit is not yet
        measured, so the default is cautious; a 429 is retried with backoff."""
        value = int(self._get("sharadar", "rate_per_min", 60))
        if value < 1:
            raise ValueError(f"[sharadar] rate_per_min must be at least 1, not {value}")
        return value

    @property
    def sharadar_bulk_dir(self) -> str:
        """Where bulk export files land, named by content (``<sha256>.<ext>``).

        Relative by default, like ``log_dir``, so a checkout writes inside the
        tree; a host sets an absolute path (install_hetzner.md §4.3).
        """
        return str(
            self._get("sharadar", "bulk_dir", "var/fafnir/landing/sharadar")
            or "var/fafnir/landing/sharadar"
        )

    @property
    def primary_price_vendor(self) -> str:
        """The night's primary vendor: ``fmp`` until the Sharadar cutover (plan v2,
        P3), ``sharadar`` after. ``daily_update.sh`` picks its sequence by it, and
        FMP's loaders refuse to write under ``sharadar`` unless told they are the
        secondary (#111). A typo fails rather than picking a mode."""
        value = str(self._get("general", "primary_price_vendor", "fmp")).strip().lower()
        if value not in PRICE_VENDORS:
            raise ValueError(
                f"[general] primary_price_vendor: {value!r} is not one of "
                f"{', '.join(repr(v) for v in PRICE_VENDORS)}"
            )
        return value

    @property
    def fmp_secondary(self) -> bool:
        """Whether the Sharadar night also runs FMP's loaders for what FMP still
        feeds: the funds and the securities with no Sharadar link (#111). True by
        default, while the reduced FMP tier serves them (#128); false once it
        does not. Read only when ``primary_price_vendor`` is ``sharadar``."""
        value = self._get("general", "fmp_secondary", True)
        if not isinstance(value, bool):
            raise ValueError(f"[general] fmp_secondary: {value!r} is not true or false")
        return value

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
    def restatement_max_ratio(self) -> float:
        """How far a nightly re-read may move a stored close, either way, before the
        loader keeps the stored bar and flags the conflict
        (``price_restatement_conflict``)."""
        return self._ratio_setting("restatement_max_ratio", 1.25)

    @property
    def restatement_max_volume_ratio(self) -> float:
        """The same, for volume."""
        return self._ratio_setting("restatement_max_volume_ratio", 3.0)

    @property
    def restatement_min_volume(self) -> int:
        """Volumes at or below this on both sides differ only as a round lot's
        rounding, never a restatement: FMP re-reads an odd-lot day as 100 shares."""
        value = int(self._get("prices", "restatement_min_volume", 100))
        if value < 0:
            raise ValueError(
                f"[prices] restatement_min_volume must be 0 or more, not {value}"
            )
        return value

    def _ratio_setting(self, key: str, default: float) -> float:
        value = float(self._get("prices", key, default))
        if value <= 1:
            raise ValueError(f"[prices] {key} must be above 1, not {value}")
        return value

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
    def excluded_instruments(self) -> tuple[str, ...]:
        """Instrument kinds kept out of the universe (ADR 0012).

        ``[general] exclude_instruments`` -- any of ``warrant``, ``right``, ``unit``;
        all three by default, ``[]`` to admit everything again. An unknown kind is
        an error rather than ignored: a misspelt ``"warants"`` would otherwise
        quietly put every warrant back into scope.
        """
        raw = self._get("general", "exclude_instruments", list(INSTRUMENT_KINDS))
        if isinstance(raw, str):
            raw = [raw]
        kinds = tuple(dict.fromkeys(str(k).strip().lower() for k in raw))
        unknown = [k for k in kinds if k not in INSTRUMENT_KINDS]
        if unknown:
            raise ValueError(
                f"[general] exclude_instruments: unknown kind(s) {unknown}; "
                f"expected any of {list(INSTRUMENT_KINDS)}"
            )
        return kinds

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

    # -- dq: the dorq engine (plan DR-0702; doc/dorq.md) -------------------
    @property
    def dq_engine(self) -> str:
        """Which engine `fafnir dq run` uses by default: ``sql``, ``dorq`` or ``both``.

        ``sql`` -- the checks in :mod:`fafnir.dq.checks`, and nothing else -- until
        an operator opts in. dorq is a second binary, so a host that has not
        installed it must keep working exactly as before.
        """
        value = str(self._get("dq", "engine", "sql")).strip().lower()
        if value not in ("sql", "dorq", "both"):
            raise ValueError(
                f"[dq] engine: {value!r} is not one of 'sql', 'dorq' or 'both'"
            )
        return value

    @property
    def dorq_path(self) -> str:
        """The dorq binary. ``FAFNIR_DORQ_PATH`` wins over the file."""
        env = os.environ.get("FAFNIR_DORQ_PATH", "").strip()
        if env:
            return env
        return str(self._get("dq", "dorq_path", "/opt/dorq/bin/dorq"))

    @property
    def dorq_config(self) -> str:
        """dorq's own config file (``priors/fafnir.toml`` and the calibration it
        includes). Empty runs dorq ``--isolated``: its defaults, and never a
        ``dorq.toml`` that happens to sit in the working directory."""
        return str(self._get("dq", "dorq_config", "") or "")

    @property
    def dorq_shadow(self) -> bool:
        """Whether dorq's flags go to ``dorq_shadow_dir`` instead of the queue.

        True by default: dorq writes to the queue only once the shadow period has
        met the plan's cutover criteria and an operator turns this off (plan §7.4).
        """
        return bool(self._get("dq", "dorq_shadow", True))

    @property
    def dorq_shadow_dir(self) -> str:
        return str(self._get("dq", "dorq_shadow_dir", "var/dorq-shadow"))

    @property
    def dorq_lookback_sessions(self) -> int:
        """Sessions of history the nightly dorq run reads per security (plan §7.2)."""
        return max(20, int(self._get("dq", "dorq_lookback_sessions", 260)))

    @property
    def dorq_threads(self) -> int:
        """dorq worker threads; 0 is one per core. The output does not depend on it."""
        return max(0, int(self._get("dq", "dorq_threads", 0)))

    @property
    def dorq_backfill_max(self) -> int:
        """The most securities a nightly run judges the whole history of, for history
        that is new or was rewritten before its window. Above it the pass is skipped
        with a warning: that many is a universe rebuild, which wants ``--full``."""
        return max(0, int(self._get("dq", "dorq_backfill_max", 500)))

    def is_loaded(self) -> bool:
        return bool(self._data)


def get_config(config_path: Optional[str] = None) -> FafnirConfig:
    """Return a FafnirConfig instance."""
    return FafnirConfig(config_path)

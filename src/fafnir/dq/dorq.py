"""dorq as a data-quality engine: export, run, ingest (dorq plan §7, DR-0702).

dorq (https://github.com/rtrimble13/dorq) is a Bayesian linter for price series.
Where the SQL checks in :mod:`fafnir.dq.checks` ask "is this move larger than 50%?",
dorq weighs the explanations of each suspicious bar -- a bad print, a scale error,
an unreported split, a feed outage, or a real move -- and reports the probability
that it is a *data error*, with the evidence. See doc/dorq.md.

This module is the whole of the integration's moving parts:

  * **export** -- the bars (``core.daily_price``, raw, ``ORDER BY security_id,
    trade_date``) stream into dorq's stdin straight from ``COPY``; the trading
    calendar, the corporate actions and the security master go beside them as
    small CSV files.
  * **run** -- ``dorq check --format fafnir``: one JSON object per line, already
    shaped as an ``ops.data_quality_flag`` row, with ``check_name`` prefixed
    ``dorq_``.
  * **ingest** -- the rows land in a temporary table and are written with one
    set-based ``INSERT ... SELECT`` carrying **the same two NOT EXISTS guards**
    (open and accepted) as every check in :mod:`fafnir.dq.checks`, so dedupe and
    acceptance behave exactly as they do today.
  * **shadow** -- or, until the shadow period has met the plan's cutover criteria,
    the rows go to ``var/dorq-shadow/<as-of>.jsonl`` and nothing touches the queue.

dorq is deterministic: the same bars, context and config give byte-identical
output whatever the thread count. That is what lets ``dq recheck`` close a dorq
flag by re-running dorq and observing that the flag is no longer emitted
(:func:`fafnir.dq.recheck.recheck_dorq`).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from psycopg import sql

from fafnir.db.connection import Database
from fafnir.dq.checks import NAV_LAGGING_ASSET_TYPES, NAV_PRICED_PREDICATE
from fafnir.logging_config import get_logger

logger = get_logger("dq")

#: Every check dorq writes is named ``dorq_<check-name>``, ``-`` as ``_``.
DORQ_PREFIX = "dorq_"

#: Sessions before the previous run's as-of date that a nightly run reports again.
#: dorq judges the newest bars provisionally (a spike has no "after" yet), so the
#: last few sessions of one night are judged again, with more evidence, the next.
SINCE_OVERLAP_SESSIONS = 5

#: dorq's checks whose judgement spans securities: a failed load across a cohort,
#: several series moving by one split ratio, a market-wide day. A re-run over the
#: few securities with open flags cannot reproduce them, so ``dq recheck`` leaves
#: them alone.
CROSS_SECTIONAL_CHECKS = frozenset(
    {"dorq_cohort_gap", "dorq_cohort_move", "dorq_market_day"}
)

# The watermark rows (ops.load_watermark) a run advances: the as-of date it judged
# up to. Separate for the queue and the shadow, so a shadow night does not move the
# window of the run that writes flags.
WATERMARK_SOURCE = "dorq"
WATERMARK_QUEUE = "dq-run"
WATERMARK_SHADOW = "dq-shadow"

# Columns of the temporary table, in the order the fafnir format names them.
_FLAG_COLUMNS = ("security_id", "table_name", "record_key", "check_name", "severity")


class DorqError(RuntimeError):
    """dorq is missing, or refused to run (a usage, config or input error)."""


@dataclass(frozen=True)
class DorqSettings:
    """How to invoke dorq. Built from ``[dq]`` in fafnirrc by :func:`settings_from`."""

    path: str = "/opt/dorq/bin/dorq"
    config: str = ""
    threads: int = 0
    lookback_sessions: int = 260
    shadow_dir: str = "var/dorq-shadow"

    def base_args(self) -> list[str]:
        args = [self.path, "check"]
        args += ["--config", self.config] if self.config else ["--isolated"]
        if self.threads:
            args += ["--threads", str(self.threads)]
        return args


def settings_from(cfg) -> DorqSettings:
    """The ``[dq]`` keys of a :class:`fafnir.config.FafnirConfig` as settings."""
    return DorqSettings(
        path=cfg.dorq_path,
        config=cfg.dorq_config,
        threads=cfg.dorq_threads,
        lookback_sessions=cfg.dorq_lookback_sessions,
        shadow_dir=cfg.dorq_shadow_dir,
    )


def dorq_version(settings: DorqSettings) -> str:
    """The version the binary reports, e.g. ``0.6.0``. Raises :class:`DorqError`
    when it is missing or will not run."""
    try:
        done = subprocess.run(
            [settings.path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DorqError(
            f"dorq not runnable at {settings.path}: {exc}. Install it (doc/dorq.md) "
            "or set [dq] dorq_path / FAFNIR_DORQ_PATH."
        ) from exc
    if done.returncode != 0:
        raise DorqError(f"{settings.path} --version exited {done.returncode}")
    return done.stdout.strip()


# ---------------------------------------------------------------------------
# The window a run judges
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """What one run reads and what it reports.

    ``bars_from`` is the first date of bars exported (None: the whole history).
    ``since`` is dorq's ``--since``: violations dated before it are not reported
    (None: everything). ``as_of`` is the market's latest open session, passed as
    dorq's ``--as-of`` so DQ304 (stale feed) judges against the market rather
    than the newest bar in whichever subset was exported.
    """

    as_of: Optional[dt.date]
    bars_from: Optional[dt.date] = None
    since: Optional[dt.date] = None


def market_latest(db: Database, exchange_code: str) -> Optional[dt.date]:
    """The newest date with a bar that is an open session on the venue calendar --
    the same reference date check_freshness uses, for the same reason (a weekend
    NAV must not move the market's latest date)."""
    return db.fetchval(
        """
        SELECT max(p.trade_date)
          FROM core.daily_price p
          JOIN ref.trading_calendar c
            ON c.trade_date = p.trade_date
           AND c.exchange_code = %s AND c.is_open
        """,
        (exchange_code,),
    )


def session_back(
    db: Database, exchange_code: str, from_date: dt.date, sessions: int
) -> Optional[dt.date]:
    """The ``sessions``-th open session counting back from ``from_date`` (itself
    the first), or the calendar's first session when there are fewer."""
    return db.fetchval(
        """
        SELECT min(trade_date) FROM (
            SELECT trade_date FROM ref.trading_calendar
             WHERE exchange_code = %s AND is_open AND trade_date <= %s
             ORDER BY trade_date DESC
             LIMIT %s) w
        """,
        (exchange_code, from_date, max(1, sessions)),
    )


def read_watermark(db: Database, endpoint: str) -> Optional[dt.date]:
    return db.fetchval(
        """
        SELECT last_loaded_date FROM ops.load_watermark
         WHERE source = %s AND endpoint = %s AND security_id = 0
        """,
        (WATERMARK_SOURCE, endpoint),
    )


def write_watermark(db: Database, endpoint: str, as_of: dt.date) -> None:
    db.execute(
        """
        INSERT INTO ops.load_watermark
            (source, endpoint, security_id, last_loaded_date, last_run_at, updated_at)
        VALUES (%s, %s, 0, %s, now(), now())
        ON CONFLICT (source, endpoint, security_id) DO UPDATE
           SET last_loaded_date = EXCLUDED.last_loaded_date,
               last_run_at = now(), updated_at = now()
        """,
        (WATERMARK_SOURCE, endpoint, as_of),
    )


def nightly_window(
    db: Database,
    exchange_code: str,
    lookback_sessions: int,
    endpoint: str,
) -> Window:
    """The incremental window: ``lookback_sessions`` of bars, reporting from
    :data:`SINCE_OVERLAP_SESSIONS` before the previous run's as-of date.

    The first run has no previous as-of and reports the whole lookback -- which is
    what the SQL checks do on their first night too.
    """
    as_of = market_latest(db, exchange_code)
    if as_of is None:
        return Window(as_of=None)
    bars_from = session_back(db, exchange_code, as_of, lookback_sessions)
    previous = read_watermark(db, endpoint)
    since = None
    if previous is not None:
        since = session_back(db, exchange_code, previous, SINCE_OVERLAP_SESSIONS + 1)
        if bars_from is not None and since is not None and since < bars_from:
            since = bars_from
    return Window(as_of=as_of, bars_from=bars_from, since=since)


def full_window(db: Database, exchange_code: str) -> Window:
    """Every bar of every security, every violation reported."""
    return Window(as_of=market_latest(db, exchange_code))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
            count += 1
    return count


def _scope(security_ids: Optional[Sequence[int]], alias: str) -> tuple[str, list]:
    if security_ids is None:
        return "", []
    return f" AND {alias}.security_id = ANY(%s)", [list(security_ids)]


def export_context(
    db: Database,
    workdir: Path,
    exchange_code: str,
    security_ids: Optional[Sequence[int]] = None,
) -> dict[str, Path]:
    """Write the calendar, corporate actions and security metadata dorq reads.

    The calendar is ``ref.trading_calendar`` for the venue -- the same sessions
    `gap` judges against. Actions are ``core.corporate_action`` as stored: dorq
    expects raw bars with the splits on file beside them, which is what lets it
    tell a split that is right from one that is misdated, inverted or applied
    twice (DQ701-DQ704). The metadata carries ``nav_priced`` by the loader's own
    rule (:data:`fafnir.dq.checks.NAV_PRICED_PREDICATE`), which dorq's profiles
    match on.
    """
    paths = {
        "calendar": workdir / "calendar.csv",
        "actions": workdir / "actions.csv",
        "meta": workdir / "meta.csv",
    }
    cal = db.fetchall(
        """
        SELECT exchange_code, trade_date, is_open FROM ref.trading_calendar
         WHERE exchange_code = %s ORDER BY trade_date
        """,
        (exchange_code,),
    )
    _write_csv(
        paths["calendar"],
        ("exchange_code", "trade_date", "is_open"),
        ((r["exchange_code"], r["trade_date"], r["is_open"]) for r in cal),
    )

    where, params = _scope(security_ids, "a")
    actions = db.fetchall(
        f"""
        SELECT a.security_id, a.ex_date, a.action_type, a.split_numerator,
               a.split_denominator, a.dividend_amount
          FROM core.corporate_action a
         WHERE a.action_type IN ('split', 'dividend'){where}
         ORDER BY a.security_id, a.ex_date, a.action_type
        """,
        params,
    )
    _write_csv(
        paths["actions"],
        (
            "security_id",
            "ex_date",
            "action_type",
            "split_numerator",
            "split_denominator",
            "dividend_amount",
        ),
        (
            (
                r["security_id"],
                r["ex_date"],
                r["action_type"],
                r["split_numerator"],
                r["split_denominator"],
                r["dividend_amount"],
            )
            for r in actions
        ),
    )

    where, params = _scope(security_ids, "s")
    meta = db.fetchall(
        f"""
        SELECT s.security_id, s.asset_type, s.exchange_code,
               {NAV_PRICED_PREDICATE} AS nav_priced
          FROM core.security s
         WHERE TRUE{where}
         ORDER BY s.security_id
        """,
        [list(NAV_LAGGING_ASSET_TYPES), *params],
    )
    _write_csv(
        paths["meta"],
        ("security_id", "asset_type", "nav_priced", "exchange"),
        (
            (
                r["security_id"],
                r["asset_type"],
                "true" if r["nav_priced"] else "false",
                r["exchange_code"],
            )
            for r in meta
        ),
    )
    return paths


def bars_copy_statement(
    bars_from: Optional[dt.date], security_ids: Optional[Sequence[int]]
) -> sql.Composed:
    """``COPY (SELECT ... FROM core.daily_price ...) TO STDOUT`` for one run.

    Ordered by security and date: dorq checks a large input as it streams, which
    needs every row of a series together. COPY takes no bind parameters, so the
    values are composed as literals.
    """
    clauses = [sql.SQL("TRUE")]
    if bars_from is not None:
        clauses.append(sql.SQL("trade_date >= {}").format(sql.Literal(bars_from)))
    if security_ids is not None:
        clauses.append(
            sql.SQL("security_id = ANY({}::bigint[])").format(
                sql.Literal(list(security_ids))
            )
        )
    return sql.SQL(
        "COPY (SELECT security_id, trade_date, open, high, low, close, volume"
        " FROM core.daily_price WHERE {} ORDER BY security_id, trade_date)"
        " TO STDOUT (FORMAT csv, HEADER)"
    ).format(sql.SQL(" AND ").join(clauses))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """What one dorq run produced."""

    version: str
    window: Window
    records: list[dict] = field(default_factory=list)
    bytes_sent: int = 0

    def by_check(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.records:
            out[r["check_name"]] = out.get(r["check_name"], 0) + 1
        return dict(sorted(out.items()))


def run_dorq(
    db: Database,
    settings: DorqSettings,
    exchange_code: str,
    window: Window,
    *,
    security_ids: Optional[Sequence[int]] = None,
) -> RunResult:
    """Export, run dorq, and parse its output. Writes nothing to the warehouse.

    The bars go from ``COPY`` straight into dorq's stdin, so the 150M-row history
    never lands on disk. dorq's stdout goes to a temporary file rather than a pipe
    -- reading a pipe while writing another invites a deadlock -- and is parsed
    once dorq has exited.
    """
    version = dorq_version(settings)
    with tempfile.TemporaryDirectory(prefix="fafnir-dorq-") as tmp:
        workdir = Path(tmp)
        paths = export_context(db, workdir, exchange_code, security_ids)
        args = settings.base_args() + [
            "-",
            "--format",
            "fafnir",
            "--exit-zero",
            "--calendar-file",
            str(paths["calendar"]),
            "--calendar-exchange",
            exchange_code,
            "--actions",
            str(paths["actions"]),
            "--meta",
            str(paths["meta"]),
        ]
        if window.as_of is not None:
            args += ["--as-of", window.as_of.isoformat()]
        if window.since is not None:
            args += ["--since", window.since.isoformat()]
        out_path = workdir / "out.jsonl"
        err_path = workdir / "err.txt"
        logger.info("dorq: %s", " ".join(args))
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=err)
            sent = _stream_bars(db, proc, window.bars_from, security_ids)
            status = proc.wait()
        if status != 0:
            tail = err_path.read_text(errors="replace").strip()[-2000:]
            raise DorqError(f"dorq exited {status}: {tail or 'no message'}")
        records = [
            json.loads(line)
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    logger.info(
        "dorq %s: %d violations from %.1f MB of bars", version, len(records), sent / 1e6
    )
    return RunResult(version=version, window=window, records=records, bytes_sent=sent)


def _stream_bars(
    db: Database,
    proc: subprocess.Popen,
    bars_from: Optional[dt.date],
    security_ids: Optional[Sequence[int]],
) -> int:
    """Copy the bars into dorq's stdin; return the bytes sent.

    COPY hands rows over one at a time, so they are gathered into blocks before
    each write: one system call per row would cost more than dorq's whole run on
    the full history. A broken pipe means dorq stopped reading -- it exited on an
    error -- so the copy stops and the caller reports dorq's own message.
    """
    block_size = 1 << 20
    sent = 0
    stdin = proc.stdin
    assert stdin is not None
    block = bytearray()
    try:
        with db.conn.cursor() as cur:
            with cur.copy(bars_copy_statement(bars_from, security_ids)) as copy:
                for chunk in copy:
                    block += chunk
                    if len(block) >= block_size:
                        stdin.write(block)
                        sent += len(block)
                        block.clear()
        if block:
            stdin.write(block)
            sent += len(block)
    except BrokenPipeError:
        logger.warning("dorq closed its input early")
    finally:
        try:
            stdin.close()
        except BrokenPipeError:
            pass
    return sent


# ---------------------------------------------------------------------------
# Ingest and shadow
# ---------------------------------------------------------------------------


def ingest(
    db: Database, records: Sequence[dict], *, ingestion_run_id: Optional[int] = None
) -> dict[str, int]:
    """Write dorq's rows to ``ops.data_quality_flag``; return new flags per check.

    One set-based insert, guarded exactly as the SQL checks are: a condition
    already open, or accepted, is not written again (two NOT EXISTS probes, each
    matched to its partial index -- see the module docstring of
    :mod:`fafnir.dq.checks`). ``security_id`` is NULL on a cross-sectional row (a
    failed load across a cohort belongs to no one security), so the probes match
    it as a value, the way ``ux_dq_flag_open_condition`` does.

    Within one run, two rows for the same condition (two missing fields on one
    bar) become one flag, the more severe.
    """
    if not records:
        return {}
    # One transaction, so the ON COMMIT DROP table lives exactly as long as the
    # insert that reads it -- under autocommit too, where it would otherwise be
    # dropped after the CREATE.
    with db.transaction():
        with db.conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE IF NOT EXISTS tmp_dorq_flag (
                    security_id bigint, table_name text, record_key jsonb,
                    check_name text, severity text, detail jsonb
                ) ON COMMIT DROP
                """)
            cur.execute("TRUNCATE tmp_dorq_flag")
            with cur.copy(
                "COPY tmp_dorq_flag (security_id, table_name, record_key, check_name,"
                " severity, detail) FROM STDIN"
            ) as copy:
                for r in records:
                    sid = r.get("security_id")
                    copy.write_row(
                        (
                            int(sid) if sid is not None else None,
                            r.get("table_name") or "core.daily_price",
                            json.dumps(r.get("record_key") or {}),
                            r["check_name"],
                            r.get("severity") or "warn",
                            json.dumps(r.get("detail") or {}),
                        )
                    )
        rows = db.fetchall(
            """
            WITH candidates AS (
                SELECT DISTINCT ON (check_name, COALESCE(security_id, -1), record_key)
                       security_id, table_name, record_key, check_name, severity, detail
                  FROM tmp_dorq_flag
                 ORDER BY check_name, COALESCE(security_id, -1), record_key,
                          CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END
            ),
            written AS (
                INSERT INTO ops.data_quality_flag
                    (ingestion_run_id, security_id, table_name, record_key, check_name,
                     severity, detail, detected_at)
                SELECT %s, c.security_id, c.table_name, c.record_key, c.check_name,
                       c.severity, c.detail, now()
                  FROM candidates c
                 WHERE NOT EXISTS (
                    SELECT 1 FROM ops.data_quality_flag f
                     WHERE f.check_name = c.check_name
                       AND (f.security_id = c.security_id
                            OR (f.security_id IS NULL AND c.security_id IS NULL))
                       AND f.record_key = c.record_key
                       AND f.resolved_at IS NULL
                 )
                   AND NOT EXISTS (
                    SELECT 1 FROM ops.data_quality_flag f
                     WHERE f.check_name = c.check_name
                       AND (f.security_id = c.security_id
                            OR (f.security_id IS NULL AND c.security_id IS NULL))
                       AND f.record_key = c.record_key
                       AND f.accepted_at IS NOT NULL
                 )
                RETURNING check_name
            )
            SELECT check_name, count(*) AS n FROM written GROUP BY check_name ORDER BY 1
            """,
            (ingestion_run_id,),
        )
    return {r["check_name"]: int(r["n"]) for r in rows}


def shadow_path(shadow_dir: str, as_of: Optional[dt.date]) -> Path:
    stamp = as_of.isoformat() if as_of else dt.date.today().isoformat()
    return Path(shadow_dir) / f"{stamp}.jsonl"


def write_shadow(records: Sequence[dict], path: Path) -> Path:
    """Write one shadow night: the rows dorq would have queued, as it emitted them.

    Written to a temporary name and renamed, so `dq compare` never reads half a
    night.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")))
            fh.write("\n")
    os.replace(tmp, path)
    return path


def read_shadow(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def start_run(db: Database, version: str, window: Window, shadow: bool) -> int:
    """An ``ops.ingestion_run`` row for the run: lineage for every flag it writes."""
    return int(
        db.fetchval(
            """
            INSERT INTO ops.ingestion_run
                (source, endpoint, params, window_from, window_to, status)
            VALUES (%s, %s, %s::jsonb, %s, %s, 'started')
            RETURNING ingestion_run_id
            """,
            (
                WATERMARK_SOURCE,
                WATERMARK_SHADOW if shadow else WATERMARK_QUEUE,
                json.dumps(
                    {
                        "dorq_version": version,
                        "since": window.since.isoformat() if window.since else None,
                        "shadow": shadow,
                    }
                ),
                window.bars_from,
                window.as_of,
            ),
        )
    )


def finish_run(
    db: Database, run_id: int, *, status: str, flagged: int = 0, error: str = ""
) -> None:
    db.execute(
        """
        UPDATE ops.ingestion_run
           SET status = %s, rows_inserted = %s, error_message = NULLIF(%s, ''),
               finished_at = now()
         WHERE ingestion_run_id = %s
        """,
        (status, flagged, error[:2000], run_id),
    )


@dataclass
class DqRunOutcome:
    """What `fafnir dq run --engine dorq` did, for the CLI to report."""

    version: str
    window: Window
    detected: dict[str, int]
    flagged: dict[str, int]
    shadow_file: Optional[Path] = None


def run(
    db: Database,
    settings: DorqSettings,
    exchange_code: str,
    *,
    shadow: bool,
    full: bool = False,
) -> DqRunOutcome:
    """One dorq pass over the warehouse: the nightly window, or ``full`` history.

    In shadow mode nothing is written to the queue; the rows go to the shadow file
    for `fafnir dq compare`. Either way the run is recorded in ops.ingestion_run
    and its watermark advanced, so the next night reports from where this one
    stopped.
    """
    endpoint = WATERMARK_SHADOW if shadow else WATERMARK_QUEUE
    window = (
        full_window(db, exchange_code)
        if full
        else nightly_window(db, exchange_code, settings.lookback_sessions, endpoint)
    )
    if window.as_of is None:
        logger.info("dorq: no bars on an open session yet; nothing to check")
        return DqRunOutcome(version="", window=window, detected={}, flagged={})
    result = run_dorq(db, settings, exchange_code, window)
    run_id = start_run(db, result.version, window, shadow)
    shadow_file = None
    flagged: dict[str, int] = {}
    if shadow:
        shadow_file = write_shadow(
            result.records, shadow_path(settings.shadow_dir, window.as_of)
        )
    else:
        flagged = ingest(db, result.records, ingestion_run_id=run_id)
    finish_run(db, run_id, status="success", flagged=sum(flagged.values()))
    write_watermark(db, endpoint, window.as_of)
    return DqRunOutcome(
        version=result.version,
        window=window,
        detected=result.by_check(),
        flagged=flagged,
        shadow_file=shadow_file,
    )


def record_key_json(record_key: Optional[dict]) -> str:
    """A record_key as canonical JSON, for comparing keys across sources."""
    return json.dumps(record_key or {}, sort_keys=True, separators=(",", ":"))


def code_of(record: dict) -> str:
    """The dorq check code (``DQ201``) of a fafnir-format row."""
    return str((record.get("detail") or {}).get("code") or "")

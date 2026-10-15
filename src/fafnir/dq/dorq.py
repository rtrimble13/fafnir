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
    ``dorq_``. Stale-feed reports on securities that have stopped trading are left
    out, as the SQL ``stale`` check leaves them out.
  * **ingest** -- the rows land in a temporary table and are written with one
    set-based ``INSERT ... SELECT`` carrying **the same two NOT EXISTS guards**
    (open and accepted) as every check in :mod:`fafnir.dq.checks`, so dedupe and
    acceptance behave exactly as they do today.
  * **shadow** -- or, until the shadow period has met the plan's cutover criteria,
    the rows go to ``<dorq_shadow_dir>/<as-of>.jsonl`` and nothing touches the queue.

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

#: dorq's stale-feed check (DQ304): bars that stop before the as-of date by more
#: sessions than the series' density explains. dorq cannot tell a delisting from a
#: feed outage, so every security that has stopped trading is stale to it, every
#: night. fafnir can tell, and :func:`fafnir.dq.checks.check_freshness` judges
#: only securities still actively trading; :func:`drop_not_trading` holds dorq's
#: reports to that same rule.
STALE_FEED_CHECK = "dorq_stale_feed"

#: The reports a backfill pass (:func:`backfill`) leaves out. A cohort of a few new
#: names means nothing, and the nightly window has already judged staleness.
BACKFILL_LEFT_OUT = CROSS_SECTIONAL_CHECKS | {STALE_FEED_CHECK}

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
    backfill_max: int = 500

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
        backfill_max=cfg.dorq_backfill_max,
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


def night_run(
    db: Database, endpoint: str, as_of: dt.date, *, nightly_only: bool = False
) -> Optional[dict]:
    """The newest successful run of ``endpoint`` for ``as_of``: the one whose shadow
    file stands for that night, since each run for a date replaces the file. None
    when there is none.

    ``nightly_only`` passes over ``--full`` runs. Those carry ``full`` in their
    params; a run from before that key was recorded is told by its window, since
    only a full run reads every bar (``window_from`` null).
    """
    return db.fetchone(
        """
        SELECT ingestion_run_id, started_at, window_from, params,
               params->>'since' AS since
          FROM ops.ingestion_run
         WHERE source = %s AND endpoint = %s AND window_to = %s AND status = 'success'
           AND (NOT %s
                OR NOT COALESCE((params->>'full')::boolean, window_from IS NULL))
         ORDER BY ingestion_run_id DESC
         LIMIT 1
        """,
        (WATERMARK_SOURCE, endpoint, as_of, nightly_only),
    )


def previous_night_start(
    db: Database, endpoint: str, as_of: dt.date
) -> Optional[dt.datetime]:
    """When the newest successful run of ``endpoint`` for a date before ``as_of``
    started: what a run for ``as_of`` -- the night's first, or a re-run of it --
    has had a chance to see. None before the first night."""
    return db.fetchval(
        """
        SELECT started_at FROM ops.ingestion_run
         WHERE source = %s AND endpoint = %s AND window_to < %s AND status = 'success'
         ORDER BY window_to DESC, ingestion_run_id DESC
         LIMIT 1
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

    A second run for a date already judged reports over the same span as that
    night's run did, not from five sessions before the night itself: its shadow
    file replaces the first's, so it has to be the same night's judgement. On
    unchanged data it is byte for byte the same file; after a repair, a fair
    re-judgement of the same span.
    """
    as_of = market_latest(db, exchange_code)
    if as_of is None:
        return Window(as_of=None)
    bars_from = session_back(db, exchange_code, as_of, lookback_sessions)
    previous = read_watermark(db, endpoint)
    if previous == as_of:
        first = night_run(db, endpoint, as_of, nightly_only=True)
        if first is not None:
            since = dt.date.fromisoformat(first["since"]) if first["since"] else None
            return Window(as_of=as_of, bars_from=bars_from, since=since)
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
    """What one dorq run produced.

    ``records`` are dorq's reports less those fafnir leaves out, which ``dropped``
    counts per check. ``config_hash`` is the hash dorq stamped on its reports
    (None when it reported nothing): it covers the config file, the command-line
    options and the calendar's content, so ``dorq config show`` cannot give it.
    """

    version: str
    window: Window
    records: list[dict] = field(default_factory=list)
    bytes_sent: int = 0
    config_hash: Optional[str] = None
    dropped: dict[str, int] = field(default_factory=dict)

    def by_check(self) -> dict[str, int]:
        return count_by_check(self.records)


def count_by_check(records: Iterable[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
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
    """Export, run dorq, and parse its output, less the stale-feed reports on
    securities not actively trading (:func:`drop_not_trading`). Writes nothing to
    the warehouse.

    The bars go from ``COPY`` straight into dorq's stdin, so the 150M-row history
    never lands on disk. dorq's stdout goes to a temporary file rather than a pipe
    -- reading a pipe while writing another invites a deadlock -- and is parsed
    once dorq has exited.

    The filter is applied here rather than by each caller, so the nightly run,
    ``--full`` and ``dq recheck`` all see the same reports.
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
            raise DorqError(f"dorq {version} exited {status}: {tail or 'no message'}")
        records = [
            json.loads(line)
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    logger.info(
        "dorq %s: %d violations from %.1f MB of bars", version, len(records), sent / 1e6
    )
    # Read before the filter: the hash is dorq's, whatever fafnir then leaves out.
    config_hash = (
        ((records[0].get("detail") or {}).get("dorq") or {}).get("config_hash")
        if records
        else None
    )
    records, dropped = drop_not_trading(db, records)
    return RunResult(
        version=version,
        window=window,
        records=records,
        bytes_sent=sent,
        config_hash=config_hash,
        dropped=dropped,
    )


def drop_not_trading(
    db: Database, records: Sequence[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Leave out the :data:`STALE_FEED_CHECK` reports on securities not actively
    trading; return the rows kept and the count left out per check.

    The rule is the SQL ``stale`` check's own (``WHERE s.is_actively_trading``),
    read when the run is made. Nothing else is touched: a delisted security's bars
    still go to dorq, and its history is judged for bad prints, splits and the
    rest as before, so the judgement carries no survivorship bias. A row with no
    security (a cross-sectional report) is kept.
    """

    def stale_feed(r: dict) -> bool:
        return r["check_name"] == STALE_FEED_CHECK and r.get("security_id") is not None

    ids = sorted({int(r["security_id"]) for r in records if stale_feed(r)})
    if not ids:
        return list(records), {}
    inactive = {
        int(r["security_id"])
        for r in db.fetchall(
            """
            SELECT security_id FROM core.security
             WHERE security_id = ANY(%s) AND NOT is_actively_trading
            """,
            (ids,),
        )
    }
    kept = [
        r for r in records if not (stale_feed(r) and int(r["security_id"]) in inactive)
    ]
    left_out = len(records) - len(kept)
    if not left_out:
        return kept, {}
    logger.info(
        "dorq: left out %d stale-feed reports on securities not actively trading",
        left_out,
    )
    return kept, {STALE_FEED_CHECK: left_out}


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
# Backfill: history the nightly window never reads
# ---------------------------------------------------------------------------
#
# The nightly window reads ``dorq_lookback_sessions`` of bars and reports from
# ``since``, so a bar is judged on the nights whose window it falls in. History
# that arrives, or changes, *behind* the window falls in none: a security minted
# tonight with years of bars (CELG-RI, minted 2026-10-13 with bars back to 2022),
# or an era an operator rescaled. The SQL ``outlier`` and ``gap`` checks have no
# window and rescan every security's whole history each night, so they judge it on
# its first night. Without this pass dorq never would.


def backfill_securities(db: Database, endpoint: str, window: Window) -> list[int]:
    """The securities whose history before ``window`` is new or was rewritten since
    the run for the previous night started (:func:`previous_night_start`).

    * **New:** minted since (``core.security.first_seen_at``), with bars before the
      window. A listing with no older bars is the window's alone.
    * **Rewritten:** an operator override made or revoked since, on a date before
      the window: ``prices delete|shift|rescale``, ``actions add|delete|redate``,
      ``security split-history`` -- the source, and the destination its overrides
      name.

    Not caught: ``security merge`` (and ``merge-rename``, ``dedupe``), which moves
    one security's bars onto another and leaves no dated record; nor a history
    re-pulled for a security that already had one. Those want ``--full``.

    Empty on the first night, and on a ``--full`` window: there is no "before".
    """
    horizon = window.since or window.bars_from
    if window.as_of is None or horizon is None:
        return []
    after = previous_night_start(db, endpoint, window.as_of)
    if after is None:
        return []
    rows = db.fetchall(
        """
        WITH touched AS (
            SELECT security_id, detail FROM ops.operator_override
             WHERE (created_at > %(after)s OR revoked_at > %(after)s)
               AND key_date < %(horizon)s
        )
        SELECT s.security_id FROM core.security s
         WHERE s.first_seen_at > %(after)s
           AND EXISTS (SELECT 1 FROM core.daily_price p
                        WHERE p.security_id = s.security_id
                          AND p.trade_date < %(horizon)s)
        UNION
        SELECT security_id FROM touched
        UNION
        SELECT (detail->'split'->>'destination_security_id')::bigint FROM touched
         WHERE detail->'split'->>'destination_security_id' IS NOT NULL
        ORDER BY 1
        """,
        {"after": after, "horizon": horizon},
    )
    return [int(r["security_id"]) for r in rows]


def report_date(record: dict) -> Optional[dt.date]:
    """The date a fafnir-format row is about: its ``record_key``'s ``trade_date``,
    else dorq's ``date``. None when it has neither."""
    text = (record.get("record_key") or {}).get("trade_date") or (
        record.get("detail") or {}
    ).get("date")
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def member_ids(values) -> list[int]:
    """The securities a cross-sectional row names (``detail.detail.members``).

    dorq 0.9 writes them as a comma-separated string (``"9161,10640"``): every value
    in a report's ``detail.detail`` is a scalar. A list is read too. dorq up to
    0.8.1 writes none.
    """
    if isinstance(values, str):
        values = values.split(",")
    out = set()
    for v in values if isinstance(values, (list, tuple)) else ():
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def condition_record_key(record: dict) -> dict:
    """The record_key that tells one of dorq's conditions from another.

    A row about one security is keyed as dorq keys it. A cross-sectional row (no
    security) is not: dorq up to 0.9 keys a cohort row on its date alone, so a 2:1
    group and a 3:2 group on one date (DQ601, 2026-10-06) share a key, and any
    (check, security, record_key) dedupe merges them -- in the compare's sets, in
    the backfill's, and in the queue, where the second is never written. The
    group's ``ratio`` is added, unless dorq has put one in the key itself (dorq
    0.10 does).

    Not its ``members``: a cohort's membership changes from night to night as
    evidence arrives (the 3:2 group of 2026-10-06 named six series on the 10-12
    night and five on 10-13), and dorq up to 0.8.1 writes none, so keying on it
    would make the same cohort a new condition every time it was re-judged. The
    members are used only for a row that carries no ratio at all.
    """
    key = dict(record.get("record_key") or {})
    if record.get("security_id") is not None:
        return key
    inner = (record.get("detail") or {}).get("detail")
    inner = inner if isinstance(inner, dict) else {}
    if "ratio" in key or "members" in key:
        return key
    if inner.get("ratio") not in (None, ""):
        key["ratio"] = inner["ratio"]
    else:
        members = member_ids(inner.get("members"))
        if members:
            key["members"] = members
    return key


def condition_key(record: dict) -> tuple:
    """One condition, as (check_name, security_id, record_key as canonical JSON):
    what the compare counts, the backfill dedupes and the queue writes once."""
    sid = record.get("security_id")
    return (
        record["check_name"],
        int(sid) if sid is not None else None,
        record_key_json(condition_record_key(record)),
    )


def backfill(
    db: Database,
    settings: DorqSettings,
    exchange_code: str,
    window: Window,
    security_ids: Sequence[int],
    records: Sequence[dict],
) -> tuple[list[dict], dict]:
    """Judge the whole history of ``security_ids`` and add what it finds before the
    window to the night's ``records``. Returns the records and what the pass did,
    for the run's ``params``.

    It is the call ``dq recheck`` makes: every bar of those securities, no
    ``--since``. Of its reports it keeps those dated before the window -- the
    window's own dates are the nightly run's to judge, with the context it had --
    less :data:`BACKFILL_LEFT_OUT`, and less any the night already holds.

    Above ``settings.backfill_max`` securities it logs a warning and judges none.
    """
    before = window.since or window.bars_from
    info: dict = {
        "securities": len(security_ids),
        "security_ids": list(security_ids),
        "before": before.isoformat() if before else None,
        "reports": 0,
    }
    if not security_ids or before is None:
        return list(records), info
    if len(security_ids) > settings.backfill_max:
        logger.warning(
            "dorq: %d securities have new or rewritten history before %s, over "
            "[dq] dorq_backfill_max (%d): their history is not judged tonight. "
            "A universe rebuild wants `fafnir dq run --engine dorq --full`.",
            len(security_ids),
            before,
            settings.backfill_max,
        )
        info["security_ids"] = []
        info["skipped"] = f"over dorq_backfill_max ({settings.backfill_max})"
        return list(records), info
    result = run_dorq(
        db,
        settings,
        exchange_code,
        Window(as_of=window.as_of),
        security_ids=security_ids,
    )
    seen = {condition_key(r) for r in records}
    added = []
    for r in result.records:
        if r["check_name"] in BACKFILL_LEFT_OUT:
            continue
        day = report_date(r)
        if day is not None and day >= before:
            continue
        if condition_key(r) in seen:
            continue
        seen.add(condition_key(r))
        added.append(r)
    info["reports"] = len(added)
    logger.info(
        "dorq: backfill judged the whole history of %d securities, %d reports "
        "before %s",
        len(security_ids),
        len(added),
        before,
    )
    return list(records) + added, info


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
                            # The condition's key, not dorq's alone: two cohorts on
                            # one date are two conditions (condition_record_key).
                            json.dumps(condition_record_key(r)),
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


def start_run(db: Database, window: Window, shadow: bool, full: bool = False) -> int:
    """An ``ops.ingestion_run`` row for the run: lineage for every flag it writes.

    Opened before dorq runs, so a run that fails still has a row. The version is
    known only once dorq has answered, so :func:`finish_run` fills it in.
    """
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
                        "dorq_version": None,
                        "since": window.since.isoformat() if window.since else None,
                        "shadow": shadow,
                        "full": full,
                    }
                ),
                window.bars_from,
                window.as_of,
            ),
        )
    )


def finish_run(
    db: Database,
    run_id: int,
    *,
    status: str,
    flagged: int = 0,
    error: str = "",
    params: Optional[dict] = None,
) -> None:
    """Close the run's row, merging ``params`` into those it opened with.

    ``finished_at`` is ``clock_timestamp()``, not ``now()``: ``now()`` is when the
    transaction began, which is before dorq ran.
    """
    db.execute(
        """
        UPDATE ops.ingestion_run
           SET status = %s, rows_inserted = %s, error_message = NULLIF(%s, ''),
               params = params || %s::jsonb, finished_at = clock_timestamp()
         WHERE ingestion_run_id = %s
        """,
        (status, flagged, error[:2000], json.dumps(params or {}), run_id),
    )


@dataclass
class DqRunOutcome:
    """What `fafnir dq run --engine dorq` did, for the CLI to report."""

    version: str
    window: Window
    detected: dict[str, int]
    flagged: dict[str, int]
    shadow_file: Optional[Path] = None
    backfill: Optional[dict] = None


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

    A nightly run then judges the history before its window that is new or was
    rewritten (:func:`backfill_securities`, :func:`backfill`), and records which
    securities in ``params.backfill``. A failure there fails the run, so the
    watermark stays and the next night takes the same securities again.

    The run's row is committed before dorq starts, and its outcome when it ends,
    failure included: ``ops.ingestion_run`` is the only record of a run an agent
    without the journal can read. A failed run is re-raised, so the CLI still
    exits non-zero, and leaves the watermark where it was.
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
    run_id = start_run(db, window, shadow, full=full)
    db.commit()
    try:
        result = run_dorq(db, settings, exchange_code, window)
        records = result.records
        backfilled = None
        if not full:
            records, backfilled = backfill(
                db,
                settings,
                exchange_code,
                window,
                backfill_securities(db, endpoint, window),
                records,
            )
        shadow_file = None
        flagged: dict[str, int] = {}
        if shadow:
            shadow_file = write_shadow(
                records, shadow_path(settings.shadow_dir, window.as_of)
            )
        else:
            flagged = ingest(db, records, ingestion_run_id=run_id)
        holding = _holding_snapshot(db, run_id, window, backfilled)
    except BaseException as exc:
        # As RunLog does: a COPY cut short when dorq exits can leave the
        # transaction aborted, so discard the uncommitted tail before recording
        # the failure.
        db.rollback()
        finish_run(db, run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        db.commit()
        raise
    finish_run(
        db,
        run_id,
        status="success",
        flagged=sum(flagged.values()),
        params={
            "dorq_version": result.version,
            "dorq_config_hash": result.config_hash,
            "dropped": result.dropped,
            **({"backfill": backfilled} if backfilled is not None else {}),
            **({"holding": holding} if holding is not None else {}),
        },
    )
    write_watermark(db, endpoint, window.as_of)
    db.commit()
    return DqRunOutcome(
        version=result.version,
        window=window,
        detected=count_by_check(records),
        flagged=flagged,
        shadow_file=shadow_file,
        backfill=backfilled,
    )


def _holding_snapshot(
    db: Database, run_id: int, window: Window, backfilled: Optional[dict]
) -> Optional[dict]:
    """The SQL flags' state as the run found them, for `dq compare` (C3).

    ``core.daily_price`` as it stands later is not what the night read: the vendor
    delivers missing bars late, so a stale flag that held when dorq ran reads as
    "traded since" a day later, and the re-read overwrites ``loaded_at`` and
    ``ingestion_run_id``, so the run-time state cannot be rebuilt from the bars.
    Computed by the comparison's own rule, as of the run's start.
    """
    from fafnir.dq import compare as cmp

    started_at = db.fetchval(
        "SELECT started_at FROM ops.ingestion_run WHERE ingestion_run_id = %s",
        (run_id,),
    )
    snapshot = cmp.holding_snapshot(
        db,
        window_from=window.since or window.bars_from,
        as_of=window.as_of,
        started_at=started_at,
        backfilled=(backfilled or {}).get("security_ids") or [],
    )
    if snapshot is None:
        logger.warning(
            "dorq: more than %d open SQL flags to snapshot; `dq compare` will "
            "recompute this run's holding states instead",
            cmp.SNAPSHOT_MAX_FLAGS,
        )
    return snapshot


def record_key_json(record_key: Optional[dict]) -> str:
    """A record_key as canonical JSON, for comparing keys across sources."""
    return json.dumps(record_key or {}, sort_keys=True, separators=(",", ":"))


def code_of(record: dict) -> str:
    """The dorq check code (``DQ201``) of a fafnir-format row."""
    return str((record.get("detail") or {}).get("code") or "")

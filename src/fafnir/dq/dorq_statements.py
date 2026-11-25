"""dorq on the statements: export, run, ingest, shadow (dorq plan §5, DR-0912).

dorq 0.13 reads financial statements as a third input kind (DQ901 to DQ951). This
module runs it on the three statement tables beside the bars run of
:mod:`fafnir.dq.dorq`, which it leaves exactly as it was:

  * **export** -- every current version (``superseded_at IS NULL``) of
    ``core.income_statement``, ``core.balance_sheet`` and
    ``core.cash_flow_statement`` streams into dorq's stdin from one ``COPY``,
    ordered by security, statement, period type, fiscal date and version. Each
    row carries ``close``, the raw close on or before its ``known_from``, from a
    lateral join on ``core.daily_price``. Beside it go three files:
    ``ref.statement_line_item`` (``--items``), the splits SF1's share counts were
    stored by (``--actions``: Sharadar's ACTIONS file, :func:`share_basis`) and the
    security master (``--meta``)
    with the trading span, the active and delisted state, the currency, the sector
    and industry, and ``statements_expected`` (the vendor master's
    ``first_quarter``).
  * **run** -- ``dorq check --kind statements --format fafnir``, as of the market's
    latest session, at warn and above (DE-15): about 283k info reports a night, 87k
    of them DQ919's, that nothing at warn and above reads. ``[dq]
    dorq_statements_info`` or ``dq run --statements-info`` adds ``--show-info``;
    ``dq recheck`` always does, so a flag whose condition is still reported at
    info is not closed.
  * **window** -- the whole history, every night: the statements are small, and
    the plan's runtime target is two minutes on the host (plan §5.2, F4).
  * **ingest** -- :func:`fafnir.dq.dorq.ingest`, the open and accepted guards
    included, with each row's ``table_name`` its statement's table, less the rows
    an accepted flag of the SQL check it succeeds carries
    (:class:`fafnir.dq.compare_statements.AcceptedPredecessors`), and less those
    the queue holds under the key an earlier dorq gave them
    (:class:`fafnir.dq.compare_statements.HeldUnderEarlierKeys`).
  * **shadow** -- or the rows go to ``<dorq_shadow_dir>/statements/<as-of>.jsonl``:
    never the shadow directory's top level, where ``dq compare`` takes the last
    ``*.jsonl`` as the newest night of bars.
  * **memory** -- dorq's output goes to a file in the run's working directory on
    disk (:func:`fafnir.dq.dorq.work_dir`), and the nightly run streams it from
    there to the shadow file or the queue, a row at a time. The night's reports
    are never held: with ``--show-info`` over the whole history they are many, and
    dorq itself holds them while DQ951 is on (#219).

A run is recorded in ``ops.ingestion_run`` (``source = 'dorq'``) under its own
endpoints, ``dq-statements`` and ``dq-statements-shadow``, each with its own
watermark, so the bars run's window and history pass never see it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from psycopg import sql

from fafnir.db import repository as repo
from fafnir.db.connection import Database
from fafnir.dq import dorq as engine
from fafnir.dq.checks import STATEMENT_TABLES, statements_exist
from fafnir.ingest.fundamentals_map import TABLES
from fafnir.logging_config import get_logger

logger = get_logger("dq")

#: The run's ``ops.ingestion_run`` endpoints and ``ops.load_watermark`` rows.
ENDPOINT_QUEUE = "dq-statements"
ENDPOINT_SHADOW = "dq-statements-shadow"

#: The statements' shadow files go here, under ``dorq_shadow_dir``.
SHADOW_SUBDIR = "statements"

#: The prefix of a statements run's working directory (under
#: :func:`fafnir.dq.dorq.work_root`, pruned with the bars run's).
WORK_PREFIX = engine.WORK_PREFIX + "statements-"

#: The table a finding on no one statement is filed under (DQ943, DQ951), as the
#: SQL statement checks file theirs. dorq writes it from its config's
#: ``[fafnir] table_name``, which an ``--isolated`` run leaves at
#: ``core.daily_price``.
DEFAULT_TABLE = "core.income_statement"

#: dorq 0.13's statement checks, by code: the ``check_name`` each writes. The
#: fafnir-dba skill places each in a sweep tier and gives each a playbook entry
#: (``test_every_statement_check_has_a_tier_and_a_playbook``).
STATEMENT_CHECKS = {
    "DQ901": "dorq_statement_key",
    "DQ902": "dorq_impossible_value",
    "DQ903": "dorq_identity_break",
    "DQ904": "dorq_period_tie",
    "DQ911": "dorq_bad_value",
    "DQ912": "dorq_unit_error",
    "DQ913": "dorq_sign_error",
    "DQ914": "dorq_copied_period",
    "DQ915": "dorq_absent_as_zero",
    "DQ916": "dorq_cumulative_quarters",
    "DQ919": "dorq_large_change",
    "DQ921": "dorq_share_terms_step",
    "DQ922": "dorq_share_count_mismatch",
    "DQ931": "dorq_filing_date_implausible",
    "DQ932": "dorq_restatement_implausible",
    "DQ941": "dorq_period_gap",
    "DQ942": "dorq_statements_stale",
    "DQ943": "dorq_statements_missing",
    "DQ944": "dorq_reporting_currency",
    "DQ951": "dorq_revision_cohort",
}

#: DQ901: a version chain that does not chain. Its repair is made on the host, so
#: it is never auto-resolved (``fafnir_mcp.tools.NEVER_AUTO_RESOLVE``).
STATEMENT_KEY_CHECK = "dorq_statement_key"

#: How far before a version's ``known_from`` its ``close`` may be: a security
#: that has not traded for a quarter has no price to sharpen DQ921 with, and the
#: row goes without one (dorq reads ``close`` as optional).
CLOSE_REACH_DAYS = 92

#: The statement tables by dorq's statement name, in the order the export reads
#: them.
STATEMENT_TABLE = dict(TABLES)


def endpoint_for(shadow: bool) -> str:
    return ENDPOINT_SHADOW if shadow else ENDPOINT_QUEUE


def shadow_dir(settings: engine.DorqSettings) -> Path:
    """Where the statements' shadow files go: a directory of their own."""
    return Path(settings.shadow_dir) / SHADOW_SUBDIR


def is_statement_flag(table_name: Optional[str]) -> bool:
    """Whether a flag (or a fafnir-format row) is about the statements."""
    return table_name in STATEMENT_TABLES


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def line_items(db: Database) -> list[dict]:
    """``ref.statement_line_item``, the taxonomy dorq reads as ``--items``."""
    return db.fetchall("""
        SELECT statement, column_name, item_class, sign_convention, is_subtotal
          FROM ref.statement_line_item
         ORDER BY statement, column_name
        """)


def _scope(security_ids: Optional[Sequence[int]], alias: str) -> tuple[str, list]:
    if security_ids is None:
        return "", []
    return f" AND {alias}.security_id = ANY(%s)", [list(security_ids)]


#: Where a statements run's splits came from (``params.split_basis``).
BASIS_ACTIONS = "sharadar_actions"
BASIS_STORED = "core.corporate_action"


def share_basis(
    db: Database,
    bulk_dir: str,
    as_of: Optional[dt.date],
    security_ids: Optional[Sequence[int]] = None,
) -> tuple[list[tuple], dict]:
    """The splits SF1's share counts were stored by, as ``(security_id, ex_date,
    numerator, denominator)``, and where they came from (DE-15).

    The SF1 loader stores a version's counts in the terms of its ``known_from``:
    SF1's value divided by Sharadar's splits after it, from the ledger's current
    ACTIONS file (ADR 0018 §4; :mod:`fafnir.ingest.fundamentals`). dorq puts each
    version's counts back into one terms with the splits it is sent, so those are
    the ones to send: they undo the loader's division exactly, and a step dorq
    finds is SF1's own. ``core.corporate_action`` is not that list. Its split rows
    include the spin-offs stored as a split's price factor (spin-as-split, #283),
    FMP's and the operator's, none of which SF1 was adjusted by, and each would
    read as a step in the counts (DQ921, DQ922).

    The ACTIONS file's splits, one per ex-date, none after ``as_of``, for each
    Sharadar link's key (open or closed: a security's statements may have come
    through either), on the linked securities. With no current ACTIONS file on
    disk, the ACTIONS rows ``core.corporate_action`` holds (``source =
    'sharadar'``), its nearest record of them.
    """
    from fafnir.ingest import sharadar_actions as sa
    from fafnir.ingest import sharadar_ledger as ledger
    from fafnir.ingest import sharadar_map
    from fafnir.ingest.sharadar_master import SECURITY_TABLES
    from fafnir.sources.sharadar_files import SharadarFileError

    wanted = set(security_ids) if security_ids is not None else None
    actions = repo.current_sharadar_full(db, "actions") if bulk_dir else None
    if actions is not None:
        keys: dict[str, set[int]] = {}
        for link in repo.vendor_links(db, sa.VENDOR):
            sid = int(link["security_id"])
            if wanted is None or sid in wanted:
                keys.setdefault(link["vendor_key"], set()).add(sid)
        key_of = repo.vendor_keys_by_ticker(db, sa.VENDOR, SECURITY_TABLES)
        try:
            found = sa.key_actions(
                ledger.rows(db, actions["file_id"], bulk_dir), set(keys), key_of
            )
        except SharadarFileError as exc:
            logger.warning(
                "dorq statements: the ACTIONS file cannot be read (%s); sending the "
                "ACTIONS rows core.corporate_action holds",
                exc,
            )
        else:
            by_day: dict[tuple[int, dt.date], tuple[int, int]] = {}
            for key, kinds in found.items():
                for day, value in kinds.get("split", ()):
                    if not value or value <= 0 or (as_of is not None and day > as_of):
                        continue
                    ratio = sharadar_map.split_ratio(value)
                    for sid in keys[key]:
                        by_day.setdefault((sid, day), ratio)
            rows = sorted((sid, day, n, d) for (sid, day), (n, d) in by_day.items())
            return rows, {"split_basis": BASIS_ACTIONS, "file_id": actions["file_id"]}
    where, params = _scope(security_ids, "a")
    stored = db.fetchall(
        f"""
        SELECT a.security_id, a.ex_date, a.split_numerator, a.split_denominator
          FROM core.corporate_action a
         WHERE a.action_type = 'split' AND a.source = 'sharadar'{where}
           AND (%s::date IS NULL OR a.ex_date <= %s::date)
         ORDER BY a.security_id, a.ex_date
        """,
        [*params, as_of, as_of],
    )
    rows = [
        (r["security_id"], r["ex_date"], r["split_numerator"], r["split_denominator"])
        for r in stored
    ]
    return rows, {"split_basis": BASIS_STORED}


def write_share_basis(
    db: Database,
    path: Path,
    bulk_dir: str,
    as_of: Optional[dt.date],
    security_ids: Optional[Sequence[int]] = None,
) -> dict:
    """:func:`share_basis` as dorq's ``--actions`` file; returns where the splits
    came from, with their count."""
    rows, basis = share_basis(db, bulk_dir, as_of, security_ids)
    engine.write_csv(
        path,
        (
            "security_id",
            "ex_date",
            "action_type",
            "split_numerator",
            "split_denominator",
            "dividend_amount",
        ),
        ((sid, day, "split", n, d, None) for sid, day, n, d in rows),
    )
    return {**basis, "splits": len(rows)}


def export_context(
    db: Database,
    workdir: Path,
    items: Sequence[dict],
    security_ids: Optional[Sequence[int]] = None,
    *,
    bulk_dir: str = "",
    as_of: Optional[dt.date] = None,
) -> dict[str, Path]:
    """Write the line items, the share terms' splits (:func:`share_basis`) and the
    security metadata dorq reads. ``paths["basis"]`` is a dict, not a path: where
    the splits came from.

    The metadata is what DQ94x judges coverage and freshness by (dorq's
    doc/context.md):

    * ``first_trade_date`` and ``last_trade_date``, the bars the warehouse holds
      (two index probes a security, as ``mart.v_security_lifecycle`` reads them);
    * ``active`` (``is_actively_trading``) and ``delisted_date``;
    * ``currency``, the trading currency DQ944 compares the reporting currency
      with;
    * ``sector`` and ``industry``, as readers see them
      (``core.v_security_classification``), which dorq's profiles can match on;
    * ``statements_expected``: the security's open Sharadar link names a
      fundamentals master row with a ``first_quarter``, the rule
      ``fundamentals_coverage`` uses.
    """
    paths = {
        "items": workdir / "items.csv",
        "actions": workdir / "actions.csv",
        "meta": workdir / "meta.csv",
    }
    engine.write_csv(
        paths["items"],
        ("column", "statement", "item_class", "sign_convention", "is_subtotal"),
        (
            (
                r["column_name"],
                r["statement"],
                r["item_class"],
                r["sign_convention"],
                "true" if r["is_subtotal"] else "false",
            )
            for r in items
        ),
    )
    basis = write_share_basis(db, paths["actions"], bulk_dir, as_of, security_ids)

    where, params = _scope(security_ids, "s")
    meta = db.fetchall(
        f"""
        SELECT s.security_id, s.asset_type, s.exchange_code, s.currency,
               (SELECT min(p.trade_date) FROM core.daily_price p
                 WHERE p.security_id = s.security_id) AS first_trade_date,
               (SELECT max(p.trade_date) FROM core.daily_price p
                 WHERE p.security_id = s.security_id) AS last_trade_date,
               s.is_actively_trading, s.delisted_date,
               r.sector_name, i.industry_name,
               EXISTS (
                   SELECT 1 FROM core.security_vendor_xref x
                     JOIN core.vendor_security v
                       ON v.vendor = 'sharadar' AND v.vendor_table = 'fundamentals'
                      AND v.vendor_key = x.vendor_key AND v.first_quarter IS NOT NULL
                    WHERE x.security_id = s.security_id AND x.vendor = 'sharadar'
                      AND x.valid_to IS NULL) AS statements_expected
          FROM core.security s
          LEFT JOIN core.v_security_classification c ON c.security_id = s.security_id
          LEFT JOIN ref.sector r ON r.sector_id = c.sector_id
          LEFT JOIN ref.industry i ON i.industry_id = c.industry_id
         WHERE TRUE{where}
         ORDER BY s.security_id
        """,
        params,
    )
    engine.write_csv(
        paths["meta"],
        (
            "security_id",
            "asset_type",
            "exchange",
            "currency",
            "first_trade_date",
            "last_trade_date",
            "active",
            "delisted_date",
            "sector",
            "industry",
            "statements_expected",
        ),
        (
            (
                r["security_id"],
                r["asset_type"],
                r["exchange_code"],
                r["currency"],
                r["first_trade_date"],
                r["last_trade_date"],
                "true" if r["is_actively_trading"] else "false",
                r["delisted_date"],
                r["sector_name"],
                r["industry_name"],
                "true" if r["statements_expected"] else "false",
            )
            for r in meta
        ),
    )
    return {**paths, "basis": basis}


#: The columns before the line items, as dorq's statements reader names them
#: (dorq's doc/statements.md). ``loaded_on`` is the day, in UTC, the row was
#: loaded: DQ951 counts the findings in rows loaded on one day.
_FIELDS = (
    "security_id",
    "statement",
    "period_type",
    "fiscal_date",
    "fiscal_year",
    "fiscal_period",
    "version_no",
    "known_from",
    "known_to",
    "knowledge_source",
    "filing_date",
    "loaded_on",
    "reported_currency",
)


def statements_copy_statement(
    items: Sequence[dict], security_ids: Optional[Sequence[int]] = None
) -> sql.Composed:
    """``COPY (...) TO STDOUT`` of every current version of the three tables.

    One stream: each table's rows carry every line item, NULL for another
    statement's (dorq reads NULL as "does not apply"), so the three tables share
    one header. Ordered by security, statement, period type, fiscal date and
    version: dorq checks a large input as it streams, which needs every row of a
    security together, and the order makes the stream the same every night the
    tables are.

    ``close`` is the raw close on or before ``known_from`` (dorq's share-terms
    checks, DQ921), within :data:`CLOSE_REACH_DAYS` of it. It is looked up once
    per security and ``known_from`` -- the three statements of a version share one
    -- and joined back to each row. The reach keeps each lookup to the one or two
    yearly partitions of ``core.daily_price`` it can fall in; without it every
    lookup probes every partition before its date. COPY takes no bind parameters,
    so values are composed as literals.
    """
    names = [str(i["column_name"]) for i in items]
    where = sql.SQL("superseded_at IS NULL")
    if security_ids is not None:
        where = sql.SQL("{} AND security_id = ANY({}::bigint[])").format(
            where, sql.Literal(list(security_ids))
        )
    rows, known = [], []
    for statement, table in STATEMENT_TABLE.items():
        own = {str(i["column_name"]) for i in items if i["statement"] == statement}
        values = [
            (
                sql.Identifier(n)
                if n in own
                else sql.SQL("NULL::numeric AS {}").format(sql.Identifier(n))
            )
            for n in names
        ]
        source = sql.Identifier(*table.split("."))
        rows.append(
            sql.SQL(
                "SELECT security_id, {statement} AS statement, period_type,"
                " fiscal_date, fiscal_year, fiscal_period, version_no, known_from,"
                " known_to, knowledge_source, filing_date,"
                " (loaded_at AT TIME ZONE 'UTC')::date AS loaded_on,"
                " reported_currency{values} FROM {source} WHERE {where}"
            ).format(
                statement=sql.Literal(statement),
                values=sql.SQL("").join(sql.SQL(", ") + v for v in values),
                source=source,
                where=where,
            )
        )
        known.append(
            sql.SQL("SELECT security_id, known_from FROM {} WHERE {}").format(
                source, where
            )
        )
    columns = sql.SQL(", ").join(
        [sql.SQL("t.{}").format(sql.Identifier(f)) for f in _FIELDS]
        + [sql.SQL("c.close")]
        + [sql.SQL("t.{}").format(sql.Identifier(n)) for n in names]
    )
    return sql.SQL(
        "COPY ("
        "WITH c AS MATERIALIZED ("
        "SELECT k.security_id, k.known_from, p.close FROM ({known}) k"
        " LEFT JOIN LATERAL (SELECT d.close FROM core.daily_price d"
        " WHERE d.security_id = k.security_id AND d.trade_date <= k.known_from"
        " AND d.trade_date > k.known_from - {reach}"
        " ORDER BY d.trade_date DESC LIMIT 1) p ON TRUE)"
        " SELECT {columns} FROM ({rows}) t"
        " JOIN c ON c.security_id = t.security_id AND c.known_from = t.known_from"
        " ORDER BY t.security_id, t.statement, t.period_type, t.fiscal_date,"
        " t.version_no"
        ") TO STDOUT (FORMAT csv, HEADER)"
    ).format(
        known=sql.SQL(" UNION ").join(known),
        reach=sql.Literal(CLOSE_REACH_DAYS),
        columns=columns,
        rows=sql.SQL(" UNION ALL ").join(rows),
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def file_under_statement(records: Iterable[dict]) -> list[dict]:
    """Each row with a statement table: its own, or :data:`DEFAULT_TABLE` for a
    finding on no one statement, whatever the config's ``[fafnir] table_name``."""
    return list(filed_under_statement(records))


def filed_under_statement(records: Iterable[dict]) -> Iterator[dict]:
    """:func:`file_under_statement`, one row at a time."""
    for r in records:
        if not is_statement_flag(r.get("table_name")):
            r = {**r, "table_name": DEFAULT_TABLE}
        yield r


def judge(
    db: Database,
    settings: engine.DorqSettings,
    as_of: Optional[dt.date],
    workdir: Path,
    *,
    security_ids: Optional[Sequence[int]] = None,
    show_info: bool = True,
) -> tuple[str, engine.Invocation]:
    """Export the statements and run dorq on them, its output left on disk in
    ``workdir``; return dorq's version and the finished run. ``show_info`` adds
    dorq's ``--show-info``: the nightly passes ``[dq] dorq_statements_info``
    (false by default), a re-run for ``dq recheck`` always reports info. Writes nothing to the
    warehouse, and holds none of dorq's rows: the caller reads them, all at once
    (:func:`run_statements`) or as a stream (:func:`run`).

    The whole history of ``security_ids`` (every security by default), as of
    ``as_of`` (dorq's ``--as-of``: DQ942 judges staleness against it). With
    ``security_ids``, the metadata is theirs alone, so dorq does not take every
    other security for one with no statements (DQ943).
    """
    version = engine.dorq_version(settings)
    engine.require_supported(version)
    items = line_items(db)
    paths = export_context(
        db,
        workdir,
        items,
        security_ids,
        bulk_dir=settings.sharadar_bulk_dir,
        as_of=as_of,
    )
    args = settings.base_args(settings.statements_config) + [
        "-",
        "--kind",
        "statements",
        "--format",
        "fafnir",
        "--exit-zero",
        *(["--show-info"] if show_info else []),
        "--items",
        str(paths["items"]),
        "--actions",
        str(paths["actions"]),
        "--meta",
        str(paths["meta"]),
    ]
    if as_of is not None:
        args += ["--as-of", as_of.isoformat()]
    done = engine.run_to_file(
        db, args, statements_copy_statement(items, security_ids), workdir, version
    )
    done.sent.update(paths["basis"])
    logger.info(
        "dorq %s: %.1f MB of statements in, %.1f MB of reports out, at a peak of %s MB",
        version,
        done.bytes_sent / 1e6,
        done.bytes_out / 1e6,
        done.usage()["dorq_max_rss_mb"],
    )
    return version, done


def run_statements(
    db: Database,
    settings: engine.DorqSettings,
    as_of: Optional[dt.date],
    *,
    security_ids: Optional[Sequence[int]] = None,
) -> engine.RunResult:
    """:func:`judge`, with dorq's rows parsed into a list. For a run over a few
    securities (``dq recheck``); the nightly run over every security streams them
    instead (:func:`run`)."""
    window = engine.Window(as_of=as_of)
    with engine.work_dir(settings, WORK_PREFIX) as workdir:
        version, done = judge(db, settings, as_of, workdir, security_ids=security_ids)
        records = list(engine.read_records(done.out_path))
    logger.info("dorq %s: %d statement violations", version, len(records))
    return engine.RunResult(
        version=version,
        window=window,
        records=file_under_statement(records),
        bytes_sent=done.bytes_sent,
        config_hash=engine.config_hash_of(records),
    )


def run(
    db: Database,
    settings: engine.DorqSettings,
    exchange_code: str,
    *,
    shadow: bool,
    rerun: bool = False,
    show_info: Optional[bool] = None,
) -> engine.DqRunOutcome:
    """One statements night: the whole history, as of the market's latest session,
    at warn and above unless ``show_info`` (``[dq] dorq_statements_info`` when
    None; DE-15).

    **A night is judged once**, as for bars (:func:`fafnir.dq.dorq.run`): when a
    statements run has already judged the market's latest session, this one
    judges nothing and returns with ``judged_by`` set. ``rerun`` judges it again,
    replacing its shadow file.

    In shadow mode the rows go to ``<dorq_shadow_dir>/statements/<as-of>.jsonl``;
    otherwise :func:`fafnir.dq.dorq.ingest` writes them, guarded as every check is.
    The run's row is committed before dorq starts and its outcome when it ends,
    failure included, and only a success moves the watermark.
    """
    endpoint = endpoint_for(shadow)
    if show_info is None:
        show_info = settings.statements_info
    as_of = engine.market_latest(db, exchange_code)
    window = engine.Window(as_of=as_of)
    nothing = engine.DqRunOutcome(version="", window=window, detected={}, flagged={})
    if as_of is None:
        logger.info("dorq statements: no bars on an open session yet; nothing to check")
        return nothing
    if not statements_exist(db):
        logger.info("dorq statements: no statement tables; nothing to check")
        return nothing
    if not rerun:
        judged = engine.judged_night(db, endpoint, as_of)
        if judged is not None:
            logger.info(
                "dorq statements: %s was judged by run %s; no newer session to judge",
                as_of,
                judged["ingestion_run_id"],
            )
            return engine.DqRunOutcome(
                version="",
                window=window,
                detected={},
                flagged={},
                judged_by={
                    "ingestion_run_id": judged["ingestion_run_id"],
                    "started_at": judged["started_at"],
                },
            )
    run_id = engine.start_run(
        db, window, shadow, full=True, endpoint=endpoint, params={"kind": "statements"}
    )
    db.commit()
    try:
        with engine.work_dir(settings, WORK_PREFIX) as workdir:
            version, done = judge(db, settings, as_of, workdir, show_info=show_info)
            # dorq's rows stream from its output file to the shadow file or the
            # queue's COPY, counted on the way: the night is never held (#219).
            rows = engine.Tally(
                filed_under_statement(engine.read_records(done.out_path))
            )
            shadow_file = None
            flagged: dict[str, int] = {}
            withheld: Optional[int] = None
            rekeyed: Optional[int] = None
            if shadow:
                shadow_file = engine.write_shadow(
                    rows, engine.shadow_path(str(shadow_dir(settings)), as_of)
                )
            else:
                # A condition the queue accepted under an SQL check's name is
                # accepted for its successor too (compare_statements.
                # AcceptedPredecessors): dorq's own guard knows only dorq's names.
                from fafnir.dq.compare_statements import (
                    AcceptedPredecessors,
                    HeldUnderEarlierKeys,
                )

                guard = AcceptedPredecessors.load(db)
                # A condition the queue holds under the key an earlier dorq gave
                # it (dorq 0.18.0 re-keyed four checks): not written again.
                held = HeldUnderEarlierKeys.load(db)
                flagged = engine.ingest(
                    db, held.withhold(guard.withhold(rows)), ingestion_run_id=run_id
                )
                withheld = guard.withheld
                rekeyed = held.withheld
                logger.info(
                    "dorq statements: withheld %d reports an accepted SQL flag "
                    "carries, and %d the queue holds under an earlier dorq's key",
                    withheld,
                    rekeyed,
                )
    except BaseException as exc:
        # As the bars run does: a COPY cut short when dorq exits can leave the
        # transaction aborted, so discard it before recording the failure.
        db.rollback()
        engine.finish_run(
            db, run_id, status="failed", error=f"{type(exc).__name__}: {exc}"
        )
        db.commit()
        raise
    engine.finish_run(
        db,
        run_id,
        status="success",
        flagged=sum(flagged.values()),
        params={
            "dorq_version": version,
            "dorq_config_hash": rows.config_hash,
            "reports": rows.count,
            "show_info": show_info,
            **done.sent,
            **({"withheld": withheld} if withheld is not None else {}),
            **({"withheld_rekeyed": rekeyed} if rekeyed is not None else {}),
            **done.usage(),
        },
    )
    engine.write_watermark(db, endpoint, as_of)
    db.commit()
    return engine.DqRunOutcome(
        version=version,
        window=window,
        detected=rows.by_check(),
        flagged=flagged,
        shadow_file=shadow_file,
        usage=done.usage(),
    )


def last_run(db: Database) -> Optional[dict]:
    """The newest statements run, either endpoint: for `fafnir status`."""
    return db.fetchone(
        """
        SELECT ingestion_run_id, endpoint, status, started_at, finished_at,
               window_to AS as_of, rows_inserted, params
          FROM ops.ingestion_run
         WHERE source = %s AND endpoint = ANY(%s)
         ORDER BY ingestion_run_id DESC
         LIMIT 1
        """,
        (engine.WATERMARK_SOURCE, [ENDPOINT_QUEUE, ENDPOINT_SHADOW]),
    )

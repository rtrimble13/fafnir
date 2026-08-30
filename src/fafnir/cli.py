"""
``fafnir`` -- administration CLI for the warehouse: migrations, seeding, ingestion,
adjustment, data-quality, and status. Read access for analysis is via ``duk`` (db mode).

Examples
--------
    fafnir db migrate
    fafnir db seed
    fafnir ingest securities --limit 500
    fafnir ingest prices --symbols AAPL,MSFT --from 2023-01-01
    fafnir source probe-fund VFIAX && fafnir track add VFIAX --note "core sleeve"
    fafnir ingest tracked
    fafnir ingest actions --symbols AAPL && fafnir adjust
    fafnir source probe-actions && fafnir ingest actions --mode auto
    fafnir adjust --changed
    fafnir db refresh-marts
    fafnir dq run
    fafnir dq list --detail --check gap --symbol AAPL
    fafnir dq resolve 12841 --note "exchange holiday, no bar expected"
    fafnir status
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import click

from fafnir import __version__
from fafnir.config import get_config
from fafnir.db.connection import Database
from fafnir.logging_config import setup_logging


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def _os_user() -> str:
    """Best-effort identity for `resolved_by` when `--by` is not given.

    A wrong-but-plausible name is worse than none, so anything unresolvable
    becomes "unknown" rather than a guess.
    """
    import getpass

    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def _split_symbols(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [s.strip().upper() for s in value.split(",") if s.strip()]


@click.group()
@click.version_option(version=__version__, message="%(version)s")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to config file (default: ~/.fafnirrc)",
)
@click.pass_context
def main(ctx, config):
    """fafnir -- financial market data warehouse administration."""
    ctx.ensure_object(dict)
    cfg = get_config(config)
    ctx.obj["config"] = cfg
    ctx.obj["logger"] = setup_logging(log_level=cfg.log_level, log_dir=cfg.log_dir)


# ---------------------------------------------------------------------------
# db group
# ---------------------------------------------------------------------------
@main.group()
def db():
    """Schema migrations, seeding, partitions, mart refresh."""


@db.command("migrate")
@click.option("--target", help="Apply up to this migration version (e.g. 0005)")
@click.pass_context
def db_migrate(ctx, target):
    """Apply pending up-migrations."""
    from fafnir.db import migrate as m

    applied = m.migrate(ctx.obj["config"].dsn, target=target)
    if applied:
        click.echo(f"Applied: {', '.join(applied)}")
    else:
        click.echo("Up to date.")


@db.command("rollback")
@click.option("--steps", default=1, show_default=True, help="Migrations to roll back")
@click.pass_context
def db_rollback(ctx, steps):
    """Roll back the most recently applied migration(s)."""
    from fafnir.db import migrate as m

    rolled = m.rollback(ctx.obj["config"].dsn, steps=steps)
    click.echo(f"Rolled back: {', '.join(rolled) or '(none)'}")


@db.command("status")
@click.pass_context
def db_status(ctx):
    """Show migration status."""
    from fafnir.db import migrate as m

    for version, name, state in m.status(ctx.obj["config"].dsn):
        click.echo(f"  {version}  {state:8s}  {name}")


@db.command("seed")
@click.pass_context
def db_seed(ctx):
    """Apply SQL seeds and generate the US trading calendar."""
    from fafnir.db import seed as s

    cfg = ctx.obj["config"]
    with Database(cfg.dsn) as database:
        result = s.seed(database, cfg.calendar_start_year, cfg.calendar_end_year)
    click.echo(
        f"Seeds: {result['sql_seeds']}; calendar rows: {result['calendar_rows']}"
    )


@db.command("ensure-partitions")
@click.option("--start-year", type=int)
@click.option("--end-year", type=int)
@click.pass_context
def db_ensure_partitions(ctx, start_year, end_year):
    """Create yearly price partitions for a year range."""
    from fafnir.db import maintenance

    cfg = ctx.obj["config"]
    start_year = start_year or cfg.calendar_start_year
    end_year = end_year or cfg.calendar_end_year
    with Database(cfg.dsn) as database:
        created = maintenance.ensure_partitions(database, start_year, end_year)
    click.echo(f"Created {created} partition(s).")


@db.command("ensure-horizon")
@click.option(
    "--through-year",
    type=int,
    help="Target horizon year (default: max(current year + horizon_extra_years, "
    "calendar_end_year)).",
)
@click.option(
    "--floor-year",
    type=int,
    help="Earliest year to guarantee a partition for (default: calendar_start_year).",
)
@click.pass_context
def db_ensure_horizon(ctx, through_year, floor_year):
    """Roll partitions + trading calendar forward to a future horizon.

    Run nightly (it is in scripts/daily_update.sh) so the warehouse always stays
    a couple of years ahead with no config edits.
    """
    from fafnir.db import maintenance

    cfg = ctx.obj["config"]
    if through_year is None:
        through_year = max(
            maintenance.current_horizon_year(cfg.horizon_extra_years),
            cfg.calendar_end_year,
        )
    floor_year = floor_year or cfg.calendar_start_year
    with Database(cfg.dsn) as database:
        created, cal_rows = maintenance.ensure_horizon(
            database, through_year=through_year, floor_year=floor_year
        )
    click.echo(
        f"Horizon ensured through {through_year}: "
        f"{created} partition(s), {cal_rows} calendar rows."
    )


@db.command("refresh-marts")
@click.pass_context
def db_refresh_marts(ctx):
    """Refresh derived materialized views."""
    from fafnir.db import maintenance

    with Database(ctx.obj["config"].dsn) as database:
        maintenance.refresh_marts(database)
    click.echo("Marts refreshed.")


# ---------------------------------------------------------------------------
# ingest group
# ---------------------------------------------------------------------------
@main.group()
def ingest():
    """Load data from sources into the warehouse."""


def _fmp_client(cfg):
    from fafnir.sources.fmp import FMPClient

    key = cfg.fmp_key
    if not key:
        raise click.ClickException(
            "No FMP API key. Set FMP_API_KEY or [api].fmp_key in ~/.fafnirrc"
        )
    return FMPClient(key, rate_per_min=cfg.request_rate_per_min)


@ingest.command("securities")
@click.option("--universe", default=None, help="Universe id (default from config)")
@click.option("--no-etfs", is_flag=True, help="Exclude ETFs")
@click.option("--limit", type=int, help="Cap securities (useful for testing)")
@click.option(
    "--enrich/--no-enrich",
    default=False,
    help="Also fetch per-symbol profiles (sector/industry)",
)
@click.pass_context
def ingest_securities(ctx, universe, no_etfs, limit, enrich):
    """Load the security master from FMP bulk lists."""
    from fafnir.ingest import security_master

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    universe = universe or cfg.universe
    with Database(cfg.dsn) as database:
        result = security_master.load_securities(
            database, fmp, universe=universe, include_etfs=not no_etfs, limit=limit
        )
        if enrich:
            syms = [
                r["primary_symbol"]
                for r in database.fetchall(
                    "SELECT primary_symbol FROM core.security ORDER BY security_id"
                    + (f" LIMIT {int(limit)}" if limit else "")
                )
            ]
            security_master.enrich_profiles(database, fmp, syms)
    click.echo(
        f"Loaded {result.total} securities ({len(result.new_symbols)} new). "
        f"FMP requests: {fmp.request_count}, bytes: {fmp.bytes_downloaded}"
    )
    if result.new_symbols:
        shown = ", ".join(result.new_symbols[:25])
        more = (
            ""
            if len(result.new_symbols) <= 25
            else f", +{len(result.new_symbols) - 25} more"
        )
        click.echo(f"New to the universe: {shown}{more}")
        click.echo(
            "Their price history loads on the next `fafnir ingest prices` "
            "(no watermark yet -- the first pull is a full backfill)."
        )


@ingest.command("tracked")
@click.pass_context
def ingest_tracked(ctx):
    """Load the declared universe (ref.tracked_symbol) into the security master.

    The screener cannot reach a mutual fund -- it has no listing venue -- so this is
    how a declared symbol becomes a security_id. Run it AFTER `ingest securities`:
    both write on the same conflict key, and running second is what makes the
    declared asset_type and venue the ones that stand.
    """
    from fafnir.ingest import tracked

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    with Database(cfg.dsn) as database:
        result = tracked.load_tracked(database, fmp)
    click.echo(
        f"Loaded {result.total} declared securities ({len(result.minted)} newly "
        f"minted). FMP bytes: {fmp.bytes_downloaded}"
    )
    if result.minted:
        click.echo(f"New to the master: {', '.join(result.minted)}")
        click.echo(
            "Their price history loads on the next `fafnir ingest prices` "
            "(no watermark yet -- the first pull is a full backfill)."
        )
    for old, new in result.renamed:
        click.echo(f"Followed a rename: {old} -> {new} (declaration updated).")
    if result.missing:
        click.echo(
            f"Unknown to FMP: {', '.join(result.missing)} -- check the ticker; "
            "flagged in the DQ queue as tracked_symbol_unknown_to_source."
        )


@ingest.command("prices")
@click.option("--symbols", help="Comma-separated symbols (default: all securities)")
@click.option("--from", "from_date", help="Start date YYYY-MM-DD (else incremental)")
@click.option("--to", "to_date", help="End date YYYY-MM-DD")
@click.option(
    "--include-inactive",
    is_flag=True,
    help="Also load delisted securities (use for backfills -- omitting it is "
    "what makes a history survivorship-biased)",
)
@click.pass_context
def ingest_prices(ctx, symbols, from_date, to_date, include_inactive):
    """Load daily OHLCV (incremental by default)."""
    from fafnir.ingest import daily_price

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    with Database(cfg.dsn) as database:
        # The nightly run wants active names only -- a delisted security will
        # never have another bar, so re-polling it burns requests forever. A
        # historical backfill wants everything, or the resulting history only
        # contains companies that happened to survive to today.
        where = "" if include_inactive else "WHERE is_actively_trading "
        syms = _split_symbols(symbols) or [
            r["primary_symbol"]
            for r in database.fetchall(
                f"SELECT primary_symbol FROM core.security {where}"
                "ORDER BY security_id"
            )
        ]
        n = daily_price.load_prices(
            database,
            fmp,
            syms,
            start_date=_parse_date(from_date),
            end_date=_parse_date(to_date),
            overlap_days=cfg.overlap_days,
        )
    click.echo(
        f"Loaded {n} price rows for {len(syms)} symbols. "
        f"FMP bytes: {fmp.bytes_downloaded}"
    )


@ingest.command("symbol-changes")
@click.option(
    "--full",
    is_flag=True,
    help="Sweep the entire rename feed instead of only its recent tail",
)
@click.pass_context
def ingest_symbol_changes(ctx, full):
    """Apply ticker renames to the securities that already hold the history.

    Run this BEFORE `ingest securities`: to the screener a renamed ticker looks
    like a new listing, and minting it as one forks the company's identity.
    """
    from fafnir.ingest import symbol_changes

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    with Database(cfg.dsn) as database:
        counts = symbol_changes.load_symbol_changes(
            database, fmp, max_pages=500 if full else 5
        )
    click.echo(
        f"Applied {counts['applied']} renames "
        f"({counts['folded']} duplicate stubs folded), "
        f"{counts['conflict']} conflicts, {counts['skipped']} already applied, "
        f"{counts['unknown']} not in the master. FMP bytes: {fmp.bytes_downloaded}"
    )
    if counts["conflict"]:
        click.echo(
            "Conflicts need a human: the new ticker already belongs to another "
            "listed security with its own history. See `fafnir status`."
        )


@ingest.command("delisted")
@click.option(
    "--full",
    is_flag=True,
    help="Sweep the entire delisted feed instead of only its recent tail",
)
@click.pass_context
def ingest_delisted(ctx, full):
    """Mark securities that have stopped trading (survivorship-bias guard)."""
    from fafnir.ingest import delisted

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    with Database(cfg.dsn) as database:
        marked, seen = delisted.load_delisted(
            database, fmp, max_pages=500 if full else 5
        )
    click.echo(
        f"Marked {marked} newly delisted securities ({seen} feed rows on our venues). "
        f"FMP bytes: {fmp.bytes_downloaded}"
    )


@ingest.command("actions")
@click.option("--symbols", help="Comma-separated symbols (always pulled per symbol)")
@click.option(
    "--mode",
    type=click.Choice(["symbol", "calendar", "auto"]),
    default=None,
    help="How to fetch [default: general.actions_mode, initially 'symbol']. "
    "symbol = full history per security every run; calendar = market-wide "
    "splits-calendar/dividends-calendar sweep against a watermark; auto = "
    "calendar, plus per-symbol for what it cannot cover (funds).",
)
@click.option(
    "--include-inactive",
    is_flag=True,
    help="Also pull delisted securities. A delisted security can never have "
    "another corporate action, so the nightly job leaves them out.",
)
@click.option(
    "--reconcile-buckets",
    type=int,
    default=None,
    help="Reconcile 1/N of the universe per run against the per-symbol feed "
    "(0 disables) [default: general.actions_reconcile_buckets].",
)
@click.option("--as-of", help="Treat this date as today (YYYY-MM-DD). Testing/replay.")
@click.pass_context
def ingest_actions(ctx, symbols, mode, include_inactive, reconcile_buckets, as_of):
    """Load corporate actions (splits + dividends).

    Incremental by default once `general.actions_mode` is switched to `auto`: one
    market-wide calendar sweep against a watermark instead of a full history pull
    per security. See doc/adr/0007-incremental-corporate-actions.md, and run
    `fafnir source probe-actions` before switching.
    """
    from fafnir.ingest import corporate_actions

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    mode = mode or cfg.actions_mode
    buckets = (
        cfg.actions_reconcile_buckets
        if reconcile_buckets is None
        else reconcile_buckets
    )
    with Database(cfg.dsn) as database:
        result = corporate_actions.run_actions(
            database,
            fmp,
            mode=mode,
            symbols=_split_symbols(symbols),
            as_of=_parse_date(as_of),
            overlap_days=cfg.actions_overlap_days,
            reconcile_buckets=buckets,
            include_inactive=include_inactive,
        )
    click.echo(
        f"Loaded {result.upserted} corporate actions "
        f"({result.changed} new or changed, across "
        f"{len(result.changed_security_ids)} securities). "
        f"FMP requests: {fmp.request_count}, bytes: {fmp.bytes_downloaded}"
    )
    if result.mode != "symbol":
        click.echo(
            f"Calendar sweep: {result.calendar_rows} rows "
            f"({result.unresolved_rows} for symbols outside this universe). "
            f"Per-symbol pulls: {result.symbols_pulled} "
            f"({result.first_loaded} with no prior history)."
        )
    if result.future_skipped:
        click.echo(
            f"{result.future_skipped} declared action(s) had not gone ex yet and "
            "were not stored -- they load on their ex-date."
        )
    if result.reconciled:
        click.echo(
            f"Reconciled {result.reconciled} securities against the per-symbol "
            f"feed; {result.drift} disagreed."
        )
    if result.drift:
        click.echo(
            "Drift means the calendar sweep missed something. The rows were "
            "repaired; the securities are flagged as 'corporate_action_drift' in "
            "ops.data_quality_flag. See `fafnir dq list --check "
            "corporate_action_drift --detail`."
        )
    if result.changed_security_ids:
        click.echo("Recompute their factors with `fafnir adjust --changed`.")


@main.group()
def track():
    """Declare the symbols the security master must hold regardless of the screener.

    The nightly universe comes from FMP's company screener, which only returns
    exchange-listed securities. An open-end mutual fund has no venue and never
    appears there, so it has to be declared. See ADR 0006.
    """


@track.command("add")
@click.argument("symbol")
@click.option(
    "--asset-type",
    type=click.Choice(["fund", "etf", "equity", "other"]),
    default="fund",
    show_default=True,
    help="What this symbol is. Authoritative over the vendor profile.",
)
@click.option(
    "--exchange",
    default=None,
    help="Venue to record [default: MUTF for a fund, none otherwise]",
)
@click.option("--note", default=None, help="Why this symbol is tracked")
@click.pass_context
def track_add(ctx, symbol, asset_type, exchange, note):
    """Declare SYMBOL. Loads on the next `fafnir ingest tracked`."""
    from fafnir.db import repository as repo
    from fafnir.ingest.tracked import FUND_EXCHANGE

    symbol = symbol.strip().upper()
    if exchange is None and asset_type == "fund":
        exchange = FUND_EXCHANGE
    with Database(ctx.obj["config"].dsn) as database:
        added = repo.upsert_tracked_symbol(
            database,
            symbol=symbol,
            asset_type=asset_type,
            exchange_code=exchange.upper() if exchange else None,
            note=note,
        )
        database.commit()
    click.echo(
        f"{'Declared' if added else 'Re-declared'} {symbol} as {asset_type}"
        + (f" on {exchange}" if exchange else "")
        + "."
    )
    click.echo(
        "Run `fafnir ingest tracked` to mint it, then `fafnir ingest prices "
        f"--symbols {symbol}` for its history (or let the nightly job do both)."
    )


@track.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include untracked symbols")
@click.pass_context
def track_list(ctx, show_all):
    """Show the declared universe and whether each symbol has been loaded."""
    from fafnir.db import repository as repo

    with Database(ctx.obj["config"].dsn) as database:
        rows = repo.list_tracked_symbols(database, tracked_only=not show_all)
    if not rows:
        click.echo(
            'Nothing declared. `fafnir track add VFIAX --note "..."` adds a symbol '
            "the screener cannot reach."
        )
        return
    table = [
        (
            row["symbol"],
            row["asset_type"],
            row["exchange_code"] or "-",
            "yes" if row["security_id"] else "NOT LOADED",
            "" if row["is_tracked"] else "untracked",
            (row["note"] or "")[:40],
        )
        for row in rows
    ]
    _echo_table(
        ("symbol", "type", "venue", "loaded", "state", "note"),
        table,
    )
    if any(not row["security_id"] for row in rows):
        click.echo(
            "\nSome declarations have no security yet -- run `fafnir ingest tracked`."
        )


@track.command("rm")
@click.argument("symbol")
@click.option(
    "--closed",
    "closed_on",
    default=None,
    help="The date the fund actually closed or merged, YYYY-MM-DD. Retires the "
    "security as well as the declaration.",
)
@click.pass_context
def track_rm(ctx, symbol, closed_on):
    """Stop declaring SYMBOL. History is always retained.

    Two different things are called "removing a fund", and they need saying apart:

    \b
      no --closed : stop loading it. The security stays active, so the freshness
                    check will flag it stale every night from here on.
      --closed D  : it stopped existing on D. Retires the security the ordinary
                    way -- delisted_date stamped, ticker period closed, every bar
                    kept -- so nothing goes on expecting a price.
    """
    from fafnir.db import repository as repo

    symbol = symbol.strip().upper()
    closed = _parse_date(closed_on)
    with Database(ctx.obj["config"].dsn) as database:
        untracked = repo.untrack_symbol(database, symbol=symbol)
        sec_id = repo.active_security_for_symbol(database, symbol)
        retired = False
        if closed is not None:
            if sec_id is None:
                raise click.ClickException(
                    f"{symbol} has no listed security to retire. Drop --closed to "
                    "just stop tracking it."
                )
            retired = repo.mark_delisted(
                database, security_id=sec_id, delisted_date=closed
            )
        database.commit()
    if not untracked:
        click.echo(f"{symbol} was not being tracked.")
    elif sec_id is None:
        # Declared but never minted: there is no security and so no history to
        # reassure anyone about, and nothing for the freshness check to flag.
        click.echo(f"Stopped tracking {symbol}. It had not been loaded yet.")
    else:
        click.echo(f"Stopped tracking {symbol}. Its history is retained.")
    if closed is not None:
        click.echo(
            f"Retired {symbol} as of {closed}."
            if retired
            else f"{symbol} was already retired; delisted_date left as it was."
        )
    elif untracked and sec_id is not None:
        click.echo(
            "The security is still marked actively trading, so `fafnir dq run` will "
            "flag it stale. Re-run with --closed <date> if it actually stopped "
            "existing."
        )


@main.command("adjust")
@click.option("--symbol", help="Recompute one symbol only (default: all with actions)")
@click.option(
    "--changed",
    is_flag=True,
    help="Recompute only the securities the last corporate-actions run changed. "
    "This is the nightly path: a few hundred securities instead of every one "
    "that has ever had an action.",
)
@click.pass_context
def adjust(ctx, symbol, changed):
    """Recompute adjustment factors from corporate actions."""
    from fafnir.db import repository as repo
    from fafnir.ingest import adjustments
    from fafnir.ingest.corporate_actions import ENDPOINT as ACTIONS_ENDPOINT

    cfg = ctx.obj["config"]
    if symbol and changed:
        raise click.ClickException("--symbol and --changed are mutually exclusive.")
    with Database(cfg.dsn) as database:
        sec_id = None
        sec_ids = None
        if symbol:
            sec_id = repo.resolve_security_id(database, symbol.upper())
            if sec_id is None:
                # Without this, an unresolved symbol falls through as "all" and a
                # mistyped ticker silently recomputes the entire universe.
                raise click.ClickException(
                    f"Unknown symbol {symbol.upper()}: not in the security master."
                )
        elif changed:
            run_id = repo.latest_run_id(database, "fmp", ACTIONS_ENDPOINT)
            if run_id is None:
                # Falling through to "all" here would turn a cheap nightly step into
                # a full recompute the first time the order of the job changed.
                raise click.ClickException(
                    "No corporate-actions run has been recorded, so there is no "
                    "changed set to recompute. Run `fafnir ingest actions` first, "
                    "or use `fafnir adjust` with no flags for a full recompute."
                )
            sec_ids = repo.securities_changed_by_run(database, run_id)
            if not sec_ids:
                click.echo(
                    f"No corporate actions changed in run {run_id}; "
                    "no factors to recompute."
                )
                return
        result = adjustments.adjust_all(
            database, security_id=sec_id, security_ids=sec_ids
        )
    click.echo(f"Recomputed adjustment factors for {result['securities']} securities.")
    if not result["failed"]:
        return

    # A stepped-over security keeps the factors from its last successful run (none,
    # on a first backfill), so its newest actions are missing from the series.
    click.echo(
        f"{result['failed']} securities failed and kept the factors from their last "
        "successful run -- none on a first backfill, stale otherwise, so their "
        "newest corporate actions are not reflected. Flagged as 'adjustment_failed' "
        "in ops.data_quality_flag; see `fafnir status`."
    )
    if result["aborted"]:
        raise click.ClickException(
            f"Stopped after the first {result['failed']} securities all failed without "
            "a single success. Check that migrations are applied "
            "(`fafnir db status`) and read the flags before re-running."
        )
    attempted = result["securities"] + result["failed"]
    systemic = (
        result["failed"] >= adjustments.SYSTEMIC_FAILURE_FLOOR
        and result["failed"] > adjustments.SYSTEMIC_FAILURE_RATIO * attempted
    )
    if systemic:
        # Not bad data at this scale -- the schema, the grants or a lock. Exiting 0
        # here would let a backfill under `set -e` sail on to refresh a mart built on
        # nothing, and a nightly cron report success while writing no factors at all.
        #
        # Both conditions, not either: the ratio is what catches it on the full
        # universe, the floor is what stops one bad security being called systemic on
        # a small or `--limit`-built one (see SYSTEMIC_FAILURE_FLOOR).
        raise click.ClickException(
            f"{result['failed']} of {attempted} securities failed "
            f"(>= {adjustments.SYSTEMIC_FAILURE_FLOOR} and "
            f"> {adjustments.SYSTEMIC_FAILURE_RATIO:.0%}); that is systemic, not bad "
            "data. Check that migrations are applied (`fafnir db status`) and read "
            "the flags before re-running."
        )
    # Otherwise a handful of bad securities: flagged, stepped over, exit 0 so a
    # backfill still reaches its mart refresh and DQ pass.


# ---------------------------------------------------------------------------
# security group -- repairs the loader deliberately will not make
# ---------------------------------------------------------------------------
#
# `ingest symbol-changes` applies every rename it safely can and records the rest
# as `conflict`, which it retries nightly. Two kinds of conflict never clear on
# their own, and both need a person:
#
#   merge-rename   -- the rename is real, but a security-master load minted the new
#                     ticker as a second security and then filled it with bars. Two
#                     rows, one company. Merging price histories is not a decision a
#                     loader should make silently, so it is made here.
#   dismiss-rename -- the rename is not real. The feed reported a pre-launch ticker
#                     shuffle, or emitted the same change in both directions. There
#                     is nothing to apply and nothing to merge.
#
# Both end with the conflict reaching a terminal status, which is what actually gets
# it out of `fafnir status` and the DQ queue. Resolving the flag alone would not:
# the sweep re-detects the condition and re-flags it the same night.


def _rename_change_date(database, old_symbol, new_symbol, override):
    """The rename's effective date: the operator's, else the recorded conflict's.

    Defaulting from core.symbol_change matters -- the date is what retarget_symbol
    closes the old ticker's xref period on, and an operator retyping it from a
    terminal is one transposed digit away from a period boundary that silently
    disagrees with the audit row it came from.
    """
    from fafnir.db import repository as repo

    if override is not None:
        return override.date() if hasattr(override, "date") else override
    row = database.fetchone(
        """
        SELECT change_date FROM core.symbol_change
         WHERE old_symbol = %s AND new_symbol = %s AND status = %s
         ORDER BY change_date DESC LIMIT 1
        """,
        (old_symbol, new_symbol, repo.CHANGE_CONFLICT),
    )
    if row is None:
        raise click.ClickException(
            f"No recorded conflict for {old_symbol} -> {new_symbol}, so there is no "
            "effective date to apply. Pass --change-date if you are repairing a "
            "rename the sweep never recorded."
        )
    return row["change_date"]


def _echo_merge_plan(plan) -> None:
    """The evidence for a merge, in the order it should be read."""
    click.echo(
        f"Survivor {plan.survivor_id} ({plan.survivor_symbol}): "
        f"{plan.survivor_bars} bars"
    )
    click.echo(
        f"Victim   {plan.victim_id} ({plan.victim_symbol}): "
        f"{plan.victim_bars} bars, {plan.victim_actions} actions, "
        f"{plan.victim_flags} flags"
    )
    click.echo(
        f"Overlap  : {plan.shared_days} shared sessions, "
        f"{plan.disagreeing_days} disagreeing on OHLC"
    )
    click.echo(
        f"Would move {plan.victim_only_bars} bars the survivor does not have, "
        f"drop {plan.shared_days} duplicated sessions, "
        f"move {plan.victim_actions - plan.colliding_actions} actions "
        f"({plan.colliding_actions} already held), and delete security "
        f"{plan.victim_id}."
    )
    if plan.volume_only_disagreements:
        # Reported, never blocking: a restated volume across a rename is common and
        # costs no price accuracy. Silence here would be worse than a line of noise.
        click.echo(
            f"Note: {plan.volume_only_disagreements} shared sessions agree on OHLC "
            "but differ on volume; the survivor's volume is kept."
        )
    for line in plan.blockers:
        click.echo(f"BLOCKER: {line}", err=True)
    for row in plan.disagreement_sample:
        click.echo(
            f"  {row['trade_date']}  close {row['survivor_close']} vs "
            f"{row['victim_close']}",
            err=True,
        )


@main.group()
def security():
    """Security-master repairs: merges and rename decisions a loader will not make."""


@security.command("merge-rename")
@click.argument("old_symbol")
@click.argument("new_symbol")
@click.option(
    "--change-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    metavar="YYYY-MM-DD",
    help="Effective date of the rename  [default: from the recorded conflict]",
)
@click.option("--note", "-m", help="Why, kept on the resolved DQ flag.")
@click.option(
    "--by", "resolved_by", help="Who  [default: the OS user running the command]"
)
@click.option(
    "--dry-run", is_flag=True, help="Show the plan and the guards, change nothing."
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.option(
    "--force",
    is_flag=True,
    help="Merge despite a failed guard. Read the blockers first.",
)
@click.pass_context
def security_merge_rename(
    ctx, old_symbol, new_symbol, change_date, note, resolved_by, dry_run, yes, force
):
    """Apply a blocked rename by merging the duplicate into the original.

    \b
      fafnir security merge-rename GREE VIP --dry-run
      fafnir security merge-rename GREE VIP -m "backfill minted VIP as a duplicate"

    For the conflict where OLD and NEW are the same instrument held as two
    security_ids -- the shape a security-master load leaves when it runs before the
    rename sweep. The row holding OLD survives and keeps its id; the row holding NEW
    is absorbed and deleted.

    Refuses unless the vendor's own identifiers agree (CUSIP/ISIN/CIK, where both
    sides have them) and the overlapping sessions agree on OHLC. Run it with
    --dry-run first: the preview is the same comparison the guard runs.
    """
    from fafnir.db import repository as repo
    from fafnir.ingest import adjustments

    old_symbol, new_symbol = old_symbol.strip().upper(), new_symbol.strip().upper()
    if old_symbol == new_symbol:
        raise click.ClickException("OLD and NEW are the same ticker.")
    if resolved_by is None:
        resolved_by = _os_user()

    with Database(ctx.obj["config"].dsn) as database:
        survivor_id = repo.active_security_for_symbol(database, old_symbol)
        if survivor_id is None:
            raise click.ClickException(
                f"{old_symbol} is not a listed security. A merge moves history onto "
                "the row that already holds it; there is nothing here to merge into."
            )
        victim_id = repo.active_security_for_symbol(database, new_symbol)
        if victim_id is None:
            # No duplicate means no conflict: the ticker is free and the ordinary
            # sweep can carry the rename. Sending the operator there is better than
            # inventing a merge with one side.
            raise click.ClickException(
                f"{new_symbol} is not held by any listed security, so there is no "
                "duplicate to merge. Run `fafnir ingest symbol-changes` -- this "
                "rename can apply on its own."
            )
        if victim_id == survivor_id:
            raise click.ClickException(
                f"{old_symbol} and {new_symbol} already resolve to security "
                f"{survivor_id}; the rename is applied. Run "
                "`fafnir ingest symbol-changes` to record it as terminal."
            )

        effective = _rename_change_date(database, old_symbol, new_symbol, change_date)
        plan = repo.compare_securities(
            database, survivor_id=survivor_id, victim_id=victim_id
        )
        click.echo(f"{old_symbol} -> {new_symbol}, effective {effective}")
        _echo_merge_plan(plan)

        if dry_run:
            click.echo("Dry run: nothing changed.")
            return
        if plan.blockers and not force:
            raise click.ClickException(
                "Refusing to merge -- see the blockers above. If you have read them "
                "and this is still one instrument, re-run with --force."
            )
        if not yes:
            click.confirm(
                f"Merge security {victim_id} ({new_symbol}) into {survivor_id} "
                f"({old_symbol}) and delete {victim_id}?",
                abort=True,
            )

        report = repo.merge_security(
            database, victim_id=victim_id, survivor_id=survivor_id, force=force
        )
        # Only now does the ticker move: the merge is about identity, the retarget
        # is about the rename, and they carry different dates.
        repo.retarget_symbol(
            database,
            security_id=survivor_id,
            old_symbol=old_symbol,
            new_symbol=new_symbol,
            change_date=effective,
        )
        repo.record_symbol_change(
            database,
            old_symbol=old_symbol,
            new_symbol=new_symbol,
            change_date=effective,
            status=repo.CHANGE_APPLIED,
            security_id=survivor_id,
            detail={
                "old_symbol": old_symbol,
                "new_symbol": new_symbol,
                "merged_security_id": victim_id,
                "merged_by": resolved_by,
            },
        )
        # The survivor's corporate actions just changed, so its factors are stale by
        # construction. Recomputing here rather than telling the operator to is the
        # difference between a repair that finishes and one that leaves a wrong
        # adjusted series behind whenever the follow-up step is forgotten.
        adjustments.compute_for_security(database, survivor_id)

        flag_ids = repo.open_dq_flag_ids_for_record(
            database,
            check_name="symbol_change_conflict",
            record_key={"old_symbol": old_symbol, "new_symbol": new_symbol},
        )
        closed = []
        if flag_ids:
            closed = repo.resolve_dq_flags(
                database,
                repo.DqFilter(state="open", flag_ids=tuple(flag_ids)),
                note=note or f"merged duplicate {victim_id} into {survivor_id}",
                resolved_by=resolved_by,
            )
        database.commit()

    click.echo(
        f"Merged {report.bars_moved} bars "
        f"({report.bars_dropped} duplicates dropped), "
        f"{report.actions_moved} actions "
        f"({report.actions_dropped} duplicates dropped), "
        f"{report.flags_moved} flags "
        f"({report.flags_dropped} duplicates dropped)."
    )
    click.echo(
        f"Security {survivor_id} is now {new_symbol}; {victim_id} is gone. "
        "Adjustment factors recomputed."
    )
    click.echo(f"Resolved {_plural(len(closed), 'DQ flag')} as {resolved_by}.")
    click.echo("Run `fafnir db refresh-marts` to pick this up in the marts.")


@security.command("dismiss-rename")
@click.argument("old_symbol")
@click.argument("new_symbol")
@click.option(
    "--change-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    metavar="YYYY-MM-DD",
    help="Dismiss only the row with this date  [default: every unapplied row for "
    "the pair]",
)
@click.option(
    "--note",
    "-m",
    required=True,
    help="Why this is not a real rename. Required -- a dismissal without a reason "
    "is the one thing nobody can audit later.",
)
@click.option(
    "--by", "dismissed_by", help="Who  [default: the OS user running the command]"
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.pass_context
def security_dismiss_rename(
    ctx, old_symbol, new_symbol, change_date, note, dismissed_by, yes
):
    """Record that a reported rename is not a rename, so the sweep stops retrying.

    \b
      fafnir security dismiss-rename QAU SCAU \\
          -m "pre-launch ticker shuffle; both listed 2026-08-04 and still trading"
      fafnir security dismiss-rename VBX USSX -m "feed emitted the change both ways"

    For the conflict that can never clear itself: both tickers belong to live
    securities that each keep trading, so no ordering of the sweep will ever free
    the target. Without this the rename re-conflicts every night in an
    error-severity queue.

    This changes no price data and merges nothing. If the rename is real but blocked
    by a duplicate, use `fafnir security merge-rename` instead -- reaching `applied`
    is a different fact from deciding the feed was wrong.
    """
    from fafnir.db import repository as repo

    old_symbol, new_symbol = old_symbol.strip().upper(), new_symbol.strip().upper()
    if dismissed_by is None:
        dismissed_by = _os_user()
    effective = None
    if change_date is not None:
        effective = change_date.date() if hasattr(change_date, "date") else change_date

    with Database(ctx.obj["config"].dsn) as database:
        if not yes:
            scope = f"dated {effective}" if effective else "every unapplied row"
            click.confirm(
                f"Dismiss the reported rename {old_symbol} -> {new_symbol} "
                f"({scope}) as not a real rename?",
                abort=True,
            )
        rows = repo.dismiss_symbol_change(
            database,
            old_symbol=old_symbol,
            new_symbol=new_symbol,
            change_date=effective,
            note=note,
            dismissed_by=dismissed_by,
        )
        if not rows:
            # Not an error: re-running a dismissal, or dismissing something a
            # colleague already settled, is a no-op. Say which, because "nothing
            # happened" reads as success and a typo looks identical.
            existing = database.fetchall(
                """
                SELECT change_date, status FROM core.symbol_change
                 WHERE old_symbol = %s AND new_symbol = %s
                 ORDER BY change_date DESC
                """,
                (old_symbol, new_symbol),
            )
            if not existing:
                raise click.ClickException(
                    f"No recorded rename {old_symbol} -> {new_symbol}. Check the "
                    "spelling against `fafnir dq list --detail "
                    "--check symbol_change_conflict`."
                )
            states = ", ".join(f"{r['change_date']}={r['status']}" for r in existing)
            click.echo(
                f"Nothing to dismiss: {old_symbol} -> {new_symbol} is already "
                f"terminal ({states})."
            )
            return

        flag_ids = repo.open_dq_flag_ids_for_record(
            database,
            check_name="symbol_change_conflict",
            record_key={"old_symbol": old_symbol, "new_symbol": new_symbol},
        )
        closed = []
        if flag_ids:
            closed = repo.resolve_dq_flags(
                database,
                repo.DqFilter(state="open", flag_ids=tuple(flag_ids)),
                note=note,
                resolved_by=dismissed_by,
            )
        database.commit()

    click.echo(
        f"Dismissed {_plural(len(rows), 'rename')} "
        f"({old_symbol} -> {new_symbol}) as {dismissed_by}."
    )
    click.echo(f"Resolved {_plural(len(closed), 'DQ flag')}.")
    click.echo("The nightly sweep will not retry this rename again.")


# ---------------------------------------------------------------------------
# dq + status
# ---------------------------------------------------------------------------
@main.group()
def source():
    """Inspect the upstream data source."""


@source.command("probe-prices")
@click.option("--symbol", default=None, help="Symbol to probe [default: AAPL]")
@click.option(
    "--date",
    "on_date",
    default=None,
    help="Date to compare, YYYY-MM-DD. Must sit behind a known split to be "
    "conclusive [default: 1990-01-02]",
)
@click.pass_context
def source_probe_prices(ctx, symbol, on_date):
    """Confirm FMP's price feed is unadjusted, and show its OHLC field names.

    Compares the unadjusted and split-adjusted endpoints for one old bar: they
    should differ by exactly the cumulative split ratio since that date. Costs 3
    requests and writes nothing. Run it before a backfill, and after any change to
    the price loader.
    """
    from fafnir.sources import probe

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    kwargs = {}
    if symbol:
        kwargs["symbol"] = symbol.upper()
    if on_date:
        kwargs["on_date"] = _parse_date(on_date)
    report = probe.probe_prices(fmp, **kwargs)
    click.echo(probe.format_report(report))
    # `volume_ambiguous` is not a failure: it means the two feeds cannot settle the
    # question, which is a prompt to check one date against an outside source, not a
    # reason to block the run.
    passing = ("unadjusted_confirmed", "inconclusive")
    vol_passing = ("volume_raw_confirmed", "inconclusive", "volume_ambiguous")
    if report["verdict"] not in passing or report["volume_verdict"] not in vol_passing:
        raise click.ClickException(
            "Price feed check FAILED -- do not backfill until this is resolved."
        )


@source.command("probe-fund")
@click.argument("symbol")
@click.option(
    "--window",
    "window_days",
    default=10,
    show_default=True,
    type=int,
    help="Days either side of the ex-date to look for a bar",
)
@click.pass_context
def source_probe_fund(ctx, symbol, window_days):
    """Confirm a fund's NAV series is RAW before declaring funds.

    `probe-prices` settles the same question for equities using splits. Funds rarely
    split, so this asks it of distributions instead: a raw NAV drops by the
    distributed amount on the ex-date, an already-reinvested one does not. If it
    does not drop, loading fund distributions into core.corporate_action would
    adjust every one of them twice.

    Costs 3 requests and writes nothing. Run it before `fafnir track add`.
    """
    from fafnir.sources import probe

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    report = probe.probe_fund_nav(fmp, symbol.upper(), window_days=window_days)
    click.echo(probe.format_fund_report(report))
    if report["verdict"] not in ("nav_raw_confirmed", "inconclusive"):
        raise click.ClickException(
            "Fund NAV check FAILED -- do not declare funds until this is resolved. "
            "See doc/adr/0006-curated-fund-universe.md."
        )


@source.command("probe-actions")
@click.option(
    "--symbols",
    default="AAPL,MSFT,KO,SPY",
    show_default=True,
    help="Comma-separated symbols to compare. Include a reliable dividend payer.",
)
@click.option(
    "--days",
    default=90,
    show_default=True,
    type=int,
    help="Window to compare, ending today",
)
@click.pass_context
def source_probe_actions(ctx, symbols, days):
    """Confirm the market-wide action calendars match the per-symbol feeds.

    Gate for `actions_mode = "auto"`. The calendar sweep replaces two requests per
    security with two requests for the whole market, which is only sound if the
    calendar carries the same events -- and if it does not, the failure is silent: a
    missing dividend is not an error, it is an adjusted price series that is quietly
    wrong. This pulls a sample both ways and diffs them.

    Costs 2 + 2N requests and writes nothing. Probe a fund separately if you hold
    any; a fund has no listing venue and the calendar is not assumed to reach it.
    """
    from fafnir.sources import probe

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    syms = _split_symbols(symbols) or []
    if not syms:
        raise click.ClickException("--symbols must name at least one symbol.")
    report = probe.probe_actions(fmp, syms, days=days)
    click.echo(probe.format_actions_report(report))
    click.echo(f"FMP requests: {fmp.request_count}, bytes: {fmp.bytes_downloaded}")
    if report["verdict"] not in ("calendar_complete", "no_events"):
        raise click.ClickException(
            'Calendar coverage check FAILED -- keep `actions_mode = "symbol"`. '
            "See doc/adr/0007-incremental-corporate-actions.md."
        )


@main.group()
def dq():
    """Data-quality checks."""


@dq.command("run")
@click.option("--exchange", default="NASDAQ", show_default=True)
@click.option("--outlier-threshold", default=0.5, show_default=True, type=float)
@click.pass_context
def dq_run(ctx, exchange, outlier_threshold):
    """Run gap / outlier / freshness checks; write flags to ops.data_quality_flag."""
    from fafnir.dq import checks

    with Database(ctx.obj["config"].dsn) as database:
        result = checks.run_all(
            database, exchange_code=exchange, outlier_threshold=outlier_threshold
        )
    click.echo(f"DQ flags written: {result}")


# ---------------------------------------------------------------------------
# dq queue presentation
# ---------------------------------------------------------------------------
#
# `fafnir dq run` writes the queue; `list` / `resolve` / `reopen` are how it is
# worked. The rendering lives here rather than in repository.py so the SQL stays
# one place and the terminal formatting another.


def _terminal_width(default: int = 100) -> int:
    """Width to fit a table into. Falls back when there is no tty (cron, pipes)."""
    import shutil

    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except OSError:
        return default


def _fmt_stamp(value) -> str:
    """A timestamp as the day it fell on -- the queue is triaged by date, and the
    clock time costs six columns that the record_key needs more."""
    if value is None:
        return "-"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _fmt_scalar(value) -> str:
    """One JSONB leaf, short enough to sit in a table cell."""
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _fmt_json(value) -> str:
    """A record_key or detail object as `k=v k=v`.

    The JSONB columns hold a handful of small fields (`trade_date`, `move`,
    `prev_close`), so one flat line reads better than pretty-printed JSON and keeps
    a flag to one row of the table. `--json` is there for the whole object.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{k}={_fmt_scalar(v)}" for k, v in value.items())
    return _fmt_scalar(value)


def _echo_table(headers, rows, right=()):
    """Print a plain column-aligned table, trimmed to the terminal width.

    The last column is the one that gets cut: it is always the free-text one
    (`detail`, or the resolution note), and losing its tail costs less than
    wrapping every row of a 200-row page. A cut line ends in `...` so a truncated
    value is never mistaken for the whole of one -- `move=0.6` and a cut
    `move=0.62...` are different claims about the data.
    """
    if not rows:
        return
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))
    ]
    limit = _terminal_width()

    def line(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        text = "  ".join(out).rstrip()
        return text if len(text) <= limit else text[: limit - 3] + "..."

    click.echo(line(headers))
    click.echo(line(["-" * w for w in widths]))
    for row in rows:
        click.echo(line(row))


def _echo_wrapped(text: str) -> None:
    """A prose line (a caveat, a hint) folded to the terminal, not run off it."""
    import textwrap

    click.echo(textwrap.fill(text, width=min(_terminal_width(), 88)))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _dq_json(payload) -> None:
    import json

    click.echo(json.dumps(payload, indent=2, default=str))


def _dq_filter(
    database,
    *,
    state="open",
    checks=(),
    severities=(),
    symbol=None,
    security_id=None,
    since=None,
    until=None,
    flag_ids=(),
):
    """Turn the shared CLI options into one repository filter.

    `--symbol` is resolved here, and an unknown one is fatal. Falling through as
    "no security filter" would silently widen the command to the whole universe --
    a mistyped ticker listing 40,000 flags is noise, but a mistyped ticker on
    `dq resolve --yes` closes the entire queue.
    """
    from fafnir.db import repository as repo

    if symbol:
        resolved = repo.resolve_security_id(database, symbol.upper())
        if resolved is None:
            raise click.ClickException(
                f"Unknown symbol {symbol.upper()}: not in the security master."
            )
        if security_id is not None and security_id != resolved:
            raise click.ClickException(
                f"--symbol {symbol.upper()} is security_id {resolved}, "
                f"which contradicts --security-id {security_id}."
            )
        security_id = resolved
    return repo.DqFilter(
        state=state,
        checks=tuple(checks),
        severities=tuple(severities),
        security_id=security_id,
        since=since.date() if since else None,
        until=until.date() if until else None,
        flag_ids=tuple(flag_ids),
    )


def _dq_filter_options(func):
    """The selection options `dq list` and `dq resolve` share.

    Shared deliberately: the workflow is to narrow with `list` until the page is
    the problem you mean, then re-run the same options under `resolve`. Options
    that drifted between the two commands would make that habit dangerous.
    """
    for option in reversed(
        [
            click.option(
                "--check",
                "checks",
                multiple=True,
                metavar="NAME",
                help="Check name, repeatable. Trailing * globs (e.g. 'price_*').",
            ),
            click.option(
                "--severity",
                "severities",
                multiple=True,
                type=click.Choice(["info", "warn", "error"]),
                help="Severity, repeatable.",
            ),
            click.option("--symbol", help="Limit to one security, by ticker."),
            click.option(
                "--security-id", type=int, help="Limit to one security, by id."
            ),
            # click.DateTime rather than the module's _parse_date: a bad date is a
            # usage error and should read as one ("invalid value for '--since'"),
            # not as a strptime traceback out of the middle of a query.
            click.option(
                "--since",
                type=click.DateTime(formats=["%Y-%m-%d"]),
                metavar="YYYY-MM-DD",
                help="Detected on or after.",
            ),
            click.option(
                "--until",
                type=click.DateTime(formats=["%Y-%m-%d"]),
                metavar="YYYY-MM-DD",
                help="Detected on or before (inclusive).",
            ),
        ]
    ):
        func = option(func)
    return func


@dq.command("list")
@click.option(
    "--detail",
    "-d",
    is_flag=True,
    help="List individual flags instead of the per-check summary.",
)
@_dq_filter_options
@click.option(
    "--state",
    type=click.Choice(["open", "resolved", "all"]),
    default="open",
    show_default=True,
    help="Open queue, the resolved record, or both.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=5000),
    default=50,
    show_default=True,
    help="Rows in --detail.",
)
@click.option(
    "--offset",
    type=click.IntRange(min=0),
    default=0,
    help="Skip this many rows in --detail.",
)
@click.option(
    "--oldest",
    is_flag=True,
    help="Oldest first in --detail (work the backlog from its front).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit rows as JSON.")
@click.pass_context
def dq_list(
    ctx,
    detail,
    checks,
    severities,
    symbol,
    security_id,
    since,
    until,
    state,
    limit,
    offset,
    oldest,
    as_json,
):
    """Show the data-quality queue: a per-check summary, or the flags themselves.

    \b
      fafnir dq list                                  # what is open, by check
      fafnir dq list --detail --check gap --limit 20  # the gaps themselves
      fafnir dq list -d --symbol AAPL --state all     # one security's history
      fafnir dq list --check 'price_*' --since 2024-08-01

    The same selection options work on `fafnir dq resolve`, so a filter narrowed
    here can be handed straight to it.
    """
    from fafnir.db import repository as repo

    with Database(ctx.obj["config"].dsn) as database:
        filt = _dq_filter(
            database,
            state=state,
            checks=checks,
            severities=severities,
            symbol=symbol,
            security_id=security_id,
            since=since,
            until=until,
        )
        totals = repo.dq_flag_totals(database, filt)
        if detail:
            rows = repo.list_dq_flags(
                database, filt, limit=limit, offset=offset, newest_first=not oldest
            )
        else:
            rows = repo.summarize_dq_flags(database, filt)

    if as_json:
        _dq_json({"totals": totals, "rows": rows})
        return
    if detail:
        _echo_dq_detail(rows, totals, filt, limit=limit, offset=offset)
    else:
        _echo_dq_summary(rows, totals, filt)


def _echo_dq_empty(filt) -> None:
    """The empty queue reads differently depending on why it is empty."""
    scope = " match this filter" if filt.is_narrowed else ""
    click.echo(f"No {_dq_label(filt).lower()} DQ flags{scope}.")


def _dq_label(filt) -> str:
    return {"open": "Open", "resolved": "Resolved", "all": "All"}[filt.state]


def _echo_dq_summary(rows, totals, filt) -> None:
    flags = int(totals.get("flags") or 0)
    label = _dq_label(filt)
    if not flags:
        _echo_dq_empty(filt)
        return
    click.echo(
        f"{label} DQ flags: {flags} across {totals.get('securities') or 0} securities, "
        f"{totals.get('checks') or 0} checks"
    )
    click.echo(
        f"Detected     : {_fmt_stamp(totals.get('first_detected'))} .. "
        f"{_fmt_stamp(totals.get('last_detected'))}"
    )
    click.echo()
    _echo_table(
        ["CHECK", "SEV", "FLAGS", "SECURITIES", "FIRST SEEN", "LAST SEEN"],
        [
            [
                r["check_name"],
                r["severity"],
                str(r["flags"]),
                str(r["securities"]),
                _fmt_stamp(r["first_detected"]),
                _fmt_stamp(r["last_detected"]),
            ]
            for r in rows
        ],
        right={2, 3},
    )
    click.echo()
    if any(r["check_name"].startswith("price_") for r in rows):
        # Everywhere else one open flag is one problem; price_* is the documented
        # exception, and a reader comparing its count against the other checks
        # deserves to be told before drawing a conclusion from it.
        _echo_wrapped(
            "Note: price_* flags repeat per re-detection by design (the repeats "
            "bound the watermark hold on a persistently-bad bar), so their count "
            "is not a count of distinct problems."
        )
    click.echo(f"Detail: fafnir dq list --detail --check {rows[0]['check_name']}")


def _echo_dq_detail(rows, totals, filt, *, limit, offset, hints=True) -> None:
    """The per-flag page. ``hints`` is off where the next command is not `resolve`
    -- inside `dq resolve --dry-run`, telling the operator to run `dq resolve` is
    noise on top of the command they already ran."""
    flags = int(totals.get("flags") or 0)
    label = _dq_label(filt)
    if not rows:
        if flags:
            click.echo(
                f"No rows at --offset {offset}; "
                f"{_plural(flags, 'flag')} match ({label.lower()})."
            )
        else:
            _echo_dq_empty(filt)
        return
    show_resolution = filt.state != "open"
    headers = ["ID", "DETECTED", "SEV", "CHECK", "SYMBOL", "RECORD KEY", "DETAIL"]
    if show_resolution:
        headers[6:6] = ["RESOLVED", "BY"]
    table = []
    for r in rows:
        cells = [
            str(r["dq_flag_id"]),
            _fmt_stamp(r["detected_at"]),
            r["severity"],
            r["check_name"],
            r["primary_symbol"]
            or (f"#{r['security_id']}" if r["security_id"] else "-"),
            _fmt_json(r["record_key"]),
        ]
        if show_resolution:
            cells += [_fmt_stamp(r["resolved_at"]), r["resolved_by"] or "-"]
        cells.append(_fmt_json(r["detail"]))
        table.append(cells)
    _echo_table(headers, table, right={0})
    click.echo()
    shown = f"{offset + 1}-{offset + len(rows)}" if offset else str(len(rows))
    click.echo(f"Showing {shown} of {_plural(flags, 'flag')} ({label.lower()}).")
    if show_resolution:
        # Grouped by note, not listed per row: one `dq resolve --check gap` writes
        # the same sentence to every flag it closed, and repeating it 50 times
        # buries the one flag somebody closed for a different reason.
        by_note: dict[str, list[str]] = {}
        for r in rows:
            if r.get("resolution_note"):
                by_note.setdefault(r["resolution_note"], []).append(
                    str(r["dq_flag_id"])
                )
        if by_note:
            click.echo()
            click.echo("Notes:")
            for text, ids in by_note.items():
                _echo_wrapped(f"  {', '.join(ids)}: {text}")
    if offset + len(rows) < flags:
        click.echo(f"More      : --offset {offset + limit}")
    if hints and filt.state == "open":
        click.echo(
            "Resolve   : fafnir dq resolve "
            + " ".join(str(r["dq_flag_id"]) for r in rows[:3])
            + ' --note "..."'
        )


@dq.command("resolve")
@click.argument("flag_ids", nargs=-1, type=int, metavar="[ID]...")
@_dq_filter_options
@click.option(
    "--note",
    "-m",
    help="Why these are being closed. Kept on the row -- write it for whoever "
    "reads the flag next.",
)
@click.option(
    "--by",
    "resolved_by",
    help="Who is closing them  [default: the OS user running the command]",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be closed and change nothing."
)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip the confirmation on a filtered resolve."
)
@click.pass_context
def dq_resolve(
    ctx,
    flag_ids,
    checks,
    severities,
    symbol,
    security_id,
    since,
    until,
    note,
    resolved_by,
    dry_run,
    yes,
):
    """Close data-quality flags, recording who closed them and why.

    \b
      fafnir dq resolve 12841 12842 --note "exchange holiday, no bar expected"
      fafnir dq resolve --check gap --symbol AAPL --note "backfilled" --yes
      fafnir dq resolve --check outlier --since 2024-08-01 --dry-run

    Takes explicit ids, or the same selection options as `fafnir dq list` -- run it
    as a list first and you can see exactly what will close. One or the other, not
    both, and never neither: an unfiltered resolve would close the whole queue.

    Resolving records a judgement, it does not fix the data. If the condition is
    still there the next `fafnir dq run` flags it again.
    """
    from fafnir.db import repository as repo

    narrowed = bool(
        checks or severities or symbol or since or until or security_id is not None
    )
    if flag_ids and narrowed:
        raise click.ClickException(
            "Give ids or filters, not both: the ids say exactly which rows, and a "
            "filter alongside them can only narrow that into something you did not "
            "list. Run `fafnir dq list` with the filters to get the ids."
        )
    if not flag_ids and not narrowed:
        raise click.ClickException(
            "Nothing selected. Pass flag ids, or narrow with --check / --symbol / "
            "--severity / --since / --until. Refusing to close the whole queue."
        )

    if resolved_by is None:
        resolved_by = _os_user()

    with Database(ctx.obj["config"].dsn) as database:
        filt = _dq_filter(
            database,
            state="open",
            checks=checks,
            severities=severities,
            symbol=symbol,
            security_id=security_id,
            since=since,
            until=until,
            flag_ids=flag_ids,
        )
        totals = repo.dq_flag_totals(database, filt)
        matched = int(totals.get("flags") or 0)

        if dry_run:
            preview = repo.list_dq_flags(database, filt, limit=20)
            _echo_dq_detail(preview, totals, filt, limit=20, offset=0, hints=False)
            click.echo(
                f"Dry run: {_plural(matched, 'flag')} would be closed. "
                "Nothing changed."
            )
            return

        if not matched:
            # Not an error: re-running a resolve, or closing something a colleague
            # already closed, is a no-op, and a nightly script piping into this
            # should not die on it.
            click.echo("Nothing to resolve: no open flags match.")
            _warn_unclosed(database, flag_ids, closed=set())
            return

        if not flag_ids and not yes:
            click.confirm(
                f"Close {_plural(matched, 'open flag')} "
                f"({totals.get('securities') or 0} securities, "
                f"{totals.get('checks') or 0} checks)?",
                abort=True,
            )

        closed = repo.resolve_dq_flags(
            database, filt, note=note, resolved_by=resolved_by
        )
        click.echo(f"Resolved {_plural(len(closed), 'flag')} as {resolved_by}.")
        if note:
            click.echo(f'Note: "{note}"')
        _warn_unclosed(database, flag_ids, closed=set(closed))
        remaining = repo.dq_flag_totals(database, repo.DqFilter())
        click.echo(f"Open DQ flags remaining: {remaining.get('flags') or 0}")


def _warn_unclosed(database, flag_ids, *, closed: set) -> None:
    """Say which requested ids did not close, and why.

    "Resolved 2 flags" after asking for 4 is the kind of quiet shortfall that gets
    read as success; the two that did not close are either a typo or a decision
    somebody else already made, and both are worth seeing.
    """
    from fafnir.db import repository as repo

    missing = [i for i in flag_ids if i not in closed]
    if not missing:
        return
    found = repo.list_dq_flags(
        database, repo.DqFilter(state="all", flag_ids=missing), limit=len(missing)
    )
    already = {int(r["dq_flag_id"]): r for r in found}
    for flag_id in missing:
        row = already.get(flag_id)
        if row is None:
            click.echo(f"  {flag_id}: no such flag.", err=True)
        else:
            click.echo(
                f"  {flag_id}: already resolved {_fmt_stamp(row['resolved_at'])}"
                f" by {row['resolved_by'] or 'unknown'}.",
                err=True,
            )


@dq.command("reopen")
@click.argument("flag_ids", nargs=-1, type=int, required=True, metavar="ID...")
@click.pass_context
def dq_reopen(ctx, flag_ids):
    """Put resolved flags back in the queue (undo a `dq resolve`).

    The resolution note and resolver are cleared with it -- a note explaining a
    decision that no longer stands is worse than none.

    Ids only. A flag whose condition has since been flagged afresh cannot reopen:
    the queue holds one open row per condition, and it already has one.
    """
    from fafnir.db import repository as repo

    with Database(ctx.obj["config"].dsn) as database:
        reopened, conflicted = repo.reopen_dq_flags(database, flag_ids)
        missing = [
            i for i in flag_ids if i not in set(reopened) and i not in set(conflicted)
        ]
        click.echo(f"Reopened {_plural(len(reopened), 'flag')}.")
        for flag_id in conflicted:
            click.echo(
                f"  {flag_id}: the same condition is already open on a newer flag; "
                "left resolved.",
                err=True,
            )
        if missing:
            found = {
                int(r["dq_flag_id"])
                for r in repo.list_dq_flags(
                    database,
                    repo.DqFilter(state="all", flag_ids=missing),
                    limit=len(missing),
                )
            }
            for flag_id in missing:
                reason = "already open" if flag_id in found else "no such flag"
                click.echo(f"  {flag_id}: {reason}.", err=True)


@main.command("status")
@click.pass_context
def status(ctx):
    """Show warehouse freshness and volume."""
    from fafnir.db import repository as repo

    with Database(ctx.obj["config"].dsn) as database:
        counts = repo.read_security_count(database)
        price_rows = database.fetchval("SELECT count(*) FROM core.daily_price")
        latest = database.fetchval("SELECT max(trade_date) FROM core.daily_price")
        actions = database.fetchval("SELECT count(*) FROM core.corporate_action")
        # Same count as ever -- count(*) WHERE resolved_at IS NULL -- now through
        # the queue's own read API, plus the error-severity slice, so the line says
        # whether the backlog contains anything that failed rather than warned.
        open_flags = int(repo.dq_flag_totals(database).get("flags") or 0)
        open_errors = int(
            repo.dq_flag_totals(database, repo.DqFilter(severities=("error",))).get(
                "flags"
            )
            or 0
        )
        new_listings = repo.count_recent_listings(database, days=7)
        pending_renames = repo.count_unapplied_symbol_changes(database)
        rename_sample = repo.unapplied_symbol_changes(database, limit=10)
    click.echo(
        f"Securities : {counts.get('securities', 0)} "
        f"(active {counts.get('active', 0)}, delisted {counts.get('delisted', 0)})"
    )
    click.echo(f"New (7d)   : {new_listings}")
    click.echo(f"Price rows : {price_rows}  (latest {latest})")
    click.echo(f"Actions    : {actions}")
    dq_line = f"Open DQ    : {open_flags}"
    if open_errors:
        dq_line += f" ({open_errors} error)"
    if open_flags:
        dq_line += "  -- fafnir dq list"
    click.echo(dq_line)
    if pending_renames:
        click.echo(f"Renames    : {pending_renames} unapplied (need review)")
        for row in rename_sample:
            click.echo(
                f"             {row['old_symbol']} -> {row['new_symbol']} "
                f"({row['change_date']}, {row['status']})"
            )


if __name__ == "__main__":
    main()

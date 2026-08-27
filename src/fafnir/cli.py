"""
``fafnir`` -- administration CLI for the warehouse: migrations, seeding, ingestion,
adjustment, data-quality, and status. Read access for analysis is via ``duk`` (db mode).

Examples
--------
    fafnir db migrate
    fafnir db seed
    fafnir ingest securities --limit 500
    fafnir ingest prices --symbols AAPL,MSFT --from 2023-01-01
    fafnir ingest actions --symbols AAPL && fafnir adjust
    fafnir db refresh-marts
    fafnir dq run
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
@click.option("--symbols", help="Comma-separated symbols (default: all securities)")
@click.pass_context
def ingest_actions(ctx, symbols):
    """Load corporate actions (splits + dividends)."""
    from fafnir.ingest import corporate_actions

    cfg = ctx.obj["config"]
    fmp = _fmp_client(cfg)
    with Database(cfg.dsn) as database:
        syms = _split_symbols(symbols) or [
            r["primary_symbol"]
            for r in database.fetchall(
                "SELECT primary_symbol FROM core.security ORDER BY security_id"
            )
        ]
        n = corporate_actions.load_actions(database, fmp, syms)
    click.echo(f"Loaded {n} corporate actions for {len(syms)} symbols.")


@main.command("adjust")
@click.option("--symbol", help="Recompute one symbol only (default: all with actions)")
@click.pass_context
def adjust(ctx, symbol):
    """Recompute adjustment factors from corporate actions."""
    from fafnir.db import repository as repo
    from fafnir.ingest import adjustments

    cfg = ctx.obj["config"]
    with Database(cfg.dsn) as database:
        sec_id = repo.resolve_security_id(database, symbol.upper()) if symbol else None
        n = adjustments.adjust_all(database, security_id=sec_id)
    click.echo(f"Recomputed adjustment factors for {n} securities.")


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
        open_flags = database.fetchval(
            "SELECT count(*) FROM ops.data_quality_flag WHERE resolved_at IS NULL"
        )
        new_listings = repo.count_recent_listings(database, days=7)
        pending_renames = repo.unapplied_symbol_changes(database)
    click.echo(
        f"Securities : {counts.get('securities', 0)} "
        f"(active {counts.get('active', 0)}, delisted {counts.get('delisted', 0)})"
    )
    click.echo(f"New (7d)   : {new_listings}")
    click.echo(f"Price rows : {price_rows}  (latest {latest})")
    click.echo(f"Actions    : {actions}")
    click.echo(f"Open DQ    : {open_flags}")
    if pending_renames:
        click.echo(f"Renames    : {len(pending_renames)} unapplied (need review)")
        for row in pending_renames[:10]:
            click.echo(
                f"             {row['old_symbol']} -> {row['new_symbol']} "
                f"({row['change_date']}, {row['status']})"
            )


if __name__ == "__main__":
    main()

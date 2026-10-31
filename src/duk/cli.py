"""
CLI entry point for duk.

This module provides the main command-line interface using Click.
"""

import csv
import datetime as dt
import difflib
import io
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

import click
import pandas as pd
from click.core import ParameterSource

from duk import __version__, company_summary, identifiers
from duk import table as tables
from duk.config import get_config
from duk.datasource import base as ds_base
from duk.datasource import db as ds_db
from duk.datasource import live as ds_live
from duk.datasource import sharadar as ds_sharadar
from duk.fmp_api import (
    actively_trading_list_api,
    get_yield_curve,
    industry_list_api,
    sector_list_api,
)
from duk.format_utils import (
    DEFAULT_DECIMALS,
    exact_records,
    format_price_columns,
    plain_records,
    round_price_columns,
)
from duk.indicators import (
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)
from duk.logging_config import enable_console_logging, setup_logging
from duk.ls_utils import process_industries, process_sectors
from duk.return_utils import (
    annualized_return,
    cumulative_log_return,
    cumulative_simple_return,
    log_return,
    price_difference,
    simple_return,
)
from duk.stats import compute_summary_stats, format_summary_stats, summary_stats_rows

# Key rate tenors for yield curve analysis
KEY_RATE_TENORS = ["year1", "year5", "year10", "year20", "year30"]


# ---------------------------------------------------------------------------
# Output format: a table for a person, CSV or JSON for a program
# ---------------------------------------------------------------------------

TABLE_HELP = "Output a bordered table (the default when stdout is a terminal)"
CSV_HELP = "Output data as CSV (the default when stdout is not a terminal)"


def table_option(func):
    """``--table``, beside every command's ``--csv`` and ``--json``."""
    return click.option("--table", "output_table", is_flag=True, help=TABLE_HELP)(func)


def output_format(ctx, output_table, output_csv, output_json):
    """``table``, ``csv`` or ``json``: the flag given, else ``DUK_FORMAT`` /
    ``[general].console_format``, else a table on a terminal and CSV in a pipe
    (:func:`duk.table.resolve_format`). Two flags at once exit 1."""
    cfg = ctx.obj.get("config") if ctx.obj else None
    try:
        return tables.resolve_format(
            table=output_table,
            csv=output_csv,
            json=output_json,
            configured=getattr(cfg, "console_format", None),
        )
    except tables.FormatError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def echo_table(rows, columns=None, **kwargs):
    """Render ``rows`` (records or a DataFrame) as a table on stdout, fitted to the
    terminal, in the characters stdout can encode."""
    style = tables.style_for_stream(sys.stdout)
    footer = kwargs.pop("footer", None)
    title = kwargs.pop("title", None)
    notes = kwargs.pop("notes", None) or []
    if isinstance(rows, pd.DataFrame):
        records = rows.to_dict(orient="records")
        columns = columns or list(rows.columns)
    else:
        records = list(rows)
    built = tables.table_from_rows(
        records, columns, style=style, title=title, footer=footer, **kwargs
    )
    built.notes = list(notes)
    click.echo(
        tables.render(
            built,
            style=style,
            max_width=tables.terminal_width(sys.stdout),
            color=tables.color_enabled(),
        )
    )


def echo_summary_table(stats, *, title="Summary statistics", precision=4):
    """`--summary` as a table: one row per numeric column."""
    if stats is None:
        click.echo("No statistics available")
        return
    footer = tables.plural(stats["n_observations"], "observation")
    if stats.get("min_date") is not None and stats.get("max_date") is not None:
        footer += f" · {stats['min_date']} → {stats['max_date']}"
    rows = summary_stats_rows(stats)
    if not rows:
        click.echo(f"{title}: no numeric columns. {footer}")
        return
    echo_table(rows, title=title, footer=footer, decimals=precision)


def bar_footer(frame, *, symbol, adjusted, frequency):
    """``61 bars · AAPL · raw · day · 2024-01-02 → 2024-03-28``."""
    parts = [
        tables.plural(len(frame), "bar"),
        symbol,
        "adjusted" if adjusted else "raw",
        frequency,
    ]
    if "date" in frame.columns and len(frame):
        first, last = frame["date"].iloc[0], frame["date"].iloc[-1]
        parts.append(
            f"{pd.Timestamp(first).date().isoformat()} → "
            f"{pd.Timestamp(last).date().isoformat()}"
        )
    return " · ".join(parts)


def match_column(df, column):
    """``column`` as ``df`` spells it, matched case-insensitively when there is
    exactly one such column; otherwise unchanged, so the caller's "not found"
    names what was typed."""
    if column in df.columns:
        return column
    found = [c for c in df.columns if str(c).lower() == str(column).lower()]
    return found[0] if len(found) == 1 else column


def apply_precision_to_dataframe(df, precision, exclude_columns=None):
    """
    Apply precision rounding to numeric columns in a DataFrame.

    Args:
        df: pandas DataFrame to apply precision to
        precision: Number of decimal places to round to
        exclude_columns: List of column names to exclude from rounding
            (default: ['date'])

    Returns:
        The modified DataFrame with precision applied
    """
    if exclude_columns is None:
        exclude_columns = ["date"]

    # Select numeric columns
    numeric_columns = df.select_dtypes(include=["float", "int"]).columns
    # Exclude specified columns
    numeric_columns = [col for col in numeric_columns if col not in exclude_columns]
    # Apply rounding
    df[numeric_columns] = df[numeric_columns].round(precision)
    return df


def parse_security_argument(argument):
    """Parse a SYMBOL argument that may be ``cik:``/``isin:``/``cusip:`` prefixed.

    Exits 1 on a malformed identifier rather than letting it reach a data source:
    ``cik:51413`` transposed is not a lookup that should come back "no data found",
    which is what sending it on as a ticker would produce.
    """
    try:
        return identifiers.parse(argument)
    except identifiers.IdentifierError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def require_source_credentials(ctx, source, *, needs_key=True):
    """Exit 1 with an actionable message when the active source cannot be read.

    ``db`` needs a DSN; ``fmp`` and ``sharadar`` need that vendor's key. Stated
    once so every command gives the same instruction, and so a missing DSN under
    the db default reads as configuration advice rather than a psycopg traceback.
    """
    cfg = ctx.obj["config"]
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if source == "db":
        if not cfg.dsn:
            logger.error("No warehouse DSN configured")
            click.echo(
                "Error: no fafnir DSN configured. Set FAFNIR_DSN or [database].dsn "
                "in ~/.dukrc, or read a vendor directly with -S fmp / -S sharadar.",
                err=True,
            )
            sys.exit(1)
        return None
    if not needs_key:
        return None
    if source == "fmp":
        if not cfg.fmp_key:
            logger.error("FMP API key not configured")
            click.echo(
                "Error: FMP API key not configured. Set FMP_API_KEY environment "
                "variable or add fmp_key to [api] section in ~/.dukrc",
                err=True,
            )
            sys.exit(1)
        return cfg.fmp_key
    if source == "sharadar":
        if not cfg.sharadar_key:
            logger.error("Sharadar API key not configured")
            click.echo(
                "Error: Sharadar API key not configured. Set SHARADAR_API_KEY "
                "environment variable or add sharadar_key to [api] section in "
                "~/.dukrc",
                err=True,
            )
            sys.exit(1)
        return cfg.sharadar_key
    return None


def resolve_security_identifier(ctx, identifier):
    """Turn an identifier into the ``(symbol, security_id)`` of one security.

    ``security_id`` is None in live mode, which has no security master; the ticker
    the vendor returned is the whole of what live resolution yields.

    Never picks among several. A CIK is an issuer and an issuer can list more than
    one class, so "the first one" would silently answer about GOOG when the caller
    meant GOOGL -- the same reason a name matching several companies prints a
    did-you-mean and stops.
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "db")

    if identifier.scheme == identifiers.TICKER_AT and source != "db":
        click.echo(
            f"Error: {identifier.describe()} resolves through the warehouse's "
            "ticker history; use -S db.",
            err=True,
        )
        sys.exit(1)
    if identifier.scheme == identifiers.SECURITY_ID and source != "db":
        click.echo(
            f"Error: {identifier.describe()} is the warehouse's own key; a vendor "
            "has no such identifier. Use -S db.",
            err=True,
        )
        sys.exit(1)

    if source == "sharadar":
        # Sharadar's TICKERS carries CUSIPs and the CIK, but duk does not read it
        # yet; the warehouse (which will hold the permaticker link) and FMP's
        # search both resolve identifiers today. Refuse rather than guess.
        click.echo(
            f"Error: {identifier.describe()} cannot be resolved against the "
            "Sharadar source yet; pass a ticker, or resolve it with -S db / -S fmp.",
            err=True,
        )
        sys.exit(1)

    try:
        if source == "db":
            candidates = ds_db.resolve_identifier(dsn=cfg.dsn, identifier=identifier)
        else:
            candidates = ds_live.resolve_identifier(
                api_key=cfg.fmp_key, identifier=identifier
            )
    except Exception as exc:
        logger.error(f"Identifier lookup failed: {exc}")
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not candidates:
        click.echo(f"No security found for {identifier.describe()}.", err=True)
        sys.exit(1)
    if len(candidates) > 1:
        click.echo(
            f"{identifier.describe()} matches {len(candidates)} securities. "
            "Re-run with one of these tickers:",
            err=True,
        )
        click.echo(company_summary.render_candidates(candidates), err=True)
        sys.exit(1)

    matched = candidates[0]
    logger.info(f"Resolved {identifier.describe()} to {matched['symbol']}")
    return matched["symbol"], matched.get("security_id")


def hint_reused_ticker(ctx, identifier):
    """On stderr, when a bare ticker has named more than one security: which, and
    the ``TICKER@YYYY-MM-DD`` form that reaches each (#80).

    The bare ticker still answers as it always has -- the security holding it now,
    or that held it last -- so a script's output is unchanged. Best effort: a
    failed lookup here never fails the command.
    """
    if not identifier.is_ticker or ctx.obj.get("source", "db") != "db":
        return
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    try:
        periods = ds_db.ticker_periods(
            dsn=ctx.obj["config"].dsn, symbol=identifier.value
        )
    except Exception as exc:  # noqa: BLE001 -- a hint, never a failure
        logger.debug(f"Ticker history lookup failed: {exc}")
        return
    if len(periods) < 2:
        return
    click.echo(
        f"Note: {identifier.value} has named {len(periods)} securities. The bare "
        "ticker reads the one holding it now, or last; for another, name a day "
        "it traded:",
        err=True,
    )
    click.echo(
        company_summary.render_ticker_periods(identifier.value, periods), err=True
    )


@click.group()
@click.version_option(version=__version__, message="%(version)s")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to configuration file (default: ~/.dukrc)",
)
@click.option(
    "--source",
    "-S",
    type=click.Choice(["db", "fmp", "sharadar", "live"], case_sensitive=False),
    default=None,
    help="Data source: 'db' (the fafnir warehouse, the default), or a vendor's API "
    "directly: 'fmp' or 'sharadar' ('live' is the older name for fmp). "
    "Defaults to [general].default_source, else db.",
)
@click.pass_context
def main(ctx, config, source):
    """
    duk - A CLI tool for downloading markets and financial data.

    Reads the fafnir PostgreSQL warehouse by default (-S db), or a vendor's API
    directly (-S fmp, -S sharadar). Use 'duk <subprogram> --help' for information
    on specific subprograms.
    """
    # Ensure that ctx.obj exists and is a dict
    ctx.ensure_object(dict)

    # Load configuration
    ctx.obj["config"] = get_config(config)

    # Setup logging
    cfg = ctx.obj["config"]
    # Console output is opt-in per subcommand via -v/--verbose; until then the
    # terminal carries only the command's own output.
    logger = setup_logging(
        log_level=cfg.log_level,
        log_dir=cfg.log_dir,
        console_output=False,
    )
    ctx.obj["logger"] = logger

    # Resolve the active data source (per-invocation override or configured default).
    ctx.obj["source"] = ds_base.resolve_source(source, cfg.default_source)

    logger.debug("duk initialized")
    logger.debug(f"Configuration loaded from: {cfg.config_path}")
    logger.debug(f"Log level: {cfg.log_level}")
    logger.debug(f"Data source: {ctx.obj['source']}")


@main.command()
@click.argument("symbol")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing price history data to stdout"
)
@click.option("-s", "--start-date", help="Start date (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="End date (YYYY-MM-DD)")
@click.option(
    "-n",
    "--limit",
    type=int,
    help="Number of bars to return (trading periods at --frequency)",
)
@click.option(
    "-f",
    "--frequency",
    type=click.Choice(
        ["day", "week", "month", "quarter", "semi-annual", "annual"],
        case_sensitive=False,
    ),
    default="day",
    help="Data frequency (default: day)",
)
@click.option("--ohlc", is_flag=True, help="Return Date, Open, High, Low, Close fields")
@click.option("--hlc", is_flag=True, help="Return Date, High, Low, Close fields")
@click.option("--close", is_flag=True, help="Return Date and Close fields")
@click.option(
    "--hlcv", is_flag=True, help="Return Date, High, Low, Close, Volume fields"
)
@click.option("--cv", is_flag=True, help="Return Date, Close, Volume fields")
@click.option("--adj", is_flag=True, help="Retrieve dividend-adjusted price history")
@click.option(
    "--exact",
    is_flag=True,
    help=(
        "Print prices exactly as the warehouse stores them, as decimal text, never "
        "through a float. Daily bars from the warehouse (-S db) only; --precision "
        "does not apply."
    ),
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=DEFAULT_DECIMALS,
    help=(
        f"Decimal places for price columns (default: {DEFAULT_DECIMALS}). Prices too "
        "small for that precision keep significant digits instead of showing 0.00. "
        "Applies to --output files only when given explicitly."
    ),
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help=(
        "Write data to file (CSV, or JSON with --json). If path is a directory, "
        "format filename as <symbol>_<start>_<end>.<ext>"
    ),
)
@click.pass_context
def ph(
    ctx,
    symbol,
    verbose,
    quiet,
    start_date,
    end_date,
    limit,
    frequency,
    ohlc,
    hlc,
    close,
    hlcv,
    cv,
    adj,
    exact,
    precision,
    output_table,
    output_csv,
    output_json,
    summary,
    output,
):
    """
    Request price history for a symbol.

    SYMBOL: The ticker symbol (e.g., AAPL, MSFT). Case insensitive. A security may
    also be named by identifier -- cik:51143, isin:US4592001014, cusip:459200101 --
    which is resolved to its ticker before anything else happens.
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    # Ticker, or an identifier standing in for one. Parsed before anything else so
    # a malformed identifier is a parse error, not an empty price series.
    identifier = parse_security_argument(symbol)
    symbol = identifier.value

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "db")
    precision_requested = (
        ctx.get_parameter_source("precision") == ParameterSource.COMMANDLINE
    )
    # --exact is a contract with a program that will not read a price through a
    # float: refused outright wherever it cannot be kept, rather than quietly
    # printing something rounded.
    if exact:
        if source != "db":
            raise click.UsageError(
                "--exact prints the warehouse's stored decimals; a vendor API sends "
                "floats, so there is nothing exact to print. Use it with -S db."
            )
        if frequency.lower() != "day":
            raise click.UsageError(
                "--exact prints stored daily bars; resampling to --frequency "
                f"{frequency} would compute new ones."
            )
        if summary:
            raise click.UsageError("--exact prints bars, not --summary statistics.")
        if precision_requested:
            raise click.UsageError(
                "--exact prints every stored digit; --precision does not apply."
            )
    api_key = require_source_credentials(ctx, source)

    # An identifier is resolved to ONE security up front, so everything downstream
    # -- the query, the log line, the -o filename -- works on a ticker exactly as it
    # always has. In db mode the resolved security_id is carried through too: a
    # security found by CIK must not be re-found by its primary_symbol, which a
    # reused ticker can resolve to somebody else.
    security_id = None
    if not identifier.is_ticker:
        symbol, security_id = resolve_security_identifier(ctx, identifier)
    else:
        hint_reused_ticker(ctx, identifier)

    logger.info(f"Requesting price history for {symbol}")

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Determine which fields to return
    fields = None
    field_filters = [ohlc, hlc, close, hlcv, cv]
    if sum(field_filters) > 1:
        logger.error("Only one field filter option can be specified")
        click.echo(
            "Error: Only one of --ohlc, --hlc, --close, --hlcv, --cv can be specified",
            err=True,
        )
        sys.exit(1)

    if ohlc:
        fields = ["open", "high", "low", "close"]
    elif hlc:
        fields = ["high", "low", "close"]
    elif close:
        fields = ["close"]
    elif hlcv:
        fields = ["high", "low", "close", "volume"]
    elif cv:
        fields = ["close", "volume"]

    # Fetch price history from the active source
    try:
        if source == "db":
            df = ds_db.price_history(
                dsn=cfg.dsn,
                symbol=symbol,
                security_id=security_id,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                limit=limit,
                fields=fields,
                adjusted=adj,
                exact=exact,
            )
        elif source == "sharadar":
            df = ds_sharadar.price_history(
                api_key=api_key,
                base_url=cfg.sharadar_base_url or None,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                limit=limit,
                fields=fields,
                adjusted=adj,
            )
        else:
            df = ds_live.price_history(
                api_key=api_key,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                limit=limit,
                fields=fields,
                adjusted=adj,
            )
    except Exception as e:
        logger.error(f"Failed to fetch price history: {e}")
        click.echo(f"Error: Failed to fetch price history: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning(f"No data returned for {symbol}")
        if not quiet:
            click.echo(f"No data found for {symbol}")
        sys.exit(0)

    logger.info(f"Retrieved {len(df)} records for {symbol}")

    # Prepare output
    # Reset index to include date as a column in output
    output_df = df.reset_index()

    if exact:
        _print_exact(
            exact_records(output_df),
            console=console,
            output=output,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            quiet=quiet,
            logger=logger,
            footer=bar_footer(output_df, symbol=symbol, adjusted=adj, frequency="day"),
        )
        return

    # Display precision is applied at the output boundary only: `df` itself keeps
    # full precision, so the library contract and the db/live parity guarantee are
    # untouched. --output files are left alone unless --precision was given
    # explicitly, because -o feeds the `ti`/`rc` compute pipeline and rounding a
    # sub-dollar adjusted series to 2dp there is a real quantization error.

    def _as_text(frame):
        return format_price_columns(frame, decimals=precision)

    def _as_numbers(frame):
        return round_price_columns(frame, decimals=precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # If output is a directory, format filename
        if output_path.is_dir():
            start_str = start_date or "earliest"
            end_str = end_date or "latest"
            filename = f"{symbol}_{start_str}_{end_str}.{file_format}"
            output_path = output_path / filename

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if file_format == "json":
            file_df = _as_numbers(output_df) if precision_requested else output_df
            file_df.to_json(output_path, orient="records", date_format="iso")
        else:
            file_df = _as_text(output_df) if precision_requested else output_df
            file_df.to_csv(output_path, index=False)

        logger.info(f"Data written to {output_path}")

        if not quiet:
            click.echo(f"Data written to {output_path}")

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(df.reset_index())
            if console == "table":
                echo_summary_table(stats, title=f"{symbol} summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(
                _as_numbers(output_df).to_json(orient="records", date_format="iso")
            )
        elif console == "table":
            echo_table(
                output_df,
                price_decimals=precision,
                footer=bar_footer(
                    output_df, symbol=symbol, adjusted=adj, frequency=frequency
                ),
            )
        else:
            click.echo(_as_text(output_df).to_csv(index=False))


def _exact_text(records, file_format):
    """Exact records as JSON (numbers as strings) or CSV (the same text)."""
    if file_format == "json":
        return json.dumps(records)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(records[0]) if records else [], lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue()


def _print_exact(
    records, *, console, output, symbol, start_date, end_date, quiet, logger, footer
):
    """`ph --exact` output: every value as the text the warehouse stores.

    JSON is a list of objects whose numbers are strings, so a reader that parses
    JSON numbers as floats cannot round one by accident; CSV is the same text. A
    table shows the same digits, every one, with thousands separators.
    """
    file_format = "json" if console == "json" else "csv"
    if output:
        output_path = Path(output)
        if output_path.is_dir():
            name = (
                f"{symbol}_{start_date or 'earliest'}_{end_date or 'latest'}."
                f"{file_format}"
            )
            output_path = output_path / name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_exact_text(records, file_format), encoding="utf-8")
        logger.info(f"Data written to {output_path}")
        if not quiet:
            click.echo(f"Data written to {output_path}")
    if quiet:
        return
    if console == "table":
        exact_rows = [
            {k: (v if k == "date" or v is None else Decimal(v)) for k, v in r.items()}
            for r in records
        ]
        echo_table(exact_rows, exact=True, footer=footer + " · exact")
    else:
        click.echo(_exact_text(records, file_format).rstrip("\n"))


@main.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing yield curve data to stdout"
)
@click.option("-s", "--start-date", help="Start date (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="End date (YYYY-MM-DD)")
@click.option(
    "-n",
    "--limit",
    type=int,
    help="Number of observations (trading days) to return",
)
@click.option(
    "-z",
    "--zero-rates",
    is_flag=True,
    help="Return zero rate yield curve (bootstrapped from par rates)",
)
@click.option(
    "--tenors",
    help=(
        "Filter tenor range. Specify as 'start_tenor, end_tenor'. "
        "Example: 'month6, year10' to return tenors from 6 months to 10 years."
    ),
)
@click.option(
    "--key-rates",
    is_flag=True,
    help="Return only key rate tenors: year1, year5, year10, year20, year30",
)
@click.option(
    "-i",
    "--interval",
    type=click.Choice(
        ["day", "week", "month", "quarter", "semi-annual", "annual"],
        case_sensitive=False,
    ),
    help="Interpolation interval between tenors",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help=(
        "Write data to file. If path is a directory, "
        "format filename as yc_<start>_<end>.<ext>"
    ),
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=4,
    help="Decimal precision for yield rates (default: 4)",
)
@click.pass_context
def yc(
    ctx,
    verbose,
    quiet,
    start_date,
    end_date,
    limit,
    zero_rates,
    tenors,
    key_rates,
    interval,
    output_table,
    output_csv,
    output_json,
    summary,
    output,
    precision,
):
    """
    Request yield curve data.

    Retrieves treasury yield curve data from the FMP API.
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info("Requesting yield curve data")

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "db")
    api_key = cfg.fmp_key

    if source == "sharadar":
        # Sharadar sells equity and fund data; it has no treasury curve.
        click.echo(
            "Error: yc has no Sharadar backend (Sharadar carries no treasury "
            "rates). Use -S fmp, or -S db, which reads FMP for it.",
            err=True,
        )
        sys.exit(1)
    if source == "fmp":
        require_source_credentials(ctx, source)

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Handle tenors parameter and key-rates mutual exclusivity
    if tenors and key_rates:
        logger.error("Cannot use both --tenors and --key-rates")
        click.echo(
            "Error: Cannot use both --tenors and --key-rates. Choose one.",
            err=True,
        )
        sys.exit(1)

    # Parse tenors parameter
    tenors_tuple = None
    if tenors:
        try:
            # Parse the tenors string (e.g., "month6, year10")
            # Tenor names are the curve's lowercase columns: Month6 is month6.
            tenor_parts = [t.strip().lower() for t in tenors.split(",")]
            if len(tenor_parts) != 2:
                raise ValueError("tenors must have exactly 2 values")
            tenors_tuple = (tenor_parts[0], tenor_parts[1])
        except Exception as e:
            logger.error(f"Invalid tenors format: {e}")
            click.echo(
                f"Error: Invalid tenors format. Use format: 'start_tenor, end_tenor'. "
                f"Example: 'month6, year10'. Error: {e}",
                err=True,
            )
            sys.exit(1)

    # Handle key-rates option
    if key_rates:
        tenors_tuple = (KEY_RATE_TENORS[0], KEY_RATE_TENORS[-1])

    # Yield-curve data is not yet in the fafnir warehouse (economic-series is a
    # fast-follow), so yc always reads live from FMP regardless of --source.
    if source == "db":
        logger.warning("yc has no db backend yet; falling back to the FMP source")
        if not api_key:
            click.echo(
                "Error: yc requires the FMP API. Set FMP_API_KEY or use -S fmp.",
                err=True,
            )
            sys.exit(1)

    # Fetch yield curve
    try:
        df = get_yield_curve(
            api_key=api_key,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            zero_rates=zero_rates,
            tenors=tenors_tuple,
            interval=interval,
        )
    except Exception as e:
        logger.error(f"Failed to fetch yield curve: {e}")
        click.echo(f"Error: Failed to fetch yield curve: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("No data returned for yield curve")
        if not quiet:
            click.echo("No yield curve data found")
        sys.exit(0)

    # Apply precision to yield rates
    rate_columns = df.select_dtypes(include=["float", "int"]).columns
    # Exclude non-rate columns like 'date' or 'years'
    rate_columns = [col for col in rate_columns if col not in ["date", "years"]]
    df[rate_columns] = df[rate_columns].round(precision)

    # Filter for key-rates if specified (after fetching data)
    if key_rates:
        # Check if we have a single-date response (tenor-indexed)
        if "years" in df.columns:
            # Single date format - filter by index (tenor names)
            available_key_tenors = [t for t in KEY_RATE_TENORS if t in df.index]
            df = df.loc[available_key_tenors]
        else:
            # Multiple date format - filter by columns (tenor names)
            available_key_tenors = [t for t in KEY_RATE_TENORS if t in df.columns]
            df = df[available_key_tenors]

    logger.info(f"Retrieved yield curve data with {len(df)} records")

    # Prepare output
    # Reset index to include date/tenor as a column in output
    output_df = df.reset_index()

    # Handle output to file
    if output:
        output_path = Path(output)

        # If output is a directory, format filename
        if output_path.is_dir():
            start_str = start_date or "earliest"
            end_str = end_date or "latest"
            filename = f"yc_{start_str}_{end_str}.{file_format}"
            output_path = output_path / filename

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if file_format == "json":
            output_df.to_json(output_path, orient="records", date_format="iso")
        else:
            output_df.to_csv(output_path, index=False)

        logger.info(f"Data written to {output_path}")

        if not quiet:
            click.echo(f"Data written to {output_path}")

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(df.reset_index())
            if console == "table":
                echo_summary_table(stats, title="Yield curve summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(output_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            kind = "zero rates" if zero_rates else "par rates"
            echo_table(
                output_df,
                decimals=precision,
                footer=f"{tables.plural(len(output_df), 'row')} · US Treasury "
                f"{kind}, percent · FMP",
            )
        else:
            click.echo(output_df.to_csv(index=False))


@main.command()
@click.argument("query", required=False)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing list data to stdout"
)
@click.option("-n", "--limit", type=int, help="Limit number of records to return")
@click.option(
    "--sectors",
    "sectors_list_flag",
    is_flag=True,
    help="List the market sectors, with how many securities each holds",
)
@click.option(
    "--industries",
    "industries_list_flag",
    is_flag=True,
    help="List the industries, with each one's sector and how many securities it "
    "holds",
)
@click.option(
    "--sector",
    "sectors_filter",
    help="Screen by sectors (comma-separated; case-insensitive)",
)
@click.option(
    "--industry",
    "industries_filter",
    help="Screen by industries (comma-separated; case-insensitive)",
)
@click.option(
    "--market-cap",
    multiple=True,
    help=(
        "Filter by market cap (use >value or <value syntax, "
        "can be specified multiple times for range)"
    ),
)
@click.option(
    "--price",
    multiple=True,
    help=(
        "Filter by stock price (use >value or <value syntax, "
        "can be specified multiple times for range)"
    ),
)
@click.option(
    "--volume",
    multiple=True,
    help=(
        "Filter by trading volume (use >value or <value syntax, "
        "can be specified multiple times for range)"
    ),
)
@click.option(
    "--beta",
    multiple=True,
    help=(
        "Filter by beta value (use >value or <value syntax, "
        "can be specified multiple times for range)"
    ),
)
@click.option(
    "--dividend",
    multiple=True,
    help=(
        "Filter by dividend (use >value or <value syntax, "
        "can be specified multiple times for range)"
    ),
)
@click.option(
    "--exchange",
    help="Filter by exchange (e.g., NASDAQ, NYSE; case-insensitive)",
)
@click.option(
    "--country",
    help="Filter by country code (e.g., US; case-insensitive)",
)
@click.option(
    "--is-etf",
    is_flag=True,
    help="Filter for ETFs only",
)
@click.option(
    "--is-fund",
    is_flag=True,
    help="Filter for funds only",
)
@click.option(
    "--is-actively-trading",
    is_flag=True,
    help="Filter for actively trading securities only (the db default)",
)
@click.option(
    "--include-delisted",
    is_flag=True,
    help="Screen the warehouse's delisted securities too (-S db only)",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def ls(
    ctx,
    query,
    verbose,
    quiet,
    limit,
    sectors_list_flag,
    industries_list_flag,
    sectors_filter,
    industries_filter,
    market_cap,
    price,
    volume,
    beta,
    dividend,
    exchange,
    country,
    is_etf,
    is_fund,
    is_actively_trading,
    include_delisted,
    output_table,
    output_csv,
    output_json,
    summary,
    output,
):
    """
    List company and market information, or summarize one company.

    With a QUERY (a ticker, an identifier such as cik:51143 / isin:US4592001014 /
    cusip:459200101, or a company name), prints everything the warehouse holds on
    that one company: meta, price-history and corporate-action statistics,
    fundamentals when loaded, and any open data-quality flags. Requires --source
    db, since three of those four describe the warehouse itself.

    Without a QUERY the behaviour is unchanged: returns actively trading
    securities with symbol and name.
    Use --sectors to list market sectors or --sector="Tech,Healthcare" to screen.
    Use --industries to list industries or --industry="Software,Banking" to screen.
    Names are case-insensitive (--sector=energy), and a name that is not one the
    source knows is refused with the nearest matches.

    In the warehouse, --sectors and --industries count the securities in each
    (actively trading unless --include-delisted), and --industries names each
    industry's sector. Screening flags scope the counts:
    `duk ls --industries --sector energy` lists the industries in Energy.

    Screening supports comparison operators:
    - Use > for greater than (e.g., --price=">50")
    - Use < for less than (e.g., --market-cap="<1000000000")
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "db")
    api_key = cfg.fmp_key

    # ------------------------------------------------------------------
    # Profile mode: a QUERY names ONE company, and the whole command becomes a
    # report about it. Handled before anything else and returns, so the list and
    # screen paths below are untouched by this feature.
    # ------------------------------------------------------------------
    if query:
        _ls_company_summary(
            ctx,
            query=query,
            quiet=quiet,
            limit=limit,
            summary=summary,
            output=output,
            output_table=output_table,
            output_csv=output_csv,
            output_json=output_json,
            screening_flags=(
                sectors_list_flag,
                industries_list_flag,
                sectors_filter,
                industries_filter,
                market_cap,
                price,
                volume,
                beta,
                dividend,
                exchange,
                country,
                is_etf,
                is_fund,
                is_actively_trading,
                include_delisted,
            ),
        )
        return

    if include_delisted and source != "db":
        click.echo(
            "Error: --include-delisted screens the warehouse; use -S db.", err=True
        )
        sys.exit(1)
    if include_delisted and is_actively_trading:
        click.echo(
            "Error: --include-delisted and --is-actively-trading contradict each "
            "other.",
            err=True,
        )
        sys.exit(1)

    if source == "sharadar":
        # Listing and screening read the warehouse snapshot or FMP's screener;
        # Sharadar's TICKERS/DAILY tables are not wired into duk.
        click.echo(
            "Error: ls is not available from the Sharadar source; use -S db "
            "(the warehouse) or -S fmp.",
            err=True,
        )
        sys.exit(1)
    require_source_credentials(ctx, source)

    # Check for mutually exclusive options in list mode
    if sectors_list_flag and industries_list_flag:
        logger.error("Only one list type option can be specified")
        click.echo(
            "Error: Only one of --sectors or --industries can be specified",
            err=True,
        )
        sys.exit(1)

    # Check if mixing list and filter flags
    if sectors_list_flag and sectors_filter:
        logger.error("Cannot use both --sectors and --sector")
        click.echo(
            "Error: Cannot use both --sectors (list flag) and --sector (filter)",
            err=True,
        )
        sys.exit(1)

    if industries_list_flag and industries_filter:
        logger.error("Cannot use both --industries and --industry")
        click.echo(
            "Error: Cannot use both --industries (list flag) and --industry (filter)",
            err=True,
        )
        sys.exit(1)

    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    screening_params = [
        market_cap,
        price,
        volume,
        beta,
        dividend,
        exchange,
        country,
        is_etf,
        is_fund,
        is_actively_trading,
        include_delisted,
    ]
    has_screening_params = any(param for param in screening_params)
    list_mode = sectors_list_flag or industries_list_flag
    footer = None

    try:
        # Sector and industry names as the source spells them, whatever the case
        # typed: `energy`, `Energy` and `ENERGY` are one sector. Unknown names are
        # refused here, with the nearest, rather than screened into "No data".
        sectors_list = industries_list = None
        if sectors_filter or industries_filter:
            known_sectors, known_industries = _known_classification(
                cfg,
                source,
                api_key,
                need_sectors=bool(sectors_filter),
                need_industries=bool(industries_filter),
            )
            if sectors_filter:
                sectors_list = canonical_names(sectors_filter, known_sectors, "sector")
            if industries_filter:
                industries_list = canonical_names(
                    industries_filter, known_industries, "industry"
                )

        screen_kwargs = _screen_kwargs(
            market_cap=market_cap,
            price=price,
            volume=volume,
            beta=beta,
            dividend=dividend,
            sectors=sectors_list,
            industries=industries_list,
            exchange=exchange,
            country=country,
            is_etf=is_etf,
            is_fund=is_fund,
            is_actively_trading=is_actively_trading,
            source=source,
        )

        if list_mode and source == "db":
            # Counted lists: every filter given scopes the counts, so
            # `--industries --sector energy` is the industries within Energy.
            what = "sector" if sectors_list_flag else "industry"
            logger.info(f"Requesting {what} list with counts (db)")
            lister = ds_db.list_sectors if sectors_list_flag else ds_db.list_industries
            df = lister(dsn=cfg.dsn, includeDelisted=include_delisted, **screen_kwargs)
            if limit is not None and limit > 0:
                df = df.head(limit)
            footer = _count_footer(df, what, include_delisted=include_delisted)
        elif list_mode:
            # FMP lists names only. The counts, and an industry's sector, are
            # the warehouse's: computing them from FMP's screener would page the
            # whole universe on every call. The columns are there, as nulls, so a
            # reader sees the same shape and cannot mistake "not known" for 0.
            if sectors_list or industries_list or has_screening_params:
                click.echo(
                    "Error: with -S fmp, --sectors and --industries list names "
                    "only; screening flags scope the warehouse's counts (-S db).",
                    err=True,
                )
                sys.exit(1)
            if sectors_list_flag:
                logger.info("Requesting sector list")
                df = process_sectors(sector_list_api(api_key))
                df["security_count"] = None
            else:
                logger.info("Requesting industry list")
                df = process_industries(industry_list_api(api_key))
                df["sector_name"] = None
                df["security_count"] = None
            click.echo(
                "Note: -S fmp lists names only; security counts and each "
                "industry's sector come from the warehouse (-S db).",
                err=True,
            )
            if limit is not None and limit > 0:
                df = df.head(limit)
            footer = tables.plural(
                len(df),
                "sector" if sectors_list_flag else "industry",
                None if sectors_list_flag else "industries",
            )
        elif sectors_list or industries_list or has_screening_params:
            logger.info("Using security screening")
            if source == "db":
                df = ds_db.screen(
                    dsn=cfg.dsn,
                    includeDelisted=include_delisted,
                    limit=limit,
                    **screen_kwargs,
                )
            else:
                df = ds_live.screen(api_key=api_key, limit=limit, **screen_kwargs)
            logger.info(f"Retrieved {len(df)} screening results")
            # Reset index if it was set to symbol
            if not df.empty and df.index.name == "symbol":
                df = df.reset_index()
            footer = _screen_footer(df, limit=limit)
        elif source == "db":
            logger.info("Requesting actively trading list (db)")
            df = ds_db.list_actively_trading(dsn=cfg.dsn, limit=limit)
            if limit is not None and limit > 0:
                df = df.head(limit)
            footer = tables.plural(len(df), "security", "securities") + (
                " · actively trading"
            )
        else:
            logger.info("Requesting actively trading list")
            data = actively_trading_list_api(api_key)
            df = pd.DataFrame(data)
            expected_cols = ["symbol", "name"]
            available_cols = [col for col in expected_cols if col in df.columns]
            if available_cols:
                df = df[available_cols]
            if limit is not None and limit > 0:
                df = df.head(limit)
            footer = tables.plural(len(df), "security", "securities") + (
                " · actively trading · FMP"
            )

    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ds_base.DataSourceError as e:
        logger.error(f"Invalid screen: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to fetch list data: {e}")
        click.echo(f"Error: Failed to fetch list data: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("No data returned")
        if not quiet:
            click.echo("No data found")
        sys.exit(0)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if file_format == "json":
            df.to_json(output_path, orient="records", date_format="iso")
        else:
            df.to_csv(output_path, index=False)

        logger.info(f"Data written to {output_path}")

        if not quiet:
            click.echo(f"Data written to {output_path}")

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            click.echo(f"Number of results: {len(df)}")
        # Otherwise print data
        elif console == "json":
            click.echo(df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(df, decimals=2, footer=footer)
        else:
            click.echo(df.to_csv(index=False))


def parse_filter_values(value_tuple):
    """Parse filter values with > or < operators, supporting ranges.

    Args:
        value_tuple: Tuple of strings like (">100",) or (">50", "<100")

    Returns:
        Tuple of (more_than, lower_than) where both can be set for ranges
    """
    if not value_tuple:
        return None, None

    more_than = None
    lower_than = None

    for value_str in value_tuple:
        if not value_str:
            continue

        value_str = value_str.strip()

        if value_str.startswith(">"):
            try:
                val = float(value_str[1:])
                if more_than is not None:
                    raise ValueError(
                        "Cannot specify multiple '>' values for the same parameter"
                    )
                more_than = val
            except ValueError as e:
                if "Cannot specify" in str(e):
                    raise
                raise ValueError(f"Invalid numeric value: {value_str[1:]}")
        elif value_str.startswith("<"):
            try:
                val = float(value_str[1:])
                if lower_than is not None:
                    raise ValueError(
                        "Cannot specify multiple '<' values for the same parameter"
                    )
                lower_than = val
            except ValueError as e:
                if "Cannot specify" in str(e):
                    raise
                raise ValueError(f"Invalid numeric value: {value_str[1:]}")
        else:
            # No operator, treat as exact value (not used in screening API)
            raise ValueError(
                f"Filter value must start with > or < operator: {value_str}"
            )

    return more_than, lower_than


def _screen_kwargs(
    *,
    market_cap,
    price,
    volume,
    beta,
    dividend,
    sectors,
    industries,
    exchange,
    country,
    is_etf,
    is_fund,
    is_actively_trading,
    source,
):
    """The screener's arguments, in FMP's names, from `ls`'s flags.

    Exchange and country codes are uppercased, the spelling both sources use, so
    `--exchange nyse` and `--country us` mean what they say.
    """
    market_cap_more, market_cap_lower = parse_filter_values(market_cap)
    price_more, price_lower = parse_filter_values(price)
    beta_more, beta_lower = parse_filter_values(beta)
    dividend_more, dividend_lower = parse_filter_values(dividend)

    # Volume needs to be parsed as int
    volume_more, volume_lower = None, None
    if volume:
        volume_more_float, volume_lower_float = parse_filter_values(volume)
        if volume_more_float is not None:
            volume_more = int(volume_more_float)
        if volume_lower_float is not None:
            volume_lower = int(volume_lower_float)

    return dict(
        marketCapMoreThan=market_cap_more,
        marketCapLowerThan=market_cap_lower,
        sector=sectors,
        industry=industries,
        betaMoreThan=beta_more,
        betaLowerThan=beta_lower,
        priceMoreThan=price_more,
        priceLowerThan=price_lower,
        dividendMoreThan=dividend_more,
        dividendLowerThan=dividend_lower,
        volumeMoreThan=volume_more,
        volumeLowerThan=volume_lower,
        exchange=exchange.strip().upper() if exchange else None,
        country=country.strip().upper() if country else None,
        isEtf=is_etf if is_etf else None,
        isFund=is_fund if is_fund else None,
        isActivelyTrading=is_actively_trading if is_actively_trading else None,
    )


def _known_classification(cfg, source, api_key, *, need_sectors, need_industries):
    """``(sector names, industry names)`` the source knows; a list not needed is
    left empty rather than fetched."""
    sectors: list = []
    industries: list = []
    if source == "db":
        if need_sectors:
            sectors = ds_db.sector_names(dsn=cfg.dsn)
        if need_industries:
            industries = ds_db.industry_names(dsn=cfg.dsn)
    else:
        if need_sectors:
            sectors = [r["sector"] for r in sector_list_api(api_key)]
        if need_industries:
            industries = [r["industry"] for r in industry_list_api(api_key)]
    return sectors, industries


def _norm(text):
    return " ".join(str(text).split()).lower()


def canonical_names(typed, known, label):
    """The names ``typed`` means, as ``known`` spells them, case-insensitively.

    ``typed`` is the comma-separated flag value. A name may itself contain a comma
    (FMP's "Furnishings, Fixtures & Appliances"), so the longest run of pieces
    that is a known name wins before a piece is taken alone. A piece matching
    nothing raises ValueError naming the closest known names: screening an unknown
    sector would answer "No data found", which reads as a fact about the market.
    """
    by_norm = {_norm(name): name for name in known}
    pieces = [p.strip() for p in str(typed).split(",") if p.strip()]
    out = []
    i = 0
    while i < len(pieces):
        for j in range(len(pieces), i, -1):
            hit = by_norm.get(_norm(", ".join(pieces[i:j])))
            if hit is not None:
                if hit not in out:
                    out.append(hit)
                i = j
                break
        else:
            close = difflib.get_close_matches(
                _norm(pieces[i]), list(by_norm), n=3, cutoff=0.6
            )
            listing = "sectors" if label == "sector" else "industries"
            hint = (
                "Did you mean: " + ", ".join(by_norm[c] for c in close) + "?"
                if close
                else f"List them with `duk ls --{listing}`."
            )
            raise ValueError(f"Unknown {label} '{pieces[i]}'. {hint}")
    return out


def _count_footer(df, what, *, include_delisted):
    """``11 sectors · 5,432 securities, actively trading · 12 unclassified``."""
    noun = (what, None) if what == "sector" else ("industry", "industries")
    parts = [tables.plural(len(df), *noun)]
    if "security_count" in df.columns and len(df):
        total = int(pd.to_numeric(df["security_count"]).sum())
        scope = "including delisted" if include_delisted else "actively trading"
        parts.append(f"{tables.plural(total, 'security', 'securities')}, {scope}")
    unclassified = df.attrs.get("unclassified")
    if unclassified:
        parts.append(f"{unclassified:,} with no {what}")
    parts.append("as of the last mart refresh")
    return " · ".join(parts)


def _screen_footer(df, *, limit):
    """``142 securities`` and, when ``-n`` cut the screen short, that it did."""
    text = tables.plural(len(df), "security", "securities")
    if limit and len(df) >= limit:
        text += f" (the first {limit:,}: -n limited the screen)"
    return text


def _ls_company_summary(
    ctx,
    *,
    query,
    quiet,
    limit,
    summary,
    output,
    output_table,
    output_csv,
    output_json,
    screening_flags,
):
    """`duk ls <ticker|company name>` -- everything the warehouse holds on one
    company.

    Split out of `ls` rather than inlined: `ls` is already long, and profile mode
    shares only the option list with list/screen mode, not the logic.
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "db")

    if any(screening_flags):
        click.echo(
            "Error: a QUERY selects one company; screening flags select many "
            "-- pick one.",
            err=True,
        )
        sys.exit(1)
    if summary:
        # In list mode --summary means "print the row count instead of the rows".
        # A profile IS the summary, so honouring the flag would mean printing "1".
        click.echo(
            "Error: --summary applies to list/screen mode; the company report is "
            "already a summary.",
            err=True,
        )
        sys.exit(1)
    try:
        asked = tables.requested_format(
            table=output_table,
            csv=output_csv,
            json=output_json,
            configured=ctx.obj["config"].console_format,
        )
    except tables.FormatError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if source != "db":
        # Deliberately not a fallback like `yc`'s: three of the four sections
        # describe what the WAREHOUSE holds, and the live API cannot answer them.
        # Silently answering a different question would be worse than an error.
        click.echo(
            "Error: 'ls QUERY' reads the warehouse; re-run with -S db.", err=True
        )
        sys.exit(1)
    if limit is not None:
        logger.debug("--limit is ignored in company-summary mode")
    require_source_credentials(ctx, source)

    # Validated here rather than inside the datasource so a typo'd identifier is a
    # parse error naming the scheme, not a lookup that reports the security missing.
    # The parsed form also decides how a miss and an ambiguity are worded: "a more
    # specific name" is useless advice to someone who typed a CIK.
    identifier = parse_security_argument(query)

    try:
        candidates = ds_db.resolve_company(dsn=cfg.dsn, query=query)
    except Exception as exc:
        logger.error(f"Company lookup failed: {exc}")
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not candidates:
        if identifier.is_ticker:
            click.echo(f"No company found matching '{query}'.", err=True)
        else:
            click.echo(f"No security found for {identifier.describe()}.", err=True)
        sys.exit(1)
    if len(candidates) > 1:
        # A did-you-mean, never a guess: picking the first of several would answer
        # confidently about the wrong company -- and for a CIK, which identifies an
        # ISSUER rather than an issue, several is the ordinary case, not a typo.
        if identifier.is_ticker:
            click.echo(
                f"'{query}' matches {len(candidates)} companies. "
                "Re-run with a ticker, or a more specific name:",
                err=True,
            )
        else:
            click.echo(
                f"{identifier.describe()} matches {len(candidates)} securities. "
                "Re-run with one of these tickers:",
                err=True,
            )
        click.echo(company_summary.render_candidates(candidates), err=True)
        sys.exit(1)

    matched = candidates[0]
    hint_reused_ticker(ctx, identifier)
    try:
        raw = ds_db.company_summary(dsn=cfg.dsn, security_id=matched["security_id"])
    except Exception as exc:
        logger.error(f"Failed to build company summary: {exc}")
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # The rename note is discovered during resolution, not by the summary query.
    raw["profile"]["matched_former_symbol"] = matched.get("matched_former_symbol")
    raw["profile"]["matched_former_valid_to"] = matched.get("matched_former_valid_to")

    report = company_summary.build_summary(raw)
    logger.info(f"Company summary for {report['meta'].get('symbol')}")

    # The sectioned report is the default in a terminal and a pipe alike, as it
    # always was: only --csv (or a configured csv) flattens it. On a terminal, or
    # with --table, it is drawn in a border; piped, it is the plain text it was.
    if asked == "json":
        payload = json.dumps(report, indent=2, default=str)
    elif asked == "csv":
        payload = pd.DataFrame([company_summary.render_flat(report)]).to_csv(
            index=False
        )
    elif asked == "table" or tables.is_terminal(sys.stdout):
        payload = company_summary.render_text(
            report, style=tables.style_for_stream(sys.stdout)
        )
    else:
        payload = company_summary.render_text(report)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload if payload.endswith("\n") else payload + "\n")
        logger.info(f"Data written to {output_path}")
        if not quiet:
            click.echo(f"Data written to {output_path}")

    if not quiet:
        click.echo(payload)


# ---------------------------------------------------------------------------
# Fundamentals: `duk fs` and `duk fa` (#107)
# ---------------------------------------------------------------------------

#: Decimal places `fa` shows by default: its factors are ratios, which two places
#: would flatten (an earnings yield of 0.0345 is not 0.03).
FACTOR_DECIMALS = 4


def _refuse_vendor_source(ctx, command, *, what="statements"):
    """`fs`, `fa`, `th`, `ca` and `tc` read the warehouse: no vendor backend, as
    `yc` has none on Sharadar."""
    source = ctx.obj.get("source", "db")
    if source != "db":
        click.echo(
            f"Error: {command} reads the warehouse's {what}; it has no "
            f"{'FMP' if source == 'fmp' else 'Sharadar'} backend (db mode only). "
            "Re-run with -S db.",
            err=True,
        )
        sys.exit(1)


def _fundamentals_security(ctx, symbol):
    """``(symbol, security_id)`` for `fs` / `fa`. An identifier or TICKER@date is
    resolved to one security here (#80); a ticker is left to the datasource's
    ladder, with the reused-ticker hint, and its security_id is None."""
    identifier = parse_security_argument(symbol)
    if not identifier.is_ticker:
        return resolve_security_identifier(ctx, identifier)
    hint_reused_ticker(ctx, identifier)
    return identifier.value, None


def _note_issuer_class(symbol, issuer):
    """Decision 5: say so when a secondary class is answered with its issuer's."""
    if issuer:
        click.echo(
            f"Note: {symbol} holds no statements of its own; SF1 files its "
            f"issuer's under {issuer['symbol']} (CIK {issuer['cik']}). Shown: "
            f"{issuer['symbol']}'s.",
            err=True,
        )


def _plain(value):
    """A warehouse value as `fs` / `fa` print it: dates as ISO text, decimals as
    floats (so --precision applies), the rest as is."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _emit(payload, *, output, default_name, quiet, logger, echo=True):
    """Write ``payload`` to ``output`` (a directory gets ``default_name``) and/or
    stdout, as `ls QUERY` does. ``echo`` False writes the file only: the console
    gets a table instead."""
    if output:
        output_path = Path(output)
        if output_path.is_dir():
            output_path = output_path / default_name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            payload if payload.endswith("\n") else payload + "\n", encoding="utf-8"
        )
        logger.info(f"Data written to {output_path}")
        if not quiet:
            click.echo(f"Data written to {output_path}")
    if not quiet and echo:
        click.echo(payload.rstrip("\n"))


#: Statement columns that say which period and version a row is, not an amount:
#: a table heads its period columns with them instead of listing them as items.
_STATEMENT_KEYS = ("fiscal_date", "fiscal_year", "fiscal_period", "period")


def _echo_statement_table(frame, *, symbol, statement, period, as_of, precision):
    """A statement as it is read: line items down, periods across, newest last.

    As many of the latest periods as fit the terminal are shown, and the footer
    says when some are not -- `-n`, or --csv, for the rest.
    """
    records = frame.to_dict(orient="records")
    label_key = next(
        (k for k in ("fiscal_date", "period_end") if k in frame.columns), None
    )
    labels = [
        str(r.get(label_key) or r.get("fiscal_year") or i)
        for i, r in enumerate(records)
    ]
    items = [c for c in frame.columns if c not in _STATEMENT_KEYS and c != label_key]
    width = tables.terminal_width(sys.stdout)
    shown = len(records)
    if width:
        label_width = max((len(str(item)) for item in items), default=4) + 4
        per_period = 18
        shown = max(1, min(len(records), (width - label_width) // per_period))
    first = len(records) - shown
    rows = []
    for item in items:
        row = {"item": item}
        for label, record in zip(labels[first:], records[first:]):
            row[label] = record.get(item)
        rows.append(row)
    columns = [tables.Column("item", header="ITEM", kind="text")]
    for label in labels[first:]:
        values = [r.get(label) for r in rows]
        kind = "number" if any(isinstance(v, (int, float)) for v in values) else "text"
        columns.append(
            tables.Column(label, header=label, kind=kind, decimals=precision)
        )
    footer = (
        f"{symbol} · {period} {statement} statement · as known on "
        f"{as_of.isoformat() if hasattr(as_of, 'isoformat') else as_of}"
    )
    if first:
        footer += (
            f" · the latest {shown} of {len(records)} periods (-n, or --csv, for all)"
        )
    style = tables.style_for_stream(sys.stdout)
    # Each cell is formatted by its row's own rule below, since one column holds a
    # date, a year, a version and amounts.
    body = []
    for row in rows:
        cells = [row["item"]]
        kind = tables.infer_kind(row["item"], [row.get(c.key) for c in columns[1:]])
        for column in columns[1:]:
            cell_column = tables.Column(
                column.key, kind=kind, decimals=precision if kind == "number" else None
            )
            cells.append(tables.format_cell(row.get(column.key), cell_column, style))
        body.append(cells)
    for column in columns[1:]:
        column.kind = "number"
    built = tables.Table(columns=columns, rows=body, footer=footer)
    click.echo(
        tables.render(built, style=style, max_width=width, color=tables.color_enabled())
    )


@main.command()
@click.argument("symbol")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress printing to stdout")
@click.option(
    "--statement",
    type=click.Choice(list(ds_db.STATEMENT_VIEWS), case_sensitive=False),
    default="income",
    show_default=True,
    help="Which statement",
)
@click.option(
    "--period",
    type=click.Choice(list(ds_db.STATEMENT_PERIODS), case_sensitive=False),
    default="annual",
    show_default=True,
    help="Fiscal years or fiscal quarters",
)
@click.option(
    "--as-of",
    "as_of",
    metavar="YYYY-MM-DD",
    help="As known on this date: each period at its version in force then, and "
    "none filed later (default: today)",
)
@click.option("-n", "--limit", type=int, help="The latest N periods (default: all)")
@click.option(
    "-p",
    "--precision",
    type=int,
    default=DEFAULT_DECIMALS,
    help=f"Decimal places for amounts (default: {DEFAULT_DECIMALS})",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write to a file. If path is a directory, the filename is "
    "<symbol>_<statement>_<period>.<ext>",
)
@click.pass_context
def fs(
    ctx,
    symbol,
    verbose,
    quiet,
    statement,
    period,
    as_of,
    limit,
    precision,
    output_table,
    output_csv,
    output_json,
    output,
):
    """
    Print a financial statement from the warehouse, as known on a date.

    SYMBOL: a ticker, an identifier (cik:51143, isin:..., cusip:...), or
    TICKER@YYYY-MM-DD for the security that traded under a reused ticker that
    day. --as-of is separate: it bounds what was known, not which security.

    One row per period, oldest first. Each period is its version in force on the
    --as-of date (default today): a period filed later is absent and a later
    restatement does not show, and known_from / knowledge_source say when each
    became known. Share counts are in the share terms of known_from (ADR 0019).
    A secondary share class answers with its issuer's primary class, through
    the shared CIK, and says so. The warehouse only (-S db).
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if verbose:
        enable_console_logging(logger)
    _refuse_vendor_source(ctx, "fs")
    console = output_format(ctx, output_table, output_csv, output_json)
    if limit is not None and limit < 1:
        click.echo("Error: --limit must be at least 1", err=True)
        sys.exit(1)
    require_source_credentials(ctx, "db")
    symbol, security_id = _fundamentals_security(ctx, symbol)
    statement, period = statement.lower(), period.lower()

    try:
        found = ds_db.statements(
            dsn=ctx.obj["config"].dsn,
            symbol=symbol,
            security_id=security_id,
            statement=statement,
            period=period,
            as_of=as_of,
            limit=limit,
        )
    except Exception as exc:
        logger.error(f"Failed to read statements: {exc}")
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if found["security_id"] is None:
        click.echo(f"No security found for {symbol}.", err=True)
        sys.exit(1)
    _note_issuer_class(symbol, found["statements_of"])
    if not found["rows"]:
        logger.warning(f"No {statement} statements for {symbol}")
        if not quiet:
            click.echo(
                f"No {period} {statement} statements for {symbol} as known on "
                f"{found['as_of'].isoformat()}"
            )
        sys.exit(0)
    logger.info(f"Retrieved {len(found['rows'])} {period} periods for {symbol}")

    frame = pd.DataFrame(
        [{k: _plain(v) for k, v in row.items()} for row in found["rows"]]
    )
    frame = apply_precision_to_dataframe(
        frame, precision, exclude_columns=["fiscal_year", "version_no"]
    )
    file_format = "json" if console == "json" else "csv"
    payload = (
        frame.to_json(orient="records")
        if file_format == "json"
        else frame.to_csv(index=False)
    )
    _emit(
        payload,
        output=output,
        default_name=f"{symbol}_{statement}_{period}.{file_format}",
        quiet=quiet,
        logger=logger,
        echo=console != "table",
    )
    if console == "table" and not quiet:
        _echo_statement_table(
            frame,
            symbol=(
                found["statements_of"]["symbol"] if found["statements_of"] else symbol
            ),
            statement=statement,
            period=period,
            as_of=found["as_of"],
            precision=precision,
        )


@main.command()
@click.argument("symbol")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress printing to stdout")
@click.option(
    "--as-of",
    "as_of",
    metavar="YYYY-MM-DD",
    help="The panel's month-end on or before this date, every input as known "
    "then (default: the latest snapshot)",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=FACTOR_DECIMALS,
    help=f"Decimal places (default: {FACTOR_DECIMALS})",
)
@table_option
@click.option(
    "--csv",
    "output_csv",
    is_flag=True,
    help="Output field,value CSV (the default when stdout is not a terminal)",
)
@click.option("--json", "output_json", is_flag=True, help="Output a JSON object")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write to a file. If path is a directory, the filename is "
    "<symbol>_factors.<ext>",
)
@click.pass_context
def fa(
    ctx,
    symbol,
    verbose,
    quiet,
    as_of,
    precision,
    output_table,
    output_csv,
    output_json,
    output,
):
    """
    Print one security's row of the factor panel from the warehouse.

    SYMBOL as for `duk fs`. Without --as-of, the latest snapshot
    (mart.security_fundamentals_latest, as of the last `fafnir db refresh-marts`);
    with it, the panel's month-end on or before that date (mart.fundamental_panel),
    every input as known then. One field per line: CSV `field,value`, or a JSON
    object. A ratio is empty where its denominator is missing, zero or negative
    (ADR 0019). A secondary share class answers with its issuer's primary class,
    and says so. The warehouse only (-S db).
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if verbose:
        enable_console_logging(logger)
    _refuse_vendor_source(ctx, "fa")
    console = output_format(ctx, output_table, output_csv, output_json)
    require_source_credentials(ctx, "db")
    symbol, security_id = _fundamentals_security(ctx, symbol)

    try:
        found = ds_db.factor_row(
            dsn=ctx.obj["config"].dsn,
            symbol=symbol,
            security_id=security_id,
            as_of=as_of,
        )
    except Exception as exc:
        logger.error(f"Failed to read the factor panel: {exc}")
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if found["security_id"] is None:
        click.echo(f"No security found for {symbol}.", err=True)
        sys.exit(1)
    _note_issuer_class(symbol, found["statements_of"])
    if found["row"] is None:
        when = found["as_of_date"]
        if when is None and as_of:
            message = (
                f"No factor row for {symbol}: the panel's first month-end is after "
                f"{as_of}"
            )
        elif when is None:
            message = f"No factor row for {symbol}: {found['source']} is empty"
        else:
            message = (
                f"No factor row for {symbol} at {when.isoformat()}: it did not "
                "trade in the week to that date"
            )
        logger.warning(message)
        if not quiet:
            click.echo(message)
        sys.exit(0)

    frame = pd.DataFrame([{k: _plain(v) for k, v in found["row"].items()}])
    frame = apply_precision_to_dataframe(
        frame, precision, exclude_columns=["security_id", "fiscal_year"]
    )
    record = {
        k: (None if isinstance(v, float) and v != v else v)
        for k, v in frame.to_dict(orient="records")[0].items()
    }
    file_format = "json" if console == "json" else "csv"
    if file_format == "json":
        payload = json.dumps(record)
    else:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["field", "value"])
        writer.writerows([k, "" if v is None else v] for k, v in record.items())
        payload = buffer.getvalue()
    _emit(
        payload,
        output=output,
        default_name=f"{symbol}_factors.{file_format}",
        quiet=quiet,
        logger=logger,
        echo=console != "table",
    )
    if console == "table" and not quiet:
        click.echo(
            tables.render_key_values(
                list(record.items()),
                title=f"{record.get('symbol') or symbol} factors",
                footer=f"{found['source']} · as of "
                f"{_plain(found['as_of_date']) or '-'}",
                decimals=precision,
                style=tables.style_for_stream(sys.stdout),
                max_width=tables.terminal_width(sys.stdout),
                color=tables.color_enabled(),
            )
        )


# ---------------------------------------------------------------------------
# `duk th`, `duk ca`, `duk tc`: identity, actions and sessions, as stored
# ---------------------------------------------------------------------------


def emit_records(
    rows,
    *,
    console,
    output,
    default_name,
    quiet,
    logger,
    exact=False,
    empty_message,
    table_rows=None,
    **table_kwargs,
):
    """Print ``rows`` (dicts of warehouse values) as a table, CSV or JSON, and
    write them to ``output`` (CSV, or JSON with --json) when asked.

    ``table_rows`` is a narrower view for the table, when every column would not
    fit a terminal; CSV and JSON always carry every column.

    JSON keeps types -- numbers, booleans, null -- or, with ``exact``, every
    number as its stored decimal text. CSV is the same values as text, a NUMERIC
    always in full.
    """
    if not rows:
        logger.warning(empty_message)
        if not quiet:
            click.echo(empty_message)
        return
    file_format = "json" if console == "json" else "csv"

    def as_text(fmt):
        if fmt == "json":
            return json.dumps(plain_records(rows, exact=exact))
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for record in plain_records(rows, exact=True):
            writer.writerow({k: "" if v is None else v for k, v in record.items()})
        return buffer.getvalue()

    if output:
        output_path = Path(output)
        if output_path.is_dir():
            output_path = output_path / f"{default_name}.{file_format}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(as_text(file_format), encoding="utf-8")
        logger.info(f"Data written to {output_path}")
        if not quiet:
            click.echo(f"Data written to {output_path}")
    if quiet:
        return
    if console == "table":
        echo_table(table_rows or rows, exact=exact, **table_kwargs)
    else:
        click.echo(as_text(console).rstrip("\n"))


def _split_ratio(row):
    """``2-for-1`` from a split's numerator and denominator, as stored."""
    num, den = row.get("split_numerator"), row.get("split_denominator")
    if num is None or den is None:
        return None

    def text(value):
        return f"{Decimal(value).normalize():f}"

    return f"{text(num)}-for-{text(den)}"


def _db_security(ctx, argument):
    """``(symbol, security_id)`` for a db command's SYMBOL: an identifier,
    ``sid:`` or ``TICKER@DATE`` resolved to one security here; a ticker left to
    the datasource's ladder (security_id None), with the reused-ticker hint."""
    identifier = parse_security_argument(argument)
    if not identifier.is_ticker:
        return resolve_security_identifier(ctx, identifier)
    hint_reused_ticker(ctx, identifier)
    return identifier.value, None


@main.command()
@click.argument("symbol")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress printing to stdout")
@click.option("-s", "--start-date", help="Keep periods that end on or after this date")
@click.option("-e", "--end-date", help="Keep periods that start on or before this date")
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write to a file. If path is a directory, the filename is "
    "<symbol>_history.<ext>",
)
@click.pass_context
def th(
    ctx,
    symbol,
    verbose,
    quiet,
    start_date,
    end_date,
    output_table,
    output_csv,
    output_json,
    output,
):
    """
    Ticker history: which security a ticker named, and when.

    SYMBOL is a ticker -- every security that has traded under it, each period
    from valid_from to valid_to inclusive (empty: still open) -- or a security
    (cik:, isin:, cusip:, sid:, TICKER@YYYY-MM-DD), for every ticker it has held:
    FB then META. Each row describes the security it names: its security_id,
    current primary ticker, name, asset type, venue, currency, country and
    identifiers. A period starting 1900-01-01 is fafnir's "first seen" fallback,
    not a listing date. --start-date / --end-date keep the periods overlapping
    the range. Nothing is resolved or picked: where two periods cover one day,
    both are listed. The warehouse only (-S db).
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if verbose:
        enable_console_logging(logger)
    _refuse_vendor_source(ctx, "th", what="ticker history")
    console = output_format(ctx, output_table, output_csv, output_json)
    require_source_credentials(ctx, "db")
    identifier = parse_security_argument(symbol)
    cfg = ctx.obj["config"]
    try:
        if identifier.is_ticker:
            rows = ds_db.ticker_history(
                dsn=cfg.dsn,
                symbol=identifier.value,
                start_date=start_date,
                end_date=end_date,
            )
            label = identifier.value
        else:
            candidates = ds_db.resolve_identifier(dsn=cfg.dsn, identifier=identifier)
            if not candidates:
                click.echo(f"No security found for {identifier.describe()}.", err=True)
                sys.exit(1)
            # A listing, not a pick: a CIK's several classes each get theirs.
            rows = ds_db.ticker_history(
                dsn=cfg.dsn,
                security_ids=[c["security_id"] for c in candidates],
                start_date=start_date,
                end_date=end_date,
            )
            label = identifier.describe()
    except ds_base.DataSourceError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Failed to read ticker history: {exc}")
        click.echo(f"Error: Failed to read ticker history: {exc}", err=True)
        sys.exit(1)

    named = len({r["security_id"] for r in rows})
    view = [
        {
            "symbol": r["symbol"],
            "security_id": r["security_id"],
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
            "is_primary": r["is_primary"],
            "trades_now_as": r["primary_symbol"],
            "company_name": r["company_name"],
            "asset_type": r["asset_type"],
            "exchange": r["exchange_code"],
            "status": "active" if r["is_actively_trading"] else "delisted",
        }
        for r in rows
    ]
    emit_records(
        rows,
        table_rows=view,
        console=console,
        output=output,
        default_name=f"{identifier.value}_history",
        quiet=quiet,
        logger=logger,
        empty_message=f"No ticker history for {label}",
        footer=f"{tables.plural(len(rows), 'period')} · {label} · "
        f"{tables.plural(named, 'security', 'securities')} · --json for every "
        "column, identifiers included",
    )


@main.command()
@click.argument("symbol")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress printing to stdout")
@click.option("-s", "--start-date", help="First ex-date (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="Last ex-date (YYYY-MM-DD)")
@click.option(
    "--type",
    "action_type",
    type=click.Choice(list(ds_db.ACTION_TYPES), case_sensitive=False),
    help="Splits or dividends only (default: both)",
)
@click.option(
    "--exact",
    is_flag=True,
    help="Print every number as the decimal text the warehouse stores (JSON "
    "strings), never through a float",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write to a file. If path is a directory, the filename is "
    "<symbol>_actions.<ext>",
)
@click.pass_context
def ca(
    ctx,
    symbol,
    verbose,
    quiet,
    start_date,
    end_date,
    action_type,
    exact,
    output_table,
    output_csv,
    output_json,
    output,
):
    """
    Corporate actions: one security's splits and cash dividends, by ex-date.

    SYMBOL as for `duk ph`: a ticker, an identifier, sid:, or TICKER@YYYY-MM-DD.
    One row per event from mart.v_corporate_action: the ex-date (entitlement),
    record and payment dates (the cash), the split ratio or the dividend per
    share, and the vendor that supplied it. Spinoffs, mergers and renames are not
    corporate actions here: a rename is `duk th`. --exact prints the stored
    NUMERIC text, as `ph --exact` does. The warehouse only (-S db).
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if verbose:
        enable_console_logging(logger)
    _refuse_vendor_source(ctx, "ca", what="corporate actions")
    console = output_format(ctx, output_table, output_csv, output_json)
    require_source_credentials(ctx, "db")
    symbol, security_id = _db_security(ctx, symbol)
    try:
        found = ds_db.corporate_actions(
            dsn=ctx.obj["config"].dsn,
            symbol=symbol,
            security_id=security_id,
            start_date=start_date,
            end_date=end_date,
            action_type=action_type.lower() if action_type else None,
        )
    except ds_base.DataSourceError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Failed to read corporate actions: {exc}")
        click.echo(f"Error: Failed to read corporate actions: {exc}", err=True)
        sys.exit(1)
    if found["security_id"] is None:
        click.echo(f"No security found for {symbol}.", err=True)
        sys.exit(1)
    rows = found["rows"]
    splits = sum(1 for r in rows if r["action_type"] == "split")
    view = [
        {
            "ex_date": r["ex_date"],
            "type": r["action_type"],
            "split": _split_ratio(r) if r["action_type"] == "split" else None,
            "dividend_amount": r["dividend_amount"],
            "currency": r["currency"] if r["action_type"] != "split" else None,
            "record_date": r["record_date"],
            "payment_date": r["payment_date"],
            "source": r["source"],
        }
        for r in rows
    ]
    emit_records(
        rows,
        table_rows=view,
        console=console,
        output=output,
        default_name=f"{symbol}_actions",
        quiet=quiet,
        logger=logger,
        exact=exact,
        empty_message=f"No corporate actions for {symbol}",
        footer=f"{symbol} · {tables.plural(splits, 'split')} · "
        f"{tables.plural(len(rows) - splits, 'dividend')}",
    )


@main.command()
@click.argument("exchange", required=False)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress printing to stdout")
@click.option("-s", "--start-date", help="First day (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="Last day (YYYY-MM-DD)")
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write to a file. If path is a directory, the filename is "
    "<exchange>_calendar.<ext>",
)
@click.pass_context
def tc(
    ctx,
    exchange,
    verbose,
    quiet,
    start_date,
    end_date,
    output_table,
    output_csv,
    output_json,
    output,
):
    """
    Trading calendar: a venue's sessions, or which venues have one.

    Without EXCHANGE, every venue the warehouse holds a calendar for, with its
    span and how many open and closed days it marks. With one (case-insensitive),
    its days from --start-date to --end-date: fafnir seeds open days only, so a
    weekday absent inside the span is closed, and a closure no rule predicts is a
    row with is_open false. Outside the span nothing is known. A venue with no
    calendar is reported, never answered with another's. The warehouse only.
    """
    logger = ctx.obj.get("logger", logging.getLogger("duk"))
    if verbose:
        enable_console_logging(logger)
    _refuse_vendor_source(ctx, "tc", what="trading calendar")
    console = output_format(ctx, output_table, output_csv, output_json)
    require_source_credentials(ctx, "db")
    cfg = ctx.obj["config"]
    try:
        if exchange:
            rows = ds_db.calendar_days(
                dsn=cfg.dsn,
                exchange=exchange,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            if start_date or end_date:
                raise click.UsageError(
                    "--start-date / --end-date select one venue's days; name it."
                )
            rows = ds_db.calendars(dsn=cfg.dsn)
    except ds_base.DataSourceError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except click.UsageError:
        raise
    except Exception as exc:
        logger.error(f"Failed to read the trading calendar: {exc}")
        click.echo(f"Error: Failed to read the trading calendar: {exc}", err=True)
        sys.exit(1)
    if exchange:
        venue = exchange.strip().upper()
        open_days = sum(1 for r in rows if r["is_open"])
        footer = (
            f"{venue} · {tables.plural(open_days, 'open day')} · "
            f"{tables.plural(len(rows) - open_days, 'closure')}"
        )
        empty = f"No trading calendar for {venue} in that range"
        name = f"{venue}_calendar"
    else:
        footer = tables.plural(len(rows), "calendar")
        empty = "No trading calendars in the warehouse"
        name = "calendars"
    emit_records(
        rows,
        console=console,
        output=output,
        default_name=name,
        quiet=quiet,
        logger=logger,
        empty_message=empty,
        footer=footer,
    )


@main.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing return data to stdout"
)
@click.option(
    "-i",
    "--input",
    "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Input file containing price data (CSV or JSON)",
)
@click.option(
    "-a",
    "--append",
    is_flag=True,
    help="Include input price data in the output",
)
@click.option(
    "--simple",
    is_flag=True,
    help="Compute arithmetic (simple) returns",
)
@click.option(
    "--log",
    "log_returns",
    is_flag=True,
    help="Compute log returns",
)
@click.option(
    "--diff",
    is_flag=True,
    help="Compute differenced prices",
)
@click.option(
    "--cum-simple",
    is_flag=True,
    help="Compute cumulative simple returns",
)
@click.option(
    "--cum-log",
    is_flag=True,
    help="Compute cumulative log returns",
)
@click.option(
    "--annual-simple",
    is_flag=True,
    help="Compute annualized simple returns (infers annual multiplier from date index)",
)
@click.option(
    "--annual-log",
    is_flag=True,
    help="Compute annualized log returns (infers annual multiplier from date index)",
)
@click.option(
    "-l",
    "--lookback",
    type=int,
    default=1,
    help=(
        "Number of periods to lookback for computing multi-period returns "
        "(default: 1)"
    ),
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=3,
    help="Decimal precision for output values (default: 3)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def rc(
    ctx,
    verbose,
    quiet,
    input_file,
    append,
    simple,
    log_returns,
    diff,
    cum_simple,
    cum_log,
    annual_simple,
    annual_log,
    lookback,
    output_table,
    output_csv,
    output_json,
    summary,
    precision,
    output,
):
    """
    Compute returns from price data.

    Reads price data from a file and computes various types of returns
    including simple returns, log returns, differenced prices, cumulative returns,
    and annualized returns.
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info(f"Computing returns from {input_file}")

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Check that at least one return calculation option is specified
    return_options = [
        simple,
        log_returns,
        diff,
        cum_simple,
        cum_log,
        annual_simple,
        annual_log,
    ]
    if not any(return_options):
        logger.error("At least one return calculation option must be specified")
        click.echo(
            "Error: At least one return calculation option must be specified "
            "(--simple, --log, --diff, --cum-simple, --cum-log, "
            "--annual-simple, --annual-log)",
            err=True,
        )
        sys.exit(1)

    # Read input file
    try:
        input_path = Path(input_file)
        if input_path.suffix.lower() == ".json":
            df = pd.read_json(input_file)
        else:
            # Default to CSV
            df = pd.read_csv(input_file)

        logger.info(f"Read {len(df)} records from {input_file}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        click.echo(f"Error: Failed to read input file: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("Input file contains no data")
        click.echo("Error: Input file contains no data", err=True)
        sys.exit(1)

    # Identify date column, whatever its case
    date_col = next((col for col in df.columns if str(col).lower() == "date"), None)

    if date_col is None:
        logger.error("No date column found in input data")
        click.echo(
            "Error: Input data must contain a 'date' column",
            err=True,
        )
        sys.exit(1)

    # Convert date column to datetime and set as index
    try:
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col)
        df = df.sort_index()
        logger.debug(f"Set '{date_col}' as index and sorted by date")
    except Exception as e:
        logger.error(f"Failed to process date column: {e}")
        click.echo(f"Error: Failed to process date column: {e}", err=True)
        sys.exit(1)

    # Extract price columns (numeric columns only)
    price_columns = df.select_dtypes(include=["number"]).columns.tolist()
    if not price_columns:
        logger.error("No numeric price columns found in input data")
        click.echo(
            "Error: Input data must contain at least one numeric price column",
            err=True,
        )
        sys.exit(1)

    logger.info(f"Found price columns: {price_columns}")
    prices = df[price_columns]

    # Infer periods_per_year for annualized returns if needed
    periods_per_year = None
    if annual_simple or annual_log:
        # Infer from date index
        try:
            date_index = prices.index
            if len(date_index) >= 2:
                # Calculate average time difference
                time_diffs = date_index[1:] - date_index[:-1]
                avg_diff = time_diffs.mean()
                days_diff = avg_diff.days

                # Map to periods_per_year
                if days_diff <= 1.5:  # Daily
                    periods_per_year = 252
                elif days_diff <= 5:  # Weekly
                    periods_per_year = 52
                elif days_diff <= 20:  # Monthly
                    periods_per_year = 12
                elif days_diff <= 70:  # Quarterly
                    periods_per_year = 4
                elif days_diff <= 150:  # Semi-annual
                    periods_per_year = 2
                else:  # Annual
                    periods_per_year = 1

                logger.info(
                    f"Inferred periods_per_year={periods_per_year} from date index "
                    f"(avg days between observations: {days_diff})"
                )
            else:
                logger.error("Need at least 2 observations to infer periods_per_year")
                click.echo(
                    "Error: Need at least 2 observations to infer periods_per_year",
                    err=True,
                )
                sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to infer periods_per_year: {e}")
            click.echo(f"Error: Failed to infer periods_per_year: {e}", err=True)
            sys.exit(1)

    # Compute returns
    result_df = pd.DataFrame(index=prices.index)

    # Add original prices if append flag is set
    if append:
        for col in price_columns:
            result_df[col] = prices[col]

    try:
        # Simple returns
        if simple:
            simple_ret = simple_return(prices, periods=lookback)
            for col in price_columns:
                suffix = f"_simple_ret_l{lookback}" if lookback != 1 else "_simple_ret"
                result_df[f"{col}{suffix}"] = simple_ret[col]
            logger.info(f"Computed simple returns with lookback={lookback}")

        # Log returns
        if log_returns:
            log_ret = log_return(prices, periods=lookback)
            for col in price_columns:
                suffix = f"_log_ret_l{lookback}" if lookback != 1 else "_log_ret"
                result_df[f"{col}{suffix}"] = log_ret[col]
            logger.info(f"Computed log returns with lookback={lookback}")

        # Price differences
        if diff:
            price_diff = price_difference(prices, periods=lookback)
            for col in price_columns:
                suffix = f"_diff_l{lookback}" if lookback != 1 else "_diff"
                result_df[f"{col}{suffix}"] = price_diff[col]
            logger.info(f"Computed price differences with lookback={lookback}")

        # Cumulative simple returns
        if cum_simple:
            # First compute simple returns, then cumulative
            simple_ret = simple_return(prices, periods=lookback)
            cum_simple_ret = cumulative_simple_return(simple_ret)
            for col in price_columns:
                suffix = f"_cum_simple_l{lookback}" if lookback != 1 else "_cum_simple"
                result_df[f"{col}{suffix}"] = cum_simple_ret[col]
            logger.info(f"Computed cumulative simple returns with lookback={lookback}")

        # Cumulative log returns
        if cum_log:
            # First compute log returns, then cumulative
            log_ret = log_return(prices, periods=lookback)
            cum_log_ret = cumulative_log_return(log_ret)
            for col in price_columns:
                suffix = f"_cum_log_l{lookback}" if lookback != 1 else "_cum_log"
                result_df[f"{col}{suffix}"] = cum_log_ret[col]
            logger.info(f"Computed cumulative log returns with lookback={lookback}")

        # Annualized simple returns
        if annual_simple:
            # First compute simple returns, then annualize
            simple_ret = simple_return(prices, periods=lookback)
            annual_ret = annualized_return(
                simple_ret, periods_per_year=periods_per_year, return_type="simple"
            )
            for col in price_columns:
                suffix = (
                    f"_annual_simple_l{lookback}" if lookback != 1 else "_annual_simple"
                )
                result_df[f"{col}{suffix}"] = annual_ret[col]
            logger.info(
                f"Computed annualized simple returns with lookback={lookback}, "
                f"periods_per_year={periods_per_year}"
            )

        # Annualized log returns
        if annual_log:
            # First compute log returns, then annualize
            log_ret = log_return(prices, periods=lookback)
            annual_ret = annualized_return(
                log_ret, periods_per_year=periods_per_year, return_type="log"
            )
            for col in price_columns:
                suffix = f"_annual_log_l{lookback}" if lookback != 1 else "_annual_log"
                result_df[f"{col}{suffix}"] = annual_ret[col]
            logger.info(
                f"Computed annualized log returns with lookback={lookback}, "
                f"periods_per_year={periods_per_year}"
            )

    except Exception as e:
        logger.error(f"Failed to compute returns: {e}")
        click.echo(f"Error: Failed to compute returns: {e}", err=True)
        sys.exit(1)

    logger.info(f"Computed returns for {len(result_df)} records")

    # Prepare output - reset index to include date as a column
    output_df = result_df.reset_index()

    # Apply precision to numeric columns
    output_df = apply_precision_to_dataframe(output_df, precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        try:
            if file_format == "json":
                output_df.to_json(output_path, orient="records", date_format="iso")
            else:
                output_df.to_csv(output_path, index=False)

            logger.info(f"Data written to {output_path}")

            if not quiet:
                click.echo(f"Data written to {output_path}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            click.echo(f"Error: Failed to write output file: {e}", err=True)
            sys.exit(1)

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(output_df)
            if console == "table":
                echo_summary_table(stats, title="returns summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(output_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(
                output_df,
                decimals=precision,
                footer=f"{tables.plural(len(output_df), 'row')} · returns · "
                f"{Path(input_file).name}",
            )
        else:
            click.echo(output_df.to_csv(index=False))


@main.group()
@click.pass_context
def ti(ctx):
    """
    Technical indicators commands.

    Compute technical indicators like moving averages on financial data.
    Use 'duk ti <command> --help' for information on specific indicator commands.
    """
    pass


@ti.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing indicator data to stdout"
)
@click.option(
    "-i",
    "--input",
    "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Input file containing price data (CSV or JSON)",
)
@click.option(
    "-c",
    "--column",
    required=True,
    help="Column name to calculate SMA on (e.g., 'close', 'high', 'low')",
)
@click.option(
    "-w",
    "--window",
    type=int,
    required=True,
    help="Window size for SMA calculation (number of periods)",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=3,
    help="Decimal precision for output values (default: 3)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def sma(
    ctx,
    verbose,
    quiet,
    input_file,
    column,
    window,
    output_table,
    output_csv,
    output_json,
    summary,
    precision,
    output,
):
    """
    Calculate Simple Moving Average (SMA) on input data.

    Computes the arithmetic mean of data points over a specified window period.
    The SMA is the average of the last N periods, where N is the window size.

    Example:
        duk ti sma --input prices.csv --column close --window 20 --output sma_result.csv
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info(f"Computing SMA from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Read input file
    try:
        input_path = Path(input_file)
        if input_path.suffix.lower() == ".json":
            df = pd.read_json(input_file)
        else:
            # Default to CSV
            df = pd.read_csv(input_file)

        logger.info(f"Read {len(df)} records from {input_file}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        click.echo(f"Error: Failed to read input file: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("Input file contains no data")
        click.echo("Error: Input file contains no data", err=True)
        sys.exit(1)

    # Validate column exists: `-c Close` finds a `close` column
    column = match_column(df, column)
    if column not in df.columns:
        logger.error(f"Column '{column}' not found in input data")
        click.echo(
            f"Error: Column '{column}' not found in input data. "
            f"Available columns: {list(df.columns)}",
            err=True,
        )
        sys.exit(1)

    # Validate column is numeric
    if not pd.api.types.is_numeric_dtype(df[column]):
        logger.error(f"Column '{column}' is not numeric")
        click.echo(f"Error: Column '{column}' must be numeric", err=True)
        sys.exit(1)

    # Check if we have enough data points
    if len(df) < window:
        msg = (
            f"Input data has {len(df)} records, but window size is {window}. "
            f"SMA will be NaN for all records."
        )
        logger.warning(msg)
        click.echo(f"Warning: {msg}", err=True)

    # Calculate SMA
    try:
        result_df = calculate_sma(df, column=column, window=window)
        logger.info(f"Calculated SMA with window={window} on column '{column}'")
    except Exception as e:
        logger.error(f"Failed to calculate SMA: {e}")
        click.echo(f"Error: Failed to calculate SMA: {e}", err=True)
        sys.exit(1)

    # Apply precision to numeric columns
    result_df = apply_precision_to_dataframe(result_df, precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        try:
            if file_format == "json":
                result_df.to_json(output_path, orient="records", date_format="iso")
            else:
                result_df.to_csv(output_path, index=False)

            logger.info(f"Data written to {output_path}")

            if not quiet:
                click.echo(f"Data written to {output_path}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            click.echo(f"Error: Failed to write output file: {e}", err=True)
            sys.exit(1)

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(result_df)
            if console == "table":
                echo_summary_table(stats, title="SMA summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(result_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(
                result_df,
                decimals=precision,
                footer=f"{tables.plural(len(result_df), 'row')} · SMA · "
                f"{Path(input_file).name}",
            )
        else:
            click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing indicator data to stdout"
)
@click.option(
    "-i",
    "--input",
    "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Input file containing price data (CSV or JSON)",
)
@click.option(
    "-c",
    "--column",
    required=True,
    help="Column name to calculate EMA on (e.g., 'close', 'high', 'low')",
)
@click.option(
    "-w",
    "--window",
    type=int,
    required=True,
    help="Window size for EMA calculation (number of periods)",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=3,
    help="Decimal precision for output values (default: 3)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def ema(
    ctx,
    verbose,
    quiet,
    input_file,
    column,
    window,
    output_table,
    output_csv,
    output_json,
    summary,
    precision,
    output,
):
    """
    Calculate Exponential Moving Average (EMA) on input data.

    Computes a weighted moving average that gives more weight to recent data points.
    The EMA is more responsive to recent price changes compared to SMA.

    Example:
        duk ti ema --input prices.csv --column close --window 20 --output ema_result.csv
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info(f"Computing EMA from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Read input file
    try:
        input_path = Path(input_file)
        if input_path.suffix.lower() == ".json":
            df = pd.read_json(input_file)
        else:
            # Default to CSV
            df = pd.read_csv(input_file)

        logger.info(f"Read {len(df)} records from {input_file}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        click.echo(f"Error: Failed to read input file: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("Input file contains no data")
        click.echo("Error: Input file contains no data", err=True)
        sys.exit(1)

    # Validate column exists: `-c Close` finds a `close` column
    column = match_column(df, column)
    if column not in df.columns:
        logger.error(f"Column '{column}' not found in input data")
        click.echo(
            f"Error: Column '{column}' not found in input data. "
            f"Available columns: {list(df.columns)}",
            err=True,
        )
        sys.exit(1)

    # Validate column is numeric
    if not pd.api.types.is_numeric_dtype(df[column]):
        logger.error(f"Column '{column}' is not numeric")
        click.echo(f"Error: Column '{column}' must be numeric", err=True)
        sys.exit(1)

    # Check if we have enough data points
    if len(df) < window:
        msg = (
            f"Input data has {len(df)} records, but window size is {window}. "
            f"EMA will be NaN for all records."
        )
        logger.warning(msg)
        click.echo(f"Warning: {msg}", err=True)

    # Calculate EMA
    try:
        result_df = calculate_ema(df, column=column, window=window)
        logger.info(f"Calculated EMA with window={window} on column '{column}'")
    except Exception as e:
        logger.error(f"Failed to calculate EMA: {e}")
        click.echo(f"Error: Failed to calculate EMA: {e}", err=True)
        sys.exit(1)

    # Apply precision to numeric columns
    result_df = apply_precision_to_dataframe(result_df, precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        try:
            if file_format == "json":
                result_df.to_json(output_path, orient="records", date_format="iso")
            else:
                result_df.to_csv(output_path, index=False)

            logger.info(f"Data written to {output_path}")

            if not quiet:
                click.echo(f"Data written to {output_path}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            click.echo(f"Error: Failed to write output file: {e}", err=True)
            sys.exit(1)

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(result_df)
            if console == "table":
                echo_summary_table(stats, title="EMA summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(result_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(
                result_df,
                decimals=precision,
                footer=f"{tables.plural(len(result_df), 'row')} · EMA · "
                f"{Path(input_file).name}",
            )
        else:
            click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing indicator data to stdout"
)
@click.option(
    "-i",
    "--input",
    "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Input file containing price data (CSV or JSON)",
)
@click.option(
    "-c",
    "--column",
    required=True,
    help="Column name to calculate RSI on (e.g., 'close', 'high', 'low')",
)
@click.option(
    "-w",
    "--window",
    type=int,
    default=14,
    help="Window size for RSI calculation (number of periods, default: 14)",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=3,
    help="Decimal precision for output values (default: 3)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def rsi(
    ctx,
    verbose,
    quiet,
    input_file,
    column,
    window,
    output_table,
    output_csv,
    output_json,
    summary,
    precision,
    output,
):
    """
    Calculate Relative Strength Index (RSI) on input data.

    The RSI is a momentum oscillator that measures the speed and magnitude
    of recent price changes to evaluate overbought or oversold conditions.
    RSI values range from 0 to 100, with values above 70 typically indicating
    overbought conditions and values below 30 indicating oversold conditions.

    The RSI uses Wilder's smoothing method (exponential moving average) to
    calculate average gains and losses over the specified window period.

    Example:
        duk ti rsi --input prices.csv --column close --window 14 --output rsi_result.csv
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info(f"Computing RSI from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Read input file
    try:
        input_path = Path(input_file)
        if input_path.suffix.lower() == ".json":
            df = pd.read_json(input_file)
        else:
            # Default to CSV
            df = pd.read_csv(input_file)

        logger.info(f"Read {len(df)} records from {input_file}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        click.echo(f"Error: Failed to read input file: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("Input file contains no data")
        click.echo("Error: Input file contains no data", err=True)
        sys.exit(1)

    # Validate column exists: `-c Close` finds a `close` column
    column = match_column(df, column)
    if column not in df.columns:
        logger.error(f"Column '{column}' not found in input data")
        click.echo(
            f"Error: Column '{column}' not found in input data. "
            f"Available columns: {list(df.columns)}",
            err=True,
        )
        sys.exit(1)

    # Validate column is numeric
    if not pd.api.types.is_numeric_dtype(df[column]):
        logger.error(f"Column '{column}' is not numeric")
        click.echo(f"Error: Column '{column}' must be numeric", err=True)
        sys.exit(1)

    # Check if we have enough data points
    # RSI needs window+1 records (window periods + 1 for price difference)
    if len(df) < window + 1:
        msg = (
            f"Input data has {len(df)} records, but window size is {window}. "
            f"RSI requires at least {window + 1} records. "
            f"RSI will be NaN for all records."
        )
        logger.warning(msg)
        click.echo(f"Warning: {msg}", err=True)

    # Calculate RSI
    try:
        result_df = calculate_rsi(df, column=column, window=window)
        logger.info(f"Calculated RSI with window={window} on column '{column}'")
    except Exception as e:
        logger.error(f"Failed to calculate RSI: {e}")
        click.echo(f"Error: Failed to calculate RSI: {e}", err=True)
        sys.exit(1)

    # Apply precision to numeric columns
    result_df = apply_precision_to_dataframe(result_df, precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        try:
            if file_format == "json":
                result_df.to_json(output_path, orient="records", date_format="iso")
            else:
                result_df.to_csv(output_path, index=False)

            logger.info(f"Data written to {output_path}")

            if not quiet:
                click.echo(f"Data written to {output_path}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            click.echo(f"Error: Failed to write output file: {e}", err=True)
            sys.exit(1)

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(result_df)
            if console == "table":
                echo_summary_table(stats, title="RSI summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(result_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(
                result_df,
                decimals=precision,
                footer=f"{tables.plural(len(result_df), 'row')} · RSI · "
                f"{Path(input_file).name}",
            )
        else:
            click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Print log output to the console (default: log file only)",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing indicator data to stdout"
)
@click.option(
    "-i",
    "--input",
    "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Input file containing price data (CSV or JSON)",
)
@click.option(
    "-c",
    "--column",
    required=True,
    help="Column name to calculate MACD on (e.g., 'close', 'high', 'low')",
)
@click.option(
    "--fast-window",
    type=int,
    default=12,
    help="Fast EMA window size (number of periods, default: 12)",
)
@click.option(
    "--slow-window",
    type=int,
    default=26,
    help="Slow EMA window size (number of periods, default: 26)",
)
@click.option(
    "--signal-window",
    type=int,
    default=9,
    help="Signal line EMA window size (number of periods, default: 9)",
)
@table_option
@click.option("--csv", "output_csv", is_flag=True, help=CSV_HELP)
@click.option("--json", "output_json", is_flag=True, help="Output data as JSON")
@click.option(
    "--summary",
    is_flag=True,
    help="Print summary statistics instead of data observations",
)
@click.option(
    "-p",
    "--precision",
    type=int,
    default=3,
    help="Decimal precision for output values (default: 3)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Write data to file",
)
@click.pass_context
def macd(
    ctx,
    verbose,
    quiet,
    input_file,
    column,
    fast_window,
    slow_window,
    signal_window,
    output_table,
    output_csv,
    output_json,
    summary,
    precision,
    output,
):
    """
    Calculate Moving Average Convergence Divergence (MACD) on input data.

    The MACD is a trend-following momentum indicator that shows the relationship
    between two exponential moving averages (EMAs). It consists of:
    - MACD Line: Difference between fast EMA and slow EMA
    - Signal Line: EMA of the MACD line
    - Histogram: Difference between MACD line and signal line

    The MACD is commonly used to identify trend changes and momentum. When MACD
    crosses above the signal line, it's a bullish signal. When it crosses below,
    it's a bearish signal.

    Example:
        duk ti macd --input prices.csv --column close --output macd_result.csv
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        enable_console_logging(logger)

    logger.info(f"Computing MACD from {input_file}")

    # Validate windows
    if fast_window <= 0:
        logger.error("Fast window must be greater than 0")
        click.echo("Error: Fast window must be greater than 0", err=True)
        sys.exit(1)
    if slow_window <= 0:
        logger.error("Slow window must be greater than 0")
        click.echo("Error: Slow window must be greater than 0", err=True)
        sys.exit(1)
    if signal_window <= 0:
        logger.error("Signal window must be greater than 0")
        click.echo("Error: Signal window must be greater than 0", err=True)
        sys.exit(1)
    if fast_window >= slow_window:
        logger.error("Fast window must be less than slow window")
        click.echo("Error: Fast window must be less than slow window", err=True)
        sys.exit(1)

    # Determine output format: the console's, and the -o file's (never a table).
    console = output_format(ctx, output_table, output_csv, output_json)
    file_format = "json" if console == "json" else "csv"

    # Read input file
    try:
        input_path = Path(input_file)
        if input_path.suffix.lower() == ".json":
            df = pd.read_json(input_file)
        else:
            # Default to CSV
            df = pd.read_csv(input_file)

        logger.info(f"Read {len(df)} records from {input_file}")
    except Exception as e:
        logger.error(f"Failed to read input file: {e}")
        click.echo(f"Error: Failed to read input file: {e}", err=True)
        sys.exit(1)

    if df.empty:
        logger.warning("Input file contains no data")
        click.echo("Error: Input file contains no data", err=True)
        sys.exit(1)

    # Validate column exists: `-c Close` finds a `close` column
    column = match_column(df, column)
    if column not in df.columns:
        logger.error(f"Column '{column}' not found in input data")
        click.echo(
            f"Error: Column '{column}' not found in input data. "
            f"Available columns: {list(df.columns)}",
            err=True,
        )
        sys.exit(1)

    # Validate column is numeric
    if not pd.api.types.is_numeric_dtype(df[column]):
        logger.error(f"Column '{column}' is not numeric")
        click.echo(f"Error: Column '{column}' must be numeric", err=True)
        sys.exit(1)

    # Check if we have enough data points
    # MACD needs slow_window + signal_window records for full calculation
    min_required = slow_window + signal_window
    if len(df) < min_required:
        msg = (
            f"Input data has {len(df)} records, but MACD with "
            f"slow_window={slow_window} and signal_window={signal_window} "
            f"requires at least {min_required} records. "
            f"MACD values will be NaN for early records."
        )
        logger.warning(msg)
        click.echo(f"Warning: {msg}", err=True)

    # Calculate MACD
    try:
        result_df = calculate_macd(
            df,
            column=column,
            fast_window=fast_window,
            slow_window=slow_window,
            signal_window=signal_window,
        )
        logger.info(
            f"Calculated MACD with fast_window={fast_window}, "
            f"slow_window={slow_window}, signal_window={signal_window} "
            f"on column '{column}'"
        )
    except Exception as e:
        logger.error(f"Failed to calculate MACD: {e}")
        click.echo(f"Error: Failed to calculate MACD: {e}", err=True)
        sys.exit(1)

    # Apply precision to numeric columns
    result_df = apply_precision_to_dataframe(result_df, precision)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        try:
            if file_format == "json":
                result_df.to_json(output_path, orient="records", date_format="iso")
            else:
                result_df.to_csv(output_path, index=False)

            logger.info(f"Data written to {output_path}")

            if not quiet:
                click.echo(f"Data written to {output_path}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            click.echo(f"Error: Failed to write output file: {e}", err=True)
            sys.exit(1)

    # Print to stdout unless quiet flag is set
    if not quiet:
        # Print summary statistics if requested
        if summary:
            stats = compute_summary_stats(result_df)
            if console == "table":
                echo_summary_table(stats, title="MACD summary statistics")
            else:
                click.echo(format_summary_stats(stats))
        # Otherwise print data
        elif console == "json":
            click.echo(result_df.to_json(orient="records", date_format="iso"))
        elif console == "table":
            echo_table(
                result_df,
                decimals=precision,
                footer=f"{tables.plural(len(result_df), 'row')} · MACD · "
                f"{Path(input_file).name}",
            )
        else:
            click.echo(result_df.to_csv(index=False))


if __name__ == "__main__":
    main(obj={})

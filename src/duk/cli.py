"""
CLI entry point for duk.

This module provides the main command-line interface using Click.
"""

import logging
import sys
from pathlib import Path

import click
import pandas as pd

from duk import __version__
from duk.config import get_config
from duk.datasource import base as ds_base
from duk.datasource import db as ds_db
from duk.datasource import live as ds_live
from duk.fmp_api import (
    actively_trading_list_api,
    get_yield_curve,
    industry_list_api,
    sector_list_api,
)
from duk.indicators import calculate_ema, calculate_macd, calculate_rsi, calculate_sma
from duk.logging_config import setup_logging
from duk.ls_utils import process_industries, process_sectors
from duk.return_utils import (
    annualized_return,
    cumulative_log_return,
    cumulative_simple_return,
    log_return,
    price_difference,
    simple_return,
)
from duk.stats import compute_summary_stats, format_summary_stats

# Key rate tenors for yield curve analysis
KEY_RATE_TENORS = ["year1", "year5", "year10", "year20", "year30"]


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
    type=click.Choice(["live", "db"], case_sensitive=False),
    default=None,
    help="Data source: 'db' (fafnir warehouse) or 'live' (FMP API). "
    "Defaults to [general].default_source (db when a DSN is set, else live).",
)
@click.pass_context
def main(ctx, config, source):
    """
    duk - A CLI tool for downloading markets and financial data.

    Reads from the fafnir PostgreSQL warehouse (db mode) or the FMP API directly
    (live mode). Use 'duk <subprogram> --help' for information on specific subprograms.
    """
    # Ensure that ctx.obj exists and is a dict
    ctx.ensure_object(dict)

    # Load configuration
    ctx.obj["config"] = get_config(config)

    # Setup logging
    cfg = ctx.obj["config"]
    logger = setup_logging(
        log_level=cfg.log_level,
        log_dir=cfg.log_dir,
        console_output=True,
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
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing price history data to stdout"
)
@click.option("-s", "--start-date", help="Start date (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="End date (YYYY-MM-DD)")
@click.option("-n", "--limit", type=int, help="Limit number of records to return")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
    output_csv,
    output_json,
    summary,
    output,
):
    """
    Request price history for a symbol.

    SYMBOL: The ticker symbol (e.g., AAPL, MSFT). Case insensitive.
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    # Make symbol uppercase for consistency
    symbol = symbol.upper()

    logger.info(f"Requesting price history for {symbol}")

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "live")
    api_key = cfg.fmp_key

    if source == "live" and not api_key:
        logger.error("FMP API key not configured")
        click.echo(
            "Error: FMP API key not configured. Set FMP_API_KEY environment variable "
            "or add fmp_key to [api] section in ~/.dukrc",
            err=True,
        )
        sys.exit(1)

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Handle output to file
    if output:
        output_path = Path(output)

        # If output is a directory, format filename
        if output_path.is_dir():
            start_str = start_date or "earliest"
            end_str = end_date or "latest"
            ext = "json" if output_format == "json" else "csv"
            filename = f"{symbol}_{start_str}_{end_str}.{ext}"
            output_path = output_path / filename

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(output_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(output_df.to_csv(index=False))


@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing yield curve data to stdout"
)
@click.option("-s", "--start-date", help="Start date (YYYY-MM-DD)")
@click.option("-e", "--end-date", help="End date (YYYY-MM-DD)")
@click.option("-n", "--limit", type=int, help="Limit number of records to return")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    logger.info("Requesting yield curve data")

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "live")
    api_key = cfg.fmp_key

    if source == "live" and not api_key:
        logger.error("FMP API key not configured")
        click.echo(
            "Error: FMP API key not configured. Set FMP_API_KEY environment variable "
            "or add fmp_key to [api] section in ~/.dukrc",
            err=True,
        )
        sys.exit(1)

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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
            tenor_parts = [t.strip() for t in tenors.split(",")]
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
        logger.warning("yc has no db backend yet; falling back to live FMP source")
        if not api_key:
            click.echo(
                "Error: yc requires the FMP API (live). Set FMP_API_KEY or use "
                "--source live.",
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
            ext = "json" if output_format == "json" else "csv"
            filename = f"yc_{start_str}_{end_str}.{ext}"
            output_path = output_path / filename

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(output_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(output_df.to_csv(index=False))


@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress printing list data to stdout"
)
@click.option("-n", "--limit", type=int, help="Limit number of records to return")
@click.option(
    "--sectors",
    "sectors_list_flag",
    is_flag=True,
    help="List all market sectors",
)
@click.option(
    "--industries",
    "industries_list_flag",
    is_flag=True,
    help="List all industries",
)
@click.option(
    "--sector",
    "sectors_filter",
    help="Screen by sectors (comma-separated values)",
)
@click.option(
    "--industry",
    "industries_filter",
    help="Screen by industries (comma-separated values)",
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
    help="Filter by exchange (e.g., NASDAQ, NYSE)",
)
@click.option(
    "--country",
    help="Filter by country code (e.g., US)",
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
    help="Filter for actively trading securities only",
)
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
    output_csv,
    output_json,
    summary,
    output,
):
    """
    List company and market information.

    By default, returns actively trading securities with symbol and name.
    Use --sectors to list market sectors or --sector="Tech,Healthcare" to screen.
    Use --industries to list industries or --industry="Software,Banking" to screen.

    Screening supports comparison operators:
    - Use > for greater than (e.g., --price=">50")
    - Use < for less than (e.g., --market-cap="<1000000000")
    """
    # Get logger from context
    logger = ctx.obj.get("logger", logging.getLogger("duk"))

    # Adjust logging based on verbose flag
    if verbose:
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    # Resolve data source and credentials
    cfg = ctx.obj["config"]
    source = ctx.obj.get("source", "live")
    api_key = cfg.fmp_key

    if source == "live" and not api_key:
        logger.error("FMP API key not configured")
        click.echo(
            "Error: FMP API key not configured. Set FMP_API_KEY environment variable "
            "or add fmp_key to [api] section in ~/.dukrc",
            err=True,
        )
        sys.exit(1)

    # Helper function to parse comparison operators
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

    # Determine if we're doing screening or listing
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
    ]
    has_screening_params = any(param for param in screening_params)

    # Parse sectors and industries
    sectors_list = None
    industries_list = None

    if sectors_filter:
        # Sectors with values - use for screening
        sectors_list = [s.strip() for s in sectors_filter.split(",")]
    if industries_filter:
        # Industries with values - use for screening
        industries_list = [i.strip() for i in industries_filter.split(",")]

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

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

    # Decide between listing and screening
    use_screening = sectors_list or industries_list or has_screening_params

    try:
        if use_screening:
            logger.info("Using security screening")

            # Parse filter parameters
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

            # Call the screener on the active source
            screen_kwargs = dict(
                marketCapMoreThan=market_cap_more,
                marketCapLowerThan=market_cap_lower,
                sector=sectors_list,
                industry=industries_list,
                betaMoreThan=beta_more,
                betaLowerThan=beta_lower,
                priceMoreThan=price_more,
                priceLowerThan=price_lower,
                dividendMoreThan=dividend_more,
                dividendLowerThan=dividend_lower,
                volumeMoreThan=volume_more,
                volumeLowerThan=volume_lower,
                exchange=exchange,
                country=country,
                isEtf=is_etf if is_etf else None,
                isFund=is_fund if is_fund else None,
                isActivelyTrading=is_actively_trading if is_actively_trading else None,
                limit=limit,
            )
            if source == "db":
                df = ds_db.screen(dsn=cfg.dsn, **screen_kwargs)
            else:
                df = ds_live.screen(api_key=api_key, **screen_kwargs)

            if df.empty:
                logger.warning("No screening results returned")
                if not quiet:
                    click.echo("No data found")
                sys.exit(0)

            logger.info(f"Retrieved {len(df)} screening results")

            # Reset index if it was set to symbol
            if df.index.name == "symbol":
                df = df.reset_index()

        elif source == "db":
            # Traditional listing mode, served from the fafnir warehouse.
            if sectors_list_flag:
                logger.info("Requesting sector list (db)")
                df = ds_db.list_sectors(dsn=cfg.dsn)
            elif industries_list_flag:
                logger.info("Requesting industry list (db)")
                df = ds_db.list_industries(dsn=cfg.dsn)
            else:
                logger.info("Requesting actively trading list (db)")
                df = ds_db.list_actively_trading(dsn=cfg.dsn, limit=limit)

            if df.empty:
                logger.warning("No data returned")
                if not quiet:
                    click.echo("No data found")
                sys.exit(0)

            # Apply limit if specified
            if limit is not None and limit > 0:
                df = df.head(limit)
                logger.debug(f"Limiting to {limit} records")

        else:
            # Traditional listing mode (live FMP)
            if sectors_list_flag:
                logger.info("Requesting sector list")
                data = sector_list_api(api_key)
            elif industries_list_flag:
                logger.info("Requesting industry list")
                data = industry_list_api(api_key)
            else:
                logger.info("Requesting actively trading list")
                data = actively_trading_list_api(api_key)

            if not data:
                logger.warning("No data returned")
                if not quiet:
                    click.echo("No data found")
                sys.exit(0)

            logger.info(f"Retrieved {len(data)} records")

            # Process data based on list type
            if sectors_list_flag:
                df = process_sectors(data)
            elif industries_list_flag:
                df = process_industries(data)
            else:
                # Convert to DataFrame for actively trading list
                df = pd.DataFrame(data)
                # Filter to expected columns
                expected_cols = ["symbol", "name"]
                available_cols = [col for col in expected_cols if col in df.columns]
                if available_cols:
                    df = df[available_cols]

            # Apply limit if specified
            if limit is not None and limit > 0:
                df = df.head(limit)
                logger.debug(f"Limiting to {limit} records")

    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to fetch list data: {e}")
        click.echo(f"Error: Failed to fetch list data: {e}", err=True)
        sys.exit(1)

    # Handle output to file
    if output:
        output_path = Path(output)

        # Ensure parent directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to file based on format
        if output_format == "json":
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
        else:
            if output_format == "json":
                click.echo(df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(df.to_csv(index=False))


@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    logger.info(f"Computing returns from {input_file}")

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Identify date column
    date_col = None
    for col in ["date", "Date", "DATE"]:
        if col in df.columns:
            date_col = col
            break

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
            if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(output_df.to_json(orient="records", date_format="iso"))
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
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    logger.info(f"Computing SMA from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Validate column exists
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
        logger.warning(
            f"Input data has {len(df)} records, but window size is {window}. "
            f"SMA will be NaN for all records."
        )

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
            if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(result_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    logger.info(f"Computing EMA from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Validate column exists
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
        logger.warning(
            f"Input data has {len(df)} records, but window size is {window}. "
            f"EMA will be NaN for all records."
        )

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
            if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(result_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

    logger.info(f"Computing RSI from {input_file}")

    # Validate window
    if window <= 0:
        logger.error("Window must be greater than 0")
        click.echo("Error: Window must be greater than 0", err=True)
        sys.exit(1)

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Validate column exists
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
        logger.warning(
            f"Input data has {len(df)} records, but window size is {window}. "
            f"RSI requires at least {window + 1} records. "
            f"RSI will be NaN for all records."
        )

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
            if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(result_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(result_df.to_csv(index=False))


@ti.command()
@click.option("-v", "--verbose", is_flag=True, help="Print all logging to stdout")
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
@click.option("--csv", "output_csv", is_flag=True, help="Output data as CSV (default)")
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
        logger.setLevel(logging.INFO)
        # Enable console output for all handlers
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.DEBUG)

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

    # Determine output format
    if output_csv and output_json:
        logger.error("Only one output format can be specified")
        click.echo(
            "Error: Only one of --csv or --json can be specified",
            err=True,
        )
        sys.exit(1)

    # Default to CSV if neither is specified
    output_format = "json" if output_json else "csv"

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

    # Validate column exists
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
        logger.warning(
            f"Input data has {len(df)} records, but MACD with "
            f"slow_window={slow_window} and signal_window={signal_window} "
            f"requires at least {min_required} records. "
            f"MACD values will be NaN for early records."
        )

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
            if output_format == "json":
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
            output_text = format_summary_stats(stats)
            click.echo(output_text)
        # Otherwise print data
        else:
            if output_format == "json":
                click.echo(result_df.to_json(orient="records", date_format="iso"))
            else:
                click.echo(result_df.to_csv(index=False))


if __name__ == "__main__":
    main(obj={})

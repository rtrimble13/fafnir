"""
Logging configuration for duk.

Records go to the log file. The console is opt-in: the CLI attaches a console
handler only when ``-v/--verbose`` is passed, so an ordinary invocation prints
nothing but its own output (data on stdout, click.echo messages on stderr).
"""

import logging
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def setup_logging(
    log_level: str = "info",
    log_dir: str = "var/duk/log",
    log_file: str = "duk.log",
    console_output: bool = True,
) -> logging.Logger:
    """
    Set up logging for the application.

    Args:
        log_level: Logging level (debug, info, warning, error, critical)
        log_dir: Directory for log files
        log_file: Name of the log file
        console_output: Whether to attach a console handler up front. The duk CLI
            passes False and calls :func:`enable_console_logging` instead, so log
            records reach the terminal only under --verbose.

    Returns:
        Configured logger instance
    """
    # Convert log level string to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Create logger
    logger = logging.getLogger("duk")
    logger.setLevel(numeric_level)

    # Remove existing handlers to avoid duplicates
    logger.handlers = []

    formatter = _formatter()

    # Set up file handler
    if log_dir:
        # Create log directory if it doesn't exist
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # Create file handler
        file_handler = logging.FileHandler(log_path / log_file)
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Set up console handler
    if console_output:
        enable_console_logging(logger, level=numeric_level)

    return logger


def enable_console_logging(
    logger: logging.Logger, level: Optional[int] = None
) -> logging.Handler:
    """Send this logger's records to the console as well as the log file.

    Called for ``-v/--verbose``. Without it duk stays quiet on the terminal: errors
    still reach the user through the ``click.echo(..., err=True)`` calls that pair
    with every logged failure, and the full record stays in the log file.

    Args:
        logger: Logger to attach the handler to.
        level: Threshold for the console handler. Defaults to the logger's own
            level, so ``log_level = debug`` in ~/.dukrc gives a debug console
            without ``--verbose`` having to widen (or narrow) what is captured.

    Returns:
        The handler that was attached.
    """
    handler = logging.StreamHandler()
    handler.setLevel(logger.level if level is None else level)
    handler.setFormatter(_formatter())
    logger.addHandler(handler)
    return handler


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get a logger instance.

    Args:
        name: Logger name. If None, returns the root duk logger

    Returns:
        Logger instance
    """
    if name is None:
        return logging.getLogger("duk")
    return logging.getLogger(f"duk.{name}")

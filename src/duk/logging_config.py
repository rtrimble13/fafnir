"""
Logging configuration for duk.

This module sets up logging to both file and stdout based on configuration.
"""

import logging
from pathlib import Path
from typing import Optional


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
        console_output: Whether to output logs to console (stdout)

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

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
        console_handler = logging.StreamHandler()
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


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

"""
Logging configuration for fafnir.

Structured-ish logging to both a rotating file and the console. Levels follow the
operating brief: ERROR for failed loads needing attention, WARNING for
quarantined rows and gaps, INFO for completed loads. The API key and full
payloads are never logged.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_level: str = "info",
    log_dir: str = "var/fafnir/log",
    log_file: str = "fafnir.log",
    console_output: bool = True,
) -> logging.Logger:
    """Configure and return the root ``fafnir`` logger."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger("fafnir")
    logger.setLevel(numeric_level)
    logger.handlers = []  # avoid duplicate handlers on re-init

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path / log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    if console_output:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the ``fafnir`` namespace."""
    if name is None:
        return logging.getLogger("fafnir")
    return logging.getLogger(f"fafnir.{name}")

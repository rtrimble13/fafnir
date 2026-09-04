"""
Logging configuration for fafnir.

Structured-ish logging to both a rotating file and the console. Levels follow the
operating brief: ERROR for failed loads needing attention, WARNING for
quarantined rows and gaps, INFO for completed loads. The API key and full
payloads are never logged.
"""

from __future__ import annotations

import getpass
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class LogDirectoryError(RuntimeError):
    """The log directory could not be created or written.

    Its own type so ``cli.main`` can turn it into a ``ClickException`` -- one
    actionable line -- instead of letting a ``PermissionError`` traceback out of
    ``mkdir``. Every fafnir command configures logging before it does anything
    else, so this failure blocks *all* of them, including read-only ones like
    ``fafnir status``. A stack trace through click and pathlib is a poor way to
    learn that a config value is relative.
    """


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
        log_path = _prepare_log_dir(log_dir)
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


def _prepare_log_dir(log_dir: str) -> Path:
    """Create the log directory, or explain precisely why it could not be.

    The diagnostic names the **resolved absolute path** and, when the configured
    value was relative, the working directory it resolved against. That pairing is
    the whole point: the default ``var/fafnir/log`` is relative, so the same
    command writes to a different place from every directory and fails outright
    from one the user cannot write. The reported path is then not the string in
    the config file, and an operator comparing the two is the fastest route to
    understanding what happened.
    """
    raw = Path(log_dir)
    resolved = Path(os.path.abspath(raw))

    def _explain(problem: str) -> LogDirectoryError:
        lines = [
            f"cannot use the log directory: {problem}",
            f"  configured log_dir : {log_dir!r}",
            f"  resolves to        : {resolved}",
        ]
        if not raw.is_absolute():
            lines.append(f"  relative to cwd    : {Path.cwd()}")
        try:
            who = getpass.getuser()
        except Exception:  # pragma: no cover - getuser needs a resolvable uid
            who = f"uid {os.getuid()}"
        lines += [
            f"  running as         : {who}",
            "",
            "Set an absolute path. Either in the config file:",
            "  [general]",
            '  log_dir = "/var/log/fafnir"',
            "or in the environment, which is what the systemd units use:",
            "  FAFNIR_LOG_DIR=/var/log/fafnir",
            "",
            "See doc/install_hetzner.md section 4.3.",
        ]
        return LogDirectoryError("\n".join(lines))

    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _explain(f"{exc.__class__.__name__}: {exc.strerror or exc}") from exc

    # mkdir succeeding does not mean the log file can be written: exist_ok=True
    # accepts a directory that already exists and is owned by someone else, which
    # is the common case when a service user inherits a path an operator created.
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise _explain("the directory exists but is not writable")

    return resolved


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the ``fafnir`` namespace."""
    if name is None:
        return logging.getLogger("fafnir")
    return logging.getLogger(f"fafnir.{name}")

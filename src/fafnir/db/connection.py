"""
Thin connection / query helpers over psycopg 3.

Every query is parameterized -- string-concatenated SQL is never used. The
:class:`Database` wrapper exposes a small surface (execute, fetch, executemany,
copy-based bulk upsert) so loaders stay declarative and consistent.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from fafnir.config import DSN_ENV_VAR, PASSWORD_ENV_VARS
from fafnir.logging_config import get_logger

logger = get_logger("db")


class DatabaseConnectionError(RuntimeError):
    """Could not connect, phrased as something to act on.

    libpq's own text is accurate and unhelpful in the same sentence:
    ``fe_sendauth: no password supplied`` names a symptom whose cause is a `host`
    value three files away, and it arrives as a psycopg traceback because every
    call site does ``with Database(cfg.dsn)``. This carries the DSN that was
    actually used (password redacted), where it came from, and the specific fix
    for the failure mode -- the same content install_hetzner.md 4.4 already has in
    a table, delivered at the moment it is needed.

    ``cli._FafnirGroup.invoke`` turns it into a ``ClickException``, so one catch
    covers every subcommand.

    The message is rendered from parts rather than baked at raise time so the CLI
    -- which holds the :class:`~fafnir.config.FafnirConfig` and therefore knows
    which file the DSN really came from, including under ``-c`` -- can set
    :attr:`source` before it is shown. Down here only the environment is visible,
    which is a guess.
    """

    def __init__(self, dsn: str, error: str, hints: list[str], source: str):
        self.dsn = dsn
        self.error = error
        self.hints = list(hints)
        self.source = source
        super().__init__(dsn, error, hints, source)

    def __str__(self) -> str:
        lines = [
            "cannot connect to the fafnir database.",
            f"  dsn   : {redact_dsn(self.dsn)}",
            f"  from  : {self.source}",
            f"  error : {self.error}",
        ]
        if self.hints:
            lines.append("")
            lines.extend(self.hints)
        return "\n".join(lines)


def redact_dsn(dsn: str) -> str:
    """Strip the password from a DSN so it is safe to print.

    Both libpq spellings, because both reach here: ``password=`` in a keyword DSN
    and ``://user:secret@host`` in a URL. A diagnostic that leaks a credential
    into a terminal, a log file or a paste into a bug report is a worse bug than
    the one it is describing.
    """
    redacted = re.sub(
        r"(?i)\bpassword\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", "password=***", dsn
    )
    redacted = re.sub(r"(?i)(://[^:/@\s]+):([^@/\s]+)@", r"\1:***@", redacted)
    return redacted


def _diagnose(dsn: str, message: str) -> list[str]:
    """Failure-specific advice, keyed off libpq's message."""
    low = message.lower()
    hints: list[str] = []

    # host= that is not a path is a TCP connection, and TCP means auth.
    host_match = re.search(r"(?i)\bhost\s*=\s*(\S+)", dsn)
    host = host_match.group(1).strip("'\"") if host_match else ""
    tcp = bool(host) and not host.startswith("/")

    if "no password supplied" in low:
        hints.append(
            "libpq wants a password, which means this is a TCP connection to a "
            "host using password auth."
        )
        if tcp:
            hints.append(
                f"  host={host} is a TCP host. The install guide (3.6) connects "
                "over the Unix socket instead, where peer auth applies and no "
                'password is needed: set host = "/var/run/postgresql".'
            )
        if os.environ.get(DSN_ENV_VAR, "").strip():
            hints.append(
                f"  {DSN_ENV_VAR} is set and is used VERBATIM, so "
                f"{PASSWORD_ENV_VARS[0]} is ignored. Supply the password with "
                f"{PASSWORD_ENV_VARS[1]}, a ~/.pgpass (0600), or password= in the "
                "DSN itself -- see the table in install_hetzner.md 4.4."
            )
        else:
            hints.append(
                f"  Or supply a password: {PASSWORD_ENV_VARS[0]}, "
                f"{PASSWORD_ENV_VARS[1]}, or [database].password."
            )
    elif "peer authentication failed" in low:
        hints.append(
            "Peer auth maps the OS user to the database role. Either you are not "
            "running as the user the pg_ident map names, or the map's rule sits "
            "BELOW the generic 'local all all peer' line in pg_hba.conf and never "
            "matches. Order matters -- install_hetzner.md 3.6."
        )
    elif "no such file or directory" in low and "socket" in low:
        hints.append(
            "No socket at that path. Check the cluster is running "
            "(systemctl status postgresql) and that its unix_socket_directories "
            "matches host= -- Debian/Ubuntu use /var/run/postgresql."
        )
    elif "connection refused" in low:
        hints.append(
            "Nothing is listening there. Check the cluster is running and that "
            "the port matches (install_hetzner.md 3.7 binds it to localhost)."
        )
    elif "does not exist" in low and "database" in low:
        hints.append(
            "The database is missing. scripts/setup_db.sh creates it, or see "
            "install_hetzner.md 3.5."
        )
    elif "does not exist" in low and "role" in low:
        hints.append(
            "The role is missing. The three functional roles are created in "
            "install_hetzner.md 3.5, as a superuser."
        )
    return hints


def _connection_error(dsn: str, exc: Exception) -> DatabaseConnectionError:
    message = str(exc).strip()
    first = message.splitlines()[0] if message else repr(exc)
    return DatabaseConnectionError(
        dsn=dsn,
        error=first,
        hints=_diagnose(dsn, message),
        source=_dsn_source_hint(),
    )


def _dsn_source_hint() -> str:
    """Provenance, without importing the config object the caller may not have.

    Only the environment can be inspected from here; when it is unset the honest
    answer names the file the DSN would have come from instead.
    """
    if os.environ.get(DSN_ENV_VAR, "").strip():
        return f"the {DSN_ENV_VAR} environment variable"
    return "~/.fafnirrc [database] (no {} in the environment)".format(DSN_ENV_VAR)


def _log_server_message(diag: psycopg.errors.Diagnostic) -> None:
    """
    Route a PostgreSQL server message into the fafnir log.

    psycopg discards notices unless a handler is registered, which would hide
    anything a migration or function reports with RAISE. Severity decides the
    level: WARNING and above are surfaced by default (e.g. migration 0001
    reporting that it could not set the role comments), while routine NOTICE
    chatter -- "schema ... already exists, skipping" on every idempotent re-run --
    stays at debug so ordinary CLI output remains clean.
    """
    severity = (diag.severity_nonlocalized or diag.severity or "NOTICE").upper()
    message = diag.message_primary or ""
    if diag.message_detail:
        message = f"{message} ({diag.message_detail})"
    if severity in ("WARNING", "ERROR", "FATAL", "PANIC", "EXCEPTION"):
        logger.warning("postgres: %s", message)
    else:
        logger.debug("postgres[%s]: %s", severity.lower(), message)


def connect(dsn: str, autocommit: bool = False) -> psycopg.Connection:
    """Open a psycopg connection. Caller owns the lifecycle.

    The single choke point every call site reaches, which is why the connection
    diagnostic is applied here rather than at each ``with Database(...)``.
    """
    try:
        conn = psycopg.connect(dsn, autocommit=autocommit)
    except psycopg.OperationalError as exc:
        raise _connection_error(dsn, exc) from exc
    conn.add_notice_handler(_log_server_message)
    return conn


class Database:
    """A small wrapper around a psycopg connection with parameterized helpers."""

    def __init__(self, dsn: str, autocommit: bool = False):
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None
        self._autocommit = autocommit

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "Database":
        self._conn = connect(self.dsn, autocommit=self._autocommit)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            if exc_type is None and not self._autocommit:
                self._conn.commit()
            elif not self._autocommit:
                self._conn.rollback()
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise RuntimeError("Database used outside of a context manager")
        return self._conn

    def commit(self) -> None:
        """Commit work so far, starting a fresh transaction.

        Long loaders call this at a natural unit boundary -- one symbol's landing
        payload, bars, watermark and DQ flags -- so a failure costs that unit
        rather than the whole run. Without it a multi-hour backfill that raises
        at the last symbol discards every watermark and re-spends the bandwidth,
        which is precisely the resumability ``initial_backfill.sh`` advertises.

        No-op under autocommit, where every statement is already durable.
        """
        if self._conn is not None and not self._autocommit:
            self._conn.commit()

    def rollback(self) -> None:
        """Discard the uncommitted tail, keeping everything already committed.

        Used to clear an aborted transaction so a final bookkeeping write (the
        run's failed status) can still go through.
        """
        if self._conn is not None and not self._autocommit:
            self._conn.rollback()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Explicit transaction block; commits on success, rolls back on error."""
        with self.conn.transaction():
            yield self.conn

    # -- queries ------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Execute a statement; return affected row count."""
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self.conn.cursor() as cur:
            cur.executemany(sql, list(rows))
            return cur.rowcount

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> Optional[dict]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetchval(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    def execute_script(self, sql_text: str) -> None:
        """Execute a multi-statement SQL script (migrations/seeds)."""
        with self.conn.cursor() as cur:
            cur.execute(sql_text)

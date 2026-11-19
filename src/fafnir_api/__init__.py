"""
The fafnir HTTP API: the warehouse's reads for a client with no database
connection, authenticated by an API key (ADR 0021).

``duk`` reaches the warehouse two ways. With a libpq DSN it runs
``duk.datasource.db`` against PostgreSQL itself: on the host, or from a laptop
through the SSH tunnel of ADR 0008. With an API address it asks this service to
run the same function on the host and send back what it returned. The service is
that and nothing more: it holds no SQL and no market logic of its own, serves
exactly the reads ``duk.datasource.db`` marks as served, and accepts no query
language.

:mod:`fafnir_api.keys`
    The keys: how one is made, what is stored (a SHA-256, never the key), and how
    a presented key is checked.
:mod:`fafnir_api.app`
    The WSGI application. Standard library only, so it is testable with no server
    and servable by any WSGI server.
:mod:`fafnir_api.cli`
    ``fafnir-api``: serve it (waitress, the ``api`` extra) or ``--check`` the role
    it would serve as.

It listens on loopback and speaks plain HTTP. TLS, and the public port, belong to
the reverse proxy in front of it (doc/install_hetzner.md §14).
"""

from __future__ import annotations

from fafnir import __version__  # one repository, one version -- see ADR 0011

__all__ = ["__version__"]

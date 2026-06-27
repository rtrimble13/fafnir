"""
duk data-source seam.

Two interchangeable backends produce the *same* DataFrame contracts the CLI and
the pure compute modules (indicators, returns, stats) already expect:

  * live -- FMP API (the original duk behaviour)
  * db   -- the fafnir PostgreSQL warehouse (mart schema)

``resolve_source`` picks the backend; ``price_history`` / ``screen`` dispatch.
"""

from duk.datasource.base import DataSourceError, resolve_source

__all__ = ["DataSourceError", "resolve_source"]

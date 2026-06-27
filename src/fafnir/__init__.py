"""
fafnir - a research-grade financial market data warehouse on PostgreSQL.

This package provides the ingestion, data-quality, and administration tooling
that populates and maintains the fafnir database. Downstream read access is via
the ``duk`` CLI (db mode), direct SQL against the ``mart`` schema, or an MCP
server (fast-follow).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

"""
fafnir - a research-grade financial market data warehouse on PostgreSQL.

This package provides the ingestion, data-quality, and administration tooling
that populates and maintains the fafnir database. Downstream read access is via
the ``duk`` CLI (db mode), direct SQL against the ``mart`` schema, or an MCP
server (fast-follow).
"""

#: The version of everything in this repository -- the canonical one, and the only
#: place the number is written. ``fafnir_mcp`` and ``duk`` re-export it, and
#: pyproject.toml reads it through ``[tool.setuptools.dynamic]``, so a release
#: changes exactly this line. `scripts/release.sh` is what changes it; ADR 0011
#: says why the number lives in source rather than being derived from the git tag.
#:
#: Keep it a plain string literal. setuptools reads this attribute *statically*
#: (by parsing, not importing) when it can, and anything computed here would force
#: an import of this package at build time.
__version__ = "0.1.0"

__all__ = ["__version__"]

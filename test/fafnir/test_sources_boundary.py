"""
The vendor layer may not import the warehouse (ADR 0017).

``fafnir.sources`` is the one place a vendor is spoken to: every request, every
endpoint constant, every file parser. It is what ``duk -S <vendor>``, the loaders
and the probes share, and what a notebook or a machine without the warehouse
could import with ``requests`` alone. That holds only while nothing in it reaches
for the database or a loader, so the imports are checked here rather than trusted:
the three modules that had crossed the line (the FMP probe, the ledger-aware file
readers, the vendor-gaps report) moved to ``fafnir.ingest`` with that ADR.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import fafnir.sources as sources_pkg

SOURCES_DIR = Path(sources_pkg.__file__).parent

#: Top-level packages a vendor module may import, besides the standard library.
ALLOWED_THIRD_PARTY = {"requests"}

#: fafnir modules a vendor module may import. Logging is configuration-free
#: plumbing; everything else in fafnir is the warehouse.
ALLOWED_FAFNIR = {"fafnir.logging_config", "fafnir.sources"}

MODULES = sorted(p for p in SOURCES_DIR.glob("*.py"))


def _imports(path: Path) -> list[tuple[str, int]]:
    """Every module name a file imports, top-level or nested, with its line."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


def _allowed(name: str) -> bool:
    top = name.split(".")[0]
    if top in sys.stdlib_module_names:
        return True
    if top in ALLOWED_THIRD_PARTY:
        return True
    if top == "fafnir":
        return any(name == a or name.startswith(a + ".") for a in ALLOWED_FAFNIR)
    return False


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_a_vendor_module_imports_nothing_from_the_warehouse(path):
    offending = [
        f"{path.name}:{line}: {name}"
        for name, line in _imports(path)
        if not _allowed(name)
    ]
    assert not offending, (
        "fafnir.sources may import only the standard library, requests, "
        "fafnir.logging_config and itself (ADR 0017). Move what needs the warehouse "
        "to fafnir.ingest:\n  " + "\n  ".join(offending)
    )


def test_the_layer_is_not_empty():
    # Guards the glob: a moved directory would otherwise pass vacuously.
    names = {p.stem for p in MODULES}
    assert {"base", "fmp", "sharadar", "sharadar_files"} <= names


def test_duk_sharadar_requests_through_the_shared_client():
    """duk keeps no Sharadar HTTP code of its own (ADR 0017 §2)."""
    import duk.sharadar_api as mod

    names = {name for name, _ in _imports(Path(mod.__file__))}
    assert "requests" not in names
    assert "fafnir.sources.sharadar" in names

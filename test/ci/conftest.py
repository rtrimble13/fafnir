"""Load the stdlib-only CI scripts under ``scripts/`` as modules.

They are not part of the ``fafnir`` package -- CI runs them with a bare Python
before anything is installed -- so they are imported by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def docs_touched():
    return _load("check_docs_touched")


@pytest.fixture(scope="session")
def doc_links():
    return _load("check_doc_links")

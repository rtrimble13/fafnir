"""One repository, one version.

Four places used to carry a version number and they had already drifted: `duk` said
1.1.0 while everything else said 0.1.0, and `doc/install_hetzner.md` pinned both as
the expected output of `--version`. Nothing noticed, because nothing looked.

These tests are what looks. `src/fafnir/__init__.py` is the only place the number is
written; `fafnir_mcp` and `duk` re-export it and `pyproject.toml` reads it through
`[tool.setuptools.dynamic]`. Each of those links is asserted here, so a future
edit that reintroduces a literal fails rather than drifts. See ADR 0011.
"""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
from pathlib import Path

import pytest

import duk
import fafnir
import fafnir_mcp

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "src" / "fafnir" / "__init__.py"

#: Releases are three numbers. The `v` belongs to the git tag, not to the version:
#: PEP 440 metadata carries no prefix, so `fafnir.__version__` is "0.2.0" and the
#: tag naming that commit is "v0.2.0".
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_is_three_numbers():
    assert SEMVER.match(fafnir.__version__), (
        f"{fafnir.__version__!r} is not X.Y.Z. scripts/release.sh enforces this on "
        "the way in; something has edited the constant by hand."
    )


def test_every_package_reports_the_same_version():
    """The re-exports are the mechanism; this is the assertion they exist for."""
    assert fafnir_mcp.__version__ == fafnir.__version__
    assert duk.__version__ == fafnir.__version__


@pytest.mark.needs_fresh_install
def test_the_distribution_metadata_matches_the_source():
    """A fresh install must pick the version up from the source constant.

    This is the link `dynamic = ["version"]` provides, and the only test that
    actually proves it: everything else here reads the source tree, which would
    look identical if the pyproject wiring were broken.

    It is marked, because it reads what *pip* recorded, not what the tree says.
    Those agree from one `pip install -e .` until the next edit of the constant --
    so a release legitimately breaks it for the length of one commit, and
    `scripts/release.sh` deselects the marker for that reason. Seeing it fail
    during ordinary work means the editable install is behind the tree: reinstall
    with `pip install -e '.[dev]'`.

    That staleness is not incidental. It is the same property that ruled out
    deriving the version from the git tag (ADR 0011): anything pip writes at
    install time describes whenever pip last ran, which on a `git pull` deployment
    is not the code that is running. `fafnir --version` reads the source constant
    and stays correct; `pip show fafnir` is the one that can lag.
    """
    assert importlib.metadata.version("fafnir") == fafnir.__version__


def test_the_number_is_written_in_exactly_one_place():
    """A second literal anywhere in src/ is drift waiting to happen.

    Matched on the assignment, not on the number, so this keeps holding after a
    release moves it.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"""^__version__\s*=\s*["']""", line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line}")

    assert offenders == [
        f"src/fafnir/__init__.py:"
        f'{_line_of_assignment()}: __version__ = "{fafnir.__version__}"'
    ], (
        "__version__ must be assigned a literal in src/fafnir/__init__.py and "
        "nowhere else; every other package re-exports it.\nFound:\n  "
        + "\n  ".join(offenders)
    )


def _line_of_assignment() -> int:
    for lineno, line in enumerate(VERSION_FILE.read_text().splitlines(), 1):
        if line.startswith("__version__ ="):
            return lineno
    raise AssertionError(f"no __version__ assignment in {VERSION_FILE}")


def test_setuptools_can_read_the_constant_without_importing_it():
    """pyproject reads this attribute statically, so it has to stay a plain literal.

    A computed value (an f-string, a call, a read of a file) still *works*, by
    making setuptools import the package at build time instead -- which turns a
    build into an execution of the code being built. Pin the literal so that
    silent downgrade cannot happen.
    """
    import ast

    tree = ast.parse(VERSION_FILE.read_text())
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    assert len(assigned) == 1, "expected exactly one __version__ assignment"
    assert isinstance(assigned[0], ast.Constant) and isinstance(
        assigned[0].value, str
    ), "__version__ must be a plain string literal, not a computed value"


def test_pyproject_takes_its_version_from_the_constant():
    """The wiring itself, so removing it fails here rather than in a release."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "fafnir.__version__" }' in text


@pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="not a git checkout")
def test_any_tag_on_this_commit_matches_the_version():
    """A tag names a commit; the version is what that commit says it is.

    Only asserts when HEAD is actually tagged, so ordinary development is unaffected.
    On a release commit this is the same check the tag-guard workflow runs, brought
    forward to where a developer sees it.
    """
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    version_tags = [t for t in tags if re.match(r"^v\d+\.\d+\.\d+$", t)]
    if not version_tags:
        pytest.skip("HEAD is not a release commit")
    assert version_tags == [f"v{fafnir.__version__}"], (
        f"HEAD is tagged {version_tags} but the code says {fafnir.__version__}. "
        "A tag that does not match its commit is worse than no tag."
    )

#!/usr/bin/env python3
"""Fail a pull request that changes what the host runs and keeps the version.

Every version that reaches main is a release (ADR 0022): on each push to main,
``.github/workflows/release.yml`` publishes ``v<__version__>`` if it has no GitHub
release yet. So the pull request is where the number is chosen, and this gate holds
it to three rules, against the branch the pull request merges into:

1. The version is ``X.Y.Z`` and never goes down.
2. A change to what the warehouse host runs -- ``src/``, ``sql/``, ``scripts/``,
   ``etc/`` or ``pyproject.toml`` -- raises it. The repository's own tooling
   (``REPO_TOOLING``) and the files under ``etc/`` that are documentation are not
   what the host runs.
3. A pull request that adds a migration raises the minor version at least: each
   one is an operator step after the pull (doc/releasing.md, "Choosing the
   number"). A migration that only fixes (an index, a constraint) and adds nothing
   a reader sees can say so with a line ``-- release: patch`` in its ``.up.sql``;
   when every added migration says so, a patch is allowed.

In CI it reads the base commit from the ``pull_request`` event
(``GITHUB_EVENT_PATH``). Locally, pass it:

    python3 scripts/check_version.py --base origin/main

Stdlib only. Exit status is 1 when the gate fails, with a message that says which
rule and what to run (``scripts/release.sh patch|minor|major``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

VERSION_FILE = "src/fafnir/__init__.py"
VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: What the warehouse host runs from its checkout: a change here is a release.
SHIPS = ("src/", "sql/", "scripts/", "etc/")
SHIPS_FILES = ("pyproject.toml",)
#: Under those, but not run by the host: the repository's own checks and the bump.
REPO_TOOLING = (
    "scripts/check_doc_links.py",
    "scripts/check_docs_touched.py",
    "scripts/check_version.py",
    "scripts/release.sh",
)
#: Under etc/, but documentation (the docs gate counts them as docs too).
ETC_DOCS = ("etc/fafnirrc", "etc/crontab.example")

MIGRATION = re.compile(r"^sql/migrations/(.+)\.up\.sql$")
PATCH_MARK = re.compile(r"^\s*--\s*release:\s*patch\s*$", re.MULTILINE)


def ships(path: str) -> bool:
    """Whether a change to ``path`` changes what the warehouse host runs."""
    if path in REPO_TOOLING or path in ETC_DOCS or "/__pycache__/" in path:
        return False
    return path in SHIPS_FILES or path.startswith(SHIPS)


def parse(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def evaluate(
    base_version: str,
    head_version: str,
    paths: list[str],
    added_migrations: dict[str, str],
) -> tuple[bool, str]:
    """Return ``(passed, message)`` for a pull request.

    ``paths`` are the paths it changes; ``added_migrations`` maps each migration it
    adds (``0047_dq_audit``) to the text of its ``.up.sql``.
    """
    if not SEMVER.match(head_version):
        return False, (
            f"version: FAILED. __version__ is {head_version!r}, not X.Y.Z "
            "(scripts/release.sh sets it)."
        )
    if not SEMVER.match(base_version):
        return (
            False,
            f"version: FAILED. The base's __version__ {base_version!r} is not X.Y.Z.",
        )
    base, head = parse(base_version), parse(head_version)
    if head < base:
        return False, (
            f"version: FAILED. {head_version} is below the base's {base_version}. "
            "Versions only go up."
        )

    shipped = sorted(p for p in paths if ships(p))
    if not shipped:
        return True, (
            f"version: nothing the host runs changed; {head_version} stays"
            + (" (a release of its own when merged)." if head > base else ".")
        )
    listing = "\n".join(f"  {p}" for p in shipped)
    if head == base:
        return False, (
            f"version: FAILED. This pull request changes what the host runs, and keeps "
            f"the version at {base_version}. Merging it publishes a release, so it "
            f"needs its own number:\n{listing}\n"
            "Run scripts/release.sh patch|minor|major (doc/releasing.md, "
            '"Choosing the number"), and commit src/fafnir/__init__.py.'
        )

    unmarked = sorted(
        n for n, text in added_migrations.items() if not PATCH_MARK.search(text)
    )
    if unmarked and head[:2] == base[:2]:
        names = ", ".join(unmarked)
        return False, (
            f"version: FAILED. {head_version} is a patch of {base_version}, and this pull "
            f"request adds migration(s) {names}. Each is an operator step after the "
            "pull, so it is a minor release at least: scripts/release.sh minor. A "
            "migration that only fixes (an index, a constraint) and adds nothing a "
            "reader sees can say so with a line '-- release: patch' in its .up.sql."
        )
    note = (
        f" and adds migration(s) {', '.join(sorted(added_migrations))}"
        if added_migrations
        else ""
    )
    return True, (
        f"version: passed. {base_version} -> {head_version} for a change to what the "
        f"host runs{note}:\n{listing}\nMerging it publishes v{head_version}."
    )


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def version_at(ref: str) -> str:
    match = VERSION_LINE.search(git("show", f"{ref}:{VERSION_FILE}"))
    return match.group(1) if match else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="base commit (default: from the pull_request event)")
    ap.add_argument("--head", default="HEAD", help="head commit (default: HEAD)")
    a = ap.parse_args(argv)

    base = a.base
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if base is None and event_path:
        with open(event_path, encoding="utf-8") as f:
            pr = json.load(f).get("pull_request") or {}
        base = (pr.get("base") or {}).get("sha")
    if not base:
        ap.error("no base commit: pass --base, or run on a pull_request event")

    # base...head: what this pull request changes, from where it forked. The
    # version it must beat is the base's own, which is what main will release.
    # --no-renames lists a moved file under both names, so a move out of src/
    # still counts as a change to src/.
    paths = [
        p
        for p in git(
            "diff", "--name-only", "--no-renames", f"{base}...{a.head}"
        ).splitlines()
        if p
    ]
    added = git(
        "diff",
        "--name-only",
        "--diff-filter=A",
        f"{base}...{a.head}",
        "--",
        "sql/migrations",
    )
    migrations = {}
    for path in added.splitlines():
        match = MIGRATION.match(path)
        if match:
            migrations[match.group(1)] = git("show", f"{a.head}:{path}")

    passed, message = evaluate(version_at(base), version_at(a.head), paths, migrations)
    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

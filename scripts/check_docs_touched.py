#!/usr/bin/env python3
"""Fail a pull request that changes behaviour and touches no documentation.

A change under ``src/``, ``sql/``, ``scripts/`` or ``etc/`` requires the same PR
to touch at least one of ``doc/``, ``README.md``, ``.claude/skills/``,
``etc/fafnirrc`` or ``etc/crontab.example``. A PR labelled ``no-docs-needed`` is
exempt: the label is the reviewer's explicit statement that nothing a reader
relies on changed.

In CI it reads everything from the ``pull_request`` event (``GITHUB_EVENT_PATH``):
the base and head commits to diff, and the labels. Locally, pass them:

    python3 scripts/check_docs_touched.py --base origin/main --head HEAD
    python3 scripts/check_docs_touched.py --base origin/main --label no-docs-needed

Stdlib only. Exit status is 1 when the gate fails, and the message names every
path that required documentation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

OVERRIDE_LABEL = "no-docs-needed"
REQUIRES_DOCS = ("src/", "sql/", "scripts/", "etc/")
DOC_PREFIXES = ("doc/", ".claude/skills/")
DOC_FILES = ("README.md", "etc/fafnirrc", "etc/crontab.example")


def is_doc(path: str) -> bool:
    return path in DOC_FILES or path.startswith(DOC_PREFIXES)


def requires_docs(path: str) -> bool:
    # etc/fafnirrc and etc/crontab.example sit under etc/ but *are* documentation:
    # touching one satisfies the gate rather than tripping it.
    return path.startswith(REQUIRES_DOCS) and not is_doc(path)


def evaluate(paths: list[str], labels: list[str]) -> tuple[bool, str]:
    """Return ``(passed, message)`` for a PR changing ``paths`` with ``labels``."""
    triggers = sorted(p for p in paths if requires_docs(p))
    docs = sorted(p for p in paths if is_doc(p))
    if not triggers:
        return (
            True,
            "docs-gate: no change under src/, sql/, scripts/ or etc/ -- no docs required.",
        )
    listing = "\n".join(f"  {p}" for p in triggers)
    if docs:
        touched = "\n".join(f"  {p}" for p in docs)
        return (
            True,
            f"docs-gate: passed. Docs touched:\n{touched}\nfor changes to:\n{listing}",
        )
    if OVERRIDE_LABEL in labels:
        return (
            True,
            f"docs-gate: passed on the '{OVERRIDE_LABEL}' label. Changes:\n{listing}",
        )
    return False, (
        "docs-gate: FAILED. These paths change behaviour, and this PR touches no docs:\n"
        f"{listing}\n"
        "Update the docs they affect (doc/, README.md, .claude/skills/, etc/fafnirrc or "
        "etc/crontab.example) in this PR, or, if no reader-facing behaviour changed, "
        f"add the '{OVERRIDE_LABEL}' label."
    )


def changed_paths(base: str, head: str) -> list[str]:
    # --no-renames lists a moved file under both names, so moving a file out of
    # src/ still counts as a change to src/.
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="base commit (default: from the pull_request event)")
    ap.add_argument("--head", help="head commit (default: from the event, else HEAD)")
    ap.add_argument(
        "--label", action="append", default=None, help="a PR label (repeatable)"
    )
    a = ap.parse_args(argv)

    base, head, labels = a.base, a.head, a.label
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and (base is None or labels is None):
        with open(event_path, encoding="utf-8") as f:
            pr = json.load(f).get("pull_request") or {}
        base = base or (pr.get("base") or {}).get("sha")
        head = head or (pr.get("head") or {}).get("sha")
        if labels is None:
            labels = [lb["name"] for lb in pr.get("labels") or []]
    if not base:
        ap.error("no base commit: pass --base, or run on a pull_request event")

    passed, message = evaluate(changed_paths(base, head or "HEAD"), labels or [])
    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

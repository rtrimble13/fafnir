#!/usr/bin/env python3
"""Check that every relative Markdown link in the documentation resolves.

Scans ``doc/``, ``README.md`` and ``.claude/skills/`` for inline links
(``[text](target)``, ``![alt](target)``) and reference definitions
(``[id]: target``). A link to another site (any ``scheme:``) is not checked; a
relative one must name a file or directory that exists, and a ``#fragment`` on a
Markdown target must name a heading in it, slugged the way GitHub slugs them.

Stdlib only, so CI runs it without installing anything. Exit status is 1 when a
link is broken, and each one is printed as ``path:line: message``.

Usage:  python3 scripts/check_doc_links.py [--root REPO_DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

SCAN = ("doc", "README.md", ".claude/skills")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_INLINE = re.compile(
    r"\]\(\s*(<[^<>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
_REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^<>]*>|\S+)")
_HTML_ANCHOR = re.compile(r"<a\s[^>]*\b(?:name|id)\s*=\s*[\"']([^\"']+)[\"']", re.I)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def github_slug(text: str) -> str:
    """Slug a heading's text the way GitHub does for its ``#anchor``.

    Markdown syntax is rendered away first (code spans keep their content, links
    keep their text, emphasis markers and HTML tags go). Then, like GitHub's
    ``github-slugger``: lower-case; drop every character that is not a letter, a
    number, a combining mark, ``_``, ``-`` or a space; turn each space into
    ``-``. Spaces are not collapsed, so ``A — B`` becomes ``a--b``.
    """
    text = _CODE_SPAN.sub(lambda m: m.group(2), text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(\*+|(?<!\w)_+|_+(?!\w))", "", text)
    out = []
    for ch in text.strip().lower():
        if ch in "-_ ":
            out.append("-" if ch == " " else ch)
        elif unicodedata.category(ch)[0] in "LNM":
            out.append(ch)
    return "".join(out)


def _prose_lines(text: str):
    """Yield ``(line_number, line, in_code)`` with fenced code blocks marked."""
    fence = None
    for n, line in enumerate(text.splitlines(), 1):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            yield n, line, True
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                if not line.strip()[len(m.group(1)) :].strip():
                    fence = None
            yield n, line, True
            continue
        yield n, line, False


def anchors(text: str) -> set[str]:
    """Every ``#fragment`` a Markdown document answers to on GitHub."""
    seen: dict[str, int] = {}
    found: set[str] = set()

    def add(heading: str) -> None:
        slug = github_slug(heading)
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        found.add(slug if n == 0 else f"{slug}-{n}")

    prev = None  # previous prose line, a candidate setext heading
    for _, line, in_code in _prose_lines(text):
        if in_code:
            prev = None
            continue
        found.update(a for a in _HTML_ANCHOR.findall(line))
        atx = _ATX.match(line)
        if atx:
            add(atx.group(2) or "")
            prev = None
            continue
        if _SETEXT.match(line) and prev is not None:
            add(prev)
            prev = None
            continue
        stripped = line.strip()
        starts_block = re.match(r"([-*+>|]|\d+[.)])(\s|$)", stripped)
        prev = stripped if stripped and not starts_block else None
    return found


def links(text: str):
    """Yield ``(line_number, target)`` for every link outside code."""
    for n, line, in_code in _prose_lines(text):
        if in_code:
            continue
        line = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        ref = _REF_DEF.match(line)
        if ref:
            yield n, ref.group(1)
            continue
        for m in _INLINE.finditer(line):
            yield n, m.group(1)


def check_file(path: Path, root: Path, cache: dict[Path, set[str]]) -> list[str]:
    errors = []
    rel = path.relative_to(root).as_posix()
    for n, raw in links(path.read_text(encoding="utf-8")):
        target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        if not target or _SCHEME.match(target) or target.startswith("//"):
            continue
        part, _, frag = target.partition("#")
        part = unquote(part.split("?", 1)[0])
        if part.startswith("/"):
            dest = root / part.lstrip("/")
        elif part:
            dest = path.parent / part
        else:
            dest = path
        if not dest.exists():
            errors.append(f"{rel}:{n}: broken link '{target}' (no such path)")
            continue
        if frag and dest.is_file() and dest.suffix.lower() == ".md":
            key = dest.resolve()
            if key not in cache:
                cache[key] = anchors(dest.read_text(encoding="utf-8"))
            if unquote(frag).lower() not in cache[key]:
                errors.append(
                    f"{rel}:{n}: broken link '{target}' (no heading '#{frag}')"
                )
    return errors


def markdown_files(root: Path) -> list[Path]:
    files = []
    for entry in SCAN:
        p = root / entry
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.md")))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    cache: dict[Path, set[str]] = {}
    files = markdown_files(root)
    errors = [e for f in files for e in check_file(f, root, cache)]
    for e in errors:
        print(e)
    print(f"checked {len(files)} Markdown files: {len(errors)} broken link(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

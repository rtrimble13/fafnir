"""
The never-auto-resolve list exists twice, and must stay one list.

``NEVER_AUTO_RESOLVE`` in :mod:`fafnir_mcp.tools` is what ``dq_triage`` stamps on
every row; standing rule 5 in ``.claude/skills/fafnir-dba/SKILL.md`` is what the
agent reads. Two copies of a safety policy is exactly the drift ADR 0010 declines
to accept for the CLI's guards -- *"two copies of every rule and a drift discovered
the night they disagree"* -- so this parses the skill and asserts they agree.

The failure this prevents is specific and quiet: someone adds a check to the code
list, the skill still lists four, and an agent reading the skill proposes a resolve
for the fifth in a batch where nobody re-reads the tool output closely.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from fafnir_mcp.tools import NEVER_AUTO_RESOLVE

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "fafnir-dba"
SKILL = SKILL_DIR / "SKILL.md"
SWEEP_POLICY = SKILL_DIR / "references" / "sweep-policy.md"
PLAYBOOKS = SKILL_DIR / "references" / "dq-playbooks.md"


def _backticked_checks(text: str) -> set[str]:
    """Every `check_name`-looking token in backticks."""
    return {
        m
        for m in re.findall(r"`([a-z][a-z0-9_]+)`", text)
        if m.startswith(("price_", "corporate_", "symbol_", "adjustment_", "dividend_"))
        or m in {"gap", "outlier", "stale"}
    }


def test_skill_file_exists():
    """A missing skill would make every assertion below vacuously true."""
    assert SKILL.is_file(), f"skill not found at {SKILL}"


def test_never_auto_matches_the_skill():
    """Standing rule 5 and NEVER_AUTO_RESOLVE name the same checks."""
    text = SKILL.read_text()
    # Rule 5 runs from its number to the start of rule 6.
    match = re.search(r"^5\. \*\*.*?(?=^6\. \*\*)", text, re.S | re.M)
    assert match, "standing rule 5 not found in SKILL.md"
    listed = _backticked_checks(match.group(0))
    assert listed == set(NEVER_AUTO_RESOLVE), (
        "SKILL.md rule 5 and fafnir_mcp.tools.NEVER_AUTO_RESOLVE disagree.\n"
        f"  only in the skill: {sorted(listed - set(NEVER_AUTO_RESOLVE))}\n"
        f"  only in the code:  {sorted(set(NEVER_AUTO_RESOLVE) - listed)}"
    )


def test_rule_5_states_the_count_it_lists():
    """'Five checks' and five checks. A stale count is how the list rots."""
    text = SKILL.read_text()
    match = re.search(r"^5\. \*\*(\w+) checks are never yours to close", text, re.M)
    assert match, "rule 5 no longer opens with a spelled-out count"
    words = {"Three": 3, "Four": 4, "Five": 5, "Six": 6, "Seven": 7}
    stated = words.get(match.group(1))
    assert stated is not None, f"unrecognised count word {match.group(1)!r}"
    assert stated == len(NEVER_AUTO_RESOLVE)


def test_sweep_policy_never_tier_matches():
    """The sweep policy's Never tier is the same list again."""
    path = SWEEP_POLICY
    assert path.is_file(), f"sweep policy not found at {path}"
    row = next(
        (ln for ln in path.read_text().splitlines() if ln.startswith("| **Never**")),
        None,
    )
    assert row, "the Never tier row is missing from sweep-policy.md"
    assert _backticked_checks(row) == set(NEVER_AUTO_RESOLVE)


@pytest.mark.parametrize("check", sorted(NEVER_AUTO_RESOLVE))
def test_every_never_check_has_a_playbook(check):
    """A check nobody may close still needs an entry saying why."""
    text = PLAYBOOKS.read_text()
    assert (
        f"## `{check}`" in text or f"`{check}`" in text
    ), f"{check} is on the never-resolve list with no playbook entry"

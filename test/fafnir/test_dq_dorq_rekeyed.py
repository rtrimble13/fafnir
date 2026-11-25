"""dorq 0.18.0's record keys: one condition, one report (dorq DE-12, fafnir DE-16).

dorq 0.18.0 folds into one report what earlier releases reported several times: a
period's versions that carry the same value or gap (DQ902, DQ903, DQ922), the
identities one wrong field breaks (DQ903), a period's basic and diluted counts
(DQ922), and a series' restatements of one day (DQ932). Their record keys change.
fafnir compares a night on either side of the release by condition atoms
(:func:`fafnir.dq.dorq.condition_atoms`), carries an accepted SQL flag to a folded
report only where it covers all of it, and withholds a folded report the queue
holds under its earlier keys. These run with no database.
"""

from __future__ import annotations

import json

from fafnir.dq import compare_statements as cst
from fafnir.dq import dorq

SID = 7
Q3 = {
    "statement": "balance_sheet",
    "period_type": "quarter",
    "fiscal_date": "2010-09-30",
}


def _record(check, key, inner=None, *, sid=SID, code="DQ903", version="0.18.0"):
    """A fafnir-format row: dorq's record in ``detail``, the check's own detail in
    ``detail.detail``."""
    return {
        "security_id": sid,
        "table_name": "core.balance_sheet",
        "record_key": key,
        "check_name": check,
        "severity": "error",
        "detail": {
            "code": code,
            "date": key.get("fiscal_date"),
            "statement": key.get("statement"),
            "period_type": key.get("period_type"),
            "detail": inner or {},
            "dorq": {"version": version},
        },
    }


def _atoms(*records):
    out = set()
    for r in records:
        out |= dorq.condition_atoms(r)
    return out


# The same conditions as dorq 0.17.0 and 0.18.0 report them (dorq's doc/output.md,
# "Record keys in 0.18.0").

# DQ903: a total assets counting the noncurrent assets twice, in v1 and in v2.
OLD_IDENTITY = [
    _record(
        dorq.IDENTITY_BREAK_CHECK,
        {
            **Q3,
            "version_no": v,
            "identity": "assets_split",
            "item": "total_noncurrent_assets",
        },
        version="0.17.0",
    )
    for v in (1, 2)
] + [
    _record(
        dorq.IDENTITY_BREAK_CHECK,
        {**Q3, "version_no": v, "identity": "balance_sheet"},
        version="0.17.0",
    )
    for v in (1, 2)
]
NEW_IDENTITY = [
    _record(
        dorq.IDENTITY_BREAK_CHECK,
        {**Q3, "version_no": 1, "item": "total_assets"},
        {
            "identities": "assets_split,balance_sheet",
            "field": "total_assets",
            "version_nos": "1,2",
        },
    )
]

# DQ903's chronic break, on two identities, and on the field.
CHRONIC = {
    "statement": "balance_sheet",
    "period_type": "quarter",
    "fiscal_date": "2010-03-31",
}
OLD_CHRONIC = [
    _record(dorq.IDENTITY_BREAK_CHECK, {**CHRONIC, "identity": i}, version="0.17.0")
    for i in ("assets_split", "balance_sheet")
]
NEW_CHRONIC = [
    _record(
        dorq.IDENTITY_BREAK_CHECK,
        {**CHRONIC, "item": "total_assets"},
        {
            "identities": "assets_split,balance_sheet",
            "field": "total_assets",
            "chronic": True,
        },
    )
]

# DQ902: revenue -5 in v1 and v2.
INCOME = {"statement": "income", "period_type": "annual", "fiscal_date": "2010-12-31"}
OLD_VALUE = [
    _record(
        dorq.IMPOSSIBLE_VALUE_CHECK,
        {**INCOME, "version_no": v, "item": "revenue"},
        code="DQ902",
        version="0.17.0",
    )
    for v in (1, 2)
]
NEW_VALUE = [
    _record(
        dorq.IMPOSSIBLE_VALUE_CHECK,
        {**INCOME, "version_no": 1, "item": "revenue"},
        {"version_nos": "1,2"},
        code="DQ902",
    )
]

# DQ922: basic and diluted, each against the outstanding count.
SHARES = {"statement": "income", "period_type": "quarter", "fiscal_date": "2013-03-31"}
OLD_SHARES = [
    _record(
        dorq.SHARE_COUNT_MISMATCH_CHECK,
        {**SHARES, "version_no": 1, "item": item},
        code="DQ922",
        version="0.17.0",
    )
    for item in ("weighted_average_shares", "weighted_average_shares_diluted")
]
NEW_SHARES = [
    _record(
        dorq.SHARE_COUNT_MISMATCH_CHECK,
        {**SHARES, "version_no": 1},
        {
            "items": "weighted_average_shares,weighted_average_shares_diluted",
            "pairs": 2,
        },
        code="DQ922",
    )
]

# DQ932: three periods restated at x1000 on one day.
OLD_RESTATED = [
    _record(
        dorq.RESTATEMENT_CHECK,
        {
            "statement": "income",
            "period_type": "quarter",
            "fiscal_date": d,
            "version_no": 2,
        },
        code="DQ932",
        version="0.17.0",
    )
    for d in ("2014-03-31", "2014-06-30", "2014-09-30")
]
NEW_RESTATED = [
    _record(
        dorq.RESTATEMENT_CHECK,
        {
            "statement": "income",
            "period_type": "quarter",
            "fiscal_date": "2014-03-31",
            "version_no": 2,
            "known_from": "2016-03-01",
        },
        {
            "known_from": "2016-03-01",
            "periods": 3,
            "restated": "income quarter 2014-03-31 v2,income quarter 2014-06-30 v2,"
            "income quarter 2014-09-30 v2",
        },
        code="DQ932",
    )
]

# A check DE-12 left alone: its key is its condition, on either release.
UNCHANGED = _record(
    "dorq_bad_value",
    {**INCOME, "version_no": 1, "item": "revenue"},
    code="DQ911",
)

OLD_NIGHT = (
    OLD_IDENTITY + OLD_CHRONIC + OLD_VALUE + OLD_SHARES + OLD_RESTATED + [UNCHANGED]
)
NEW_NIGHT = (
    NEW_IDENTITY + NEW_CHRONIC + NEW_VALUE + NEW_SHARES + NEW_RESTATED + [UNCHANGED]
)


def test_an_earlier_releases_keys_and_the_folded_report_are_the_same_atoms():
    for old, new in (
        (OLD_IDENTITY, NEW_IDENTITY),
        (OLD_CHRONIC, NEW_CHRONIC),
        (OLD_VALUE, NEW_VALUE),
        (OLD_SHARES, NEW_SHARES),
        (OLD_RESTATED, NEW_RESTATED),
    ):
        assert _atoms(*old) == _atoms(*new), old[0]["check_name"]
    # An atom is keyed as dorq before 0.18.0 keyed a condition: DQ903's identity
    # with no item, on one version.
    assert (
        dorq.IDENTITY_BREAK_CHECK,
        SID,
        dorq.record_key_json({**Q3, "version_no": 2, "identity": "assets_split"}),
    ) in _atoms(*NEW_IDENTITY)
    # Any other check is one atom, its own condition key.
    assert dorq.condition_atoms(UNCHANGED) == {dorq.condition_key(UNCHANGED)}


def _night(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_a_night_across_the_release_has_no_new_condition(tmp_path):
    old = _night(tmp_path / "2026-12-01.jsonl", OLD_NIGHT)
    new = _night(tmp_path / "2026-12-02.jsonl", NEW_NIGHT)
    # Keyed exactly, four of the folded reports would be new conditions (DQ902's
    # keeps the first version's key, which v1's own report had).
    old_keys = {dorq.condition_key(r) for r in OLD_NIGHT}
    assert sum(dorq.condition_key(r) not in old_keys for r in NEW_NIGHT) == 4
    # By atoms, an unchanged night on 0.18.0 has none, nor one rolled back.
    before = set(cst._atom_hashes(old))
    assert not [r for r in NEW_NIGHT if cst.is_new(r, before)]
    after = set(cst._atom_hashes(new))
    assert not [r for r in OLD_NIGHT if cst.is_new(r, after)]
    # A later version repeating a condition is the same condition; another period's
    # is new.
    v3 = _record(
        dorq.IMPOSSIBLE_VALUE_CHECK,
        {**INCOME, "version_no": 1, "item": "revenue"},
        {"version_nos": "1,2,3"},
        code="DQ902",
    )
    assert not cst.is_new(v3, before)
    other = _record(
        dorq.IMPOSSIBLE_VALUE_CHECK,
        {**INCOME, "fiscal_date": "2011-12-31", "version_no": 1, "item": "revenue"},
        code="DQ902",
    )
    assert cst.is_new(other, before)


IDENTITY_FLAG = {
    "check_name": "balance_sheet_identity",
    "security_id": SID,
    "table_name": "core.balance_sheet",
    "record_key": {"period_type": "quarter", "fiscal_date": "2010-09-30"},
    "detail": None,
}


def test_an_accepted_sql_flag_carries_a_folded_report_only_where_it_covers_it_all():
    folded = cst.StatementReport.of(NEW_IDENTITY[0])
    assert folded.identities == ("assets_split", "balance_sheet")
    assert folded.version_set == (1, 2)
    # The overlap sees the flag's identity among the report's.
    assert cst.covers(cst.SUCCESSORS["balance_sheet_identity"], IDENTITY_FLAG, folded)
    # But accepting the balance sheet's gap is no verdict on assets_split's: the
    # report is written, as assets_split's own was before the fold.
    assert not cst.accepted_covers(IDENTITY_FLAG, folded)
    # One identity, folded across versions: a flag on v1 alone carries it only
    # where v1 is its only version.
    one = _record(
        dorq.IDENTITY_BREAK_CHECK,
        {**Q3, "version_no": 1, "identity": "balance_sheet"},
        {"version_nos": "1,2"},
    )
    v1_flag = {
        **IDENTITY_FLAG,
        "record_key": {**IDENTITY_FLAG["record_key"], "version_no": 1},
    }
    assert cst.accepted_covers(IDENTITY_FLAG, cst.StatementReport.of(one))
    assert not cst.accepted_covers(v1_flag, cst.StatementReport.of(one))
    lone = _record(
        dorq.IDENTITY_BREAK_CHECK, {**Q3, "version_no": 1, "identity": "balance_sheet"}
    )
    assert cst.accepted_covers(v1_flag, cst.StatementReport.of(lone))


def test_the_queue_holds_a_folded_condition_under_its_earlier_keys():
    held = cst.HeldUnderEarlierKeys(OLD_IDENTITY + OLD_SHARES)
    assert held.holds(NEW_IDENTITY[0])
    assert held.holds(NEW_SHARES[0])
    # A report with an atom no flag holds is written: here, a third version.
    v3 = _record(
        dorq.IDENTITY_BREAK_CHECK,
        {**Q3, "version_no": 1, "item": "total_assets"},
        {"identities": "assets_split,balance_sheet", "version_nos": "1,2,3"},
    )
    assert not held.holds(v3)
    # A report under a key the queue has is the ingest's own guards' to see.
    assert not cst.HeldUnderEarlierKeys(NEW_IDENTITY).holds(NEW_IDENTITY[0])
    # And another check's report never is.
    assert not held.holds(UNCHANGED)
    written = list(held.withhold([NEW_IDENTITY[0], v3, UNCHANGED]))
    assert written == [v3, UNCHANGED]
    assert held.withheld == 1

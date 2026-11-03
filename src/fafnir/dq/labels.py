"""`fafnir dq export-labels`: the warehouse's decisions as labels for dorq (DR-0701).

dorq calibrates its price model on labelled history (`dorq calibrate`, dorq's
doc/calibration.md), and is measured against it (`fafnir dq compare`, dorq-eval).
The warehouse already holds most of those labels -- every repair an operator made,
and every flag an operator judged -- and a person states the rest:

``ops.operator_override``
    A repair made because the row was wrong (``reason = 'data_error'``) is a
    confirmed data error, and the override keeps the row *as it stood*
    (``detail.row``), so the evidence the error left can be put back with dorq's
    ``--restore`` (bars) and ``--restore-actions`` (corporate actions):

    * a deleted bar                     -> ``data_error``, any code (``bar_deleted``)
    * a shifted history (prices shift)  -> ``data_error``, DQ206 (``date_shift``)
    * a rescaled era (prices rescale)   -> ``data_error``, DQ202 (``scale_era``)
    * a history moved to another
      security (security split)         -> ``data_error``, DQ205 (``history_split``)
    * a split the operator deleted      -> ``data_error``, DQ702|DQ704
      (``split_deleted``): the bars do not move by it -- a phantom, or a
      duplicate of a real one
    * a split the operator re-dated     -> ``data_error``, DQ701 on the
      *original* ex-date (``split_redated``)
    * a split the operator added        -> ``context_gap``, DQ203 (``split_added``)
    * a dividend the operator deleted   -> ``data_error``, DQ705
      (``dividend_deleted``)
    * a dividend the operator added     -> ``context_gap``, DQ201|DQ203
      (``dividend_added``): a price drop the missing dividend explains

    An override made for another reason -- ``out_of_scope`` (CELG-RI's bars,
    deleted because a contingent value right does not belong) or ``cleanup`` -- is
    not a statement that the vendor's row was wrong, and is not a label.

``ops.data_quality_flag``
    A flag closed by judgement rather than by repair:

    * ``outlier`` accepted, or resolved with the bar unchanged and no repair near
      it                                -> ``market_fact`` (the move was real)
    * ``outlier`` closed by ``dq recheck`` because the split explaining it has
      since been loaded                 -> ``context_gap``, DQ203 (``split_loaded``)
    * ``dorq_*`` accepted               -> ``data_error`` with its code (the
      condition is real and has no repair)
    * ``dorq_*`` resolved with no repair near it -> ``market_fact`` for its code

``ops.dq_label``
    Labels a person stated (``fafnir dq label add|import``, ``dq accept|resolve
    --label``): feed gaps, odd-lot-only staleness, real distributions, a vendor's
    wrong instrument, cohort facts. A row there wins over a derived label on the
    same security, overlapping dates and class, and of the same kind (bars or
    statements).

The statements (DR-0913; the dorq fundamentals plan, §5.7)
    The warehouse never edits a statement value. Its repairs are the vendor's: SF1
    corrects a first-reported row, and the loader supersedes it and loads the
    corrected one with the same version (``fundamentals_vendor_correction``). The
    superseded row keeps the evidence, so a correction is a label, as an operator's
    repair of a bar is:

    * a value moved beyond the SQL checks' tolerance (the larger of 0.5% and 1M)
      -> ``data_error``: by a power of ten DQ912 (``corrected_unit``), a sign
      DQ913 (``corrected_sign``), from a zero DQ915 (``corrected_zero``), and
      otherwise any of the codes a wrong value can draw (``corrected_value``);
    * a filing date moved -> ``data_error``, DQ931 (``corrected_filing_date``).

    Each is a label on the superseded version's period, with its statement, period
    type, version and item, and applies until the correction loaded.
    ``--restore-statements`` writes the superseded rows in the statements input's
    shape. A statement flag closed by judgement:

    * accepted (the filing shows it), SQL or dorq -> ``market_fact`` on the code
      that succeeds its check, or its own (``<check>_accepted``);
    * resolved with a vendor correction of its period between its detection and
      its closure -> ``data_error`` on that code, added to the correction's label
      (``<check>_vendor_fixed`` when the correction moved nothing beyond
      tolerance).

    Any other resolution says nothing about the data, and is not a label.

Only an unambiguous disposition is derived. An accepted ``gap`` can mean "the
session had no trades" (a market fact) or "the vendor has no bar and never will" (a
data error); acceptance alone cannot say which, so it is not guessed -- a person
states it in ``ops.dq_label``.

When a label applies
--------------------
Every label carries ``decided_at`` (when the decision was made) and
``applies_until``. A *repair* label -- anything from ``ops.operator_override``, and
``split_loaded``, whose split the vendor has since put on file -- applies until the
data changed: after it, what dorq reads is the corrected data, so the label is no
longer a statement about what dorq saw. A judgement that changed no data (a market
fact, an accepted ``dorq_*`` flag, a stated feed gap) has ``applies_until`` null and
always applies. ``fafnir dq compare`` judges a shadow night only by the labels that
applied when its run started.

``verified`` is ``true`` for a flag closed ``--per-flag`` and a stated label marked
verified, ``false`` for one closed ``--by-rule`` (the bulk acceptances of
2026-09-10/11) or a stated label that is not, and absent when nobody said.

What is left out, and why: a flag closed by ``dq recheck`` because the *vendor*
corrected the bar (the evidence is gone and nothing kept it); a re-dated dividend
(no dorq check names a misdated dividend); the corporate actions a history split or
a bar shift moved along with the bars (they belong to that repair's label); and
gaps, stale feeds and dividends judged without a stated label.

Reads only.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional

from fafnir.db.connection import Database
from fafnir.dq import checks as _checks
from fafnir.ingest import fundamentals_map as fm

#: Calendar days around a repair within which a judged flag is taken to be about
#: that repair, and so not exported a second time as a judgement.
REPAIR_SLACK_DAYS = 3

#: The only override reason that is a statement about the vendor's data.
LABELLED_REASON = "data_error"

_BAR_FIELDS = ("open", "high", "low", "close", "volume")

#: The columns `fafnir.dq.dorq.export_context` writes for dorq's ``--actions``,
#: which ``--restore-actions`` writes too, so the two files can be read alike.
ACTION_FIELDS = (
    "security_id",
    "ex_date",
    "action_type",
    "split_numerator",
    "split_denominator",
    "dividend_amount",
)

LABEL_CLASSES = ("data_error", "context_gap", "market_fact")

#: The label kinds a person states (ops.dq_label), with the class each belongs to
#: and the codes it implies when none are given. A list is several labels for one
#: statement: a real distribution is a false DQ705 *and* a false price report.
#: Other kinds are free text, as in dorq's label format, and need --class.
STATED_KINDS: dict[str, tuple[str, list[dict]]] = {
    "feed_gap": ("data_error", [{"expect": "DQ304"}]),
    "odd_lots_only": ("market_fact", [{"codes": "DQ304"}]),
    "distribution": ("market_fact", [{"codes": "DQ705"}, {"codes": "DQ2"}]),
    "dividend_units": ("data_error", [{"expect": "DQ705"}]),
    "wrong_instrument": ("data_error", [{"expect": "DQ202|DQ205"}]),
    "bad_print": ("data_error", [{"expect": "DQ201"}]),
    "cohort_fact": ("market_fact", [{"codes": "DQ601"}]),
    # Statements (DR-0913): give the label's period with --fiscal-date or
    # --first/--last, and its --statement, --period-type, --version-no, --item.
    "unit_error": ("data_error", [{"expect": "DQ912"}]),
    "sign_error": ("data_error", [{"expect": "DQ913"}]),
    "misdated_filing": ("data_error", [{"expect": "DQ931"}]),
    "structural_gap": ("market_fact", [{"codes": "DQ903"}]),
    "fiscal_year_change": ("market_fact", [{"codes": "DQ941"}]),
    "real_zero": ("market_fact", [{"codes": "DQ915"}]),
}

#: The statements' kinds: a stated label of one is about statements.
STATEMENT_KINDS = frozenset(
    {
        "unit_error",
        "sign_error",
        "misdated_filing",
        "structural_gap",
        "fiscal_year_change",
        "real_zero",
    }
)

#: dorq's statement names, and fafnir's tables for them.
STATEMENT_TABLES = dict(fm.TABLES)
STATEMENT_OF_TABLE = {table: name for name, table in STATEMENT_TABLES.items()}
PERIOD_TYPES = ("quarter", "annual")

#: The statement scope a label can carry (dorq's doc/labels.md): each optional,
#: an unset one covering every value.
SCOPE_FIELDS = ("statement", "period_type", "version_no", "item")

#: A corrected value is a label when it moved by more than the SQL checks'
#: tolerance: this share of the larger value, and this much of the reporting
#: currency (``fafnir.dq.checks.IDENTITY_SHARE``, ``TIE_FLOOR``). A move by a power
#: of ten, by a sign or from a zero is a label whatever its size.
MOVE_SHARE = _checks.IDENTITY_SHARE
MOVE_FLOOR = _checks.TIE_FLOOR
#: A share count's floor: the loader's recovery rounding.
SHARE_FLOOR = 1000
#: The codes a wrong statement value can draw, for a correction that is neither a
#: power of ten, a sign nor a zero: dorq's value, identity, tie, share and
#: restatement checks.
VALUE_CODES = "DQ902|DQ903|DQ904|DQ911|DQ912|DQ913|DQ914|DQ915|DQ916|DQ921|DQ922|DQ932"
#: Expected codes by the kind of a correction's move.
MOVE_EXPECT = {
    "power": "DQ912",
    "power_sign": "DQ912|DQ913",
    "sign": "DQ913",
    "zero": "DQ915",
    "other": VALUE_CODES,
}
MOVE_KIND = {
    "power": "corrected_unit",
    "power_sign": "corrected_unit",
    "sign": "corrected_sign",
    "zero": "corrected_zero",
    "other": "corrected_value",
}


@dataclass
class LabelExport:
    labels: list[dict] = field(default_factory=list)
    restore: list[dict] = field(default_factory=list)
    restore_actions: list[dict] = field(default_factory=list)
    #: The superseded statement rows the correction labels describe, in the
    #: statements input's shape (:data:`fafnir.dq.dorq_statements._FIELDS`, then
    #: ``close``, then every line item).
    restore_statements: list[dict] = field(default_factory=list)

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label in self.labels:
            out[label["kind"]] = out.get(label["kind"], 0) + 1
        return dict(sorted(out.items()))


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else str(d)


def _label(
    security_id,
    first,
    last,
    label_class: str,
    kind: str,
    source: str,
    note: Optional[str],
    *,
    decided_at=None,
    applies_until=None,
    verified: Optional[bool] = None,
    expect: str = "",
    codes: str = "",
    remove: tuple = (),
    extra: Optional[dict] = None,
    scope: Optional[dict] = None,
) -> dict:
    out = {
        "series": "*" if security_id is None else str(security_id),
        "first": _iso(first),
        "last": _iso(last),
        "class": label_class,
        "kind": kind,
        "source": source,
    }
    if expect:
        out["expect"] = expect
    if codes:
        out["codes"] = codes
    if remove:
        out["remove"] = ",".join(_iso(d) for d in sorted(remove))
    if note:
        out["note"] = note
    out["decided_at"] = _iso(decided_at)
    out["applies_until"] = _iso(applies_until)
    if verified is not None:
        out["verified"] = bool(verified)
    for name in SCOPE_FIELDS:
        value = (scope or {}).get(name)
        if value not in (None, ""):
            out[name] = int(value) if name == "version_no" else str(value)
    if extra:
        out.update(extra)
    return out


def is_statements(label: dict) -> bool:
    """Whether a label (as exported) is about statements: it names a statement
    scope, or expects or excuses a DQ9xx code."""
    if any(label.get(f) not in (None, "") for f in SCOPE_FIELDS):
        return True
    codes = str(label.get("codes") or "")
    expect = str(label.get("expect") or "")
    return codes.startswith("DQ9") or any(
        c.startswith("DQ9") for c in expect.split("|")
    )


def _repair(
    security_id, first, last, label_class, kind, row, **kwargs
) -> dict:  # one override (or edit) -> one label, applying until it was made
    return _label(
        security_id,
        first,
        last,
        label_class,
        kind,
        "operator_override",
        row["note"],
        decided_at=row["created_at"],
        applies_until=row["created_at"],
        **kwargs,
    )


def _restore_row(security_id: int, key_date, row: dict) -> Optional[dict]:
    if not row or any(row.get(f) in (None, "") for f in _BAR_FIELDS[:4]):
        return None
    out = {"series": str(security_id), "date": _iso(row.get("trade_date") or key_date)}
    for f in _BAR_FIELDS:
        out[f] = row.get(f)
    return out


def _restore_action(security_id: int, key_date, action_type: str, row: dict) -> dict:
    return {
        "security_id": security_id,
        "ex_date": _iso(row.get("ex_date") or key_date),
        "action_type": row.get("action_type") or action_type,
        "split_numerator": row.get("split_numerator"),
        "split_denominator": row.get("split_denominator"),
        "dividend_amount": row.get("dividend_amount"),
    }


def _price_overrides(db: Database, out: LabelExport) -> dict[int, list[tuple]]:
    """Labels from the active bar repairs; returns the repaired spans per security."""
    rows = db.fetchall(
        """
        SELECT override_id, security_id, key_date, operation, detail, note,
               created_at
          FROM ops.operator_override
         WHERE revoked_at IS NULL AND target = 'daily_price' AND reason = %s
         ORDER BY security_id, key_date, override_id
        """,
        (LABELLED_REASON,),
    )
    repaired: dict[int, list[tuple]] = {}
    edits: dict[tuple, list[dict]] = {}
    moves: dict[tuple, list[dict]] = {}
    for r in rows:
        detail = r["detail"] or {}
        if "transform" in detail:
            edit = detail["transform"].get("edit") or r["override_id"]
            edits.setdefault((r["security_id"], int(edit)), []).append(r)
        elif "split" in detail:
            dest = detail["split"].get("destination_security_id")
            moves.setdefault((r["security_id"], dest), []).append(r)
        elif r["operation"] == "delete":
            sid, d = r["security_id"], r["key_date"]
            out.labels.append(_repair(sid, d, d, "data_error", "bar_deleted", r))
            restored = _restore_row(sid, d, detail.get("row") or {})
            if restored:
                out.restore.append(restored)
            repaired.setdefault(sid, []).append((d, d))

    for (sid, _edit), halves in edits.items():
        kind = (halves[0]["detail"].get("transform") or {}).get("kind")
        deletes = [h for h in halves if h["operation"] == "delete"]
        adds = [h for h in halves if h["operation"] == "add"]
        dates = [h["key_date"] for h in halves]
        first, last = min(dates), max(dates)
        expect, name = (
            ("DQ206", "date_shift") if kind == "shift" else ("DQ202", "scale_era")
        )
        removed = {h["key_date"] for h in adds} - {h["key_date"] for h in deletes}
        made = min(halves, key=lambda h: h["created_at"])
        out.labels.append(
            _repair(
                sid,
                first,
                last,
                "data_error",
                name,
                made,
                expect=expect,
                remove=tuple(removed),
            )
        )
        for h in deletes:
            restored = _restore_row(
                sid, h["key_date"], (h["detail"] or {}).get("row") or {}
            )
            if restored:
                out.restore.append(restored)
        repaired.setdefault(sid, []).append((first, last))

    for (sid, _dest), moved in moves.items():
        dates = [m["key_date"] for m in moved]
        first, last = min(dates), max(dates)
        made = min(moved, key=lambda m: m["created_at"])
        out.labels.append(
            _repair(
                sid, first, last, "data_error", "history_split", made, expect="DQ205"
            )
        )
        for m in moved:
            restored = _restore_row(
                sid, m["key_date"], (m["detail"] or {}).get("row") or {}
            )
            if restored:
                out.restore.append(restored)
        repaired.setdefault(sid, []).append((first, last))
    return repaired


def _action_overrides(db: Database, out: LabelExport, repaired: dict) -> None:
    """Labels from the active corporate-action repairs (L1).

    A history split's and a bar shift's overrides (``detail.split``,
    ``detail.transform``) are left out: the actions moved with the bars, and the
    repair is labelled as the bars' (``history_split``, ``date_shift``). A re-date is
    a delete of the old key and an add at the new one; it is labelled once, at the
    old ex-date, from its delete half.
    """
    rows = db.fetchall(
        """
        SELECT override_id, security_id, key_date, action_type, operation, detail,
               note, created_at
          FROM ops.operator_override
         WHERE revoked_at IS NULL AND target = 'corporate_action' AND reason = %s
           AND NOT (detail ? 'split') AND NOT (detail ? 'transform')
         ORDER BY security_id, key_date, override_id
        """,
        (LABELLED_REASON,),
    )
    for r in rows:
        sid, d, kind_of = r["security_id"], r["key_date"], r["action_type"]
        detail = r["detail"] or {}
        if r["operation"] == "add":
            if "redated_from" in detail:
                continue  # labelled at the old date, from the delete half
            if kind_of == "split":
                label = _repair(
                    sid, d, d, "context_gap", "split_added", r, expect="DQ203"
                )
            else:
                label = _repair(
                    sid, d, d, "context_gap", "dividend_added", r, expect="DQ201|DQ203"
                )
        else:
            redated_to = detail.get("redated_to")
            if redated_to and kind_of == "split":
                # The corrected row is on file at `redated_to`: whoever restores the
                # misdated one has to drop that one, or the series shows both.
                label = _repair(
                    sid,
                    d,
                    d,
                    "data_error",
                    "split_redated",
                    r,
                    expect="DQ701",
                    extra={"redated_to": str(redated_to)},
                )
            elif redated_to:
                continue  # a re-dated dividend: no check names one
            elif kind_of == "split":
                label = _repair(
                    sid, d, d, "data_error", "split_deleted", r, expect="DQ702|DQ704"
                )
            else:
                label = _repair(
                    sid, d, d, "data_error", "dividend_deleted", r, expect="DQ705"
                )
            out.restore_actions.append(
                _restore_action(sid, d, kind_of, detail.get("row") or {})
            )
        out.labels.append(label)
        repaired.setdefault(sid, []).append((d, d))


def _near_repair(repaired: dict, sid: int, d: dt.date) -> bool:
    slack = dt.timedelta(days=REPAIR_SLACK_DAYS)
    return any(a - slack <= d <= b + slack for a, b in repaired.get(sid, ()))


def _verified(basis: Optional[str]) -> Optional[bool]:
    return {"per_flag": True, "by_rule": False}.get(basis or "")


def _judged_flags(db: Database, out: LabelExport, repaired: dict) -> None:
    rows = db.fetchall(r"""
        SELECT f.dq_flag_id, f.security_id, f.check_name, f.detail,
               (f.record_key->>'trade_date')::date AS d,
               f.resolution_note, f.accepted_at IS NOT NULL AS accepted,
               COALESCE(f.accepted_at, f.resolved_at) AS decided_at,
               f.judgement_basis,
               p.close AS close_now,
               (SELECT min(a.loaded_at) FROM core.corporate_action a
                 WHERE a.security_id = f.security_id
                   AND a.action_type = 'split'
                   AND a.ex_date = (f.record_key->>'trade_date')::date)
                   AS split_loaded_at
          FROM ops.data_quality_flag f
          LEFT JOIN core.daily_price p
            ON p.security_id = f.security_id
           AND p.trade_date = (f.record_key->>'trade_date')::date
         WHERE f.resolved_at IS NOT NULL
           AND f.security_id IS NOT NULL
           AND f.record_key ? 'trade_date'
           AND (f.check_name = 'outlier' OR f.check_name LIKE 'dorq\_%')
         ORDER BY f.security_id, d, f.dq_flag_id
        """)
    for r in rows:
        sid, d = r["security_id"], r["d"]
        note = r["resolution_note"] or ""
        source = f"dq_flag:{r['dq_flag_id']}"
        if _near_repair(repaired, sid, d):
            continue  # exported as the repair
        detail = r["detail"] or {}
        rechecked = note.startswith("Re-checked:")
        judged = {
            "decided_at": r["decided_at"],
            "verified": _verified(r["judgement_basis"]),
        }
        if r["check_name"] == "outlier":
            if rechecked:
                if r["split_loaded_at"] is not None:
                    # The vendor has since put the split on file: from then on dorq
                    # reads it beside the bars, and DQ203 is no longer the answer.
                    out.labels.append(
                        _label(
                            sid,
                            d,
                            d,
                            "context_gap",
                            "split_loaded",
                            source,
                            note,
                            decided_at=r["decided_at"],
                            applies_until=r["split_loaded_at"],
                            verified=judged["verified"],
                            expect="DQ203",
                        )
                    )
                continue  # otherwise the bar changed: the evidence is gone
            unchanged = (
                r["close_now"] is not None
                and detail.get("close") is not None
                and abs(float(r["close_now"]) - float(detail["close"]))
                <= 1e-6 * max(1.0, abs(float(detail["close"])))
            )
            if r["accepted"] or unchanged:
                kind = "outlier_accepted" if r["accepted"] else "outlier_resolved"
                out.labels.append(
                    _label(sid, d, d, "market_fact", kind, source, note, **judged)
                )
            continue
        # dorq_*: the code the flag carried.
        code = str(detail.get("code") or "")
        if not code or rechecked:
            continue
        last = detail.get("end_date") or d
        if r["accepted"]:
            out.labels.append(
                _label(
                    sid,
                    d,
                    last,
                    "data_error",
                    "dorq_accepted",
                    source,
                    note,
                    expect=code,
                    **judged,
                )
            )
        else:
            out.labels.append(
                _label(
                    sid,
                    d,
                    last,
                    "market_fact",
                    "dorq_resolved",
                    source,
                    note,
                    codes=code,
                    **judged,
                )
            )


# ---------------------------------------------------------------------------
# Statements (DR-0913)
# ---------------------------------------------------------------------------


def classify_move(old, new, *, shares: bool = False) -> Optional[str]:
    """How a corrected value moved, or None when it did not move beyond tolerance.

    ``power`` (by a power of ten, ``power_sign`` with its sign flipped too),
    ``sign``, ``zero`` (a zero the vendor replaced with a value: absent stored as
    zero) or ``other``. ``shares`` takes the share counts' floor.
    """
    if old is None and new is None:
        return None
    floor = SHARE_FLOOR if shares else MOVE_FLOOR
    if old is None or new is None:
        value = abs(float(new if old is None else old))
        return "other" if value > floor else None
    a, b = float(old), float(new)
    if a == b or abs(b - a) <= MOVE_SHARE * max(abs(a), abs(b)):
        return None
    if a == 0:
        return "zero"
    if b == 0:
        return "other" if abs(a) > floor else None
    ratio = b / a
    if ratio < 0 and abs(abs(ratio) - 1) <= MOVE_SHARE:
        return "sign"
    power = math.log10(abs(ratio))
    if abs(power) >= 0.9 and abs(power - round(power)) <= 0.02:
        return "power_sign" if ratio < 0 else "power"
    return "other" if abs(b - a) > floor else None


def _union(*expects: str) -> str:
    """Expected codes, each once, in order: ``DQ912|DQ903``."""
    seen: list[str] = []
    for expect in expects:
        for code in (expect or "").split("|"):
            if code and code not in seen:
                seen.append(code)
    return "|".join(seen)


def _scope_of(statement, period_type, version_no=None, item=None) -> dict:
    return {
        "statement": statement,
        "period_type": period_type,
        "version_no": version_no,
        "item": item,
    }


def _statement_row(statement: str, row: dict, items: list[str], close) -> dict:
    """A statement row in the statements input's shape (the export's columns)."""
    loaded = row.get("loaded_at")
    out = {
        "security_id": row["security_id"],
        "statement": statement,
        "period_type": row["period_type"],
        "fiscal_date": row["fiscal_date"],
        "fiscal_year": row["fiscal_year"],
        "fiscal_period": row["fiscal_period"],
        "version_no": row["version_no"],
        "known_from": row["known_from"],
        "known_to": row["known_to"],
        "knowledge_source": row["knowledge_source"],
        "filing_date": row["filing_date"],
        "loaded_on": (
            loaded.astimezone(dt.timezone.utc).date()
            if isinstance(loaded, dt.datetime)
            else loaded
        ),
        "reported_currency": row["reported_currency"],
        "close": close,
    }
    for name in items:
        out[name] = row.get(name)
    return out


def _closes(db: Database, pairs: set[tuple]) -> dict[tuple, object]:
    """The close each (security, known_from) had, as the statements export finds it."""
    from fafnir.dq.dorq_statements import CLOSE_REACH_DAYS

    if not pairs:
        return {}
    ordered = sorted(pairs)
    rows = db.fetchall(
        """
        SELECT k.security_id, k.known_from, p.close
          FROM unnest(%s::bigint[], %s::date[]) AS k(security_id, known_from)
          LEFT JOIN LATERAL (
                SELECT d.close FROM core.daily_price d
                 WHERE d.security_id = k.security_id AND d.trade_date <= k.known_from
                   AND d.trade_date > k.known_from - %s
                 ORDER BY d.trade_date DESC LIMIT 1) p ON TRUE
        """,
        ([s for s, _ in ordered], [d for _, d in ordered], CLOSE_REACH_DAYS),
    )
    return {(r["security_id"], r["known_from"]): r["close"] for r in rows}


@dataclass
class _Corrections:
    """What the corrections of the statements left: every correction, by key
    (security, statement, period type, fiscal date, version) with when it loaded,
    and the labels made of them, by the same key."""

    times: dict[tuple, list] = field(default_factory=dict)
    labels: dict[tuple, list[dict]] = field(default_factory=dict)
    originals: dict[tuple, dict] = field(default_factory=dict)
    restored: set = field(default_factory=set)


def _statement_corrections(db: Database, out: LabelExport) -> _Corrections:
    """Labels from SF1's corrections of first-reported rows (the superseded rows).

    Per version, the row as first loaded (the earliest superseded) is compared with
    the row now current. Each kind of move beyond tolerance is one label: on its
    item when it moved alone, on the row when several items moved alike (a row
    stored in thousands). A moved filing date is one more. They apply until the
    first correction loaded: from then on dorq reads the corrected row.
    """
    from fafnir.dq.dorq_statements import line_items

    items = [str(i["column_name"]) for i in line_items(db)]
    found = _Corrections()
    restore_keys: list[tuple] = []
    for statement, table in STATEMENT_TABLES.items():
        own = {i.column: i for i in fm.STATEMENTS[statement]}
        corrections = db.fetchall(f"""
            SELECT security_id, period_type, fiscal_date, version_no, superseded_at
              FROM {table} WHERE superseded_at IS NOT NULL
            """)
        for r in corrections:
            key = (
                r["security_id"],
                statement,
                r["period_type"],
                r["fiscal_date"],
                r["version_no"],
            )
            found.times.setdefault(key, []).append(r["superseded_at"])
        originals = db.fetchall(f"""
            SELECT DISTINCT ON (security_id, period_type, fiscal_date, version_no) *
              FROM {table} WHERE superseded_at IS NOT NULL
             ORDER BY security_id, period_type, fiscal_date, version_no, loaded_at
            """)
        current = {
            (r["security_id"], r["period_type"], r["fiscal_date"], r["version_no"]): r
            for r in db.fetchall(f"""
                SELECT c.* FROM {table} c
                 WHERE c.superseded_at IS NULL
                   AND EXISTS (SELECT 1 FROM {table} o
                                WHERE o.superseded_at IS NOT NULL
                                  AND o.security_id = c.security_id
                                  AND o.period_type = c.period_type
                                  AND o.fiscal_date = c.fiscal_date
                                  AND o.version_no = c.version_no)
                """)
        }
        for old in originals:
            sid, ptype = old["security_id"], old["period_type"]
            fd, version = old["fiscal_date"], old["version_no"]
            new = current.get((sid, ptype, fd, version))
            if new is None:
                continue  # superseded with no successor: nothing to compare with
            key = (sid, statement, ptype, fd, version)
            found.originals[key] = old
            groups: dict[str, list[str]] = {}
            for column, item in own.items():
                move = classify_move(
                    old.get(column),
                    new.get(column),
                    shares=item.item_class == "share_count",
                )
                if move is None:
                    continue
                if move.startswith("power"):
                    ratio = float(new[column]) / float(old[column])
                    move = f"{move}:{round(math.log10(abs(ratio)))}"
                groups.setdefault(move, []).append(column)
            labels = []
            corrected_at = old["superseded_at"]
            period = f"{statement} {ptype} {fd} v{version}"
            for move, columns in sorted(groups.items()):
                base = move.split(":")[0]
                labels.append(
                    _label(
                        sid,
                        fd,
                        fd,
                        "data_error",
                        MOVE_KIND[base],
                        "vendor_correction",
                        f"SF1 corrected {period}: {', '.join(columns)}",
                        decided_at=corrected_at,
                        applies_until=corrected_at,
                        expect=MOVE_EXPECT[base],
                        scope=_scope_of(
                            statement,
                            ptype,
                            version,
                            columns[0] if len(columns) == 1 else None,
                        ),
                    )
                )
            if (
                old.get("filing_date") is not None
                and new.get("filing_date") is not None
                and old["filing_date"] != new["filing_date"]
            ):
                labels.append(
                    _label(
                        sid,
                        fd,
                        fd,
                        "data_error",
                        "corrected_filing_date",
                        "vendor_correction",
                        f"SF1 corrected {period}'s filing date: "
                        f"{old['filing_date']} to {new['filing_date']}",
                        decided_at=corrected_at,
                        applies_until=corrected_at,
                        expect="DQ931",
                        scope=_scope_of(statement, ptype, version),
                    )
                )
            if labels:
                found.labels[key] = labels
                restore_keys.append(key)
    _restore(db, out, found, restore_keys, items)
    for labels in found.labels.values():
        out.labels.extend(labels)
    return found


def _restore(
    db: Database,
    out: LabelExport,
    found: _Corrections,
    keys: list[tuple],
    items: Optional[list[str]] = None,
) -> None:
    """The original rows of ``keys``, for ``--restore-statements``, once each."""
    from fafnir.dq.dorq_statements import line_items

    keys = [k for k in keys if k not in found.restored and k in found.originals]
    if not keys:
        return
    if items is None:
        items = [str(i["column_name"]) for i in line_items(db)]
    closes = _closes(
        db,
        {
            (found.originals[k]["security_id"], found.originals[k]["known_from"])
            for k in keys
        },
    )
    for key in keys:
        old = found.originals[key]
        out.restore_statements.append(
            _statement_row(
                key[1], old, items, closes.get((old["security_id"], old["known_from"]))
            )
        )
        found.restored.add(key)


def _year_spans(db: Database, wanted: set[tuple]) -> dict[tuple, tuple]:
    """``(security, table, fiscal_year) -> (first, last)`` fiscal dates in force."""
    out: dict[tuple, tuple] = {}
    by_table: dict[str, set[int]] = {}
    for sid, table, _year in wanted:
        by_table.setdefault(table, set()).add(sid)
    for table, sids in by_table.items():
        for r in db.fetchall(
            f"""
            SELECT security_id, fiscal_year, min(fiscal_date) AS first,
                   max(fiscal_date) AS last
              FROM {table}
             WHERE superseded_at IS NULL AND security_id = ANY(%s)
             GROUP BY security_id, fiscal_year
            """,
            (sorted(sids),),
        ):
            out[(r["security_id"], table, r["fiscal_year"])] = (r["first"], r["last"])
    return out


def _flag_scope(flag: dict, spans: dict) -> Optional[tuple]:
    """``(code, scope, first, last)`` for a closed statement flag, or None when it
    cannot be placed. A dorq flag carries its own; an SQL flag takes its
    successor's code (``fafnir.dq.compare_statements.SUCCESSORS``) and the period,
    fiscal year or span its key names."""
    from fafnir.dq import compare_statements as cst

    key = flag["record_key"] or {}
    detail = flag["detail"] or {}
    table = flag["table_name"]
    decided = flag["decided_at"]
    decided_day = decided.date() if isinstance(decided, dt.datetime) else decided

    def day(value) -> Optional[dt.date]:
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    if flag["check_name"].startswith("dorq_"):
        code = str(detail.get("code") or "")
        first = day(key.get("fiscal_date")) or day(detail.get("date"))
        if not code or first is None:
            return None
        last = day(detail.get("end_date")) or first
        version = key.get("version_no")
        scope = _scope_of(
            key.get("statement") or STATEMENT_OF_TABLE.get(table),
            key.get("period_type"),
            int(version) if version not in (None, "") else None,
            key.get("item"),
        )
        return code, scope, first, max(first, last)
    rule = cst.SUCCESSORS[flag["check_name"]]
    code = rule.codes[0]
    if rule.statement == "key":
        statement = key.get("statement") or STATEMENT_OF_TABLE.get(table)
    elif rule.statement == "table":
        statement = STATEMENT_OF_TABLE.get(table)
    elif rule.statement is not None:
        statement = rule.statement
    elif rule.match in ("security", "currency", "span", "fiscal_year_or_before"):
        # Read on the income statement, as its successor (DQ94x) is filed.
        statement = STATEMENT_OF_TABLE.get(table)
    else:
        statement = None  # a tie or a filing: dorq files it under its own
    floor = dt.date.fromisoformat(_checks.COVERAGE_FLOOR)
    if rule.match == "period":
        first = day(key.get("fiscal_date"))
        if first is None:
            return None
        version = key.get("version_no")
        scope = _scope_of(
            statement,
            key.get("period_type"),
            int(version) if version not in (None, "") else None,
            key.get("item"),
        )
        return code, scope, first, first
    if rule.match in ("fiscal_year", "fiscal_year_or_before"):
        year = key.get("fiscal_year")
        if year in (None, ""):
            return None
        years = [int(year) - 1, int(year)]
        if rule.match == "fiscal_year":
            years = years[1:]
        found = [
            spans[(flag["security_id"], table, y)]
            for y in years
            if (flag["security_id"], table, y) in spans
        ]
        if not found:
            return None
        first = min(f for f, _ in found)
        last = max(lst for _, lst in found)
        return code, _scope_of(statement, None, None, key.get("item")), first, last
    if rule.match == "span":
        first, last = day(key.get("from")), day(key.get("to"))
        if first is None or last is None:
            return None
        return code, _scope_of(statement, None), first, last
    # The security: from its latest fiscal date (stale), else from the floor.
    first = day(key.get("latest_fiscal_date")) or floor
    return code, _scope_of(statement, None), first, max(first, decided_day)


def _statement_flags(db: Database, out: LabelExport, found: _Corrections) -> None:
    """Labels from closed statement flags: accepted is a filing fact; resolved with
    a vendor correction of its period in between is a data error."""
    from fafnir.dq import compare_statements as cst

    rows = db.fetchall(
        r"""
        SELECT f.dq_flag_id, f.security_id, f.check_name, f.table_name,
               f.record_key, f.detail, f.detected_at, f.resolved_at,
               f.resolution_note, f.accepted_at IS NOT NULL AS accepted,
               COALESCE(f.accepted_at, f.resolved_at) AS decided_at,
               f.judgement_basis
          FROM ops.data_quality_flag f
         WHERE f.resolved_at IS NOT NULL AND f.security_id IS NOT NULL
           AND (f.check_name = ANY(%s)
                OR (f.check_name LIKE 'dorq\_%%' AND f.table_name = ANY(%s)))
         ORDER BY f.security_id, f.dq_flag_id
        """,
        (list(cst.SUCCESSORS), list(STATEMENT_TABLES.values())),
    )
    wanted: set[tuple] = set()
    for r in rows:
        if r["check_name"].startswith("dorq_"):
            continue
        if not cst.SUCCESSORS[r["check_name"]].match.startswith("fiscal_year"):
            continue
        year = (r["record_key"] or {}).get("fiscal_year")
        if year in (None, ""):
            continue
        for y in (int(year) - 1, int(year)):
            wanted.add((r["security_id"], r["table_name"], y))
    spans = _year_spans(db, wanted) if wanted else {}
    fixed_keys: list[tuple] = []
    for r in rows:
        placed = _flag_scope(r, spans)
        if placed is None:
            continue
        code, scope, first, last = placed
        source = f"dq_flag:{r['dq_flag_id']}"
        verified = _verified(r["judgement_basis"])
        if r["accepted"]:
            out.labels.append(
                _label(
                    r["security_id"],
                    first,
                    last,
                    "market_fact",
                    f"{r['check_name']}_accepted",
                    source,
                    r["resolution_note"],
                    decided_at=r["decided_at"],
                    verified=verified,
                    codes=code,
                    scope=scope,
                )
            )
            continue
        # Resolved: a data error when SF1 corrected the period in between.
        statement = scope.get("statement")
        hits = [
            (key, at)
            for key, times in found.times.items()
            if key[0] == r["security_id"]
            and (statement is None or key[1] == statement)
            and (scope.get("period_type") is None or key[2] == scope["period_type"])
            and first <= key[3] <= last
            for at in times
            if r["detected_at"] < at <= r["resolved_at"]
        ]
        if not hits:
            continue
        for key, at in sorted(hits, key=lambda h: (h[0], h[1])):
            labels = found.labels.get(key)
            if labels:
                for lab in labels:
                    lab["expect"] = _union(lab.get("expect", ""), code)
                    lab["source"] = f"{lab['source']},{source}"
                continue
            label = _label(
                r["security_id"],
                key[3],
                key[3],
                "data_error",
                f"{r['check_name']}_vendor_fixed",
                f"vendor_correction,{source}",
                r["resolution_note"],
                decided_at=r["decided_at"],
                applies_until=at,
                verified=verified,
                expect=code,
                scope=_scope_of(key[1], key[2], key[4], scope.get("item")),
            )
            found.labels[key] = [label]
            out.labels.append(label)
            fixed_keys.append(key)
    _restore(db, out, found, fixed_keys)


def stated_label(row: dict) -> dict:
    """An ops.dq_label row as a label."""
    extra: dict = {"label_id": int(row["label_id"])}
    if row.get("members"):
        extra["members"] = sorted(int(m) for m in row["members"])
    scope = {f: row.get(f) for f in SCOPE_FIELDS}
    return _label(
        row["security_id"],
        row["first_date"],
        row["last_date"],
        row["class"],
        row["kind"],
        row["source"],
        row["note"],
        decided_at=row["decided_at"],
        applies_until=row["applies_until"],
        verified=bool(row["verified"]),
        expect=row["expect"] or "",
        codes=row["codes"] or "",
        extra=extra,
        scope=scope,
    )


def _stated_labels(db: Database) -> list[dict]:
    from fafnir.db import repository as repo

    return [stated_label(r) for r in repo.list_dq_labels(db)]


def _same_decision(derived: dict, stated: dict) -> bool:
    """Whether a stated label covers what a derived one says: the same security,
    overlapping dates, the same class -- and both about bars or both about
    statements, whose dates are fiscal dates: a stated label on a fiscal 2024-06-30
    is no decision about the bars of that week. Two statement labels must also
    agree on each scope field both name."""
    if is_statements(derived) != is_statements(stated):
        return False
    for f in SCOPE_FIELDS:
        a, b = derived.get(f), stated.get(f)
        if a is not None and b is not None and a != b:
            return False
    return (
        derived["series"] == stated["series"]
        and derived["class"] == stated["class"]
        and derived["first"] <= stated["last"]
        and stated["first"] <= derived["last"]
    )


def _series_order(series: str) -> int:
    return -1 if series == "*" else int(series)


def build(db: Database, *, verified_only: bool = False) -> LabelExport:
    """Every label the warehouse can state, in a stable order.

    The derived labels, then the stated ones (ops.dq_label). Where a stated label
    and a derived one are the same decision -- same security, overlapping dates,
    same class -- the stated one wins. ``verified_only`` keeps the labels marked
    ``verified: true``.
    """
    out = LabelExport()
    repaired = _price_overrides(db, out)
    _action_overrides(db, out, repaired)
    _judged_flags(db, out, repaired)
    if _checks.statements_exist(db):
        corrections = _statement_corrections(db, out)
        _statement_flags(db, out, corrections)
    stated = _stated_labels(db)
    if stated:
        by_series: dict[str, list[dict]] = {}
        for s in stated:
            by_series.setdefault(s["series"], []).append(s)
        out.labels = [
            lab
            for lab in out.labels
            if not any(_same_decision(lab, s) for s in by_series.get(lab["series"], ()))
        ] + stated
    if verified_only:
        out.labels = [lab for lab in out.labels if lab.get("verified") is True]
    out.labels.sort(
        key=lambda lab: (
            _series_order(lab["series"]),
            lab["first"],
            lab["kind"],
            lab["source"],
        )
    )
    out.restore.sort(key=lambda r: (int(r["series"]), r["date"]))
    out.restore_actions.sort(
        key=lambda r: (int(r["security_id"]), r["ex_date"], r["action_type"])
    )
    out.restore_statements.sort(
        key=lambda r: (
            int(r["security_id"]),
            r["statement"],
            r["period_type"],
            str(r["fiscal_date"]),
            int(r["version_no"]),
        )
    )
    return out


def write_labels(export: LabelExport, fh: IO[str]) -> None:
    for label in export.labels:
        fh.write(json.dumps(label, separators=(",", ":"), ensure_ascii=False))
        fh.write("\n")


def write(
    export: LabelExport,
    labels_path: Path,
    restore_path: Optional[Path],
    actions_path: Optional[Path] = None,
    statements_path: Optional[Path] = None,
    statement_items: Optional[list[str]] = None,
) -> None:
    """Write the labels (``-`` is stdout), and the bars, actions and statement rows
    as they stood. ``statement_items`` are the line items, in the export's order;
    by default the rows' own."""
    if str(labels_path) == "-":
        write_labels(export, sys.stdout)
        sys.stdout.flush()
    else:
        labels_path = Path(labels_path)
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        with labels_path.open("w", encoding="utf-8") as fh:
            write_labels(export, fh)
    if restore_path is not None:
        restore_path = Path(restore_path)
        restore_path.parent.mkdir(parents=True, exist_ok=True)
        with restore_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(("series", "date", *_BAR_FIELDS))
            for r in export.restore:
                writer.writerow([r["series"], r["date"], *(r[f] for f in _BAR_FIELDS)])
    if actions_path is not None:
        actions_path = Path(actions_path)
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        with actions_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(ACTION_FIELDS)
            for r in export.restore_actions:
                writer.writerow(["" if r[f] is None else r[f] for f in ACTION_FIELDS])
    if statements_path is not None:
        write_restore_statements(export, Path(statements_path), statement_items)


def write_restore_statements(
    export: LabelExport, path: Path, items: Optional[list[str]] = None
) -> None:
    """The superseded statement rows, in the statements input's shape: the
    columns ``fafnir dq run``'s statements export sends dorq, in its order."""
    from fafnir.dq.dorq_statements import _FIELDS

    fields = list(_FIELDS) + ["close"]
    if items is None:
        items = sorted(
            {k for r in export.restore_statements for k in r if k not in fields}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(fields + list(items))
        for r in export.restore_statements:
            writer.writerow(
                ["" if r.get(f) is None else _iso(r[f]) for f in fields + list(items)]
            )


# ---------------------------------------------------------------------------
# Stating labels (ops.dq_label)
# ---------------------------------------------------------------------------


class LabelRefused(ValueError):
    """A stated label that would not say anything a reader could use."""


def stated_rows(
    *,
    security_id: Optional[int],
    members: Optional[list[int]],
    first: dt.date,
    last: Optional[dt.date],
    label_class: Optional[str],
    kind: str,
    expect: Optional[str],
    codes: Optional[str],
    note: str,
    verified: bool,
    source: str,
    decided_by: str,
    applies_until: Optional[dt.datetime] = None,
    statement: Optional[str] = None,
    period_type: Optional[str] = None,
    version_no: Optional[int] = None,
    item: Optional[str] = None,
) -> list[dict]:
    """The ops.dq_label rows one statement makes.

    A known kind (:data:`STATED_KINDS`) fills in its class and codes when they are
    not given, and refuses a class that contradicts it; ``distribution`` is two
    rows. Any other kind needs ``label_class``.

    ``statement``, ``period_type``, ``version_no`` and ``item`` make it a label
    about statements (dorq's doc/labels.md): then ``first`` and ``last`` are fiscal
    dates. A statement kind (:data:`STATEMENT_KINDS`) is about statements with or
    without them; one of them on a bars kind is refused.
    """
    kind = (kind or "").strip()
    if not kind:
        raise LabelRefused("A label needs a kind.")
    if not (note and note.strip()):
        raise LabelRefused("A label needs a note: what was checked, and how.")
    known = STATED_KINDS.get(kind)
    if known is not None:
        if label_class and label_class != known[0]:
            raise LabelRefused(f"A {kind} label is {known[0]}, not {label_class}.")
        label_class = known[0]
    if label_class not in LABEL_CLASSES:
        raise LabelRefused(
            f"{kind!r} is not a known kind; give --class "
            f"({', '.join(LABEL_CLASSES)})."
        )
    if kind == "cohort_fact" and not members:
        raise LabelRefused(
            "A cohort fact names its members: without them it would cover every "
            "cross-sectional row on its dates."
        )
    if members and security_id is not None:
        raise LabelRefused("A label is about one security, or a cohort's members.")
    last = last or first
    if last < first:
        raise LabelRefused(f"--last {last} is before --first {first}.")
    scope = _stated_scope(statement, period_type, version_no, item)
    if any(v is not None for v in scope.values()):
        if known is not None and kind not in STATEMENT_KINDS:
            raise LabelRefused(
                f"A {kind} label is about bars; --statement, --period-type, "
                "--version-no and --item are for a label about statements."
            )
        if members:
            raise LabelRefused("A label about statements is about one security.")
    defaults = known[1] if known is not None else [{}]
    if expect or codes:
        defaults = [{"expect": expect or "", "codes": codes or ""}]
    rows = []
    for d in defaults:
        rows.append(
            {
                "security_id": security_id,
                "members": members or None,
                "first_date": first,
                "last_date": last,
                "class": label_class,
                "kind": kind,
                "expect": (
                    (d.get("expect") or "") if label_class != "market_fact" else ""
                ),
                "codes": (d.get("codes") or "") if label_class == "market_fact" else "",
                "note": note,
                "verified": verified,
                "source": source,
                "decided_by": decided_by,
                "applies_until": applies_until,
                **scope,
            }
        )
    return rows


def _stated_scope(statement, period_type, version_no, item) -> dict:
    """The statement scope, read and checked: a statement's name or table, a
    period type, a version of 1 or more, an item."""
    if statement not in (None, ""):
        statement = STATEMENT_OF_TABLE.get(str(statement), str(statement))
        if statement not in STATEMENT_TABLES:
            raise LabelRefused(
                f"{statement!r} is not a statement (income, balance_sheet, cash_flow)."
            )
    else:
        statement = None
    if period_type not in (None, ""):
        if period_type not in PERIOD_TYPES:
            raise LabelRefused(
                f"{period_type!r} is not a period type (quarter, annual)."
            )
    else:
        period_type = None
    if version_no is not None:
        try:
            version_no = int(version_no)
        except (TypeError, ValueError) as exc:
            raise LabelRefused(f"version {version_no!r} is not a number.") from exc
        if version_no < 1:
            raise LabelRefused("A version is 1 or more.")
    item = str(item).strip() if item not in (None, "") else None
    return {
        "statement": statement,
        "period_type": period_type,
        "version_no": version_no,
        "item": item,
    }


def read_label_file(fh: IO[str], name: str = "<labels>") -> list[dict]:
    """Labels in dorq's JSONL format (dorq's doc/labels.md), as plain dicts.

    Every line needs ``series``, ``first`` (or ``date``) and ``class``; a bad line
    raises :class:`LabelRefused` naming it.
    """
    out = []
    for n, line in enumerate(fh, 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            series = str(obj["series"])
            first = dt.date.fromisoformat(str(obj.get("first") or obj["date"])[:10])
            last = (
                dt.date.fromisoformat(str(obj["last"])[:10])
                if obj.get("last")
                else first
            )
            label_class = obj["class"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LabelRefused(f"{name} line {n}: not a label ({exc})") from exc
        if label_class not in LABEL_CLASSES:
            raise LabelRefused(f"{name} line {n}: unknown class {label_class!r}")
        if last < first:
            raise LabelRefused(f"{name} line {n}: last is before first")
        if series != "*" and not series.isdigit():
            raise LabelRefused(
                f"{name} line {n}: series {series!r} is not a security id (or *)"
            )
        members = obj.get("members")
        if isinstance(members, str):
            members = [m for m in members.split(",") if m.strip()]
        out.append(
            {
                "line": n,
                "series": series,
                "first": first,
                "last": last,
                "class": label_class,
                "kind": str(obj.get("kind") or label_class),
                "expect": str(obj.get("expect") or ""),
                "codes": str(obj.get("codes") or ""),
                "note": str(obj.get("note") or ""),
                "source": str(obj.get("source") or ""),
                "verified": obj.get("verified"),
                "members": [int(m) for m in members] if members else None,
                "applies_until": obj.get("applies_until"),
                **_read_scope(obj, name, n),
            }
        )
    return out


def _read_scope(obj: dict, name: str, n: int) -> dict:
    try:
        return _stated_scope(
            obj.get("statement"),
            obj.get("period_type"),
            obj.get("version_no"),
            obj.get("item"),
        )
    except LabelRefused as exc:
        raise LabelRefused(f"{name} line {n}: {exc}") from exc


def flag_label_rows(
    flags: list[dict],
    *,
    label_class: str,
    kind: Optional[str],
    expect: Optional[str],
    codes: Optional[str],
    note: str,
    verified: bool,
    decided_by: str,
    accepted: bool,
) -> tuple[list[dict], list[int]]:
    """The labels `dq accept|resolve --label` states, one per closed flag (several
    for a kind that is several labels). Returns ``(rows, skipped_flag_ids)``.

    Each is keyed to the flag's security and the date it is about (a dorq report's
    span runs to its ``end_date``); a cross-sectional dorq flag becomes a cohort
    label over its members. A flag with no date, or a cross-sectional one naming no
    members, cannot be placed and is skipped. With no ``--expect``/``--codes``, a
    dorq flag's own code is the label's.
    """
    rows: list[dict] = []
    skipped: list[int] = []
    for f in flags:
        if f.get("d") is None:
            skipped.append(int(f["dq_flag_id"]))
            continue
        members = None
        if f.get("security_id") is None:
            raw = f.get("members") or ""
            members = sorted({int(m) for m in str(raw).split(",") if m.strip()}) or None
            if not members:
                skipped.append(int(f["dq_flag_id"]))
                continue
        code = f.get("code") or ""
        flag_kind = (
            kind or f"{f['check_name']}_{'accepted' if accepted else 'resolved'}"
        )
        own_expect, own_codes = expect, codes
        if not (expect or codes) and kind not in STATED_KINDS and code:
            if label_class == "market_fact":
                own_codes = code
            else:
                own_expect = code
        first = f["d"]
        last = f.get("end_date") or first
        statement = f.get("statement") or STATEMENT_OF_TABLE.get(
            f.get("table_name") or ""
        )
        scope = (
            {
                "statement": statement,
                "period_type": f.get("period_type"),
                "version_no": f.get("version_no"),
                "item": f.get("item"),
            }
            if statement
            else {}
        )
        rows += stated_rows(
            security_id=f.get("security_id"),
            members=members,
            first=first,
            last=max(first, last),
            label_class=label_class,
            kind=flag_kind,
            expect=own_expect,
            codes=own_codes,
            note=note,
            verified=verified,
            source=f"dq_flag:{f['dq_flag_id']}",
            decided_by=decided_by,
            **scope,
        )
    return rows, skipped

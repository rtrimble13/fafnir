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
    same security, overlapping dates and class.

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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional

from fafnir.db.connection import Database

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
}


@dataclass
class LabelExport:
    labels: list[dict] = field(default_factory=list)
    restore: list[dict] = field(default_factory=list)
    restore_actions: list[dict] = field(default_factory=list)

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
    if extra:
        out.update(extra)
    return out


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


def stated_label(row: dict) -> dict:
    """An ops.dq_label row as a label."""
    extra: dict = {"label_id": int(row["label_id"])}
    if row.get("members"):
        extra["members"] = sorted(int(m) for m in row["members"])
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
    )


def _stated_labels(db: Database) -> list[dict]:
    from fafnir.db import repository as repo

    return [stated_label(r) for r in repo.list_dq_labels(db)]


def _same_decision(derived: dict, stated: dict) -> bool:
    """Whether a stated label covers what a derived one says: the same security,
    overlapping dates, the same class."""
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
) -> None:
    """Write the labels (``-`` is stdout), and the bars and actions as they stood."""
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
) -> list[dict]:
    """The ops.dq_label rows one statement makes.

    A known kind (:data:`STATED_KINDS`) fills in its class and codes when they are
    not given, and refuses a class that contradicts it; ``distribution`` is two
    rows. Any other kind needs ``label_class``.
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
            }
        )
    return rows


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
            }
        )
    return out


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
        )
    return rows, skipped

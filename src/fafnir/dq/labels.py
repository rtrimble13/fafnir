"""`fafnir dq export-labels`: the warehouse's decisions as labels for dorq (DR-0701).

dorq calibrates its price model on labelled history (`dorq calibrate`, dorq's
doc/calibration.md), and is measured against it (`fafnir dq compare`, dorq-eval).
The warehouse already holds those labels -- every repair an operator made, and
every flag an operator judged -- in two tables:

``ops.operator_override``
    A repair is a confirmed data error, and the override keeps the bar *as it
    stood* (``detail.row``), so the evidence the error left can be put back with
    dorq's ``--restore``:

    * a deleted bar                     -> ``data_error``, any code (``bar_deleted``)
    * a shifted history (prices shift)  -> ``data_error``, DQ206 (``date_shift``)
    * a rescaled era (prices rescale)   -> ``data_error``, DQ202 (``scale_era``)
    * a history moved to another
      security (security split)         -> ``data_error``, DQ205 (``history_split``)
    * a split the operator added        -> ``context_gap``, DQ203 (``split_added``)

``ops.data_quality_flag``
    A flag closed by judgement rather than by repair:

    * ``outlier`` accepted, or resolved with the bar unchanged and no repair near
      it                                -> ``market_fact`` (the move was real)
    * ``outlier`` closed by ``dq recheck`` because the split explaining it has
      since been loaded                 -> ``context_gap``, DQ203 (``split_loaded``)
    * ``dorq_*`` accepted               -> ``data_error`` with its code (the
      condition is real and has no repair)
    * ``dorq_*`` resolved with no repair near it -> ``market_fact`` for its code

What is left out, and why: a flag closed by ``dq recheck`` because the *vendor*
corrected the bar (the evidence is gone and nothing kept it), corporate-action
deletes and re-dates (dorq would need the action as it stood, which ``--restore``
does not carry), and gaps (a missing session is not something the price model
judges). The labelling pass the plan calls for (DR-0701) reviews this export and
adds what the tables cannot say on their own.

Reads only.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fafnir.db.connection import Database

#: Calendar days around a repair within which a judged flag is taken to be about
#: that repair, and so not exported a second time as a judgement.
REPAIR_SLACK_DAYS = 3

_BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class LabelExport:
    labels: list[dict] = field(default_factory=list)
    restore: list[dict] = field(default_factory=list)

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label in self.labels:
            out[label["kind"]] = out.get(label["kind"], 0) + 1
        return dict(sorted(out.items()))


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else str(d)


def _label(
    security_id: int,
    first,
    last,
    label_class: str,
    kind: str,
    source: str,
    note: Optional[str],
    *,
    expect: str = "",
    codes: str = "",
    remove: tuple = (),
) -> dict:
    out = {
        "series": str(security_id),
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
    return out


def _restore_row(security_id: int, key_date, row: dict) -> Optional[dict]:
    if not row or any(row.get(f) in (None, "") for f in _BAR_FIELDS[:4]):
        return None
    out = {"series": str(security_id), "date": _iso(row.get("trade_date") or key_date)}
    for f in _BAR_FIELDS:
        out[f] = row.get(f)
    return out


def _price_overrides(db: Database, out: LabelExport) -> dict[int, list[tuple]]:
    """Labels from the active bar overrides; returns the repaired spans per security."""
    rows = db.fetchall("""
        SELECT override_id, security_id, key_date, operation, detail, note
          FROM ops.operator_override
         WHERE revoked_at IS NULL AND target = 'daily_price'
         ORDER BY security_id, key_date, override_id
        """)
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
            out.labels.append(
                _label(
                    sid,
                    d,
                    d,
                    "data_error",
                    "bar_deleted",
                    "operator_override",
                    r["note"],
                )
            )
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
        out.labels.append(
            _label(
                sid,
                first,
                last,
                "data_error",
                name,
                "operator_override",
                halves[0]["note"],
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
        out.labels.append(
            _label(
                sid,
                first,
                last,
                "data_error",
                "history_split",
                "operator_override",
                moved[0]["note"],
                expect="DQ205",
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
    rows = db.fetchall("""
        SELECT security_id, key_date, detail, note
          FROM ops.operator_override
         WHERE revoked_at IS NULL AND target = 'corporate_action'
           AND action_type = 'split' AND operation = 'add'
           AND NOT (detail ? 'redated_from')
         ORDER BY security_id, key_date
        """)
    for r in rows:
        sid, d = r["security_id"], r["key_date"]
        out.labels.append(
            _label(
                sid,
                d,
                d,
                "context_gap",
                "split_added",
                "operator_override",
                r["note"],
                expect="DQ203",
            )
        )
        repaired.setdefault(sid, []).append((d, d))


def _near_repair(repaired: dict, sid: int, d: dt.date) -> bool:
    slack = dt.timedelta(days=REPAIR_SLACK_DAYS)
    return any(a - slack <= d <= b + slack for a, b in repaired.get(sid, ()))


def _judged_flags(db: Database, out: LabelExport, repaired: dict) -> None:
    rows = db.fetchall(r"""
        SELECT f.dq_flag_id, f.security_id, f.check_name, f.detail,
               (f.record_key->>'trade_date')::date AS d,
               f.resolution_note, f.accepted_at IS NOT NULL AS accepted,
               p.close AS close_now,
               EXISTS (SELECT 1 FROM core.corporate_action a
                        WHERE a.security_id = f.security_id
                          AND a.action_type = 'split'
                          AND a.ex_date = (f.record_key->>'trade_date')::date)
                   AS split_on_date
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
        if r["check_name"] == "outlier":
            if rechecked:
                if r["split_on_date"]:
                    out.labels.append(
                        _label(
                            sid,
                            d,
                            d,
                            "context_gap",
                            "split_loaded",
                            source,
                            note,
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
                out.labels.append(_label(sid, d, d, "market_fact", kind, source, note))
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
                )
            )


def build(db: Database) -> LabelExport:
    """Every label the warehouse can state, in a stable order."""
    out = LabelExport()
    repaired = _price_overrides(db, out)
    _action_overrides(db, out, repaired)
    _judged_flags(db, out, repaired)
    out.labels.sort(
        key=lambda lab: (int(lab["series"]), lab["first"], lab["kind"], lab["source"])
    )
    out.restore.sort(key=lambda r: (int(r["series"]), r["date"]))
    return out


def write(export: LabelExport, labels_path: Path, restore_path: Optional[Path]) -> None:
    labels_path = Path(labels_path)
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    with labels_path.open("w", encoding="utf-8") as fh:
        for label in export.labels:
            fh.write(json.dumps(label, separators=(",", ":"), ensure_ascii=False))
            fh.write("\n")
    if restore_path is not None:
        restore_path = Path(restore_path)
        restore_path.parent.mkdir(parents=True, exist_ok=True)
        with restore_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(("series", "date", *_BAR_FIELDS))
            for r in export.restore:
                writer.writerow([r["series"], r["date"], *(r[f] for f in _BAR_FIELDS)])

"""Week adjustments: one-week facts entered by payroll staff (never rules)."""
from __future__ import annotations

from ..db.conn import audit
from ..money import D
from ..payroll.calculate import DEBTS

KINDS = ("new_advance", "room_charge", "flight_charge", "other_charge", "collection", "defer", "protected_check",
         "manual_hold", "cash_credit", "reserve")
METHODS = ("", "all", "fixed", "percent", "none")


def _finalized(c, week: str) -> bool:
    """A finalized week is closed to edits unless a correction revision is open."""
    final = c.execute("select 1 from payroll_runs where week=%s and status='finalized'", (week,)).fetchone()
    corr = c.execute("select 1 from payroll_runs where week=%s and status='calculated' and supersedes is not null", (week,)).fetchone()
    return bool(final and not corr)


def add(c, week: str, person: str, kind: str, actor: str, authorized_by: str, amount=0, method: str = "", value=0,
        debts: list[str] | None = None, creditor: str = "EMG", label: str = "", note: str = "", external_ref: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"Unknown adjustment kind {kind}")
    if method not in METHODS:
        raise ValueError(f"Unknown collection method {method}")
    if any(d not in DEBTS for d in debts or []):
        raise ValueError(f"Debts must be among {DEBTS}")
    if not authorized_by:
        raise ValueError("Record who authorized this adjustment")
    if D(amount) < 0:
        raise ValueError("Amounts are positive; the kind decides the direction")
    if kind in ("new_advance", "room_charge", "flight_charge", "other_charge", "protected_check", "cash_credit", "reserve") and not D(amount):
        raise ValueError(f"{kind} needs an amount")
    if _finalized(c, week):
        raise ValueError(f"{week} is finalized; start a correction or add it to the next open week")
    if not c.execute("select 1 from people where name=%s", (person,)).fetchone():
        raise ValueError(f"{person!r} is not a known person; add them or an alias first")
    if external_ref:
        dup = c.execute("select week, person, amount from week_adjustments where kind=%s and external_ref=%s and voided_at is null",
                        (kind, external_ref)).fetchone()
        if dup:
            raise ValueError(f"Duplicate: {kind} with reference {external_ref} already recorded for {dup['person']} in {dup['week']} ({dup['amount']})")
    row = c.execute(
        "insert into week_adjustments(week,person,kind,amount,method,value,debts,creditor,label,note,authorized_by,external_ref,created_by)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (week, person, kind, D(amount), method or None, D(value), debts or [], creditor or "EMG", label or None, note or None,
         authorized_by, external_ref, actor),
    ).fetchone()
    audit(c, actor, "adjustment.add", "week_adjustment", str(row["id"]), None, {k: str(v) for k, v in row.items()}, note)
    return row


def void(c, adj_id, actor: str, reason: str) -> None:
    row = c.execute("select * from week_adjustments where id=%s for update", (adj_id,)).fetchone()
    if not row or row["voided_at"]:
        raise ValueError("Adjustment not found or already voided")
    if _finalized(c, row["week"]):
        raise ValueError("The week is finalized; start a correction first")
    c.execute("update week_adjustments set voided_by=%s, voided_at=now() where id=%s", (actor, adj_id))
    audit(c, actor, "adjustment.void", "week_adjustment", str(adj_id), None, None, reason)


def list_week(c, week: str, include_void: bool = False) -> list[dict]:
    q = "select * from week_adjustments where week=%s" + ("" if include_void else " and voided_at is null") + " order by person, kind"
    return c.execute(q, (week,)).fetchall()

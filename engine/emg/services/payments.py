"""Payments move separately from calculated payroll: calculated -> submitted -> confirmed (or failed/void).

Sending a statement email never changes a payment. Confirmation records the
provider reference (e.g. Paychex batch id)."""
from __future__ import annotations

from ..db.conn import audit

TRANSITIONS = {
    "calculated": {"submitted", "void"},
    "submitted": {"confirmed", "failed", "void"},
    "failed": {"submitted", "void"},
    "confirmed": set(),
    "void": set(),
}


def set_state(c, payment_id, state: str, actor: str, provider_ref: str | None = None, note: str | None = None) -> dict:
    p = c.execute("select * from payments where id=%s for update", (payment_id,)).fetchone()
    if not p:
        raise LookupError("payment not found")
    if p["state"] == state:
        return p  # idempotent retry
    if state not in TRANSITIONS[p["state"]]:
        raise ValueError(f"Cannot move a {p['state']} payment to {state}")
    run = c.execute("select status from payroll_runs where id=%s", (p["run_id"],)).fetchone()
    if run["status"] != "finalized" and state in ("submitted", "confirmed"):
        raise ValueError("Only payments of the finalized revision can be submitted or confirmed")
    if state == "confirmed" and not (provider_ref or p["provider_ref"]):
        raise ValueError("Confirmation needs the payroll provider reference")
    row = c.execute("update payments set state=%s, provider_ref=coalesce(%s, provider_ref), updated_by=%s, updated_at=now() where id=%s returning *",
                    (state, provider_ref, actor, payment_id)).fetchone()
    audit(c, actor, f"payment.{state}", "payment", str(payment_id), {"state": p["state"]}, {"state": state, "provider_ref": row["provider_ref"]}, note)
    return row


def bulk(c, run_id, state: str, actor: str, provider_ref: str | None = None) -> int:
    ids = [r["id"] for r in c.execute("select id from payments where run_id=%s and state <> %s and state <> 'void'", (run_id, state))]
    for i in ids:
        set_state(c, i, state, actor, provider_ref)
    return len(ids)


def for_run(c, run_id) -> list[dict]:
    return c.execute("select * from payments where run_id=%s order by upper(person)", (run_id,)).fetchall()

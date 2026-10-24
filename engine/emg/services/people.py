"""People (payroll identities), aliases and explicit merges.

Aliases live in the rule set (they are versioned with the rules and can be
scoped to one vendor or limited to a week range for temporary ownership).
Ledgers are never combined implicitly: two names are one person only after an
explicit, audited merge.
"""
from __future__ import annotations

from ..db.conn import audit


def upsert(c, name: str, actor: str, kind: str = "agent", email: str | None = None, email_verified: bool = False,
           email_excluded: bool = False, email_status: str | None = None, notes: str | None = None) -> dict:
    before = c.execute("select * from people where name=%s", (name,)).fetchone()
    row = c.execute(
        "insert into people(name,kind,email,email_verified,email_excluded,email_status,notes) values (%s,%s,%s,%s,%s,%s,%s)"
        " on conflict (name) do update set kind=excluded.kind, email=coalesce(excluded.email, people.email),"
        " email_verified=excluded.email_verified, email_excluded=excluded.email_excluded,"
        " email_status=coalesce(excluded.email_status, people.email_status), notes=coalesce(excluded.notes, people.notes) returning *",
        (name, kind, email, email_verified, email_excluded, email_status, notes),
    ).fetchone()
    if before != row:
        audit(c, actor, "person.upsert", "person", name, before and {k: str(v) for k, v in before.items()}, {k: str(v) for k, v in row.items()})
    return row


def merge(c, source: str, target: str, actor: str, reason: str) -> int:
    """Explicitly merge ``source`` into ``target``: moves ledger balances with transfer entries (both sides kept)."""
    if not reason:
        raise ValueError("A merge needs a reason")
    if source == target:
        raise ValueError("Cannot merge a person into themselves")
    for n in (source, target):
        if not c.execute("select 1 from people where name=%s", (n,)).fetchone():
            raise LookupError(f"{n} not found")
    from . import ledger

    n = 0
    for b in ledger.balances(c, source):
        wk = b["last_week"]
        n += ledger.post(c, source, b["debt_type"], wk, "adjustment", -b["balance"], f"merge:{source}->{target}:{b['debt_type']}:{b['creditor']}", actor, b["creditor"], note=f"Merged into {target}: {reason}")
        n += ledger.post(c, target, b["debt_type"], wk, "adjustment", b["balance"], f"merge-in:{source}->{target}:{b['debt_type']}:{b['creditor']}", actor, b["creditor"], note=f"Merged from {source}: {reason}")
    c.execute("update people set merged_into=%s, status='merged' where name=%s", (target, source))
    audit(c, actor, "person.merge", "person", source, None, {"into": target}, reason)
    return n


def roster(c) -> list[dict]:
    return c.execute("select * from people order by upper(name)").fetchall()

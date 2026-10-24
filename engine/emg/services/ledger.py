"""Debt ledgers. One account per (person, debt type, creditor); balance = sum(amount).

Sign: +amount increases what the person owes EMG (or another creditor);
-amount reduces it. Money EMG is holding for a person (unapplied holds,
clawback reserves) is negative: EMG owes it back.

Entries are only appended. A correction appends reversals of the original
run's entries and then posts the corrected run, so history stays readable.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ..money import D, cents
from ..payroll.calculate import Adjustment, Opening, PayrollResult

ZERO = Decimal("0")


def post(c, person: str, debt_type: str, week: str, entry_type: str, amount, idem: str, actor: str, creditor: str = "EMG",
         run_id=None, reference: str | None = None, note: str | None = None) -> bool:
    amount = cents(D(amount))
    if amount == 0:
        return False
    row = c.execute(
        "insert into ledger_entries(person,debt_type,creditor,week,run_id,entry_type,amount,reference,note,idempotency_key,created_by)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (idempotency_key) do nothing returning id",
        (person, debt_type, creditor, week, run_id, entry_type, amount, reference, note, idem, actor),
    ).fetchone()
    return row is not None


def openings(c, week: str) -> dict[str, Opening]:
    """Balances from every entry dated before ``week`` (labels sort chronologically)."""
    out: dict[str, Opening] = {}
    for r in c.execute("select person, debt_type, creditor, sum(amount) bal from ledger_entries where week < %s group by 1,2,3 having sum(amount) <> 0", (week,)):
        op = out.setdefault(r["person"], Opening())
        t, bal = r["debt_type"], r["bal"]
        if t == "advance":
            op.advance[r["creditor"]] = op.advance.get(r["creditor"], ZERO) + bal
        elif t in ("chargeback", "room", "flight", "other"):
            setattr(op, t, getattr(op, t) + bal)
    return out


def balances(c, person: str | None = None, as_of_week: str | None = None) -> list[dict]:
    q = "select person, debt_type, creditor, sum(amount) balance, max(week) last_week from ledger_entries where true"
    args: list = []
    if person:
        q += " and person=%s"
        args.append(person)
    if as_of_week:
        q += " and week <= %s"
        args.append(as_of_week)
    q += " group by 1,2,3 having sum(amount) <> 0 order by 1,2,3"
    return c.execute(q, args).fetchall()


def history(c, person: str) -> list[dict]:
    return c.execute("select * from ledger_entries where person=%s order by week, id", (person,)).fetchall()


def seed_openings(c, data: dict[str, dict], week: str, actor: str, source: str) -> int:
    """Load closing balances of ``week`` (e.g. from the control workbook) as opening entries."""
    n = 0
    for person, v in data.items():
        items = [("chargeback", "EMG", v.get("chargeback")), ("room", "EMG", v.get("room")), ("flight", "EMG", v.get("flight")),
                 ("other", "EMG", v.get("other"))]
        items += [("advance", cred, amt) for cred, amt in (v.get("advance") or {}).items()]
        if D(v.get("reserve")):
            items.append(("reserve", "EMG", -D(v["reserve"])))
        for t, cred, amt in items:
            n += post(c, person, t, week, "opening", amt, f"opening:{week}:{person}:{t}:{cred}", actor, cred,
                      reference=source, note=v.get("advance_note") if t == "advance" else None)
    return n


def post_run(c, run_id, week: str, result: PayrollResult, adjustments: list[Adjustment], actor: str) -> int:
    """Assessed and deducted entries for a finalized run."""
    n = 0
    new_adv: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    releases: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for a in adjustments:
        if a.kind == "new_advance":
            new_adv[(a.person, a.creditor)] += a.amount
        if a.kind == "cash_credit" and a.label == "hold_release":
            releases[a.person] += a.amount
    for p in result.people:
        k = f"run:{run_id}:{p.payee}"

        def put(t, entry, amt, cred="EMG", note=None):
            nonlocal n
            n += post(c, p.payee, t, week, entry, amt, f"{k}:{t}:{cred}:{entry}", actor, cred, run_id, note=note)

        put("chargeback", "assessed", p.new_chargebacks)
        put("chargeback", "deducted", -p.chargeback_recovery)
        for t in ("room", "flight", "other"):
            put(t, "assessed", getattr(p, f"{t}_new"))
            put(t, "deducted", -getattr(p, f"{t}_deduction"))
        for (person, cred), amt in new_adv.items():
            if person == p.payee:
                put("advance", "assessed", amt, cred)
        for cred, d in p.advance_by_creditor.items():
            put("advance", "deducted", -D(d["deducted"]), cred)
        put("unapplied_hold", "assessed", -p.manual_hold, note="Pay withheld, not applied to any debt")
        put("unapplied_hold", "deducted", releases.get(p.payee, ZERO), note="Held pay released")
        put("reserve", "assessed", -p.reserve_withheld, note="Clawback reserve withheld")
    return n


def reverse_run(c, run_id, actor: str, reason: str) -> int:
    n = 0
    for e in c.execute("select * from ledger_entries where run_id=%s and entry_type <> 'reversal' order by id", (run_id,)).fetchall():
        n += post(c, e["person"], e["debt_type"], e["week"], "reversal", -e["amount"], f"reversal:{e['id']}", actor, e["creditor"],
                  run_id, reference=str(e["id"]), note=reason)
    return n

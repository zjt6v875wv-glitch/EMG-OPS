"""Weekly payroll waterfall.

Order (per person):
 1. Current agent commission.
 2. Manager overrides and personal premiums.
 3. Prior unpaid chargebacks plus new verified chargebacks.
 4. Chargeback recovery from available earnings.
 5. Holds, then authorized deductions in the configured order
    (default: room, advance, flight, other), each capped at what is owed.
 6. Final check, never below zero; cash credits (refunds, reimbursements,
    bonuses) are added after deductions and are not commission.
 7. Remaining balances carry to the next week, each debt type separately.

Week adjustments are one-week facts (a new advance, a room charge, a hold, a
protected check). They are stored with their week and never become rules.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from ..model import ChargebackLine, EarningLine, ZERO
from ..money import D, cents
from ..rules.model import CollectionRule, RuleSet

DEBTS = ("room", "advance", "flight", "other")
DEFAULT_COLLECTION = {"room": "all", "advance": "none", "flight": "none", "other": "all"}


@dataclass
class Adjustment:
    """kinds:
    new_advance, room_charge, flight_charge, other_charge  (amount, creditor)
    collection   (method all|fixed|percent|none, value, debts)  -- this week only
    defer        (debts)                                      -- skip collection this week
    protected_check (amount)                                  -- minimum check this week
    manual_hold  (amount, or label="all" to hold all available pay; reason)
    cash_credit  (amount, label: refund|reimbursement|bonus|creditor_credit)
    reserve      (amount) clawback reserve withheld this week
    """

    kind: str
    person: str
    amount: Decimal = ZERO
    method: str = ""
    value: Decimal = ZERO
    debts: list[str] = field(default_factory=list)
    creditor: str = "EMG"
    label: str = ""
    note: str = ""
    authorization: str = ""

    @staticmethod
    def from_dict(d: dict) -> "Adjustment":
        return Adjustment(
            kind=d["kind"], person=d["person"], amount=D(d.get("amount")), method=d.get("method", ""),
            value=D(d.get("value")), debts=d.get("debts", []), creditor=d.get("creditor") or "EMG",
            label=d.get("label", ""), note=d.get("note", ""), authorization=d.get("authorization", ""),
        )


@dataclass
class Opening:
    chargeback: Decimal = ZERO
    advance: dict[str, Decimal] = field(default_factory=dict)  # creditor -> balance
    room: Decimal = ZERO
    flight: Decimal = ZERO
    other: Decimal = ZERO

    @property
    def advance_total(self) -> Decimal:
        return sum(self.advance.values(), ZERO)


@dataclass
class PersonResult:
    payee: str
    agent_commission: Decimal = ZERO
    manager_override: Decimal = ZERO
    gross_compensation: Decimal = ZERO
    opening_chargeback: Decimal = ZERO
    new_chargebacks: Decimal = ZERO
    held_chargebacks: Decimal = ZERO
    chargeback_recovery: Decimal = ZERO
    ending_chargeback: Decimal = ZERO
    manual_hold: Decimal = ZERO
    reserve_withheld: Decimal = ZERO
    room_opening: Decimal = ZERO
    room_new: Decimal = ZERO
    room_deduction: Decimal = ZERO
    room_ending: Decimal = ZERO
    advance_opening: Decimal = ZERO
    new_advance: Decimal = ZERO
    advance_deduction: Decimal = ZERO
    advance_ending: Decimal = ZERO
    advance_by_creditor: dict[str, dict[str, str]] = field(default_factory=dict)
    flight_opening: Decimal = ZERO
    flight_new: Decimal = ZERO
    flight_deduction: Decimal = ZERO
    flight_ending: Decimal = ZERO
    other_opening: Decimal = ZERO
    other_new: Decimal = ZERO
    other_deduction: Decimal = ZERO
    other_ending: Decimal = ZERO
    protected_check: Decimal = ZERO
    cash_credits: Decimal = ZERO
    commission_payment: Decimal = ZERO
    final_check: Decimal = ZERO
    status: str = "NO CHECK"
    collection_basis: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


@dataclass
class PayrollResult:
    week: str
    people: list[PersonResult]
    lines: list[EarningLine]
    chargebacks: list[ChargebackLine]
    totals: dict[str, Any]

    def person(self, name: str) -> PersonResult | None:
        return next((p for p in self.people if p.payee == name), None)


def _collection_for(person: str, debt: str, week: str, rules: list[CollectionRule], adjustments: list[Adjustment]) -> tuple[str, Decimal, str]:
    """Return (method, value, basis) for a debt. Week adjustments beat standing rules."""
    for a in adjustments:
        if a.person == person and a.kind == "defer" and (not a.debts or debt in a.debts):
            return "none", ZERO, f"deferred this week: {a.note or a.authorization}"
    for a in adjustments:
        if a.person == person and a.kind == "collection" and (not a.debts or debt in a.debts):
            return a.method or "all", a.value, f"week instruction: {a.note or a.authorization}"
    for r in rules:
        if r.person == person and r.live(week) and debt in r.debts:
            return r.method, r.value, f"standing rule: {r.note}"
    return DEFAULT_COLLECTION[debt], ZERO, "default"


def _take(method: str, value: Decimal, available: Decimal, owed: Decimal) -> Decimal:
    if owed <= 0 or available <= 0:
        return ZERO
    if method == "all":
        amt = owed
    elif method == "fixed":
        amt = min(value, owed)
    elif method == "percent":
        amt = cents(available * value / Decimal(100))
    else:
        return ZERO
    return cents(max(ZERO, min(amt, available, owed)))


def run_payroll(
    rs: RuleSet,
    week: str,
    lines: list[EarningLine],
    openings: dict[str, Opening],
    chargebacks: list[ChargebackLine],
    adjustments: list[Adjustment],
    badge_holds: set[str] | None = None,
    company_payees: set[str] | None = None,
) -> PayrollResult:
    badge_holds = badge_holds or set()
    company_payees = company_payees or {r.payee for r in rs.routing}
    agent = defaultdict(lambda: ZERO)
    other = defaultdict(lambda: ZERO)
    for ln in lines:
        if ln.payee in company_payees:
            continue
        if ln.activity == "Agent Commission":
            agent[ln.payee] += ln.amount
        else:
            other[ln.payee] += ln.amount
    new_cb = defaultdict(lambda: ZERO)
    held_cb = defaultdict(lambda: ZERO)
    for cb in chargebacks:
        (new_cb if cb.verified else held_cb)[cb.payee] += cb.amount

    people = set(agent) | set(other) | set(new_cb) | set(held_cb) | set(openings) | {a.person for a in adjustments}
    people -= company_payees
    order = [d for d in rs.deduction_order if d in DEBTS]
    results = []
    for n in sorted(people, key=lambda s: s.upper()):
        op = openings.get(n, Opening())
        adj = [a for a in adjustments if a.person == n]
        r = PersonResult(payee=n)
        r.agent_commission = cents(agent[n])
        r.manager_override = cents(other[n])
        r.gross_compensation = r.agent_commission + r.manager_override
        r.opening_chargeback = cents(op.chargeback)
        r.new_chargebacks = cents(new_cb[n])
        r.held_chargebacks = cents(held_cb[n])
        owed_cb = r.opening_chargeback + r.new_chargebacks
        r.chargeback_recovery = cents(max(ZERO, min(r.gross_compensation, owed_cb)))
        r.ending_chargeback = owed_cb - r.chargeback_recovery
        available = r.gross_compensation - r.chargeback_recovery

        # Holds: badge holds and manual holds withhold pay without applying it to any debt.
        hold_all = n in badge_holds or any(a.kind == "manual_hold" and a.label == "all" for a in adj)
        hold_amt = sum((a.amount for a in adj if a.kind == "manual_hold" and a.label != "all"), ZERO)
        r.manual_hold = cents(available if hold_all else min(available, hold_amt))
        available -= r.manual_hold
        reserve = sum((a.amount for a in adj if a.kind == "reserve"), ZERO)
        r.reserve_withheld = cents(min(available, reserve))
        available -= r.reserve_withheld

        protected = max((a.amount for a in adj if a.kind == "protected_check"), default=ZERO)
        r.protected_check = cents(protected)

        r.room_opening = cents(op.room)
        r.room_new = cents(sum((a.amount for a in adj if a.kind == "room_charge"), ZERO))
        r.flight_opening = cents(op.flight)
        r.flight_new = cents(sum((a.amount for a in adj if a.kind == "flight_charge"), ZERO))
        r.other_opening = cents(op.other)
        r.other_new = cents(sum((a.amount for a in adj if a.kind == "other_charge"), ZERO))
        adv = dict(op.advance)
        for a in adj:
            if a.kind == "new_advance":
                adv[a.creditor] = adv.get(a.creditor, ZERO) + a.amount
        r.advance_opening = cents(op.advance_total)
        r.new_advance = cents(sum((a.amount for a in adj if a.kind == "new_advance"), ZERO))
        owed = {
            "room": r.room_opening + r.room_new,
            "advance": r.advance_opening + r.new_advance,
            "flight": r.flight_opening + r.flight_new,
            "other": r.other_opening + r.other_new,
        }
        taken = {d: ZERO for d in DEBTS}
        for debt in order:
            method, value, basis = _collection_for(n, debt, week, rs.collections, adj)
            r.collection_basis[debt] = f"{method} ({basis})" if owed[debt] else ""
            room_left = max(ZERO, available - r.protected_check)
            amt = _take(method, value, room_left, owed[debt])
            taken[debt] = amt
            available -= amt
        r.room_deduction, r.advance_deduction, r.flight_deduction, r.other_deduction = (taken[d] for d in DEBTS)
        r.room_ending = owed["room"] - r.room_deduction
        r.advance_ending = owed["advance"] - r.advance_deduction
        r.flight_ending = owed["flight"] - r.flight_deduction
        r.other_ending = owed["other"] - r.other_deduction
        # Allocate the advance deduction across creditors in a stable order (EMG first).
        left = r.advance_deduction
        for cred in sorted(adv, key=lambda c: (c != "EMG", c)):
            take = min(left, adv[cred])
            r.advance_by_creditor[cred] = {"owed": str(cents(adv[cred])), "deducted": str(cents(take)), "ending": str(cents(adv[cred] - take))}
            left -= take

        r.commission_payment = cents(max(ZERO, available))
        r.cash_credits = cents(sum((a.amount for a in adj if a.kind == "cash_credit"), ZERO))
        r.final_check = r.commission_payment + r.cash_credits
        r.status = "PAYABLE" if r.final_check > 0 else ("HELD" if r.manual_hold > 0 else "NO CHECK")
        r.notes = [f"{a.kind}: {a.note or a.authorization}" for a in adj if a.note or a.authorization]
        assert r.final_check >= 0 and r.advance_ending >= 0 and r.ending_chargeback >= 0
        results.append(r)

    company = defaultdict(lambda: ZERO)
    for ln in lines:
        if ln.payee in company_payees:
            company[ln.payee] += ln.amount
    totals = {
        "gross_compensation": sum((p.gross_compensation for p in results), ZERO),
        "chargeback_recovery": sum((p.chargeback_recovery for p in results), ZERO),
        "new_chargebacks": sum((p.new_chargebacks for p in results), ZERO),
        "held_chargebacks": sum((p.held_chargebacks for p in results), ZERO),
        "manual_holds": sum((p.manual_hold for p in results), ZERO),
        "room_deductions": sum((p.room_deduction for p in results), ZERO),
        "advance_deductions": sum((p.advance_deduction for p in results), ZERO),
        "flight_deductions": sum((p.flight_deduction for p in results), ZERO),
        "other_deductions": sum((p.other_deduction for p in results), ZERO),
        "cash_credits": sum((p.cash_credits for p in results), ZERO),
        "total_checks": sum((p.final_check for p in results), ZERO),
        "positive_checks": sum(1 for p in results if p.final_check > 0),
        "company_payables": {k: cents(v) for k, v in company.items()},
    }
    # Every dollar of gross is accounted for: recovered, held, deducted, or paid.
    lhs = totals["gross_compensation"] + totals["cash_credits"]
    rhs = (totals["chargeback_recovery"] + totals["manual_holds"] + sum((p.reserve_withheld for p in results), ZERO)
           + totals["room_deductions"] + totals["advance_deductions"] + totals["flight_deductions"] + totals["other_deductions"] + totals["total_checks"])
    assert abs(lhs - rhs) < Decimal("0.005"), (lhs, rhs)
    return PayrollResult(week, results, lines, chargebacks, totals)

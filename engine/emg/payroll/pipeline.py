"""End-to-end weekly calculation used by the app, the CLI and replay tests.

inputs -> commissions -> chargeback matching -> waterfall -> result bundle
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..model import ChargebackLine, CompanyAdjustment, EarningLine, ReviewItem, SaleRow, ZERO
from ..money import D, cents
from ..names import key
from ..rules.model import RuleSet
from ..weeks import pay_date, parse_week
from .calculate import Adjustment, Opening, PayrollResult, run_payroll
from .commissions import apply_assignments, calculate_commissions, resolve_seller


@dataclass
class PayoutEvidence:
    """A documented historical payout that a chargeback may reverse exactly."""

    vendor: str
    enrollment_id: str
    payee: str
    amount: Decimal
    rate: Decimal = ZERO
    activity: str = ""
    week: str = ""
    proof: str = ""


@dataclass
class WeekInputs:
    week: str
    rows: list[SaleRow]
    openings: dict[str, Opening]
    adjustments: list[Adjustment] = field(default_factory=list)
    payout_history: list[PayoutEvidence] = field(default_factory=list)
    processed_chargeback_refs: set[str] = field(default_factory=set)
    prior_paid_keys: set[tuple[str, str]] = field(default_factory=set)  # (vendor, enrollment) already paid to an agent


@dataclass
class WeekBundle:
    week: str
    pay_date: str
    result: PayrollResult
    lines: list[EarningLine]
    chargebacks: list[ChargebackLine]
    review: list[ReviewItem]
    nonpayable: list[SaleRow]
    pending: list[SaleRow]
    company_adjustments: list[CompanyAdjustment]
    assignments: list[dict]
    vendor_totals: dict[str, Decimal]
    reconciliation: dict[str, Any]

    @property
    def blocking_review(self) -> list[ReviewItem]:
        return [r for r in self.review if r.blocking]


def openings_from_json(data: dict) -> dict[str, Opening]:
    out = {}
    for n, v in data.items():
        out[n] = Opening(
            chargeback=D(v.get("chargeback")),
            advance={c: D(a) for c, a in (v.get("advance") or {}).items()},
            room=D(v.get("room")), flight=D(v.get("flight")), other=D(v.get("other")),
        )
    return out


def match_chargebacks(rs: RuleSet, inputs: WeekInputs, res_review: list[ReviewItem], company: list[CompanyAdjustment]) -> list[ChargebackLine]:
    """Vendor chargebacks become payroll reversals only with payout evidence.

    - Already-processed references are skipped (no double deduction).
    - Duplicate-payment reversals with exactly one historical agent payout are
      company-only: the agent was paid once, legitimately.
    - With documented payouts, every payee (agent and managers) is reversed by
      the exact historical amount.
    - Without evidence the chargeback is held for review; nothing is guessed.
    """
    by_ref = defaultdict(list)
    for p in inputs.payout_history:
        by_ref[(p.vendor, p.enrollment_id)].append(p)
    out: list[ChargebackLine] = []
    for s in inputs.rows:
        if s.line_type != "chargeback":
            continue
        if s.enrollment_id in inputs.processed_chargeback_refs:
            continue
        hist = by_ref.get((s.vendor, s.enrollment_id), [])
        agent_hist = [h for h in hist if "AGENT" in key(h.activity) or not h.activity]
        if "DUPLICATE PAYMENT" in key(s.status):
            if len(agent_hist) == 1:
                company.append(CompanyAdjustment(s.vendor, s.enrollment_id, s.vendor_amount,
                                                 f"Vendor duplicate-payment reversal; one original agent payout found ({agent_hist[0].week}); no payroll reversal", s.customer))
            else:
                res_review.append(ReviewItem("unmatched_chargeback", f"Duplicate-payment reversal with {len(agent_hist)} agent payouts on record", s.vendor, s.enrollment_id, s.raw_rep, s.vendor_amount))
            continue
        seller = resolve_seller(rs, s.raw_rep, s.sale_date, s.vendor, inputs.week)[0]
        if not hist:
            res_review.append(ReviewItem("unmatched_chargeback", "Original payout not verified; held, no payroll deduction", s.vendor, s.enrollment_id, seller, s.vendor_amount, blocking=False))
            out.append(ChargebackLine(seller, s.vendor, s.enrollment_id, ZERO, seller, s.customer, s.utility, s.product, s.kwh, ZERO, False, "", s.sale_date, "HELD - original payout not verified"))
            continue
        for h in hist:
            out.append(ChargebackLine(h.payee, s.vendor, s.enrollment_id, h.amount, seller, s.customer, s.utility, s.product,
                                      s.kwh, h.rate, True, h.week, s.sale_date, f"Exact historical reversal: {h.proof}"))
    return out


def calculate_week(rs: RuleSet, inputs: WeekInputs) -> WeekBundle:
    week = inputs.week
    year = parse_week(week)[0]
    payable = [r for r in inputs.rows if r.line_type == "payable"]
    # Duplicate guard: one agent payout per (vendor, enrollment), across this file set and history.
    seen: dict[tuple[str, str], SaleRow] = {}
    review: list[ReviewItem] = []
    unique = []
    for r in payable:
        if r.key in seen:
            review.append(ReviewItem("duplicate", f"Enrollment appears twice as payable ({seen[r.key].source_file} row {seen[r.key].source_row} and {r.source_file} row {r.source_row})", r.vendor, r.enrollment_id, r.raw_rep, r.vendor_amount))
            continue
        if r.key in inputs.prior_paid_keys:
            review.append(ReviewItem("duplicate", "Enrollment was already paid in a prior finalized week; held for review", r.vendor, r.enrollment_id, r.raw_rep, r.vendor_amount))
            continue
        seen[r.key] = r
        unique.append(r)
    for r in inputs.rows:
        if r.line_type == "review":
            review.append(ReviewItem("needs_review", r.notes or "Row needs review", r.vendor, r.enrollment_id, r.raw_rep, r.vendor_amount))
    comm = calculate_commissions(rs, unique, week)
    review.extend(comm.review)
    assignments = apply_assignments(rs, comm.lines, week)

    company: list[CompanyAdjustment] = []
    for s in inputs.rows:
        if s.line_type == "adjustment":
            company.append(CompanyAdjustment(s.vendor, s.enrollment_id, s.vendor_amount, "Company-held rate/kWh adjustment", s.customer))
    for s in comm.company_only:
        company.append(CompanyAdjustment(s.vendor, s.enrollment_id, s.vendor_amount, s.notes or "Company-only installment", s.customer))
    chargebacks = match_chargebacks(rs, inputs, review, company)

    paid = {(l.vendor, l.enrollment_id) for l in comm.lines if l.activity in ("Agent Commission", "Company Payable")}
    nonpayable = [r for r in inputs.rows if r.line_type == "nonpayable" and (r.vendor, r.enrollment_id) not in paid
                  and (not r.sale_date or r.sale_date.year == year)]
    for r in nonpayable:
        seller = resolve_seller(rs, r.raw_rep, r.sale_date, r.vendor, week)[0]
        r.raw.setdefault("seller", seller)
        route = next((x for x in rs.routing if x.live(week) and x.vendor == r.vendor and seller in x.sellers), None)
        if route:
            r.raw["company_payee"] = route.payee  # reported to the company (e.g. AEG), not to EMG individuals
        elif seller not in rs.people and not rs.aliases.known(r.raw_rep, r.vendor) and not any(m.agent == seller for m in rs.memberships):
            review.append(ReviewItem("unknown_agent", f"Agent name {r.raw_rep!r} on a non-payable row is not in the alias table or roster", r.vendor, r.enrollment_id, seller, blocking=False))
    pending = [r for r in inputs.rows if r.line_type == "pending"]

    result = run_payroll(rs, week, comm.lines, inputs.openings, chargebacks, inputs.adjustments, comm.holds)

    vendor_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in inputs.rows:
        if r.line_type in ("payable", "company_only", "adjustment", "chargeback", "review"):
            vendor_totals[r.vendor] += r.vendor_amount
    pay_by_vendor: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for l in comm.lines:
        pay_by_vendor[l.vendor] += l.amount
    reconciliation = {
        v: {"vendor_revenue": str(cents(vendor_totals[v])), "payroll_cost": str(cents(pay_by_vendor[v])),
            "company_retained": str(cents(vendor_totals[v] - pay_by_vendor[v]))}
        for v in sorted(set(vendor_totals) | set(pay_by_vendor))
    }
    return WeekBundle(week, pay_date(week).isoformat(), result, comm.lines, chargebacks, review, nonpayable, pending, company,
                      assignments, dict(vendor_totals), reconciliation)

"""Turn payable sale rows into earning lines using a rule set.

Nothing is dropped silently: a row that cannot be priced or attributed
becomes a ReviewItem and is kept in the review queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..model import EarningLine, ReviewItem, SaleRow, ZERO
from ..names import key
from ..rules.model import AgentRate, OverrideRule, RuleSet


@dataclass
class CommissionResult:
    lines: list[EarningLine] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    holds: set[str] = field(default_factory=set)  # people whose badge rule says HOLD
    company_only: list[SaleRow] = field(default_factory=list)


def resolve_seller(rs: RuleSet, raw_rep: str, sale_date: date | None, vendor: str, week: str) -> tuple[str, str | None, BadgeRuleHit | None]:
    """Apply date-based badge ownership, then the alias table."""
    for b in rs.badges:
        if not b.live(week) or key(b.badge) != key(raw_rep):
            continue
        if sale_date is None:
            return rs.canon(raw_rep, vendor), "Badge ownership needs a sale date", None
        d = sale_date.isoformat()
        if b.start_date and d < str(b.start_date)[:10]:
            continue
        if b.end_date and d > str(b.end_date)[:10]:
            continue
        return rs.canon(b.actual_seller, vendor), None, BadgeRuleHit(b.badge, b.actual_seller, b.handling)
    return rs.canon(raw_rep, vendor), None, None


@dataclass
class BadgeRuleHit:
    badge: str
    seller: str
    handling: str


def _pick(candidates: list, what: str):
    if not candidates:
        return None, None
    best = max(c.specificity() for c in candidates)
    top = [c for c in candidates if c.specificity() == best]
    if len(top) > 1 and len({c.rate for c in top}) > 1:
        return None, f"Ambiguous {what}: " + ", ".join(f"{c.id or c.note}={c.rate}" for c in top)
    return top[0], None


def agent_rate(rs: RuleSet, vendor: str, product: str, utility: str, seller: str, week: str) -> tuple[AgentRate | None, str | None]:
    cands = [
        r for r in rs.agent_rates
        if r.live(week)
        and r.vendor == vendor
        and (r.product is None or key(r.product) == key(product))
        and (r.agent is None or r.agent == seller)
        and rs.utility_in(r.utility_group, utility)
    ]
    rule, err = _pick(cands, "agent rate")
    if rule is None and err is None:
        err = f"No agent rate for {vendor} / {product} / {utility} / {seller}"
    return rule, err


def _override_candidates(rs: RuleSet, manager: str, vendor: str, product: str, utility: str, seller: str, week: str) -> list[OverrideRule]:
    out = []
    for o in rs.overrides:
        if o.manager != manager or not o.live(week):
            continue
        if o.vendors is not None and vendor not in o.vendors:
            continue
        if o.products is not None and key(product) not in {key(p) for p in o.products}:
            continue
        if not rs.utility_in(o.utility_group, utility):
            continue
        if o.sellers is not None and seller not in o.sellers:
            continue
        out.append(o)
    return out


def personal_targets(rs: RuleSet, person: str, week: str):
    return [p for p in rs.personal_targets if p.person == person and p.live(week)]


def price_sale(rs: RuleSet, s: SaleRow, week: str, res: CommissionResult) -> None:
    vendor_cfg = rs.vendors.get(s.vendor)
    if vendor_cfg is None:
        res.review.append(ReviewItem("unknown_vendor", f"Vendor {s.vendor!r} is not configured", s.vendor, s.enrollment_id, s.raw_rep, s.vendor_amount))
        return
    product = vendor_cfg.product_aliases.get(key(s.product), s.product)
    if vendor_cfg.products and key(product) not in {key(p) for p in vendor_cfg.products}:
        res.review.append(ReviewItem("product_unknown", f"Product {s.product!r} is not an eligible {s.vendor} product; rate review required", s.vendor, s.enrollment_id, s.raw_rep, s.vendor_amount))
        return
    if not s.raw_rep:
        res.review.append(ReviewItem("unknown_agent", "Sale has no selling agent", s.vendor, s.enrollment_id, "", s.vendor_amount))
        return

    seller, err, badge = resolve_seller(rs, s.raw_rep, s.sale_date, s.vendor, week)
    if err:
        res.review.append(ReviewItem("badge_date_missing", err, s.vendor, s.enrollment_id, s.raw_rep, s.vendor_amount))
        return
    if badge and badge.handling.upper() == "HOLD":
        res.holds.add(seller)
    basis = vendor_cfg.basis
    qty = s.kwh if basis == "kwh" else s.units

    def line(payee, activity, rate, rule_id, notes="", rep=seller):
        amount = qty * rate
        res.lines.append(EarningLine(
            payee=payee, activity=activity, vendor=s.vendor, rep=rep, enrollment_id=s.enrollment_id,
            customer=s.customer, utility=s.utility, product=product, kwh=s.kwh if basis == "kwh" else ZERO,
            units=s.units if basis != "kwh" else ZERO, rate=rate, amount=amount, sale_date=s.sale_date,
            transaction_id=s.transaction_id, rule_id=rule_id, source_rep=seller, notes=notes,
        ))

    # Company routing (e.g. AEG repricing): the company is paid, not the individual.
    for r in rs.routing:
        if r.live(week) and r.vendor == s.vendor and seller in r.sellers:
            line(r.payee, "Company Payable", r.rate, r.id, f"{r.payee} pays its own agents; excluded from individual payroll")
            return

    rate_rule, err = agent_rate(rs, s.vendor, product, s.utility, seller, week)
    if rate_rule is None:
        res.review.append(ReviewItem("missing_rate", err or "No rate", s.vendor, s.enrollment_id, seller, s.vendor_amount, data={"product": product, "utility": s.utility}))
        return
    base = rate_rule.rate
    if seller not in rs.people and not rs.aliases.known(s.raw_rep, s.vendor) and not any(m.agent == seller for m in rs.memberships):
        # Not blocking: pay is computed, but the name should be confirmed in the alias table.
        res.review.append(ReviewItem("unknown_agent", f"Agent name {s.raw_rep!r} is not in the alias table or roster", s.vendor, s.enrollment_id, seller, ZERO, blocking=False))
    line(seller, "Agent Commission", base, rate_rule.id)

    targets = personal_targets(rs, seller, week)
    if targets:
        for t in targets:
            if t.vendor == s.vendor and key(t.product) == key(product) and rs.utility_in(t.utility_group, s.utility) and t.target > base:
                line(seller, "Personal Rate Top-Up", t.target - base, f"target:{t.person}:{t.vendor}:{t.product}", "Personal total target")
        if any(t.suppress_overrides for t in targets):
            return

    first_override = len(res.lines)
    managers = {o.manager for o in rs.overrides if o.live(week)}
    for manager in sorted(managers):
        cands = _override_candidates(rs, manager, s.vendor, product, s.utility, seller, week)
        on_team = seller in rs.team(manager, week, s.vendor, product)
        explicit = [c for c in cands if c.sellers is not None]
        team_rules = [c for c in cands if c.sellers is None]
        chosen = None
        if explicit:
            chosen, err = _pick(explicit, f"override for {manager}")
        elif on_team and team_rules:
            chosen, err = _pick(team_rules, f"override for {manager}")
        elif on_team and seller != manager:
            # A current team member with no rule for this product: never pay zero silently.
            if any(o.manager == manager and o.live(week) and o.sellers is None and (o.vendors is None or s.vendor in o.vendors) for o in rs.overrides):
                res.review.append(ReviewItem("missing_override", f"{manager} override missing for {seller}, {s.vendor} {product}, {s.utility}", s.vendor, s.enrollment_id, manager, ZERO, data={"seller": seller}))
            continue
        else:
            continue
        if chosen is None:
            if err:
                res.review.append(ReviewItem("ambiguous_override", err, s.vendor, s.enrollment_id, manager))
            continue
        if chosen.type == "target_remainder":
            rate = max(ZERO, chosen.rate - base)
            if rate == 0:
                continue
            line(manager, "Manager Override", rate, chosen.id, "Remainder to target total")
        else:
            line(manager, "Manager Override", chosen.rate, chosen.id, "Current team override")

    cap = rs.commercial.get("max_total_rate")
    if cap and key(product) == "COMMERCIAL":
        total = base + sum(l.rate for l in res.lines[first_override:])
        if total > Decimal(str(cap)) + Decimal("0.0000000001"):
            res.review.append(ReviewItem("rate_cap", f"Commercial total payout {total}/kWh exceeds cap {cap}", s.vendor, s.enrollment_id, seller))


def apply_assignments(rs: RuleSet, lines: list[EarningLine], week: str) -> list[dict]:
    """Payroll-week sales ownership changes. Debts are never transferred."""
    live = [a for a in rs.assignments if a.live(week)]
    ownership = {rs.canon(a.source_rep): rs.canon(a.actual_seller) for a in live}
    for ln in lines:
        if ln.rep in ownership:
            prior = ln.rep
            ln.source_rep = prior
            ln.rep = ownership[prior]
            if ln.activity == "Agent Commission":
                ln.payee = ownership[prior]
            ln.notes = (ln.notes + f" | {week} sales assignment: {prior} -> {ln.rep}; manager overrides retained").strip(" |")
    return [{"source_rep": a.source_rep, "actual_seller": a.actual_seller, "first_week": a.first_week, "last_week": a.last_week} for a in live]


def calculate_commissions(rs: RuleSet, sales: list[SaleRow], week: str) -> CommissionResult:
    res = CommissionResult()
    for s in sales:
        if s.line_type == "company_only":
            res.company_only.append(s)
            continue
        if s.line_type != "payable":
            continue
        price_sale(rs, s, week, res)
    return res

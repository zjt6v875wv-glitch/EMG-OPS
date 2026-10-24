"""Versioned commission rule set.

Every rule is data. A rule set is loaded from JSON (sample config, the
control-workbook importer) or from the database. Rules carry an inclusive
``first_week``/``last_week`` window, so a one-week exception is a rule (or a
week adjustment) that simply ends that week and cannot leak into later runs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..money import D
from ..names import AliasTable, key
from ..weeks import week_in_range


@dataclass
class UtilityGroup:
    name: str
    contains_any: list[str] = field(default_factory=list)
    endswith_any: list[str] = field(default_factory=list)

    def matches(self, utility: str) -> bool:
        u = key(utility)
        return any(c.upper() in u for c in self.contains_any) or any(u.endswith(e.upper()) for e in self.endswith_any)


@dataclass
class Windowed:
    first_week: str | None = None
    last_week: str | None = None
    status: str = "active"  # active | inactive | historical

    def live(self, week: str) -> bool:
        return self.status == "active" and week_in_range(week, self.first_week, self.last_week)


@dataclass
class AgentRate(Windowed):
    id: str = ""
    vendor: str = ""
    product: str | None = None  # None = any eligible product
    utility_group: str | None = None
    agent: str | None = None  # None = default
    rate: Decimal = Decimal("0")
    basis: str = "kwh"  # kwh | enrollment
    note: str = ""

    def specificity(self) -> int:
        return (100 if self.agent else 0) + (10 if self.utility_group else 0) + (1 if self.product else 0)


@dataclass
class Membership(Windowed):
    manager: str = ""
    agent: str = ""
    vendors: list[str] | None = None  # None = all vendors
    products: list[str] | None = None  # None = all products in scope
    note: str = ""

    def covers(self, vendor: str, product: str) -> bool:
        if self.vendors is not None and vendor not in self.vendors:
            return False
        if self.products is not None and key(product) not in {key(p) for p in self.products}:
            return False
        return True


@dataclass
class OverrideRule(Windowed):
    id: str = ""
    manager: str = ""
    type: str = "fixed"  # fixed | target_remainder | flat
    vendors: list[str] | None = None
    products: list[str] | None = None
    utility_group: str | None = None
    rate: Decimal = Decimal("0")  # $/kWh, target $/kWh, or $/enrollment for flat
    sellers: list[str] | None = None  # explicit seller list; None = manager's team
    note: str = ""

    def specificity(self) -> int:
        return (10 if self.utility_group else 0) + (1 if self.products else 0) + (5 if self.sellers else 0)


@dataclass
class PersonalTarget(Windowed):
    person: str = ""
    vendor: str = ""
    product: str = ""
    target: Decimal = Decimal("0")
    utility_group: str | None = None  # None = any utility
    suppress_overrides: bool = True
    note: str = ""


@dataclass
class RoutingRule(Windowed):
    """Route a seller's enrollments to a company payee (e.g. AEG) at a flat rate."""

    id: str = ""
    vendor: str = ""
    sellers: list[str] = field(default_factory=list)
    payee: str = ""
    rate: Decimal = Decimal("0")
    basis: str = "enrollment"
    exclude_from_individual_payroll: bool = True
    note: str = ""


@dataclass
class BadgeRule(Windowed):
    badge: str = ""
    start_date: str | None = None
    end_date: str | None = None
    actual_seller: str = ""
    handling: str = "PAY"  # PAY | HOLD
    note: str = ""


@dataclass
class TemporaryAssignment(Windowed):
    source_rep: str = ""
    actual_seller: str = ""
    note: str = ""


@dataclass
class CollectionRule(Windowed):
    """How much of available pay may be collected for a person's debts.

    method: all (100% of available, capped at owed) | fixed | percent | none
    debts: which ledgers this rule collects, in order (e.g. advance, flight).
    """

    person: str = ""
    method: str = "all"
    value: Decimal = Decimal("0")
    debts: list[str] = field(default_factory=lambda: ["advance"])
    creditor: str | None = None
    note: str = ""


@dataclass
class VendorConfig:
    name: str
    basis: str = "kwh"
    products: list[str] = field(default_factory=list)
    product_aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class RuleSet:
    version: str
    utility_groups: dict[str, UtilityGroup]
    vendors: dict[str, VendorConfig]
    aliases: AliasTable
    alias_rows: list[dict]
    agent_rates: list[AgentRate]
    memberships: list[Membership]
    overrides: list[OverrideRule]
    personal_targets: list[PersonalTarget]
    routing: list[RoutingRule]
    badges: list[BadgeRule]
    assignments: list[TemporaryAssignment]
    collections: list[CollectionRule]
    commercial: dict[str, Any]
    deduction_order: list[str]
    raw: dict
    people: set[str] = field(default_factory=set)  # roster names (canonical)

    # ---- lookups -------------------------------------------------------
    def group(self, name: str | None) -> UtilityGroup | None:
        if not name:
            return None
        if name not in self.utility_groups:
            raise KeyError(f"Unknown utility group {name!r}")
        return self.utility_groups[name]

    def utility_in(self, group: str | None, utility: str) -> bool:
        g = self.group(group)
        return True if g is None else g.matches(utility)

    def canon(self, raw, vendor: str | None = None) -> str:
        """Alias table first; then a roster person whose normalized name matches
        (repairs mojibake/case/hyphen variants to the roster spelling)."""
        name = self.aliases.resolve(raw, vendor)
        if name in self.people:
            return name
        if not hasattr(self, "_people_by_key"):
            self._people_by_key = {key(p): p for p in self.people}
        return self._people_by_key.get(key(name), name)

    def team(self, manager: str, week: str, vendor: str, product: str) -> set[str]:
        return {
            m.agent
            for m in self.memberships
            if m.manager == manager and m.live(week) and m.covers(vendor, product)
        }

    def managers_of(self, agent: str, week: str, vendor: str, product: str) -> set[str]:
        return {
            m.manager
            for m in self.memberships
            if m.agent == agent and m.live(week) and m.covers(vendor, product)
        }

    def to_json(self) -> dict:
        return self.raw


# ---- loading -------------------------------------------------------------

def _windowed(row: dict) -> dict:
    return {
        "first_week": row.get("first_week"),
        "last_week": row.get("last_week"),
        "status": row.get("status", "active"),
    }


def _dec(row: dict, k: str) -> Decimal:
    return D(row.get(k, 0))


def load_ruleset(data: dict | str | Path) -> RuleSet:
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text())
    groups = {n: UtilityGroup(n, g.get("contains_any", []), g.get("endswith_any", [])) for n, g in data.get("utility_groups", {}).items()}
    vendors = {
        n: VendorConfig(n, v.get("basis", "kwh"), v.get("products", []), v.get("product_aliases", {}))
        for n, v in data.get("vendors", {}).items()
    }
    aliases = AliasTable()
    for a in data.get("aliases", []):
        aliases.add(a["canonical"], a["alias"], a.get("scope"))
    rs = RuleSet(
        version=data.get("version", "unversioned"),
        utility_groups=groups,
        vendors=vendors,
        aliases=aliases,
        alias_rows=data.get("aliases", []),
        agent_rates=[
            AgentRate(**_windowed(r), id=r.get("id", ""), vendor=r["vendor"], product=r.get("product"), utility_group=r.get("utility_group"),
                      agent=r.get("agent"), rate=_dec(r, "rate"), basis=r.get("basis", "kwh"), note=r.get("note", ""))
            for r in data.get("agent_rates", [])
        ],
        memberships=[
            Membership(**_windowed(r), manager=r["manager"], agent=r["agent"], vendors=r.get("vendors"), products=r.get("products"), note=r.get("note", ""))
            for r in data.get("memberships", [])
        ],
        overrides=[
            OverrideRule(**_windowed(r), id=r.get("id", ""), manager=r["manager"], type=r.get("type", "fixed"), vendors=r.get("vendors"),
                         products=r.get("products"), utility_group=r.get("utility_group"), rate=_dec(r, "rate"), sellers=r.get("sellers"), note=r.get("note", ""))
            for r in data.get("override_rules", [])
        ],
        personal_targets=[
            PersonalTarget(**_windowed(r), person=r["person"], vendor=r["vendor"], product=r["product"], target=_dec(r, "target"),
                           utility_group=r.get("utility_group"), suppress_overrides=r.get("suppress_overrides", True), note=r.get("note", ""))
            for r in data.get("personal_targets", [])
        ],
        routing=[
            RoutingRule(**_windowed(r), id=r.get("id", ""), vendor=r["vendor"], sellers=r.get("sellers", []), payee=r["payee"], rate=_dec(r, "rate"),
                        basis=r.get("basis", "enrollment"), exclude_from_individual_payroll=r.get("exclude_from_individual_payroll", True), note=r.get("note", ""))
            for r in data.get("routing_rules", [])
        ],
        badges=[
            BadgeRule(**_windowed(r), badge=r["badge"], start_date=r.get("start_date"), end_date=r.get("end_date"), actual_seller=r["actual_seller"],
                      handling=r.get("handling", "PAY"), note=r.get("note", ""))
            for r in data.get("badge_rules", [])
        ],
        assignments=[
            TemporaryAssignment(**_windowed(r), source_rep=r["source_rep"], actual_seller=r["actual_seller"], note=r.get("note", ""))
            for r in data.get("temporary_assignments", [])
        ],
        collections=[
            CollectionRule(**_windowed(r), person=r["person"], method=r.get("method", "all"), value=_dec(r, "value"), debts=r.get("debts", ["advance"]),
                           creditor=r.get("creditor"), note=r.get("note", ""))
            for r in data.get("collection_rules", [])
        ],
        commercial=data.get("commercial", {}),
        deduction_order=data.get("deduction_order", ["room", "advance", "flight", "other"]),
        raw=data,
        people={p["name"] if isinstance(p, dict) else p for p in data.get("people", [])},
    )
    return rs


def dump_rule(obj) -> dict:
    d = asdict(obj)
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

"""Import the EMG Payroll Control workbook into a structured rule set and
opening balances.

The workbook describes conditions in prose ("Minnesota", "Arcadia / Perch /
Ampion", "2026-W35 onward"). Each phrase is translated through the explicit
tables below. A phrase that is not recognised is never guessed: the rule is
imported with status "review" and listed in the import report, so a person
confirms it in the app before it can price anything.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import openpyxl

from ..money import D, cents
from ..names import AliasTable, clean, key

KWH_VENDORS = ["Arcadia", "Perch", "Nexamp", "Ampion"]

UTILITY_GROUPS = {
    "MN": {"contains_any": ["MINNESOTA", " MN", "XCEL"], "endswith_any": ["MN"]},
    "IL": {"contains_any": ["ILLINOIS", "AMEREN", "COMED", "COMMONWEALTH EDISON"]},
    "NATGRID": {"contains_any": ["NATIONAL GRID"]},
    "NYSEG": {"contains_any": ["NYSEG"]},
    "DELMARVA": {"contains_any": ["DELMARVA"]},
    "NY": {"contains_any": ["NY", "NEW YORK"]},
}

VENDORS = {
    "Arcadia": {"basis": "kwh", "products": ["DUAL OPEN", "DUAL LMI"]},
    "Perch": {"basis": "kwh", "products": ["UCB LMI", "UCB OPEN", "UCB (generic)", "COMMERCIAL"],
              "product_aliases": {"UCB": "UCB (generic)", "UCB (GENERIC)": "UCB (generic)"}},
    "Nexamp": {"basis": "kwh", "products": ["LMI", "OPEN"]},
    "Ampion": {"basis": "kwh", "products": ["Community Solar"]},
    "Spark": {"basis": "enrollment", "products": ["Natural Gas Enrollment"]},
}

UTILITY_PHRASES = {
    "ALL": None, "ALL ELIGIBLE UTILITIES": None, "ALL UTILITIES": None,
    "MINNESOTA": "MN", "MN UTILITIES": "MN",
    "ILLINOIS": "IL",
    "NATIONAL GRID (NY)": "NATGRID",
    "NYSEG": "NYSEG",
    "DELMARVA POWER DE": "DELMARVA", "DELMARVA POWER": "DELMARVA",
    "NY UTILITIES": "NY",
    "EXCEPT NY LMI (SEE SPECIFIC RULE)": None,  # the more specific NY LMI rule wins by specificity
    "COMMERCIAL INITIAL/UPFRONT PAYMENT": None,  # installment stage handled by the importer
    "INITIAL/UPFRONT PAYMENT": None,
    "INITIAL/UPFRONT PAYMENT; EXISTING ELIGIBLE TEAM": None,
}
SKIP_UTILITY = {"COMMERCIAL SECOND PAYMENT", "SECOND PAYMENT"}  # company-only installments
HISTORICAL_UTILITY = {"HISTORICAL CHARGEBACK ONLY"}

ANY_PRODUCT = {"ANY ELIGIBLE", "ELIGIBLE", "ELIGIBLE PRODUCTS", "HISTORICAL", "ALL"}
_SCOPE_ALL = re.compile(r"^ALL( ELIGIBLE( [A-Z]+)?)? PRODUCTS$")  # "All eligible <manager> products", "All products"


def scope_all(phrase: str) -> bool:
    return bool(_SCOPE_ALL.match(phrase))


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, sheet, row, level, message, data=None):
        self.items.append({"sheet": sheet, "row": row, "level": level, "message": message, "data": data})


def _rows(wb, name, min_row, stop_at_blank=False):
    if name not in wb.sheetnames:
        return []
    out = []
    for i, r in enumerate(wb[name].iter_rows(min_row=min_row, values_only=True), start=min_row):
        r = list(r)
        if stop_at_blank and not any(c not in (None, "") for c in r):
            break
        if r and isinstance(r[0], (int, float)) and not isinstance(r[0], bool) and len(r) > 1 and isinstance(r[1], str):
            r = r[1:]  # some rows carry a leading index column
        if any(c not in (None, "") for c in r):
            out.append((i, r))
    return out


def _vendors(phrase: str) -> list[str] | None:
    p = key(phrase)
    if p in ("ALL", "ANY") or scope_all(p):
        return list(KWH_VENDORS)
    parts = [x.strip().title() for x in re.split(r"/|,", clean(phrase)) if x.strip()]
    known = [v for v in parts if v in VENDORS]
    return known if known and len(known) == len(parts) else None


def _product(vendor_list, phrase: str):
    p = key(phrase)
    if p in ANY_PRODUCT or p.startswith("ANY"):
        return None, True
    if p == "LMI / OPEN":
        return ["LMI", "OPEN"], True
    if p in ("UCB (GENERIC)", "UCB"):
        return ["UCB (generic)"], True
    allp = {key(x): x for v in (vendor_list or []) for x in VENDORS.get(v, {}).get("products", [])}
    if p in allp:
        return [allp[p]], True
    return None, False


def _week_window(phrase: str):
    p = clean(phrase)
    m = re.match(r"^(\d{4}-W\d{2})\s+onward$", p, re.I)
    if m:
        return m.group(1), None, True
    m = re.match(r"^(\d{4}-W\d{2})\s+only$", p, re.I)
    if m:
        return m.group(1), m.group(1), True
    if key(p) in ("HISTORICAL", ""):
        return None, None, True
    return None, None, False


def _status(phrase: str) -> str:
    p = key(phrase)
    if p == "ACTIVE" or p == "CURRENT":
        return "active"
    if p.startswith("HISTORICAL"):
        return "historical"
    return "inactive"


def import_rules(path: str | Path, version: str | None = None) -> tuple[dict, Report]:
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rep = Report()
    cfg: dict = {
        "version": version or f"control:{path.name}",
        "source": path.name,
        "utility_groups": UTILITY_GROUPS,
        "vendors": VENDORS,
        "aliases": [], "agent_rates": [], "memberships": [], "override_rules": [], "personal_targets": [],
        "routing_rules": [], "badge_rules": [], "temporary_assignments": [], "collection_rules": [],
        "commercial": {"max_total_rate": "0.015"},
        "deduction_order": ["room", "advance", "flight", "other"],
    }

    # Aliases -------------------------------------------------------------
    for i, r in _rows(wb, "Aliases", 5):
        if r[0] and r[1]:
            scope = "Spark" if "SPARK SOURCE ONLY" in key(r[2] if len(r) > 2 else "") else None
            cfg["aliases"].append({"canonical": clean(r[0]), "alias": clean(r[1]), "scope": scope, "note": clean(r[2] if len(r) > 2 else "")})
    aliases = AliasTable()
    for a in cfg["aliases"]:
        aliases.add(a["canonical"], a["alias"], a.get("scope"))
    canon = lambda n: aliases.resolve(n)  # noqa: E731

    # Agent rates -------------------------------------------------------------
    for i, r in _rows(wb, "Agent Rates", 5):
        vendor, product, cond, agent, rate, status, note = (r + [None] * 7)[:7]
        if not vendor or rate is None:
            continue
        vendor = clean(vendor)
        uc = key(cond)
        if uc in SKIP_UTILITY:
            rep.add("Agent Rates", i, "info", f"Skipped company-only installment row ({clean(cond)}); handled by commercial stage")
            continue
        st = _status(status)
        if uc in HISTORICAL_UTILITY:
            st, group, ok_u = "historical", None, True
        else:
            ok_u = uc in UTILITY_PHRASES
            group = UTILITY_PHRASES.get(uc)
        prods, ok_p = _product([vendor], clean(product))
        who = None if key(agent) == "DEFAULT" else canon(clean(agent).split(" / ")[0])
        rule = {"id": f"rate:{i}", "vendor": vendor, "product": prods[0] if prods else None, "utility_group": group, "agent": who,
                "rate": str(D(rate)), "basis": "kwh", "status": st, "note": clean(note), "source_row": i}
        if not (ok_u and ok_p) or vendor not in VENDORS:
            rule["status"] = "review"
            rep.add("Agent Rates", i, "review", f"Unrecognised condition: product={product!r} utility={cond!r}; imported for review", rule)
        cfg["agent_rates"].append(rule)

    # Memberships ----------------------------------------------------------
    for i, r in _rows(wb, "Manager Teams", 5):
        manager, agent, membership, scope, effective, note = (r + [None] * 6)[:6]
        if not manager or not agent:
            continue
        m = key(membership)
        first, last, ok_w = _week_window(clean(effective))
        if m == "CURRENT":
            st = "active"
        elif m.endswith("ONLY") and "WEEK" in m:
            st = "active"
            wk = re.search(r"(\d+)", m)
            if wk and not first:
                first = last = f"2026-W{int(wk.group(1)):02d}"
        elif m.startswith("HISTORICAL"):
            st = "historical"
        else:
            st = "inactive"  # Excluded rows are kept for the audit trail
        sc = key(scope)
        vendors, products = None, None
        if scope_all(sc):
            vendors = list(KWH_VENDORS)
        elif sc == "ARCADIA DUAL OPEN ONLY":
            vendors, products = ["Arcadia"], ["DUAL OPEN"]
        elif sc.startswith("NEXAMP"):
            vendors = ["Nexamp"]
        else:
            vendors = _vendors(clean(scope))
            if vendors is None:
                rep.add("Manager Teams", i, "review", f"Unrecognised scope {scope!r} for {manager} -> {agent}")
                st = "review"
        cfg["memberships"].append({"manager": canon(clean(manager)), "agent": canon(clean(agent)), "vendors": vendors, "products": products,
                                   "first_week": first, "last_week": last, "status": st, "note": clean(note), "source_row": i})
        if not ok_w:
            rep.add("Manager Teams", i, "review", f"Unrecognised effective window {effective!r}")
    # Override rules -------------------------------------------------------------
    raw_rules = []
    for i, r in _rows(wb, "Manager Rules", 5):
        manager, vendor_p, product, cond, rtype, rate, status, note = (r + [None] * 8)[:8]
        if not manager or rate is None:
            continue
        t = key(rtype)
        if t == "COMPANY ONLY" or key(cond) in SKIP_UTILITY:
            rep.add("Manager Rules", i, "info", f"Company-only row for {manager}: handled by commercial stage, no override")
            continue
        raw_rules.append((i, canon(clean(manager)), clean(vendor_p), clean(product), clean(cond), t, rate, status, clean(note)))
    kinds_by_manager = defaultdict(set)
    for i, mgr, *_rest in raw_rules:
        kinds_by_manager[mgr].add(_rest[3])
    members = defaultdict(list)
    for m in cfg["memberships"]:
        if m["status"] == "active":
            members[m["manager"]].append(m)
    for i, mgr, vend, prod, cond, t, rate, status, note in raw_rules:
        vendors = _vendors(vend)
        prods, ok_p = _product(vendors, prod)
        uc = key(cond)
        ok_u = uc in UTILITY_PHRASES
        rtype = "target_remainder" if t == "TARGET TOTAL RATE" else "fixed"
        sellers = None
        if len(kinds_by_manager[mgr]) > 1:
            # A manager with several rule kinds: link each rule to the team rows whose note names that kind.
            pat = {"TARGET TOTAL RATE": "remainder", "LEGACY FIXED OVERRIDE": "legacy", "FIXED OVERRIDE": "fixed"}.get(t)
            sellers = sorted({m["agent"] for m in members[mgr] if pat and pat in m["note"].lower()}) or None
            if sellers is None and _status(status) == "active":
                rep.add("Manager Rules", i, "review", f"{mgr}: cannot link {rtype} rule to specific team members")
        rule = {"id": f"ovr:{i}", "manager": mgr, "type": rtype, "vendors": vendors, "products": prods, "utility_group": UTILITY_PHRASES.get(uc),
                "rate": str(D(rate)), "sellers": sellers, "status": _status(status), "note": note, "source_row": i}
        if vendors is None or not ok_p or not ok_u:
            rule["status"] = "review" if _status(status) == "active" else rule["status"]
            rep.add("Manager Rules", i, "review", f"Unrecognised rule phrase vendor={vend!r} product={prod!r} cond={cond!r}", rule)
        cfg["override_rules"].append(rule)

    # Personal targets -------------------------------------------------------
    for i, r in _rows(wb, "Personal Effective Rates", 5):
        person, vendor, product, target, status, note = (r + [None] * 6)[:6]
        if not person or target is None:
            continue
        vendor = clean(vendor)
        prods, _ = _product([vendor], clean(product))
        group = None if vendor == "Ampion" else ("MN" if "MINNESOTA" in key(note) else None)
        if vendor == "Ampion" and "MINNESOTA" in key(note):
            rep.add("Personal Effective Rates", i, "info", "Ampion personal target applied to all utilities (as calculate_week43.py does); note says Minnesota only. Confirm.")
        cfg["personal_targets"].append({"person": canon(clean(person)), "vendor": vendor, "product": prods[0] if prods else clean(product),
                                        "target": str(D(target)), "utility_group": group, "suppress_overrides": True,
                                        "status": _status(status), "note": clean(note), "source_row": i})

    # Badge rules -------------------------------------------------------------
    for i, r in _rows(wb, "Badge Ownership Rules", 2):
        badge, start, end, seller, handling, status, note = (r + [None] * 7)[:7]
        if not badge:
            continue
        cfg["badge_rules"].append({"badge": clean(badge), "start_date": str(start)[:10] if start else None, "end_date": str(end)[:10] if end else None,
                                   "actual_seller": canon(clean(seller)), "handling": key(handling) or "PAY", "status": _status(status), "note": clean(note)})

    # Temporary assignments ----------------------------------------------------
    for i, r in _rows(wb, "Temporary Sales Assignments", 2):
        src, actual, first, last = (r + [None] * 4)[:4]
        if src and actual:
            cfg["temporary_assignments"].append({"source_rep": clean(src), "actual_seller": clean(actual), "first_week": clean(first) or None,
                                                 "last_week": clean(last) or None, "status": "active", "note": clean(r[6] if len(r) > 6 else "")})

    # Standing collection rules ------------------------------------------------
    for i, r in _rows(wb, "Advance Collection Rules", 2):
        person, method, eff, end, status, source = (r + [None] * 6)[:6]
        if not person:
            continue
        mk = key(method)
        if "100%" not in mk:
            rep.add("Advance Collection Rules", i, "review", f"Unrecognised collection method {method!r}")
            continue
        debts = ["advance", "flight"] if "FLIGHT" in mk else ["advance"]
        cfg["collection_rules"].append({"person": canon(clean(person)), "method": "all", "value": "0", "debts": debts,
                                        "first_week": clean(eff) or None, "last_week": None, "status": _status(status),
                                        "note": f"{clean(method)} | {clean(end)} | {clean(source)}"})
    # Roster: everyone on the Email Map is a known person (emails are imported separately).
    cfg["people"] = sorted({canon(clean(r[0])) for i, r in _rows(wb, "Email Map", 5) if r[0]})
    return cfg, rep


def _latest(wb, sheet, week_col, aliases: AliasTable, extra_cols=None):
    records = {}
    for i, r in _rows(wb, sheet, 5):
        if not r[0]:
            continue
        n = aliases.resolve(r[0])
        wk = str(r[week_col] or "")
        bal = cents(D(r[1]))
        if n not in records or wk > records[n][0]:
            records[n] = (wk, bal, [r[c] for c in (extra_cols or [])])
        elif wk == records[n][0] and records[n][1] != bal:
            raise ValueError(f"{sheet}: conflicting same-week balances for {n}")
    return records


def import_openings(path: str | Path, cfg: dict) -> dict:
    """Closing balances of the workbook's last week = openings of the next week."""
    wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    aliases = AliasTable()
    for a in cfg["aliases"]:
        aliases.add(a["canonical"], a["alias"], a.get("scope"))
    out: dict[str, dict] = defaultdict(lambda: {"chargeback": "0", "advance": {}, "room": "0", "flight": "0", "other": "0", "reserve": "0"})
    for n, (wk, bal, _) in _latest(wb, "Chargeback Balances", 3, aliases).items():
        if bal:
            out[n]["chargeback"] = str(bal)
    for n, (wk, bal, extra) in _latest(wb, "Advance Balances", 4, aliases, [2, 3]).items():
        if bal:
            out[n]["advance"] = {"EMG": str(bal)}
            out[n]["advance_note"] = clean(extra[0])
    if "Room Balances" in wb.sheetnames:
        for i, r in _rows(wb, "Room Balances", 2):
            if r[0] and D(r[1]):
                out[aliases.resolve(r[0])]["room"] = str(cents(D(r[1])))
    else:
        rooms = [s for s in wb.sheetnames if re.match(r"W\d+ Room Deductions", s)]
        if rooms:
            for i, r in _rows(wb, sorted(rooms)[-1], 2):
                if r[0] and len(r) > 5 and D(r[5]):
                    out[aliases.resolve(r[0])]["room"] = str(cents(D(r[5])))
    for i, r in _rows(wb, "Flight Debt Balances", 2):
        if r[0] and D(r[1]):
            out[aliases.resolve(r[0])]["flight"] = str(cents(D(r[1])))
    for i, r in _rows(wb, "Clawback Reserves", 5, stop_at_blank=True):
        if r[0] and D(r[1]):
            out[aliases.resolve(r[0])]["reserve"] = str(cents(D(r[1])))
    return dict(out)


def import_email_map(path: str | Path) -> list[dict]:
    wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    out = []
    for i, r in _rows(wb, "Email Map", 5):
        if r[0]:
            status = clean(r[2] if len(r) > 2 else "")
            email = clean(r[1]) if len(r) > 1 else ""
            verified = bool(email) and "VERIFIED" in key(status)
            excluded = "DO NOT" in key(status) or "EXCLUD" in key(status)
            out.append({"person": clean(r[0]), "email": email or None, "verified": verified, "excluded": excluded, "status": status,
                        "note": clean(r[3] if len(r) > 3 else "")})
    return out


def import_processed_chargebacks(path: str | Path) -> list[dict]:
    wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    out = []
    for i, r in _rows(wb, "Processed Chargebacks", 5):
        if len(r) > 2 and r[2]:
            out.append({"key": clean(r[0]), "source": clean(r[1]), "reference": clean(r[2]).split(" - ")[0], "raw_reference": clean(r[2])})
    return out

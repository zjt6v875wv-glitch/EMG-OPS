"""Replay a finalized historical week from its original inputs and compare
every person's result with the final package (the acceptance fixture).

Mismatches are reported, never "fixed" in the historical record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..importers import arcadia_perch, nexamp, spark
from ..money import D, cents
from ..names import ref_key
from ..payroll.calculate import Adjustment
from ..payroll.pipeline import PayoutEvidence, WeekInputs, calculate_week, openings_from_json
from ..rules.control_import import import_openings, import_processed_chargebacks, import_rules
from ..rules.model import load_ruleset

COMPARE_FIELDS = [
    ("agent_commission", "agent_commission"),
    ("manager_override", "manager_override"),
    ("gross_compensation", "gross_compensation"),
    ("opening_chargeback", "opening_chargeback"),
    ("new_chargebacks", "new_chargebacks"),
    ("chargeback_recovery", "chargeback_recovery"),
    ("ending_chargeback", "ending_chargeback"),
    ("manual_hold", "manual_hold"),
    ("room_deduction", "room_deduction"),
    ("room_ending", "room_uncollected"),
    ("advance_deduction", "advance_deduction"),
    ("advance_ending", "advance_ending"),
    ("flight_deduction", "flight_debt_deduction"),
    ("flight_ending", "flight_debt_ending"),
    ("cash_credits", "historical_advance_refund"),
    ("final_check", "preliminary_check"),
]


def merge_overlay(cfg: dict, overlay: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    for k, v in overlay.items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            out[k].extend(v)
    out["version"] = f"{cfg.get('version')}+overlay"
    return out


@dataclass
class ReplayReport:
    week: str
    matched: int = 0
    mismatches: list[dict] = field(default_factory=list)
    missing_in_app: list[str] = field(default_factory=list)
    extra_in_app: list[str] = field(default_factory=list)
    totals_app: dict = field(default_factory=dict)
    totals_fixture: dict = field(default_factory=dict)
    review: list[dict] = field(default_factory=list)
    import_report: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.missing_in_app and not self.extra_in_app

    def to_dict(self) -> dict:
        return {"week": self.week, "ok": self.ok, "matched_people": self.matched, "mismatches": self.mismatches,
                "missing_in_app": self.missing_in_app, "extra_in_app": self.extra_in_app,
                "totals_app": self.totals_app, "totals_fixture": self.totals_fixture,
                "review": self.review, "import_report": self.import_report}


def w43_adjustments(calc: dict, facts: dict | None = None) -> list[Adjustment]:
    """Translate the documented W43 one-week facts into week adjustments.

    Intake, hotel and collection decisions come from the week's calc file;
    explicit one-off decisions come from a private facts file (never committed)."""
    adj: list[Adjustment] = []
    for t in calc.get("advance_intake_audit", []):
        if t["decision"] == "ADD":
            cred = t.get("creditor") or "EMG"
            adj.append(Adjustment("new_advance", t["payee"], D(t["amount"]), creditor="EMG" if cred in ("Unspecified", "") else cred,
                                  note=f"{t['source']}: {t.get('memo', '')}", authorization="W43 advance intake"))
    for a in calc.get("hotel_allocations", []):
        if a["pay_date"] == calc["pay_date"]:
            adj.append(Adjustment("room_charge", a["payee"], D(a["amount"]), note=f"Hotel average share {a['share']}", authorization="W43 hotel averaging"))
    for person, d in calc.get("collection_decisions", {}).items():
        if isinstance(d, str):  # decision recorded as prose: collect this week, paired with a protected check in the facts file
            adj.append(Adjustment("collection", person, method="all", debts=["advance"], note=d, authorization="W43 decision"))
            continue
        if "2026-W43 only" in d.get("ongoing", ""):
            adj.append(Adjustment("collection", person, method="all", debts=["advance"], note=d["ongoing"], authorization="W43 decision"))
    for a in (facts or {}).get("adjustments", []):
        adj.append(Adjustment.from_dict(a))
    return adj


def w43_evidence(calc: dict) -> list[PayoutEvidence]:
    ev = [PayoutEvidence(c["source"], ref_key(c["reference"]), c["payee"], D(c["amount"]), D(c["rate"]), "Agent Commission" if c["payee"] == c["rep"] else "Manager Override",
                         "", c.get("notes", "").replace("Exact historical reversal: ", "")) for c in calc.get("chargeback_detail", [])]
    for v in calc.get("vendor_chargeback_audit", []):
        if v["source"] == "Perch" and v.get("historical_agent_pay"):
            ev.append(PayoutEvidence("Perch", ref_key(v["reference"]), v["rep"], D(v["historical_agent_pay"]), activity="Agent Commission",
                                     week=v["evidence"].split(" ")[4] if len(v["evidence"].split(" ")) > 4 else "", proof=v["evidence"]))
    return ev


def load_facts(overlay_path: str | Path | None, facts_path: str | Path | None = None) -> dict:
    p = Path(facts_path) if facts_path else (Path(overlay_path).parent / "w43_week_facts.json" if overlay_path else None)
    return json.loads(p.read_text()) if p and p.exists() else {}


def replay_w43(fixture_dir: str | Path, overlay_path: str | Path | None, facts_path: str | Path | None = None) -> tuple[ReplayReport, object]:
    base = Path(fixture_dir)
    control = base / "recovery/week42/EMG_Payroll_Control_Week42_WORKING.xlsx"
    calc = json.loads((base / "week43_calc.json").read_text())
    cfg, imp = import_rules(control, version="control:W42-working")
    if overlay_path:
        cfg = merge_overlay(cfg, json.loads(Path(overlay_path).read_text()))
    rs = load_ruleset(cfg)
    openings = openings_from_json(import_openings(control, cfg))
    inp = base / "inputs/week43"
    sales_report = nexamp.parse_sales_report(inp / "Sales Report Energy Marketing.xlsx")
    index = {r.enrollment_id: r for r in sales_report.rows}
    arc = arcadia_perch.parse(inp / "ENERGY MARKETING GROUP LLC Commission Statement 10.21.26.xlsx", year=2026)
    nex = nexamp.parse_commissions(inp / "Commissions_EnergyMarketingGroup_CAL26WK43.xlsx", index)
    spk = spark.parse(inp / "EMGD2D_20261017.xlsx")
    paid_nex = {r.enrollment_id for r in nex.rows if r.line_type == "payable"}
    nex_nonpay = nexamp.nonpayables_for_week(sales_report, 43, 2026, paid_nex)
    rows = arc.rows + nex.rows + nex_nonpay + spk.rows
    processed = {p["reference"] for p in import_processed_chargebacks(control)}
    inputs = WeekInputs("2026-W43", rows, openings, w43_adjustments(calc, load_facts(overlay_path, facts_path)), w43_evidence(calc), processed)
    bundle = calculate_week(rs, inputs)

    rep = ReplayReport("2026-W43")
    rep.import_report = [i for i in imp.items if i["level"] == "review"]
    fixture = {r["payee"]: r for r in calc["summary"]}
    app = {p.payee: p for p in bundle.result.people}
    for name, fx in fixture.items():
        p = app.get(name)
        if p is None:
            if any(D(fx.get(f2, 0)) for _, f2 in COMPARE_FIELDS):
                rep.missing_in_app.append(name)
            continue
        diffs = {}
        for f1, f2 in COMPARE_FIELDS:
            a, b = cents(getattr(p, f1)), cents(D(fx.get(f2, 0)))
            if abs(a - b) > Decimal("0.00"):
                diffs[f1] = {"app": str(a), "final": str(b)}
        if diffs:
            rep.mismatches.append({"payee": name, "diffs": diffs})
        else:
            rep.matched += 1
    for name, p in app.items():
        if name not in fixture and (p.gross_compensation or p.final_check or p.advance_ending or p.ending_chargeback):
            rep.extra_in_app.append(name)
    t = bundle.result.totals
    rep.totals_app = {k: (str(v) if isinstance(v, Decimal) else {kk: str(vv) for kk, vv in v.items()} if isinstance(v, dict) else v) for k, v in t.items()}
    rep.totals_app["nonpayable_rows_emg"] = sum(1 for r in bundle.nonpayable if "company_payee" not in r.raw)
    rep.totals_fixture = calc["qa"]
    rep.review = [r.to_dict() for r in bundle.review]
    return rep, bundle

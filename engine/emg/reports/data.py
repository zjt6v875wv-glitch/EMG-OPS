"""Everything a report needs about one payroll run, loaded once."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..money import D
from ..weeks import parse_week, pay_date


@dataclass
class ReportData:
    run: dict
    people: list[dict]                      # run_people rows (data = PersonResult dict)
    lines: list[dict]
    chargebacks: list[dict]
    items: list[dict]                       # nonpayable / pending / company_adjustment
    adjustments: list[dict]
    review: list[dict]
    sources: list[dict]                     # import batches with file names and hashes
    rules: dict
    emails: dict[str, dict] = field(default_factory=dict)

    @property
    def week(self) -> str:
        return self.run["week"]

    @property
    def week_no(self) -> int:
        return parse_week(self.week)[1]

    @property
    def pay_date(self):
        return pay_date(self.week)

    @property
    def label(self) -> str:
        """Final / Preliminary, with the revision when a correction replaced an earlier final."""
        base = "Final" if self.run["status"] in ("finalized", "superseded") and self.run.get("finalized_at") else "Preliminary"
        return f"{base} Rev {self.run['revision']}" if self.run.get("supersedes") else base

    @property
    def company_payees(self) -> set[str]:
        return {r["payee"] for r in self.rules.get("routing_rules", [])}

    def person(self, name: str) -> dict | None:
        return next((p for p in self.people if p["payee"] == name), None)

    def lines_for(self, name: str) -> list[dict]:
        return [l for l in self.lines if l["payee"] == name]

    def chargebacks_for(self, name: str) -> list[dict]:
        return [c for c in self.chargebacks if c["payee"] == name]

    def nonpayable_for(self, name: str) -> list[dict]:
        return [i for i in self.items if i["kind"] in ("nonpayable", "pending") and i["payee"] == name]

    ACTIVITY = ("gross_compensation", "new_advance", "new_chargebacks", "held_chargebacks", "chargeback_recovery", "room_new", "room_deduction",
                "flight_opening", "flight_new", "flight_deduction", "other_new", "other_deduction", "advance_deduction", "manual_hold",
                "reserve_withheld", "cash_credits", "final_check")

    def report_people(self) -> list[str]:
        """People with activity this week (pay, new charges, deductions, holds, credits, flight debt) or
        non-payable sales. A carried balance or pending-only activity does not generate a statement
        (pending rows still appear on statements that exist)."""
        out = {p["payee"] for p in self.people if any(D(p["data"].get(k, 0)) for k in self.ACTIVITY)}
        out |= {i["payee"] for i in self.items if i["kind"] == "nonpayable" and i["payee"]}
        return sorted(out - self.company_payees - {None}, key=str.upper)


def load(c, run_id) -> ReportData:
    run = c.execute("select * from payroll_runs where id=%s", (run_id,)).fetchone()
    if not run:
        raise LookupError("run not found")
    rules = c.execute("select config from rule_sets where version=%s", (run["rule_set_version"],)).fetchone()["config"]
    return ReportData(
        run=run,
        people=c.execute("select payee, final_check, status, data from run_people where run_id=%s order by upper(payee)", (run_id,)).fetchall(),
        lines=c.execute("select * from run_lines where run_id=%s order by payee, vendor, product, sale_date, enrollment_id", (run_id,)).fetchall(),
        chargebacks=c.execute("select * from run_chargebacks where run_id=%s order by payee, vendor, enrollment_id", (run_id,)).fetchall(),
        items=c.execute("select * from run_items where run_id=%s order by kind, payee, vendor, enrollment_id", (run_id,)).fetchall(),
        adjustments=c.execute("select * from week_adjustments where week=%s and voided_at is null order by person, kind", (run["week"],)).fetchall(),
        review=c.execute("select * from review_items where run_id=%s or batch_id = any(%s) order by blocking desc, kind", (run_id, run["batch_ids"])).fetchall(),
        sources=c.execute("select b.id, b.importer, b.vendor, b.summary, f.id file_id, f.file_name, f.sha256, f.storage_key from import_batches b"
                          " join source_files f on f.id=b.source_file_id where b.id = any(%s) order by b.created_at", (run["batch_ids"],)).fetchall(),
        rules=rules,
        emails={p["name"]: p for p in c.execute("select * from people")},
    )

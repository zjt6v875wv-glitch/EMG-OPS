"""Engine scenarios on the fictional sample rules (no database)."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from conftest import sale
from emg.money import D, cents
from emg.names import AliasTable, key
from emg.payroll.calculate import Adjustment, Opening, run_payroll
from emg.payroll.commissions import calculate_commissions
from emg.payroll.pipeline import PayoutEvidence, WeekInputs, calculate_week
from emg.rules.model import load_ruleset
from emg.weeks import business_week_bounds, local_date, pay_date, week_of

W = "2026-W43"


def lines_for(res, payee):
    return [l for l in res.lines if l.payee == payee]


# -- rates and overrides -----------------------------------------------------------

def test_specific_rate_beats_default_and_override_applies(rs):
    res = calculate_commissions(rs, [sale(utility="NORTH POWER CO")], W)
    alex = lines_for(res, "Alex Rivera")
    assert [l.rate for l in alex] == [D("0.012")]          # utility-group rate beats the product default
    jl = lines_for(res, "Jordan Lee")
    # Explicit NORTH target_remainder rule (more specific) wins over the flat team rule: 0.015 - 0.012
    assert [l.rate for l in jl] == [D("0.003")]
    assert sum(l.amount for l in res.lines) == D("15.000")


def test_personal_sale_gets_target_and_no_override(rs):
    res = calculate_commissions(rs, [sale(rep="Jordan Lee")], W)
    assert {(l.activity, l.rate) for l in res.lines} == {("Agent Commission", D("0.010")), ("Personal Rate Top-Up", D("0.004"))}
    assert not [l for l in res.lines if l.activity == "Manager Override"]


def test_team_scope_by_vendor(rs):
    # Taylor is on Jordan's team for Nexamp only: Arcadia sale earns no override
    res = calculate_commissions(rs, [sale(vendor="Arcadia", rep="Taylor Brooks", product="CS")], W)
    assert not lines_for(res, "Jordan Lee")
    res = calculate_commissions(rs, [sale(rep="Taylor Brooks")], W)
    assert lines_for(res, "Jordan Lee")


def test_ambiguous_rate_goes_to_review(cfg):
    cfg["agent_rates"].append({"id": "dup", "vendor": "Nexamp", "product": "LMI", "rate": "0.011"})
    res = calculate_commissions(load_ruleset(cfg), [sale()], W)
    assert not res.lines and res.review[0].kind == "missing_rate" and "Ambiguous" in res.review[0].message


def test_spark_flat_rates_alias_and_flat_override(rs):
    res = calculate_commissions(rs, [sale(vendor="Spark", rep="SPK001", product="", kwh="0", units=Decimal(1)),
                                     sale(vendor="Spark", ref="E2", rep="Robbie", product="", kwh="0", units=Decimal(1)),
                                     sale(vendor="Spark", ref="E3", rep="Casey Morgan", product="", kwh="0", units=Decimal(1))], W)
    got = {(l.payee, l.activity): l.amount for l in res.lines}
    assert got[("Alex Rivera", "Agent Commission")] == D(40)      # vendor-scoped alias SPK001
    assert got[("Robin Shaw", "Agent Commission")] == D(45)       # agent-specific rate
    assert got[("Casey Morgan", "Agent Commission")] == D(40)
    assert got[("Robin Shaw", "Manager Override")] == D(5)        # flat per enrollment for an explicit seller


def test_vendor_scoped_alias_does_not_leak(rs):
    assert rs.canon("SPK001", "Nexamp") == "SPK001"
    assert rs.canon("SPK001", "Spark") == "Alex Rivera"


def test_company_routing_excluded_from_individual_payroll(rs):
    rows = [sale(vendor="Spark", rep="Pat Quinn", product="", kwh="0", units=Decimal(1))]
    b = calculate_week(rs, WeekInputs(W, rows, {}))
    assert b.result.totals["company_payables"] == {"Partner Co": D("70.00")}
    assert all(p.payee != "Pat Quinn" and p.payee != "Partner Co" for p in b.result.people)


def test_temporary_assignment_moves_pay_keeps_override_and_expires(cfg):
    cfg["temporary_assignments"] = [{"source_rep": "Sam Patel", "actual_seller": "Casey Morgan", "first_week": W, "last_week": W}]
    rs = load_ruleset(cfg)
    res = calculate_commissions(rs, [sale(rep="Sam Patel")], W)
    from emg.payroll.commissions import apply_assignments

    apply_assignments(rs, res.lines, W)
    assert {l.payee for l in res.lines} == {"Casey Morgan", "Jordan Lee"}
    nxt = calculate_commissions(rs, [sale(rep="Sam Patel")], "2026-W44")
    apply_assignments(rs, nxt.lines, "2026-W44")
    assert "Sam Patel" in {l.payee for l in nxt.lines}


def test_unknown_product_is_review_not_dropped(rs):
    res = calculate_commissions(rs, [sale(product="MYSTERY")], W)
    assert not res.lines and res.review[0].kind == "product_unknown"


# -- waterfall ---------------------------------------------------------------------

def _run(rs, gross_by_person, openings=None, cbs=None, adj=None):
    from emg.model import ChargebackLine, EarningLine

    lines = [EarningLine(p, "Agent Commission", "Nexamp", p, f"X{p}", amount=D(a)) for p, a in gross_by_person.items()]
    return run_payroll(rs, W, lines, openings or {}, cbs or [], adj or [])


def test_partial_chargeback_recovery_carries_forward(rs):
    from emg.model import ChargebackLine

    cb = [ChargebackLine("Alex Rivera", "Nexamp", "OLD1", D("150"), verified=True)]
    r = _run(rs, {"Alex Rivera": "100"}, {"Alex Rivera": Opening(chargeback=D("20"))}, cb).person("Alex Rivera")
    assert (r.chargeback_recovery, r.ending_chargeback, r.final_check) == (D("100.00"), D("70.00"), D("0.00"))


def test_unverified_chargeback_is_held_not_deducted(rs):
    from emg.model import ChargebackLine

    cb = [ChargebackLine("Alex Rivera", "Nexamp", "OLD1", D("0"), verified=False)]
    r = _run(rs, {"Alex Rivera": "100"}, cbs=cb).person("Alex Rivera")
    assert r.final_check == D("100.00")


def test_deduction_order_defaults_and_standing_rule(rs):
    op = {"Sam Patel": Opening(room=D("30"), advance={"EMG": D("200")}, flight=D("40"), other=D("10"))}
    r = _run(rs, {"Sam Patel": "300"}, op).person("Sam Patel")
    # room all (30), advance standing $50, flight default none, other all (10)
    assert (r.room_deduction, r.advance_deduction, r.flight_deduction, r.other_deduction) == (D("30.00"), D("50.00"), D("0.00"), D("10.00"))
    assert r.final_check == D("210.00") and r.advance_ending == D("150.00") and r.flight_ending == D("40.00")


def test_week_instruction_beats_rule_and_does_not_persist(rs):
    op = {"Sam Patel": Opening(advance={"EMG": D("200")})}
    adj = [Adjustment("collection", "Sam Patel", method="all", debts=["advance"], note="this week only")]
    assert _run(rs, {"Sam Patel": "300"}, op, adj=adj).person("Sam Patel").advance_deduction == D("200.00")
    assert _run(rs, {"Sam Patel": "300"}, op).person("Sam Patel").advance_deduction == D("50.00")


def test_defer_skips_collection(rs):
    op = {"Sam Patel": Opening(advance={"EMG": D("200")})}
    r = _run(rs, {"Sam Patel": "300"}, op, adj=[Adjustment("defer", "Sam Patel", debts=["advance"])]).person("Sam Patel")
    assert r.advance_deduction == 0 and r.final_check == D("300.00")


def test_protected_check_limits_deductions(rs):
    op = {"Alex Rivera": Opening(room=D("500"), advance={"EMG": D("500")})}
    adj = [Adjustment("collection", "Alex Rivera", method="all", debts=["advance"]), Adjustment("protected_check", "Alex Rivera", D("400"))]
    r = _run(rs, {"Alex Rivera": "600"}, op, adj=adj).person("Alex Rivera")
    assert r.final_check == D("400.00") and r.room_deduction == D("200.00") and r.advance_deduction == 0


def test_protected_check_is_one_week_only(rs):
    op = {"Alex Rivera": Opening(room=D("500"))}
    assert _run(rs, {"Alex Rivera": "600"}, op).person("Alex Rivera").final_check == D("100.00")


def test_hold_all_and_cash_credit_after_deductions(rs):
    adj = [Adjustment("manual_hold", "Alex Rivera", label="all"), Adjustment("cash_credit", "Alex Rivera", D("25"), label="refund")]
    r = _run(rs, {"Alex Rivera": "300"}, adj=adj).person("Alex Rivera")
    assert (r.manual_hold, r.final_check, r.status) == (D("300.00"), D("25.00"), "PAYABLE")


def test_separate_creditors_and_new_advance(rs):
    op = {"Alex Rivera": Opening(advance={"EMG": D("100"), "Creditor B": D("60")})}
    adj = [Adjustment("new_advance", "Alex Rivera", D("40"), creditor="Creditor B"), Adjustment("collection", "Alex Rivera", method="fixed", value=D("120"), debts=["advance"])]
    r = _run(rs, {"Alex Rivera": "500"}, op, adj=adj).person("Alex Rivera")
    assert r.advance_by_creditor["EMG"] == {"owed": "100.00", "deducted": "100.00", "ending": "0.00"}
    assert r.advance_by_creditor["Creditor B"] == {"owed": "100.00", "deducted": "20.00", "ending": "80.00"}


def test_percent_collection_and_never_negative(rs):
    op = {"Alex Rivera": Opening(advance={"EMG": D("1000")})}
    adj = [Adjustment("collection", "Alex Rivera", method="percent", value=D("25"), debts=["advance"])]
    r = _run(rs, {"Alex Rivera": "333.33"}, op, adj=adj).person("Alex Rivera")
    assert r.advance_deduction == D("83.33") and r.final_check == D("250.00")


def test_reconciliation_identity_holds(rs):
    op = {"Alex Rivera": Opening(room=D("30"), chargeback=D("10")), "Sam Patel": Opening(advance={"EMG": D("75")})}
    res = _run(rs, {"Alex Rivera": "99.995", "Sam Patel": "12.345"}, op)
    t = res.totals
    assert t["gross_compensation"] == sum(p.gross_compensation for p in res.people)


# -- chargebacks and duplicates ------------------------------------------------------

def test_exact_historical_reversal_for_agent_and_manager(rs):
    rows = [sale(ref="OLD1", line_type="chargeback", amount="-50")]
    ev = [PayoutEvidence("Nexamp", "OLD1", "Alex Rivera", D("10.00"), D("0.010"), "Agent Commission", "2026-W30"),
          PayoutEvidence("Nexamp", "OLD1", "Jordan Lee", D("2.00"), D("0.002"), "Manager Override", "2026-W30")]
    b = calculate_week(rs, WeekInputs(W, rows, {}, payout_history=ev))
    assert {(c.payee, c.amount, c.verified) for c in b.chargebacks} == {("Alex Rivera", D("10.00"), True), ("Jordan Lee", D("2.00"), True)}


def test_chargeback_without_evidence_is_held(rs):
    b = calculate_week(rs, WeekInputs(W, [sale(ref="OLD2", line_type="chargeback", amount="-50")], {}))
    assert b.chargebacks[0].verified is False and b.review[0].kind == "unmatched_chargeback"
    assert b.result.totals["new_chargebacks"] == 0


def test_processed_chargeback_not_deducted_twice(rs):
    ev = [PayoutEvidence("Nexamp", "OLD1", "Alex Rivera", D("10.00"))]
    b = calculate_week(rs, WeekInputs(W, [sale(ref="OLD1", line_type="chargeback", amount="-50")], {}, payout_history=ev, processed_chargeback_refs={"OLD1"}))
    assert not b.chargebacks


def test_duplicate_payment_reversal_is_company_only(rs):
    ev = [PayoutEvidence("Nexamp", "OLD1", "Alex Rivera", D("10.00"), activity="Agent Commission")]
    b = calculate_week(rs, WeekInputs(W, [sale(ref="OLD1", line_type="chargeback", amount="-50", status="Duplicate Payment")], {}, payout_history=ev))
    assert not b.chargebacks and b.company_adjustments


def test_duplicate_enrollment_in_week_and_prior_week(rs):
    rows = [sale(ref="E1"), sale(ref="E1", source_row=9), sale(ref="E2")]
    b = calculate_week(rs, WeekInputs(W, rows, {}, prior_paid_keys={("Nexamp", "E2")}))
    assert {l.enrollment_id for l in b.lines if l.activity == "Agent Commission"} == {"E1"}
    assert sorted(r.kind for r in b.review) == ["duplicate", "duplicate"]


def test_prior_year_nonpayable_excluded(rs):
    rows = [sale(ref="N1", line_type="nonpayable", sale_date=date(2025, 12, 30)), sale(ref="N2", line_type="nonpayable")]
    b = calculate_week(rs, WeekInputs(W, rows, {}))
    assert [r.enrollment_id for r in b.nonpayable] == ["N2"]


# -- names, weeks, time zones ---------------------------------------------------------

def test_name_keys():
    assert key("Mary-Jane  Doe") == key("MARY JANE DOE")
    assert key("D’Andra") == key("D’ANDRA")
    t = AliasTable()
    t.add("Robin Shaw", "Robbie")
    assert t.resolve(" robbie ") == "Robin Shaw"


def test_weeks_and_pay_dates():
    assert pay_date("2026-W43") == date(2026, 10, 23)
    assert week_of(date(2026, 10, 19)) == "2026-W43" and week_of(date(2026, 10, 25)) == "2026-W43"
    assert business_week_bounds(date(2026, 10, 25)) == (date(2026, 10, 19), date(2026, 10, 25))


def test_timezone_boundary():
    # 02:30 UTC Monday is still Sunday in New York: the sale belongs to the previous business week.
    ts = datetime(2026, 10, 26, 2, 30, tzinfo=timezone.utc)
    assert local_date(ts) == date(2026, 10, 25)
    assert week_of(local_date(ts)) == "2026-W43"
    assert local_date(ts, "UTC") == date(2026, 10, 26)


def test_rounding_half_up():
    assert cents(D("0.005")) == D("0.01") and cents(D("2.675")) == D("2.68") and cents(D("-0.005")) == D("-0.01")

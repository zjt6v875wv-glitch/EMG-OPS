"""Payroll runs: calculate (pre-finalization view) -> finalize -> correct.

* ``calculate`` is repeatable and side-effect free on ledgers: it stores a
  numbered revision with every line, person result, chargeback and review item.
* ``finalize`` is atomic and idempotent (same finalize key = same result). It
  refuses stale runs (inputs changed since calculation) and open blocking
  review items, then posts ledger entries, payout evidence, processed
  chargebacks and *calculated* payments in one transaction.
* ``correct`` creates a new revision of a finalized week. Finalizing it
  reverses the old run's ledger entries, retires its payout evidence, voids
  its unconfirmed payments (confirmed ones are kept and the difference goes to
  review) and posts the corrected run. The old revision stays readable.
"""
from __future__ import annotations

import hashlib
import json

from ..db.conn import J, audit
from ..model import ZERO
from ..money import D, cents
from ..payroll.calculate import Adjustment
from ..payroll.pipeline import PayoutEvidence, WeekBundle, WeekInputs, calculate_week
from . import imports, ledger
from . import rules as rules_svc

PAID_ACTIVITIES = ("Agent Commission", "Company Payable")


class FinalizeBlocked(Exception):
    def __init__(self, message: str, items: list | None = None):
        super().__init__(message)
        self.items = items or []


# -- inputs ----------------------------------------------------------------------

def week_adjustments(c, week: str) -> list[dict]:
    return c.execute("select * from week_adjustments where week=%s and voided_at is null order by created_at, id", (week,)).fetchall()


def _adjustments(rows: list[dict]) -> list[Adjustment]:
    return [Adjustment.from_dict(dict(r) | {"authorization": r["authorized_by"], "debts": list(r["debts"])}) for r in rows]


def _evidence(c, week: str) -> list[PayoutEvidence]:
    return [PayoutEvidence(r["vendor"], r["enrollment_id"], r["payee"], r["amount"], r["rate"] or ZERO, r["activity"], r["week"], r["proof"] or "")
            for r in c.execute("select * from payout_evidence where active and week <> %s order by id", (week,))]


def gather(c, week: str, rule_version: int | None = None):
    rs, rule_row = rules_svc.ruleset(c, rule_version)
    rows, batch_ids = imports.committed_rows(c, week)
    adj_rows = week_adjustments(c, week)
    openings = ledger.openings(c, week)
    evidence = _evidence(c, week)
    processed = {r["enrollment_id"] for r in c.execute("select enrollment_id from processed_chargebacks where active and week <> %s", (week,))}
    prior_paid = {(r["vendor"], r["enrollment_id"]) for r in c.execute(
        "select distinct vendor, enrollment_id from payout_evidence where active and week <> %s and activity = any(%s)", (week, list(PAID_ACTIVITIES)))}
    inputs = WeekInputs(week, rows, openings, _adjustments(adj_rows), evidence, processed, prior_paid)
    fingerprint = {
        "rules": rule_row["version"],
        "batches": sorted(str(b) for b in batch_ids),
        "adjustments": sorted(str(a["id"]) for a in adj_rows),
        "ledger": c.execute("select count(*) n, coalesce(sum(abs(amount)),0) s, coalesce(max(id),0) m from ledger_entries where week < %s", (week,)).fetchone(),
        "evidence": c.execute("select count(*) n, coalesce(max(id),0) m from payout_evidence where active and week <> %s", (week,)).fetchone(),
        "processed": sorted(processed),
    }
    h = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()
    return rs, rule_row, inputs, batch_ids, h


# -- calculate -------------------------------------------------------------------

def open_correction(c, week: str) -> dict | None:
    return c.execute("select * from payroll_runs where week=%s and status='calculated' and supersedes is not null order by revision desc limit 1", (week,)).fetchone()


def calculate(c, week: str, actor: str, rule_version: int | None = None, supersedes=None, correction_reason: str | None = None) -> dict:
    final = c.execute("select id from payroll_runs where week=%s and status='finalized'", (week,)).fetchone()
    if final and supersedes is None:
        oc = open_correction(c, week)
        if not oc:
            raise ValueError(f"{week} is finalized. Start a correction to recalculate it.")
        supersedes, correction_reason = oc["supersedes"], oc["correction_reason"]
    rs, rule_row, inputs, batch_ids, h = gather(c, week, rule_version)
    bundle = calculate_week(rs, inputs)
    rev = c.execute("select coalesce(max(revision),0)+1 r from payroll_runs where week=%s", (week,)).fetchone()["r"]
    c.execute("update payroll_runs set status='superseded' where week=%s and status='calculated'", (week,))
    totals = _jsonable(bundle.result.totals) | {
        "aeg_and_company": {k: str(v) for k, v in bundle.result.totals["company_payables"].items()},
        "nonpayable_rows": len(bundle.nonpayable),
        "pending_rows": len(bundle.pending),
        "review_blocking": len(bundle.blocking_review),
        "review_total": len(bundle.review),
        "pay_date": bundle.pay_date,
    }
    run = c.execute(
        "insert into payroll_runs(week,revision,status,rule_set_version,batch_ids,inputs_hash,totals,reconciliation,supersedes,correction_reason,created_by)"
        " values (%s,%s,'calculated',%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (week, rev, rule_row["version"], batch_ids, h, J(totals), J(bundle.reconciliation), supersedes, correction_reason, actor),
    ).fetchone()
    _store_bundle(c, run["id"], bundle)
    _sync_review(c, week, run["id"], rev, bundle)
    audit(c, actor, "payroll.calculate", "payroll_run", str(run["id"]), None, {"week": week, "revision": rev, "total_checks": totals["total_checks"]})
    return run


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v) if hasattr(v, "quantize") else v


def _store_bundle(c, run_id, b: WeekBundle) -> None:
    with c.cursor() as cur:
        cur.executemany(
            "insert into run_lines(run_id,payee,activity,vendor,rep,source_rep,enrollment_id,transaction_id,customer,utility,product,kwh,units,rate,amount,sale_date,rule_id,notes)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(run_id, l.payee, l.activity, l.vendor, l.rep, l.source_rep, l.enrollment_id, l.transaction_id, l.customer, l.utility, l.product,
              l.kwh, l.units, l.rate, l.amount, l.sale_date, l.rule_id, l.notes) for l in b.lines],
        )
        cur.executemany("insert into run_people(run_id,payee,final_check,status,data) values (%s,%s,%s,%s,%s)",
                        [(run_id, p.payee, p.final_check, p.status, J(p.to_dict())) for p in b.result.people])
        cur.executemany("insert into run_chargebacks(run_id,payee,vendor,enrollment_id,amount,verified,data) values (%s,%s,%s,%s,%s,%s,%s)",
                        [(run_id, x.payee, x.vendor, x.enrollment_id, x.amount, x.verified, J(x.to_dict())) for x in b.chargebacks])
        items = [("nonpayable", r.raw.get("company_payee") or r.raw.get("seller"), r.vendor, r.enrollment_id, r.to_dict() | {"company_payee": r.raw.get("company_payee")}) for r in b.nonpayable]
        items += [("pending", r.raw_rep, r.vendor, r.enrollment_id, r.to_dict()) for r in b.pending]
        items += [("company_adjustment", None, a.vendor, a.enrollment_id, a.to_dict()) for a in b.company_adjustments]
        cur.executemany("insert into run_items(run_id,kind,payee,vendor,enrollment_id,data) values (%s,%s,%s,%s,%s,%s)",
                        [(run_id, k, p, v, e, J(d)) for k, p, v, e, d in items])


def _sync_review(c, week: str, run_id, rev: int, b: WeekBundle) -> None:
    """Upsert review items with stable keys so a resolution survives recalculation;
    open items that no longer occur are closed with a note."""
    keys = []
    for r in b.review:
        msg_h = hashlib.sha1(r.message.encode()).hexdigest()[:10]
        k = f"run:{week}:{r.kind}:{r.vendor}:{r.enrollment_id}:{r.person}:{msg_h}"
        keys.append(k)
        c.execute(
            "insert into review_items(scope_key,week,run_id,kind,message,vendor,enrollment_id,person,amount,blocking,data)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " on conflict (scope_key) do update set run_id=excluded.run_id, amount=excluded.amount, blocking=excluded.blocking, data=excluded.data",
            (k, week, run_id, r.kind, r.message, r.vendor, r.enrollment_id, r.person, r.amount, r.blocking, J(r.data)),
        )
    c.execute("update review_items set status='resolved', resolution=%s, resolved_by='system', resolved_at=now()"
              " where week=%s and scope_key like 'run:%%' and status='open' and not (scope_key = any(%s))",
              (f"No longer present in revision {rev}", week, keys))


# -- finalize ----------------------------------------------------------------------

def blocking_items(c, run: dict) -> list[dict]:
    return c.execute(
        "select * from review_items where status='open' and blocking and (run_id=%s or batch_id = any(%s)) order by kind, enrollment_id",
        (run["id"], run["batch_ids"]),
    ).fetchall()


def finalize(c, run_id, actor: str, finalize_key: str) -> dict:
    if not finalize_key:
        raise ValueError("finalize_key is required (makes retries safe)")
    prior = c.execute("select * from payroll_runs where finalize_key=%s", (finalize_key,)).fetchone()
    if prior:
        if str(prior["id"]) != str(run_id):
            raise ValueError("This finalize key was already used for a different run")
        return prior  # retry of a completed finalization
    run = c.execute("select * from payroll_runs where id=%s for update", (run_id,)).fetchone()
    if not run:
        raise LookupError("run not found")
    if run["status"] != "calculated":
        raise ValueError(f"Run is {run['status']}; only the current calculated revision can be finalized")
    week = run["week"]
    c.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"finalize:{week}",))
    current = c.execute("select * from payroll_runs where week=%s and status='finalized' for update", (week,)).fetchone()
    if current and str(run["supersedes"] or "") != str(current["id"]):
        raise ValueError(f"{week} is already finalized (revision {current['revision']}). Start a correction instead.")
    # The old final's own entries and evidence are dated this week, so they never enter the hash.
    *_, h = gather(c, week, run["rule_set_version"])
    if h != run["inputs_hash"]:
        raise FinalizeBlocked("Inputs changed since this revision was calculated (imports, adjustments, rules or balances). Recalculate first.")
    blockers = blocking_items(c, run)
    if blockers:
        raise FinalizeBlocked(f"{len(blockers)} blocking review item(s) must be resolved first", blockers)

    if current:
        _retire(c, current, actor, run["correction_reason"] or "correction")

    adjustments = _adjustments(week_adjustments(c, week))
    from ..payroll.calculate import PayrollResult, PersonResult  # local: rebuild the result from stored rows
    people = []
    for p in c.execute("select data from run_people where run_id=%s", (run_id,)):
        d = p["data"]
        pr = PersonResult(payee=d["payee"])
        for k, v in d.items():
            cur = getattr(pr, k, None)
            setattr(pr, k, D(v) if hasattr(cur, "quantize") else v)
        people.append(pr)
    ledger.post_run(c, run_id, week, PayrollResult(week, people, [], [], {}), adjustments, actor)

    c.execute(
        "insert into payout_evidence(vendor,enrollment_id,payee,activity,amount,rate,week,run_id,source,proof,created_by)"
        " select vendor, enrollment_id, payee, activity, amount, rate, %s, run_id, 'payroll_run', %s, %s from run_lines where run_id=%s",
        (week, f"{week} rev {run['revision']}", actor, run_id),
    )
    c.execute(
        "insert into processed_chargebacks(vendor,enrollment_id,week,run_id,source,note)"
        " select distinct vendor, enrollment_id, %s, run_id, 'payroll_run', 'reversed in payroll' from run_chargebacks where run_id=%s and verified"
        " on conflict do nothing", (week, run_id))
    c.execute(
        "insert into processed_chargebacks(vendor,enrollment_id,week,run_id,source,note)"
        " select distinct vendor, enrollment_id, %s, run_id, 'payroll_run', 'company-only reversal' from run_items"
        " where run_id=%s and kind='company_adjustment' and data->>'reason' ilike 'Vendor duplicate-payment%%' on conflict do nothing", (week, run_id))
    c.execute("insert into payments(run_id,person,amount,state,updated_by) select run_id, payee, final_check, 'calculated', %s from run_people"
              " where run_id=%s and final_check > 0", (actor, run_id))
    _net_confirmed(c, run_id, week, actor)
    run = c.execute("update payroll_runs set status='finalized', finalized_by=%s, finalized_at=now(), finalize_key=%s where id=%s returning *",
                    (actor, finalize_key, run_id)).fetchone()
    audit(c, actor, "payroll.finalize", "payroll_run", str(run_id), None, {"week": week, "revision": run["revision"], "totals": run["totals"]})
    return run


def _net_confirmed(c, run_id, week: str, actor: str) -> None:
    """After a correction, pay only the difference over what was already confirmed for the week."""
    paid = c.execute("select p.person, sum(p.amount) amt from payments p join payroll_runs r on r.id=p.run_id"
                     " where r.week=%s and r.id <> %s and p.state='confirmed' group by 1", (week, run_id)).fetchall()
    for x in paid:
        new = c.execute("select final_check from run_people where run_id=%s and payee=%s", (run_id, x["person"])).fetchone()
        due = (new["final_check"] if new else ZERO) - x["amt"]
        if due > 0:
            c.execute("update payments set amount=%s where run_id=%s and person=%s", (due, run_id, x["person"]))
        else:
            c.execute("delete from payments where run_id=%s and person=%s and state='calculated'", (run_id, x["person"]))
        if due < 0:
            c.execute(
                "insert into review_items(scope_key,week,run_id,kind,message,person,amount,blocking) values (%s,%s,%s,'overpayment',%s,%s,%s,false)"
                " on conflict (scope_key) do nothing",
                (f"overpay:{run_id}:{x['person']}", week, run_id,
                 f"{x['person']} was paid {x['amt']} but the corrected check is {new['final_check'] if new else 0}; recover {-due} with an other_charge in an open week",
                 x["person"], -due))


def _retire(c, old: dict, actor: str, reason: str) -> None:
    later = c.execute("select week from payroll_runs where status='finalized' and week > %s order by week", (old["week"],)).fetchall()
    if later:
        raise FinalizeBlocked(f"Later weeks are finalized ({', '.join(r['week'] for r in later)}); correct them in order or post an adjustment in the open week instead.")
    ledger.reverse_run(c, old["id"], actor, f"Superseded by correction: {reason}")
    c.execute("update payout_evidence set active=false where run_id=%s", (old["id"],))
    c.execute("update processed_chargebacks set active=false where run_id=%s", (old["id"],))
    for p in c.execute("select * from payments where run_id=%s", (old["id"],)).fetchall():
        if p["state"] in ("calculated", "submitted", "failed"):
            c.execute("update payments set state='void', updated_by=%s, updated_at=now() where id=%s", (actor, p["id"]))
        elif p["state"] == "confirmed":
            c.execute(
                "insert into review_items(scope_key,week,kind,message,person,amount,blocking,data) values (%s,%s,'confirmed_payment_on_corrected_run',%s,%s,%s,false,%s)"
                " on conflict (scope_key) do nothing",
                (f"paycorr:{p['id']}", old["week"], f"{p['person']} was already paid {p['amount']} on revision {old['revision']}; the corrected run pays only the difference",
                 p["person"], p["amount"], J({"payment_id": str(p["id"])})),
            )
    c.execute("update payroll_runs set status='superseded' where id=%s", (old["id"],))
    audit(c, actor, "payroll.supersede", "payroll_run", str(old["id"]), None, {"week": old["week"], "revision": old["revision"]}, reason)


def start_correction(c, week: str, actor: str, reason: str, rule_version: int | None = None) -> dict:
    if not reason:
        raise ValueError("A correction needs a reason")
    current = c.execute("select * from payroll_runs where week=%s and status='finalized'", (week,)).fetchone()
    if not current:
        raise ValueError(f"{week} has no finalized run to correct")
    return calculate(c, week, actor, rule_version, supersedes=current["id"], correction_reason=reason)


# -- views -------------------------------------------------------------------------

def run_view(c, run_id) -> dict:
    run = c.execute("select * from payroll_runs where id=%s", (run_id,)).fetchone()
    if not run:
        raise LookupError("run not found")
    people = c.execute("select payee, final_check, status, data from run_people where run_id=%s order by upper(payee)", (run_id,)).fetchall()
    review = c.execute("select * from review_items where run_id=%s or batch_id = any(%s) order by blocking desc, kind", (run_id, run["batch_ids"])).fetchall()
    return dict(run) | {"people": people, "review": review}


def person_detail(c, run_id, payee: str) -> dict:
    p = c.execute("select * from run_people where run_id=%s and payee=%s", (run_id, payee)).fetchone()
    if not p:
        raise LookupError("person not in run")
    return {
        "result": p["data"],
        "lines": c.execute("select * from run_lines where run_id=%s and payee=%s order by vendor, sale_date, enrollment_id", (run_id, payee)).fetchall(),
        "chargebacks": c.execute("select * from run_chargebacks where run_id=%s and payee=%s", (run_id, payee)).fetchall(),
        "nonpayable": c.execute("select * from run_items where run_id=%s and payee=%s and kind in ('nonpayable','pending')", (run_id, payee)).fetchall(),
    }


def compare(c, run_a, run_b) -> list[dict]:
    """Per-person differences between two revisions (shown before finalizing a correction)."""
    a = {r["payee"]: r["data"] for r in c.execute("select payee, data from run_people where run_id=%s", (run_a,))}
    b = {r["payee"]: r["data"] for r in c.execute("select payee, data from run_people where run_id=%s", (run_b,))}
    out = []
    for n in sorted(set(a) | set(b), key=str.upper):
        da, db = a.get(n, {}), b.get(n, {})
        diffs = {k: {"before": da.get(k, "0"), "after": db.get(k, "0")} for k in sorted(set(da) | set(db))
                 if k not in ("notes", "collection_basis", "advance_by_creditor", "payee") and str(da.get(k, "0")) != str(db.get(k, "0"))}
        if diffs:
            out.append({"payee": n, "diffs": diffs})
    return out


def preview_rules(c, week: str, draft_version: int) -> dict:
    """Affected checks if ``draft_version`` were active: recalculates in a savepoint and discards it."""
    with c.transaction(force_rollback=True):
        base = calculate(c, week, "preview", None)
        new = calculate(c, week, "preview", draft_version)
        diffs = compare(c, base["id"], new["id"])
        totals = {"before": base["totals"]["total_checks"], "after": new["totals"]["total_checks"]}
    return {"week": week, "draft_version": draft_version, "affected_people": diffs, "totals": totals}

"""Imports, finalization, corrections, payments and access control against Postgres (fictional data)."""
import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import SAMPLE

W = "2026-W43"
MAPPING = {"enrollment_id": "Account", "raw_rep": "Agent", "kwh": "kWh", "product": "Product", "utility": "Utility",
           "vendor_amount": "Commission", "sale_date": "Signed", "status": "Status", "invoice_date": "Invoice Date"}
OPTS = {"vendor": "Nexamp", "mapping": MAPPING, "payable_statuses": ["Approved"], "nonpayable_statuses": ["Cancelled"]}


def write_csv(path: Path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Account", "Agent", "kWh", "Product", "Utility", "Commission", "Signed", "Status", "Invoice Date"])
        w.writerows(rows)
    return path


ROWS = [
    ["N-100", "Alex Rivera", "1000", "LMI", "SOUTH ELECTRIC", "30", "2026-10-01", "Approved", "2026-10-20"],
    ["N-101", "A. Rivera", "2000", "OPEN", "SOUTH ELECTRIC", "40", "2026-10-02", "Approved", "2026-10-20"],
    ["N-102", "Sam Patel", "1500", "LMI", "NORTH POWER", "35", "2026-10-03", "Approved", "2026-10-20"],
    ["N-103", "Jordan Lee", "1000", "LMI", "SOUTH ELECTRIC", "30", "2026-10-04", "Approved", "2026-10-20"],
    ["N-104", "Sam Patel", "900", "LMI", "SOUTH ELECTRIC", "0", "2026-10-05", "Cancelled", ""],
    ["N-099", "Alex Rivera", "800", "LMI", "SOUTH ELECTRIC", "20", "2025-12-15", "Approved", "2026-10-20"],
]


@pytest.fixture
def env(db, tmp_path):
    from emg.db.conn import connect
    from emg.services import auth, ledger, people
    from emg.services import rules as rules_svc
    from emg.services.storage import get_storage

    cfg = json.loads(SAMPLE.read_text())
    with connect(db) as c:
        v = rules_svc.create(c, cfg, "sample", "test")
        rules_svc.activate(c, v["version"], "test", "initial")
        for n in cfg["people"]:
            people.upsert(c, n, "test")
        ledger.seed_openings(c, {"Sam Patel": {"advance": {"EMG": "120"}}, "Alex Rivera": {"chargeback": "5"}}, "2026-W42", "test", "seed")
        auth.create_user(c, "owner@x.test", "Owner", "owner", "owner-password-1", "test")
        auth.create_user(c, "sam@x.test", "Sam", "agent", "agent-password-1", "test", person_name="Sam Patel")
        auth.create_user(c, "jordan@x.test", "Jordan", "manager", "manager-password-1", "test", person_name="Jordan Lee")
    return {"db": db, "storage": get_storage(), "csv": write_csv(tmp_path / "nexamp_w43.csv", ROWS), "tmp": tmp_path}


def import_csv(c, env, path=None, commit=True):
    from emg.services import imports

    p = path or env["csv"]
    sf = imports.store_upload(c, env["storage"], p.read_bytes(), p.name, "test")
    b = imports.preview(c, env["storage"], sf["id"], W, "test", "generic", OPTS, work_dir=env["tmp"] / "work")
    return imports.commit(c, b["id"], "test") if commit else b


def test_import_preview_commit_and_duplicate_refusal(env):
    from emg.db.conn import connect

    with connect(env["db"]) as c:
        b = import_csv(c, env, commit=False)
        assert b["status"] == "preview"
        assert {r["line_type"]: r["n"] for r in b["line_counts"]} == {"payable": 4, "nonpayable": 1}
        assert len(b["excluded"]) == 1 and "Prior-year" in b["excluded"][0]["reason"]
        assert c.execute("select count(*) n from invoice_lines").fetchone()["n"] == 0   # nothing until commit
        from emg.services import imports

        imports.commit(c, b["id"], "test")
        assert c.execute("select count(*) n from invoice_lines").fetchone()["n"] == 4
        # Same file again: stored once, every money line refused as already imported
        b2 = import_csv(c, env)
        assert b2["source_file_id"] == b["source_file_id"]
        assert b2["summary"]["committed"]["refused_duplicates"] == 4
        assert c.execute("select count(*) n from invoice_lines").fetchone()["n"] == 4
        # Status history recorded with the source row
        h = c.execute("select h.status, h.source_row from enrollment_status_history h join enrollments e on e.id=h.enrollment_pk where e.enrollment_id='N-104'").fetchall()
        assert h[0]["status"] == "Cancelled" and h[0]["source_row"] == 6


def test_status_change_appends_history(env):
    from emg.db.conn import connect

    with connect(env["db"]) as c:
        import_csv(c, env)
        p2 = write_csv(env["tmp"] / "later.csv", [["N-104", "Sam Patel", "900", "LMI", "SOUTH ELECTRIC", "25", "2026-10-05", "Approved", "2026-10-27"]])
        import_csv(c, env, p2)
        hist = c.execute("select h.status from enrollment_status_history h join enrollments e on e.id=h.enrollment_pk"
                         " where e.enrollment_id='N-104' order by h.id").fetchall()
        assert [h["status"] for h in hist] == ["Cancelled", "Paid by vendor"]
        e = c.execute("select sale_date from enrollments where enrollment_id='N-104'").fetchone()
        assert str(e["sale_date"]) == "2026-10-05"


def calc_and_finalize(c, key="k1"):
    from emg.services import runs

    r = runs.calculate(c, W, "test")
    return r, runs.finalize(c, r["id"], "test", key)


def test_calculate_finalize_idempotent_and_ledger(env):
    from emg.db.conn import connect
    from emg.services import ledger, runs

    with connect(env["db"]) as c:
        import_csv(c, env)
        r = runs.calculate(c, W, "test")
        people = {p["payee"]: p["data"] for p in runs.run_view(c, r["id"])["people"]}
        # Sam: 1500 kWh NORTH LMI at 0.012 = 18.00; standing $50 advance rule capped by available -> 18.00
        assert people["Sam Patel"]["gross_compensation"] == "18.00" and people["Sam Patel"]["advance_deduction"] == "18.00"
        # Jordan: own LMI sale 10.00 + 4.00 personal top-up; overrides: Alex LMI .002*1000 + Alex OPEN .002*2000 + Sam NORTH remainder .003*1500
        assert people["Jordan Lee"]["agent_commission"] == "10.00"
        assert people["Jordan Lee"]["manager_override"] == str(Decimal("4.00") + Decimal("2.00") + Decimal("4.00") + Decimal("4.50"))
        assert people["Alex Rivera"]["chargeback_recovery"] == "5.00"
        f = runs.finalize(c, r["id"], "test", "k1")
        assert f["status"] == "finalized"
    with connect(env["db"]) as c:
        assert runs.finalize(c, r["id"], "test", "k1")["status"] == "finalized"     # retry is harmless
        n_entries = c.execute("select count(*) n from ledger_entries").fetchone()["n"]
        with pytest.raises(ValueError):
            runs.calculate(c, W, "test")                                            # finalized week is closed
        assert c.execute("select count(*) n from ledger_entries").fetchone()["n"] == n_entries
        bal = {(b["person"], b["debt_type"]): b["balance"] for b in ledger.balances(c)}
        assert bal[("Sam Patel", "advance")] == Decimal("102.00")
        assert ("Alex Rivera", "chargeback") not in bal
        assert ledger.openings(c, "2026-W44")["Sam Patel"].advance == {"EMG": Decimal("102.00")}


def test_stale_and_blocking_review_prevent_finalize(env):
    from emg.db.conn import connect
    from emg.services import adjustments, runs

    with connect(env["db"]) as c:
        import_csv(c, env)
        r = runs.calculate(c, W, "test")
        adjustments.add(c, W, "Alex Rivera", "room_charge", "test", "Manager", amount="20")
        with pytest.raises(runs.FinalizeBlocked, match="Inputs changed"):
            runs.finalize(c, r["id"], "test", "k1")
        p = write_csv(env["tmp"] / "bad.csv", [["N-200", "Alex Rivera", "100", "MYSTERY", "SOUTH ELECTRIC", "3", "2026-10-06", "Approved", "2026-10-20"]])
        import_csv(c, env, p)
        r2 = runs.calculate(c, W, "test")
        with pytest.raises(runs.FinalizeBlocked) as e:
            runs.finalize(c, r2["id"], "test", "k2")
        assert e.value.items[0]["kind"] == "product_unknown"
        c.execute("update review_items set status='resolved', resolution='Ask vendor; unpaid this week' where kind='product_unknown'")
        assert runs.finalize(c, r2["id"], "test", "k2")["status"] == "finalized"


def test_correction_reverses_and_nets_confirmed_payment(env):
    from emg.db.conn import connect
    from emg.services import adjustments, ledger, payments, runs

    with connect(env["db"]) as c:
        import_csv(c, env)
        adjustments.add(c, W, "Alex Rivera", "cash_credit", "test", "Owner", amount="100", label="bonus")
        r, f = calc_and_finalize(c)
        alex = next(p for p in payments.for_run(c, f["id"]) if p["person"] == "Alex Rivera")
        payments.set_state(c, alex["id"], "submitted", "test")
        payments.set_state(c, alex["id"], "confirmed", "test", "PAYCHEX-1")
        adv_before = ledger.balances(c, "Sam Patel")
        corr = runs.start_correction(c, W, "test", "Bonus was 60")
        bonus = next(a for a in adjustments.list_week(c, W) if a["kind"] == "cash_credit")
        adjustments.void(c, bonus["id"], "test", "wrong")
        adjustments.add(c, W, "Alex Rivera", "cash_credit", "test", "Owner", amount="60", label="bonus")
        corr = runs.calculate(c, W, "test")
        diff = runs.compare(c, f["id"], corr["id"])
        assert [d["payee"] for d in diff] == ["Alex Rivera"]
        f2 = runs.finalize(c, corr["id"], "test", "k-corr")
        assert f2["revision"] == 3 and f2["supersedes"] == f["id"]
        states = {(p["run_id"], p["person"]): p for p in c.execute("select * from payments").fetchall()}
        assert states[(f["id"], "Alex Rivera")]["state"] == "confirmed"
        assert (f2["id"], "Alex Rivera") not in states                       # already overpaid: nothing more to pay
        over = c.execute("select amount from review_items where kind='overpayment'").fetchone()
        assert over["amount"] == Decimal("40.0000")
        assert ledger.balances(c, "Sam Patel") == adv_before                 # reversal + repost leaves balances intact
        assert c.execute("select count(*) n from payroll_runs where week=%s and status='finalized'", (W,)).fetchone()["n"] == 1


def test_adjustment_guards(env):
    from emg.db.conn import connect
    from emg.services import adjustments

    with connect(env["db"]) as c:
        adjustments.add(c, W, "Sam Patel", "new_advance", "test", "Owner", amount="100", external_ref="IMG_1.PNG:1")
        with pytest.raises(ValueError, match="Duplicate"):
            adjustments.add(c, W, "Sam Patel", "new_advance", "test", "Owner", amount="100", external_ref="IMG_1.PNG:1")
        with pytest.raises(ValueError, match="not a known person"):
            adjustments.add(c, W, "Nobody", "room_charge", "test", "Owner", amount="10")
        with pytest.raises(ValueError, match="authorized"):
            adjustments.add(c, W, "Sam Patel", "room_charge", "test", "", amount="10")


def test_payment_state_machine(env):
    from emg.db.conn import connect
    from emg.services import payments

    with connect(env["db"]) as c:
        import_csv(c, env)
        _, f = calc_and_finalize(c)
        p = payments.for_run(c, f["id"])[0]
        with pytest.raises(ValueError):
            payments.set_state(c, p["id"], "confirmed", "test", "X")            # must be submitted first
        payments.set_state(c, p["id"], "submitted", "test")
        with pytest.raises(ValueError, match="provider reference"):
            payments.set_state(c, p["id"], "confirmed", "test")
        assert payments.set_state(c, p["id"], "confirmed", "test", "REF")["state"] == "confirmed"
        assert payments.set_state(c, p["id"], "confirmed", "test")["state"] == "confirmed"   # idempotent


def test_api_roles_and_money_strings(env):
    from fastapi.testclient import TestClient

    from emg.api.app import app
    from emg.db.conn import connect

    with connect(env["db"]) as c:
        import_csv(c, env)
        r, f = calc_and_finalize(c)
    cl = TestClient(app)

    def tok(email, pw):
        return {"Authorization": "Bearer " + cl.post("/api/auth/login", json={"email": email, "password": pw}).json()["token"]}

    owner, sam, jordan = tok("owner@x.test", "owner-password-1"), tok("sam@x.test", "agent-password-1"), tok("jordan@x.test", "manager-password-1")
    run = cl.get(f"/api/runs/{f['id']}", headers=owner).json()
    assert isinstance(run["people"][0]["final_check"], str)
    assert cl.get(f"/api/runs/{f['id']}/people/Sam Patel", headers=sam).status_code == 200
    assert cl.get(f"/api/runs/{f['id']}/people/Alex Rivera", headers=sam).status_code == 403
    assert cl.get(f"/api/runs/{f['id']}/people/Alex Rivera", headers=jordan).status_code == 200   # on Jordan's team
    assert cl.get(f"/api/runs/{f['id']}/people/Robin Shaw", headers=jordan).status_code == 403
    assert {p["payee"] for p in cl.get(f"/api/runs/{f['id']}", headers=sam).json()["people"]} == {"Sam Patel"}
    assert cl.post(f"/api/weeks/{W}/calculate", headers=sam).status_code == 403
    assert cl.get("/api/audit", headers=jordan).status_code == 403
    assert cl.get("/api/ledger?person=Sam Patel", headers=sam).json()[0]["balance"] == "102.00"
    assert cl.get("/api/dashboard").status_code == 401


def test_job_queue_claims_once_and_retries(env, monkeypatch):
    from emg.db.conn import connect
    from emg.services import jobs

    calls = []

    @jobs.handler("test.flaky")
    def flaky(c, p, actor):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    with connect(env["db"]) as c:
        j = jobs.enqueue(c, "test.flaky", {}, "test")
    assert jobs.run_one("w1")
    with connect(env["db"]) as c:
        row = jobs.get(c, j["id"])
        assert row["status"] == "queued" and row["attempts"] == 1
        c.execute("update jobs set run_after=now() where id=%s", (j["id"],))
    assert jobs.run_one("w2")
    with connect(env["db"]) as c:
        assert jobs.get(c, j["id"])["status"] == "done"
    assert not jobs.run_one("w3")

"""Acceptance: replay the finalized Week 43 from its original files.

Runs only where the private fixtures exist (never committed):
  EMG_FIXTURES=/path/to/Payroll_Oct23 EMG_RULES_OVERLAY=/path/to/rules_overlay.json pytest tests/test_replay_w43.py
"""
import os
from pathlib import Path

import pytest

FIX = Path(os.environ.get("EMG_FIXTURES", "/mnt/project-files/emg-private/fixtures/Payroll_Oct23"))
OVERLAY = os.environ.get("EMG_RULES_OVERLAY", "/mnt/project-files/emg-private/config/rules_overlay_2026-W43_spark.json")
pytestmark = pytest.mark.skipif(not (FIX / "week43_calc.json").exists(), reason="private W43 fixtures not present")


def test_engine_replay_matches_final_for_every_person():
    from emg.replay.historical import replay_w43

    rep, bundle = replay_w43(FIX, OVERLAY if Path(OVERLAY).exists() else None)
    assert rep.ok, rep.to_dict()["mismatches"][:5]
    t = rep.totals_app
    assert t["total_checks"] == "17591.81" and t["positive_checks"] == 28
    assert t["company_payables"] == {"AEG": "5760.00"}
    assert t["nonpayable_rows_emg"] == 302


def test_database_replay_and_finalize(db):
    from emg.cli import db_replay_w43
    from emg.db.conn import connect
    from emg.services import ledger, runs
    from emg.services.storage import get_storage

    with connect(db) as c:
        out = db_replay_w43(c, get_storage(), FIX, Path(OVERLAY) if Path(OVERLAY).exists() else None, "test")
        assert out["ok"], out["mismatches"][:5]
        assert out["totals"]["total_checks"] == "17591.81"
        runs.finalize(c, out["run_id"], "test", "w43")
        # Every closing balance equals the final's ending balance, including unresolved payment contacts
        import json

        calc = json.loads((FIX / "week43_calc.json").read_text())
        unresolved = [r["person"] for r in c.execute("select person from review_items where kind='identity_unresolved'")]
        assert unresolved
        for s in calc["summary"]:
            if s["payee"] in unresolved:
                adv = sum((b["balance"] for b in ledger.balances(c, s["payee"]) if b["debt_type"] == "advance"), 0)
                assert f"{adv:.2f}" == f"{s['advance_ending']:.2f}"


def test_nonpayable_attribution_matches_final():
    import collections
    import json

    from emg.replay.historical import replay_w43

    _, b = replay_w43(FIX, OVERLAY if Path(OVERLAY).exists() else None)
    calc = json.loads((FIX / "week43_calc.json").read_text())
    mine = collections.Counter(r.raw["seller"] for r in b.nonpayable if "company_payee" not in r.raw)
    assert mine == collections.Counter(r["payee"] for r in calc["nonpayable"])


def test_reports_match_final_package(db, tmp_path):
    import csv
    import io

    import openpyxl

    from emg.cli import db_replay_w43
    from emg.db.conn import connect
    from emg.reports.naming import slug
    from emg.services import reports, runs
    from emg.services.storage import get_storage

    st = get_storage()
    with connect(db) as c:
        out = db_replay_w43(c, st, FIX, Path(OVERLAY) if Path(OVERLAY).exists() else None, "test")
        runs.finalize(c, out["run_id"], "test", "w43")
        reports.build(c, st, out["run_id"], "test", work_dir=tmp_path)
        cur = {(r["kind"], r["person"]): r for r in reports.list_current(c, out["run_id"])}
        get = lambda k: st.get_bytes(c.execute("select storage_key from report_versions where id=%s", (cur[k]["id"],)).fetchone()["storage_key"])  # noqa: E731
        # Same statement recipients as the final package
        original = {p.name.rsplit("_2026", 1)[0] for p in (FIX / "individual_reports").glob("*.pdf")}
        assert {slug(p) for k, p in cur if k == "agent_pdf"} == original
        # Paychex import equals the final payable checks
        final = {r["Agent"]: r["Final Check"] for r in csv.DictReader(open(FIX / "EMG_Payable_Checks_Only_2026-10-23.csv"))}
        assert dict(list(csv.reader(io.StringIO(get(("paychex_csv", None)).decode())))[1:]) == final
        # AEG repriced workbook equals the reference (ignoring blank trailing rows)
        ref = FIX.parent / "AEG_Spark_Invoice_20261017_Repriced_90.xlsx"
        if ref.exists():
            a, b = openpyxl.load_workbook(io.BytesIO(get(("aeg_workbook", "AEG")))), openpyxl.load_workbook(ref)
            rows = lambda ws: [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]  # noqa: E731
            assert rows(a["DETAILS"]) == rows(b["DETAILS"])
            assert rows(a["SUMMARY"]) == rows(b["SUMMARY"])

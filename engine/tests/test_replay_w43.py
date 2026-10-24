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

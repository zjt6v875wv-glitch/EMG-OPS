"""Reports, versioning and statement email (fictional data, outbox provider)."""
import csv
import email
import email.policy
import io
import zipfile

import pytest

from test_db_flow import W, calc_and_finalize, env, import_csv  # noqa: F401  (fixture re-export)


def _finalized_with_reports(env):
    from emg.db.conn import connect
    from emg.services import people, reports

    with connect(env["db"]) as c:
        import_csv(c, env)
        people.upsert(c, "Alex Rivera", "test", email="alex@example.test", email_verified=True)
        people.upsert(c, "Sam Patel", "test", email="sam@example.test", email_verified=False)
        people.upsert(c, "Jordan Lee", "test", email="jordan@example.test", email_verified=True)
        _, f = calc_and_finalize(c)
        reports.build(c, env["storage"], f["id"], "test", work_dir=env["tmp"] / "rep")
    return f


def test_report_files_names_and_paychex(env):
    from emg.db.conn import connect
    from emg.services import reports

    f = _finalized_with_reports(env)
    with connect(env["db"]) as c:
        cur = {(r["kind"], r["person"]): r for r in reports.list_current(c, f["id"])}
        names = {r["file_name"] for r in cur.values()}
        assert "EMG_Week43_Final_Package_2026-10-23.zip" in names
        assert "Alex_Rivera_Week43_2026-10-23.pdf" in names
        assert {p for k, p in cur if k == "agent_pdf"} == {"Alex Rivera", "Jordan Lee", "Sam Patel"}
        key = c.execute("select storage_key from report_versions where id=%s", (cur[("paychex_csv", None)]["id"],)).fetchone()["storage_key"]
        rows = list(csv.reader(io.StringIO(env["storage"].get_bytes(key).decode())))
        assert rows[0] == ["Contractor", "1099-NEC Amount"]
        checks = {p["payee"]: str(p["final_check"]) for p in c.execute("select payee, final_check from run_people where run_id=%s and final_check > 0", (f["id"],))}
        assert dict(rows[1:]) == checks
        pkg = c.execute("select storage_key from report_versions where id=%s", (cur[("package_zip", None)]["id"],)).fetchone()["storage_key"]
        z = zipfile.ZipFile(io.BytesIO(env["storage"].get_bytes(pkg)))
        assert {"manifest.json", "rules_snapshot.json", "sources/nexamp_w43.csv"} <= set(z.namelist())
        assert any(n.startswith("individual_reports/Alex_Rivera") for n in z.namelist())


def test_rebuild_is_stable_and_correction_keeps_prior_final(env):
    from emg.db.conn import connect
    from emg.services import adjustments, reports, runs

    f = _finalized_with_reports(env)
    with connect(env["db"]) as c:
        n = c.execute("select count(*) n from report_versions").fetchone()["n"]
        reports.build(c, env["storage"], f["id"], "test", kinds=("paychex_csv", "payable_csv", "master_xlsx"), work_dir=env["tmp"] / "rep")
        assert c.execute("select count(*) n from report_versions").fetchone()["n"] == n   # same bytes: no new versions
        runs.start_correction(c, W, "test", "add bonus")
        adjustments.add(c, W, "Alex Rivera", "cash_credit", "test", "Owner", amount="10", label="bonus")
        corr = runs.calculate(c, W, "test")
        runs.finalize(c, corr["id"], "test", "k-corr")
        reports.build(c, env["storage"], corr["id"], "test", work_dir=env["tmp"] / "rep2")
        new = {r["file_name"] for r in reports.list_current(c, corr["id"])}
        assert "Alex_Rivera_Week43_Final_Rev3_2026-10-23.pdf" in new and "EMG_Week43_Final_Rev3_Package_2026-10-23.zip" in new
        old = reports.list_current(c, f["id"])
        assert any(r["file_name"] == "EMG_Week43_Final_Package_2026-10-23.zip" for r in old)   # prior final kept


def test_email_rules_and_idempotency(env):
    from emg.db.conn import connect
    from emg.services import email as em

    f = _finalized_with_reports(env)
    with connect(env["db"]) as c:
        em.exclude(c, "Jordan Lee", "Asked for paper statements", "test")
        pv = {r["person"]: r for r in em.preview(c, f["id"])["rows"]}
        assert pv["Alex Rivera"]["state"] == "ready"
        assert pv["Sam Patel"]["state"] == "unverified_email"
        assert pv["Jordan Lee"]["state"] == "excluded"
        with pytest.raises(ValueError, match="Type SEND"):
            em.authorize(c, f["id"], ["Alex Rivera"], "test", "yes")
        res = em.authorize(c, f["id"], ["Alex Rivera", "Sam Patel", "Jordan Lee"], "test", f"SEND {W}")
        assert res["queued"] == ["Alex Rivera"] and {r["person"] for r in res["refused"]} == {"Sam Patel", "Jordan Lee"}
        again = em.authorize(c, f["id"], ["Alex Rivera"], "test", f"SEND {W}")
        assert again["queued"] == []                                                   # no duplicate authorization
        send = c.execute("select * from email_sends").fetchone()
    with connect(env["db"]) as c:
        s = em.deliver(c, send["id"])
        assert s["status"] == "sent" and s["provider"] == "outbox"
    with connect(env["db"]) as c:
        assert em.deliver(c, send["id"])["status"] == "sent"                           # retry does not resend
        assert c.execute("select count(*) n from jobs where kind='email.send'").fetchone()["n"] == 1
        # The message: one recipient, no Cc/Bcc, only Alex's own statement attached
        key = next(k for k in env["storage"].root.rglob("*.eml"))
        msg = email.message_from_bytes(key.read_bytes(), policy=email.policy.default)
        assert msg["To"] == "alex@example.test" and msg["Cc"] is None and msg["Bcc"] is None
        atts = [p.get_filename() for p in msg.iter_attachments()]
        assert atts == ["Alex_Rivera_Week43_2026-10-23.pdf"]
        em.record_event(c, s["provider_message_id"], "bounced", "mailbox full")
        assert c.execute("select status from email_sends where id=%s", (send["id"],)).fetchone()["status"] == "bounced"
        # Emailing never touches payments
        assert {p["state"] for p in c.execute("select state from payments")} == {"calculated"}


def test_preliminary_is_not_sendable_by_default(env):
    from emg.db.conn import connect
    from emg.services import email as em
    from emg.services import people, reports, runs

    with connect(env["db"]) as c:
        import_csv(c, env)
        people.upsert(c, "Alex Rivera", "test", email="alex@example.test", email_verified=True)
        r = runs.calculate(c, W, "test")
        reports.build(c, env["storage"], r["id"], "test", kinds=("agent_pdf",), work_dir=env["tmp"] / "rep")
        assert any("Preliminary" in x["file_name"] for x in reports.list_current(c, r["id"]))
        with pytest.raises(ValueError, match="finalized"):
            em.authorize(c, r["id"], ["Alex Rivera"], "test", f"SEND {W}")

"""Statement email distribution.

Rules enforced here:
* one recipient per message, To only (no CC/BCC), attachment = that person's own current report;
* recipients come only from verified roster emails; never guessed;
* exclusions are kept (people.email_excluded and email_exclusions);
* nothing is sent without an explicit authorization naming the people;
* the idempotency key is run + person + report hash, so retries never send twice and a new report
  version (after a correction) can be sent once more;
* a send interrupted after handing the message to the provider is marked "unknown" and never retried
  automatically;
* emailing never changes a payment.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..db.conn import J, audit
from ..money import D
from ..reports.pdf import dollars
from ..weeks import parse_week, pay_date
from .jobs import enqueue, handler
from .storage import get_storage

DEFAULT_TEMPLATE = {
    "subject": "EMG pay statement - Week {week_no} ({pay_date})",
    "body": ("Hi {first_name},\n\nAttached is your Energy Marketing Group pay statement for Week {week_no}, pay date {pay_date_long}.\n\n"
             "{check_line}\n\nIf anything looks wrong, reply to this email and payroll will review it.\n\nEnergy Marketing Group Payroll"),
}
SENT_STATES = ("sent", "delivered")


def template(c) -> dict:
    row = c.execute("select value from settings where key='email_template'").fetchone()
    return row["value"] if row else DEFAULT_TEMPLATE


def save_template(c, subject: str, body: str, actor: str) -> dict:
    for token in ("{first_name}", "{check_line}"):
        if token not in body:
            raise ValueError(f"The body must include {token}")
    value = {"subject": subject, "body": body}
    c.execute("insert into settings(key,value,updated_by) values ('email_template',%s,%s) on conflict (key) do update set value=excluded.value,"
              " updated_by=excluded.updated_by, updated_at=now()", (J(value), actor))
    audit(c, actor, "email.template", "settings", "email_template", None, value)
    return value


def _render(tpl: dict, week: str, person: str, check, status: str) -> tuple[str, str]:
    pd = pay_date(week)
    check = D(check)
    if check > 0:
        line = f"Your check this week: {dollars(check)}."
    elif status == "HELD":
        line = "Your pay is held this week. The statement explains why."
    else:
        line = "There is no check this week. The statement shows the details."
    vals = {"first_name": person.split()[0].title() if person.split()[0].isupper() else person.split()[0], "name": person,
            "week": week, "week_no": parse_week(week)[1], "pay_date": pd.isoformat(), "pay_date_long": f"{pd.strftime('%A, %B')} {pd.day}, {pd.year}",
            "check": dollars(check), "check_line": line}
    return tpl["subject"].format(**vals), tpl["body"].format(**vals)


def preview(c, run_id) -> dict:
    run = c.execute("select * from payroll_runs where id=%s", (run_id,)).fetchone()
    if not run:
        raise LookupError("run not found")
    reports = {r["person"]: r for r in c.execute("select * from report_versions where run_id=%s and kind='agent_pdf' and current", (run_id,))}
    people = {p["name"]: p for p in c.execute("select * from people")}
    excluded = {e["person"]: e["reason"] for e in c.execute("select * from email_exclusions")}
    results = {r["payee"]: r for r in c.execute("select payee, final_check, status from run_people where run_id=%s", (run_id,))}
    sends: dict[str, list] = {}
    for s in c.execute("select s.*, r.run_id report_run from email_sends s join report_versions r on r.id=s.report_version_id"
                       " where s.person = any(%s) and r.week=%s order by s.authorized_at", (list(reports), run["week"])):
        sends.setdefault(s["person"], []).append(s)
    tpl = template(c)
    rows = []
    for person, rep in sorted(reports.items(), key=lambda kv: kv[0].upper()):
        p = people.get(person) or {}
        res = results.get(person) or {"final_check": 0, "status": "NO CHECK"}
        prior = sends.get(person, [])
        same = [s for s in prior if s["report_sha256"] == rep["sha256"]]
        if person in excluded or p.get("email_excluded"):
            state, reason = "excluded", excluded.get(person) or p.get("email_status") or "Excluded"
        elif not p.get("email"):
            state, reason = "missing_email", "No email on the roster"
        elif not p.get("email_verified"):
            state, reason = "unverified_email", f"{p['email']} is not verified"
        elif same and same[-1]["status"] in SENT_STATES + ("authorized", "sending", "unknown"):
            state, reason = "already_sent" if same[-1]["status"] in SENT_STATES else same[-1]["status"], f"This report version: {same[-1]['status']}"
        elif any(s["status"] in SENT_STATES for s in prior):
            state, reason = "ready_new_version", "An earlier version was sent; this replaces it"
        else:
            state, reason = "ready", ""
        subject, _ = _render(tpl, run["week"], person, res["final_check"], res["status"])
        rows.append({"person": person, "email": p.get("email") if state not in ("missing_email",) else None, "state": state, "reason": reason,
                     "final_check": res["final_check"], "pay_status": res["status"], "report_version_id": rep["id"], "file_name": rep["file_name"],
                     "report_sha256": rep["sha256"], "label": rep["label"], "subject": subject,
                     "history": [{"status": s["status"], "recipient": s["recipient"], "sent_at": s["sent_at"], "message_id": s["provider_message_id"],
                                  "same_version": s["report_sha256"] == rep["sha256"]} for s in prior]})
    skipped_no_report = sorted(n for n, r in results.items() if n not in reports and D(r["final_check"]) > 0)
    return {"run_id": str(run_id), "week": run["week"], "run_status": run["status"], "label": reports and next(iter(reports.values()))["label"],
            "send_enabled": os.environ.get("EMAIL_SEND_ENABLED") == "1", "rows": rows, "paid_without_report": skipped_no_report,
            "counts": {s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}}}


def authorize(c, run_id, people: list[str], actor: str, confirm: str, allow_preliminary: bool = False) -> dict:
    """Explicit authorization: the caller lists exactly who receives their statement and types the week to confirm."""
    run = c.execute("select * from payroll_runs where id=%s", (run_id,)).fetchone()
    if not run:
        raise LookupError("run not found")
    if confirm != f"SEND {run['week']}":
        raise ValueError(f"Type SEND {run['week']} to authorize sending")
    if run["status"] != "finalized" and not allow_preliminary:
        raise ValueError("Only finalized statements can be sent (preliminary sending must be explicitly allowed)")
    pv = {r["person"]: r for r in preview(c, run_id)["rows"]}
    queued, refused = [], []
    for person in people:
        r = pv.get(person)
        if not r or r["state"] not in ("ready", "ready_new_version"):
            refused.append({"person": person, "reason": r["reason"] if r else "No current report for this person", "state": r and r["state"]})
            continue
        key = f"{run_id}:{person}:{r['report_sha256']}"
        row = c.execute(
            "insert into email_sends(run_id,person,recipient,report_version_id,report_sha256,idempotency_key,status,provider,authorized_by)"
            " values (%s,%s,%s,%s,%s,%s,'authorized',%s,%s) on conflict (idempotency_key) do nothing returning *",
            (run_id, person, r["email"], r["report_version_id"], r["report_sha256"], key, provider_name(), actor),
        ).fetchone()
        if row:
            enqueue(c, "email.send", {"send_id": str(row["id"])}, actor)
            queued.append(person)
        else:
            refused.append({"person": person, "reason": "Already authorized for this report version", "state": "duplicate"})
    audit(c, actor, "email.authorize", "payroll_run", str(run_id), None, {"queued": queued, "refused": refused})
    return {"queued": queued, "refused": refused}


# -- delivery ----------------------------------------------------------------------

def provider_name() -> str:
    return "smtp" if os.environ.get("EMAIL_SEND_ENABLED") == "1" else "outbox"


def _message(c, send: dict) -> EmailMessage:
    rep = c.execute("select * from report_versions where id=%s", (send["report_version_id"],)).fetchone()
    res = c.execute("select final_check, status from run_people where run_id=%s and payee=%s", (rep["run_id"], send["person"])).fetchone()
    subject, body = _render(template(c), rep["week"], send["person"], res["final_check"] if res else 0, res["status"] if res else "")
    msg = EmailMessage()
    msg["From"] = formataddr(("Energy Marketing Group Payroll", os.environ.get("EMAIL_FROM", "payroll@example.com")))
    msg["To"] = send["recipient"]  # exactly one recipient; no Cc/Bcc headers are ever set
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(os.environ.get("EMAIL_FROM", "payroll@example.com").split("@")[-1]))
    msg.set_content(body)
    data = get_storage().get_bytes(rep["storage_key"])
    import hashlib

    if hashlib.sha256(data).hexdigest() != send["report_sha256"]:
        raise RuntimeError("Stored report does not match the authorized version")
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=rep["file_name"])
    return msg


def deliver(c, send_id) -> dict:
    send = c.execute("select * from email_sends where id=%s for update", (send_id,)).fetchone()
    if not send:
        raise LookupError("send not found")
    if send["status"] != "authorized":
        return send  # sent, sending (interrupted -> unknown), failed: never resend automatically
    latest = c.execute("select id from report_versions where id=%s and current", (send["report_version_id"],)).fetchone()
    if not latest:
        c.execute("update email_sends set status='failed', error='Report was replaced before sending; re-authorize the new version', updated_at=now() where id=%s", (send_id,))
        return c.execute("select * from email_sends where id=%s", (send_id,)).fetchone()
    msg = _message(c, send)
    assert len(msg.get_all("To")) == 1 and not msg.get_all("Cc") and not msg.get_all("Bcc")
    # Commit "sending" before talking to the provider so a crash can never cause a second send.
    c.execute("update email_sends set status='sending', provider_message_id=%s, updated_at=now() where id=%s", (msg["Message-ID"], send_id))
    c.commit()
    try:
        if provider_name() == "smtp":
            host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
            with smtplib.SMTP(host, port, timeout=60) as s:
                s.starttls(context=ssl.create_default_context())
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                refused = s.send_message(msg, to_addrs=[send["recipient"]])
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
        else:
            get_storage().put_bytes(msg.as_bytes(), "outbox", f"{send_id}.eml")  # development: nothing leaves the server
        c.execute("update email_sends set status='sent', sent_at=now(), updated_at=now() where id=%s", (send_id,))
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPAuthenticationError, ConnectionRefusedError) as e:
        c.execute("update email_sends set status='failed', error=%s, updated_at=now() where id=%s", (str(e)[:2000], send_id))
    except Exception as e:  # outcome unknown (timeout mid-transfer, etc.): needs a human check, never an automatic resend
        c.execute("update email_sends set status='unknown', error=%s, updated_at=now() where id=%s", (str(e)[:2000], send_id))
    return c.execute("select * from email_sends where id=%s", (send_id,)).fetchone()


@handler("email.send")
def _job(c, payload: dict, actor: str):
    s = deliver(c, payload["send_id"])
    return {"status": s["status"], "message_id": s["provider_message_id"]}


def record_event(c, message_id: str, status: str, detail: str = "", actor: str = "provider") -> dict | None:
    """Provider webhook or manual reconciliation: delivered / bounced / sent / failed."""
    if status not in ("delivered", "bounced", "sent", "failed"):
        raise ValueError("status must be delivered, bounced, sent or failed")
    row = c.execute("update email_sends set status=%s, error=coalesce(nullif(%s,''), error), updated_at=now() where provider_message_id=%s returning *",
                    (status, detail, message_id)).fetchone()
    if row:
        audit(c, actor, f"email.{status}", "email", str(row["id"]), None, {"message_id": message_id}, detail)
    return row


def exclude(c, person: str, reason: str, actor: str) -> None:
    if not reason:
        raise ValueError("Give a reason")
    c.execute("insert into email_exclusions(person,reason,created_by) values (%s,%s,%s) on conflict (person) do update set reason=excluded.reason",
              (person, reason, actor))
    audit(c, actor, "email.exclude", "person", person, None, None, reason)


def include(c, person: str, actor: str) -> None:
    c.execute("delete from email_exclusions where person=%s", (person,))
    audit(c, actor, "email.include", "person", person)


def sends(c, run_id) -> list[dict]:
    return c.execute("select s.*, r.file_name, r.label from email_sends s join report_versions r on r.id=s.report_version_id where s.run_id=%s"
                     " order by upper(s.person), s.authorized_at", (run_id,)).fetchall()

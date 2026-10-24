"""Vendor connectors: a fake vendor portal driven by a real headless browser, API
connectors, encrypted credentials, failure handling and the status sync."""
import json
import os
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from emg.connectors import portal
from emg.connectors.secrets import new_key, totp
from test_db_flow import env  # noqa: F401  (fixture re-export)

CHROME = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
EXPORT = {"rows": "Enrollment ID,Sales Rep,Date Signed,Status,Approved On\n"
                  "P-1,Alex Rivera,10/13/2026,Approved,10/14/2026\n"
                  "P-2,Sam Patel,10/14/2026,Pending Utility,\n"
                  "P-3,Sam Patel,10/14/2026,Pending Utility,\n"}


class FakePortal(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html", headers=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        u = urlparse(self.path)
        authed = "sid=ok" in (self.headers.get("Cookie") or "")
        if u.path == "/out":                      # a redirect off the allowed hosts
            self.send_response(302)
            self.send_header("Location", "https://evil.example.net/login")
            self.end_headers()
            return
        if u.path == "/login":
            self._send('<form method="post" action="/login"><input id="email" name="email"><input id="pw" name="pw" type="password">'
                       '<button type="submit">Sign in</button></form>')
        elif u.path == "/enrollments" and authed:
            q = parse_qs(u.query)
            self._send(f'<h1>Enrollments</h1><p id="range">{q.get("from", [""])[0]}</p><a id="export" href="/export.csv" download="enrollments.csv">Export</a>')
        elif u.path == "/export.csv" and authed:
            self._send(EXPORT["rows"], "text/csv", [("Content-Disposition", 'attachment; filename="enrollments.csv"')])
        else:
            self._send("<h1>Please sign in</h1>")

    def do_POST(self):
        body = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if body.get("email") == ["bot@example.test"] and body.get("pw") == ["s3cret-pass"]:
            self.send_response(302)
            self.send_header("Set-Cookie", "sid=ok; Path=/")
            self.send_header("Location", "/enrollments?from=x")
            self.end_headers()
        else:
            self._send("<h1>Wrong password</h1>")


@pytest.fixture
def fake_portal(monkeypatch):
    pytest.importorskip("playwright")
    if os.path.exists(CHROME):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_PATH", CHROME)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePortal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def recipe(base, password_selector="#pw"):
    return {"allowed_hosts": ["127.0.0.1"], "lookback_days": 30, "steps": [
        {"goto": f"{base}/login"}, {"fill": "#email", "value": "{username}"}, {"fill": password_selector, "value": "{password}"},
        {"click": "button[type=submit]"}, {"wait_for": "h1:has-text('Enrollments')"},
        {"goto": f"{base}/enrollments?from={{date_from}}"}, {"download": "#export"}]}


MAPPING = {"enrollment_id": "Enrollment ID", "raw_rep": "Sales Rep", "sale_date": "Date Signed", "status": "Status", "approval_date": "Approved On"}


def test_recipe_validation():
    with pytest.raises(ValueError, match="allowed_hosts"):
        portal.validate({"steps": [{"download": "#x"}]})
    with pytest.raises(ValueError, match="download"):
        portal.validate({"allowed_hosts": ["portal.example.com"], "steps": [{"goto": "https://portal.example.com"}]})
    with pytest.raises(ValueError, match="not in allowed_hosts"):
        portal.validate({"allowed_hosts": ["portal.example.com"], "steps": [{"goto": "https://evil.example.net"}, {"download": "#x"}]})
    with pytest.raises(ValueError, match="https"):
        portal.validate({"allowed_hosts": ["portal.example.com"], "steps": [{"goto": "http://portal.example.com"}, {"download": "#x"}]})
    portal.validate({"allowed_hosts": ["example.com"], "steps": [{"goto": "https://app.example.com/login"}, {"download": "#x"}]})


def test_totp_matches_rfc6238():
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59, digits=8) == "94287082"


def _connector(c, base, monkeypatch, **kw):
    from emg.services import connectors

    monkeypatch.setenv("CONNECTOR_SECRET_KEY", new_key())
    conn = connectors.save(c, {"name": "Acme portal", "vendor": "Acme", "kind": "portal", "config": recipe(base, **kw), "mapping": MAPPING,
                               "run_hours": [6], "enabled": True}, "t")
    connectors.set_credentials(c, conn["id"], {"username": "bot@example.test", "password": "s3cret-pass"}, "t")
    return conn


def test_portal_run_syncs_statuses_for_leaderboards(env, fake_portal, monkeypatch):
    from emg.db.conn import connect
    from emg.services import connectors
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        conn = _connector(c, fake_portal, monkeypatch)
        blob = bytes(c.execute("select credentials from connectors").fetchone()["credentials"])
        assert b"s3cret-pass" not in blob and "credentials" not in connectors.list_(c)[0]
        # P-3 is already known from a payroll file with a different sale date: the payroll value wins.
        c.execute("insert into enrollments(vendor,enrollment_id,raw_rep,sale_date,status) values ('Acme','P-3','Sam Patel','2026-10-12','Pending')")
        r = connectors.run(c, env["storage"], conn["id"], "t", today=date(2026, 10, 20))
        assert r["status"] == "ok", r
        assert r["summary"]["created"] == 2 and r["summary"]["status_changed"] == 1
        p1, p3 = (c.execute("select * from enrollments where enrollment_id=%s", (x,)).fetchone() for x in ("P-1", "P-3"))
        assert (p1["origin"], str(p1["approval_date"]), p1["approval_date_source"]) == ("portal", "2026-10-14", "vendor")
        assert str(p3["sale_date"]) == "2026-10-12" and p3["status"] == "Pending Utility"
        board = lb.compute(c, None, "weekly", date(2026, 10, 14), "written", vendors=["Acme"])
        assert [(x["name"], x["count"]) for x in board["rows"]] == [("Sam Patel", 2), ("Alex Rivera", 1)]
        # Next night the vendor approves P-2: one status change, recorded in history.
        EXPORT["rows"] = EXPORT["rows"].replace("P-2,Sam Patel,10/14/2026,Pending Utility,", "P-2,Sam Patel,10/14/2026,Approved,10/21/2026")
        r2 = connectors.run(c, env["storage"], conn["id"], "t", today=date(2026, 10, 21))
        assert r2["summary"]["status_changed"] == 1 and r2["summary"]["unchanged"] == 2
        assert [x["status"] for x in c.execute("select h.status from enrollment_status_history h join enrollments e on e.id=h.enrollment_pk"
                                                 " where e.enrollment_id='P-2' order by h.id")] == ["Pending Utility", "Approved"]
        appr = lb.compute(c, None, "weekly", date(2026, 10, 21), "approved", vendors=["Acme"])
        assert [(x["name"], x["count"]) for x in appr["rows"]] == [("Sam Patel", 1)] and appr["proxies"] == []


def test_failed_run_keeps_screenshot_alerts_and_hides_password(env, fake_portal, monkeypatch):
    from emg.db.conn import connect
    from emg.services import connectors

    with connect(env["db"]) as c:
        conn = _connector(c, fake_portal, monkeypatch, password_selector="#missing")
        r = connectors.run(c, env["storage"], conn["id"], "t", fetcher=lambda cfg, creds, today=None: portal.run(cfg, creds, today, timeout_ms=1500))
        assert r["status"] == "failed" and "Step 3" in r["error"] and "s3cret-pass" not in r["error"]
        run = c.execute("select * from connector_runs order by started_at desc limit 1").fetchone()
        assert env["storage"].get_bytes(run["screenshot_key"]).startswith(b"\x89PNG")
        item = c.execute("select * from review_items where kind='connector_failed'").fetchone()
        assert item["status"] == "open" and item["blocking"] is False
        assert c.execute("select count(*) n from enrollments").fetchone()["n"] == 0
        # Fix the recipe: the next run succeeds and closes the alert.
        connectors.save(c, {"name": "Acme portal", "vendor": "Acme", "kind": "portal", "config": recipe(fake_portal), "mapping": MAPPING, "enabled": True}, "t", conn["id"])
        assert connectors.run(c, env["storage"], conn["id"], "t")["status"] == "ok"
        assert c.execute("select status from review_items where kind='connector_failed'").fetchone()["status"] == "resolved"


def test_portal_never_types_credentials_off_the_allowed_hosts(fake_portal):
    cfg = recipe(fake_portal)
    cfg["steps"][0] = {"goto": f"{fake_portal}/out"}                     # the portal redirects to another host
    with pytest.raises(portal.PortalError) as e:
        portal.run(cfg, {"username": "u", "password": "p"}, timeout_ms=1500)
    assert e.value.step == 1                                             # stopped before any credential was typed


def test_api_connector_uses_the_same_sync(env, monkeypatch):
    from emg.db.conn import connect
    from emg.services import connectors

    monkeypatch.setenv("CONNECTOR_SECRET_KEY", new_key())
    with connect(env["db"]) as c:
        conn = connectors.save(c, {"name": "Acme API", "vendor": "Acme", "kind": "api", "config": {"provider": "http_json", "url": "https://api.example.test/e"},
                                   "mapping": {"enrollment_id": "id", "raw_rep": "agent.name", "sale_date": "signed", "status": "state"}}, "t")
        connectors.set_credentials(c, conn["id"], {"token": "tok"}, "t")
        rows = [{"id": 101, "agent": {"name": "Alex Rivera"}, "signed": "2026-10-13", "state": "Approved"}]
        r = connectors.run(c, env["storage"], conn["id"], "t", fetcher=lambda cfg, creds, since: rows)
        assert r["summary"]["created"] == 1
        e = c.execute("select raw_rep, sale_date, origin from enrollments where enrollment_id='101'").fetchone()
        assert (e["raw_rep"], str(e["sale_date"]), e["origin"]) == ("Alex Rivera", "2026-10-13", "portal")


def test_connectors_are_due_at_their_local_hours(env, monkeypatch):
    from emg.db.conn import connect
    from emg.services import connectors

    monkeypatch.setenv("CONNECTOR_SECRET_KEY", new_key())
    with connect(env["db"]) as c:
        conn = connectors.save(c, {"name": "Acme API", "vendor": "Acme", "kind": "api", "config": {"provider": "http_json", "url": "https://x.test"},
                                   "mapping": {"enrollment_id": "id", "status": "s"}, "run_hours": [6, 18], "enabled": True}, "t")
        at6 = datetime(2026, 10, 20, 10, 5, tzinfo=timezone.utc)     # 06:05 New York
        assert [x["name"] for x in connectors.due(c, at6)] == ["Acme API"]
        assert connectors.due(c, datetime(2026, 10, 20, 11, 5, tzinfo=timezone.utc)) == []
        c.execute("insert into connector_runs(connector_id,trigger,created_by,status,started_at) values (%s,'schedule','scheduler','ok',%s)", (conn["id"], at6))
        assert connectors.due(c, at6.replace(minute=40)) == []           # already ran this hour

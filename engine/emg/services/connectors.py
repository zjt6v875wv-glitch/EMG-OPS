"""Vendor connectors: fetch the vendor's enrollment export (portal or API) and sync
enrollment statuses so leaderboards stay current without manual uploads.

A sync never touches payroll: it creates no import batch, no invoice lines and no
ledger entries. It fills missing facts on known enrollments (sale date, approval
date, rep, kWh) without overwriting values the payroll files set, updates the
status (with history), and adds enrollments the payroll files have not seen yet.
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..connectors import api as api_mod
from ..connectors import portal
from ..connectors.secrets import seal, unseal
from ..db.conn import J, audit
from ..importers.base import num, to_date
from ..names import clean
from . import imports
from .statuses import classifier

TARGETS = ("enrollment_id", "raw_rep", "sale_date", "approval_date", "status", "status_reason", "customer", "utility", "product", "kwh",
           "service_address", "city", "state", "zip")
FILL_ONCE = {"raw_rep": "raw_rep", "sale_date": "sale_date", "approval_date": "approval_date", "customer": "customer_name",
             "utility": "utility", "product": "product", "kwh": "kwh", "service_address": "service_address", "city": "city", "state": "state", "zip": "zip"}
PUBLIC = "id, name, vendor, kind, config, mapping, run_hours, enabled, credentials is not null as has_credentials, credentials_updated_at, alert_destination, created_at"


# -- configuration ---------------------------------------------------------------------

def _check(kind: str, config: dict, mapping: dict) -> None:
    if kind == "portal":
        portal.validate(config)
    elif kind == "api":
        if config.get("provider") not in api_mod.PROVIDERS:
            raise ValueError(f"API provider must be one of {sorted(api_mod.PROVIDERS)}")
    else:
        raise ValueError("kind must be portal or api")
    bad = [t for t in mapping if t not in TARGETS]
    if bad:
        raise ValueError(f"Unknown target fields {bad}; use {TARGETS}")
    for need in ("enrollment_id", "status"):
        if need not in mapping:
            raise ValueError(f"Map the {need} column")


def save(c, body: dict, actor: str, cid=None) -> dict:
    kind, config, mapping = body.get("kind", "portal"), body.get("config") or {}, body.get("mapping") or {}
    _check(kind, config, mapping)
    hours = sorted({int(h) for h in (body.get("run_hours") or [6])})
    if any(not 0 <= h <= 23 for h in hours):
        raise ValueError("run_hours are 0-23")
    vals = (body["name"], body["vendor"], kind, J(config), J(mapping), hours, bool(body.get("enabled")), body.get("alert_destination") or None)
    if cid:
        before = c.execute(f"select {PUBLIC} from connectors where id=%s", (cid,)).fetchone()
        if not before:
            raise LookupError("Unknown connector")
        row = c.execute(f"update connectors set name=%s, vendor=%s, kind=%s, config=%s, mapping=%s, run_hours=%s, enabled=%s, alert_destination=%s"
                        f" where id=%s returning {PUBLIC}", (*vals, cid)).fetchone()
        audit(c, actor, "connector.update", "connector", str(cid), before, row)
    else:
        row = c.execute(f"insert into connectors(name,vendor,kind,config,mapping,run_hours,enabled,alert_destination,created_by)"
                        f" values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning {PUBLIC}", (*vals, actor)).fetchone()
        audit(c, actor, "connector.create", "connector", str(row["id"]), None, row)
    return row


def set_credentials(c, cid, creds: dict, actor: str) -> dict:
    allowed = {k: str(v) for k, v in creds.items() if k in ("username", "password", "totp_secret", "token") and v not in (None, "")}
    if not allowed:
        raise ValueError("Enter a username and password (or an API token)")
    row = c.execute("update connectors set credentials=%s, credentials_updated_at=now() where id=%s returning id", (seal(allowed), cid)).fetchone()
    if not row:
        raise LookupError("Unknown connector")
    audit(c, actor, "connector.credentials", "connector", str(cid), None, {"fields": sorted(allowed)})  # never the values
    return {"ok": True, "fields": sorted(allowed)}


def list_(c) -> list[dict]:
    rows = c.execute(f"select {PUBLIC} from connectors order by name").fetchall()
    for r in rows:
        r["last_run"] = c.execute("select id, status, trigger, started_at, finished_at, summary, error, screenshot_key is not null as has_screenshot"
                                  " from connector_runs where connector_id=%s order by started_at desc limit 1", (r["id"],)).fetchone()
    return rows


def runs(c, cid, limit: int = 30) -> list[dict]:
    return c.execute("select id, status, trigger, started_at, finished_at, summary, error, source_file_id, screenshot_key is not null as has_screenshot"
                     " from connector_runs where connector_id=%s order by started_at desc limit %s", (cid, limit)).fetchall()


# -- parsing and sync --------------------------------------------------------------------

def _table(file_name: str, data: bytes) -> list[list]:
    from ..importers.generic import _table as generic_table

    work = Path("/tmp/emg-connectors")
    work.mkdir(parents=True, exist_ok=True)
    p = work / f"export{Path(file_name).suffix.lower() or '.csv'}"
    p.write_bytes(data)
    if p.suffix not in (".csv", ".xlsx", ".xlsm", ".pdf"):
        raise ValueError(f"Unsupported export type {p.suffix}")
    return generic_table(p, None)


def records_from_file(file_name: str, data: bytes, mapping: dict) -> list[dict]:
    """Rows of the export as {target: value}. The header row is the first row that
    contains every mapped column label."""
    rows = _table(file_name, data)
    want = {t: clean(lbl).lower() for t, lbl in mapping.items()}
    for hi, r in enumerate(rows):
        labels = [clean(x).lower() for x in r]
        if all(w in labels for w in want.values()):
            idx = {t: labels.index(w) for t, w in want.items()}
            break
    else:
        raise ValueError(f"The export has no header row with all mapped columns: {sorted(mapping.values())}")
    out = []
    for r in rows[hi + 1:]:
        if not any(x not in (None, "") for x in r):
            continue
        out.append({t: (r[i] if i < len(r) else None) for t, i in idx.items()})
    return out


def records_from_api(rows: list[dict], mapping: dict) -> list[dict]:
    def dig(d, path):
        for p in path.split("."):
            d = d.get(p) if isinstance(d, dict) else None
        return d
    return [{t: dig(r, k) for t, k in mapping.items()} for r in rows]


def sync(c, vendor: str, records: list[dict], source_label: str, actor: str) -> dict:
    cls = classifier(c)
    stats, unknown = Counter(), Counter()
    seen = set()
    for i, rec in enumerate(records, 2):
        eid = clean(rec.get("enrollment_id"))
        if isinstance(rec.get("enrollment_id"), float) and rec["enrollment_id"].is_integer():
            eid = str(int(rec["enrollment_id"]))
        if not eid:
            stats["skipped_no_id"] += 1
            continue
        if eid in seen:
            stats["duplicate_rows"] += 1
            continue
        seen.add(eid)
        status = clean(rec.get("status")) or None
        if status and cls(status) == "other":
            unknown[status] += 1
        vals = {
            "raw_rep": clean(rec.get("raw_rep")) or None, "sale_date": to_date(rec.get("sale_date")), "approval_date": to_date(rec.get("approval_date")),
            "customer": clean(rec.get("customer")) or None, "utility": clean(rec.get("utility")) or None, "product": clean(rec.get("product")) or None,
            "kwh": num(rec.get("kwh")) if rec.get("kwh") not in (None, "") else None, "service_address": clean(rec.get("service_address")) or None,
            "city": clean(rec.get("city")) or None, "state": clean(rec.get("state")).upper() or None, "zip": clean(rec.get("zip")) or None,
        }
        e = c.execute("select * from enrollments where vendor=%s and enrollment_id=%s for update", (vendor, eid)).fetchone()
        if not e:
            e = c.execute(
                "insert into enrollments(vendor,enrollment_id,raw_rep,sale_date,approval_date,approval_date_source,customer_name,utility,product,kwh,"
                "service_address,city,state,zip,status,status_reason,origin,address_source) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'portal',%s) returning *",
                (vendor, eid, vals["raw_rep"], vals["sale_date"], vals["approval_date"], "vendor" if vals["approval_date"] else None, vals["customer"],
                 vals["utility"], vals["product"], vals["kwh"], vals["service_address"], vals["city"], vals["state"], vals["zip"], status,
                 clean(rec.get("status_reason")) or None, source_label if vals["service_address"] else None)).fetchone()
            c.execute("insert into enrollment_status_history(enrollment_pk,status,reason,source_file,source_row) values (%s,%s,%s,%s,%s)",
                      (e["id"], status or "Unknown", "connector", source_label, i))
            stats["created"] += 1
            continue
        sets, args = [], []
        for k, col in FILL_ONCE.items():  # fill gaps only; payroll-file values win
            if vals[k] is not None and e[col] is None:
                sets.append(f"{col}=%s")
                args.append(vals[k])
                if k == "approval_date":
                    sets.append("approval_date_source='vendor'")
        if status and status != e["status"]:
            sets += ["status=%s", "status_reason=%s"]
            args += [status, clean(rec.get("status_reason")) or None]
            c.execute("insert into enrollment_status_history(enrollment_pk,status,reason,source_file,source_row) values (%s,%s,%s,%s,%s)",
                      (e["id"], status, "connector", source_label, i))
            stats["status_changed"] += 1
        if sets:
            c.execute(f"update enrollments set {', '.join(sets)}, updated_at=now() where id=%s", (*args, e["id"]))
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
    out = dict(stats) | {"rows": len(records)}
    if unknown:
        out["uncategorised_statuses"] = dict(unknown.most_common(20))
    return out


# -- running -------------------------------------------------------------------------------

def run(c, storage, cid, actor: str, trigger: str = "manual", fetcher=None, today: date | None = None) -> dict:
    """Fetch and sync one connector. The run row is committed first so a crash
    still leaves a record; ``fetcher`` replaces the network step in tests."""
    conn = c.execute("select * from connectors where id=%s", (cid,)).fetchone()
    if not conn:
        raise LookupError("Unknown connector")
    r = c.execute("insert into connector_runs(connector_id,trigger,created_by) values (%s,%s,%s) returning id", (cid, trigger, actor)).fetchone()
    c.commit()
    try:
        creds = unseal(conn["credentials"])
        if not creds:
            raise ValueError("No credentials saved for this connector")
        cfg = conn["config"]
        if conn["kind"] == "portal":
            got = (fetcher or portal.run)(cfg, creds, today=today)
            sf = imports.store_upload(c, storage, got.data, f"{conn['vendor']}_portal_{(today or date.today()).isoformat()}_{got.file_name}", actor)
            records = records_from_file(got.file_name, got.data, conn["mapping"])
            label = sf["file_name"]
        else:
            since = (today or date.today()) - timedelta(days=int(cfg.get("lookback_days", 45)))
            rows = (fetcher or api_mod.PROVIDERS[cfg["provider"]])(cfg, creds, since)
            data = json.dumps(rows, default=str).encode()
            sf = imports.store_upload(c, storage, data, f"{conn['vendor']}_api_{(today or date.today()).isoformat()}.json", actor)
            records = records_from_api(rows, conn["mapping"])
            label = sf["file_name"]
        summary = sync(c, conn["vendor"], records, label, actor)
        c.execute("update connector_runs set status='ok', finished_at=now(), source_file_id=%s, summary=%s where id=%s", (sf["id"], J(summary), r["id"]))
        c.execute("update review_items set status='resolved', resolution='next run succeeded', resolved_by='system', resolved_at=now()"
                  " where scope_key=%s and status='open'", (f"connector:{cid}",))
        audit(c, actor, "connector.run", "connector", str(cid), None, summary)
        return {"run_id": str(r["id"]), "status": "ok", "summary": summary}
    except Exception as e:
        c.rollback()
        shot = getattr(e, "screenshot", None)
        key = storage.put_bytes(shot, f"connectors/{cid}", "failure.png")[0] if shot else None
        msg = str(e)[:1000]
        c.execute("update connector_runs set status='failed', finished_at=now(), error=%s, screenshot_key=%s where id=%s", (msg, key, r["id"]))
        _alert(c, conn, msg)
        audit(c, actor, "connector.failed", "connector", str(cid), None, {"error": msg[:300]})
        return {"run_id": str(r["id"]), "status": "failed", "error": msg}


def _alert(c, conn: dict, msg: str) -> None:
    """A failed sync shows in the review queue (non-blocking) and, if set, posts to an enabled destination."""
    c.execute("insert into review_items(scope_key,kind,message,vendor,blocking,data) values (%s,'connector_failed',%s,%s,false,%s)"
              " on conflict (scope_key) do update set message=excluded.message, status='open', resolved_at=null, resolution=null",
              (f"connector:{conn['id']}", f"{conn['name']} could not sync: {msg[:300]}", conn["vendor"], J({"connector_id": str(conn["id"])})))
    if not conn["alert_destination"]:
        return
    from .leaderboards import destinations

    d = next((x for x in destinations(c) if x["name"] == conn["alert_destination"] and x["enabled"] and x["type"] == "webhook"), None)
    if d:
        try:
            req = urllib.request.Request(d["url"], data=json.dumps({"text": f"EMG: {conn['name']} sync failed: {msg[:300]}"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).close()
        except OSError:
            pass


def due(c, now: datetime | None = None) -> list[dict]:
    from .leaderboards import timezone

    tz = timezone(c)
    local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    hour_start = local.replace(minute=0, second=0, microsecond=0)
    out = []
    for conn in c.execute("select id, name, run_hours from connectors where enabled").fetchall():
        if local.hour not in conn["run_hours"]:
            continue
        ran = c.execute("select 1 from connector_runs where connector_id=%s and trigger='schedule' and started_at >= %s", (conn["id"], hour_start)).fetchone()
        if not ran:
            out.append(conn)
    return out


def test_mapping(file_name: str, data: bytes, mapping: dict, limit: int = 20) -> dict:
    """Preview how an export will be read, before saving a connector."""
    recs = records_from_file(file_name, data, mapping)
    return {"rows": len(recs), "sample": recs[:limit]}


def export_csv(records: list[dict]) -> bytes:  # pragma: no cover - debugging aid
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TARGETS)
    w.writeheader()
    for r in records:
        w.writerow({k: r.get(k) for k in TARGETS})
    return buf.getvalue().encode()


def chromium_ok() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True if not os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") else os.path.exists(os.environ["PLAYWRIGHT_CHROMIUM_PATH"])

"""Daily and weekly Top-N leaderboards.

Metrics (each counts unique enrollments, vendor + enrollment id):

* ``written``           sales by sale date (vendor-marked duplicates excluded).
* ``approved``          newly approved: enrollments whose approval date falls in
                        the period and that are approved now. When the vendor gives
                        no approval date the invoice date is used, then the first
                        time the app saw the enrollment approved; every proxy is
                        labelled in the result.
* ``written_approved``  written in the period and approved now.

Periods are calendar days, or Monday-Sunday weeks, in the configured timezone.
Ranks use competition ranking (1, 2, 2, 4); ties are listed alphabetically.
Exports never include customer, address or debt details.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..db.conn import J, audit
from ..weeks import DEFAULT_TZ, business_week_bounds, local_date
from . import auth
from .statuses import classifier, seller_resolver

METRICS = ("written", "approved", "written_approved")
METRIC_LABELS = {"written": "Written sales", "approved": "Newly approved", "written_approved": "Written & now approved"}
PROXY_LABELS = {"vendor": None, "invoice_proxy": "approval date estimated from the invoice date",
                "first_seen": "approval date estimated from when the app first saw it approved"}


def timezone(c) -> str:
    row = c.execute("select value from settings where key='timezone'").fetchone()
    tz = (row["value"] if row else None) or os.environ.get("EMG_TIMEZONE") or DEFAULT_TZ
    ZoneInfo(tz)  # validate
    return tz


def today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def period(cadence: str, anchor: date) -> tuple[date, date]:
    if cadence == "daily":
        return anchor, anchor
    if cadence == "weekly":
        return business_week_bounds(anchor)
    raise ValueError("cadence must be daily or weekly")


def previous(cadence: str, start: date) -> tuple[date, date]:
    return period(cadence, start - timedelta(days=1))


def _facts(c, tz: str, vendors: list[str] | None) -> list[dict]:
    """One row per enrollment with seller, category and effective approval date."""
    cls, seller_of = classifier(c), seller_resolver(c)
    q = ("select e.id, e.vendor, e.enrollment_id, e.raw_rep, e.sale_date, e.approval_date, e.invoice_date, e.status, e.origin from enrollments e")
    rows = c.execute(q + (" where e.vendor = any(%s)" if vendors else ""), (list(vendors),) if vendors else ()).fetchall()
    # Last resort for approvals with no vendor or invoice date: the first time an import
    # showed the enrollment approved, but only when an earlier import showed it not yet
    # approved (a first import of an already-approved enrollment says nothing about when).
    statuses = [r["status"] for r in c.execute("select distinct status from enrollment_status_history")]
    ok = [st for st in statuses if cls(st) == "approved"]
    first_ok = {r["enrollment_pk"]: r["at"] for r in c.execute(
        "select h.enrollment_pk, min(h.observed_at) at from enrollment_status_history h where h.status = any(%s)"
        " and exists (select 1 from enrollment_status_history p where p.enrollment_pk=h.enrollment_pk and p.observed_at < h.observed_at and not (p.status = any(%s)))"
        " group by 1", (ok, ok))} if ok else {}
    out = []
    for e in rows:
        cat = cls(e["status"])
        if cat == "duplicate":
            continue
        appr, src = None, None
        if cat == "approved":
            if e["approval_date"]:
                appr, src = e["approval_date"], "vendor"
            elif e["invoice_date"]:
                appr, src = e["invoice_date"], "invoice_proxy"
            elif e["id"] in first_ok:
                appr, src = local_date(first_ok[e["id"]], tz), "first_seen"
        out.append({"vendor": e["vendor"], "enrollment_id": e["enrollment_id"], "seller": seller_of(e["raw_rep"], e["vendor"]),
                    "sale_date": e["sale_date"] if e["origin"] != "status_report" else None, "category": cat, "status": e["status"],
                    "approved_on": appr, "approval_source": src})
    return out


def _in(d, a: date, b: date) -> bool:
    return d is not None and a <= d <= b


def _select(facts: list[dict], metric: str, a: date, b: date) -> list[dict]:
    if metric == "written":
        return [f for f in facts if _in(f["sale_date"], a, b)]
    if metric == "approved":
        return [f for f in facts if f["category"] == "approved" and _in(f["approved_on"], a, b)]
    if metric == "written_approved":
        return [f for f in facts if f["category"] == "approved" and _in(f["sale_date"], a, b)]
    raise ValueError(f"metric must be one of {METRICS}")


class Teams:
    """Live manager memberships for the period: who gets credit for a sale."""

    def __init__(self, rs, week_label: str):
        self.of: dict[str, list[tuple[str, list | None]]] = defaultdict(list)  # seller -> [(manager, vendors or None)]
        self.managers: set[str] = set()
        for m in (rs.memberships if rs else []):
            if m.live(week_label):
                self.managers.add(m.manager)
                if m.manager != m.agent:
                    self.of[m.agent].append((m.manager, m.vendors))

    def credit(self, f: dict, group: str) -> list[str]:
        if group == "agent":
            return [f["seller"]]
        own = [f["seller"]] if f["seller"] in self.managers else []  # managers rank on their own sales too
        return list(dict.fromkeys(own + [m for m, vs in self.of.get(f["seller"], []) if vs is None or f["vendor"] in vs]))

    def team(self, manager: str) -> set[str]:
        return {manager} | {s for s, ms in self.of.items() if any(m == manager for m, _ in ms)}


def rank(counts: dict[str, int]) -> list[dict]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].upper()))
    out, last, r = [], None, 0
    for i, (name, n) in enumerate(ordered, 1):
        if n != last:
            r, last = i, n
        out.append({"rank": r, "name": name, "count": n})
    return out


def compute(c, user: dict | None, cadence: str, anchor: date | None = None, metric: str = "written", group: str = "agent",
            vendors: list[str] | None = None, top: int = 10, manager: str | None = None, tz: str | None = None) -> dict:
    from ..weeks import week_of

    tz = tz or timezone(c)
    anchor = anchor or today(tz)
    a, b = period(cadence, anchor)
    pa, pb = previous(cadence, a)
    if group not in ("agent", "manager"):
        raise ValueError("group must be agent or manager")
    facts = _facts(c, tz, vendors)
    teams = Teams(seller_resolver(c).ruleset, week_of(b))
    team = teams.team(manager) if manager else None

    def credit(sel: list[dict]) -> tuple[dict[str, int], dict[str, list[dict]]]:
        counts: dict[str, set] = defaultdict(set)
        items: dict[str, list[dict]] = defaultdict(list)
        for f in sel:
            for n in teams.credit(f, group):
                if team is not None and (n not in team if group == "agent" else n != manager):
                    continue
                k = (f["vendor"], f["enrollment_id"])
                if k not in counts[n]:
                    counts[n].add(k)
                    items[n].append(f)
        return {n: len(v) for n, v in counts.items()}, items

    cur_sel, prev_sel = _select(facts, metric, a, b), _select(facts, metric, pa, pb)
    cur, items = credit(cur_sel)
    prev, _ = credit(prev_sel)
    prev_rank = {r["name"]: r["rank"] for r in rank(prev)}
    rows = []
    for r in rank(cur):
        if r["rank"] > top:
            break
        mine = items[r["name"]]
        r["previous_rank"] = prev_rank.get(r["name"])
        r["change"] = None if r["previous_rank"] is None else r["previous_rank"] - r["rank"]
        r["by_vendor"] = dict(sorted(_count(mine, "vendor").items()))
        if metric == "approved":
            r["also_written_in_period"] = sum(1 for f in mine if _in(f["sale_date"], a, b))
        if metric == "written":
            r["approved_now"] = sum(1 for f in mine if f["category"] == "approved")
        rows.append(r)
    proxies = _count([f for f in cur_sel if f["approval_source"] not in (None, "vendor")], "approval_source") if metric == "approved" else {}
    res = {"cadence": cadence, "metric": metric, "metric_label": METRIC_LABELS[metric], "group": group, "timezone": tz,
           "period_start": a, "period_end": b, "previous_start": pa, "previous_end": pb, "vendors": vendors or [], "manager": manager,
           "top": top, "rows": rows, "total_enrollments": len({(f["vendor"], f["enrollment_id"]) for f in cur_sel}),
           "people_ranked": len(cur), "proxies": [{"source": k, "count": n, "label": PROXY_LABELS[k]} for k, n in proxies.items()],
           "notes": _notes(metric, vendors, facts)}
    return res


def _count(fs: list[dict], key: str) -> dict:
    out: dict = defaultdict(int)
    for f in fs:
        out[f[key]] += 1
    return dict(out)


def _notes(metric: str, vendors, facts) -> list[str]:
    notes = ["Counts are unique enrollments; vendor-marked duplicates are excluded."]
    if metric == "approved":
        no_date = sum(1 for f in facts if f["category"] == "approved" and f["approved_on"] is None)
        if no_date:
            notes.append(f"{no_date} approved enrollments (any date) have no approval date or usable proxy, so they never appear on approved boards.")
    return notes


def drilldown(c, user: dict, name: str, cadence: str, anchor: date, metric: str, group: str = "agent", vendors=None, tz: str | None = None) -> list[dict]:
    """The enrollments behind one leaderboard row (no customer details)."""
    visible = auth.visible_people(c, user)
    if visible is not None and name not in visible:
        raise PermissionError("You can only open your own or your team's results")
    tz = tz or timezone(c)
    a, b = period(cadence, anchor)
    sel = _select(_facts(c, tz, vendors), metric, a, b)
    from ..weeks import week_of

    teams = Teams(seller_resolver(c).ruleset, week_of(b))
    sel = [f for f in sel if name in teams.credit(f, group)]
    seen, out = set(), []
    for f in sorted(sel, key=lambda f: (f["sale_date"] or date.min, f["vendor"], f["enrollment_id"])):
        k = (f["vendor"], f["enrollment_id"])
        if k not in seen:
            seen.add(k)
            out.append({k2: f[k2] for k2 in ("vendor", "enrollment_id", "seller", "sale_date", "status", "category", "approved_on", "approval_source")})
    return out


# -- exports ------------------------------------------------------------------------

NAVY, TEAL, SLATE, LIGHT, LINE = "#173247", "#087F8C", "#617482", "#EDF3F6", "#DCE5E9"


def _title(res: dict) -> str:
    a, b = res["period_start"], res["period_end"]
    when = a.strftime("%a %b %-d, %Y") if a == b else f"{a.strftime('%b %-d')} - {b.strftime('%b %-d, %Y')}"
    who = "Managers" if res["group"] == "manager" else "Agents"
    return f"{res['metric_label']} - Top {res['top']} {who}", f"{'Daily' if res['cadence'] == 'daily' else 'Weekly'} | {when} | {res['timezone']}"


def _change(r) -> str:
    if r["change"] is None:
        return "new"
    return "-" if r["change"] == 0 else (f"+{r['change']}" if r["change"] > 0 else str(r["change"]))


def export_csv(res: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rank", "Change", "Name", res["metric_label"], "Previous rank"])
    for r in res["rows"]:
        w.writerow([r["rank"], _change(r), r["name"], r["count"], r["previous_rank"] or ""])
    return buf.getvalue().encode("utf-8-sig")


def export_pdf(res: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from ..reports.pdf import _fonts

    font, bold = _fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=48, rightMargin=48, topMargin=44, bottomMargin=44,
                            title="EMG Leaderboard", author="Energy Marketing Group")
    t, sub = _title(res)
    story = [Paragraph("ENERGY MARKETING GROUP", ParagraphStyle("b", fontName=bold, fontSize=9, textColor=colors.HexColor(TEAL))),
             Paragraph(t, ParagraphStyle("t", fontName=bold, fontSize=20, leading=26, textColor=colors.HexColor(NAVY))),
             Paragraph(sub, ParagraphStyle("s", fontName=font, fontSize=10, textColor=colors.HexColor(SLATE))), Spacer(1, 14)]
    data = [["Rank", "", "Name", "Count"]] + [[str(r["rank"]), _change(r), r["name"], str(r["count"])] for r in res["rows"]]
    if len(data) == 1:
        data.append(["", "", "No sales in this period", ""])
    tbl = Table(data, colWidths=[44, 44, 300, 80])
    tbl.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), font, 11), ("FONT", (0, 0), (-1, 0), bold, 10),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(LIGHT)]),
                             ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(LINE)), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                             ("TEXTCOLOR", (1, 1), (1, -1), colors.HexColor(SLATE)), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(tbl)
    foot = [f"{res['total_enrollments']} unique enrollments. Change compares with the previous {'day' if res['cadence'] == 'daily' else 'week'}."]
    foot += [f"{p['count']} counted with {p['label']}." for p in res["proxies"]]
    story += [Spacer(1, 12)] + [Paragraph(x, ParagraphStyle("f", fontName=font, fontSize=8, textColor=colors.HexColor(SLATE))) for x in foot]
    doc.build(story)
    return buf.getvalue()


def export_png(res: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    def f(size, b=False):
        for p in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if b else ''}.ttf",):
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()

    rows = res["rows"] or [{"rank": "", "name": "No sales in this period", "count": "", "change": 0, "previous_rank": None}]
    W, top, rh = 1080, 190, 64
    H = top + rh * len(rows) + 90
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 150], fill=NAVY)
    t, sub = _title(res)
    d.text((48, 30), "ENERGY MARKETING GROUP", fill="#9FD3D9", font=f(20, True))
    d.text((48, 62), t, fill="white", font=f(38, True))
    d.text((48, 112), sub, fill="#C9D6DD", font=f(20))
    for i, r in enumerate(rows):
        y = top + i * rh
        if i % 2:
            d.rectangle([0, y - 10, W, y + rh - 10], fill=LIGHT)
        d.text((56, y + 4), str(r["rank"]), fill=TEAL if r["rank"] in (1, 2, 3) else NAVY, font=f(30, True))
        d.text((130, y + 12), _change(r) if r["name"] != "No sales in this period" else "", fill=SLATE, font=f(18))
        d.text((210, y + 6), str(r["name"]), fill=NAVY, font=f(28))
        cnt = str(r["count"])
        d.text((W - 56 - d.textlength(cnt, font=f(30, True)), y + 4), cnt, fill=NAVY, font=f(30, True))
    note = f"{res['total_enrollments']} unique enrollments" + "".join(f"  |  {p['count']} with {p['label']}" for p in res["proxies"])
    d.text((48, H - 56), note, fill=SLATE, font=f(16))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


EXPORTS = {"csv": (export_csv, "text/csv"), "pdf": (export_pdf, "application/pdf"), "png": (export_png, "image/png")}


def file_name(res: dict, ext: str) -> str:
    who = "Managers" if res["group"] == "manager" else "Agents"
    return f"EMG_{res['cadence'].title()}_{res['metric'].title().replace('_', '')}_{who}_{res['period_start'].isoformat()}.{ext}"


# -- schedules and publishing ---------------------------------------------------------

def destinations(c) -> list[dict]:
    row = c.execute("select value from settings where key='leaderboard_destinations'").fetchone()
    return row["value"] if row else []


def save_destinations(c, items: list[dict], actor: str) -> list[dict]:
    clean = []
    for d in items:
        if d.get("type") not in ("webhook", "outbox"):
            raise ValueError("Destinations are webhook (Slack/Teams-compatible) or outbox (stored only)")
        if d["type"] == "webhook" and not str(d.get("url", "")).startswith("https://"):
            raise ValueError("Webhook destinations need an https URL")
        clean.append({"name": str(d["name"]), "type": d["type"], "url": d.get("url"), "enabled": bool(d.get("enabled"))})
    before = destinations(c)
    c.execute("insert into settings(key,value,updated_by) values ('leaderboard_destinations',%s,%s) on conflict (key) do update set value=excluded.value,"
              " updated_by=excluded.updated_by, updated_at=now()", (J(clean), actor))
    audit(c, actor, "leaderboards.destinations", "settings", "leaderboard_destinations", [{**d, "url": bool(d.get("url"))} for d in before],
          [{**d, "url": bool(d.get("url"))} for d in clean])
    return clean


def upsert_destination(c, d: dict, actor: str) -> list[dict]:
    """Add or replace one destination by name; an empty URL keeps the stored one."""
    items = destinations(c)
    old = next((x for x in items if x["name"] == d.get("name")), None)
    if old and not d.get("url"):
        d = {**d, "url": old.get("url")}
    return save_destinations(c, [x for x in items if x["name"] != d.get("name")] + [d], actor)


def due_period(s: dict, now: datetime) -> tuple[date, date] | None:
    """The last complete period for a schedule, if its run time has passed."""
    local = now.astimezone(ZoneInfo(s["timezone"]))
    if local.hour < s["run_hour"]:
        return None
    if s["cadence"] == "daily":
        d = local.date() - timedelta(days=1)
        return d, d
    if local.weekday() != s["run_weekday"]:
        return None
    return previous("weekly", business_week_bounds(local.date())[0])


def run_schedule(c, storage, s: dict, start: date, actor: str = "scheduler") -> dict | None:
    exists = c.execute("select id from leaderboard_runs where schedule_id=%s and period_start=%s", (s["id"], start)).fetchone()
    if exists:
        return None
    f = s["filters"] or {}
    boards, files = [], []
    for metric in s["metrics"]:
        for group in s["groups"]:
            res = compute(c, None, s["cadence"], start, metric, group, f.get("vendors"), s["top_n"], tz=s["timezone"])
            boards.append(res)
            for ext, (fn, mime) in EXPORTS.items():
                data = fn(res)
                name = file_name(res, ext)
                key, _ = storage.put_bytes(data, f"leaderboards/{start.isoformat()}", name)
                files.append({"kind": ext, "metric": metric, "group": group, "file_name": name, "storage_key": key,
                              "sha256": hashlib.sha256(data).hexdigest(), "mime": mime})
    run = c.execute("insert into leaderboard_runs(schedule_id,cadence,period_start,period_end,timezone,filters,result,files,created_by)"
                    " values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
                    (s["id"], s["cadence"], start, boards[0]["period_end"] if boards else start, s["timezone"], J(f), J(boards), J(files), actor)).fetchone()
    c.execute("update leaderboard_schedules set last_run_at=now() where id=%s", (s["id"],))
    pub = publish(c, s, run, boards, files)
    audit(c, actor, "leaderboards.run", "leaderboard_schedule", str(s["id"]), None, {"period_start": start, "files": len(files), "published": pub["published"]})
    return run | {"publish_result": pub}


def publish(c, s: dict, run: dict, boards: list[dict], files: list[dict]) -> dict:
    """Publish only when the schedule allows it AND its destination is enabled."""
    dest_name = (s.get("destination") or {}).get("name")
    dest = next((d for d in destinations(c) if d["name"] == dest_name), None)
    if not s["publish_enabled"] or not dest or not dest["enabled"]:
        result = {"published": False, "reason": "Publishing is off for this schedule" if not s["publish_enabled"] else
                  "No destination selected" if not dest_name else f"Destination {dest_name} is missing or disabled"}
    elif dest["type"] == "outbox":
        result = {"published": True, "destination": dest["name"], "type": "outbox"}
    else:
        text = "\n\n".join(_text(b) for b in boards)
        req = urllib.request.Request(dest["url"], data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                result = {"published": 200 <= r.status < 300, "destination": dest["name"], "status": r.status}
        except OSError as e:
            result = {"published": False, "destination": dest["name"], "error": str(e)[:300]}
    c.execute("update leaderboard_runs set published=%s, publish_result=%s where id=%s", (result["published"], J(result), run["id"]))
    return result


def _text(res: dict) -> str:
    t, sub = _title(res)
    lines = [f"*{t}*", sub] + [f"{r['rank']}. {r['name']} - {r['count']} ({_change(r)})" for r in res["rows"]]
    return "\n".join(lines if res["rows"] else lines + ["No sales in this period"])


def run_due(c, storage, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(ZoneInfo("UTC"))
    out = []
    for s in c.execute("select * from leaderboard_schedules where enabled order by created_at").fetchall():
        p = due_period(s, now)
        if p:
            r = run_schedule(c, storage, s, p[0])
            if r:
                out.append({"schedule": s["name"], "period_start": p[0], "published": r["publish_result"]["published"]})
    return out


def save_schedule(c, body: dict, actor: str, sid=None) -> dict:
    cadence = body.get("cadence")
    if cadence not in ("daily", "weekly"):
        raise ValueError("cadence must be daily or weekly")
    metrics = body.get("metrics") or ["written"]
    if any(m not in METRICS for m in metrics):
        raise ValueError(f"metrics must be from {METRICS}")
    groups = body.get("groups") or ["agent"]
    if any(g not in ("agent", "manager") for g in groups):
        raise ValueError("groups must be agent and/or manager")
    tz = body.get("timezone") or timezone(c)
    ZoneInfo(tz)
    vals = (body.get("name") or f"{cadence.title()} leaderboard", cadence, metrics, J(body.get("filters") or {}), tz,
            J({"name": body["destination"]}) if body.get("destination") else None, bool(body.get("enabled")), bool(body.get("publish_enabled")),
            int(body.get("run_hour", 8)), int(body.get("run_weekday", 0)), int(body.get("top_n", 10)), groups)
    cols = "name,cadence,metrics,filters,timezone,destination,enabled,publish_enabled,run_hour,run_weekday,top_n,groups"
    if sid:
        before = c.execute("select * from leaderboard_schedules where id=%s", (sid,)).fetchone()
        if not before:
            raise LookupError("Unknown schedule")
        row = c.execute(f"update leaderboard_schedules set ({cols}) = ({', '.join(['%s'] * 12)}) where id=%s returning *", (*vals, sid)).fetchone()
        audit(c, actor, "leaderboards.schedule_update", "leaderboard_schedule", str(sid), before, row)
    else:
        row = c.execute(f"insert into leaderboard_schedules({cols},created_by) values ({', '.join(['%s'] * 13)}) returning *", (*vals, actor)).fetchone()
        audit(c, actor, "leaderboards.schedule_create", "leaderboard_schedule", str(row["id"]), None, row)
    return row

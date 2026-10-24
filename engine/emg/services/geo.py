"""Sales map: address enrichment, geocoding with a cache, territories, capacity and map queries.

Locations are never invented. A point appears on the map only when it came
from a geocoder match, an imported coordinate or a manual placement; anything
else is counted as unmapped.
"""
from __future__ import annotations

import csv
import io
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

from ..db.conn import J, audit
from ..geo import enrich, geometry
from ..geo.geocode import GeoResult, address_key, get_provider
from . import auth
from .statuses import COLORS, classifier, seller_resolver

MAP_MIN_CONFIDENCE = float(os.environ.get("MAP_MIN_CONFIDENCE", "0.8"))


# -- enrichment ---------------------------------------------------------------------

def enrich_preview(c, storage, source_file_id, actor: str, vendor: str | None = None, kind: str | None = None) -> dict:
    sf = c.execute("select * from source_files where id=%s", (source_file_id,)).fetchone()
    if not sf:
        raise LookupError("Unknown file")
    work = Path("/tmp/emg-enrich")
    work.mkdir(parents=True, exist_ok=True)
    local = storage.copy_to(sf["storage_key"], work / f"{sf['sha256']}{Path(sf['file_name']).suffix}")
    rows = enrich._rows(local)
    if not rows:
        raise ValueError("The file has no rows")
    kind = kind or enrich.detect(list(rows[0].keys()))
    if kind == "status_report":
        if not vendor:
            raise ValueError("Choose the vendor this status report belongs to")
        known = {e["enrollment_id"]: e for e in c.execute("select * from enrollments where vendor=%s", (vendor,))}
        plan = enrich.plan_status_report(rows, vendor, known)
    elif kind == "address_mapping":
        q = "select vendor, enrollment_id, customer_name, sale_date, raw_rep, service_address, city, state, zip from enrollments where customer_name is not null"
        cands = c.execute(q + (" and vendor=%s" if vendor else ""), (vendor,) if vendor else ()).fetchall()
        plan = enrich.plan_address_mapping(rows, cands, seller_resolver(c))
    elif kind == "coordinates":
        known = {(e["vendor"], e["enrollment_id"]) for e in c.execute("select vendor, enrollment_id from enrollments")}
        plan = enrich.plan_coordinates(rows, known)
    else:
        raise ValueError(f"Unknown enrichment kind {kind}")
    summary = dict(Counter(p["action"] for p in plan)) | {"rows": len(plan)}
    b = c.execute("insert into enrichment_batches(source_file_id,kind,vendor,summary,plan,created_by) values (%s,%s,%s,%s,%s,%s) returning *",
                  (source_file_id, kind, vendor, J(summary), J(plan), actor)).fetchone()
    audit(c, actor, "enrich.preview", "enrichment_batch", str(b["id"]), None, summary)
    return b


def enrich_apply(c, batch_id, actor: str) -> dict:
    b = c.execute("select * from enrichment_batches where id=%s for update", (batch_id,)).fetchone()
    if not b:
        raise LookupError("Unknown enrichment batch")
    if b["status"] != "previewed":
        raise ValueError(f"Batch is already {b['status']}")
    sf = c.execute("select file_name from source_files where id=%s", (b["source_file_id"],)).fetchone()
    done = Counter()
    for p in b["plan"]:
        act = p["action"]
        if act == "create":
            f = p["fields"]
            e = c.execute(
                "insert into enrollments(vendor,enrollment_id,customer_name,raw_rep,service_address,city,state,zip,utility,status,kwh,project,origin,address_source)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'status_report',%s) on conflict (vendor,enrollment_id) do nothing returning id",
                (p["vendor"], p["enrollment_id"], f["customer_name"], f["raw_rep"], f["service_address"], f["city"], f["state"], f["zip"],
                 f["utility"], f["status"], f["kwh"], f["project"], sf["file_name"])).fetchone()
            if e:
                c.execute("insert into enrollment_status_history(enrollment_pk,status,reason,source_file,source_row) values (%s,%s,%s,%s,%s)",
                          (e["id"], f["status"] or "Unknown", "status report", sf["file_name"], p["row"]))
                done["created"] += 1
        elif act == "enrich":
            f = dict(p["fields"])
            sets = [f"{k}=%s" for k in f] + ["address_source=%s", "updated_at=now()"]
            if any(k in f for k in ("service_address", "city", "state", "zip")):
                # A new address invalidates any earlier location unless it was placed by hand.
                sets.append("lat=case when geocode_status='manual' then lat end, lng=case when geocode_status='manual' then lng end,"
                            " geocode_status=case when geocode_status='manual' then 'manual' end,"
                            " geocode_confidence=case when geocode_status='manual' then geocode_confidence end")
            c.execute(f"update enrollments set {', '.join(sets)} where vendor=%s and enrollment_id=%s",
                      (*f.values(), sf["file_name"], p["vendor"], p["enrollment_id"]))
            done["enriched"] += 1
        elif act == "locate":
            set_location(c, p["vendor"], p["enrollment_id"], p["lat"], p["lng"], actor, source=sf["file_name"], audit_it=False)
            done["located"] += 1
    c.execute("update enrichment_batches set status='applied', applied_by=%s, applied_at=now() where id=%s", (actor, batch_id))
    audit(c, actor, "enrich.apply", "enrichment_batch", str(batch_id), None, dict(done))
    return dict(done)


# -- geocoding ----------------------------------------------------------------------

def _enrollment_key(e: dict) -> str:
    return address_key(e["service_address"] or "", e["city"] or "", e["state"] or "", e["zip"] or "")


def _apply_cached(c, e: dict, g: dict) -> None:
    ok = g["status"] in ("matched", "ambiguous", "manual")
    c.execute("update enrollments set lat=%s, lng=%s, geocode_status=%s, geocode_confidence=%s, updated_at=now() where id=%s",
              (g["lat"] if ok else None, g["lng"] if ok else None, g["status"], g["confidence"], e["id"]))


def geocode_pending(c, actor: str, limit: int = 5000, provider=None) -> dict:
    """Locate enrollments that have an address but no location. Cached results
    are reused; new addresses go to the configured provider in one batch."""
    todo = c.execute("select id, service_address, city, state, zip from enrollments where service_address is not null and lat is null"
                     " and coalesce(geocode_status,'') not in ('unmatched','manual') limit %s", (limit,)).fetchall()
    stats = Counter()
    misses: dict[str, list[dict]] = {}
    for e in todo:
        k = _enrollment_key(e)
        g = c.execute("select * from geocode_cache where address_key=%s", (k,)).fetchone()
        if g:
            _apply_cached(c, e, g)
            stats["from_cache"] += 1
        else:
            misses.setdefault(k, []).append(e)
    if misses:
        provider = provider or get_provider()
        items = [(k, es[0]["service_address"], es[0]["city"] or "", es[0]["state"] or "", es[0]["zip"] or "") for k, es in misses.items()]
        try:
            results: list[GeoResult] = provider.geocode(items)
        except OSError as exc:  # network refused or timed out: leave rows pending, never guess
            stats["provider_error"] = len(items)
            audit(c, actor, "geocode.error", "geocode", None, None, {"provider": getattr(provider, "name", "?"), "error": str(exc)[:300]})
            return dict(stats) | {"pending": len(items), "error": str(exc)[:300]}
        for r in results:
            row = c.execute(
                "insert into geocode_cache(address_key,query,lat,lng,confidence,match_type,provider,status,candidates) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (address_key) do update set query=excluded.query, lat=excluded.lat, lng=excluded.lng, confidence=excluded.confidence,"
                " match_type=excluded.match_type, provider=excluded.provider, status=excluded.status, candidates=excluded.candidates, updated_at=now()"
                " where geocode_cache.status <> 'manual' returning *",
                (r.key, r.query, r.lat, r.lng, r.confidence, r.match_type, r.provider, r.status, J(r.candidates))).fetchone()
            row = row or c.execute("select * from geocode_cache where address_key=%s", (r.key,)).fetchone()
            for e in misses.get(r.key, []):
                _apply_cached(c, e, row)
            stats[r.status] += 1
    audit(c, actor, "geocode.run", "geocode", None, None, dict(stats))
    return dict(stats)


def set_location(c, vendor: str, enrollment_id: str, lat: float, lng: float, actor: str, source: str = "manual", audit_it: bool = True) -> dict:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Coordinates out of range")
    e = c.execute("select * from enrollments where vendor=%s and enrollment_id=%s", (vendor, enrollment_id)).fetchone()
    if not e:
        raise LookupError("Unknown enrollment")
    c.execute("update enrollments set lat=%s, lng=%s, geocode_status='manual', geocode_confidence=1.0, updated_at=now() where id=%s", (lat, lng, e["id"]))
    if e["service_address"]:
        k = _enrollment_key(e)
        c.execute("insert into geocode_cache(address_key,query,lat,lng,confidence,match_type,provider,status,reviewed_by) values (%s,%s,%s,%s,1.0,'rooftop',%s,'manual',%s)"
                  " on conflict (address_key) do update set lat=excluded.lat, lng=excluded.lng, confidence=1.0, match_type='rooftop',"
                  " provider=excluded.provider, status='manual', reviewed_by=excluded.reviewed_by, updated_at=now()",
                  (k, k, lat, lng, source, actor))
    if audit_it:
        audit(c, actor, "geocode.manual", "enrollment", f"{vendor}:{enrollment_id}", {"lat": e["lat"], "lng": e["lng"]}, {"lat": lat, "lng": lng})
    return {"vendor": vendor, "enrollment_id": enrollment_id, "lat": lat, "lng": lng, "status": "manual"}


def geocode_queue(c, limit: int = 200) -> list[dict]:
    """Addresses that need a person: unmatched or low-confidence."""
    return c.execute(
        "select vendor, enrollment_id, service_address, city, state, zip, geocode_status, geocode_confidence, lat, lng from enrollments"
        " where service_address is not null and (geocode_status in ('unmatched','ambiguous') or (geocode_confidence is not null and geocode_confidence < %s))"
        " order by geocode_status, vendor, enrollment_id limit %s", (MAP_MIN_CONFIDENCE, limit)).fetchall()


# -- territories --------------------------------------------------------------------

def territory_preview(file_name: str, data: bytes) -> list[dict]:
    feats = geometry.parse_territories(file_name, data)
    return [{"name": f["name"], "bbox": geometry.bbox(f["geometry"]), "type": f["geometry"]["type"],
             "capacity_kwh": f["properties"].get("capacity_kwh")} for f in feats]


def territory_import(c, storage, file_name: str, data: bytes, kind: str, vendor: str | None, actor: str) -> list[dict]:
    from . import imports

    if kind not in ("territory", "project"):
        raise ValueError("kind must be territory or project")
    feats = geometry.parse_territories(file_name, data)
    sf = imports.store_upload(c, storage, data, file_name, actor)
    out = []
    for f in feats:
        cap = f["properties"].get("capacity_kwh")
        t = c.execute("insert into territories(name,kind,vendor,geojson,capacity_kwh,capacity_enabled,source_file_id,created_by) values (%s,%s,%s,%s,%s,%s,%s,%s)"
                      " returning id, name, kind, vendor, capacity_kwh, capacity_enabled, active",
                      (f["name"], kind, vendor or None, J(f["geometry"]), cap, cap is not None, sf["id"], actor)).fetchone()
        out.append(t)
    audit(c, actor, "territory.import", "source_file", str(sf["id"]), None, {"count": len(out), "kind": kind, "vendor": vendor})
    return out


def territories(c, with_geometry: bool = True) -> list[dict]:
    cols = "id, name, kind, vendor, capacity_kwh, capacity_enabled, active, created_at" + (", geojson" if with_geometry else "")
    return c.execute(f"select {cols} from territories order by kind, name").fetchall()


def territory_update(c, tid, actor: str, **changes) -> dict:
    allowed = {k: v for k, v in changes.items() if k in ("name", "active", "capacity_kwh", "capacity_enabled", "vendor") and v is not None}
    if not allowed:
        raise ValueError("Nothing to change")
    before = c.execute("select name, active, capacity_kwh, capacity_enabled, vendor from territories where id=%s", (tid,)).fetchone()
    if not before:
        raise LookupError("Unknown territory")
    row = c.execute(f"update territories set {', '.join(f'{k}=%s' for k in allowed)} where id=%s returning id, name, kind, vendor, capacity_kwh, capacity_enabled, active",
                    (*allowed.values(), tid)).fetchone()
    audit(c, actor, "territory.update", "territory", str(tid), before, allowed)
    return row


def _territory_index(c):
    ts = [t for t in c.execute("select id, name, kind, vendor, geojson from territories where active")]
    for t in ts:
        t["bbox"] = geometry.bbox(t["geojson"])
    return ts


def _inside(t, lng, lat) -> bool:
    x0, y0, x1, y1 = t["bbox"]
    return x0 <= lng <= x1 and y0 <= lat <= y1 and geometry.contains(t["geojson"], lng, lat)


def place(ts: list[dict], vendor: str, lng: float, lat: float) -> tuple[list[str], bool | None]:
    """(names of areas containing the point, out_of_territory). out_of_territory is
    None when no territory applies to this vendor (nothing to compare against)."""
    names = [t["name"] for t in ts if (t["vendor"] in (None, vendor)) and _inside(t, lng, lat)]
    applicable = [t for t in ts if t["kind"] == "territory" and t["vendor"] in (None, vendor)]
    if not applicable:
        return names, None
    return names, not any(_inside(t, lng, lat) for t in applicable)


# -- capacity -----------------------------------------------------------------------

def allocate(c, territory_id, vendor: str, enrollment_id: str, actor: str, kwh=None, note: str = "") -> dict:
    t = c.execute("select * from territories where id=%s", (territory_id,)).fetchone()
    e = c.execute("select kwh from enrollments where vendor=%s and enrollment_id=%s", (vendor, enrollment_id)).fetchone()
    if not t or not e:
        raise LookupError("Unknown territory or enrollment")
    if not t["capacity_enabled"]:
        raise ValueError("Capacity tracking is off for this area")
    basis = "estimate" if kwh is not None else "actual"
    kwh = Decimal(str(kwh)) if kwh is not None else e["kwh"]
    if kwh is None:
        raise ValueError("This enrollment has no kWh; enter an estimate")
    row = c.execute("insert into capacity_allocations(territory_id,vendor,enrollment_id,kwh,basis,allocated_by,note) values (%s,%s,%s,%s,%s,%s,%s) returning *",
                    (territory_id, vendor, enrollment_id, kwh, basis, actor, note or None)).fetchone()
    audit(c, actor, "capacity.allocate", "territory", str(territory_id), None, {"enrollment": f"{vendor}:{enrollment_id}", "kwh": kwh, "basis": basis})
    return row


def release(c, allocation_id, actor: str, note: str = "") -> dict:
    row = c.execute("update capacity_allocations set released_by=%s, released_at=now(), note=coalesce(%s,note) where id=%s and released_at is null returning *",
                    (actor, note or None, allocation_id)).fetchone()
    if not row:
        raise LookupError("No active allocation")
    audit(c, actor, "capacity.release", "territory", str(row["territory_id"]), None, {"enrollment": f"{row['vendor']}:{row['enrollment_id']}"})
    return row


def capacity(c) -> list[dict]:
    rows = c.execute(
        "select t.id, t.name, t.kind, t.vendor, t.capacity_kwh,"
        " coalesce(sum(a.kwh) filter (where a.released_at is null),0) allocated_kwh,"
        " coalesce(sum(a.kwh) filter (where a.released_at is null and a.basis='estimate'),0) estimated_kwh,"
        " count(a.id) filter (where a.released_at is null) enrollments"
        " from territories t left join capacity_allocations a on a.territory_id=t.id"
        " where t.capacity_enabled and t.active group by t.id order by t.name").fetchall()
    for r in rows:
        r["remaining_kwh"] = (r["capacity_kwh"] - r["allocated_kwh"]) if r["capacity_kwh"] is not None else None
        r["over_capacity"] = r["remaining_kwh"] is not None and r["remaining_kwh"] < 0
    return rows


# -- map ----------------------------------------------------------------------------

def points(c, user: dict, f: dict | None = None) -> dict:
    """Map points visible to ``user`` under filters ``f``.

    Filters: vendors, categories, sellers, date_from/date_to (sale date), project,
    territory (name), out_of_territory (bool), q (search), min_confidence, bbox
    ([west, south, east, north]).
    """
    if not auth.can(user, "map.view"):
        raise PermissionError("Map access is limited to managers and office staff")
    f = f or {}
    visible = auth.visible_people(c, user) if not auth.can(user, "map.view_all") else None
    show_customer = auth.can(user, "map.view_all")
    cls, seller_of = classifier(c), seller_resolver(c)
    where, args = ["true"], []
    if f.get("vendors"):
        where.append("vendor = any(%s)")
        args.append(list(f["vendors"]))
    if f.get("date_from"):
        where.append("sale_date >= %s")
        args.append(f["date_from"])
    if f.get("date_to"):
        where.append("sale_date <= %s")
        args.append(f["date_to"])
    if f.get("project"):
        where.append("project = %s")
        args.append(f["project"])
    rows = c.execute("select id, vendor, enrollment_id, customer_name, service_address, city, state, zip, raw_rep, sale_date, approval_date,"
                     " status, kwh, project, lat, lng, geocode_status, geocode_confidence from enrollments where " + " and ".join(where), args).fetchall()
    ts = _territory_index(c)
    min_conf = float(f.get("min_confidence", MAP_MIN_CONFIDENCE))
    q = (f.get("q") or "").strip().lower()
    cats, sellers = set(f.get("categories") or []), set(f.get("sellers") or [])
    bbox = f.get("bbox")
    out, unmapped = [], Counter()
    for e in rows:
        seller = seller_of(e["raw_rep"], e["vendor"])
        if visible is not None and seller not in visible:
            continue
        cat = cls(e["status"])
        if (cats and cat not in cats) or (sellers and seller not in sellers):
            continue
        if q and q not in " ".join(str(x or "") for x in (e["enrollment_id"], seller, e["city"], e["zip"], e["project"],
                                                           *((e["customer_name"], e["service_address"]) if show_customer else ()))).lower():
            continue
        if e["lat"] is None:
            unmapped["no_address" if not e["service_address"] else e["geocode_status"] or "not_geocoded"] += 1
            continue
        conf = e["geocode_confidence"] or 0
        if e["geocode_status"] != "manual" and conf < min_conf:
            unmapped["low_confidence"] += 1
            continue
        if bbox and not (bbox[0] <= e["lng"] <= bbox[2] and bbox[1] <= e["lat"] <= bbox[3]):
            continue
        areas, oot = place(ts, e["vendor"], e["lng"], e["lat"])
        if f.get("territory") and f["territory"] not in areas:
            continue
        if f.get("out_of_territory") is not None and oot is not bool(f["out_of_territory"]):
            continue
        p = {"id": str(e["id"]), "vendor": e["vendor"], "enrollment_id": e["enrollment_id"], "lat": e["lat"], "lng": e["lng"],
             "status": e["status"], "category": cat, "seller": seller, "sale_date": e["sale_date"], "approval_date": e["approval_date"],
             "kwh": e["kwh"], "project": e["project"], "city": e["city"], "state": e["state"], "zip": e["zip"],
             "confidence": conf, "geocode": e["geocode_status"], "areas": areas, "out_of_territory": oot}
        if show_customer:
            p |= {"customer": e["customer_name"], "address": e["service_address"]}
        out.append(p)
    return {"points": out, "unmapped": dict(unmapped), "total": len(out), "colors": COLORS, "show_customer": show_customer,
            "min_confidence": min_conf, "tile_url": os.environ.get("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
            "tile_attribution": os.environ.get("MAP_TILE_ATTRIBUTION", "&copy; OpenStreetMap contributors")}


def totals(pts: list[dict]) -> dict:
    kwh = sum((Decimal(str(p["kwh"])) for p in pts if p.get("kwh") is not None), Decimal(0))
    return {"count": len(pts), "kwh": kwh, "by_category": dict(Counter(p["category"] for p in pts)),
            "by_vendor": dict(Counter(p["vendor"] for p in pts)), "out_of_territory": sum(1 for p in pts if p["out_of_territory"])}


def export_csv(res: dict) -> bytes:
    cols = ["vendor", "enrollment_id", "seller", "status", "category", "sale_date", "approval_date", "kwh", "project", "city", "state", "zip",
            "lat", "lng", "confidence", "geocode", "areas", "out_of_territory"]
    if res["show_customer"]:
        cols[3:3] = ["customer", "address"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for p in res["points"]:
        w.writerow(["; ".join(p[c]) if c == "areas" else ("" if p.get(c) is None else p[c]) for c in cols])
    return buf.getvalue().encode("utf-8-sig")

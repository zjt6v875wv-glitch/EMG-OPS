"""Service-address enrichment from files that are not payroll invoices.

Vendor payroll files carry no service addresses, so the map is fed by:

* ``status_report``   a project enrollment status report (Enrollment Number,
                      service address, Annual kWh, project, broker rep).
                      Rows for enrollments not yet known are created as
                      map-only enrollments (origin ``status_report``, no sale date).
* ``address_mapping`` an export with Sale Date, Full Service Address, Customer
                      Name, Status, Selling Agent and no enrollment id. Rows are
                      matched to known enrollments by customer name and sale date
                      (within 3 days); anything not matched exactly once is left
                      unmatched rather than guessed.
* ``coordinates``     Vendor, Enrollment ID, Latitude, Longitude: known locations
                      (stored as ``manual`` with full confidence).

Parsing only builds a plan; nothing is written until the plan is applied.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

from ..names import clean, key

MATCH_DAYS = 3


def _rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = None
    for r in it:  # first row with at least two text cells is the header
        if sum(1 for v in r if isinstance(v, str) and v.strip()) >= 2:
            header = [clean(v) for v in r]
            break
    if not header:
        return []
    out = []
    for r in it:
        if not any(v not in (None, "") for v in r):
            continue
        d = {}
        for h, v in zip(header, r):
            if h and h not in d:  # the status report repeats "Current ER Status"; keep the first
                d[h] = v
        out.append(d)
    return out


def detect(header: list[str]) -> str:
    hs = {h.lower() for h in header}
    if "enrollment number" in hs and "service address line 1" in hs:
        return "status_report"
    if "full service address" in hs and "customer name" in hs:
        return "address_mapping"
    if {"latitude", "longitude"} <= hs or {"lat", "lng"} <= hs:
        return "coordinates"
    raise ValueError("Unrecognised address file: expected a status report, an address mapping export, or a coordinates CSV")


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = clean(v)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.split()[0], fmt).date()
        except (ValueError, IndexError):
            continue
    return None


def _zip(v) -> str:
    s = clean(v)
    if re.fullmatch(r"\d{1,4}", s):  # spreadsheets drop New England leading zeros
        s = s.zfill(5)
    return s[:10]


def split_full_address(s: str) -> tuple[str, str, str, str]:
    """'123 Main St Apt 4, Garden Grove, CA 92843' -> (street, city, state, zip)."""
    parts = [p.strip() for p in clean(s).split(",") if p.strip()]
    if len(parts) < 3:
        return clean(s), "", "", ""
    m = re.fullmatch(r"([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?", parts[-1])
    if not m:
        return ", ".join(parts[:-2]), parts[-2], "", ""
    return ", ".join(parts[:-2]), parts[-2], m.group(1).upper(), m.group(2) or ""


def plan_status_report(rows: list[dict], vendor: str, known: dict[str, dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 2):
        eid = clean(r.get("Enrollment Number"))
        if not eid:
            out.append({"row": i, "action": "skip", "reason": "No enrollment number"})
            continue
        street = ", ".join(x for x in (clean(r.get("Service Address Line 1")), clean(r.get("Service Address Line 2"))) if x)
        fields = {"service_address": street or None, "city": clean(r.get("Service City")) or None, "state": clean(r.get("Service State")).upper() or None,
                  "zip": _zip(r.get("Service Zip Code")) or None, "project": clean(r.get("Current Project Name")) or None}
        kwh = r.get("Annual k Wh") or r.get("Annual kWh")
        e = known.get(eid)
        if e is None:
            out.append({"row": i, "action": "create", "vendor": vendor, "enrollment_id": eid, "fields": fields | {
                "customer_name": clean(r.get("Customer Name")) or None, "raw_rep": clean(r.get("Broker Rep")) or None,
                "utility": clean(r.get("Utility Name")) or None, "status": clean(r.get("Current ER Status")) or None,
                "kwh": str(kwh) if kwh not in (None, "") else None}})
            continue
        change = {k: v for k, v in fields.items() if v and v != e.get(k)}
        if kwh not in (None, "") and e.get("kwh") is None:
            change["kwh"] = str(kwh)
        out.append({"row": i, "action": "enrich" if change else "unchanged", "vendor": vendor, "enrollment_id": eid, "fields": change})
    return out


def plan_address_mapping(rows: list[dict], candidates: list[dict], canon) -> list[dict]:
    """candidates: enrollments with customer_name, sale_date, raw_rep, vendor."""
    by_name: dict[str, list[dict]] = {}
    for e in candidates:
        by_name.setdefault(key(e["customer_name"]), []).append(e)
    out = []
    for i, r in enumerate(rows, 2):
        status = clean(r.get("Status"))
        if status.lower() in ("no sale",):
            out.append({"row": i, "action": "skip", "reason": f"Status {status}"})
            continue
        sd = _date(r.get("Sale Date"))
        street, city, state, z = split_full_address(r.get("Full Service Address"))
        if not street:
            out.append({"row": i, "action": "skip", "reason": "No address"})
            continue
        hits = [e for e in by_name.get(key(r.get("Customer Name")), []) if sd and e["sale_date"] and abs((e["sale_date"] - sd).days) <= MATCH_DAYS]
        agent = canon(clean(r.get("Selling Agent")))
        if len(hits) > 1:  # same customer twice: narrow by the selling agent, then by exact date
            hits = [e for e in hits if canon(e["raw_rep"], e["vendor"]) == agent] or hits
            if len(hits) > 1:
                hits = [e for e in hits if e["sale_date"] == sd] or hits
        if not hits:
            out.append({"row": i, "action": "unmatched", "reason": "No enrollment with this customer within 3 days of the sale date",
                        "customer": clean(r.get("Customer Name")), "sale_date": sd.isoformat() if sd else None})
            continue
        if len(hits) > 1:
            out.append({"row": i, "action": "ambiguous", "reason": f"{len(hits)} enrollments match", "candidates": [f"{e['vendor']}:{e['enrollment_id']}" for e in hits]})
            continue
        e = hits[0]
        fields = {"service_address": street, "city": city or None, "state": state or None, "zip": z or None}
        change = {k: v for k, v in fields.items() if v and v != e.get(k)}
        note = None if canon(e["raw_rep"], e["vendor"]) == agent else "Selling agent differs from the vendor file"
        out.append({"row": i, "action": "enrich" if change else "unchanged", "vendor": e["vendor"], "enrollment_id": e["enrollment_id"],
                    "fields": change, **({"note": note} if note else {})})
    return out


def plan_coordinates(rows: list[dict], known: set[tuple[str, str]]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 2):
        low = {k.lower().strip(): v for k, v in r.items() if k}
        vendor, eid = clean(low.get("vendor")), clean(low.get("enrollment id") or low.get("enrollment_id") or low.get("enrollment number"))
        try:
            lat, lng = float(low.get("latitude") or low.get("lat")), float(low.get("longitude") or low.get("lng"))
        except (TypeError, ValueError):
            out.append({"row": i, "action": "skip", "reason": "Latitude/longitude missing or not numeric"})
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            out.append({"row": i, "action": "skip", "reason": "Coordinates out of range"})
        elif (vendor, eid) not in known:
            out.append({"row": i, "action": "unmatched", "reason": f"Unknown enrollment {vendor}:{eid}"})
        else:
            out.append({"row": i, "action": "locate", "vendor": vendor, "enrollment_id": eid, "lat": lat, "lng": lng})
    return out

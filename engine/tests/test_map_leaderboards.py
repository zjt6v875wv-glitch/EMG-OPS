"""Sales map, territories, capacity and leaderboards (fictional data)."""
import json
from datetime import date, datetime, timezone

import pytest

from emg.geo import enrich, geometry
from test_db_flow import env  # noqa: F401  (fixture re-export)

SQUARE = {"type": "Polygon", "coordinates": [[[-100, 40], [-99, 40], [-99, 41], [-100, 41], [-100, 40]]]}
OWNER = {"email": "o@example.test", "role": "owner", "person_name": None}
MANAGER = {"email": "m@example.test", "role": "manager", "person_name": "Jordan Lee"}
AGENT = {"email": "a@example.test", "role": "agent", "person_name": "Alex Rivera"}


def ins(c, vendor, eid, rep, sale=None, status="Pending", appr=None, inv=None, customer=None, lat=None, lng=None, kwh=None, geo=None, conf=None):
    return c.execute(
        "insert into enrollments(vendor,enrollment_id,raw_rep,sale_date,status,approval_date,approval_date_source,invoice_date,customer_name,lat,lng,kwh,"
        "geocode_status,geocode_confidence,service_address,city,state,zip) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Springfield','KS','66000') returning *",
        (vendor, eid, rep, sale, status, appr, "vendor" if appr else None, inv, customer or f"Customer {eid}", lat, lng, kwh,
         geo or ("matched" if lat is not None else None), conf if conf is not None else (0.95 if lat is not None else None),
         f"{eid} Main St" if lat is not None or geo else None)).fetchone()


# -- geometry ---------------------------------------------------------------------------

def test_point_in_polygon_with_hole_and_multipolygon():
    donut = {"type": "Polygon", "coordinates": [SQUARE["coordinates"][0], [[-99.8, 40.2], [-99.2, 40.2], [-99.2, 40.8], [-99.8, 40.8], [-99.8, 40.2]]]}
    assert geometry.contains(SQUARE, -99.5, 40.5)
    assert not geometry.contains(donut, -99.5, 40.5)          # inside the hole
    assert geometry.contains(donut, -99.9, 40.1)
    multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"], [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]]}
    assert geometry.contains(multi, 0.5, 0.5) and not geometry.contains(multi, 5, 5)


def test_territory_files_kml_geojson_csv():
    kml = """<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>North</name>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>-100,40,0 -99,40,0 -99,41,0 -100,41,0 -100,40,0</coordinates></LinearRing></outerBoundaryIs></Polygon>
      </Placemark></Document></kml>"""
    k = geometry.parse_territories("t.kml", kml.encode())
    assert k[0]["name"] == "North" and geometry.contains(k[0]["geometry"], -99.5, 40.5)
    gj = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "Sq", "capacity_kwh": 5000}, "geometry": SQUARE}]})
    g = geometry.parse_territories("t.geojson", gj.encode())
    assert g[0]["properties"]["capacity_kwh"] == 5000
    wkt = 'name,wkt\nW,"POLYGON((-100 40, -99 40, -99 41, -100 41, -100 40))"\n'
    assert geometry.contains(geometry.parse_territories("t.csv", wkt.encode())[0]["geometry"], -99.5, 40.5)
    verts = "name,lat,lng\nV,40,-100\nV,40,-99\nV,41,-99\nV,41,-100\n"
    assert geometry.contains(geometry.parse_territories("t.csv", verts.encode())[0]["geometry"], -99.5, 40.5)  # ring is closed for you
    with pytest.raises(ValueError):
        geometry.parse_territories("t.geojson", json.dumps({"type": "Point", "coordinates": [0, 0]}).encode())


# -- enrichment ---------------------------------------------------------------------------

def test_address_mapping_matches_by_customer_and_date_never_guesses():
    cands = [
        {"vendor": "Spark", "enrollment_id": "1", "customer_name": "Ana Cruz", "sale_date": date(2026, 10, 6), "raw_rep": "Alex Rivera"},
        {"vendor": "Spark", "enrollment_id": "2", "customer_name": "Bo Diaz", "sale_date": date(2026, 10, 6), "raw_rep": "Alex Rivera"},
        {"vendor": "Spark", "enrollment_id": "3", "customer_name": "Bo Diaz", "sale_date": date(2026, 10, 7), "raw_rep": "Sam Patel"},
        {"vendor": "Spark", "enrollment_id": "4", "customer_name": "Cy Ng", "sale_date": date(2026, 10, 6), "raw_rep": "Sam Patel"},
        {"vendor": "Spark", "enrollment_id": "5", "customer_name": "Cy Ng", "sale_date": date(2026, 10, 6), "raw_rep": "Sam Patel"},
    ]
    canon = lambda raw, vendor=None: raw  # noqa: E731
    rows = [
        {"Sale Date": datetime(2026, 10, 7, 9), "Full Service Address": "1 Elm St Apt 2, Springfield, KS 66000", "Customer Name": "ANA CRUZ", "Status": "Complete", "Selling Agent": "Alex Rivera"},
        {"Sale Date": datetime(2026, 10, 6), "Full Service Address": "2 Oak St, Springfield, KS 66000", "Customer Name": "Bo Diaz", "Status": "Complete", "Selling Agent": "Sam Patel"},
        {"Sale Date": datetime(2026, 10, 6), "Full Service Address": "3 Pine St, Springfield, KS 66000", "Customer Name": "Cy Ng", "Status": "Complete", "Selling Agent": "Sam Patel"},
        {"Sale Date": datetime(2026, 10, 20), "Full Service Address": "4 Fir St, Springfield, KS 66000", "Customer Name": "Ana Cruz", "Status": "Complete", "Selling Agent": "Alex Rivera"},
        {"Sale Date": datetime(2026, 10, 6), "Full Service Address": "5 Ash St, Springfield, KS 66000", "Customer Name": "Di Wu", "Status": "No Sale", "Selling Agent": "Sam Patel"},
    ]
    plan = enrich.plan_address_mapping(rows, cands, canon)
    assert plan[0]["action"] == "enrich" and plan[0]["enrollment_id"] == "1"
    assert plan[0]["fields"] == {"service_address": "1 Elm St Apt 2", "city": "Springfield", "state": "KS", "zip": "66000"}
    assert plan[1]["enrollment_id"] == "3"                    # two Bo Diaz sales: the selling agent decides
    assert plan[2]["action"] == "ambiguous"                   # still two candidates: left alone
    assert plan[3]["action"] == "unmatched"                   # 14 days away: not the same sale
    assert plan[4]["action"] == "skip"


def test_status_report_creates_map_only_enrollments_and_coordinates_are_manual(env, tmp_path):
    import openpyxl

    from emg.db.conn import connect
    from emg.services import geo, imports

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Current Project Name", "Customer Name", "Annual k Wh", "Enrollment Number", "Broker Rep", "Utility Name", "Current ER Status",
               "Service Address Line 1", "Service Address Line 2", "Service City", "Service State", "Service Zip Code"])
    ws.append(["Hilltop", "PAT KIM", 4200, "880001", "Alex Rivera", "North Power", "FNLD", "9 Shore Rd", None, "Bath", "ME", 4530])
    p = tmp_path / "status.xlsx"
    wb.save(p)
    with connect(env["db"]) as c:
        sf = imports.store_upload(c, env["storage"], p.read_bytes(), "status.xlsx", "test")
        with pytest.raises(ValueError):
            geo.enrich_preview(c, env["storage"], sf["id"], "test")      # vendor is required for status reports
        b = geo.enrich_preview(c, env["storage"], sf["id"], "test", vendor="SolarCo")
        assert b["summary"]["create"] == 1
        assert geo.enrich_apply(c, b["id"], "test") == {"created": 1}
        e = c.execute("select * from enrollments where vendor='SolarCo'").fetchone()
        assert (e["zip"], e["project"], e["origin"], e["sale_date"], e["lat"]) == ("04530", "Hilltop", "status_report", None, None)
        with pytest.raises(ValueError):
            geo.enrich_apply(c, b["id"], "test")                          # applying twice is refused
        coords = b"Vendor,Enrollment ID,Latitude,Longitude\nSolarCo,880001,43.91,-69.82\nSolarCo,999,43,-69\n"
        sf2 = imports.store_upload(c, env["storage"], coords, "coords.csv", "test")
        b2 = geo.enrich_preview(c, env["storage"], sf2["id"], "test")
        assert (b2["kind"], b2["summary"].get("locate"), b2["summary"].get("unmatched")) == ("coordinates", 1, 1)
        geo.enrich_apply(c, b2["id"], "test")
        e = c.execute("select * from enrollments where vendor='SolarCo'").fetchone()
        assert (e["lat"], e["geocode_status"], e["geocode_confidence"]) == (43.91, "manual", 1.0)


class FakeProvider:
    name = "fake"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def geocode(self, items):
        from emg.geo.geocode import GeoResult

        if self.fail:
            raise OSError("network down")
        self.calls.append(items)
        return [GeoResult(k, s, 40.5, -99.5, 0.95, "street", "matched", "fake", []) if "Main" in s else
                GeoResult(k, s, None, None, None, "none", "unmatched", "fake", []) for k, s, *_ in items]


def test_geocoding_uses_cache_and_never_invents(env):
    from emg.db.conn import connect
    from emg.services import geo

    with connect(env["db"]) as c:
        for eid, addr in (("A", "1 Main St"), ("B", "1 Main St Apt 4"), ("C", "Nowhere Rd")):
            c.execute("insert into enrollments(vendor,enrollment_id,raw_rep,service_address,city,state,zip,status) values ('Spark',%s,'Alex Rivera',%s,'Springfield','KS','66000','Pending')", (eid, addr))
        assert geo.geocode_pending(c, "t", provider=FakeProvider(fail=True))["pending"] == 2   # error: nothing is located or cached
        assert c.execute("select count(*) n from geocode_cache").fetchone()["n"] == 0
        prov = FakeProvider()
        geo.geocode_pending(c, "t", provider=prov)
        assert len(prov.calls[0]) == 2                       # the apartment shares its building's lookup
        got = {r["enrollment_id"]: r for r in c.execute("select * from enrollments")}
        assert got["A"]["lat"] == got["B"]["lat"] == 40.5 and got["C"]["lat"] is None and got["C"]["geocode_status"] == "unmatched"
        c.execute("insert into enrollments(vendor,enrollment_id,raw_rep,service_address,city,state,zip,status) values ('Spark','D','Sam Patel','1 Main St','Springfield','KS','66000','Pending')")
        prov2 = FakeProvider()
        assert geo.geocode_pending(c, "t", provider=prov2) == {"from_cache": 1} and not prov2.calls
        geo.set_location(c, "Spark", "C", 40.1, -99.1, "t")
        assert c.execute("select status from geocode_cache where address_key='NOWHERE RD SPRINGFIELD KS 66000'").fetchone()["status"] == "manual"
        pts = geo.points(c, OWNER)
        assert {p["enrollment_id"] for p in pts["points"]} == {"A", "B", "C", "D"}


def test_map_scoping_territory_flags_and_export(env):
    from emg.db.conn import connect
    from emg.services import geo

    with connect(env["db"]) as c:
        ins(c, "Nexamp", "N1", "Alex Rivera", date(2026, 10, 5), "Closed Won", lat=40.5, lng=-99.5, kwh=1000)
        ins(c, "Nexamp", "N2", "Sam Patel", date(2026, 10, 5), "Pending Review", lat=45.0, lng=-90.0, kwh=500)
        ins(c, "Spark", "S1", "Casey Morgan", date(2026, 10, 6), "Paid by vendor", lat=40.6, lng=-99.6)
        ins(c, "Spark", "S2", "Casey Morgan", date(2026, 10, 6), "Pending", lat=40.6, lng=-99.6, conf=0.5)     # low confidence: hidden
        ins(c, "Spark", "S3", "Casey Morgan", date(2026, 10, 6), "Pending")                                    # no address
        assert geo.points(c, OWNER)["points"][0]["out_of_territory"] is None                                  # no territory defined yet
        geo.territory_import(c, env["storage"], "t.geojson", json.dumps({"type": "Feature", "properties": {"name": "Plains"}, "geometry": SQUARE}).encode(),
                             "territory", "Nexamp", "t")
        res = geo.points(c, OWNER)
        by = {p["enrollment_id"]: p for p in res["points"]}
        assert set(by) == {"N1", "N2", "S1"} and res["unmapped"] == {"low_confidence": 1, "no_address": 1}
        assert (by["N1"]["out_of_territory"], by["N2"]["out_of_territory"], by["S1"]["out_of_territory"]) == (False, True, None)
        assert by["N1"]["category"] == "approved" and by["N1"]["customer"] == "Customer N1"
        assert [p["enrollment_id"] for p in geo.points(c, OWNER, {"out_of_territory": True})["points"]] == ["N2"]
        assert {p["enrollment_id"] for p in geo.points(c, OWNER, {"bbox": [-100, 40, -99, 41]})["points"]} == {"N1", "S1"}
        assert geo.totals(res["points"])["kwh"] == 1500
        mine = geo.points(c, MANAGER)                       # Jordan Lee's team: Alex, Sam, Taylor (not Casey)
        assert {p["enrollment_id"] for p in mine["points"]} == {"N1", "N2"} and "customer" not in mine["points"][0]
        assert b"Customer" not in geo.export_csv(mine) and b"Customer N1" in geo.export_csv(res)
        assert geo.points(c, MANAGER, {"q": "customer n1"})["points"] == []   # managers cannot search by customer
        with pytest.raises(PermissionError):
            geo.points(c, AGENT)


def test_capacity_allocation(env):
    from emg.db.conn import connect
    from emg.services import geo

    with connect(env["db"]) as c:
        ins(c, "Nexamp", "N1", "Alex Rivera", date(2026, 10, 5), "Closed Won", kwh=3000)
        ins(c, "Nexamp", "N2", "Alex Rivera", date(2026, 10, 5), "Closed Won")
        t = geo.territory_import(c, env["storage"], "p.geojson", json.dumps({"type": "Feature", "properties": {"name": "Hilltop", "capacity_kwh": 4000}, "geometry": SQUARE}).encode(),
                                 "project", None, "t")[0]
        geo.allocate(c, t["id"], "Nexamp", "N1", "t")
        with pytest.raises(ValueError):
            geo.allocate(c, t["id"], "Nexamp", "N2", "t")              # no kWh and no estimate
        a2 = geo.allocate(c, t["id"], "Nexamp", "N2", "t", kwh=2000)
        cap = geo.capacity(c)[0]
        assert (cap["allocated_kwh"], cap["estimated_kwh"], cap["remaining_kwh"], cap["over_capacity"]) == (5000, 2000, -1000, True)
        with pytest.raises(Exception), c.transaction():
            geo.allocate(c, t["id"], "Nexamp", "N1", "t")              # one active allocation per enrollment
        geo.release(c, a2["id"], "t")
        assert geo.capacity(c)[0]["remaining_kwh"] == 1000


# -- leaderboards ------------------------------------------------------------------------

def _sales(c):
    # Week of Mon 2026-10-12 .. Sun 10-18, and the week before.
    for i in range(3):
        ins(c, "Nexamp", f"A{i}", "Alex Rivera", date(2026, 10, 13), "Closed Won", appr=date(2026, 10, 14))
    ins(c, "Nexamp", "A9", "Alex Rivera", date(2026, 10, 13), "Duplicate")                  # duplicates never count
    ins(c, "Spark", "T1", "Taylor Brooks", date(2026, 10, 18), "Payable on 10/29/2026", inv=date(2026, 10, 19))
    ins(c, "Nexamp", "T2", "Taylor Brooks", date(2026, 10, 12), "Closed Lost")
    for i in range(2):
        ins(c, "Nexamp", f"S{i}", "Sam Patel", date(2026, 10, 15), "Pending Review")
    ins(c, "Spark", "C1", "Casey Morgan", date(2026, 10, 16), "Paid by vendor", inv=date(2026, 10, 16))
    ins(c, "Spark", "C2", "Casey Morgan", date(2026, 10, 19), "Pending")                       # next Monday: next week
    for i in range(4):
        ins(c, "Nexamp", f"P{i}", "Sam Patel", date(2026, 10, 7), "Closed Won", appr=date(2026, 10, 8))   # previous week


def test_written_weekly_ranks_ties_and_changes(env):
    from emg.db.conn import connect
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        _sales(c)
        r = lb.compute(c, None, "weekly", date(2026, 10, 15), "written")
        assert (r["period_start"], r["period_end"]) == (date(2026, 10, 12), date(2026, 10, 18))
        rows = [(x["rank"], x["name"], x["count"], x["change"]) for x in r["rows"]]
        assert rows == [(1, "Alex Rivera", 3, None), (2, "Sam Patel", 2, -1), (2, "Taylor Brooks", 2, None), (4, "Casey Morgan", 1, None)]
        assert r["rows"][0]["approved_now"] == 3 and r["total_enrollments"] == 8
        assert [x["name"] for x in lb.compute(c, None, "weekly", date(2026, 10, 15), "written", top=1)["rows"]] == ["Alex Rivera"]
        d = lb.compute(c, None, "daily", date(2026, 10, 18), "written")
        assert [(x["name"], x["count"]) for x in d["rows"]] == [("Taylor Brooks", 1)]
        assert lb.compute(c, None, "weekly", date(2026, 10, 15), "written", vendors=["Spark"])["total_enrollments"] == 2


def test_approved_boards_label_proxies(env):
    from emg.db.conn import connect
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        _sales(c)
        r = lb.compute(c, None, "weekly", date(2026, 10, 15), "approved")
        assert [(x["name"], x["count"]) for x in r["rows"]] == [("Alex Rivera", 3), ("Casey Morgan", 1)]
        assert r["proxies"] == [{"source": "invoice_proxy", "count": 1, "label": "approval date estimated from the invoice date"}]
        assert r["rows"][0]["also_written_in_period"] == 3
        nxt = lb.compute(c, None, "weekly", date(2026, 10, 20), "approved")      # Taylor's sale is approved by invoice on 10/19
        assert [(x["name"], x["count"]) for x in nxt["rows"]] == [("Taylor Brooks", 1)]
        wa = lb.compute(c, None, "weekly", date(2026, 10, 15), "written_approved")
        assert [(x["name"], x["count"]) for x in wa["rows"]] == [("Alex Rivera", 3), ("Casey Morgan", 1), ("Taylor Brooks", 1)]


def test_manager_rankings_respect_vendor_scoped_memberships(env):
    from emg.db.conn import connect
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        _sales(c)
        r = lb.compute(c, None, "weekly", date(2026, 10, 15), "written", group="manager")
        # Jordan Lee: Alex 3 + Sam 2 + Taylor's Nexamp sale only (membership is Nexamp-only) = 6
        assert [(x["name"], x["count"]) for x in r["rows"]] == [("Jordan Lee", 6)]
        team = lb.compute(c, None, "weekly", date(2026, 10, 15), "written", manager="Jordan Lee")
        assert {x["name"] for x in team["rows"]} == {"Alex Rivera", "Sam Patel", "Taylor Brooks"}
        dd = lb.drilldown(c, MANAGER, "Jordan Lee", "weekly", date(2026, 10, 15), "written", group="manager")
        assert len(dd) == 6 and "T1" not in {x["enrollment_id"] for x in dd}
        assert "customer_name" not in dd[0]
        with pytest.raises(PermissionError):
            lb.drilldown(c, AGENT, "Sam Patel", "weekly", date(2026, 10, 15), "written")
        assert len(lb.drilldown(c, AGENT, "Alex Rivera", "weekly", date(2026, 10, 15), "written")) == 3


def test_exports_have_no_customer_details(env):
    from emg.db.conn import connect
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        _sales(c)
        r = lb.compute(c, None, "weekly", date(2026, 10, 15), "written")
        csv_ = lb.export_csv(r).decode("utf-8-sig")
        assert csv_.splitlines()[1] == "1,new,Alex Rivera,3,"
        pdf = lb.export_pdf(r)
        png = lb.export_png(r)
        assert pdf.startswith(b"%PDF") and png.startswith(b"\x89PNG")
        assert b"Customer" not in pdf and "Customer" not in csv_
        assert lb.file_name(r, "png") == "EMG_Weekly_Written_Agents_2026-10-12.png"


def test_schedule_due_periods_in_business_timezone():
    from emg.services import leaderboards as lb

    s = {"cadence": "daily", "timezone": "America/Los_Angeles", "run_hour": 8, "run_weekday": 0}
    # 14:00 UTC on Oct 20 is 07:00 in Los Angeles: not yet due
    assert lb.due_period(s, datetime(2026, 10, 20, 14, tzinfo=timezone.utc)) is None
    assert lb.due_period(s, datetime(2026, 10, 20, 16, tzinfo=timezone.utc)) == (date(2026, 10, 19), date(2026, 10, 19))
    w = s | {"cadence": "weekly"}
    assert lb.due_period(w, datetime(2026, 10, 19, 16, tzinfo=timezone.utc)) == (date(2026, 10, 12), date(2026, 10, 18))
    assert lb.due_period(w, datetime(2026, 10, 20, 16, tzinfo=timezone.utc)) is None    # not Monday


def test_schedules_generate_once_and_publish_only_when_enabled(env):
    from emg.db.conn import connect
    from emg.services import leaderboards as lb

    with connect(env["db"]) as c:
        _sales(c)
        lb.save_destinations(c, [{"name": "box", "type": "outbox", "enabled": False}], "t")
        with pytest.raises(ValueError):
            lb.save_destinations(c, [{"name": "x", "type": "webhook", "url": "http://insecure.example"}], "t")
        s = lb.save_schedule(c, {"name": "Weekly", "cadence": "weekly", "metrics": ["written", "approved"], "groups": ["agent"],
                                 "enabled": True, "publish_enabled": True, "destination": "box"}, "t")
        r = lb.run_schedule(c, env["storage"], s, date(2026, 10, 12))
        assert r["publish_result"] == {"published": False, "reason": "Destination box is missing or disabled"}
        assert len(r["files"]) == 6 and lb.run_schedule(c, env["storage"], s, date(2026, 10, 12)) is None
        lb.upsert_destination(c, {"name": "box", "type": "outbox", "enabled": True}, "t")
        r2 = lb.run_schedule(c, env["storage"], s, date(2026, 10, 5))
        assert r2["publish_result"]["published"] is True
        off = lb.save_schedule(c, {"cadence": "daily", "metrics": ["written"], "enabled": True, "publish_enabled": False, "destination": "box"}, "t")
        assert lb.run_schedule(c, env["storage"], off, date(2026, 10, 13))["publish_result"]["published"] is False
        ran = lb.run_due(c, env["storage"], datetime(2026, 10, 19, 13, tzinfo=timezone.utc))   # Monday 09:00 New York
        assert {x["schedule"] for x in ran} == {"Daily leaderboard"}                           # weekly for 10/12 already exists

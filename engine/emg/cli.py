"""Command line for setup, seeding and replay.

    python -m emg.cli migrate
    python -m emg.cli create-user owner@example.com "Owner" owner
    python -m emg.cli seed-control CONTROL.xlsx --week 2026-W42 [--overlay rules_overlay.json]
    python -m emg.cli sync-roster CONTROL.xlsx                            # refresh statement emails from a newer Email Map
    python -m emg.cli import FILE --week 2026-W43 [--importer spark] [--commit]
    python -m emg.cli calculate 2026-W43
    python -m emg.cli finalize RUN_ID --key KEY
    python -m emg.cli replay-w43 FIXTURE_DIR [--overlay OVERLAY]          # engine only, no DB
    python -m emg.cli db-replay-w43 FIXTURE_DIR [--overlay OVERLAY]       # full DB path into an empty database
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from decimal import Decimal
from pathlib import Path

from .db.conn import connect, migrate
from .money import D, cents

ACTOR = "cli"


def cmd_migrate(a):
    print(migrate() or "up to date")


def cmd_create_user(a):
    from .services import auth

    pw = a.password or getpass.getpass("Password: ")
    with connect() as c:
        u = auth.create_user(c, a.email, a.name, a.role, pw, ACTOR, person_name=a.person)
    print("created", u["email"], u["role"])


def seed_control(c, control: Path, week: str, overlay: Path | None, actor: str = ACTOR) -> dict:
    from .replay.historical import merge_overlay
    from .rules.control_import import import_email_map, import_openings, import_processed_chargebacks, import_rules
    from .services import ledger, people
    from .services import rules as rules_svc

    cfg, rep = import_rules(control, version=f"control:{week}")
    if overlay:
        cfg = merge_overlay(cfg, json.loads(Path(overlay).read_text()))
    rs = rules_svc.create(c, cfg, f"Imported from {control.name}" + (f" + {overlay.name}" if overlay else ""), actor,
                          note=f"{len(rep.items)} import notes ({sum(1 for i in rep.items if i['level'] == 'review')} need review)")
    rules_svc.activate(c, rs["version"], actor, f"Initial rules from {control.name}")
    for i in rep.items:
        if i["level"] == "review":
            c.execute("insert into review_items(scope_key,kind,message,blocking,data) values (%s,'rule_import',%s,false,%s) on conflict do nothing",
                      (f"ruleimport:{control.name}:{i['sheet']}:{i['row']}", f"{i['sheet']} row {i['row']}: {i['message']}",
                       __import__("emg.db.conn", fromlist=["J"]).J(i)))
    emails = {e["person"]: e for e in import_email_map(control)}
    for n in sorted(set(cfg.get("people", [])) | set(emails)):
        e = emails.get(n, {})
        people.upsert(c, n, actor, email=e.get("email"), email_verified=e.get("verified", False), email_excluded=e.get("excluded", False),
                      email_status=e.get("status"), notes=e.get("note"))
    for r in cfg.get("routing", []):
        people.upsert(c, r["payee"], actor, kind="company")
    for m in cfg.get("memberships", []):
        people.upsert(c, m["manager"], actor, kind="manager") if not c.execute("select 1 from people where name=%s", (m["manager"],)).fetchone() else None
    openings = import_openings(control, cfg)
    for n in openings:
        if not c.execute("select 1 from people where name=%s", (n,)).fetchone():
            people.upsert(c, n, actor, notes="Has a ledger balance but is not on the Email Map")
    n_open = ledger.seed_openings(c, openings, week, actor, control.name)
    n_proc = 0
    for p in import_processed_chargebacks(control):
        vendor = p["source"] or "unknown"
        r = c.execute("insert into processed_chargebacks(vendor,enrollment_id,week,source,note) values (%s,%s,%s,'historical_import',%s) on conflict do nothing returning 1",
                      (vendor, p["reference"], week, p["raw_reference"])).fetchone()
        n_proc += bool(r)
    return {"rule_version": rs["version"], "people": len(set(cfg.get("people", [])) | set(emails)), "opening_entries": n_open, "processed_chargebacks": n_proc}


def cmd_seed_control(a):
    with connect() as c:
        print(json.dumps(seed_control(c, Path(a.control), a.week, Path(a.overlay) if a.overlay else None), indent=1))


def sync_roster(c, control: Path, actor: str = ACTOR) -> dict:
    """Update people and statement emails from a newer control workbook's Email Map (never removes anyone)."""
    from .rules.control_import import import_email_map
    from .services import people

    n = 0
    for e in import_email_map(control):
        before = c.execute("select email, email_verified, email_excluded from people where name=%s", (e["person"],)).fetchone()
        people.upsert(c, e["person"], actor, email=e.get("email"), email_verified=e["verified"], email_excluded=e["excluded"],
                      email_status=e.get("status"), notes=e.get("note"))
        n += before != c.execute("select email, email_verified, email_excluded from people where name=%s", (e["person"],)).fetchone()
    return {"changed": n}


def cmd_sync_roster(a):
    with connect() as c:
        print(json.dumps(sync_roster(c, Path(a.control))))


def import_file(c, storage, path: Path, week: str, importer: str | None, commit: bool, actor: str = ACTOR) -> dict:
    from .services import imports

    sf = imports.store_upload(c, storage, path.read_bytes(), path.name, actor)
    b = imports.preview(c, storage, sf["id"], week, actor, importer)
    if commit:
        b = imports.commit(c, b["id"], actor, accept_errors=True)
    return b


def cmd_import(a):
    from .services.storage import get_storage

    with connect() as c:
        b = import_file(c, get_storage(), Path(a.file), a.week, a.importer, a.commit)
    print(json.dumps({"batch": str(b["id"]), "importer": b["importer"], "status": b["status"], "summary": b["summary"]}, indent=1, default=str))


def cmd_calculate(a):
    from .services import runs

    with connect() as c:
        r = runs.calculate(c, a.week, ACTOR)
    print(json.dumps({"run": str(r["id"]), "revision": r["revision"], "totals": r["totals"]}, indent=1, default=str))


def cmd_finalize(a):
    from .services import runs

    with connect() as c:
        r = runs.finalize(c, a.run, ACTOR, a.key)
    print(json.dumps({"run": str(r["id"]), "status": r["status"]}, default=str))


def cmd_replay(a):
    from .replay.historical import replay_w43

    rep, _ = replay_w43(a.fixtures, a.overlay)
    d = rep.to_dict()
    print(json.dumps({k: d[k] for k in ("ok", "matched_people", "mismatches", "missing_in_app", "extra_in_app", "totals_app")}, indent=1))
    sys.exit(0 if rep.ok else 1)


def db_replay_w43(c, storage, fixtures: Path, overlay: Path | None, actor: str = ACTOR) -> dict:
    """Seed an empty database from the W42 control workbook, import the W43
    vendor files, record the documented W43 week facts and compare the
    calculated run with the W43 final for every person."""
    from .replay.historical import COMPARE_FIELDS, load_facts, w43_adjustments, w43_evidence
    from .services import adjustments, runs

    calc = json.loads((fixtures / "week43_calc.json").read_text())
    seeded = seed_control(c, fixtures / "recovery/week42/EMG_Payroll_Control_Week42_WORKING.xlsx", "2026-W42", overlay, actor)
    inp = fixtures / "inputs/week43"
    batches = {}
    for f in ("Sales Report Energy Marketing.xlsx", "ENERGY MARKETING GROUP LLC Commission Statement 10.21.26.xlsx",
              "Commissions_EnergyMarketingGroup_CAL26WK43.xlsx", "EMGD2D_20261017.xlsx"):
        b = import_file(c, storage, inp / f, "2026-W43", None, True, actor)
        batches[f] = {"importer": b["importer"], "counts": b["summary"]["counts"], "refused": b["summary"].get("committed", {}).get("refused_duplicates")}
    for ev in w43_evidence(calc):
        c.execute("insert into payout_evidence(vendor,enrollment_id,payee,activity,amount,rate,week,source,proof,created_by)"
                  " values (%s,%s,%s,%s,%s,%s,%s,'historical_import',%s,%s)",
                  (ev.vendor, ev.enrollment_id, ev.payee, ev.activity, ev.amount, ev.rate, ev.week or "2026-W42", ev.proof, actor))
    from .services import people

    for adj in w43_adjustments(calc, load_facts(overlay)):
        if not c.execute("select 1 from people where name=%s", (adj.person,)).fetchone():
            # The final opened a ledger under this payment contact; keep it separate until identified.
            people.upsert(c, adj.person, actor, notes="Payment contact from W43 advance intake; full payroll identity unresolved")
            c.execute("insert into review_items(scope_key,week,kind,message,person,blocking) values (%s,'2026-W43','identity_unresolved',%s,%s,false) on conflict do nothing",
                      (f"identity:{adj.person}", f"{adj.person!r} has a ledger but no confirmed payroll identity", adj.person))
        adjustments.add(c, "2026-W43", adj.person, adj.kind, actor, adj.authorization, amount=adj.amount, method=adj.method,
                        value=adj.value, debts=adj.debts, creditor=adj.creditor, label=adj.label, note=adj.note)
    run = runs.calculate(c, "2026-W43", actor)
    fixture = {r["payee"]: r for r in calc["summary"]}
    app = {r["payee"]: r["data"] for r in c.execute("select payee, data from run_people where run_id=%s", (run["id"],))}
    mismatches, matched, missing = [], 0, []
    for name, fx in fixture.items():
        p = app.get(name)
        if p is None:
            if any(D(fx.get(f2, 0)) for _, f2 in COMPARE_FIELDS):
                missing.append(name)
            continue
        diffs = {f1: {"app": p[f1], "final": str(cents(D(fx.get(f2, 0))))} for f1, f2 in COMPARE_FIELDS if cents(D(p[f1])) != cents(D(fx.get(f2, 0)))}
        if diffs:
            mismatches.append({"payee": name, "diffs": diffs})
        else:
            matched += 1
    extra = [n for n, p in app.items() if n not in fixture and (D(p["gross_compensation"]) or D(p["final_check"]))]
    return {"seeded": seeded, "batches": batches, "run_id": str(run["id"]), "matched": matched, "mismatches": mismatches,
            "missing": missing, "extra": extra, "totals": run["totals"], "ok": not (mismatches or missing or extra)}


def cmd_db_replay(a):
    from .services.storage import get_storage

    with connect() as c:
        out = db_replay_w43(c, get_storage(), Path(a.fixtures), Path(a.overlay) if a.overlay else None)
    print(json.dumps(out, indent=1, default=str))
    sys.exit(0 if out["ok"] else 1)


def cmd_seed_demo(a):
    """Fictional demo data (sample rules, users, enrollments with locations, a territory).
    Safe for a public demo: no real names, customers or addresses."""
    import random
    from datetime import date, timedelta

    from .services import auth, geo, rules as rules_svc
    from .services.storage import get_storage

    rnd = random.Random(7)
    root = Path(__file__).resolve().parents[2]
    with connect() as c:
        if c.execute("select 1 from enrollments limit 1").fetchone():
            raise SystemExit("seed-demo only runs on an empty database")
        v = rules_svc.create(c, json.loads((root / "config/sample/rules.sample.json").read_text()), "sample", "demo")
        rules_svc.activate(c, v["version"], "demo", "demo rules")
        for email, name, role, person in (("owner@example.test", "Demo Owner", "owner", None), ("manager@example.test", "Jordan Lee", "manager", "Jordan Lee"),
                                          ("agent@example.test", "Alex Rivera", "agent", "Alex Rivera")):
            auth.create_user(c, email, name, role, "demo-password-1", "demo", person)
        reps = ["Alex Rivera", "Sam Patel", "Taylor Brooks", "Casey Morgan", "Jordan Lee"]
        statuses = [("Closed Won", 5), ("Pending Review", 3), ("Paid by vendor", 3), ("Cannot Process - Need Updated Bill Copy", 1), ("Denied - Not in Approved Territory", 1),
                    ("Canceled", 1), ("Charged back", 1)]
        pool = [s for s, w in statuses for _ in range(w)]
        end = date.today()
        for i in range(400):
            vendor = rnd.choice(["Nexamp", "Spark", "Perch"])
            sold = end - timedelta(days=rnd.randint(0, 27))
            st = rnd.choice(pool)
            ok = st in ("Closed Won", "Paid by vendor")
            lat, lng = 38.9 + rnd.gauss(0, 0.35), -94.9 + rnd.gauss(0, 0.5)
            c.execute("insert into enrollments(vendor,enrollment_id,customer_name,raw_rep,service_address,city,state,zip,sale_date,status,approval_date,approval_date_source,"
                      "kwh,lat,lng,geocode_status,geocode_confidence) values (%s,%s,%s,%s,%s,'Demo City','KS','66000',%s,%s,%s,%s,%s,%s,%s,'matched',0.95)",
                      (vendor, f"D{100000 + i}", f"Demo Customer {i}", rnd.choice(reps), f"{100 + i} Example Ave", sold, st,
                       min(end, sold + timedelta(days=rnd.randint(1, 6))) if ok else None, "vendor" if ok else None, rnd.randint(2, 12) * 500, lat, lng))
        box = {"type": "Feature", "properties": {"name": "Demo Territory"},
               "geometry": {"type": "Polygon", "coordinates": [[[-95.6, 38.4], [-94.2, 38.4], [-94.2, 39.4], [-95.6, 39.4], [-95.6, 38.4]]]}}
        geo.territory_import(c, get_storage(), "demo.geojson", json.dumps(box).encode(), "territory", None, "demo")
    print("Demo data ready. Sign in as owner@example.test / demo-password-1 (also manager@ and agent@).")


def cmd_connector_key(a):
    from .connectors.secrets import new_key

    print(new_key())


def cmd_run_connector(a):
    from .services import connectors
    from .services.storage import get_storage

    with connect() as c:
        row = c.execute("select id from connectors where name=%s", (a.name,)).fetchone()
        if not row:
            raise SystemExit(f"No connector named {a.name}")
        print(json.dumps(connectors.run(c, get_storage(), row["id"], "cli"), indent=1, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="emg")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("migrate").set_defaults(fn=cmd_migrate)
    x = sp.add_parser("create-user")
    x.add_argument("email"); x.add_argument("name"); x.add_argument("role"); x.add_argument("--person"); x.add_argument("--password")
    x.set_defaults(fn=cmd_create_user)
    x = sp.add_parser("seed-control")
    x.add_argument("control"); x.add_argument("--week", required=True); x.add_argument("--overlay")
    x.set_defaults(fn=cmd_seed_control)
    x = sp.add_parser("sync-roster"); x.add_argument("control"); x.set_defaults(fn=cmd_sync_roster)
    x = sp.add_parser("import")
    x.add_argument("file"); x.add_argument("--week", required=True); x.add_argument("--importer"); x.add_argument("--commit", action="store_true")
    x.set_defaults(fn=cmd_import)
    x = sp.add_parser("calculate"); x.add_argument("week"); x.set_defaults(fn=cmd_calculate)
    x = sp.add_parser("finalize"); x.add_argument("run"); x.add_argument("--key", required=True); x.set_defaults(fn=cmd_finalize)
    for name, fn in (("replay-w43", cmd_replay), ("db-replay-w43", cmd_db_replay)):
        x = sp.add_parser(name); x.add_argument("fixtures"); x.add_argument("--overlay"); x.set_defaults(fn=fn)
    sp.add_parser("seed-demo").set_defaults(fn=cmd_seed_demo)
    sp.add_parser("connector-key").set_defaults(fn=cmd_connector_key)
    x = sp.add_parser("run-connector"); x.add_argument("name"); x.set_defaults(fn=cmd_run_connector)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

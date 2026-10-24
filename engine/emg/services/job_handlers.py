"""Background job handlers (registered on import)."""
from __future__ import annotations

from .jobs import handler
from .storage import get_storage


@handler("import.preview")
def import_preview(c, p: dict, actor: str):
    from . import imports

    b = imports.preview(c, get_storage(), p["source_file_id"], p.get("week"), actor, p.get("importer"), p.get("options"))
    return {"batch_id": str(b["id"]), "summary": b["summary"]}


@handler("payroll.calculate")
def payroll_calculate(c, p: dict, actor: str):
    from . import runs

    r = runs.calculate(c, p["week"], actor, p.get("rule_version"))
    return {"run_id": str(r["id"]), "revision": r["revision"]}


@handler("reports.build")
def reports_build(c, p: dict, actor: str):
    from . import reports

    return reports.build(c, get_storage(), p["run_id"], actor, tuple(p.get("kinds") or reports.ALL_KINDS))


from . import email  # noqa: E402,F401  (registers email.send)


@handler("geocode.run")
def geocode_run(c, p: dict, actor: str):
    from . import geo

    return geo.geocode_pending(c, actor, int(p.get("limit", 5000)))


@handler("scheduler.tick")
def scheduler_tick(c, p: dict, actor: str):
    """Once a minute: generate due leaderboards and queue due connector syncs."""
    from . import connectors, jobs, leaderboards

    queued = []
    for conn in connectors.due(c):
        if not c.execute("select 1 from jobs where kind='connector.run' and status in ('queued','running') and payload->>'connector_id'=%s",
                         (str(conn["id"]),)).fetchone():
            jobs.enqueue(c, "connector.run", {"connector_id": str(conn["id"]), "trigger": "schedule"}, "scheduler")
            queued.append(conn["name"])
    return {"leaderboards": leaderboards.run_due(c, get_storage()), "connectors_queued": queued}


@handler("connector.run")
def connector_run(c, p: dict, actor: str):
    from . import connectors

    return connectors.run(c, get_storage(), p["connector_id"], actor, p.get("trigger", "manual"))

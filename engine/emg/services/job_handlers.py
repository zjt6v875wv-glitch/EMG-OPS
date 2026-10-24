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


@handler("leaderboards.tick")
def leaderboards_tick(c, p: dict, actor: str):
    from . import leaderboards

    return {"ran": leaderboards.run_due(c, get_storage())}

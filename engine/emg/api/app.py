"""HTTP API used by the Next.js app. Money is always serialized as strings.

Run: uvicorn emg.api.app:app --reload
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlparse
from decimal import Decimal
from typing import Any

from fastapi import encoders as fastapi_encoders
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..db.conn import _default, audit, connect
from ..services import adjustments, auth, connectors, geo, imports, jobs, leaderboards, ledger, payments, people, reports, runs
from ..services import email as email_svc
from ..services import rules as rules_svc
from ..services.storage import get_storage


# Money must never become a float on the wire.
fastapi_encoders.ENCODERS_BY_TYPE[Decimal] = str


class MoneyJSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=_default, ensure_ascii=False).encode()


app = FastAPI(title="EMG Ops API", default_response_class=MoneyJSON)
origins = [o for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(runs.FinalizeBlocked)
async def _blocked(_: Request, e: runs.FinalizeBlocked):
    return MoneyJSON({"detail": str(e), "items": e.items}, status_code=409)


@app.exception_handler(ValueError)
async def _bad(_: Request, e: ValueError):
    return MoneyJSON({"detail": str(e)}, status_code=400)


@app.exception_handler(LookupError)
async def _missing(_: Request, e: LookupError):
    return MoneyJSON({"detail": str(e)}, status_code=404)


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, e: PermissionError):
    return MoneyJSON({"detail": str(e) or "Not allowed"}, status_code=403)


# -- plumbing ----------------------------------------------------------------------

def db():
    with connect() as c:
        yield c


def _token(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return request.cookies.get("emg_session")


def current_user(request: Request, authorization: str | None = Header(default=None), c=Depends(db)) -> dict:
    tok = _token(request, authorization)
    u = tok and auth.user_for_token(c, tok)
    if not u:
        raise HTTPException(401, "Sign in required")
    return u


def need(perm: str):
    def dep(u=Depends(current_user)):
        if not auth.can(u, perm):
            raise HTTPException(403, f"Your role ({u['role']}) cannot {perm}")
        return u
    return dep


def _see(c, u: dict, person: str) -> None:
    vis = auth.visible_people(c, u)
    if vis is not None and person not in vis:
        raise HTTPException(403, "Not your statement")


# -- auth --------------------------------------------------------------------------

class Login(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: Login, response: Response, c=Depends(db)):
    r = auth.login(c, body.email, body.password)
    if not r:
        raise HTTPException(401, "Wrong email or password")
    token, user = r
    response.set_cookie("emg_session", token, httponly=True, samesite="lax", secure=os.environ.get("COOKIE_SECURE", "1") == "1",
                        max_age=auth.SESSION_HOURS * 3600)
    return {"user": user, "token": token}


@app.post("/api/auth/logout")
def logout(request: Request, authorization: str | None = Header(default=None), c=Depends(db)):
    tok = _token(request, authorization)
    if tok:
        auth.logout(c, tok)
    return {"ok": True}


@app.get("/api/auth/me")
def me(u=Depends(current_user)):
    return {"user": u, "permissions": sorted(p for p in auth.PERMS if auth.can(u, p))}


class NewUser(BaseModel):
    email: str
    name: str
    role: str
    password: str
    person_name: str | None = None


@app.get("/api/users")
def list_users(u=Depends(need("users.manage")), c=Depends(db)):
    return c.execute("select id,email,name,role,person_name,active,created_at from users order by email").fetchall()


@app.post("/api/users")
def add_user(body: NewUser, u=Depends(need("users.manage")), c=Depends(db)):
    if body.role == "owner" and u["role"] != "owner":
        raise HTTPException(403, "Only an owner can create owners")
    return auth.create_user(c, body.email, body.name, body.role, body.password, u["email"], body.person_name)


# -- dashboard ---------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(week: str | None = None, u=Depends(current_user), c=Depends(db)):
    weeks = c.execute("select week, max(revision) rev, bool_or(status='finalized') finalized from payroll_runs group by week order by week desc limit 12").fetchall()
    week = week or (weeks[0]["week"] if weeks else None)
    run = week and (c.execute("select * from payroll_runs where week=%s and status='finalized'", (week,)).fetchone()
                    or c.execute("select * from payroll_runs where week=%s and status='calculated' order by revision desc limit 1", (week,)).fetchone())
    open_review = c.execute("select count(*) filter (where blocking) blocking, count(*) total from review_items where status='open'").fetchone()
    out = {"week": week, "weeks": weeks, "review": open_review}
    if run and auth.can(u, "payroll.view_all"):
        out["run"] = {k: run[k] for k in ("id", "week", "revision", "status", "totals", "reconciliation", "rule_set_version", "finalized_at")}
        out["payments"] = c.execute("select state, count(*) n, sum(amount) amount from payments where run_id=%s group by 1", (run["id"],)).fetchall()
        out["imports"] = c.execute("select b.id, b.importer, b.vendor, b.status, b.summary->'counts' counts, f.file_name from import_batches b"
                                   " join source_files f on f.id=b.source_file_id where b.week=%s order by b.created_at", (week,)).fetchall()
    elif run:
        out["mine"] = c.execute("select payee, final_check, status, data from run_people where run_id=%s and payee = any(%s)",
                                (run["id"], list(auth.visible_people(c, u) or []))).fetchall()
    return out


# -- imports -----------------------------------------------------------------------

@app.post("/api/imports")
async def upload(file: UploadFile = File(...), week: str = Form(...), importer: str | None = Form(None), options: str | None = Form(None),
                 background: bool = Form(False), u=Depends(need("imports.manage")), c=Depends(db)):
    data = await file.read()
    sf = imports.store_upload(c, get_storage(), data, file.filename or "upload", u["email"])
    opts = json.loads(options) if options else None
    if background:
        j = jobs.enqueue(c, "import.preview", {"source_file_id": str(sf["id"]), "week": week, "importer": importer, "options": opts}, u["email"])
        return {"job_id": j["id"], "source_file": sf}
    return imports.preview(c, get_storage(), sf["id"], week, u["email"], importer, opts)


@app.get("/api/imports")
def list_imports(week: str | None = None, u=Depends(need("imports.manage")), c=Depends(db)):
    q = ("select b.id, b.week, b.importer, b.vendor, b.status, b.summary, jsonb_array_length(b.errors) errors, b.created_by, b.created_at,"
         " b.committed_at, f.file_name, f.sha256 from import_batches b join source_files f on f.id=b.source_file_id")
    return c.execute(q + (" where b.week=%s" if week else "") + " order by b.created_at desc limit 200", (week,) if week else ()).fetchall()


@app.get("/api/imports/{batch_id}")
def get_import(batch_id: uuid.UUID, u=Depends(need("imports.manage")), c=Depends(db)):
    return imports.batch_view(c, batch_id)


@app.get("/api/imports/{batch_id}/rows")
def import_rows(batch_id: uuid.UUID, line_type: str | None = None, duplicates: bool = False, offset: int = 0, limit: int = 100,
                u=Depends(need("imports.manage")), c=Depends(db)):
    q = "select id, row_no, line_type, enrollment_id, transaction_key, duplicate_of, payload from import_rows where batch_id=%s"
    args: list = [batch_id]
    if line_type:
        q += " and line_type=%s"
        args.append(line_type)
    if duplicates:
        q += " and duplicate_of is not null"
    q += " order by row_no offset %s limit %s"
    return c.execute(q, (*args, offset, min(limit, 500))).fetchall()


class CommitBody(BaseModel):
    accept_errors: bool = False


@app.post("/api/imports/{batch_id}/commit")
def commit_import(batch_id: uuid.UUID, body: CommitBody, u=Depends(need("imports.manage")), c=Depends(db)):
    return imports.commit(c, batch_id, u["email"], body.accept_errors)


class NoteBody(BaseModel):
    note: str


@app.post("/api/imports/{batch_id}/reject")
def reject_import(batch_id: uuid.UUID, body: NoteBody, u=Depends(need("imports.manage")), c=Depends(db)):
    imports.reject(c, batch_id, u["email"], body.note)
    return {"ok": True}


@app.get("/api/files/{file_id}")
def download_file(file_id: uuid.UUID, u=Depends(need("imports.manage")), c=Depends(db)):
    sf = c.execute("select * from source_files where id=%s", (file_id,)).fetchone()
    if not sf:
        raise LookupError("file not found")
    audit(c, u["email"], "file.download", "source_file", str(file_id))
    return Response(get_storage().get_bytes(sf["storage_key"]), media_type=sf["mime"] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{sf["file_name"]}"'})


@app.get("/api/jobs/{job_id}")
def job(job_id: int, u=Depends(current_user), c=Depends(db)):
    j = jobs.get(c, job_id)
    if not j:
        raise LookupError("job not found")
    return j


# -- review queue ------------------------------------------------------------------

@app.get("/api/review")
def review(week: str | None = None, status: str = "open", kind: str | None = None, u=Depends(need("payroll.view_all")), c=Depends(db)):
    q, args = "select * from review_items where status=%s", [status]
    if week:
        q += " and (week=%s or week is null)"
        args.append(week)
    if kind:
        q += " and kind=%s"
        args.append(kind)
    return c.execute(q + " order by blocking desc, kind, created_at limit 1000", args).fetchall()


class Resolve(BaseModel):
    resolution: str
    status: str = "resolved"


@app.post("/api/review/{item_id}/resolve")
def resolve(item_id: uuid.UUID, body: Resolve, u=Depends(need("review.resolve")), c=Depends(db)):
    if body.status not in ("resolved", "dismissed", "open") or not body.resolution.strip():
        raise ValueError("Give a resolution note and a status of resolved, dismissed or open")
    before = c.execute("select status, resolution from review_items where id=%s", (item_id,)).fetchone()
    if not before:
        raise LookupError("review item not found")
    row = c.execute("update review_items set status=%s, resolution=%s, resolved_by=%s, resolved_at=now() where id=%s returning *",
                    (body.status, body.resolution, u["email"], item_id)).fetchone()
    audit(c, u["email"], "review.resolve", "review_item", str(item_id), before, {"status": body.status}, body.resolution)
    return row


# -- rules -------------------------------------------------------------------------

@app.get("/api/rules")
def rule_versions(u=Depends(need("payroll.view_all")), c=Depends(db)):
    return c.execute("select version,label,status,parent_version,change_note,created_by,created_at,activated_by,activated_at from rule_sets order by version desc").fetchall()


@app.get("/api/rules/{version}")
def rule_version(version: int, u=Depends(need("payroll.view_all")), c=Depends(db)):
    return rules_svc.by_version(c, version)


class Draft(BaseModel):
    label: str
    config: dict | None = None
    note: str = ""


@app.post("/api/rules/drafts")
def new_draft(body: Draft, u=Depends(need("rules.edit")), c=Depends(db)):
    act = rules_svc.active(c)
    return rules_svc.create(c, body.config or act["config"], body.label, u["email"], body.note, parent=act["version"])


class DraftUpdate(BaseModel):
    config: dict
    note: str = ""


@app.put("/api/rules/{version}")
def edit_draft(version: int, body: DraftUpdate, u=Depends(need("rules.edit")), c=Depends(db)):
    return rules_svc.update_draft(c, version, body.config, u["email"], body.note)


@app.get("/api/rules/{version}/diff")
def rule_diff(version: int, against: int | None = None, u=Depends(need("payroll.view_all")), c=Depends(db)):
    new = rules_svc.by_version(c, version)
    old = rules_svc.by_version(c, against) if against else (rules_svc.by_version(c, new["parent_version"]) if new["parent_version"] else rules_svc.active(c))
    return {"from": old["version"], "to": version, "changes": rules_svc.diff(old["config"], new["config"])}


@app.post("/api/rules/{version}/preview")
def rule_preview(version: int, week: str, u=Depends(need("rules.edit")), c=Depends(db)):
    return runs.preview_rules(c, week, version)


@app.post("/api/rules/{version}/activate")
def rule_activate(version: int, body: NoteBody, u=Depends(need("rules.activate")), c=Depends(db)):
    return rules_svc.activate(c, version, u["email"], body.note)


# -- people ------------------------------------------------------------------------

@app.get("/api/people")
def list_people(u=Depends(need("payroll.view_all")), c=Depends(db)):
    return people.roster(c)


class PersonBody(BaseModel):
    name: str
    kind: str = "agent"
    email: str | None = None
    email_verified: bool = False
    email_excluded: bool = False
    notes: str | None = None


@app.post("/api/people")
def save_person(body: PersonBody, u=Depends(need("people.manage")), c=Depends(db)):
    return people.upsert(c, body.name, u["email"], body.kind, body.email, body.email_verified, body.email_excluded, notes=body.notes)


class MergeBody(BaseModel):
    source: str
    target: str
    reason: str


@app.post("/api/people/merge")
def merge_people(body: MergeBody, u=Depends(need("people.merge")), c=Depends(db)):
    return {"entries": people.merge(c, body.source, body.target, u["email"], body.reason)}


# -- week adjustments --------------------------------------------------------------

@app.get("/api/weeks/{week}/adjustments")
def list_adjustments(week: str, include_void: bool = False, u=Depends(need("payroll.view_all")), c=Depends(db)):
    return adjustments.list_week(c, week, include_void)


class AdjBody(BaseModel):
    person: str
    kind: str
    authorized_by: str
    amount: str = "0"
    method: str = ""
    value: str = "0"
    debts: list[str] = []
    creditor: str = "EMG"
    label: str = ""
    note: str = ""
    external_ref: str | None = None


@app.post("/api/weeks/{week}/adjustments")
def add_adjustment(week: str, body: AdjBody, u=Depends(need("adjustments.manage")), c=Depends(db)):
    return adjustments.add(c, week, body.person, body.kind, u["email"], body.authorized_by, body.amount, body.method, body.value,
                           body.debts, body.creditor, body.label, body.note, body.external_ref)


@app.post("/api/adjustments/{adj_id}/void")
def void_adjustment(adj_id: uuid.UUID, body: NoteBody, u=Depends(need("adjustments.manage")), c=Depends(db)):
    adjustments.void(c, adj_id, u["email"], body.note)
    return {"ok": True}


# -- payroll runs ------------------------------------------------------------------

@app.post("/api/weeks/{week}/calculate")
def calculate(week: str, rule_version: int | None = None, u=Depends(need("payroll.calculate")), c=Depends(db)):
    r = runs.calculate(c, week, u["email"], rule_version)
    return runs.run_view(c, r["id"])


@app.get("/api/weeks/{week}/runs")
def week_runs(week: str, u=Depends(need("payroll.view_all")), c=Depends(db)):
    return c.execute("select id, revision, status, rule_set_version, totals, supersedes, correction_reason, created_by, created_at, finalized_by, finalized_at"
                     " from payroll_runs where week=%s order by revision desc", (week,)).fetchall()


@app.get("/api/runs/{run_id}")
def get_run(run_id: uuid.UUID, u=Depends(current_user), c=Depends(db)):
    v = runs.run_view(c, run_id)
    vis = auth.visible_people(c, u)
    if vis is not None:
        v = {k: v[k] for k in ("id", "week", "revision", "status")} | {"people": [p for p in v["people"] if p["payee"] in vis]}
    return v


@app.get("/api/runs/{run_id}/people/{payee}")
def run_person(run_id: uuid.UUID, payee: str, u=Depends(current_user), c=Depends(db)):
    _see(c, u, payee)
    return runs.person_detail(c, run_id, payee)


@app.get("/api/statements")
def statements(person: str | None = None, u=Depends(current_user), c=Depends(db)):
    """Finalized weeks for one person (agents default to themselves)."""
    person = person or u.get("person_name")
    if not person:
        raise ValueError("Choose a person")
    _see(c, u, person)
    return c.execute("select r.id run_id, r.week, r.revision, rp.final_check, rp.status from run_people rp join payroll_runs r on r.id=rp.run_id"
                     " where rp.payee=%s and r.status='finalized' order by r.week desc", (person,)).fetchall()


@app.get("/api/runs/{run_id}/lines")
def run_lines(run_id: uuid.UUID, vendor: str | None = None, enrollment_id: str | None = None, u=Depends(need("payroll.view_all")), c=Depends(db)):
    q, args = "select * from run_lines where run_id=%s", [run_id]
    if vendor:
        q += " and vendor=%s"
        args.append(vendor)
    if enrollment_id:
        q += " and enrollment_id=%s"
        args.append(enrollment_id)
    return c.execute(q + " order by payee, vendor, enrollment_id limit 5000", args).fetchall()


@app.get("/api/runs/{run_id}/items")
def run_items(run_id: uuid.UUID, kind: str | None = None, u=Depends(need("payroll.view_all")), c=Depends(db)):
    q = "select * from run_items where run_id=%s" + (" and kind=%s" if kind else "") + " order by kind, payee, enrollment_id"
    return c.execute(q, (run_id, kind) if kind else (run_id,)).fetchall()


@app.get("/api/runs/{a}/compare/{b}")
def compare_runs(a: uuid.UUID, b: uuid.UUID, u=Depends(need("payroll.view_all")), c=Depends(db)):
    return runs.compare(c, a, b)


class FinalizeBody(BaseModel):
    finalize_key: str


@app.post("/api/runs/{run_id}/finalize")
def finalize(run_id: uuid.UUID, body: FinalizeBody, u=Depends(need("payroll.finalize")), c=Depends(db)):
    run = c.execute("select supersedes from payroll_runs where id=%s", (run_id,)).fetchone()
    if run and run["supersedes"] and not auth.can(u, "payroll.correct"):
        raise HTTPException(403, "Finalizing a correction needs an owner or admin")
    return runs.finalize(c, run_id, u["email"], body.finalize_key)


class CorrectionBody(BaseModel):
    reason: str
    rule_version: int | None = None


@app.post("/api/weeks/{week}/corrections")
def correction(week: str, body: CorrectionBody, u=Depends(need("payroll.correct")), c=Depends(db)):
    r = runs.start_correction(c, week, u["email"], body.reason, body.rule_version)
    return runs.run_view(c, r["id"])


# -- payments and ledgers ----------------------------------------------------------

@app.get("/api/runs/{run_id}/payments")
def run_payments(run_id: uuid.UUID, u=Depends(need("payments.manage")), c=Depends(db)):
    return payments.for_run(c, run_id)


class PayState(BaseModel):
    state: str
    provider_ref: str | None = None
    note: str | None = None


@app.post("/api/payments/{payment_id}/state")
def payment_state(payment_id: uuid.UUID, body: PayState, u=Depends(need("payments.manage")), c=Depends(db)):
    return payments.set_state(c, payment_id, body.state, u["email"], body.provider_ref, body.note)


@app.post("/api/runs/{run_id}/payments/state")
def payments_bulk(run_id: uuid.UUID, body: PayState, u=Depends(need("payments.manage")), c=Depends(db)):
    return {"updated": payments.bulk(c, run_id, body.state, u["email"], body.provider_ref)}


@app.get("/api/ledger")
def ledger_balances(person: str | None = None, u=Depends(current_user), c=Depends(db)):
    if person:
        _see(c, u, person)
    elif not auth.can(u, "ledger.view_all"):
        raise HTTPException(403, "Choose a person")
    return ledger.balances(c, person)


@app.get("/api/ledger/{person}/history")
def ledger_history(person: str, u=Depends(current_user), c=Depends(db)):
    _see(c, u, person)
    return ledger.history(c, person)


# -- reports -----------------------------------------------------------------------

class BuildBody(BaseModel):
    kinds: list[str] | None = None
    background: bool = False


@app.post("/api/runs/{run_id}/reports")
def build_reports(run_id: uuid.UUID, body: BuildBody, u=Depends(need("reports.build")), c=Depends(db)):
    kinds = tuple(body.kinds or reports.ALL_KINDS)
    bad = [k for k in kinds if k not in reports.ALL_KINDS]
    if bad:
        raise ValueError(f"Unknown report kinds {bad}")
    if body.background:
        return {"job_id": jobs.enqueue(c, "reports.build", {"run_id": str(run_id), "kinds": list(kinds)}, u["email"])["id"]}
    return reports.build(c, get_storage(), run_id, u["email"], kinds)


@app.get("/api/runs/{run_id}/reports")
def run_reports(run_id: uuid.UUID, u=Depends(current_user), c=Depends(db)):
    rows = reports.list_current(c, run_id)
    vis = auth.visible_people(c, u)
    return rows if vis is None else [r for r in rows if r["kind"] == "agent_pdf" and r["person"] in vis]


@app.get("/api/weeks/{week}/reports")
def week_reports(week: str, u=Depends(need("reports.build")), c=Depends(db)):
    return reports.history(c, week)


@app.get("/api/reports/{version_id}/download")
def download_report(version_id: uuid.UUID, u=Depends(current_user), c=Depends(db)):
    r = c.execute("select * from report_versions where id=%s", (version_id,)).fetchone()
    if not r:
        raise LookupError("report not found")
    if not auth.can(u, "reports.build"):
        if r["kind"] != "agent_pdf":
            raise HTTPException(403, "Not allowed")
        _see(c, u, r["person"])
    audit(c, u["email"], "report.download", "report", str(version_id), None, {"file": r["file_name"]})
    media = {"pdf": "application/pdf", "csv": "text/csv", "zip": "application/zip",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(r["file_name"].rsplit(".", 1)[-1], "application/octet-stream")
    return Response(get_storage().get_bytes(r["storage_key"]), media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{r["file_name"]}"'})


# -- email ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/email")
def email_preview(run_id: uuid.UUID, u=Depends(need("email.send")), c=Depends(db)):
    return email_svc.preview(c, run_id)


class AuthorizeBody(BaseModel):
    people: list[str]
    confirm: str
    allow_preliminary: bool = False


@app.post("/api/runs/{run_id}/email/authorize")
def email_authorize(run_id: uuid.UUID, body: AuthorizeBody, u=Depends(need("email.send")), c=Depends(db)):
    if body.allow_preliminary and not auth.can(u, "payroll.correct"):
        raise HTTPException(403, "Only an owner or admin can send preliminary statements")
    return email_svc.authorize(c, run_id, body.people, u["email"], body.confirm, body.allow_preliminary)


@app.get("/api/runs/{run_id}/email/sends")
def email_sends(run_id: uuid.UUID, u=Depends(need("email.send")), c=Depends(db)):
    return email_svc.sends(c, run_id)


class TemplateBody(BaseModel):
    subject: str
    body: str


@app.get("/api/email/template")
def get_template(u=Depends(need("email.send")), c=Depends(db)):
    return email_svc.template(c)


@app.put("/api/email/template")
def put_template(body: TemplateBody, u=Depends(need("email.send")), c=Depends(db)):
    return email_svc.save_template(c, body.subject, body.body, u["email"])


class ExcludeBody(BaseModel):
    person: str
    reason: str = ""
    excluded: bool = True


@app.post("/api/email/exclusions")
def set_exclusion(body: ExcludeBody, u=Depends(need("email.send")), c=Depends(db)):
    (email_svc.exclude(c, body.person, body.reason, u["email"]) if body.excluded else email_svc.include(c, body.person, u["email"]))
    return {"ok": True}


class EventBody(BaseModel):
    message_id: str
    status: str
    detail: str = ""


@app.post("/api/email/events")
def email_event(body: EventBody, request: Request, authorization: str | None = Header(default=None), x_webhook_secret: str | None = Header(default=None), c=Depends(db)):
    """Delivery/bounce events: from the provider (shared secret) or from a signed-in payroll user (manual reconciliation)."""
    secret = os.environ.get("EMAIL_WEBHOOK_SECRET")
    actor = "provider"
    if not (secret and x_webhook_secret and secrets_equal(secret, x_webhook_secret)):
        tok = _token(request, authorization)
        u = tok and auth.user_for_token(c, tok)
        if not u or not auth.can(u, "email.send"):
            raise HTTPException(401, "Not allowed")
        actor = u["email"]
    row = email_svc.record_event(c, body.message_id, body.status, body.detail, actor)
    if not row:
        raise LookupError("No send with that message id")
    return row


def secrets_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


# -- audit -------------------------------------------------------------------------

@app.get("/api/audit")
def audit_log(entity: str | None = None, entity_id: str | None = None, limit: int = 200, u=Depends(need("audit.view")), c=Depends(db)):
    q, args = "select * from audit_log where true", []
    if entity:
        q += " and entity=%s"
        args.append(entity)
    if entity_id:
        q += " and entity_id=%s"
        args.append(entity_id)
    return c.execute(q + " order by id desc limit %s", (*args, min(limit, 1000))).fetchall()


@app.get("/api/enrollments/{vendor}/{enrollment_id}")
def enrollment(vendor: str, enrollment_id: str, u=Depends(need("payroll.view_all")), c=Depends(db)):
    """Drill-down: registry row, status history with source files, every money line and payout."""
    e = c.execute("select * from enrollments where vendor=%s and enrollment_id=%s", (vendor, enrollment_id)).fetchone()
    if not e:
        raise LookupError("enrollment not found")
    return {
        "enrollment": e,
        "status_history": c.execute("select * from enrollment_status_history where enrollment_pk=%s order by observed_at, id", (e["id"],)).fetchall(),
        "invoice_lines": c.execute("select l.*, r.payload->>'source_file' source_file, r.payload->>'source_row' source_row, r.batch_id"
                                   " from invoice_lines l join import_rows r on r.id=l.import_row_id where l.vendor=%s and l.enrollment_id=%s order by l.id",
                                   (vendor, enrollment_id)).fetchall(),
        "payouts": c.execute("select p.*, pr.rule_set_version from payout_evidence p left join payroll_runs pr on pr.id=p.run_id"
                             " where p.vendor=%s and p.enrollment_id=%s order by p.id", (vendor, enrollment_id)).fetchall(),
    }


# -- sales map ---------------------------------------------------------------------

class MapFilters(BaseModel):
    vendors: list[str] | None = None
    categories: list[str] | None = None
    sellers: list[str] | None = None
    date_from: str | None = None
    date_to: str | None = None
    project: str | None = None
    territory: str | None = None
    out_of_territory: bool | None = None
    q: str | None = None
    min_confidence: float | None = None
    bbox: list[float] | None = None


def _filters(f: MapFilters) -> dict:
    return {k: v for k, v in f.model_dump().items() if v not in (None, [], "")}


@app.post("/api/map/points")
def map_points(f: MapFilters, u=Depends(need("map.view")), c=Depends(db)):
    res = geo.points(c, u, _filters(f))
    return res | {"totals": geo.totals(res["points"])}


@app.post("/api/map/export")
def map_export(f: MapFilters, u=Depends(need("map.view")), c=Depends(db)):
    res = geo.points(c, u, _filters(f))
    audit(c, u["email"], "map.export", "map", None, None, {"filters": _filters(f), "rows": len(res["points"])})
    return Response(geo.export_csv(res), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="EMG_Sales_Map.csv"'})


@app.get("/api/map/geocode/queue")
def geocode_queue(u=Depends(need("map.view_all")), c=Depends(db)):
    return {"rows": geo.geocode_queue(c),
            "summary": c.execute("select coalesce(geocode_status, case when service_address is null then 'no_address' else 'not_geocoded' end) s, count(*) n"
                                 " from enrollments group by 1 order by 2 desc").fetchall()}


@app.post("/api/map/geocode/run")
def geocode_run(u=Depends(need("territories.manage")), c=Depends(db)):
    return jobs.enqueue(c, "geocode.run", {}, u["email"])


class Location(BaseModel):
    vendor: str
    enrollment_id: str
    lat: float
    lng: float


@app.post("/api/map/location")
def map_location(body: Location, u=Depends(need("territories.manage")), c=Depends(db)):
    return geo.set_location(c, body.vendor, body.enrollment_id, body.lat, body.lng, u["email"])


@app.post("/api/map/enrich")
async def enrich_upload(file: UploadFile = File(...), vendor: str | None = Form(None), kind: str | None = Form(None),
                        u=Depends(need("territories.manage")), c=Depends(db)):
    sf = imports.store_upload(c, get_storage(), await file.read(), file.filename or "addresses.xlsx", u["email"])
    b = geo.enrich_preview(c, get_storage(), sf["id"], u["email"], vendor or None, kind or None)
    return b


@app.get("/api/map/enrich/{batch_id}")
def enrich_batch(batch_id: uuid.UUID, u=Depends(need("territories.manage")), c=Depends(db)):
    b = c.execute("select * from enrichment_batches where id=%s", (batch_id,)).fetchone()
    if not b:
        raise HTTPException(404, "Unknown batch")
    return b


@app.post("/api/map/enrich/{batch_id}/apply")
def enrich_apply(batch_id: uuid.UUID, u=Depends(need("territories.manage")), c=Depends(db)):
    return geo.enrich_apply(c, batch_id, u["email"])


@app.get("/api/territories")
def list_territories(geometry: bool = True, u=Depends(need("map.view")), c=Depends(db)):
    return {"territories": geo.territories(c, geometry), "capacity": geo.capacity(c)}


@app.post("/api/territories/preview")
async def territories_preview(file: UploadFile = File(...), u=Depends(need("territories.manage"))):
    return {"features": geo.territory_preview(file.filename or "", await file.read())}


@app.post("/api/territories")
async def territories_import(file: UploadFile = File(...), kind: str = Form("territory"), vendor: str | None = Form(None),
                             u=Depends(need("territories.manage")), c=Depends(db)):
    return {"created": geo.territory_import(c, get_storage(), file.filename or "", await file.read(), kind, vendor or None, u["email"])}


class TerritoryPatch(BaseModel):
    name: str | None = None
    active: bool | None = None
    capacity_kwh: Decimal | None = None
    capacity_enabled: bool | None = None
    vendor: str | None = None


@app.patch("/api/territories/{tid}")
def territory_patch(tid: uuid.UUID, body: TerritoryPatch, u=Depends(need("territories.manage")), c=Depends(db)):
    return geo.territory_update(c, tid, u["email"], **body.model_dump())


class Allocation(BaseModel):
    vendor: str
    enrollment_id: str
    kwh: Decimal | None = None
    note: str = ""


@app.get("/api/territories/{tid}/allocations")
def territory_allocations(tid: uuid.UUID, u=Depends(need("map.view_all")), c=Depends(db)):
    return c.execute("select * from capacity_allocations where territory_id=%s order by released_at nulls first, allocated_at desc", (tid,)).fetchall()


@app.post("/api/territories/{tid}/allocations")
def territory_allocate(tid: uuid.UUID, body: Allocation, u=Depends(need("territories.manage")), c=Depends(db)):
    return geo.allocate(c, tid, body.vendor, body.enrollment_id, u["email"], body.kwh, body.note)


@app.post("/api/allocations/{aid}/release")
def allocation_release(aid: uuid.UUID, u=Depends(need("territories.manage")), c=Depends(db)):
    return geo.release(c, aid, u["email"])


# -- leaderboards --------------------------------------------------------------------

def _board_args(cadence: str, day: str | None, metric: str, group: str, vendors: str | None, top: int, manager: str | None):
    from datetime import date as _d

    return dict(cadence=cadence, anchor=_d.fromisoformat(day) if day else None, metric=metric, group=group,
                vendors=[v for v in (vendors or "").split(",") if v] or None, top=max(1, min(top, 100)), manager=manager or None)


@app.get("/api/leaderboards")
def leaderboard(cadence: str = "daily", day: str | None = None, metric: str = "written", group: str = "agent", vendors: str | None = None,
                top: int = 10, manager: str | None = None, u=Depends(need("leaderboards.view")), c=Depends(db)):
    return leaderboards.compute(c, u, **_board_args(cadence, day, metric, group, vendors, top, manager))


@app.get("/api/leaderboards/export.{ext}")
def leaderboard_export(ext: str, cadence: str = "daily", day: str | None = None, metric: str = "written", group: str = "agent",
                       vendors: str | None = None, top: int = 10, manager: str | None = None, u=Depends(need("leaderboards.view")), c=Depends(db)):
    if ext not in leaderboards.EXPORTS:
        raise HTTPException(404, "Export as csv, pdf or png")
    res = leaderboards.compute(c, u, **_board_args(cadence, day, metric, group, vendors, top, manager))
    fn, mime = leaderboards.EXPORTS[ext]
    return Response(fn(res), media_type=mime, headers={"Content-Disposition": f'attachment; filename="{leaderboards.file_name(res, ext)}"'})


@app.get("/api/leaderboards/drilldown")
def leaderboard_drilldown(name: str, cadence: str = "daily", day: str | None = None, metric: str = "written", group: str = "agent",
                          vendors: str | None = None, u=Depends(need("leaderboards.view")), c=Depends(db)):
    a = _board_args(cadence, day, metric, group, vendors, 10, None)
    return {"rows": leaderboards.drilldown(c, u, name, cadence, a["anchor"] or leaderboards.today(leaderboards.timezone(c)), metric, group, a["vendors"])}


@app.get("/api/leaderboards/settings")
def leaderboard_settings(u=Depends(need("leaderboards.publish")), c=Depends(db)):
    return {"timezone": leaderboards.timezone(c), "destinations": [{**d, "url": f"{urlparse(d['url']).netloc}/..." if d.get("url") else None} for d in leaderboards.destinations(c)],
            "schedules": c.execute("select * from leaderboard_schedules order by created_at").fetchall(),
            "runs": c.execute("select id, schedule_id, cadence, period_start, period_end, files, published, publish_result, created_at from leaderboard_runs"
                              " order by created_at desc limit 50").fetchall()}


class TzBody(BaseModel):
    timezone: str


@app.put("/api/leaderboards/timezone")
def leaderboard_tz(body: TzBody, u=Depends(need("leaderboards.publish")), c=Depends(db)):
    from zoneinfo import ZoneInfo

    ZoneInfo(body.timezone)
    c.execute("insert into settings(key,value,updated_by) values ('timezone',%s,%s) on conflict (key) do update set value=excluded.value, updated_by=excluded.updated_by,"
              " updated_at=now()", (json.dumps(body.timezone), u["email"]))
    audit(c, u["email"], "settings.timezone", "settings", "timezone", None, body.timezone)
    return {"timezone": body.timezone}


@app.put("/api/leaderboards/destinations")
def leaderboard_destination(body: dict, u=Depends(need("leaderboards.publish")), c=Depends(db)):
    """Add or replace one destination by name (an empty URL keeps the stored one)."""
    return [{**d, "url": bool(d.get("url"))} for d in leaderboards.upsert_destination(c, body, u["email"])]


@app.post("/api/leaderboards/schedules")
def schedule_create(body: dict, u=Depends(need("leaderboards.publish")), c=Depends(db)):
    return leaderboards.save_schedule(c, body, u["email"])


@app.put("/api/leaderboards/schedules/{sid}")
def schedule_update(sid: uuid.UUID, body: dict, u=Depends(need("leaderboards.publish")), c=Depends(db)):
    return leaderboards.save_schedule(c, body, u["email"], sid)


@app.post("/api/leaderboards/schedules/{sid}/run")
def schedule_run(sid: uuid.UUID, day: str | None = None, u=Depends(need("leaderboards.publish")), c=Depends(db)):
    """Generate now for the period containing ``day`` (default: the last complete period)."""
    from datetime import date as _d, timedelta

    s = c.execute("select * from leaderboard_schedules where id=%s", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "Unknown schedule")
    t = leaderboards.today(s["timezone"])
    anchor = _d.fromisoformat(day) if day else (t - timedelta(days=1) if s["cadence"] == "daily" else t - timedelta(days=7))
    start = leaderboards.period(s["cadence"], anchor)[0]
    r = leaderboards.run_schedule(c, get_storage(), s, start, u["email"])
    if not r:
        raise HTTPException(409, "This period was already generated for this schedule")
    return r


@app.get("/api/leaderboards/runs/{run_id}/files/{idx}")
def leaderboard_file(run_id: uuid.UUID, idx: int, u=Depends(need("leaderboards.view")), c=Depends(db)):
    r = c.execute("select files from leaderboard_runs where id=%s", (run_id,)).fetchone()
    if not r or not (0 <= idx < len(r["files"])):
        raise HTTPException(404, "Unknown file")
    f = r["files"][idx]
    return Response(get_storage().get_bytes(f["storage_key"]), media_type=f["mime"], headers={"Content-Disposition": f'attachment; filename="{f["file_name"]}"'})


# -- vendor connectors ------------------------------------------------------------------

@app.get("/api/connectors")
def connectors_list(u=Depends(need("connectors.manage")), c=Depends(db)):
    return {"connectors": connectors.list_(c), "targets": connectors.TARGETS, "browser_ready": connectors.chromium_ok(),
            "key_set": bool(os.environ.get("CONNECTOR_SECRET_KEY"))}


@app.post("/api/connectors")
def connectors_create(body: dict, u=Depends(need("connectors.manage")), c=Depends(db)):
    return connectors.save(c, body, u["email"])


@app.put("/api/connectors/{cid}")
def connectors_update(cid: uuid.UUID, body: dict, u=Depends(need("connectors.manage")), c=Depends(db)):
    return connectors.save(c, body, u["email"], cid)


@app.put("/api/connectors/{cid}/credentials")
def connectors_credentials(cid: uuid.UUID, body: dict, u=Depends(need("connectors.manage")), c=Depends(db)):
    """Write-only: credentials are encrypted and never returned."""
    return connectors.set_credentials(c, cid, body, u["email"])


@app.post("/api/connectors/{cid}/run")
def connectors_run(cid: uuid.UUID, u=Depends(need("connectors.manage")), c=Depends(db)):
    return jobs.enqueue(c, "connector.run", {"connector_id": str(cid), "trigger": "manual"}, u["email"])


@app.get("/api/connectors/{cid}/runs")
def connectors_runs(cid: uuid.UUID, u=Depends(need("connectors.manage")), c=Depends(db)):
    return connectors.runs(c, cid)


@app.get("/api/connector-runs/{rid}/screenshot")
def connectors_screenshot(rid: uuid.UUID, u=Depends(need("connectors.manage")), c=Depends(db)):
    r = c.execute("select screenshot_key from connector_runs where id=%s", (rid,)).fetchone()
    if not r or not r["screenshot_key"]:
        raise HTTPException(404, "No screenshot")
    return Response(get_storage().get_bytes(r["screenshot_key"]), media_type="image/png")


@app.post("/api/connectors/test-mapping")
async def connectors_test_mapping(file: UploadFile = File(...), mapping: str = Form(...), u=Depends(need("connectors.manage"))):
    """Upload one export by hand to check the column mapping before the first automated run."""
    return connectors.test_mapping(file.filename or "export.csv", await file.read(), json.loads(mapping))


@app.get("/api/health")
def health(c=Depends(db)):
    c.execute("select 1")
    return {"ok": True}

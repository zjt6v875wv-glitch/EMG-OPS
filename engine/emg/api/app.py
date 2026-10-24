"""HTTP API used by the Next.js app. Money is always serialized as strings.

Run: uvicorn emg.api.app:app --reload
"""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import Any

from fastapi import encoders as fastapi_encoders
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..db.conn import _default, audit, connect
from ..services import adjustments, auth, imports, jobs, ledger, payments, people, runs
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


@app.get("/api/health")
def health(c=Depends(db)):
    c.execute("select 1")
    return {"ok": True}

"""Build and version report artifacts for a payroll run.

Every artifact is stored content-addressed and recorded in report_versions.
Rebuilding replaces the *current* version only when the bytes changed; old
versions stay downloadable. Reports of an earlier final revision are kept
when a correction is finalized (they belong to that revision's run).
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

from ..db.conn import J, audit
from ..importers import spark
from ..money import D
from ..reports import naming, pdf, workbook
from ..reports.data import ReportData, load
from ..rules.model import load_ruleset

ALL_KINDS = ("agent_pdf", "combined_pdf", "reports_zip", "master_xlsx", "payable_csv", "payable_only_csv", "paychex_csv",
             "aeg_workbook", "aeg_invoice_pdf", "aeg_invoice_csv", "package_zip")


def _save(c, storage, rd: ReportData, kind: str, file_name: str, data: bytes, actor: str, person: str | None = None) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    cur = c.execute("select * from report_versions where run_id=%s and coalesce(person,'')=%s and kind=%s and current",
                    (rd.run["id"], person or "", kind)).fetchone()
    if cur and cur["sha256"] == sha and cur["file_name"] == file_name:
        return cur
    key, _ = storage.put_bytes(data, f"reports/{rd.week}/rev{rd.run['revision']}", file_name)
    if cur:
        c.execute("update report_versions set current=false where id=%s", (cur["id"],))
    row = c.execute(
        "insert into report_versions(run_id,week,person,kind,revision,file_name,storage_key,sha256,size_bytes,created_by,label)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (rd.run["id"], rd.week, person, kind, rd.run["revision"], file_name, key, sha, len(data), actor, rd.label),
    ).fetchone()
    if cur:
        c.execute("update report_versions set replaced_by=%s where id=%s", (row["id"], cur["id"]))
    return row


def _company_lines(rd: ReportData) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for l in rd.lines:
        if l["activity"] == "Company Payable":
            out.setdefault(l["payee"], []).append(l)
    return out


def _spark_source(c, rd: ReportData):
    return next((s for s in rd.sources if s["importer"] == "spark"), None)


def aeg_workbook(c, storage, rd: ReportData, payee: str, work: Path) -> tuple[bytes, dict] | None:
    """The original Spark workbook, all columns and formatting kept, filtered to the
    routed agents' rows, with payable rows repriced at the routing rate."""
    src = _spark_source(c, rd)
    rs = load_ruleset(rd.rules)
    routes = [r for r in rs.routing if r.payee == payee and r.vendor == "Spark" and r.live(rd.week)]
    if not src or not routes:
        return None
    sellers = set().union(*(r.sellers for r in routes))
    raw = {r["payload"]["raw_rep"] for r in c.execute("select payload from import_rows where batch_id=%s", (src["id"],))}
    keep = {x for x in raw if rs.canon(x, "Spark") in sellers}
    local = storage.copy_to(src["storage_key"], work / f"{src['sha256']}.xlsx")
    dest = work / f"{payee}_repriced.xlsx"
    info = spark.write_repriced_copy(local, dest, keep, routes[0].rate)
    expected = sum((D(l["amount"]) for l in rd.lines if l["payee"] == payee and l["vendor"] == "Spark"), Decimal(0))
    if D(info["total"]) != expected:
        raise AssertionError(f"Repriced {payee} workbook totals {info['total']} but payroll routed {expected}")
    return dest.read_bytes(), info


def build(c, storage, run_id, actor: str, kinds: tuple[str, ...] = ALL_KINDS, work_dir: Path | None = None,
          include_sources_in_package: bool = True) -> dict:
    rd = load(c, run_id)
    work = work_dir or Path("/tmp/emg-reports") / str(run_id)
    work.mkdir(parents=True, exist_ok=True)
    wk, pd, label = rd.week_no, rd.pay_date, rd.label
    made: dict[str, list[dict]] = {}
    pdfs: list[tuple[str, bytes]] = []

    def keep(kind, name, data, person=None):
        made.setdefault(kind, []).append(_save(c, storage, rd, kind, name, data, actor, person))
        return data

    if any(k in kinds for k in ("agent_pdf", "combined_pdf", "reports_zip", "package_zip")):
        for name in rd.report_people():
            data = pdf.statement_pdf(rd, name)
            fn = naming.agent_pdf(name, wk, pd, label)
            pdfs.append((fn, data))
            if "agent_pdf" in kinds:
                keep("agent_pdf", fn, data, name)
    if "agent_pdf" in kinds:
        # People who no longer get a statement in this revision lose their "current" report.
        c.execute("update report_versions set current=false where run_id=%s and kind='agent_pdf' and current and not (person = any(%s))",
                  (rd.run["id"], rd.report_people()))
    if "combined_pdf" in kinds and pdfs:
        keep("combined_pdf", naming.package("Reports", wk, pd, label, "pdf"), pdf.combine([d for _, d in pdfs]))
    if "reports_zip" in kinds and pdfs:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fn, d in pdfs:
                z.writestr(fn, d)
        keep("reports_zip", naming.package("Individual_Reports", wk, pd, label, "zip"), buf.getvalue())
    files: list[tuple[str, bytes]] = []
    if "master_xlsx" in kinds or "package_zip" in kinds:
        files.append((naming.package("Payroll", wk, pd, label, "xlsx"), workbook.master_workbook(rd)))
        if "master_xlsx" in kinds:
            keep("master_xlsx", *files[-1])
    for kind, fn_kind, fn in (("payable_csv", "Payable_Checks", workbook.payable_csv), ("payable_only_csv", "Payable_Checks_Only", workbook.payable_only_csv),
                              ("paychex_csv", "Paychex_Import", workbook.paychex_csv)):
        if kind in kinds or "package_zip" in kinds:
            files.append((naming.package(fn_kind, wk, pd, label, "csv"), fn(rd)))
            if kind in kinds:
                keep(kind, *files[-1])
    for payee, lines in _company_lines(rd).items():
        inv = f"EMG-{naming.slug(payee).upper()}-{pd.strftime('%Y%m%d')}" + (f"-R{rd.run['revision']}" if rd.run.get("supersedes") else "")
        src = _spark_source(c, rd)
        source_invoice = ""
        if src:
            first = c.execute("select payload->>'invoice_number' n from import_rows where batch_id=%s and payload->>'invoice_number' <> '' limit 1", (src["id"],)).fetchone()
            source_invoice = first["n"] if first else ""
        if "aeg_invoice_pdf" in kinds or "package_zip" in kinds:
            files.append((f"{naming.slug(payee)}_Invoice_Week{wk}_{pd.isoformat()}.pdf", pdf.company_invoice_pdf(rd, payee, inv, source_invoice)))
            if "aeg_invoice_pdf" in kinds:
                keep("aeg_invoice_pdf", *files[-1], person=payee)
        if "aeg_invoice_csv" in kinds or "package_zip" in kinds:
            files.append((f"{naming.slug(payee)}_Invoice_Week{wk}_{pd.isoformat()}.csv", workbook.company_invoice_csv(rd, payee, inv, source_invoice)))
            if "aeg_invoice_csv" in kinds:
                keep("aeg_invoice_csv", *files[-1], person=payee)
        if "aeg_workbook" in kinds or "package_zip" in kinds:
            res = aeg_workbook(c, storage, rd, payee, work)
            if res:
                files.append((f"{naming.slug(payee)}_Spark_Repriced_Week{wk}_{pd.isoformat()}.xlsx", res[0]))
                if "aeg_workbook" in kinds:
                    keep("aeg_workbook", *files[-1], person=payee)

    if "package_zip" in kinds:
        manifest = {"week": rd.week, "pay_date": pd.isoformat(), "label": label, "revision": rd.run["revision"], "run_id": str(rd.run["id"]),
                    "rule_set_version": rd.run["rule_set_version"], "totals": rd.run["totals"], "files": []}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fn, d in files + [(f"individual_reports/{fn}", d) for fn, d in pdfs]:
                z.writestr(fn, d)
                manifest["files"].append({"name": fn, "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)})
            if pdfs:
                comb = pdf.combine([d for _, d in pdfs])
                fn = naming.package("Reports", wk, pd, label, "pdf")
                z.writestr(fn, comb)
                manifest["files"].append({"name": fn, "sha256": hashlib.sha256(comb).hexdigest(), "bytes": len(comb)})
            if include_sources_in_package:
                for s in rd.sources:
                    d = storage.get_bytes(s["storage_key"])
                    z.writestr(f"sources/{s['file_name']}", d)
                    manifest["files"].append({"name": f"sources/{s['file_name']}", "sha256": s["sha256"], "bytes": len(d)})
            z.writestr("rules_snapshot.json", json.dumps(rd.rules, indent=1, default=str))
            z.writestr("manifest.json", json.dumps(manifest, indent=1, default=str))
        keep("package_zip", naming.package("Package", wk, pd, label, "zip"), buf.getvalue())
    audit(c, actor, "reports.build", "payroll_run", str(run_id), None, {k: len(v) for k, v in made.items()})
    return {"run_id": str(run_id), "label": label, "built": {k: [r["file_name"] for r in v] for k, v in made.items()}}


def list_current(c, run_id, kind: str | None = None) -> list[dict]:
    q = "select id, person, kind, revision, label, file_name, sha256, size_bytes, created_at, created_by from report_versions where run_id=%s and current"
    return c.execute(q + (" and kind=%s" if kind else "") + " order by kind, upper(coalesce(person,''))", (run_id, kind) if kind else (run_id,)).fetchall()


def history(c, week: str) -> list[dict]:
    """Every version ever built for a week, including earlier final revisions."""
    return c.execute("select id, run_id, person, kind, revision, label, file_name, sha256, current, created_at from report_versions"
                     " where week=%s order by revision desc, kind, person, created_at desc", (week,)).fetchall()

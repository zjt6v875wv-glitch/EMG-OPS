"""Upload -> store original -> preview batch -> commit.

Originals are stored unchanged (content-addressed). A preview parses the file
into ``import_rows`` and shows counts, mapping, errors, exclusions and
duplicates before anything touches payroll. Commit records each money line
once in ``invoice_lines`` (a second import of the same line is refused and
flagged), upserts the enrollment registry and appends status history.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from ..db.conn import J, audit
from ..importers import arcadia_perch, generic, nexamp, spark
from ..importers.base import ImportResult
from ..model import SaleRow
from ..weeks import parse_week

MONEY_TYPES = ("payable", "chargeback", "adjustment", "company_only", "review", "pending")
IMPORTERS = ("arcadia_perch", "nexamp_commissions", "nexamp_sales_report", "spark", "generic")


def detect(path: str | Path) -> str | None:
    p = Path(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        return None
    for name, fn in (("spark", spark.detect), ("nexamp_commissions", nexamp.detect_commissions),
                     ("nexamp_sales_report", nexamp.detect_sales_report), ("arcadia_perch", arcadia_perch.detect)):
        try:
            if fn(p):
                return name
        except Exception:  # a detector failing on a foreign file just means "not mine"
            continue
    return None


def transaction_key(r: SaleRow) -> str:
    """Identity of one vendor money line. Vendor transaction IDs win; otherwise
    enrollment + invoice + sheet + stage + amount (an exact re-sent line)."""
    if r.transaction_id:
        return f"tx:{r.transaction_id}"
    inv = r.invoice_number or (r.invoice_date.isoformat() if r.invoice_date else "")
    return f"{r.enrollment_id}|{inv}|{r.source_sheet}|{r.stage}|{r.vendor_amount.normalize() if r.vendor_amount else 0}"


def store_upload(c, storage, data: bytes, file_name: str, actor: str) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    row = c.execute("select * from source_files where sha256=%s", (sha,)).fetchone()
    if row:
        return row | {"already_stored": True}
    key, _ = storage.put_bytes(data, "uploads", file_name)
    row = c.execute(
        "insert into source_files(sha256,file_name,size_bytes,mime,storage_key,uploaded_by) values (%s,%s,%s,%s,%s,%s) returning *",
        (sha, file_name, len(data), mimetypes.guess_type(file_name)[0], key, actor),
    ).fetchone()
    audit(c, actor, "file.upload", "source_file", str(row["id"]), None, {"file_name": file_name, "sha256": sha})
    return row | {"already_stored": False}


def _sales_index(c, vendor: str) -> dict[str, SaleRow]:
    out = {}
    for e in c.execute("select * from enrollments where vendor=%s", (vendor,)):
        out[e["enrollment_id"]] = SaleRow(vendor=vendor, enrollment_id=e["enrollment_id"], raw_rep=e["raw_rep"] or "",
                                          product=e["product"] or "", kwh=e["kwh"] or 0, sale_date=e["sale_date"],
                                          approval_date=e["approval_date"])
    return out


def parse(c, importer: str, path: Path, week: str | None, options: dict | None = None) -> ImportResult:
    year = parse_week(week)[0] if week else None
    if importer == "arcadia_perch":
        return arcadia_perch.parse(path, year=year)
    if importer == "nexamp_commissions":
        return nexamp.parse_commissions(path, _sales_index(c, "Nexamp"))
    if importer == "nexamp_sales_report":
        return nexamp.parse_sales_report(path)
    if importer == "spark":
        return spark.parse(path)
    if importer == "generic":
        return generic.parse(path, **(options or {}), year=year)
    raise ValueError(f"Unknown importer {importer}")


def preview(c, storage, source_file_id, week: str | None, actor: str, importer: str | None = None, options: dict | None = None,
            work_dir: Path | None = None) -> dict:
    sf = c.execute("select * from source_files where id=%s", (source_file_id,)).fetchone()
    if not sf:
        raise LookupError("source file not found")
    work_dir = work_dir or Path("/tmp/emg-imports")
    work_dir.mkdir(parents=True, exist_ok=True)
    local = storage.copy_to(sf["storage_key"], work_dir / f"{sf['sha256']}{Path(sf['file_name']).suffix}")
    importer = importer or detect(local)
    if not importer:
        raise ValueError("Could not recognise this file. Choose an importer (or the generic mapped importer) explicitly.")
    res = parse(c, importer, local, week, options)

    batch = c.execute(
        "insert into import_batches(source_file_id,importer,vendor,week,status,summary,errors,warnings,excluded,created_by)"
        " values (%s,%s,%s,%s,'preview',%s,%s,%s,%s,%s) returning *",
        (source_file_id, importer, res.vendor, week, J(res.summary()), J(res.errors), J(res.warnings), J(res.excluded), actor),
    ).fetchone()
    dupes = 0
    with c.cursor() as cur:
        for n, r in enumerate(res.rows, start=1):
            tk = transaction_key(r) if r.line_type in MONEY_TYPES else None
            dup = None
            if tk:
                hit = c.execute("select import_row_id from invoice_lines where vendor=%s and line_type=%s and transaction_key=%s",
                                (r.vendor, r.line_type, tk)).fetchone()
                dup = hit and hit["import_row_id"]
                dupes += bool(dup)
            cur.execute("insert into import_rows(batch_id,row_no,line_type,vendor,enrollment_id,transaction_key,payload,duplicate_of)"
                        " values (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (batch["id"], n, r.line_type, r.vendor, r.enrollment_id, tk, J(r.to_payload()), dup))
    summary = res.summary() | {"already_imported_lines": dupes}
    same_file = c.execute("select id, week from import_batches where source_file_id=%s and status='committed'", (source_file_id,)).fetchall()
    if same_file:
        summary["same_file_committed_batches"] = [str(b["id"]) for b in same_file]
    c.execute("update import_batches set summary=%s where id=%s", (J(summary), batch["id"]))
    audit(c, actor, "import.preview", "import_batch", str(batch["id"]), None, summary)
    return batch_view(c, batch["id"])


def batch_view(c, batch_id, sample: int = 25) -> dict:
    b = c.execute("select b.*, f.file_name, f.sha256 from import_batches b join source_files f on f.id=b.source_file_id where b.id=%s", (batch_id,)).fetchone()
    if not b:
        raise LookupError("batch not found")
    counts = c.execute("select line_type, count(*) n, count(duplicate_of) dup from import_rows where batch_id=%s group by 1 order by 1", (batch_id,)).fetchall()
    rows = c.execute("select row_no, line_type, enrollment_id, duplicate_of, payload from import_rows where batch_id=%s order by row_no limit %s", (batch_id, sample)).fetchall()
    return dict(b) | {"line_counts": counts, "sample_rows": rows}


def commit(c, batch_id, actor: str, accept_errors: bool = False) -> dict:
    b = c.execute("select * from import_batches where id=%s for update", (batch_id,)).fetchone()
    if not b:
        raise LookupError("batch not found")
    if b["status"] == "committed":
        return batch_view(c, batch_id)  # idempotent
    if b["status"] != "preview":
        raise ValueError(f"Batch is {b['status']}")
    if b["errors"] and not accept_errors:
        raise ValueError(f"Batch has {len(b['errors'])} row errors; review them and commit with accept_errors to keep them in the review queue")
    inserted = refused = enrolled = status_changes = 0
    rows = c.execute("select * from import_rows where batch_id=%s order by row_no", (batch_id,)).fetchall()
    for ir in rows:
        r = SaleRow.from_payload(ir["payload"])
        if ir["transaction_key"]:
            hit = c.execute(
                "insert into invoice_lines(vendor,line_type,transaction_key,enrollment_id,invoice_number,invoice_date,amount,import_row_id)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (vendor,line_type,transaction_key) do nothing returning id",
                (r.vendor, r.line_type, ir["transaction_key"], r.enrollment_id, r.invoice_number or None, r.invoice_date, r.vendor_amount, ir["id"]),
            ).fetchone()
            if hit:
                inserted += 1
            else:
                orig = c.execute("select import_row_id from invoice_lines where vendor=%s and line_type=%s and transaction_key=%s",
                                 (r.vendor, r.line_type, ir["transaction_key"])).fetchone()["import_row_id"]
                c.execute("update import_rows set duplicate_of=%s where id=%s", (orig, ir["id"]))
                refused += 1
                c.execute(
                    "insert into review_items(scope_key,week,batch_id,kind,message,vendor,enrollment_id,person,amount,blocking,data)"
                    " values (%s,%s,%s,'duplicate_import',%s,%s,%s,%s,%s,false,%s) on conflict (scope_key) do nothing",
                    (f"dupimport:{r.vendor}:{r.line_type}:{ir['transaction_key']}:{batch_id}", b["week"], batch_id,
                     f"{r.line_type} line already imported (original import row {orig}); not counted again", r.vendor, r.enrollment_id,
                     r.raw_rep, r.vendor_amount, J({"transaction_key": ir["transaction_key"]})),
                )
        if r.enrollment_id and r.line_type != "review":
            e, changed = _upsert_enrollment(c, r, b, ir)
            enrolled += 1
            status_changes += changed
    for err in b["errors"]:
        ref = err.get("reference") or err.get("enrollment_id") or ""
        c.execute(
            "insert into review_items(scope_key,week,batch_id,kind,message,vendor,enrollment_id,blocking,data)"
            " values (%s,%s,%s,'import_error',%s,%s,%s,true,%s) on conflict (scope_key) do nothing",
            (f"importerr:{batch_id}:{err.get('sheet')}:{err.get('row')}", b["week"], batch_id, err.get("error", "Import error"), b["vendor"], ref, J(err)),
        )
    c.execute("update import_batches set status='committed', committed_by=%s, committed_at=now(), summary = summary || %s where id=%s",
              (actor, J({"committed": {"lines": inserted, "refused_duplicates": refused, "enrollments": enrolled, "status_changes": status_changes}}), batch_id))
    audit(c, actor, "import.commit", "import_batch", str(batch_id), None, {"lines": inserted, "refused_duplicates": refused})
    return batch_view(c, batch_id)


def reject(c, batch_id, actor: str, note: str) -> None:
    b = c.execute("select status from import_batches where id=%s for update", (batch_id,)).fetchone()
    if not b or b["status"] != "preview":
        raise ValueError("Only previews can be rejected")
    c.execute("update import_batches set status='rejected' where id=%s", (batch_id,))
    audit(c, actor, "import.reject", "import_batch", str(batch_id), None, None, note)


def _upsert_enrollment(c, r: SaleRow, batch: dict, ir: dict) -> tuple[dict, int]:
    vals = {
        "customer_id": r.customer_id or None, "customer_name": r.customer or None, "phone": r.phone or None,
        "service_address": r.service_address or None, "city": r.city or None, "state": r.state or None, "zip": r.zip or None,
        "utility": r.utility or None, "product": r.product or None, "premise": r.premise or None, "raw_rep": r.raw_rep or None,
        "sale_date": r.sale_date, "approval_date": r.approval_date, "invoice_date": r.invoice_date,
        "kwh": r.kwh if r.kwh else None, "units": r.units,
    }
    status = _status_label(r)
    approval_source = "vendor" if r.approval_date else None
    e = c.execute("select * from enrollments where vendor=%s and enrollment_id=%s for update", (r.vendor, r.enrollment_id)).fetchone()
    if not e:
        e = c.execute(
            "insert into enrollments(vendor,enrollment_id,customer_id,customer_name,phone,service_address,city,state,zip,utility,product,premise,raw_rep,"
            "sale_date,approval_date,approval_date_source,invoice_date,kwh,units,status,status_reason) values"
            " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
            (r.vendor, r.enrollment_id, vals["customer_id"], vals["customer_name"], vals["phone"], vals["service_address"], vals["city"],
             vals["state"], vals["zip"], vals["utility"], vals["product"], vals["premise"], vals["raw_rep"], vals["sale_date"],
             vals["approval_date"], approval_source, vals["invoice_date"], vals["kwh"], vals["units"], status, r.stage or None),
        ).fetchone()
        changed = 1
    else:
        # Descriptive fields take the latest vendor value; dates are set once and
        # never overwritten (sale/approval dates come from the first source that knows them).
        sets, args = [], []
        for col, v in vals.items():
            if v is None or v == e[col]:
                continue
            if col in ("sale_date", "approval_date", "invoice_date") and e[col] is not None:
                continue
            sets.append(f"{col}=%s")
            args.append(v)
        if r.approval_date and not e["approval_date"]:
            sets.append("approval_date_source='vendor'")
        changed = int(status != e["status"])
        if changed:
            sets += ["status=%s", "status_reason=%s"]
            args += [status, r.stage or None]
        if sets:
            e = c.execute(f"update enrollments set {', '.join(dict.fromkeys(sets))}, updated_at=now() where id=%s returning *", (*args, e["id"])).fetchone()
    if changed:
        c.execute("insert into enrollment_status_history(enrollment_pk,status,reason,import_batch_id,source_file,source_row) values (%s,%s,%s,%s,%s,%s)",
                  (e["id"], status, r.stage or r.notes or None, batch["id"], r.source_file, r.source_row))
    return e, changed


def _status_label(r: SaleRow) -> str:
    return {
        "payable": "Paid by vendor", "chargeback": "Charged back", "nonpayable": r.status or "Non-payable",
        "pending": r.status or "Pending", "company_only": "Company-only installment", "adjustment": "Rate adjustment",
    }.get(r.line_type, r.status or r.line_type)


def committed_rows(c, week: str) -> tuple[list[SaleRow], list]:
    """All committed, non-duplicate rows for a payroll week, plus the batch ids used.

    Nexamp non-payables are derived from the week's sales report (current-year
    sales that reached this commission week unpaid)."""
    batches = c.execute("select id, importer from import_batches where week=%s and status='committed' order by created_at", (week,)).fetchall()
    rows: list[SaleRow] = []
    sales_report: list[SaleRow] = []
    for b in batches:
        for ir in c.execute("select payload from import_rows where batch_id=%s and duplicate_of is null order by row_no", (b["id"],)):
            r = SaleRow.from_payload(ir["payload"])
            (sales_report if b["importer"] == "nexamp_sales_report" else rows).append(r)
    if sales_report:
        year, wk = parse_week(week)
        paid = {r.enrollment_id for r in rows if r.vendor == "Nexamp" and r.line_type == "payable"}
        rep = ImportResult("Nexamp", "nexamp_sales_report", "", "", rows=sales_report)
        rows += nexamp.nonpayables_for_week(rep, wk, year, paid)
    return rows, [b["id"] for b in batches]

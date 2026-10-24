"""Nexamp commission workbook and Nexamp all-sales report.

Commission workbook: sheet "Nexamp" (paid rows), "Sheet1" (chargebacks),
"Summary". Original sale dates come from the all-sales report
("Created Date"), never from the commission date.
"""
from __future__ import annotations

from pathlib import Path

import openpyxl

from ..model import SaleRow
from ..names import clean, key, ref_key
from .base import FieldMapping, ImportResult, col, find_header, num, sha256_file, to_date


def detect_commissions(path) -> bool:
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return False
    return "Nexamp" in wb.sheetnames


def detect_sales_report(path) -> bool:
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return False
    first = next(wb.worksheets[0].iter_rows(max_row=1, values_only=True), ())
    labels = {key(c) for c in first if c}
    return {"PEG OPPORTUNITY ID", "PARTNER SALES REP", "CREATED DATE"} <= labels


def parse_sales_report(path: str | Path) -> ImportResult:
    """Every Nexamp opportunity with its lifecycle fields (status history source)."""
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    idx = {key(h): i for i, h in enumerate(header) if h}
    res = ImportResult("Nexamp", "nexamp_sales_report", path.name, sha256_file(path))
    res.mapping = [FieldMapping(t, s, t in ("enrollment_id", "raw_rep")) for t, s in [
        ("enrollment_id", "PEG Opportunity ID"), ("raw_rep", "Partner Sales Rep"), ("sale_date", "Created Date"),
        ("approval_date", "Close Date (Closed Won only)"), ("status", "Stage / Sub Stage / Loss Reason"), ("kwh", "Billable kWh"),
        ("utility", "Utility"), ("product", "LMI Category"), ("project", "Associated Project LLC Name")]]
    for i, r in enumerate(rows, start=2):
        ref = ref_key(col(r, idx, "PEG Opportunity ID"))
        if not ref:
            continue
        stage = clean(col(r, idx, "Stage"))
        parts = [clean(col(r, idx, h)) for h in ("Sub Stage", "Loss Reason", "Screening Notes")]
        reason = next((p for p in parts if p not in ("", "-", "0")), "")
        won = key(stage) == "CLOSED WON"
        s = SaleRow(
            vendor="Nexamp", raw_vendor="Nexamp", enrollment_id=ref, raw_rep=clean(col(r, idx, "Partner Sales Rep")),
            customer=clean(f"{col(r, idx, 'First Name') or ''} {col(r, idx, 'Last Name') or ''}"),
            utility=clean(col(r, idx, "Utility")), product="LMI" if key(col(r, idx, "LMI Category")) == "LMI" else "OPEN",
            kwh=num(col(r, idx, "Billable kWh")), sale_date=to_date(col(r, idx, "Created Date")),
            approval_date=to_date(col(r, idx, "Close Date")) if won else None,
            line_type="status", status=stage, stage=reason, source_file=path.name, source_sheet=ws.title, source_row=i,
            raw={"commission_week": col(r, idx, "Commission Week"), "sales_week": col(r, idx, "Sales Week"),
                 "project": col(r, idx, "Associated Project LLC Name"), "commission_paid": str(col(r, idx, "Commission PAID") or ""),
                 "cancel_status": col(r, idx, "Cancelation Status")},
        )
        res.rows.append(s)
    return res


def nonpayables_for_week(report: ImportResult, commission_week: int, year: int, paid_refs: set[str]) -> list[SaleRow]:
    """Current-year sales that reached this commission week without being paid."""
    out = []
    for s in report.rows:
        cw = s.raw.get("commission_week")
        if str(cw).replace(".0", "") != str(commission_week):
            continue
        if not s.sale_date or s.sale_date.year != year:
            continue
        if s.enrollment_id in paid_refs or key(s.status) == "CLOSED WON":
            continue
        n = SaleRow(**{**s.__dict__})
        n.line_type = "nonpayable"
        n.status = ": ".join(x for x in (s.status, s.stage) if x and x != "-") or "Non-payable"
        out.append(n)
    return out


def parse_commissions(path: str | Path, sales_index: dict[str, SaleRow] | None = None) -> ImportResult:
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    res = ImportResult("Nexamp", "nexamp_commissions", path.name, sha256_file(path))
    sales_index = sales_index or {}
    rows = list(wb["Nexamp"].iter_rows(values_only=True))
    hi, idx = find_header(rows, ["Partner Sales Rep", "PEG Opportunity ID"])
    res.mapping = [FieldMapping(c, c, c in ("Partner Sales Rep", "PEG Opportunity ID"), rows[hi + 1][idx[key(c)]] if len(rows) > hi + 1 and key(c) in idx else None)
                   for c in ["Partner Sales Rep", "Commission Week", "PEG Opportunity ID", "Utility", "First Name", "Last Name", "Billable kWh", "Commission", "Commission Date", "LMI Category"]]
    total = 0
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        if not col(r, idx, "Partner Sales Rep") or not col(r, idx, "PEG Opportunity ID"):
            continue
        ref = ref_key(col(r, idx, "PEG Opportunity ID"))
        sales = sales_index.get(ref)
        s = SaleRow(
            vendor="Nexamp", raw_vendor="Nexamp", enrollment_id=ref, raw_rep=clean(col(r, idx, "Partner Sales Rep")),
            customer=clean(f"{col(r, idx, 'First Name') or ''} {col(r, idx, 'Last Name') or ''}"),
            utility=clean(col(r, idx, "Utility")), product="LMI" if key(col(r, idx, "LMI Category")) == "LMI" else "OPEN",
            kwh=num(col(r, idx, "Billable kWh")), vendor_amount=num(col(r, idx, "Commission")),
            invoice_date=to_date(col(r, idx, "Commission Date")), invoice_number=f"NEX-WK{col(r, idx, 'Commission Week')}",
            sale_date=sales.sale_date if sales else None, approval_date=sales.approval_date if sales else None,
            line_type="payable", status=clean(col(r, idx, "Stage")), source_file=path.name, source_sheet="Nexamp", source_row=i + 1,
            raw={"commission_week": col(r, idx, "Commission Week")},
        )
        total += s.vendor_amount
        if s.sale_date is None:
            res.errors.append({"sheet": "Nexamp", "row": i + 1, "reference": ref, "error": "Original sale date not found in the Nexamp sales report"})
        res.rows.append(s)
    res.meta["vendor_total"] = str(total)
    if "Sheet1" in wb.sheetnames:
        cb = list(wb["Sheet1"].iter_rows(values_only=True))
        try:
            h2, cidx = find_header(cb, ["PEG Opportunity ID", "Field Commission Paid"])
        except ValueError:
            h2 = None
        if h2 is not None:
            for i in range(h2 + 1, len(cb)):
                r = cb[i]
                raw_ref = clean(col(r, cidx, "PEG Opportunity ID"))
                if not raw_ref:
                    continue
                ref = raw_ref.split(" - ")[0]
                sales = sales_index.get(ref)
                res.rows.append(SaleRow(
                    vendor="Nexamp", raw_vendor="Nexamp", enrollment_id=ref, raw_rep=clean(col(r, cidx, "Partner Sales Rep")),
                    customer=clean(col(r, cidx, "Opportunity Name")), utility=clean(col(r, cidx, "Utility")),
                    product=sales.product if sales else "", kwh=sales.kwh if sales else num(0),
                    sale_date=to_date(col(r, cidx, "Created Date")), invoice_date=to_date(col(r, cidx, "Chargeback Date")),
                    vendor_amount=-abs(num(col(r, cidx, "Field Commission Paid"))), line_type="chargeback",
                    status=clean(col(r, cidx, "Stage")), source_file=path.name, source_sheet="Sheet1", source_row=i + 1,
                    raw={"raw_reference": raw_ref},
                ))
    return res

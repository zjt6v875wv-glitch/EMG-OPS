"""Mapped importer for vendors without a dedicated parser (Ampion and others).

Works on CSV, XLSX and text-based PDF tables. The caller supplies a mapping of
target fields to source column labels (saved per vendor in the UI), plus how
to classify rows:

    mapping = {"enrollment_id": "Account #", "raw_rep": "Agent", "vendor_amount": "Commission",
               "sale_date": "Signed", "status": "Status", ...}
    payable_statuses = ["Approved", "Paid"]      # rows whose status matches -> payable
    chargeback_when_negative = True               # negative amounts -> chargeback

Unmapped or unclassifiable rows become ``review`` rows, never dropped. Rows
whose sale date is in a prior year (when ``year`` is given) are excluded and
listed in ``excluded``.
"""
from __future__ import annotations

import csv
from pathlib import Path

import openpyxl

from ..model import SaleRow
from ..names import clean, key, ref_key
from .base import FieldMapping, ImportResult, num, sha256_file, to_date

TARGETS = ("enrollment_id", "raw_rep", "customer", "customer_id", "transaction_id", "product_id", "utility", "state", "product",
           "premise", "kwh", "units", "sale_date", "approval_date", "invoice_date", "invoice_number", "vendor_amount", "status",
           "service_address", "city", "zip", "phone")
REQUIRED = ("enrollment_id", "raw_rep")


def _table(path: Path, sheet: str | None) -> list[list]:
    suf = path.suffix.lower()
    if suf == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            return [list(r) for r in csv.reader(f)]
    if suf in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if suf == ".pdf":
        import pdfplumber

        out: list[list] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for t in page.extract_tables():
                    out.extend(t)
        return out
    raise ValueError(f"Unsupported file type {suf}")


def parse(path: str | Path, vendor: str, mapping: dict[str, str], payable_statuses: list[str] | None = None,
          nonpayable_statuses: list[str] | None = None, chargeback_when_negative: bool = True, sheet: str | None = None,
          defaults: dict | None = None, year: int | None = None) -> ImportResult:
    path = Path(path)
    bad = [t for t in mapping if t not in TARGETS]
    if bad:
        raise ValueError(f"Unknown target fields {bad}")
    missing = [t for t in REQUIRED if t not in mapping]
    if missing:
        raise ValueError(f"Mapping must include {missing}")
    rows = _table(path, sheet)
    labels = {key(v) for v in mapping.values()}
    hi = next((i for i, r in enumerate(rows[:40]) if labels <= {key(c) for c in r if c not in (None, "")}), None)
    res = ImportResult(vendor, f"generic:{path.suffix.lower().lstrip('.')}", path.name, sha256_file(path))
    if hi is None:
        res.errors.append({"row": 0, "error": f"Header row with columns {sorted(mapping.values())} not found"})
        return res
    idx = {key(c): j for j, c in enumerate(rows[hi]) if c not in (None, "")}
    res.mapping = [FieldMapping(t, s, t in REQUIRED, rows[hi + 1][idx[key(s)]] if len(rows) > hi + 1 else None) for t, s in mapping.items()]
    pay = {key(s) for s in payable_statuses or []}
    nonpay = {key(s) for s in nonpayable_statuses or []}
    defaults = defaults or {}
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        if not any(c not in (None, "") for c in r):
            continue

        def g(t):
            j = idx.get(key(mapping[t])) if t in mapping else None
            return r[j] if j is not None and j < len(r) else defaults.get(t)

        ref = ref_key(g("enrollment_id"))
        s = SaleRow(
            vendor=vendor, raw_vendor=vendor, enrollment_id=ref, raw_rep=clean(g("raw_rep")), customer=clean(g("customer")),
            customer_id=clean(g("customer_id")), transaction_id=clean(g("transaction_id")), product_id=clean(g("product_id")),
            utility=clean(g("utility")), state=clean(g("state")), product=clean(g("product")), premise=clean(g("premise")) or "Residential",
            kwh=num(g("kwh")), units=num(g("units")) or num(1), sale_date=to_date(g("sale_date")), approval_date=to_date(g("approval_date")),
            invoice_date=to_date(g("invoice_date")), invoice_number=clean(g("invoice_number")), vendor_amount=num(g("vendor_amount")),
            status=clean(g("status")), service_address=clean(g("service_address")), city=clean(g("city")), zip=clean(g("zip")),
            phone=clean(g("phone")), source_file=path.name, source_sheet=sheet or "", source_row=i + 1,
        )
        if not ref or not s.raw_rep:
            s.line_type, s.notes = "review", "Missing enrollment ID or agent"
        elif year and s.sale_date and s.sale_date.year < year:
            res.excluded.append({"row": i + 1, "reference": ref, "reason": f"Prior-year sale ({s.sale_date.isoformat()})", "amount": str(s.vendor_amount)})
            continue
        elif chargeback_when_negative and s.vendor_amount < 0:
            s.line_type = "chargeback"
        elif pay and key(s.status) in pay:
            s.line_type = "payable"
        elif nonpay and key(s.status) in nonpay:
            s.line_type = "nonpayable"
        elif not pay and s.vendor_amount > 0:
            s.line_type = "payable"
        else:
            s.line_type, s.notes = "review", f"Status {s.status!r} not classified"
        res.rows.append(s)
    return res

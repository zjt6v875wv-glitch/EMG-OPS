"""Spark Energy marketer invoice (SUMMARY + DETAILS sheets).

IDs: TransactionDetId = invoice transaction, Main Id = enrollment.
A row is payable when RCE STATUS is "Payable" with a positive invoice amount.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import openpyxl

from ..model import SaleRow
from ..names import clean, key, ref_key
from .base import FieldMapping, ImportResult, col, find_header, num, sha256_file, to_date

MARKETS = {"SOCAL": "SoCalGas"}
PRODUCT = "Natural Gas Enrollment"


def detect(path) -> bool:
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return False
    if "DETAILS" not in wb.sheetnames:
        return False
    first = next(wb["DETAILS"].iter_rows(max_row=1, values_only=True), ())
    return "TRANSACTIONDETID" in {key(c) for c in first if c}


def parse(path: str | Path) -> ImportResult:
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    res = ImportResult("Spark", "spark_marketer_invoice", path.name, sha256_file(path))
    rows = list(wb["DETAILS"].iter_rows(values_only=True))
    hi, idx = find_header(rows, ["TransactionDetId", "Main Id", "Sales Agent"])
    res.mapping = [FieldMapping(t, s, t in ("transaction_id", "enrollment_id", "raw_rep"), rows[hi + 1][idx[key(s)]] if len(rows) > hi + 1 and key(s) in idx else None)
                   for t, s in [("transaction_id", "TransactionDetId"), ("enrollment_id", "Main Id"), ("invoice_number", "Invoice Number"),
                                ("vendor_amount", "Invoice Amount"), ("sale_date", "Sold Date"), ("raw_rep", "Sales Agent"),
                                ("status", "Commission Status"), ("payable", "RCE STATUS"), ("approval_date", "Utility Response Date"),
                                ("customer", "Customer Name"), ("state", "Service State")]]
    seen: dict[str, int] = {}
    total = Decimal(0)
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        tx = clean(col(r, idx, "TransactionDetId"))
        if not tx:
            continue
        tx = ref_key(tx)
        enrollment = ref_key(col(r, idx, "Main Id"))
        payable = key(col(r, idx, "RCE STATUS")) == "PAYABLE" and num(col(r, idx, "Invoice Amount")) > 0
        status = clean(col(r, idx, "Commission Status"))
        reject = clean(col(r, idx, "Reject Description"))
        s = SaleRow(
            vendor="Spark", raw_vendor="Spark", enrollment_id=enrollment, transaction_id=tx,
            product_id=clean(col(r, idx, "Product Offering")), raw_rep=clean(col(r, idx, "Sales Agent")),
            customer=clean(col(r, idx, "Customer Name")), utility=MARKETS.get(key(col(r, idx, "Market")), clean(col(r, idx, "Market"))),
            state=clean(col(r, idx, "Service State")), product=PRODUCT, premise=clean(col(r, idx, "Premise Type")).title() or "Residential",
            kwh=num(0), units=Decimal(1), sale_date=to_date(col(r, idx, "Sold Date")),
            approval_date=to_date(col(r, idx, "Utility Response Date")) if key(status) == "ENROLLMENT ACCEPTED" else None,
            invoice_date=to_date(col(r, idx, "Paid Date")), invoice_number=clean(col(r, idx, "Invoice Number")),
            vendor_amount=num(col(r, idx, "Invoice Amount")), line_type="payable" if payable else "nonpayable",
            status=status + (f": {reject}" if reject else ""), phone=clean(col(r, idx, "Phone Number")),
            source_file=path.name, source_sheet="DETAILS", source_row=i + 1,
            raw={"agent_id": clean(col(r, idx, "Agent ID")), "transaction_type": clean(col(r, idx, "Transaction Type"))},
        )
        if s.line_type == "payable":
            total += s.vendor_amount
            if enrollment in seen:
                s.line_type = "review"
                s.notes = f"Duplicate payable enrollment {enrollment} (also row {seen[enrollment]})"
                res.warnings.append({"row": i + 1, "warning": s.notes})
            else:
                seen[enrollment] = i + 1
        res.rows.append(s)
    res.meta["vendor_total"] = str(total)
    res.meta["invoice_numbers"] = sorted({r.invoice_number for r in res.rows if r.invoice_number})
    return res


def write_repriced_copy(src: str | Path, dest: str | Path, keep_agents_raw: set[str], rate: Decimal) -> dict:
    """Copy the original Spark workbook, keep only the given agents' DETAILS rows,
    and reprice payable rows only. All source columns and formats are kept."""
    wb = openpyxl.load_workbook(src)  # full mode keeps styles
    ws = wb["DETAILS"]
    header = [c.value for c in ws[1]]
    h = {key(v): i for i, v in enumerate(header) if v}
    amount_col = h["INVOICE AMOUNT"] + 1
    agent_cols = [h[k] + 1 for k in ("SALES AGENT", "AGENT ID") if k in h]
    rce_col = h["RCE STATUS"] + 1
    keep_keys = {key(a) for a in keep_agents_raw}
    original_total = sum((num(ws.cell(r, amount_col).value) for r in range(2, ws.max_row + 1)), Decimal(0))
    kept = repriced = 0
    for row in range(ws.max_row, 1, -1):
        vals = [ws.cell(row, c).value for c in agent_cols]
        if not any(v for v in [ws.cell(row, 1).value]):
            continue
        if any(key(v) in keep_keys for v in vals if v):
            kept += 1
            if key(ws.cell(row, rce_col).value) == "PAYABLE" and num(ws.cell(row, amount_col).value) > 0:
                ws.cell(row, amount_col).value = float(rate)
                repriced += 1
        else:
            ws.delete_rows(row, 1)
    total = repriced * rate
    if "SUMMARY" in wb.sheetnames:
        import re
        sm = wb["SUMMARY"]
        for row in sm.iter_rows():
            for c in row:
                if isinstance(c.value, str) and "Commission Total:" in c.value:
                    c.value = re.sub(r"Commission Total: \$[\d,\.]+", f"Commission Total: ${total:,.0f}", c.value)
                elif (isinstance(c.value, str) and c.value.upper().startswith("=SUM(DETAILS!")) or (
                        isinstance(c.value, (int, float)) and not isinstance(c.value, bool) and original_total and Decimal(str(c.value)) == original_total):
                    # the commission total cell becomes a live sum of the repriced rows
                    letter = openpyxl.utils.get_column_letter(amount_col)
                    c.value = f"=SUM(DETAILS!{letter}2:{letter}{kept + 1})"
    wb.save(dest)
    return {"kept_rows": kept, "repriced_rows": repriced, "total": str(total)}

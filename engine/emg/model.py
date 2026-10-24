"""Canonical records shared by importers, the commission calculator and payroll."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


@dataclass
class SaleRow:
    """One vendor row about one enrollment.

    IDs are kept distinct: ``customer_id`` (vendor customer/account),
    ``enrollment_id`` (the enrollment / opportunity / reference),
    ``product_id`` (vendor product offering), ``transaction_id`` (the
    invoice line).
    """

    vendor: str
    enrollment_id: str
    raw_rep: str
    customer: str = ""
    customer_id: str = ""
    transaction_id: str = ""
    product_id: str = ""
    raw_vendor: str = ""
    utility: str = ""
    state: str = ""
    product: str = ""
    premise: str = "Residential"
    kwh: Decimal = ZERO
    units: Decimal = Decimal("1")
    sale_date: date | None = None
    approval_date: date | None = None
    invoice_date: date | None = None
    invoice_number: str = ""
    vendor_amount: Decimal = ZERO
    line_type: str = "payable"  # payable | nonpayable | pending | chargeback | adjustment | company_only
    status: str = ""
    stage: str = ""
    service_address: str = ""
    city: str = ""
    zip: str = ""
    phone: str = ""
    source_file: str = ""
    source_sheet: str = ""
    source_row: int = 0
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.vendor, self.enrollment_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, date):
                d[k] = v.isoformat()
        d.pop("raw", None)
        return d

    def to_payload(self) -> dict:
        """Lossless JSON form (keeps ``raw``) for import_rows.payload."""
        d = self.to_dict()
        d["raw"] = {k: (v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, Decimal) else v) for k, v in self.raw.items()}
        return d

    @staticmethod
    def from_payload(d: dict) -> "SaleRow":
        kw = dict(d)
        for k in ("kwh", "units", "vendor_amount"):
            if kw.get(k) is not None:
                kw[k] = Decimal(str(kw[k]))
        for k in ("sale_date", "approval_date", "invoice_date"):
            if kw.get(k):
                kw[k] = date.fromisoformat(str(kw[k])[:10])
        kw["raw"] = dict(kw.get("raw") or {})
        return SaleRow(**kw)


@dataclass
class EarningLine:
    payee: str
    activity: str  # Agent Commission | Manager Override | Personal Rate Top-Up | Company Payable
    vendor: str
    rep: str
    enrollment_id: str
    customer: str = ""
    utility: str = ""
    product: str = ""
    kwh: Decimal = ZERO
    units: Decimal = ZERO
    rate: Decimal = ZERO
    amount: Decimal = ZERO
    sale_date: date | None = None
    transaction_id: str = ""
    rule_id: str = ""
    source_rep: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, date):
                d[k] = v.isoformat()
        return d


@dataclass
class ReviewItem:
    kind: str  # missing_rate | unknown_agent | missing_override | duplicate | unmatched_chargeback | rate_cap | product_unknown | ...
    message: str
    vendor: str = ""
    enrollment_id: str = ""
    person: str = ""
    amount: Decimal = ZERO
    blocking: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        return d


@dataclass
class ChargebackLine:
    """A proposed payroll reversal of a specific historical payout."""

    payee: str
    vendor: str
    enrollment_id: str
    amount: Decimal  # positive number to recover
    rep: str = ""
    customer: str = ""
    utility: str = ""
    product: str = ""
    kwh: Decimal = ZERO
    rate: Decimal = ZERO
    verified: bool = False
    original_week: str = ""
    sale_date: date | None = None
    evidence: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, date):
                d[k] = v.isoformat()
        return d


@dataclass
class CompanyAdjustment:
    vendor: str
    enrollment_id: str
    amount: Decimal
    reason: str
    customer: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = str(self.amount)
        return d

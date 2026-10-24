"""Decimal money helpers.

Rounding rules (documented in docs/ROUNDING.md):
- Line amounts (kWh x rate) are kept at full Decimal precision.
- Per-payee category totals (agent pay, overrides, chargebacks) are summed at
  full precision and rounded once, half-up, to cents.
- Every deduction and every balance is in cents.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")


def D(value) -> Decimal:
    """Convert a spreadsheet/JSON value to Decimal without float artifacts."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip() or "0")
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


def cents(value) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def dmin(*values) -> Decimal:
    return min(D(v) for v in values)


def dmax(*values) -> Decimal:
    return max(D(v) for v in values)


def fmt(value) -> str:
    v = cents(value)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"

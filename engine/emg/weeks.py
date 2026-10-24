"""Payroll week helpers.

A payroll week is labelled ``YYYY-Www`` (ISO week). Pay dates are Fridays.
Business weeks for leaderboards run Monday-Sunday in the configured timezone.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
DEFAULT_TZ = "America/New_York"


def parse_week(label: str) -> tuple[int, int]:
    m = WEEK_RE.match(str(label).strip())
    if not m:
        raise ValueError(f"Week must look like 2026-W43, got {label!r}")
    return int(m.group(1)), int(m.group(2))


def week_label(year: int, week: int) -> str:
    return f"{year}-W{week:02d}"


def pay_date(label: str) -> date:
    """Friday of the ISO week."""
    y, w = parse_week(label)
    return date.fromisocalendar(y, w, 5)


def week_of(d: date) -> str:
    y, w, _ = d.isocalendar()
    return week_label(y, w)


def week_number(label: str) -> int:
    return parse_week(label)[1]


def week_in_range(label: str, first: str | None, last: str | None) -> bool:
    """Inclusive range check; ``None`` means open-ended."""
    if first and label < first:
        return False
    if last and label > last:
        return False
    return True


def shift_week(label: str, n: int) -> str:
    y, w = parse_week(label)
    d = date.fromisocalendar(y, w, 1) + timedelta(weeks=n)
    return week_of(d)


def business_week_bounds(d: date) -> tuple[date, date]:
    """Monday..Sunday containing ``d``."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def local_date(ts: datetime | date | str | None, tz: str = DEFAULT_TZ) -> date | None:
    """Convert a timestamp to the business-local calendar date.

    Naive datetimes are treated as already local (vendor files carry dates
    without zones). Aware datetimes are converted into ``tz``.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        ts = datetime.fromisoformat(s.replace("Z", "+00:00")) if ("T" in s or ":" in s) else date.fromisoformat(s[:10])
    if isinstance(ts, datetime):
        if ts.tzinfo is not None:
            ts = ts.astimezone(ZoneInfo(tz))
        return ts.date()
    return ts

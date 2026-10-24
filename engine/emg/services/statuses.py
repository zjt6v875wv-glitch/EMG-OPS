"""Enrollment status categories used by the map and leaderboards.

Vendor statuses are free text. Each is mapped to one category by the first
matching pattern; admins can override the patterns in settings
(key ``status_categories``). Unknown statuses fall into ``other`` and are
never counted as approved.
"""
from __future__ import annotations

import re

CATEGORIES = ("approved", "pending", "needs_action", "denied", "cancelled", "chargeback", "duplicate", "other")

DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("chargeback", r"charged back|chargeback|clawback"),
    ("duplicate", r"duplicate"),
    ("approved", r"paid by vendor|^payable|closed won|company-only installment|rate adjustment|^approved|^active$"),
    ("cancelled", r"cancel|closed lost|not interested|^closed$"),
    ("denied", r"^denied|rejected|ineligible"),
    ("needs_action", r"cannot process|need|invalid|missing"),
    ("pending", r"pending|^sent$|bill analysis|tpv|proposal|review|submitted|in progress"),
]

COLORS = {"approved": "#1F8A4C", "pending": "#D69E2E", "needs_action": "#DD6B20", "denied": "#C53030",
          "cancelled": "#718096", "chargeback": "#6B46C1", "duplicate": "#A0AEC0", "other": "#2B6CB0"}


class Classifier:
    def __init__(self, patterns: list[tuple[str, str]] | None = None):
        self.patterns = [(cat, re.compile(rx, re.I)) for cat, rx in (patterns or DEFAULT_PATTERNS)]
        self._memo: dict[str, str] = {}

    def __call__(self, status: str | None) -> str:
        s = (status or "").strip()
        if s not in self._memo:
            self._memo[s] = next((cat for cat, rx in self.patterns if rx.search(s)), "other")
        return self._memo[s]


def classifier(c) -> Classifier:
    row = c.execute("select value from settings where key='status_categories'").fetchone()
    pats = [(p["category"], p["pattern"]) for p in row["value"]] if row else None
    if pats:
        for cat, rx in pats:
            if cat not in CATEGORIES:
                raise ValueError(f"Unknown status category {cat}")
            re.compile(rx)
    return Classifier(pats)


def seller_resolver(c):
    """raw vendor rep -> canonical seller, using the active rule set's aliases and roster."""
    from . import rules as rules_svc
    from ..names import clean

    try:
        rs, _ = rules_svc.ruleset(c)
    except LookupError:
        rs = None
    memo: dict[tuple, str] = {}

    def resolve(raw, vendor=None) -> str:
        k = (raw or "", vendor)
        if k not in memo:
            memo[k] = (rs.canon(raw, vendor) if rs and raw else clean(raw)) or "Unattributed"
        return memo[k]

    resolve.ruleset = rs
    return resolve

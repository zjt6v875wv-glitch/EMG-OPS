"""Importer plumbing: hashing, header detection, preview results."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..model import SaleRow
from ..money import D
from ..names import clean, key


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_date(v) -> date | None:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = clean(v)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%m-%d-%Y"):
        try:
            return datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class FieldMapping:
    target: str
    source: str | None
    required: bool = False
    sample: Any = None


@dataclass
class ImportResult:
    vendor: str
    format: str
    file_name: str
    sha256: str
    rows: list[SaleRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # blocking problems per row
    warnings: list[dict] = field(default_factory=list)
    mapping: list[FieldMapping] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    excluded: list[dict] = field(default_factory=list)  # visible, counted exclusions (e.g. prior year)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.line_type] = out.get(r.line_type, 0) + 1
        return out

    def summary(self) -> dict:
        return {
            "vendor": self.vendor,
            "format": self.format,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "counts": self.counts(),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "excluded": len(self.excluded),
            "meta": {k: (str(v) if not isinstance(v, (int, float, str, bool, type(None), list, dict)) else v) for k, v in self.meta.items()},
            "mapping": [m.__dict__ | {"sample": str(m.sample) if m.sample is not None else None} for m in self.mapping],
        }


def find_header(rows: list[tuple], must: list[str], max_scan: int = 30) -> tuple[int, dict[str, int]]:
    """Locate the header row containing all ``must`` labels (case-insensitive)."""
    for i, r in enumerate(rows[:max_scan]):
        labels = {key(c): j for j, c in enumerate(r) if c is not None and clean(c)}
        if all(key(m) in labels for m in must):
            return i, labels
    raise ValueError(f"Header not found; expected columns {must}")


def col(row: tuple, idx: dict[str, int], name: str, default=None):
    j = idx.get(key(name))
    if j is None or j >= len(row):
        return default
    return row[j]


def num(v):
    return D(v) if v not in (None, "", "-") else D(0)

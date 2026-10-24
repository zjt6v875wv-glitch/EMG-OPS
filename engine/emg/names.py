"""Agent name normalization through an editable alias table.

Aliases never merge people on their own: two canonical names stay two
people (and two debt ledgers) unless an explicit merge record says otherwise.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "").strip())


def key(value) -> str:
    s = clean(value)
    # Repair common mojibake for curly apostrophes before comparing.
    s = s.replace("Â€™", "’").replace("â€™", "’")
    s = unicodedata.normalize("NFKC", s)
    # Hyphens and spaces are equivalent in names ("Mary-Jane Doe" == "MARY JANE DOE").
    s = re.sub(r"\s*-\s*", " ", s)
    return s.upper()


def ref_key(value) -> str:
    """Normalize a vendor reference (Excel often turns 123 into 123.0)."""
    s = clean(value)
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


@dataclass
class AliasTable:
    """alias KEY -> canonical name, optionally scoped to one vendor."""

    global_map: dict[str, str] = field(default_factory=dict)
    scoped: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, canonical: str, alias: str, scope: str | None = None) -> None:
        canonical = clean(canonical)
        target = self.scoped if scope and scope != "*" else self.global_map
        if scope and scope != "*":
            target[(scope.upper(), key(alias))] = canonical
            target[(scope.upper(), key(canonical))] = canonical
        else:
            target[key(alias)] = canonical
            target.setdefault(key(canonical), canonical)

    def resolve(self, raw, vendor: str | None = None) -> str:
        s = clean(raw)
        if not s:
            return ""
        k = key(s)
        if vendor and (vendor.upper(), k) in self.scoped:
            return self.scoped[(vendor.upper(), k)]
        if k in self.global_map:
            return self.global_map[k]
        return s.title() if s.isupper() and " " in s else s

    def known(self, raw, vendor: str | None = None) -> bool:
        k = key(raw)
        return k in self.global_map or bool(vendor and (vendor.upper(), k) in self.scoped)

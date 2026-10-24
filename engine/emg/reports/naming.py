"""File names, e.g. EMG_Week43_Final_Package_2026-10-23.zip and Jane_Doe_Week43_2026-10-23.pdf."""
from __future__ import annotations

import re
import unicodedata


def slug(name: str) -> str:
    s = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))  # Zoë -> Zoe
    return re.sub(r"[^A-Za-z0-9-]+", "_", s).strip("_") or "person"  # D’Andra -> D_Andra


def _tag(label: str) -> str:
    # "Final" -> "Final"; "Final Rev 3" -> "Final_Rev3"; "Preliminary" -> "Preliminary"
    return label.replace(" Rev ", "_Rev")


def agent_pdf(name: str, week_no: int, pay_date, label: str = "Final") -> str:
    suffix = "" if label == "Final" else f"_{_tag(label)}"
    return f"{slug(name)}_Week{week_no}{suffix}_{pay_date.isoformat()}.pdf"


def package(kind: str, week_no: int, pay_date, label: str, ext: str) -> str:
    return f"EMG_Week{week_no}_{_tag(label)}_{kind}_{pay_date.isoformat()}.{ext}"

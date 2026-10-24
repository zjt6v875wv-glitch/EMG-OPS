"""Geocoding with a cache and explicit confidence. Locations are never invented:
an address that does not match stays unmatched (no ZIP-centroid guessing)."""
from __future__ import annotations

import csv
import io
import os
import re
import urllib.request
from dataclasses import dataclass

from ..names import clean


def address_key(address: str, city: str = "", state: str = "", zip_code: str = "") -> str:
    full = " ".join(x for x in (address, city, state, (zip_code or "")[:5]) if x)
    s = re.sub(r"#\s*\w+", " ", clean(full).upper())
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(APT|UNIT|SPC|SP|STE|SUITE|LOT|TRLR)\b\s*([A-Z]?\d+[A-Z]?|[A-Z])\b", " ", s)  # unit numbers do not change the rooftop
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class GeoResult:
    key: str
    query: str
    lat: float | None
    lng: float | None
    confidence: float | None
    match_type: str          # rooftop | street | none
    status: str              # matched | ambiguous | unmatched
    provider: str
    candidates: list


class CensusProvider:
    """US Census batch geocoder (free, no API key, up to 10,000 addresses per request)."""

    name = "census"
    url = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"

    def geocode(self, items: list[tuple[str, str, str, str, str]]) -> list[GeoResult]:
        """items: (key, street, city, state, zip)"""
        buf = io.StringIO()
        w = csv.writer(buf)
        for i, (k, street, city, state, z) in enumerate(items):
            w.writerow([i, street, city, state, (z or "")[:5]])
        boundary = "----emgboundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\nContent-Type: text/csv\r\n\r\n{buf.getvalue()}\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("GEOCODE_TIMEOUT", "300"))) as r:
            text = r.read().decode("utf-8", "replace")
        out = {}
        for row in csv.reader(io.StringIO(text)):
            if not row or not row[0].isdigit():
                continue
            i = int(row[0])
            k, street, city, state, z = items[i]
            q = ", ".join(x for x in (street, city, state, z) if x)
            if len(row) > 5 and row[2] == "Match":
                lng, lat = (float(v) for v in row[5].split(","))
                exact = row[3] == "Exact"
                out[i] = GeoResult(k, q, lat, lng, 0.95 if exact else 0.75, "street", "matched" if exact else "ambiguous", self.name, [{"matched": row[4]}])
            else:
                out[i] = GeoResult(k, q, None, None, None, "none", "unmatched", self.name, [])
        return [out.get(i) or GeoResult(items[i][0], items[i][1], None, None, None, "none", "unmatched", self.name, []) for i in range(len(items))]


def get_provider():
    name = os.environ.get("GEOCODER", "census")
    if name == "census":
        return CensusProvider()
    raise ValueError(f"Unknown geocoder {name}")

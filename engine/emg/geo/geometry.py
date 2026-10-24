"""Minimal polygon geometry (no GEOS dependency): point-in-polygon and territory file parsing.

Coordinates are GeoJSON order: [lng, lat].
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET


def _ring_contains(ring: list, x: float, y: float) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-300) + x1:
            inside = not inside
    return inside


def contains(geometry: dict, lng: float, lat: float) -> bool:
    t = geometry.get("type")
    polys = [geometry["coordinates"]] if t == "Polygon" else geometry["coordinates"] if t == "MultiPolygon" else []
    for poly in polys:
        if poly and _ring_contains(poly[0], lng, lat) and not any(_ring_contains(h, lng, lat) for h in poly[1:]):
            return True
    return False


def bbox(geometry: dict) -> tuple[float, float, float, float]:
    pts = [p for poly in ([geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]) for ring in poly for p in ring]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _valid(geom: dict) -> dict:
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Territories must be polygons, got {geom.get('type')}")
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        for ring in poly:
            if len(ring) < 4:
                raise ValueError("Polygon ring needs at least 4 points (closed)")
            for x, y, *_ in ring:
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    raise ValueError(f"Coordinate out of range: {x}, {y}")
    return geom


def parse_geojson(text: str) -> list[dict]:
    data = json.loads(text)
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data] if data.get("type") == "Feature" else [{"type": "Feature", "geometry": data, "properties": {}}]
    out = []
    for i, f in enumerate(feats, 1):
        props = f.get("properties") or {}
        out.append({"name": str(props.get("name") or props.get("Name") or props.get("NAME") or f"Territory {i}"), "geometry": _valid(f["geometry"]), "properties": props})
    return out


def parse_kml(text: str) -> list[dict]:
    root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    ns = {"k": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"k": ""}
    q = (lambda t: f"k:{t}") if ns["k"] else (lambda t: t)
    out = []
    for i, pm in enumerate(root.iter(f"{{{ns['k']}}}Placemark" if ns["k"] else "Placemark"), 1):
        name_el = pm.find(q("name"), ns) if ns["k"] else pm.find("name")
        polys = []
        for poly in pm.iter(f"{{{ns['k']}}}Polygon" if ns["k"] else "Polygon"):
            rings = []
            for tag in ("outerBoundaryIs", "innerBoundaryIs"):
                for b in poly.iter(f"{{{ns['k']}}}{tag}" if ns["k"] else tag):
                    coords = b.find(f".//{q('coordinates')}", ns) if ns["k"] else b.find(".//coordinates")
                    if coords is not None and coords.text:
                        rings.append([[float(v) for v in c.split(",")[:2]] for c in coords.text.split()])
            if rings:
                polys.append(rings)
        if polys:
            geom = {"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1 else {"type": "MultiPolygon", "coordinates": polys}
            out.append({"name": (name_el.text or "").strip() if name_el is not None and name_el.text else f"Territory {i}", "geometry": _valid(geom), "properties": {}})
    if not out:
        raise ValueError("No polygons found in the KML file")
    return out


def parse_csv(text: str) -> list[dict]:
    """CSV with either a WKT column (name, wkt) or vertex rows (name, lat, lng[, order])."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("Empty CSV")
    cols = {c.lower().strip(): c for c in rows[0]}
    name_c = cols.get("name") or cols.get("territory") or cols.get("project")
    if not name_c:
        raise ValueError("CSV needs a name (or territory / project) column")
    if "wkt" in cols:
        return [{"name": r[name_c], "geometry": _valid(_wkt(r[cols["wkt"]])), "properties": {}} for r in rows]
    lat_c, lng_c = cols.get("lat") or cols.get("latitude"), cols.get("lng") or cols.get("lon") or cols.get("longitude")
    if not (lat_c and lng_c):
        raise ValueError("CSV needs a WKT column, or lat and lng columns with one row per vertex")
    groups: dict[str, list] = {}
    for i, r in enumerate(rows):
        groups.setdefault(r[name_c], []).append((float(r[cols["order"]]) if "order" in cols else i, float(r[lng_c]), float(r[lat_c])))
    out = []
    for name, pts in groups.items():
        ring = [[x, y] for _, x, y in sorted(pts)]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        out.append({"name": name, "geometry": _valid({"type": "Polygon", "coordinates": [ring]}), "properties": {}})
    return out


def _wkt(s: str) -> dict:
    s = s.strip()
    nums = lambda part: [[float(a), float(b)] for a, b in (p.split()[:2] for p in part.split(","))]  # noqa: E731
    if s.upper().startswith("POLYGON"):
        rings = re.findall(r"\(([^()]+)\)", s)
        return {"type": "Polygon", "coordinates": [nums(r) for r in rings]}
    if s.upper().startswith("MULTIPOLYGON"):
        polys = re.findall(r"\(\(([^()]+(?:\),\s*\([^()]+)*)\)\)", s)
        return {"type": "MultiPolygon", "coordinates": [[nums(r) for r in re.split(r"\),\s*\(", p)] for p in polys]}
    raise ValueError("WKT must be POLYGON or MULTIPOLYGON")


def parse_territories(file_name: str, data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig")
    ext = file_name.lower().rsplit(".", 1)[-1]
    if ext in ("geojson", "json"):
        return parse_geojson(text)
    if ext == "kml":
        return parse_kml(text)
    if ext == "csv":
        return parse_csv(text)
    raise ValueError("Territory files must be KML, GeoJSON or CSV")

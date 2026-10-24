"""API connectors. Each returns records keyed by target field, which go through the
same status sync as portal exports, so switching a vendor from portal to API is a
configuration change.

To add a vendor API: write a function ``fetch(config, creds, since) -> list[dict]``
and register it in PROVIDERS.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date


def http_json(config: dict, creds: dict, since: date) -> list[dict]:
    """Generic JSON endpoint: GET {url}?{since_param}=YYYY-MM-DD with a bearer token.
    ``records_path`` is a dotted path to the list (e.g. "data.enrollments");
    ``mapping`` (target -> JSON key) is applied by the sync."""
    url = config["url"]
    if not url.startswith("https://"):
        raise ValueError("API connectors must use https")
    if config.get("since_param"):
        url += ("&" if "?" in url else "?") + f"{config['since_param']}={since.isoformat()}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {creds.get('token', '')}", "Accept": "application/json"})
    out, next_url = [], url
    while next_url:
        req.full_url = next_url
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
        rows = body
        for part in (config.get("records_path") or "").split("."):
            if part:
                rows = rows[part]
        out += rows
        next_url = body.get(config["next_path"]) if config.get("next_path") and isinstance(body, dict) else None
    return out


PROVIDERS = {"http_json": http_json}

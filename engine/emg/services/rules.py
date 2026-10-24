"""Rule-set versions: draft -> preview -> activate, all audited."""
from __future__ import annotations

import json

from ..db.conn import J, audit
from ..rules.model import load_ruleset


def active(c):
    row = c.execute("select * from rule_sets where status='active'").fetchone()
    if not row:
        raise LookupError("No active rule set. Import the EMG Payroll Control workbook or load a config first.")
    return row


def by_version(c, version: int):
    row = c.execute("select * from rule_sets where version=%s", (version,)).fetchone()
    if not row:
        raise LookupError(f"Rule set v{version} not found")
    return row


def ruleset(c, version: int | None = None):
    row = by_version(c, version) if version else active(c)
    rs = load_ruleset(row["config"])
    rs.version = str(row["version"])
    return rs, row


def create(c, config: dict, label: str, actor: str, note: str = "", parent: int | None = None, status: str = "draft") -> dict:
    load_ruleset(config)  # validate structure before saving
    nxt = c.execute("select coalesce(max(version),0)+1 v from rule_sets").fetchone()["v"]
    row = c.execute(
        "insert into rule_sets(version,label,config,status,parent_version,change_note,created_by) values (%s,%s,%s,%s,%s,%s,%s) returning *",
        (nxt, label, J(config), status, parent, note, actor),
    ).fetchone()
    audit(c, actor, "rules.create", "rule_set", str(nxt), None, {"label": label, "status": status}, note)
    return row


def update_draft(c, version: int, config: dict, actor: str, note: str = "") -> dict:
    row = by_version(c, version)
    if row["status"] != "draft":
        raise ValueError("Only drafts can be edited; create a new draft from the active rules")
    load_ruleset(config)
    c.execute("update rule_sets set config=%s, change_note=%s where version=%s", (J(config), note or row["change_note"], version))
    audit(c, actor, "rules.update_draft", "rule_set", str(version), row["config"], config, note)
    return by_version(c, version)


def activate(c, version: int, actor: str, note: str) -> dict:
    if not note:
        raise ValueError("Activation needs a change note")
    row = by_version(c, version)
    if row["status"] != "draft":
        raise ValueError("Only drafts can be activated")
    prev = c.execute("select version, config from rule_sets where status='active' for update").fetchone()
    if prev:
        c.execute("update rule_sets set status='superseded' where version=%s", (prev["version"],))
    c.execute("update rule_sets set status='active', activated_by=%s, activated_at=now(), change_note=%s where version=%s", (actor, note, version))
    audit(c, actor, "rules.activate", "rule_set", str(version), {"previous_version": prev and prev["version"]}, {"version": version}, note)
    return by_version(c, version)


def diff(old: dict, new: dict) -> list[dict]:
    """Rule-level diff by section for the change preview."""
    out = []
    for section in sorted(set(old) | set(new)):
        a, b = old.get(section), new.get(section)
        if a == b:
            continue
        if isinstance(a, list) and isinstance(b, list):
            sa = {json.dumps(x, sort_keys=True) for x in a}
            sb = {json.dumps(x, sort_keys=True) for x in b}
            for x in sorted(sa - sb):
                out.append({"section": section, "change": "removed", "rule": json.loads(x)})
            for x in sorted(sb - sa):
                out.append({"section": section, "change": "added", "rule": json.loads(x)})
        else:
            out.append({"section": section, "change": "changed", "before": a, "after": b})
    return out

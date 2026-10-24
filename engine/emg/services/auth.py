"""Users, sessions and role-based access.

Roles: owner > admin > payroll > manager > agent. Managers see their own
team's results; agents see only their own statements. Passwords are bcrypt;
session tokens are random and stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from ..db.conn import audit

ROLES = ("owner", "admin", "payroll", "manager", "agent")
SESSION_HOURS = 12

# capability -> roles allowed
PERMS = {
    "users.manage": {"owner", "admin"},
    "rules.edit": {"owner", "admin", "payroll"},
    "rules.activate": {"owner", "admin"},
    "imports.manage": {"owner", "admin", "payroll"},
    "review.resolve": {"owner", "admin", "payroll"},
    "adjustments.manage": {"owner", "admin", "payroll"},
    "payroll.calculate": {"owner", "admin", "payroll"},
    "payroll.finalize": {"owner", "admin", "payroll"},
    "payroll.correct": {"owner", "admin"},
    "payments.manage": {"owner", "admin", "payroll"},
    "people.manage": {"owner", "admin", "payroll"},
    "people.merge": {"owner", "admin"},
    "payroll.view_all": {"owner", "admin", "payroll"},
    "ledger.view_all": {"owner", "admin", "payroll"},
    "reports.build": {"owner", "admin", "payroll"},
    "email.send": {"owner", "admin", "payroll"},
    "map.view": {"owner", "admin", "payroll", "manager"},
    "map.view_all": {"owner", "admin", "payroll"},
    "territories.manage": {"owner", "admin"},
    "leaderboards.view": set(ROLES),
    "leaderboards.publish": {"owner", "admin"},
    "audit.view": {"owner", "admin"},
}


def can(user: dict, perm: str) -> bool:
    return user["role"] in PERMS.get(perm, set())


def hash_password(pw: str) -> str:
    if len(pw) < 10:
        raise ValueError("Passwords need at least 10 characters")
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def create_user(c, email: str, name: str, role: str, password: str, actor: str, person_name: str | None = None) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if role in ("manager", "agent") and not person_name:
        raise ValueError("Manager and agent logins must be linked to their payroll name")
    u = c.execute("insert into users(email,name,role,person_name,password_hash) values (%s,%s,%s,%s,%s) returning id,email,name,role,person_name,active",
                  (email, name, role, person_name, hash_password(password))).fetchone()
    audit(c, actor, "user.create", "user", str(u["id"]), None, {"email": email, "role": role, "person_name": person_name})
    return u


def login(c, email: str, password: str) -> tuple[str, dict] | None:
    u = c.execute("select * from users where email=%s and active", (email,)).fetchone()
    if not u or not bcrypt.checkpw(password.encode(), u["password_hash"].encode()):
        audit(c, email, "auth.login_failed", "user", None)
        return None
    token = secrets.token_urlsafe(32)
    c.execute("insert into sessions(token_hash,user_id,expires_at) values (%s,%s,%s)",
              (hashlib.sha256(token.encode()).hexdigest(), u["id"], datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)))
    audit(c, u["email"], "auth.login", "user", str(u["id"]))
    return token, {k: u[k] for k in ("id", "email", "name", "role", "person_name")}


def user_for_token(c, token: str) -> dict | None:
    return c.execute(
        "select u.id, u.email, u.name, u.role, u.person_name from sessions s join users u on u.id=s.user_id"
        " where s.token_hash=%s and s.expires_at > now() and u.active", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()


def logout(c, token: str) -> None:
    c.execute("delete from sessions where token_hash=%s", (hashlib.sha256(token.encode()).hexdigest(),))


def team_of(c, manager: str) -> set[str]:
    """People a manager may see: themselves plus sellers in their active memberships."""
    from . import rules as rules_svc

    try:
        rs, _ = rules_svc.ruleset(c)
    except LookupError:
        return {manager}
    return {manager} | {m.agent for m in rs.memberships if m.manager == manager}


def visible_people(c, user: dict) -> set[str] | None:
    """None = everyone."""
    if can(user, "payroll.view_all"):
        return None
    if user["role"] == "manager":
        return team_of(c, user["person_name"])
    return {user["person_name"]}

"""Thin psycopg helpers. All money columns come back as Decimal."""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).resolve().parents[3] / "db" / "migrations"


def dsn() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://emg:emg@localhost:5432/emg")


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


def J(value: Any) -> Jsonb:
    """Wrap a Python value for a jsonb column (Decimals become strings)."""
    return Jsonb(json.loads(json.dumps(value, default=_default)))


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(url or dsn(), row_factory=dict_row, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(url: str | None = None) -> list[str]:
    applied = []
    with connect(url) as c:
        c.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())")
        done = {r["name"] for r in c.execute("select name from schema_migrations").fetchall()}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            c.execute(f.read_text())
            c.execute("insert into schema_migrations(name) values (%s)", (f.name,))
            applied.append(f.name)
    return applied


def audit(c: psycopg.Connection, actor: str, action: str, entity: str, entity_id: str | None, before=None, after=None, note: str | None = None) -> None:
    c.execute(
        "insert into audit_log(actor, action, entity, entity_id, before, after, note) values (%s,%s,%s,%s,%s,%s,%s)",
        (actor, action, entity, entity_id, J(before) if before is not None else None, J(after) if after is not None else None, note),
    )


if __name__ == "__main__":
    print(migrate() or "up to date")

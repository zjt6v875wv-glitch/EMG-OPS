import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from emg.model import SaleRow  # noqa: E402
from emg.rules.model import load_ruleset  # noqa: E402

SAMPLE = ROOT / "config/sample/rules.sample.json"
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://emg:emg@localhost:5432/emg_test")


@pytest.fixture
def cfg():
    import json

    return json.loads(SAMPLE.read_text())


@pytest.fixture
def rs(cfg):
    return load_ruleset(cfg)


def sale(vendor="Nexamp", ref="E1", rep="Alex Rivera", kwh="1000", product="LMI", utility="SOUTH ELECTRIC", line_type="payable",
         amount="0", sale_date=date(2026, 3, 2), **kw):
    return SaleRow(vendor=vendor, enrollment_id=ref, raw_rep=rep, kwh=Decimal(kwh), product=product, utility=utility,
                   line_type=line_type, vendor_amount=Decimal(amount), sale_date=sale_date, **kw)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh schema in the test database for each test."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(TEST_DB, autocommit=True)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    conn.execute("drop schema if exists public cascade; create schema public")
    conn.execute("create extension if not exists pgcrypto; create extension if not exists citext")
    conn.close()
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    from emg.db.conn import migrate

    migrate(TEST_DB)
    return TEST_DB

"""
Tests run against a real PostgreSQL database (per the assignment's
requirement to not use an in-memory replacement).

How to run:
    docker compose up -d
    createdb -h localhost -U reservation reservation_test   # or see README
    TEST_DATABASE_URL=postgresql://reservation:reservation@localhost:5432/reservation_test \
        pytest

IMPORTANT: this file sets os.environ["DATABASE_URL"] *before* importing
anything from `app`, because app/db.py opens its connection pool at import
time using that variable. This must stay the first thing that happens.
"""
import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://reservation:reservation@localhost:5432/reservation_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pathlib

import psycopg
import pytest
from fastapi.testclient import TestClient

MIGRATIONS_DIR = pathlib.Path(__file__).parent.parent / "migrations"

TABLES = ["reservation_items", "reservations", "stock", "products", "warehouses"]


def _run_migrations(conn):
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text())
    conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        _run_migrations(conn)
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate everything before each test so tests are independent."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def seed(client):
    """Creates one warehouse and one product, returns their ids."""
    w = client.post("/warehouses", params={"name": "Main WH"}).json()
    p = client.post("/products", params={"sku": "SKU-1", "name": "Widget"}).json()
    return {"warehouse_id": w["id"], "product_id": p["id"]}

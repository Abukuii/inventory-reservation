"""
All SQL lives here. Business-rule *decisions* (which errors to raise, when)
live in service.py; this module is deliberately "dumb" about business
meaning beyond what's needed to make the queries correct and safe.

Concurrency strategy (see README for the full writeup):

  Reservation creation locks the `stock` row for every requested product,
  in ascending product_id order, using SELECT ... FOR UPDATE, before
  computing available quantity. Two transactions that both want to reserve
  product 42 will serialize on that row's lock: the second transaction's
  "how much is currently reserved" read is guaranteed to happen only after
  the first transaction has committed (and its reservation_items are
  visible) or rolled back (and nothing changed). This is what prevents
  overselling under concurrent requests, without decrementing a counter.

  Idempotency is enforced by a UNIQUE index on reservations.idempotency_key.
  The "check first" read below is an optimization to avoid unnecessary
  locking on the common path; the actual guarantee comes from catching the
  unique-violation on INSERT and re-reading, because two requests with the
  same key can reach the "check first" step at the same time.
"""
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

RESERVATION_TTL = timedelta(minutes=15)


def _now(conn) -> datetime:
    """
    Database server time, not app-server time. The assignment requires the
    database to be the source of truth for expiration, so we never compare
    against Python's `datetime.now()` -- app servers can have clock drift
    relative to each other and relative to Postgres.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

def add_stock(conn, warehouse_id: int, product_id: int, quantity: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stock (warehouse_id, product_id, physical_quantity, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (warehouse_id, product_id)
            DO UPDATE SET
                physical_quantity = stock.physical_quantity + EXCLUDED.physical_quantity,
                updated_at = now()
            """,
            (warehouse_id, product_id, quantity),
        )


def get_reserved_quantity(conn, warehouse_id: int, product_id: int) -> int:
    """
    Sum of quantity held by reservations that currently occupy stock:
      - 'pending' reservations that have not yet expired, and
      - 'confirmed' reservations (confirmation removes the expiry check,
        but does NOT release the hold -- a confirmed reservation is an
        actual commitment, e.g. a paid order, until something explicitly
        cancels/fulfills it).
    'cancelled' and 'expired' never count.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(ri.quantity), 0)
            FROM reservation_items ri
            JOIN reservations r ON r.id = ri.reservation_id
            WHERE r.warehouse_id = %s
              AND ri.product_id = %s
              AND (
                    r.status = 'confirmed'
                    OR (r.status = 'pending' AND r.expires_at > now())
                  )
            """,
            (warehouse_id, product_id),
        )
        return cur.fetchone()[0]


def get_stock(conn, warehouse_id: int, product_id: int) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT physical_quantity FROM stock WHERE warehouse_id = %s AND product_id = %s",
            (warehouse_id, product_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    reserved = get_reserved_quantity(conn, warehouse_id, product_id)
    return {
        "warehouse_id": warehouse_id,
        "product_id": product_id,
        "physical_quantity": row["physical_quantity"],
        "available_quantity": row["physical_quantity"] - reserved,
    }


def lock_stock_rows(conn, warehouse_id: int, product_ids: list[int]) -> dict[int, int]:
    """
    Locks the stock rows for the given products, in ascending product_id
    order (deadlock avoidance for multi-item reservations), and returns
    {product_id: physical_quantity}. Missing rows are simply absent from
    the returned dict -- callers treat "no stock row" as zero stock.
    """
    if not product_ids:
        return {}
    ordered = sorted(set(product_ids))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT product_id, physical_quantity
            FROM stock
            WHERE warehouse_id = %s AND product_id = ANY(%s)
            ORDER BY product_id
            FOR UPDATE
            """,
            (warehouse_id, ordered),
        )
        rows = cur.fetchall()
    return {r["product_id"]: r["physical_quantity"] for r in rows}


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

def find_reservation_by_idempotency_key(conn, idempotency_key: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM reservations WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return get_reservation(conn, row["id"])


def insert_reservation(
    conn, warehouse_id: int, idempotency_key: str, items: list[dict]
) -> int:
    """
    Inserts the reservation + its items. Raises psycopg.errors.UniqueViolation
    if idempotency_key already exists (caller handles the race).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reservations (warehouse_id, status, idempotency_key, created_at, expires_at)
            VALUES (%s, 'pending', %s, now(), now() + %s)
            RETURNING id
            """,
            (warehouse_id, idempotency_key, RESERVATION_TTL),
        )
        reservation_id = cur.fetchone()[0]

        cur.executemany(
            """
            INSERT INTO reservation_items (reservation_id, product_id, quantity)
            VALUES (%s, %s, %s)
            """,
            [(reservation_id, item["product_id"], item["quantity"]) for item in items],
        )
    return reservation_id


def get_reservation(conn, reservation_id: int) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, warehouse_id, status, created_at, expires_at, confirmed_at, cancelled_at
            FROM reservations
            WHERE id = %s
            """,
            (reservation_id,),
        )
        reservation = cur.fetchone()
        if reservation is None:
            return None

        cur.execute(
            "SELECT product_id, quantity FROM reservation_items WHERE reservation_id = %s ORDER BY product_id",
            (reservation_id,),
        )
        reservation["items"] = cur.fetchall()

    return reservation


def lock_reservation_row(conn, reservation_id: int) -> dict | None:
    """Locks the reservation row for a confirm/cancel state transition."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, warehouse_id, status, created_at, expires_at, confirmed_at, cancelled_at
            FROM reservations
            WHERE id = %s
            FOR UPDATE
            """,
            (reservation_id,),
        )
        return cur.fetchone()


def mark_expired_if_due(conn, reservation: dict) -> dict:
    """
    Lazily persists status='expired' for a pending reservation whose
    expires_at has passed. Must be called with the row already locked
    (see lock_reservation_row) to avoid a lost-update race with a
    concurrent confirm/cancel.
    """
    if reservation["status"] != "pending":
        return reservation
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
    if reservation["expires_at"] <= now:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = 'expired' WHERE id = %s AND status = 'pending'",
                (reservation["id"],),
            )
        reservation["status"] = "expired"
    return reservation


def set_status(conn, reservation_id: int, new_status: str, timestamp_column: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE reservations
            SET status = %s, {timestamp_column} = now()
            WHERE id = %s
            """,
            (new_status, reservation_id),
        )

"""
Business logic. Every function here owns exactly one transaction
(via db.get_connection()) so "all or nothing" is enforced by Postgres,
not by try/except cleanup in application code.
"""
from psycopg import errors as pg_errors

from app import repository as repo
from app.db import get_connection
from app.exceptions import (
    InsufficientStockError,
    InvalidTransitionError,
    NotFoundError,
)

# States from which each transition is legal. Anything else -> InvalidTransitionError.
CONFIRMABLE_FROM = {"pending"}
CANCELLABLE_FROM = {"pending"}


def add_stock(warehouse_id: int, product_id: int, quantity: int) -> dict:
    with get_connection() as conn:
        try:
            repo.add_stock(conn, warehouse_id, product_id, quantity)
        except pg_errors.ForeignKeyViolation:
            raise NotFoundError("warehouse or product does not exist")
        return repo.get_stock(conn, warehouse_id, product_id)


def get_stock(warehouse_id: int, product_id: int) -> dict:
    with get_connection() as conn:
        stock = repo.get_stock(conn, warehouse_id, product_id)
    if stock is None:
        raise NotFoundError(
            f"no stock record for product_id={product_id} in warehouse_id={warehouse_id}"
        )
    return stock


def create_reservation(warehouse_id: int, items: list[dict], idempotency_key: str) -> dict:
    """
    "All or nothing": stock is locked and checked for every item before
    any reservation row is written. If any single item lacks availability,
    the whole transaction rolls back and nothing is reserved.
    """
    with get_connection() as conn:
        # Fast path: this exact request has already succeeded before.
        existing = repo.find_reservation_by_idempotency_key(conn, idempotency_key)
        if existing is not None:
            return existing

        product_ids = [item["product_id"] for item in items]
        physical_by_product = repo.lock_stock_rows(conn, warehouse_id, product_ids)

        for item in items:
            product_id = item["product_id"]
            physical = physical_by_product.get(product_id, 0)
            reserved = repo.get_reserved_quantity(conn, warehouse_id, product_id)
            available = physical - reserved
            if available < item["quantity"]:
                # Whole transaction rolls back on exception -- no partial reservation.
                raise InsufficientStockError(product_id)

        try:
            reservation_id = repo.insert_reservation(conn, warehouse_id, idempotency_key, items)
        except pg_errors.UniqueViolation:
            # Lost the race: another concurrent request with the same
            # idempotency_key committed first. This is not an error from
            # the client's point of view -- return what actually got created.
            conn.rollback()
            with get_connection() as conn2:
                return repo.find_reservation_by_idempotency_key(conn2, idempotency_key)

        return repo.get_reservation(conn, reservation_id)


def get_reservation(reservation_id: int) -> dict:
    with get_connection() as conn:
        reservation = repo.lock_reservation_row(conn, reservation_id)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id} not found")
        reservation = repo.mark_expired_if_due(conn, reservation)
        return repo.get_reservation(conn, reservation_id)


def confirm_reservation(reservation_id: int) -> dict:
    with get_connection() as conn:
        reservation = repo.lock_reservation_row(conn, reservation_id)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id} not found")

        reservation = repo.mark_expired_if_due(conn, reservation)

        if reservation["status"] == "confirmed":
            # Idempotent: repeating a confirm on an already-confirmed
            # reservation is a success, not an error.
            return repo.get_reservation(conn, reservation_id)

        if reservation["status"] not in CONFIRMABLE_FROM:
            raise InvalidTransitionError(
                f"cannot confirm reservation in status={reservation['status']}"
            )

        repo.set_status(conn, reservation_id, "confirmed", "confirmed_at")
        return repo.get_reservation(conn, reservation_id)


def cancel_reservation(reservation_id: int) -> dict:
    with get_connection() as conn:
        reservation = repo.lock_reservation_row(conn, reservation_id)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id} not found")

        reservation = repo.mark_expired_if_due(conn, reservation)

        if reservation["status"] == "cancelled":
            return repo.get_reservation(conn, reservation_id)

        if reservation["status"] not in CANCELLABLE_FROM:
            raise InvalidTransitionError(
                f"cannot cancel reservation in status={reservation['status']}"
            )

        repo.set_status(conn, reservation_id, "cancelled", "cancelled_at")
        return repo.get_reservation(conn, reservation_id)

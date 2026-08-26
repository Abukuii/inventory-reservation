import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

TEST_DATABASE_URL = os.environ["DATABASE_URL"]  # set by conftest.py before this module loads


def _idem():
    return str(uuid.uuid4())


def _add_stock(client, warehouse_id, product_id, qty):
    r = client.post(
        f"/warehouses/{warehouse_id}/stock",
        json={"product_id": product_id, "quantity": qty},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_product(client, sku):
    return client.post("/products", params={"sku": sku, "name": sku}).json()["id"]


# ---------------------------------------------------------------------------
# Multi-item "all or nothing"
# ---------------------------------------------------------------------------

def test_create_valid_multi_item_reservation(client, seed):
    wh = seed["warehouse_id"]
    p1 = seed["product_id"]
    p2 = _create_product(client, "SKU-2")

    _add_stock(client, wh, p1, 10)
    _add_stock(client, wh, p2, 10)

    r = client.post(
        "/reservations",
        json={
            "warehouse_id": wh,
            "items": [
                {"product_id": p1, "quantity": 3},
                {"product_id": p2, "quantity": 4},
            ],
            "idempotency_key": _idem(),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert {i["product_id"]: i["quantity"] for i in body["items"]} == {p1: 3, p2: 4}

    # Available stock reflects the reservation for both products.
    s1 = client.get(f"/warehouses/{wh}/products/{p1}/stock").json()
    s2 = client.get(f"/warehouses/{wh}/products/{p2}/stock").json()
    assert s1["available_quantity"] == 7
    assert s2["available_quantity"] == 6


def test_reservation_rejected_when_one_item_lacks_stock_reserves_nothing(client, seed):
    wh = seed["warehouse_id"]
    p1 = seed["product_id"]
    p2 = _create_product(client, "SKU-2")

    _add_stock(client, wh, p1, 10)
    _add_stock(client, wh, p2, 2)  # not enough for the request below

    r = client.post(
        "/reservations",
        json={
            "warehouse_id": wh,
            "items": [
                {"product_id": p1, "quantity": 3},
                {"product_id": p2, "quantity": 5},  # exceeds available (2)
            ],
            "idempotency_key": _idem(),
        },
    )
    assert r.status_code == 409, r.text

    # Neither product should show any reservation impact.
    s1 = client.get(f"/warehouses/{wh}/products/{p1}/stock").json()
    s2 = client.get(f"/warehouses/{wh}/products/{p2}/stock").json()
    assert s1["available_quantity"] == 10
    assert s2["available_quantity"] == 2


# ---------------------------------------------------------------------------
# Confirm / cancel
# ---------------------------------------------------------------------------

def test_confirm_reservation(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    res = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": _idem()},
    ).json()

    r = client.post(f"/reservations/{res['id']}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"
    assert r.json()["confirmed_at"] is not None

    # Confirmed reservations still hold stock (available stays reduced) --
    # only cancel/expiry releases it. See README for the state model.
    stock = client.get(f"/warehouses/{wh}/products/{p}/stock").json()
    assert stock["available_quantity"] == 3


def test_cancel_reservation_releases_stock(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    res = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": _idem()},
    ).json()

    r = client.post(f"/reservations/{res['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    stock = client.get(f"/warehouses/{wh}/products/{p}/stock").json()
    assert stock["available_quantity"] == 5  # fully released


def test_cancel_after_confirm_is_invalid_transition(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    res = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": _idem()},
    ).json()
    client.post(f"/reservations/{res['id']}/confirm")

    r = client.post(f"/reservations/{res['id']}/cancel")
    assert r.status_code == 409
    assert client.get(f"/reservations/{res['id']}").json()["status"] == "confirmed"


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------

def _force_expire(reservation_id):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE reservations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (reservation_id,),
        )


def test_confirmation_rejected_after_expiration(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    res = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": _idem()},
    ).json()

    _force_expire(res["id"])

    r = client.post(f"/reservations/{res['id']}/confirm")
    assert r.status_code == 409, r.text

    # Expired reservations no longer reduce available stock.
    stock = client.get(f"/warehouses/{wh}/products/{p}/stock").json()
    assert stock["available_quantity"] == 5

    assert client.get(f"/reservations/{res['id']}").json()["status"] == "expired"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_repeating_idempotent_create_request_returns_same_reservation(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    key = _idem()
    body = {"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": key}

    r1 = client.post("/reservations", json=body)
    r2 = client.post("/reservations", json=body)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    # Stock was only reserved once, not twice.
    stock = client.get(f"/warehouses/{wh}/products/{p}/stock").json()
    assert stock["available_quantity"] == 3


# ---------------------------------------------------------------------------
# Concurrency: overselling must be impossible
# ---------------------------------------------------------------------------

def test_concurrent_reservations_do_not_oversell(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)

    def attempt(_):
        return client.post(
            "/reservations",
            json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 3}], "idempotency_key": _idem()},
        )

    # Two requests for qty=3 each against a stock of 5: at most one can win.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]

    stock = client.get(f"/warehouses/{wh}/products/{p}/stock").json()
    assert stock["available_quantity"] == 2  # never negative, exactly one reservation won


# ---------------------------------------------------------------------------
# Invalid input / invalid transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_stock_quantity_rejected(client, seed, quantity):
    wh, p = seed["warehouse_id"], seed["product_id"]
    r = client.post(f"/warehouses/{wh}/stock", json={"product_id": p, "quantity": quantity})
    assert r.status_code == 422  # pydantic validation


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_reservation_quantity_rejected(client, seed, quantity):
    wh, p = seed["warehouse_id"], seed["product_id"]
    r = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": quantity}], "idempotency_key": _idem()},
    )
    assert r.status_code == 422


def test_confirm_unknown_reservation_returns_404(client):
    r = client.post("/reservations/999999/confirm")
    assert r.status_code == 404


def test_confirm_already_cancelled_reservation_is_invalid_transition(client, seed):
    wh, p = seed["warehouse_id"], seed["product_id"]
    _add_stock(client, wh, p, 5)
    res = client.post(
        "/reservations",
        json={"warehouse_id": wh, "items": [{"product_id": p, "quantity": 2}], "idempotency_key": _idem()},
    ).json()
    client.post(f"/reservations/{res['id']}/cancel")

    r = client.post(f"/reservations/{res['id']}/confirm")
    assert r.status_code == 409

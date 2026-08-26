from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import errors as pg_errors

from app import service
from app.exceptions import DomainError
from app.schemas import (
    AddStockRequest,
    CreateReservationRequest,
    ReservationResponse,
    StockResponse,
)

app = FastAPI(title="Inventory Reservation Service (Python prototype)")


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})


@app.exception_handler(pg_errors.ForeignKeyViolation)
async def fk_error_handler(request: Request, exc: pg_errors.ForeignKeyViolation):
    return JSONResponse(status_code=404, content={"error": "referenced entity does not exist"})


# --- Minimal setup endpoints (not in the spec's "at minimum" list, but the
#     spec assumes products/warehouses already exist somewhere) --------------

@app.post("/products", status_code=201)
def create_product(sku: str, name: str):
    from app.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO products (sku, name) VALUES (%s, %s) RETURNING id",
                (sku, name),
            )
            return {"id": cur.fetchone()[0], "sku": sku, "name": name}


@app.post("/warehouses", status_code=201)
def create_warehouse(name: str):
    from app.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO warehouses (name) VALUES (%s) RETURNING id",
                (name,),
            )
            return {"id": cur.fetchone()[0], "name": name}


# --- Required endpoints ------------------------------------------------------

@app.post("/warehouses/{warehouse_id}/stock", response_model=StockResponse)
def add_stock(warehouse_id: int, body: AddStockRequest):
    result = service.add_stock(warehouse_id, body.product_id, body.quantity)
    return result


@app.get(
    "/warehouses/{warehouse_id}/products/{product_id}/stock",
    response_model=StockResponse,
)
def get_stock(warehouse_id: int, product_id: int):
    return service.get_stock(warehouse_id, product_id)


@app.post("/reservations", response_model=ReservationResponse, status_code=201)
def create_reservation(body: CreateReservationRequest):
    items = [{"product_id": i.product_id, "quantity": i.quantity} for i in body.items]
    return service.create_reservation(body.warehouse_id, items, body.idempotency_key)


@app.get("/reservations/{reservation_id}", response_model=ReservationResponse)
def get_reservation(reservation_id: int):
    return service.get_reservation(reservation_id)


@app.post("/reservations/{reservation_id}/confirm", response_model=ReservationResponse)
def confirm_reservation(reservation_id: int):
    return service.confirm_reservation(reservation_id)


@app.post("/reservations/{reservation_id}/cancel", response_model=ReservationResponse)
def cancel_reservation(reservation_id: int):
    return service.cancel_reservation(reservation_id)

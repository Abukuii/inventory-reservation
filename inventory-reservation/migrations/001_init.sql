-- 001_init.sql
-- Core schema for the Inventory Reservation Service.
--
-- Design notes (see README.md "Database and transaction design" for full detail):
--
-- 1. We do NOT decrement `stock.physical_quantity` when a reservation is created.
--    Physical quantity always reflects what is actually in the warehouse.
--    "Reserved" quantity is derived on read from active (pending, non-expired)
--    reservation_items rows. This keeps physical stock trustworthy for anyone
--    doing a manual stock count, and makes cancel/expiry a no-op on `stock`
--    (nothing to "give back", because nothing was taken).
--
-- 2. Concurrency safety comes from row-level locking on `stock` rows
--    (SELECT ... FOR UPDATE) during reservation creation, not from
--    decrementing a counter. See app/repository.py::create_reservation.
--
-- 3. `reservations.status` is the source of truth for confirm/cancel,
--    but "expired" is partly a *computed* state: a pending reservation
--    whose expires_at has passed is treated as expired for the purpose of
--    computing available stock and for confirm/cancel checks, even before
--    any process has written status='expired' to the row. We do lazily
--    persist the 'expired' status the first time a request touches that
--    row (read or confirm/cancel attempt), purely so operators can query
--    reservations by status without recomputing time comparisons.

CREATE TABLE products (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE warehouses (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Physical stock of a product in a warehouse.
CREATE TABLE stock (
    warehouse_id        BIGINT NOT NULL REFERENCES warehouses(id),
    product_id           BIGINT NOT NULL REFERENCES products(id),
    physical_quantity     INTEGER NOT NULL DEFAULT 0 CHECK (physical_quantity >= 0),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, product_id)
);

CREATE TYPE reservation_status AS ENUM ('pending', 'confirmed', 'cancelled', 'expired');

CREATE TABLE reservations (
    id                BIGSERIAL PRIMARY KEY,
    warehouse_id       BIGINT NOT NULL REFERENCES warehouses(id),
    status              reservation_status NOT NULL DEFAULT 'pending',
    idempotency_key      TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at            TIMESTAMPTZ NOT NULL,
    confirmed_at          TIMESTAMPTZ,
    cancelled_at          TIMESTAMPTZ
);

-- Idempotency key must be unique so a retried "create reservation" request
-- can never create a second reservation. Scoped globally (not per-warehouse)
-- because the client is expected to generate a fresh UUID per logical
-- checkout attempt regardless of warehouse.
CREATE UNIQUE INDEX idx_reservations_idempotency_key ON reservations (idempotency_key);

CREATE INDEX idx_reservations_status ON reservations (status);
CREATE INDEX idx_reservations_warehouse_status ON reservations (warehouse_id, status);

CREATE TABLE reservation_items (
    id               BIGSERIAL PRIMARY KEY,
    reservation_id     BIGINT NOT NULL REFERENCES reservations(id),
    product_id          BIGINT NOT NULL REFERENCES products(id),
    quantity             INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (reservation_id, product_id)
);

-- Speeds up "how much of product X in warehouse Y is currently reserved".
CREATE INDEX idx_reservation_items_product ON reservation_items (product_id);

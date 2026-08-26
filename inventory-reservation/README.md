# Inventory Reservation Service (Python prototype)

> **Status note:** This is a *Python prototype* (FastAPI + PostgreSQL), built
> first to nail down the business rules, schema, and concurrency strategy
> in a language the author is fluent in. The take-home assignment requires
> **Go**. This repository is the design/reference version; a Go port
> (same schema, same API, same test scenarios) is the actual deliverable.
> Everything below describes this prototype; the design decisions carry
> over to the Go version unchanged.

## Setup and run

```bash
docker compose up -d                # starts Postgres on localhost:5432
pip install -r requirements.txt
psql postgresql://reservation:reservation@localhost:5432/reservation \
     -f migrations/001_init.sql
uvicorn app.main:app --reload
```

## Running tests

Tests run against a real PostgreSQL database (a separate `reservation_test`
database, wiped and re-migrated at the start of the test session, truncated
between individual tests).

```bash
createdb -h localhost -U reservation reservation_test
TEST_DATABASE_URL=postgresql://reservation:reservation@localhost:5432/reservation_test \
    pytest -v
```

## API

### Add stock
```
POST /warehouses/{warehouse_id}/stock
{ "product_id": 1, "quantity": 10 }
→ 200 { "warehouse_id": 1, "product_id": 1, "physical_quantity": 10, "available_quantity": 10 }
```
Quantity must be a positive integer (`422` otherwise). Stock is additive
(calling it twice adds up), not a set-to-value operation.

### Get stock
```
GET /warehouses/{warehouse_id}/products/{product_id}/stock
→ 200 { "warehouse_id": 1, "product_id": 1, "physical_quantity": 10, "available_quantity": 7 }
→ 404 if no stock record exists for that pair
```

### Create reservation
```
POST /reservations
{
  "warehouse_id": 1,
  "items": [{ "product_id": 1, "quantity": 2 }, { "product_id": 2, "quantity": 1 }],
  "idempotency_key": "a client-generated UUID, unique per logical checkout attempt"
}
→ 201 { "id": 5, "warehouse_id": 1, "status": "pending", "items": [...], "created_at": "...", "expires_at": "..." }
→ 409 { "error": "insufficient available stock for product_id=2" }   -- nothing was reserved
→ 422 on empty items, duplicate product_id in items, or non-positive quantity
```

**Idempotency key contract:** a required field in the request body,
1–255 chars, chosen by the client (a UUID is recommended). Sending the
same key again — with the same or even a *different* body — returns the
original reservation with `201` rather than creating a new one or
re-validating stock. Scope and limitation: the key only deduplicates
*reservation creation*; it is not checked for consistency against the
resubmitted body, so a client must treat the key as tied to one specific
request payload and not reuse it for a logically different reservation.

### Get / confirm / cancel
```
GET  /reservations/{id}          → 200 or 404
POST /reservations/{id}/confirm  → 200 (idempotent), 404, or 409 (invalid transition)
POST /reservations/{id}/cancel   → 200 (idempotent), 404, or 409 (invalid transition)
```

## Reservation state machine

```
   create
     │
     ▼
  pending ──confirm──▶ confirmed
     │                     
     ├──cancel──▶ cancelled
     │
     └──(15 min pass, lazily detected)──▶ expired
```

- **pending**: holds stock (counts against `available_quantity`); can be
  confirmed or cancelled; auto-expires 15 minutes after creation.
- **confirmed**: still holds stock (it's now a real commitment, e.g. a
  paid order) but can no longer expire or be cancelled through this API.
  Repeating `confirm` on an already-confirmed reservation returns `200`
  with no side effect (idempotent).
- **cancelled**: releases stock immediately. Repeating `cancel` is
  idempotent (`200`, no-op). Cancelling a *confirmed* reservation is an
  **invalid transition** (`409`) — the assumption is that "un-confirming"
  a commitment needs a separate, explicit business process (e.g. a refund
  flow), not a bare cancel call. See "Assumptions" below.
- **expired**: releases stock; a terminal state reached automatically.
  Confirming an expired reservation is a `409`. Expiration is detected
  *lazily*: nothing runs in the background; any read or state-changing
  request on a reservation past its `expires_at` first flips it to
  `expired` (once, under a row lock) before doing anything else. This is
  correct because "is it expired" is decided using the database's own
  clock (`now()` in Postgres, not the app server's clock), not something
  cached in application memory.

## Database and transaction design

Five tables: `products`, `warehouses`, `stock`, `reservations`,
`reservation_items`. See `migrations/001_init.sql` for full DDL and
inline rationale comments.

**Key decision: `stock.physical_quantity` is never decremented by a
reservation.** Available stock is *computed* on read:

```
available = physical_quantity - SUM(quantity of active reservation_items)
```

where "active" means `status = 'confirmed'` or (`status = 'pending' AND
expires_at > now()`). The alternative — decrementing a `reserved_quantity`
column when a reservation is created and incrementing it back on
cancel/expiry — was considered and rejected for this prototype because:

- It requires *someone* to run the increment-back step on expiry, which
  either means a background worker (explicitly out of scope) or every
  future reader remembering to do it — easy to get subtly wrong.
- The computed version is self-healing: if a row is somehow inserted or
  updated by hand, `available_quantity` is still correct the next time
  anyone asks, because it isn't cached anywhere.

Tradeoff: computing `available_quantity` costs a join/aggregate on every
stock read, instead of an O(1) column read. For a small number of active
reservations per product this is fine; at very high scale a materialized
"reserved" counter maintained transactionally would be faster to read,
at the cost of the correctness risk above.

**Concurrency / no overselling:** creating a reservation locks the
relevant `stock` rows with `SELECT ... FOR UPDATE`, in ascending
`product_id` order (to avoid deadlocks when two multi-item reservations
overlap in different orders), *before* computing available quantity for
each requested product. Two transactions trying to reserve the same
product serialize on that row's lock: whichever commits first "wins" and
the second one, once it acquires the lock, sees the first one's
reservation and can correctly compute a smaller (or zero) availability.
This works across multiple service instances because the lock lives in
Postgres, not in process memory.

**Idempotency:** `reservations.idempotency_key` has a `UNIQUE` index.
The service does a cheap existence check first (avoids taking stock locks
for a request we've already handled), but the actual guarantee comes from
catching the unique-violation on `INSERT` and re-reading the row that
won — this covers the race where two copies of the same retried request
arrive at literally the same instant on different service instances.

**All-or-nothing:** every item's availability is checked, and the
reservation + all its items are inserted, inside one Postgres transaction.
If any item is short, an exception is raised before the `INSERT`, the
transaction rolls back, and nothing is written — no partial reservation
ever exists, even transiently.

## Assumptions

- A confirmed reservation cannot be cancelled through this API (see state
  machine above) — undoing a commitment is treated as a separate business
  process (refund/return), not exposed here. **Would confirm with product
  owner.**
- `idempotency_key` is a single required field scoped globally (not
  per-warehouse) — a client is expected to mint a fresh key per checkout
  attempt, not reuse one across unrelated reservations.
- Adding stock is additive (delta), not "set physical quantity to X".
  A separate stock-correction endpoint (for inventory audits) is out of
  scope here.
- A reservation belongs to exactly one warehouse; splitting one logical
  order across warehouses is out of scope, matching the assignment text
  ("one or more products from one warehouse").
- 15-minute TTL is fixed and not configurable per-request.

## Tradeoffs

- Computed `available_quantity` (join at read time) over a maintained
  counter — see Database design above. Chosen for correctness/simplicity
  over raw read performance at this scale.
- Lazy expiration only, no background sweeper — matches the assignment's
  explicit allowance, but means a `pending` row can sit in the table
  indefinitely in `status='pending'` if nobody ever touches it again after
  it expires (it's still *functionally* expired for stock purposes, just
  not marked so in the `status` column until the next read/write touches
  it).
- Synchronous FastAPI handlers + a small connection pool, rather than
  fully async I/O — simpler to reason about the locking code for a
  prototype; a production Go version would use a proper connection pool
  either way.

## Known limitations

- No authentication/authorization (explicitly out of scope).
- No pagination/listing endpoints (e.g. "list reservations for a
  warehouse") — only single-resource lookups, matching the assignment's
  minimum API surface.
- `idempotency_key` reuse with a *different* body is not detected or
  rejected; the second request's body is silently ignored in favor of the
  first. A stricter version would hash the request body and return `409`
  on a key/body mismatch.
- This is the Python version; it is **not** the assignment deliverable by
  itself. The Go port needs to reproduce this schema and this test suite's
  scenarios 1:1.

## What I'd improve with more time

- Hash-and-compare the request body against a stored hash for a reused
  `idempotency_key`, to catch client bugs rather than silently ignoring
  a mismatched retry.
- A `GET /warehouses/{id}/reservations?status=pending` listing endpoint
  for operational visibility.
- Structured logging around lock waits, to make contention visible in
  production rather than only inferable from latency.
- Property-based tests for the state machine (e.g. using Hypothesis) to
  fuzz the confirm/cancel/expire ordering beyond the explicit cases above.

# AI_USAGE.md

> **Note:** this documents the AI usage during the **Python prototyping
> phase**, used to design and validate the schema, state machine, and
> concurrency approach before porting to Go. When the Go implementation is
> done, write a new (or updated) version of this file that honestly
> reflects *that* process — don't just copy this one. If AI is used
> similarly for the Go port, say so the same way: what was asked, what was
> rejected, what was changed, and how it was verified.

## 1. Which AI tools did I use?

Claude (Anthropic), via the claude.ai chat interface with code execution,
for the Python prototype: schema design, FastAPI service code, test suite,
and this documentation.

## 2. What tasks did I ask it to perform?

- Translate the assignment's requirements into a concrete Postgres schema
  and a locking/idempotency strategy for reservation creation.
- Write the FastAPI application, repository/service layers, and a pytest
  suite covering the assignment's required test scenarios, including a
  concurrency test.
- Actually run the tests against a real, locally-installed PostgreSQL
  instance (not mocked) and fix what failed.
- Draft the code-review of the provided buggy `Reserve` function.

## 3. One AI suggestion I rejected, and why

The first draft of `get_reserved_quantity` only counted reservations with
`status = 'pending' AND expires_at > now()`. Running the test suite
against real Postgres immediately surfaced the bug: after confirming a
reservation, `available_quantity` went back up as if the stock were free,
because confirmed reservations no longer matched that filter. I rejected
leaving it as "pending only" and required the query to also count
`status = 'confirmed'` — a confirmed reservation is a real commitment and
must keep holding stock until something explicitly cancels it. This is
recorded in the README's state machine section and in a comment in
`repository.py`.

## 4. What generated code did I substantially change or simplify?

- Removed an initial draft of a `List reservations` endpoint that wasn't
  in the assignment's minimum API surface — added complexity (filtering,
  pagination) without being asked for, which conflicts with the
  assignment's explicit instruction not to add unnecessary layers.
- Simplified the idempotency handling to rely on the database's `UNIQUE`
  constraint plus a catch-and-reread on conflict, instead of a
  first-draft version that tried to pre-check availability of the
  idempotency key using a separate advisory lock — unnecessary
  complexity given Postgres already guarantees uniqueness.

## 5. How did I verify generated code was correct?

- Installed PostgreSQL locally and ran the full test suite against it
  (not an in-memory substitute) — this is what caught the confirmed-stock
  bug described above.
- Re-ran the concurrency test (`test_concurrent_reservations_do_not_oversell`)
  five times in a row to check for flakiness in the threading-based race
  test; all runs passed with the expected 201/409 split and correct final
  available quantity.
- Manually traced through the state machine table in the README against
  every `if`/transition check in `service.py` to confirm there's no
  state that falls through without an explicit rule (e.g. confirming a
  cancelled reservation, cancelling an expired one).
- Read the SQL in `repository.py` line by line to confirm the lock
  ordering (`ORDER BY product_id` before `FOR UPDATE`) actually matches
  what's needed to avoid deadlocks on multi-item reservations — this
  wasn't something a test could easily prove, so it was verified by
  reasoning rather than execution.

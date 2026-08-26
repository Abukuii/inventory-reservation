# REVIEW.md — Code review of `Reserve`

```go
func Reserve(ctx context.Context, db *sql.DB, productID int64, qty int) error {
	var available int

	err := db.QueryRowContext(
		ctx,
		`SELECT quantity FROM stock WHERE product_id = $1`,
		productID,
	).Scan(&available)
	if err != nil {
		return err
	}

	if available < qty {
		return errors.New("not enough stock")
	}

	_, err = db.ExecContext(
		ctx,
		`UPDATE stock SET quantity = quantity - $1 WHERE product_id = $2`,
		qty,
		productID,
	)
	return err
}
```

## 1. What can go wrong

1. **Check-then-act race condition.** The `SELECT` and `UPDATE` are two
   independent round trips against `db *sql.DB` with no transaction and no
   row lock. Two concurrent calls for the same `productID` can both read
   `available = 5`, both pass `available < qty` for `qty = 3`, and both
   proceed to `UPDATE`. The `UPDATE` itself is atomic per-statement, so the
   two updates run one after another (5→2→-1), but the *decision* to
   proceed was made on stale data. Result: stock goes negative and both
   callers are told "success", i.e. overselling.
2. **No validation of `qty`.** A zero or negative `qty` passes the
   `available < qty` check trivially and would *increase* stock via the
   `UPDATE` (subtracting a negative number), or silently no-op for zero.
3. **`sql.ErrNoRows` is not distinguished.** If `productID` doesn't exist,
   `Scan` returns `sql.ErrNoRows`, which this function returns as a bare
   error indistinguishable from a database connectivity failure. A caller
   (e.g. an HTTP handler) cannot tell "404 product not found" from "500
   internal error" without string-matching.
4. **`errors.New("not enough stock")` is not a sentinel/typed error.**
   Callers can't reliably branch on it (e.g. to return `409` instead of
   `500`) without comparing error strings, which is fragile.
5. **No verification that the `UPDATE` actually matched a row / had the
   expected effect.** `RowsAffected` is never checked. Combined with #1,
   even if a `CHECK (quantity >= 0)` constraint existed at the DB level,
   this code would return a raw constraint-violation error to the caller
   rather than a clean "not enough stock" — and without such a constraint,
   nothing stops quantity from going negative at all.
6. **No idempotency / no retry safety.** If a client times out waiting for
   this call and retries, and the first call actually succeeded, the
   second call decrements stock a second time for the same logical
   request. There is no request identifier anywhere in this function.
7. **Conceptually, this isn't a "reservation" at all.** It permanently
   decrements physical stock with no corresponding record that a
   reservation exists, no expiry, and no way to release the hold later
   (cancel) short of manually incrementing stock back — which nothing
   here does. If this is meant to implement the assignment's "reserve
   stock for checkout" behavior, it's solving a different, simpler (and
   irreversible) problem: an immediate, permanent deduction.

## 2. Business-rule vs. implementation

| # | Issue | Category |
|---|-------|----------|
| 1 | Check-then-act race → overselling | Implementation (also violates the business rule "never oversell") |
| 2 | No `qty > 0` validation | Business rule (input validation is a business requirement) |
| 3 | `sql.ErrNoRows` not distinguished | Implementation |
| 4 | Untyped error for "not enough stock" | Implementation |
| 5 | `RowsAffected` unchecked | Implementation |
| 6 | No idempotency | Business rule (the assignment explicitly requires idempotent reservation creation) |
| 7 | Permanently decrements stock instead of creating a releasable, expiring reservation | Business rule (this is the core domain model, not a coding detail) |

Issue #7 is the one I'd flag most loudly in a real review: the other six
are all fixable inside this function's current shape, but #7 means the
function is solving the wrong problem relative to the domain description
("temporarily makes stock unavailable while a customer completes
checkout"). No amount of locking makes a permanent decrement into a
temporary hold.

## 3. How I would correct it

Assuming the intent is closer to "decrement available stock atomically
and safely" (setting aside #7's larger redesign, which is the
reservation/expiry system built elsewhere in this repo), the row-locking
and conditional-update fix looks like this:

```go
var ErrInsufficientStock = errors.New("insufficient stock")
var ErrProductNotFound = errors.New("product not found")
var ErrInvalidQuantity = errors.New("quantity must be positive")

func Reserve(ctx context.Context, db *sql.DB, productID int64, qty int) error {
	if qty <= 0 {
		return ErrInvalidQuantity
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() // no-op if committed

	var available int
	err = tx.QueryRowContext(
		ctx,
		`SELECT quantity FROM stock WHERE product_id = $1 FOR UPDATE`,
		productID,
	).Scan(&available)
	if errors.Is(err, sql.ErrNoRows) {
		return ErrProductNotFound
	}
	if err != nil {
		return err
	}

	if available < qty {
		return ErrInsufficientStock
	}

	res, err := tx.ExecContext(
		ctx,
		`UPDATE stock SET quantity = quantity - $1 WHERE product_id = $2 AND quantity >= $1`,
		qty,
		productID,
	)
	if err != nil {
		return err
	}
	rows, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if rows == 0 {
		// Someone else changed the row between our SELECT and UPDATE
		// despite the lock (defensive; shouldn't happen with FOR UPDATE
		// held for the duration of the transaction, but cheap to check).
		return ErrInsufficientStock
	}

	return tx.Commit()
}
```

Key changes: everything happens inside one transaction; `SELECT ... FOR
UPDATE` holds a row lock for the duration of the transaction so a second
concurrent call blocks until the first commits or rolls back and then
sees the updated quantity; the `UPDATE`'s `WHERE` clause double-checks
sufficiency at write time; typed sentinel errors let callers map to the
right HTTP status without string matching; `qty <= 0` is rejected up
front.

This still does **not** add idempotency or convert the permanent
decrement into a releasable reservation — those require the
`reservations`/`reservation_items` schema and lazy-expiry design used
elsewhere in this repo, not a fix to this one function.

## 4. Tests that would prove the fix works

- **Concurrency test**: seed stock at a known quantity (e.g. 5), fire two
  goroutines calling `Reserve` concurrently for `qty=3` each, assert
  exactly one returns `nil` and the other returns `ErrInsufficientStock`,
  and assert the final stored quantity is `2` (never negative, never
  double-decremented).
- **Invalid quantity test**: `qty = 0` and `qty = -1` both return
  `ErrInvalidQuantity` and leave stock unchanged.
- **Missing product test**: unknown `productID` returns `ErrProductNotFound`,
  not a generic error.
- **Exact-boundary test**: `available == qty` succeeds and leaves `0`.
- **Insufficient stock test**: `qty > available` returns
  `ErrInsufficientStock` and leaves stock unchanged (verify the row wasn't
  touched, not just that an error came back).
- **Transaction rollback test**: force an error after the lock is
  acquired (e.g. cancel the context mid-transaction) and verify stock is
  left unchanged — proves the transaction boundary is real, not just
  present in the code.

## 5. Assumptions I'd confirm with a product owner before implementing

- Is this function meant to be a permanent sale-time deduction, or the
  temporary "hold during checkout" described in the assignment? These are
  different features with different rollback semantics, and the fix
  above only makes sense if the answer is "permanent deduction" (e.g.
  this runs *after* a reservation is already confirmed, as the actual
  fulfillment step).
- What HTTP status / caller behavior is expected for "product not found"
  vs. "insufficient stock" — are these both client errors, and should
  they be distinguishable in the API response?
- Does this call need to be idempotent against client retries, and if so,
  what identifier does the caller provide to detect a duplicate?
- Is there an upper bound on `qty` per call worth validating (e.g. to
  catch integer overflow or obviously-wrong input from a UI bug)?

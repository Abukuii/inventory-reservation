"""
Connection handling.

We use a plain psycopg3 connection pool. Endpoints are synchronous
(FastAPI runs sync `def` handlers in a threadpool), which keeps the
transaction/locking code easy to read for prototyping purposes.

DATABASE_URL example:
    postgresql://reservation:reservation@localhost:5432/reservation
"""
import os
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://reservation:reservation@localhost:5432/reservation",
)

# min_size=1 keeps this usable in tests without idle connections piling up.
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True)


@contextmanager
def get_connection():
    """
    Yields a connection with a transaction. Commits on success,
    rolls back on any exception. Every write path in this service
    should go through exactly one of these per request so that
    "reserve everything or nothing" is enforced by Postgres itself,
    not by application-level cleanup code.
    """
    with pool.connection() as conn:
        yield conn

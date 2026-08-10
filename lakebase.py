"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL,
e.g. postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
This keeps setup to a single secret instead of five separate env vars.

The URL comes from LAKEBASE_URL when set, and otherwise from the Databricks
secret scope. Both the deployed app and local development use the env var: on
Databricks Apps a secret *resource* injects it (see the `valueFrom` entry in
app.yaml), locally it comes from .env. The secret-scope branch is a fallback for
a deployment whose resource hasn't been configured.
"""

import base64
import logging
import os
import threading
from contextlib import contextmanager
from functools import lru_cache

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2 import pool as pgpool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "support")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

# Kept small on purpose: Lakebase caps connections, and a single Apps container
# serving a handful of users never needs many.
_POOL_MAX = int(os.environ.get("LAKEBASE_POOL_MAX", "8"))

_pool: pgpool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


@lru_cache(maxsize=1)
def _client() -> WorkspaceClient:
    """Build the Databricks client on first use.

    Constructed lazily so that importing this module doesn't require Databricks
    auth - a local run with LAKEBASE_URL set never needs a workspace at all.
    """
    return WorkspaceClient()


@lru_cache(maxsize=1)
def _lakebase_url() -> str:
    """Return the Lakebase connection URL from the env var or the secret scope.

    Cached because get_connection() calls this on every single connection, and
    the secret-scope branch is a network round trip to the Databricks API - one
    per database call. The URL points at a static, non-expiring password role, so
    it can't go stale mid-process.

    Logs which source won (never the value). A secret resource that resolves to
    an empty string falls through to the scope silently otherwise, and the two
    are indistinguishable from the outside.
    """
    url = os.environ.get("LAKEBASE_URL")
    if url:
        logger.info("Lakebase URL resolved from the LAKEBASE_URL environment variable")
        return url
    logger.info("Lakebase URL resolved from secret scope %s/%s", _SCOPE, _KEY)
    secret = _client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


def _get_pool() -> pgpool.ThreadedConnectionPool:
    """Build the connection pool on first use.

    Double-checked under a lock because Flask serves requests on threads, and
    two of them racing here would otherwise build two pools - quietly doubling
    the connection count against Lakebase.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pgpool.ThreadedConnectionPool(
                    1, _POOL_MAX, dsn=_lakebase_url(), cursor_factory=RealDictCursor
                )
                logger.info("Opened Lakebase connection pool (max %s)", _POOL_MAX)
    return _pool


def _is_live(conn) -> bool:
    """Cheap liveness probe on an already-open socket."""
    if conn.closed:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except psycopg2.Error:
        return False


@contextmanager
def get_connection():
    """Check a live connection out of the pool, and hand it back afterwards.

    Opening a fresh connection to Lakebase costs roughly 400ms - a TLS handshake
    plus authentication across the network - so the statistics endpoint, which
    ran four queries, spent about 1.7s doing nothing but connecting. Pooling
    reuses the sockets instead.

    Every checkout is validated first, because a pooled handle can be dead
    through no fault of ours: Lakebase closes idle connections and can pause the
    instance outright. Validating costs one round trip on an already-open socket
    - far less than reconnecting - and it deliberately keeps the recovery in the
    *checkout* path rather than retrying failed statements, since replaying a
    half-committed INSERT would not be safe.

    psycopg2's putconn() rolls back any transaction still open, so a connection
    is never returned to the pool carrying uncommitted work.
    """
    pool = _get_pool()

    conn = None
    for _ in range(3):
        candidate = pool.getconn()
        if _is_live(candidate):
            conn = candidate
            break
        logger.info("Discarded a dead pooled Lakebase connection.")
        pool.putconn(candidate, close=True)

    if conn is None:
        raise psycopg2.OperationalError("Could not obtain a live Lakebase connection")

    broken = False
    try:
        yield conn
    except Exception:
        broken = True
        raise
    finally:
        # close=True on failure: a connection that raised may be in an unknown
        # state, and returning it to the pool would hand that state to the next
        # request.
        pool.putconn(conn, close=broken)


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_queries(statements: list[tuple[str, tuple | dict | None]]) -> list[list[dict]]:
    """Run several read queries on ONE connection, returning each result set.

    For endpoints that need a handful of unrelated aggregates: calling
    run_query() four times would check out four connections and validate each,
    for four times the round trips.
    """
    results: list[list[dict]] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
                results.append(cur.fetchall())
    return results


def run_query_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run a read query expected to match at most one row."""
    rows = run_query(sql, params)
    return rows[0] if rows else None


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def run_write_returning(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run an INSERT/UPDATE ... RETURNING and give back the single row.

    The plain run_write() only reports a rowcount, which is useless for a POST
    that has to echo back the server-assigned BIGSERIAL id and the DEFAULT
    timestamps.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            conn.commit()
            return row


def run_many(statements: list[tuple[str, tuple | dict | None]]) -> list[dict | None]:
    """Run several statements in ONE transaction, committing once at the end.

    Needed because a write is rarely a single statement here: adding a message
    must also bump tickets.updated_at, and the two must land together or the
    "last activity" ordering starts lying. run_write() commits per call and
    opens a fresh connection each time, so it cannot express that.

    Returns the first row of each statement that produced one (RETURNING), and
    None for the statements that didn't.
    """
    results: list[dict | None] = []
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                for sql, params in statements:
                    cur.execute(sql, params)
                    results.append(cur.fetchone() if cur.description else None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return results

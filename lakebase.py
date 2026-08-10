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
from contextlib import contextmanager
from functools import lru_cache

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "support")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


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


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


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

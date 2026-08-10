"""
Lakebase-powered support ticket app.

A small internal support system: users open tickets and hold a threaded
conversation on them. Every byte of operational state lives in Lakebase
(Databricks-managed Postgres) - there is no in-memory or hard-coded data, which
is exactly why a page refresh shows the same thing the database does.

The HTTP surface is deliberately a clean JSON API under /api/*, with the HTML
page as just one more client of it. Later boot-camp projects point an AI agent
at these same endpoints.
"""

import datetime as dt
import logging
import os
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

# Load .env before any os.environ reads below, so local development can
# override the Databricks secret scope with LAKEBASE_URL.
load_dotenv()

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support-app")

app = Flask(__name__)

TICKETS_TABLE = os.environ.get("TICKETS_TABLE_NAME", "tickets")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE_NAME", "ticket_messages")

# The allowed values live here AND as CHECK constraints in sql/01_schema.sql.
# The constraints are the real guarantee; these give the user a 400 with a
# readable message instead of a 500 from a constraint violation.
STATUSES = ("open", "in_progress", "resolved", "closed")
PRIORITIES = ("low", "medium", "high", "urgent")
CATEGORIES = ("general", "billing", "technical", "account", "feature_request")

TITLE_MAX = 200
TITLE_MIN = 3
DESCRIPTION_MAX = 5000
MESSAGE_MAX = 4000


@lru_cache(maxsize=1)
def _client() -> WorkspaceClient:
    """Build the Databricks client on first use. Only needed to resolve the
    current user locally - Databricks Apps supply the X-Forwarded-Email header."""
    return WorkspaceClient()


def _current_user_email() -> str:
    """
    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set,
    and finally to LOCAL_USER_EMAIL so the app still works locally with no
    Databricks auth configured at all.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    try:
        return _client().current_user.me().user_name
    except Exception:
        logger.info("No Databricks auth available; using LOCAL_USER_EMAIL.")
        return os.getenv("LOCAL_USER_EMAIL", "local@dev")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_tables_ready = False


def ensure_tables() -> None:
    """Create the support tables if they don't exist yet.

    Mirrors sql/01_schema.sql so a fresh deployment self-heals even if nobody
    ran the SQL files by hand. It needs the role to hold CREATE on schema
    public - see sql/00_grant_app_role.sql.

    Every data route calls this, but it only touches the database once per
    process: five DDL statements plus a connection on every request is real
    latency for a result that cannot change. The flag is only set after the DDL
    succeeds, so a failure (missing GRANT, database asleep) is retried on the
    next request rather than latched in.
    """
    global _tables_ready
    if _tables_ready:
        return

    lakebase.run_many([
        (
            f"""
            CREATE TABLE IF NOT EXISTS {TICKETS_TABLE} (
                ticket_id   BIGSERIAL PRIMARY KEY,
                title       TEXT        NOT NULL,
                description TEXT,
                status      TEXT        NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','in_progress','resolved','closed')),
                priority    TEXT        NOT NULL DEFAULT 'medium'
                            CHECK (priority IN ('low','medium','high','urgent')),
                category    TEXT        NOT NULL DEFAULT 'general'
                            CHECK (category IN ('general','billing','technical','account','feature_request')),
                created_by  TEXT        NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            None,
        ),
        (
            f"""
            CREATE TABLE IF NOT EXISTS {MESSAGES_TABLE} (
                message_id   BIGSERIAL   PRIMARY KEY,
                ticket_id    BIGINT      NOT NULL
                             REFERENCES {TICKETS_TABLE}(ticket_id) ON DELETE CASCADE,
                message_text TEXT        NOT NULL,
                author       TEXT        NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            None,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS idx_{MESSAGES_TABLE}_ticket_id "
            f"ON {MESSAGES_TABLE} (ticket_id)",
            None,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS idx_{TICKETS_TABLE}_status "
            f"ON {TICKETS_TABLE} (status)",
            None,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS idx_{TICKETS_TABLE}_updated_at "
            f"ON {TICKETS_TABLE} (updated_at DESC)",
            None,
        ),
    ])


# --------------------------------------------------------------------------
# Validation helpers - each returns (value, error_message)
# --------------------------------------------------------------------------

def _payload() -> dict:
    """Accept either a JSON body or a classic form post, always as a dict."""
    if request.is_json:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}
    return request.form.to_dict()


def _clean_text(raw, field: str, *, required: bool, max_len: int, min_len: int = 0):
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        return None, f"{field} must be text."
    value = raw.strip()
    if not value:
        if required:
            return None, f"{field} is required."
        return None, None
    if len(value) < min_len:
        return None, f"{field} must be at least {min_len} characters."
    if len(value) > max_len:
        return None, f"{field} must be {max_len} characters or fewer (got {len(value)})."
    return value, None


def _clean_choice(raw, field: str, allowed: tuple, *, required: bool):
    if raw is None or raw == "":
        if required:
            return None, f"{field} is required and must be one of: {', '.join(allowed)}."
        return None, None
    if not isinstance(raw, str):
        return None, f"{field} must be text."
    value = raw.strip().lower()
    if value not in allowed:
        return None, f"{field} must be one of: {', '.join(allowed)} (got '{raw}')."
    return value, None


def _bad_request(message: str):
    """400 with a readable message. Returned explicitly rather than raised, so
    it never passes through the generic handler that scrubs error text."""
    return jsonify({"error": message}), 400


def _serialize(row: dict | None) -> dict | None:
    """Turn timestamps into ISO-8601 strings.

    Flask's default JSON encoder renders datetimes as RFC-822 HTTP dates
    ("Mon, 10 Aug 2026 18:00:00 GMT"), which the browser's Date parsing handles
    but no other client should have to. ISO keeps the API boring.
    """
    if row is None:
        return None
    out = dict(row)
    for key, value in out.items():
        if isinstance(value, (dt.datetime, dt.date)):
            out[key] = value.isoformat()
    return out


def _serialize_all(rows: list[dict]) -> list[dict]:
    return [_serialize(r) for r in rows]


def _fetch_ticket(ticket_id: int) -> dict | None:
    return lakebase.run_query_one(
        f"SELECT * FROM {TICKETS_TABLE} WHERE ticket_id = %s", (ticket_id,)
    )


def _fetch_messages(ticket_id: int) -> list[dict]:
    return lakebase.run_query(
        f"""
        SELECT message_id, ticket_id, message_text, author, created_at
        FROM {MESSAGES_TABLE}
        WHERE ticket_id = %s
        ORDER BY created_at ASC, message_id ASC
        """,
        (ticket_id,),
    )


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------

@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page), so the
    front end's `data.error` path always works.

    The response deliberately does NOT include str(err). Exception text
    routinely carries credentials: a psycopg2 connection failure puts the entire
    DSN, password and all, into its message. Full detail goes to the logs.
    """
    if isinstance(err, HTTPException):
        logger.info("HTTP %s on %s: %s", err.code, request.path, err.description)
        return jsonify({"error": err.description}), err.code

    logger.exception("Unhandled exception while processing request")
    return jsonify({"error": "Internal server error. Check the app logs for detail."}), 500


# --------------------------------------------------------------------------
# Pages and health
# --------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return render_template(
        "index.html",
        statuses=STATUSES,
        priorities=PRIORITIES,
        categories=CATEGORIES,
        current_user=_current_user_email(),
        title_max=TITLE_MAX,
        description_max=DESCRIPTION_MAX,
        message_max=MESSAGE_MAX,
    )


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

@app.route("/api/tickets", methods=["GET"])
def list_tickets():
    """All tickets, newest activity first, with optional filters.

    Message counts come back in the same round trip so the list view never has
    to fan out one request per ticket.
    """
    ensure_tables()

    status, err = _clean_choice(request.args.get("status"), "status", STATUSES, required=False)
    if err:
        return _bad_request(err)
    priority, err = _clean_choice(request.args.get("priority"), "priority", PRIORITIES, required=False)
    if err:
        return _bad_request(err)
    category, err = _clean_choice(request.args.get("category"), "category", CATEGORIES, required=False)
    if err:
        return _bad_request(err)

    search = (request.args.get("q") or "").strip() or None

    rows = lakebase.run_query(
        f"""
        SELECT t.ticket_id, t.title, t.description, t.status, t.priority,
               t.category, t.created_by, t.created_at, t.updated_at,
               COUNT(m.message_id)                       AS message_count,
               COALESCE(MAX(m.created_at), t.created_at) AS last_activity
        FROM {TICKETS_TABLE} t
        LEFT JOIN {MESSAGES_TABLE} m ON m.ticket_id = t.ticket_id
        WHERE (%(status)s::text   IS NULL OR t.status   = %(status)s)
          AND (%(priority)s::text IS NULL OR t.priority = %(priority)s)
          AND (%(category)s::text IS NULL OR t.category = %(category)s)
          AND (%(q)s::text        IS NULL
               OR t.title ILIKE '%%' || %(q)s || '%%'
               OR COALESCE(t.description, '') ILIKE '%%' || %(q)s || '%%')
        GROUP BY t.ticket_id
        ORDER BY t.updated_at DESC, t.ticket_id DESC
        """,
        {"status": status, "priority": priority, "category": category, "q": search},
    )
    return jsonify(_serialize_all(rows))


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    """Create a ticket, optionally with its opening message."""
    ensure_tables()
    body = _payload()

    title, err = _clean_text(
        body.get("title"), "Title", required=True, max_len=TITLE_MAX, min_len=TITLE_MIN
    )
    if err:
        return _bad_request(err)

    description, err = _clean_text(
        body.get("description"), "Description", required=False, max_len=DESCRIPTION_MAX
    )
    if err:
        return _bad_request(err)

    status, err = _clean_choice(body.get("status"), "status", STATUSES, required=False)
    if err:
        return _bad_request(err)
    priority, err = _clean_choice(body.get("priority"), "priority", PRIORITIES, required=False)
    if err:
        return _bad_request(err)
    category, err = _clean_choice(body.get("category"), "category", CATEGORIES, required=False)
    if err:
        return _bad_request(err)

    first_message, err = _clean_text(
        body.get("message_text"), "Message", required=False, max_len=MESSAGE_MAX
    )
    if err:
        return _bad_request(err)

    email = _current_user_email()

    statements = [
        (
            f"""
            INSERT INTO {TICKETS_TABLE}
                (title, description, status, priority, category, created_by)
            VALUES (%s, %s, COALESCE(%s, 'open'), COALESCE(%s, 'medium'), COALESCE(%s, 'general'), %s)
            RETURNING *
            """,
            (title, description, status, priority, category, email),
        ),
    ]
    if first_message:
        # currval() rather than a second round trip: same transaction, same
        # session, and it is unaffected by other sessions' inserts.
        statements.append(
            (
                f"""
                INSERT INTO {MESSAGES_TABLE} (ticket_id, message_text, author)
                VALUES (currval(pg_get_serial_sequence('{TICKETS_TABLE}', 'ticket_id')), %s, %s)
                """,
                (first_message, email),
            )
        )

    ticket = lakebase.run_many(statements)[0]
    logger.info("Ticket %s created by %s", ticket["ticket_id"], email)
    return jsonify(_serialize(ticket)), 201


@app.route("/api/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id: int):
    """One ticket plus its full message thread."""
    ensure_tables()
    ticket = _fetch_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": f"Ticket {ticket_id} not found."}), 404

    payload = _serialize(ticket)
    payload["messages"] = _serialize_all(_fetch_messages(ticket_id))
    payload["message_count"] = len(payload["messages"])
    return jsonify(payload)


@app.route("/api/tickets/<int:ticket_id>", methods=["PATCH", "POST"])
def update_ticket(ticket_id: int):
    """Update a ticket's status (and optionally priority, category, title).

    POST is accepted alongside PATCH only because HTML forms cannot emit PATCH;
    the JSON front end uses PATCH.
    """
    ensure_tables()
    body = _payload()

    updates: dict[str, str] = {}

    for field, allowed in (("status", STATUSES), ("priority", PRIORITIES), ("category", CATEGORIES)):
        if field in body:
            value, err = _clean_choice(body.get(field), field, allowed, required=True)
            if err:
                return _bad_request(err)
            updates[field] = value

    if "title" in body:
        value, err = _clean_text(
            body.get("title"), "Title", required=True, max_len=TITLE_MAX, min_len=TITLE_MIN
        )
        if err:
            return _bad_request(err)
        updates["title"] = value

    if "description" in body:
        value, err = _clean_text(
            body.get("description"), "Description", required=False, max_len=DESCRIPTION_MAX
        )
        if err:
            return _bad_request(err)
        updates["description"] = value

    if not updates:
        return _bad_request(
            "Nothing to update. Send at least one of: status, priority, category, "
            "title, description."
        )

    assignments = ", ".join(f"{col} = %({col})s" for col in updates)
    params = dict(updates, ticket_id=ticket_id)
    ticket = lakebase.run_write_returning(
        f"""
        UPDATE {TICKETS_TABLE}
        SET {assignments}, updated_at = now()
        WHERE ticket_id = %(ticket_id)s
        RETURNING *
        """,
        params,
    )
    if not ticket:
        return jsonify({"error": f"Ticket {ticket_id} not found."}), 404

    logger.info("Ticket %s updated (%s)", ticket_id, ", ".join(updates))
    return jsonify(_serialize(ticket))


@app.route("/api/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id: int):
    """Delete a ticket and its messages. Requires an explicit confirmation.

    The confirm flag is the server-side half of the UI's confirmation dialog:
    a stray DELETE - a mis-clicked link, a retried request, an agent looping -
    cannot destroy a thread without saying so on purpose.
    """
    ensure_tables()
    body = _payload()
    confirm = body.get("confirm", request.args.get("confirm"))
    if confirm not in (True, "true", "True", "1", 1):
        return _bad_request(
            "Deleting a ticket also deletes its messages. Re-send with "
            '{"confirm": true} to proceed.'
        )

    ticket = lakebase.run_write_returning(
        f"DELETE FROM {TICKETS_TABLE} WHERE ticket_id = %s RETURNING ticket_id, title",
        (ticket_id,),
    )
    if not ticket:
        return jsonify({"error": f"Ticket {ticket_id} not found."}), 404

    # ticket_messages has ON DELETE CASCADE, so the thread went with it.
    logger.info("Ticket %s deleted by %s", ticket_id, _current_user_email())
    return jsonify({"ticket_id": ticket["ticket_id"], "title": ticket["title"], "deleted": True})


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

@app.route("/api/tickets/<int:ticket_id>/messages", methods=["GET"])
def list_messages(ticket_id: int):
    ensure_tables()
    if not _fetch_ticket(ticket_id):
        return jsonify({"error": f"Ticket {ticket_id} not found."}), 404
    return jsonify(_serialize_all(_fetch_messages(ticket_id)))


@app.route("/api/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_message(ticket_id: int):
    """Append a message to a ticket and bump the ticket's last-activity stamp."""
    ensure_tables()
    body = _payload()

    text, err = _clean_text(
        body.get("message_text"), "Message", required=True, max_len=MESSAGE_MAX, min_len=1
    )
    if err:
        return _bad_request(err)

    if not _fetch_ticket(ticket_id):
        return jsonify({"error": f"Ticket {ticket_id} not found."}), 404

    email = _current_user_email()
    message, _ = lakebase.run_many([
        (
            f"""
            INSERT INTO {MESSAGES_TABLE} (ticket_id, message_text, author)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (ticket_id, text, email),
        ),
        (
            # Same transaction as the insert: the thread and the sort key that
            # surfaces it can never disagree.
            f"UPDATE {TICKETS_TABLE} SET updated_at = now() WHERE ticket_id = %s",
            (ticket_id,),
        ),
    ])

    logger.info("Message %s added to ticket %s by %s", message["message_id"], ticket_id, email)
    return jsonify(_serialize(message)), 201


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
def stats():
    """Headline counters plus per-status and per-priority breakdowns."""
    ensure_tables()

    totals = lakebase.run_query_one(
        f"""
        SELECT COUNT(*)                                       AS total_tickets,
               COUNT(*) FILTER (WHERE status = 'open')        AS open_tickets,
               COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress_tickets,
               COUNT(*) FILTER (WHERE status = 'resolved')    AS resolved_tickets,
               COUNT(*) FILTER (WHERE status = 'closed')      AS closed_tickets,
               COUNT(*) FILTER (WHERE priority IN ('high','urgent')
                                  AND status NOT IN ('resolved','closed'))
                                                              AS urgent_open,
               (SELECT COUNT(*) FROM {MESSAGES_TABLE})        AS total_messages
        FROM {TICKETS_TABLE}
        """
    ) or {}

    by_status = lakebase.run_query(
        f"SELECT status, COUNT(*) AS count FROM {TICKETS_TABLE} GROUP BY status"
    )
    by_priority = lakebase.run_query(
        f"SELECT priority, COUNT(*) AS count FROM {TICKETS_TABLE} GROUP BY priority"
    )
    by_category = lakebase.run_query(
        f"SELECT category, COUNT(*) AS count FROM {TICKETS_TABLE} GROUP BY category"
    )

    total = totals.get("total_tickets") or 0
    messages = totals.get("total_messages") or 0

    return jsonify({
        **totals,
        "avg_messages_per_ticket": round(messages / total, 1) if total else 0,
        # Zero-filled so the UI can render every bucket without checking for
        # missing keys - a status with no tickets still deserves a row.
        "by_status": {s: 0 for s in STATUSES} | {r["status"]: r["count"] for r in by_status},
        "by_priority": {p: 0 for p in PRIORITIES} | {r["priority"]: r["count"] for r in by_priority},
        "by_category": {c: 0 for c in CATEGORIES} | {r["category"]: r["count"] for r in by_category},
    })


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')

    # Databricks Apps assigns the port and injects it as DATABRICKS_APP_PORT;
    # the platform health-checks that exact port, so binding anywhere else
    # leaves the app permanently "starting" and unreachable. It must win over
    # the local-dev default.
    port = int(
        os.getenv('DATABRICKS_APP_PORT')
        or os.getenv('FLASK_RUN_PORT')
        or 8000
    )

    debug = os.getenv('FLASK_DEBUG', '1') == '1'
    logger.info("Starting Flask on http://%s:%s (debug=%s)", host, port, debug)
    app.run(debug=debug, host=host, port=port)

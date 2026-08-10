-- Support ticket schema for Lakebase (Databricks-managed Postgres).
--
-- Run this in the LAKEBASE INSTANCE'S OWN query editor (open it from the
-- database instance page), or with `python seed.py` from a terminal that has
-- LAKEBASE_URL set. Do NOT paste it into a notebook %sql cell - that goes to
-- Unity Catalog, which is a different engine entirely.
--
-- Safe to re-run: every statement is IF NOT EXISTS. app.py's ensure_tables()
-- runs the same DDL, so a deployment where nobody ran this file still works.

CREATE TABLE IF NOT EXISTS tickets (
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
);

-- ticket_messages.ticket_id references a real ticket. ON DELETE CASCADE is what
-- lets "delete this ticket" be a single statement instead of a two-step dance:
-- the thread cannot outlive the ticket it belongs to.
CREATE TABLE IF NOT EXISTS ticket_messages (
    message_id   BIGSERIAL   PRIMARY KEY,
    ticket_id    BIGINT      NOT NULL
                 REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    message_text TEXT        NOT NULL,
    author       TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every message read is "give me this one ticket's thread"; without this index
-- that is a sequential scan of the whole table.
CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_id ON ticket_messages (ticket_id);

-- Backs the status filter and the default "most recent activity first" sort.
CREATE INDEX IF NOT EXISTS idx_tickets_status     ON tickets (status);
CREATE INDEX IF NOT EXISTS idx_tickets_updated_at ON tickets (updated_at DESC);

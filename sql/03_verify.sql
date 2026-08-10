-- Verification queries. Run after seeding, and again after using the app, to
-- confirm the UI is really writing to Lakebase rather than to browser state.

-- 1. Both tables exist with the expected columns.
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('tickets', 'ticket_messages')
ORDER BY table_name, ordinal_position;

-- 2. The foreign key from ticket_messages to tickets is in place.
SELECT tc.constraint_name,
       kcu.column_name        AS fk_column,
       ccu.table_name         AS references_table,
       ccu.column_name        AS references_column,
       rc.delete_rule
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
JOIN information_schema.referential_constraints rc
  ON rc.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_name = 'ticket_messages';

-- 3. Requirement check: >= 3 tickets, >= 2 statuses, >= 2 messages per ticket.
SELECT COUNT(*)                AS ticket_count,
       COUNT(DISTINCT status)  AS distinct_statuses
FROM tickets;

SELECT t.ticket_id, t.title, t.status, t.priority, t.category,
       COUNT(m.message_id) AS message_count
FROM tickets t
LEFT JOIN ticket_messages m ON m.ticket_id = t.ticket_id
GROUP BY t.ticket_id, t.title, t.status, t.priority, t.category
ORDER BY t.ticket_id;

-- 4. Tickets per status - should shift as you change statuses in the app.
SELECT status, COUNT(*) AS count FROM tickets GROUP BY status ORDER BY status;

-- 5. No orphaned messages (should return zero rows; the FK guarantees it).
SELECT m.message_id, m.ticket_id
FROM ticket_messages m
LEFT JOIN tickets t ON t.ticket_id = m.ticket_id
WHERE t.ticket_id IS NULL;

-- 6. Most recent writes - run this right after creating a ticket in the UI to
--    prove the row came from the app.
SELECT ticket_id, title, status, created_by, created_at, updated_at
FROM tickets
ORDER BY created_at DESC
LIMIT 5;

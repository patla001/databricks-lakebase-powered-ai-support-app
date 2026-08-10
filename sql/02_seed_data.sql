-- Sample support data: 5 tickets across 3 statuses, 2-3 messages each.
--
-- Idempotent by construction. `tickets.ticket_id` is a BIGSERIAL, so there is
-- no natural key to hang ON CONFLICT on; instead each INSERT is guarded by
-- "only if the table is empty". Re-running this file after the app has been
-- used therefore does nothing rather than duplicating the demo data.
--
-- To reseed from scratch (this destroys real tickets too):
--     TRUNCATE ticket_messages, tickets RESTART IDENTITY;

INSERT INTO tickets (title, description, status, priority, category, created_by, created_at, updated_at)
SELECT title, description, status, priority, category, created_by, created_at, updated_at
FROM (VALUES
    ('Cannot reset my password'::text,
     'The reset email never arrives. Checked spam. Tried two browsers.'::text,
     'open'::text, 'high'::text, 'account'::text, 'dana.reed@example.com'::text,
     (now() - INTERVAL '5 days')::timestamptz, (now() - INTERVAL '3 hours')::timestamptz),

    ('Invoice shows duplicate charge',
     'Invoice INV-4821 lists the March seat charge twice. Total is $240 over.',
     'in_progress', 'urgent', 'billing', 'marcus.lee@example.com',
     now() - INTERVAL '4 days', now() - INTERVAL '6 hours'),

    ('Dashboard loads slowly after login',
     'First load after signing in takes 20-30 seconds. Subsequent loads are fine.',
     'in_progress', 'medium', 'technical', 'priya.nair@example.com',
     now() - INTERVAL '3 days', now() - INTERVAL '1 day'),

    ('Export to CSV fails for large reports',
     'Exports over roughly 50k rows return a 502. Smaller exports succeed.',
     'resolved', 'high', 'technical', 'sam.okafor@example.com',
     now() - INTERVAL '6 days', now() - INTERVAL '2 days'),

    ('Add dark mode to the console',
     'Would like a dark theme, or at least one that follows the OS setting.',
     'open', 'low', 'feature_request', 'jordan.kim@example.com',
     now() - INTERVAL '2 days', now() - INTERVAL '2 days')
) AS seed(title, description, status, priority, category, created_by, created_at, updated_at)
WHERE NOT EXISTS (SELECT 1 FROM tickets);


-- Messages join back to their ticket by title rather than by a hardcoded id,
-- so the seed does not depend on what the BIGSERIAL sequence happens to be at.
INSERT INTO ticket_messages (ticket_id, message_text, author, created_at)
SELECT t.ticket_id, m.message_text, m.author, m.created_at
FROM (VALUES
    ('Cannot reset my password'::text,
     'I have requested a reset link four times today and nothing has arrived.'::text,
     'dana.reed@example.com'::text, (now() - INTERVAL '5 days')::timestamptz),
    ('Cannot reset my password',
     'Thanks for reporting. Can you confirm the exact address on the account?',
     'support@example.com', now() - INTERVAL '4 days'),
    ('Cannot reset my password',
     'It is dana.reed@example.com - same one I am writing from.',
     'dana.reed@example.com', now() - INTERVAL '3 hours'),

    ('Invoice shows duplicate charge',
     'Attaching invoice INV-4821. The March seat charge appears on lines 3 and 7.',
     'marcus.lee@example.com', now() - INTERVAL '4 days'),
    ('Invoice shows duplicate charge',
     'Confirmed on our side - a retry during the billing run double-posted it.',
     'support@example.com', now() - INTERVAL '2 days'),
    ('Invoice shows duplicate charge',
     'Credit memo is queued and should land on the next statement.',
     'support@example.com', now() - INTERVAL '6 hours'),

    ('Dashboard loads slowly after login',
     'Consistently 20-30 seconds on the first load, then instant afterwards.',
     'priya.nair@example.com', now() - INTERVAL '3 days'),
    ('Dashboard loads slowly after login',
     'That matches a cold cache on our side. We are looking at prewarming it.',
     'support@example.com', now() - INTERVAL '1 day'),

    ('Export to CSV fails for large reports',
     'Any export past about 50k rows comes back as a 502 after two minutes.',
     'sam.okafor@example.com', now() - INTERVAL '6 days'),
    ('Export to CSV fails for large reports',
     'Reproduced. The export ran past the gateway timeout instead of streaming.',
     'support@example.com', now() - INTERVAL '4 days'),
    ('Export to CSV fails for large reports',
     'Fixed in this week release - exports now stream. Marking this resolved.',
     'support@example.com', now() - INTERVAL '2 days'),

    ('Add dark mode to the console',
     'The console is painful to read at night. A dark theme would help a lot.',
     'jordan.kim@example.com', now() - INTERVAL '2 days'),
    ('Add dark mode to the console',
     'Logged as a feature request. No committed date yet, but it is on the list.',
     'support@example.com', now() - INTERVAL '2 days')
) AS m(title, message_text, author, created_at)
JOIN tickets t ON t.title = m.title
WHERE NOT EXISTS (SELECT 1 FROM ticket_messages);

"""
End-to-end test against the DEPLOYED Databricks App.

    python test_deployment.py https://<your-app>.databricksapps.com

Walks the five capabilities the assignment asks you to demonstrate - load
tickets, create one, add a message, update a status, and see changes survive a
refresh - plus the bonus features, and checks the error paths behave.

What makes this more than a smoke test: every write is verified TWICE. Once by
re-reading it through the app, and once by connecting straight to Lakebase with
psycopg2 and looking for the row. An app that cached in memory, or wrote
somewhere else entirely, passes the first check and fails the second.

Cleans up after itself - the tickets it creates are deleted at the end, so your
sample data is left exactly as it was.

Needs:
  - Databricks workspace auth (the same `databricks auth login` used elsewhere),
    to mint the OAuth token the App requires.
  - LAKEBASE_URL, or secret-scope access, for the direct database check.
"""

import sys
import uuid

import requests
from databricks.sdk import WorkspaceClient
from dotenv import load_dotenv

load_dotenv()

import lakebase

MARKER = f"[apptest-{uuid.uuid4().hex[:8]}]"   # tags rows this run created

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{' - ' + detail if detail else ''}")
    return condition


def main(app_url: str) -> int:
    app_url = app_url.rstrip("/")

    # Databricks Apps sit behind workspace auth; the SDK hands back whatever
    # Authorization header your current login implies.
    session = requests.Session()
    session.headers.update(WorkspaceClient().config.authenticate())

    def api(method, path, **kw):
        return session.request(method, f"{app_url}{path}", timeout=60, **kw)

    created: list[int] = []

    try:
        print(f"\nTesting {app_url}\n")

        # -- 1. Existing tickets load from Lakebase ------------------------
        print("1. Existing tickets load from Lakebase")
        r = api("GET", "/api/tickets")
        check("GET /api/tickets returns 200", r.status_code == 200, f"got {r.status_code}")
        tickets = r.json() if r.status_code == 200 else []
        check("at least 3 tickets", len(tickets) >= 3, f"{len(tickets)} tickets")
        check("at least 2 distinct statuses",
              len({t["status"] for t in tickets}) >= 2,
              ", ".join(sorted({t["status"] for t in tickets})))
        check("every ticket has >= 2 messages",
              all(int(t["message_count"]) >= 2 for t in tickets),
              "counts: " + ", ".join(str(t["message_count"]) for t in tickets))

        db_ids = {r["ticket_id"] for r in lakebase.run_query("SELECT ticket_id FROM tickets")}
        check("the app is serving the same rows Lakebase holds",
              {t["ticket_id"] for t in tickets} == db_ids,
              f"app {len(tickets)} / db {len(db_ids)}")

        # -- 2. A new ticket can be created --------------------------------
        print("\n2. A new ticket can be created")
        title = f"{MARKER} Laptop will not connect to VPN"
        r = api("POST", "/api/tickets", json={
            "title": title,
            "description": "Fails at the authentication step every time.",
            "priority": "high",
            "category": "technical",
            "message_text": "Started after the update this morning.",
        })
        check("POST /api/tickets returns 201", r.status_code == 201, f"got {r.status_code}")
        if r.status_code != 201:
            print("   cannot continue without a ticket"); return 1
        ticket = r.json()
        tid = ticket["ticket_id"]
        created.append(tid)
        check("server assigned an id", isinstance(tid, int), f"#{tid}")
        check("priority and category stored",
              ticket["priority"] == "high" and ticket["category"] == "technical")
        check("created_by came from the signed-in Databricks user",
              "@" in str(ticket["created_by"]), ticket["created_by"])

        row = lakebase.run_query_one("SELECT * FROM tickets WHERE ticket_id = %s", (tid,))
        check("IN LAKEBASE: the row really exists", row is not None)
        check("IN LAKEBASE: title matches", row and row["title"] == title)

        # -- 3. A message can be added -------------------------------------
        print("\n3. A message can be added to an existing ticket")
        body = f"{MARKER} Reproduced on a second laptop."
        r = api("POST", f"/api/tickets/{tid}/messages", json={"message_text": body})
        check("POST message returns 201", r.status_code == 201, f"got {r.status_code}")

        msgs = lakebase.run_query(
            "SELECT message_text, author FROM ticket_messages WHERE ticket_id = %s "
            "ORDER BY message_id", (tid,))
        check("IN LAKEBASE: both messages are stored", len(msgs) == 2, f"{len(msgs)} messages")
        check("IN LAKEBASE: the new message text matches",
              any(m["message_text"] == body for m in msgs))
        check("IN LAKEBASE: foreign key ties them to this ticket", len(msgs) > 0)

        # -- 4. A ticket's status can be updated ---------------------------
        print("\n4. A ticket's status can be updated")
        r = api("PATCH", f"/api/tickets/{tid}", json={"status": "in_progress"})
        check("PATCH returns 200", r.status_code == 200, f"got {r.status_code}")
        check("response shows the new status",
              r.status_code == 200 and r.json().get("status") == "in_progress")

        row = lakebase.run_query_one("SELECT status, updated_at, created_at FROM tickets WHERE ticket_id = %s", (tid,))
        check("IN LAKEBASE: status is in_progress", row and row["status"] == "in_progress",
              row["status"] if row else "no row")
        check("IN LAKEBASE: updated_at moved past created_at",
              row and row["updated_at"] > row["created_at"])

        # -- 5. Changes remain after refreshing ----------------------------
        print("\n5. Changes remain after a refresh")
        # A brand-new request with no shared state - the same thing a browser
        # reload does.
        fresh = requests.Session()
        fresh.headers.update(WorkspaceClient().config.authenticate())
        r = fresh.get(f"{app_url}/api/tickets/{tid}", timeout=60)
        check("re-fetched on a new connection", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            check("status persisted", d["status"] == "in_progress")
            check("message persisted", any(m["message_text"] == body for m in d["messages"]))
            check("priority persisted", d["priority"] == "high")

        # -- 6. Bonus: filtering, search, statistics -----------------------
        print("\n6. Bonus features")
        r = api("GET", "/api/tickets", params={"status": "in_progress"})
        check("filter by status", r.status_code == 200 and
              all(t["status"] == "in_progress" for t in r.json()),
              f"{len(r.json())} rows")
        r = api("GET", "/api/tickets", params={"priority": "high"})
        check("filter by priority", r.status_code == 200 and
              all(t["priority"] == "high" for t in r.json()))
        r = api("GET", "/api/tickets", params={"q": "VPN"})
        check("free-text search", r.status_code == 200 and
              any(t["ticket_id"] == tid for t in r.json()))

        r = api("GET", "/api/stats")
        check("GET /api/stats returns 200", r.status_code == 200)
        if r.status_code == 200:
            s = r.json()
            check("statistics count real tickets",
                  s["total_tickets"] == len(db_ids) + len(created),
                  f"{s['total_tickets']} total, {s['total_messages']} messages")
            check("per-status breakdown present",
                  isinstance(s.get("by_status"), dict) and "open" in s["by_status"],
                  str(s.get("by_status")))

        # -- 7. Bonus: validation and error handling -----------------------
        print("\n7. Input validation and error messages")
        for label, method, path, payload, want in [
            ("title too short",        "POST",   "/api/tickets",              {"title": "ab"}, 400),
            ("title missing",          "POST",   "/api/tickets",              {"description": "x"}, 400),
            ("invalid priority",       "POST",   "/api/tickets",              {"title": "a valid title", "priority": "asap"}, 400),
            ("invalid status",         "PATCH",  f"/api/tickets/{tid}",       {"status": "nope"}, 400),
            ("empty update",           "PATCH",  f"/api/tickets/{tid}",       {}, 400),
            ("blank message",          "POST",   f"/api/tickets/{tid}/messages", {"message_text": "   "}, 400),
            ("message on missing ticket", "POST", "/api/tickets/99999999/messages", {"message_text": "x"}, 404),
            ("fetch missing ticket",   "GET",    "/api/tickets/99999999",     None, 404),
        ]:
            r = api(method, path, json=payload) if payload is not None else api(method, path)
            msg = (r.json() or {}).get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            check(f"{label} -> {want}", r.status_code == want and bool(msg),
                  f"{r.status_code}: {msg[:70]}")

        # -- 8. Bonus: delete requires confirmation ------------------------
        print("\n8. Delete requires an explicit confirmation")
        r = api("DELETE", f"/api/tickets/{tid}", json={})
        check("delete without confirm is refused", r.status_code == 400, f"got {r.status_code}")
        check("IN LAKEBASE: ticket survived the unconfirmed delete",
              lakebase.run_query_one("SELECT 1 AS x FROM tickets WHERE ticket_id = %s", (tid,)) is not None)

        r = api("DELETE", f"/api/tickets/{tid}", json={"confirm": True})
        check("delete with confirm succeeds", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            created.remove(tid)
        check("IN LAKEBASE: ticket is gone",
              lakebase.run_query_one("SELECT 1 AS x FROM tickets WHERE ticket_id = %s", (tid,)) is None)
        check("IN LAKEBASE: its messages cascaded away",
              not lakebase.run_query("SELECT 1 AS x FROM ticket_messages WHERE ticket_id = %s", (tid,)))

        # -- 9. Sample data untouched --------------------------------------
        print("\n9. Your sample data is unchanged")
        after = {r["ticket_id"] for r in lakebase.run_query("SELECT ticket_id FROM tickets")}
        check("same tickets as when we started", after == db_ids,
              f"{len(after)} tickets")

    finally:
        # Belt and braces: remove anything an early failure left behind.
        for leftover in created:
            try:
                lakebase.run_write("DELETE FROM tickets WHERE ticket_id = %s", (leftover,))
                print(f"\n  (cleaned up leftover ticket #{leftover})")
            except Exception as err:
                print(f"\n  WARNING: could not clean up ticket #{leftover}: {err}")

    print(f"\n{'=' * 58}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 58}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python test_deployment.py https://<your-app>.databricksapps.com")
    sys.exit(main(sys.argv[1]))

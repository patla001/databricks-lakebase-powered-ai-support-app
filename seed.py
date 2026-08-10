"""
Create the support schema and load sample data into Lakebase.

    python seed.py            # schema + sample data, then a summary
    python seed.py --schema   # schema only
    python seed.py --verify   # no writes, just report what is there

Reads the SQL from sql/01_schema.sql and sql/02_seed_data.sql rather than
carrying its own copy, so the files you can paste into the Lakebase query editor
and the ones this script runs can never drift apart.

Both files are safe to re-run: the schema is all IF NOT EXISTS, and the sample
data only inserts when the tables are empty. Running this against a database
that already holds real tickets changes nothing.

Needs LAKEBASE_URL (from .env or the environment), or Databricks workspace auth
so lakebase.py can read it from the secret scope.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import lakebase

SQL_DIR = Path(__file__).parent / "sql"


def _run_file(name: str) -> None:
    path = SQL_DIR / name
    sql = path.read_text()
    print(f"Running {path.name} ...")
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            # psycopg2 happily executes a multi-statement script in one call,
            # which is what keeps these files runnable both here and by hand in
            # the Lakebase query editor.
            cur.execute(sql)
        conn.commit()
    print(f"  {path.name} applied.")


def _verify() -> int:
    """Print what is actually in the database. Returns a shell exit code."""
    tickets = lakebase.run_query(
        """
        SELECT t.ticket_id, t.title, t.status, t.priority, t.category,
               COUNT(m.message_id) AS message_count
        FROM tickets t
        LEFT JOIN ticket_messages m ON m.ticket_id = t.ticket_id
        GROUP BY t.ticket_id, t.title, t.status, t.priority, t.category
        ORDER BY t.ticket_id
        """
    )

    print(f"\n{len(tickets)} ticket(s) in Lakebase:\n")
    for t in tickets:
        print(
            f"  #{t['ticket_id']:<4} [{t['status']:<11}] [{t['priority']:<6}] "
            f"{t['title'][:46]:<48} {t['message_count']} msg"
        )

    statuses = {t["status"] for t in tickets}
    thin = [t["ticket_id"] for t in tickets if t["message_count"] < 2]

    print("\nAssignment requirements:")
    checks = [
        (len(tickets) >= 3, f"at least 3 tickets (have {len(tickets)})"),
        (len(statuses) >= 2, f"at least 2 distinct statuses (have {len(statuses)}: {', '.join(sorted(statuses)) or 'none'})"),
        (not thin, f"at least 2 messages per ticket ({'ok' if not thin else 'short: ' + ', '.join(f'#{i}' for i in thin)})"),
    ]
    for ok, label in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    return 0 if all(ok for ok, _ in checks) else 1


if __name__ == "__main__":
    args = set(sys.argv[1:])

    if args - {"--schema", "--verify"}:
        sys.exit("Unknown argument. Usage: python seed.py [--schema | --verify]")

    if "--verify" not in args:
        _run_file("01_schema.sql")
        if "--schema" not in args:
            _run_file("02_seed_data.sql")

    sys.exit(_verify())

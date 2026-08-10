"""
One-time setup: creates the Databricks secret scope and stores the Lakebase
connection URL. Safe to re-run.

Three supported ways to run it:

1. Local terminal (input is masked by getpass):
       python setup_secrets.py

2. Databricks notebook - paste into a **Python** cell, or run:
       exec(open("setup_secrets.py").read())
   Notebook Python cells support interactive input(), so the prompts work
   (but they echo in plaintext - clear the cell output afterwards).

3. Non-interactive (CI, or when you'd rather not be prompted):
       LAKEBASE_URL='postgresql://...' python setup_secrets.py

WARNING: `%sh python setup_secrets.py` does NOT work. The %sh magic runs the
script in a subshell with no TTY, so the prompt never reaches you and the cell
hangs forever. Use option 2 instead.

Never commit the resulting secret value anywhere.
"""

import getpass
import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from databricks.sdk.service import workspace

SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "support")
KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

try:
    w = WorkspaceClient()
except ValueError as err:
    # Writing secrets goes through the Databricks API, so this script always
    # needs workspace auth - unlike app.py, which can run purely off .env.
    sys.exit(
        f"\nCannot authenticate to Databricks: {err}\n\n"
        "This script writes to the Databricks secrets API, so it needs workspace\n"
        "credentials. Pick one:\n\n"
        "  1. Run it inside a Databricks notebook Python cell (auth is automatic):\n"
        '       exec(open("setup_secrets.py").read())\n\n'
        "  2. Authenticate this terminal with a personal access token\n"
        "     (Databricks UI: Settings > Developer > Access tokens):\n"
        "       export DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com\n"
        "       export DATABRICKS_TOKEN=<your-pat>\n"
        "       python setup_secrets.py\n"
    )


def _prompt(label: str, env_var: str) -> str:
    """Read a secret value from the environment, a TTY, or a notebook cell.

    Resolution order: env var (non-interactive) -> getpass (real terminal,
    masked) -> input() (Databricks notebook Python cell, echoes). Deliberately
    never a command-line flag, which would land the DSN in shell history.
    """
    value = os.environ.get(env_var)
    if value:
        print(f"Using {env_var} from the environment.")
        return value.strip()

    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(label).strip()
        return input(label).strip()
    except (EOFError, OSError) as err:
        raise RuntimeError(
            f"Cannot prompt for {env_var}: no interactive input available "
            f"({err}). If you ran this with `%sh`, that shell has no TTY - "
            f"run it in a notebook Python cell instead, or set {env_var} in "
            f"the environment and re-run."
        ) from err


def _ensure_scope(scope: str) -> None:
    """Create a secret scope, treating an existing scope as success."""
    try:
        w.secrets.create_scope(scope=scope)
        print(f"Created secret scope '{scope}'.")
    except DatabricksError as err:
        if "RESOURCE_ALREADY_EXISTS" in str(err) or "already exists" in str(err).lower():
            print(f"Secret scope '{scope}' already exists.")
        else:
            raise


def _grant_read(scope: str) -> None:
    """Grant workspace users READ on the scope. Non-fatal: some workspace tiers
    reject ACL changes, and the secret is already stored by this point."""
    try:
        w.secrets.put_acl(scope=scope, principal="users", permission=workspace.AclPermission.READ)
        print(f"Granted READ on '{scope}' to 'users'.")
    except DatabricksError as err:
        print(f"WARNING: could not set ACL on '{scope}': {err}")


url = _prompt("Paste your Lakebase URL: ", "LAKEBASE_URL")

if not url.startswith("postgresql://"):
    sys.exit(
        "That does not look like a Postgres URL. Expected:\n"
        "  postgresql://<role>:<password>@<host>:5432/databricks_postgres?sslmode=require"
    )

_ensure_scope(SCOPE)
w.secrets.put_secret(scope=SCOPE, key=KEY, string_value=url)
_grant_read(SCOPE)

# Verify: print the stored key names only - never the values.
print(f"Scope '{SCOPE}' now contains: {[s.key for s in w.secrets.list_secrets(scope=SCOPE)]}")
print(
    f"\nNext: add a Secret app resource with resource key 'lakebase-url' pointing at\n"
    f"scope '{SCOPE}' / key '{KEY}' so app.yaml's `valueFrom` can resolve it."
)

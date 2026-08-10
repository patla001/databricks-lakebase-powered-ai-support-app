-- Grant the app's Postgres role permission to create objects in schema public.
--
-- WHY THIS IS NEEDED
-- PostgreSQL 15 removed the historical grant of CREATE on schema `public` to
-- every role. A freshly created Lakebase password role therefore gets USAGE but
-- NOT CREATE, and the first thing the app does - ensure_tables() - fails with:
--
--     permission denied for schema public
--
-- WHERE TO RUN IT
-- This is PostgreSQL DDL. It must reach the Lakebase Postgres engine:
--
--   a) the Lakebase instance's own query editor - open it from the database
--      instance page, NOT the generic SQL editor; or
--   b) psql:
--        psql "postgresql://<your-databricks-email>@<host>:5432/databricks_postgres?sslmode=require"
--   c) psycopg2 inside a notebook **Python** cell.
--
-- DO NOT run it in a notebook %sql cell. That routes to Unity Catalog, which
-- reads `support_app` as a Databricks principal and fails with
-- PRINCIPAL_DOES_NOT_EXIST. The syntax is valid in both dialects, which is
-- exactly what makes the mistake easy to miss.

GRANT CREATE ON SCHEMA public TO support_app;
GRANT USAGE  ON SCHEMA public TO support_app;

-- Verify - both columns should come back true.
SELECT has_schema_privilege('support_app', 'public', 'USAGE')  AS has_usage,
       has_schema_privilege('support_app', 'public', 'CREATE') AS has_create;

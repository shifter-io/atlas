-- SEC-INGEST-ROLE-1 (#1022): undo scripts/db/ingest-role.sql. Run it only after the orchestrator
-- no longer uses the role (ATLAS_INGEST_DATABASE_FILE removed and the orchestrator restarted),
-- connected to the framework's database as the login that ran ingest-role.sql.
--   Dry run (the default): everything rolls back.
--     psql "<admin connection>" -v ON_ERROR_STOP=1 -f scripts/db/ingest-role-rollback.sql
--   Apply:
--     psql "<admin connection>" -v ON_ERROR_STOP=1 -v apply=1 -f scripts/db/ingest-role-rollback.sql
-- Revoking a table's privileges revokes its column privileges too. DROP ROLE fails, and nothing
-- changes, while the role still holds a right anywhere else (it then names it).
\set ON_ERROR_STOP on
BEGIN;
REVOKE ALL ON ALL TABLES IN SCHEMA "public" FROM "atlas_ingest";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA "public" FROM "atlas_ingest";
REVOKE ALL ON SCHEMA "public" FROM "atlas_ingest";
REVOKE ALL ON DATABASE :"DBNAME" FROM "atlas_ingest";
DROP ROLE "atlas_ingest";
\if :{?apply}
COMMIT;
\echo 'Applied: the ingest role is gone.'
\else
ROLLBACK;
\echo 'Dry run: nothing changed. Run again with -v apply=1 to keep it.'
\endif

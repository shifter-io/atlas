-- SEC-INGEST-ROLE-1 (#1022): the login of the manager's upload path, which may only add runs.
-- It reads and adds the rows an uploaded run brings, updates only the lifetime counter columns a
-- new run moves (and test_runs.batch_id, which joins a stored run to its batch), and makes its
-- own temporary tables. No DELETE, TRUNCATE, DDL or other table. src/atlas/dbserver/ingest.py
-- holds the same list; `atlas db-server check` prints these statements for a new role, and
-- tests/test_ingest_role.py checks this file against it and runs the upload path under it.
--
-- Run once per database server, connected to the framework's database as the admin login if
-- it has CREATEROLE, else as the server's superuser. Nothing here holds or prints a password.
--   Dry run (the default): everything rolls back; the last query shows what the role would hold.
--     psql "<admin connection>" -v ON_ERROR_STOP=1 -f scripts/db/ingest-role.sql
--   Apply:
--     psql "<admin connection>" -v ON_ERROR_STOP=1 -v apply=1 -f scripts/db/ingest-role.sql
--   Then the password, typed at the prompt and sent hashed (never on a command line or in Git):
--     psql "<admin connection>" -c '\password atlas_ingest'
-- Undo: scripts/db/ingest-role-rollback.sql, after the orchestrator no longer uses the role.
\set ON_ERROR_STOP on
BEGIN;
CREATE ROLE "atlas_ingest" LOGIN NOINHERIT;
GRANT USAGE ON SCHEMA "public" TO "atlas_ingest";
GRANT SELECT, INSERT ON public.asn_names TO "atlas_ingest";
GRANT SELECT, INSERT ON public.country_verdicts TO "atlas_ingest";
GRANT SELECT, INSERT ON public.dbip_releases TO "atlas_ingest";
GRANT SELECT, INSERT ON public.enrichment_locations TO "atlas_ingest";
GRANT SELECT, INSERT ON public.enrichment_networks TO "atlas_ingest";
GRANT SELECT, INSERT ON public.ip_addresses TO "atlas_ingest";
GRANT INSERT ON public.ip_enrichment TO "atlas_ingest";
GRANT SELECT ON public.ip_enrichment_fields TO "atlas_ingest";
GRANT INSERT ON public.ip_observations TO "atlas_ingest";
GRANT SELECT, INSERT ON public.place_names TO "atlas_ingest";
GRANT SELECT, INSERT ON public.pool_ips TO "atlas_ingest";
GRANT SELECT, INSERT ON public.pool_stats TO "atlas_ingest";
GRANT UPDATE ("unique_ips") ON public.pool_stats TO "atlas_ingest";
GRANT SELECT, INSERT ON public.product_ips TO "atlas_ingest";
GRANT UPDATE ("first_seen", "last_seen", "observation_count", "run_count") ON public.product_ips TO "atlas_ingest";
GRANT SELECT, INSERT ON public.products TO "atlas_ingest";
GRANT SELECT, INSERT ON public.provider_overlap TO "atlas_ingest";
GRANT UPDATE ("shared_ips", "updated_at") ON public.provider_overlap TO "atlas_ingest";
GRANT INSERT ON public.providers TO "atlas_ingest";
GRANT SELECT, INSERT ON public.run_buckets TO "atlas_ingest";
GRANT SELECT, INSERT ON public.run_enrichments TO "atlas_ingest";
GRANT INSERT ON public.run_geo_buckets TO "atlas_ingest";
GRANT SELECT ON public.schema_versions TO "atlas_ingest";
GRANT SELECT ON public.scope_details TO "atlas_ingest";
GRANT SELECT, INSERT ON public.scope_ips TO "atlas_ingest";
GRANT SELECT, INSERT ON public.scope_stats TO "atlas_ingest";
GRANT UPDATE ("metrics", "histogram_steps", "histogram_counts", "updated_at") ON public.scope_stats TO "atlas_ingest";
GRANT INSERT ON public.targets TO "atlas_ingest";
GRANT SELECT, INSERT ON public.test_runs TO "atlas_ingest";
GRANT UPDATE ("batch_id") ON public.test_runs TO "atlas_ingest";
GRANT EXECUTE ON FUNCTION public.artifact_time(timestamp with time zone) TO "atlas_ingest";
GRANT EXECUTE ON FUNCTION public.histogram(steps integer[], counts bigint[]) TO "atlas_ingest";
GRANT EXECUTE ON FUNCTION public.histogram_bins(steps integer[], counts bigint[]) TO "atlas_ingest";
GRANT EXECUTE ON FUNCTION public.run_bucket_counters(b run_buckets) TO "atlas_ingest";
GRANT EXECUTE ON FUNCTION public.stored_run_summary(wanted bigint) TO "atlas_ingest";
GRANT TEMPORARY ON DATABASE :"DBNAME" TO "atlas_ingest";
-- What the role holds now, per table (column grants in brackets), for the operator to read.
SELECT c.relname AS "table",
       array_to_string(ARRAY(
         SELECT p FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p
         WHERE has_table_privilege('atlas_ingest', c.oid, p)), ', ') AS "privileges",
       (SELECT string_agg(a.attname, ', ' ORDER BY a.attnum) FROM pg_attribute a
        WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
          AND NOT has_table_privilege('atlas_ingest', c.oid, 'UPDATE')
          AND has_column_privilege('atlas_ingest', c.oid, a.attnum, 'UPDATE')) AS "update (columns)"
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm')
  AND NOT c.relispartition AND has_any_column_privilege('atlas_ingest', c.oid, 'SELECT, INSERT, UPDATE')
ORDER BY 1;
\if :{?apply}
COMMIT;
\echo 'Applied: the role exists. Set its password with \\password atlas_ingest.'
\else
ROLLBACK;
\echo 'Dry run: nothing changed. Run again with -v apply=1 to keep it.'
\endif

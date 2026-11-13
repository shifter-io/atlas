-- Transactional v4 → v5 migration, applied by storage.migrate() (proxy-index db-migrate).
-- The v4 JSON is kept in temporary tables; after schema.sql recreates the views and functions,
-- storage.MIGRATION_CHECKS requires every rebuilt object to equal its v4 original.
CREATE TEMP TABLE v4_pools ON COMMIT DROP AS SELECT pool_id,definition FROM pool_stats;
CREATE TEMP TABLE v4_scopes ON COMMIT DROP AS SELECT scope_id,scope,metrics FROM scope_stats;
CREATE TEMP TABLE v4_buckets ON COMMIT DROP AS SELECT run_key,bucket_start,counters FROM run_buckets;
CREATE TEMP TABLE v4_summaries ON COMMIT DROP AS
SELECT run_key,stored_run_summary(run_key) AS summary FROM test_runs;
DROP VIEW test_run_details;
DROP FUNCTION stored_run_summary(bigint);
-- True when a JSON value can be held by a column of this kind (absent values cannot).
CREATE OR REPLACE FUNCTION pg_temp.fits(v jsonb, kind text) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
SELECT CASE WHEN v IS NULL THEN false WHEN kind='json' OR jsonb_typeof(v)='null' THEN true
 WHEN kind='text' THEN jsonb_typeof(v)='string'
 WHEN kind='number' THEN jsonb_typeof(v)='number'
 WHEN kind='integer' AND jsonb_typeof(v)='number' AND v::text ~ '^-?[0-9]+$'
 THEN v::text::numeric BETWEEN -2147483648 AND 2147483647 ELSE false END
$$;
CREATE FUNCTION pg_temp.adapter_fits(a jsonb) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
SELECT CASE WHEN jsonb_typeof(a) IS DISTINCT FROM 'object' THEN false
 WHEN NOT a ?& ARRAY['adapter','version','gateway','pool','parameters','session_policy',
  'effective_ttl_seconds','extra_flags'] OR (SELECT count(*) FROM jsonb_object_keys(a))<>8 THEN false
 WHEN jsonb_typeof(a->'adapter')<>'string' OR jsonb_typeof(a->'gateway')<>'object' THEN false
 WHEN NOT a->'gateway' ?& ARRAY['host','port']
  OR (SELECT count(*) FROM jsonb_object_keys(a->'gateway'))<>2 THEN false
 ELSE pg_temp.fits(a->'version','text') AND pg_temp.fits(a->'gateway'->'host','text')
  AND pg_temp.fits(a->'gateway'->'port','integer') AND pg_temp.fits(a->'pool','text')
  AND pg_temp.fits(a->'session_policy','text') AND pg_temp.fits(a->'effective_ttl_seconds','integer')
 END
$$;
-- Millisecond gaps between consecutive bins (the first is absolute), in artifact order.
CREATE FUNCTION pg_temp.steps(bins jsonb) RETURNS integer[] LANGUAGE SQL IMMUTABLE AS $$
SELECT ARRAY(SELECT (b->>0)::integer - coalesce(lag((b->>0)::integer) OVER (ORDER BY i),0)
 FROM jsonb_array_elements(bins) WITH ORDINALITY e(b,i) ORDER BY i)
$$;
-- Pools: typed profile columns, unknown or non-conforming keys verbatim in extra.
ALTER TABLE pool_stats ADD COLUMN target_id text, ADD COLUMN target_url text,
 ADD COLUMN target_engine text, ADD COLUMN target_version text, ADD COLUMN proxy_protocol text,
 ADD COLUMN requested_country text, ADD COLUMN runner_location text, ADD COLUMN adapter text,
 ADD COLUMN adapter_version text, ADD COLUMN gateway_host text, ADD COLUMN gateway_port integer,
 ADD COLUMN account_pool text, ADD COLUMN parameters jsonb, ADD COLUMN extra_flags jsonb,
 ADD COLUMN session_policy text, ADD COLUMN ttl_seconds integer, ADD COLUMN extra jsonb;
CREATE TEMP TABLE v5_pools ON COMMIT DROP AS
SELECT p.pool_id,p.definition->'target' AS t,p.definition->'config' AS c,
 pg_temp.adapter_fits(p.definition->'config'->'provider_config') AS typed,
 array_remove(ARRAY[CASE WHEN p.definition->'config'->'provider'=to_jsonb(r.provider) THEN 'provider' END,
  CASE WHEN p.definition->'config'->'product'=to_jsonb(r.name) THEN 'product' END,
  CASE WHEN p.definition->'config'->'target'=p.definition->'target'->'id' THEN 'target' END,
  CASE WHEN pg_temp.fits(p.definition->'config'->'requested_country','text')
   THEN 'requested_country' END,
  CASE WHEN pg_temp.fits(p.definition->'config'->'runner_location','text') THEN 'runner_location' END,
  CASE WHEN jsonb_typeof(p.definition->'config'->'proxy_protocol')='string' THEN 'proxy_protocol' END,
  CASE WHEN pg_temp.adapter_fits(p.definition->'config'->'provider_config')
   THEN 'provider_config' END],NULL) AS taken,
 ARRAY['id'] || ARRAY(SELECT k FROM unnest(ARRAY['url','engine','version']) k
  WHERE pg_temp.fits(p.definition->'target'->k,'text')) AS taken_target
FROM pool_stats p JOIN products r USING(product_id);
UPDATE pool_stats p SET target_id=v.t->>'id',
 target_url=CASE WHEN 'url'=ANY(v.taken_target) THEN v.t->>'url' END,
 target_engine=CASE WHEN 'engine'=ANY(v.taken_target) THEN v.t->>'engine' END,
 target_version=CASE WHEN 'version'=ANY(v.taken_target) THEN v.t->>'version' END,
 requested_country=CASE WHEN 'requested_country'=ANY(v.taken) THEN v.c->>'requested_country' END,
 runner_location=CASE WHEN 'runner_location'=ANY(v.taken) THEN v.c->>'runner_location' END,
 proxy_protocol=CASE WHEN 'proxy_protocol'=ANY(v.taken) THEN v.c->>'proxy_protocol' END,
 adapter=CASE WHEN v.typed THEN v.c->'provider_config'->>'adapter' END,
 adapter_version=CASE WHEN v.typed THEN v.c->'provider_config'->>'version' END,
 gateway_host=CASE WHEN v.typed THEN v.c->'provider_config'->'gateway'->>'host' END,
 gateway_port=CASE WHEN v.typed THEN (v.c->'provider_config'->'gateway'->>'port')::integer END,
 account_pool=CASE WHEN v.typed THEN v.c->'provider_config'->>'pool' END,
 parameters=CASE WHEN v.typed THEN nullif(v.c->'provider_config'->'parameters','null') END,
 extra_flags=CASE WHEN v.typed THEN nullif(v.c->'provider_config'->'extra_flags','null') END,
 session_policy=CASE WHEN v.typed THEN v.c->'provider_config'->>'session_policy' END,
 ttl_seconds=CASE WHEN v.typed
  THEN (v.c->'provider_config'->>'effective_ttl_seconds')::integer END,
 extra=nullif(CASE WHEN v.c - v.taken='{}' THEN '{}'
  ELSE jsonb_build_object('config',v.c - v.taken) END
  || CASE WHEN v.t - v.taken_target='{}' THEN '{}'
  ELSE jsonb_build_object('target',v.t - v.taken_target) END,'{}')
FROM v5_pools v WHERE p.pool_id=v.pool_id;
ALTER TABLE pool_stats ALTER COLUMN target_id SET NOT NULL, DROP COLUMN definition;
-- Scopes: performance settings typed; the rest comes from the pool. Histogram as two arrays.
ALTER TABLE scope_stats ADD COLUMN concurrency integer, ADD COLUMN timeout_seconds numeric,
 ADD COLUMN requests_per_second numeric, ADD COLUMN histogram_steps integer[] NOT NULL DEFAULT '{}',
 ADD COLUMN histogram_counts bigint[] NOT NULL DEFAULT '{}';
UPDATE scope_stats SET concurrency=(scope->'config'->>'concurrency')::integer,
 timeout_seconds=(scope->'config'->>'timeout_seconds')::numeric,
 requests_per_second=(scope->'config'->>'requests_per_second')::numeric,
 histogram_steps=pg_temp.steps(metrics->'latency_histogram'->'bins'),
 histogram_counts=ARRAY(SELECT (b->>1)::bigint FROM jsonb_array_elements(
  metrics->'latency_histogram'->'bins') WITH ORDINALITY e(b,i) ORDER BY i),
 metrics=metrics - 'latency_histogram';
ALTER TABLE scope_stats ALTER COLUMN concurrency SET NOT NULL,
 ALTER COLUMN timeout_seconds SET NOT NULL, DROP COLUMN scope;
-- Runs: drop summary keys that the typed columns reproduce exactly.
UPDATE test_runs SET summary=summary - array_remove(ARRAY[
 CASE WHEN summary->'run_id'=to_jsonb(run_id) THEN 'run_id' END,
 CASE WHEN summary->'status'=to_jsonb(status) THEN 'status' END,
 CASE WHEN summary->>'started_at'=to_char(started_at AT TIME ZONE 'UTC',
  'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') THEN 'started_at' END,
 CASE WHEN summary->>'finished_at'=to_char(finished_at AT TIME ZONE 'UTC',
  'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') THEN 'finished_at' END],NULL);
-- Buckets: typed counters and histogram arrays; unknown keys and odd labels in extra.
ALTER TABLE run_buckets ADD COLUMN completed bigint, ADD COLUMN successes bigint,
 ADD COLUMN response_body_bytes bigint, ADD COLUMN errors jsonb, ADD COLUMN status_codes jsonb,
 ADD COLUMN histogram_steps integer[], ADD COLUMN histogram_counts integer[], ADD COLUMN extra jsonb;
UPDATE run_buckets SET completed=(counters->>'completed')::bigint,
 successes=(counters->>'successes')::bigint,
 response_body_bytes=(counters->>'response_body_bytes')::bigint,
 errors=counters->'errors', status_codes=counters->'status_codes',
 histogram_steps=pg_temp.steps(counters->'latency_histogram'->'bins'),
 histogram_counts=ARRAY(SELECT (b->>1)::integer FROM jsonb_array_elements(
  counters->'latency_histogram'->'bins') WITH ORDINALITY e(b,i) ORDER BY i),
 extra=nullif((counters - ARRAY['bucket_start','completed','successes','response_body_bytes',
  'errors','status_codes','latency_histogram'])
  || CASE WHEN counters->>'bucket_start'=to_char(bucket_start AT TIME ZONE 'UTC',
   'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') THEN '{}'
   ELSE jsonb_build_object('bucket_start',counters->'bucket_start') END
  || CASE WHEN (SELECT count(*) FROM jsonb_object_keys(counters->'latency_histogram'))=2 THEN '{}'
   ELSE jsonb_build_object('latency_histogram',counters->'latency_histogram') END,'{}');
ALTER TABLE run_buckets ALTER COLUMN completed SET NOT NULL, ALTER COLUMN successes SET NOT NULL,
 ALTER COLUMN response_body_bytes SET NOT NULL, ALTER COLUMN errors SET NOT NULL,
 ALTER COLUMN status_codes SET NOT NULL, ALTER COLUMN histogram_steps SET NOT NULL,
 ALTER COLUMN histogram_counts SET NOT NULL,
 ADD CHECK(cardinality(histogram_steps) = cardinality(histogram_counts)), DROP COLUMN counters;

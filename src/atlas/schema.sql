-- Schema v18: one canonical address, product membership, shared enrichment, typed test profiles,
-- the census/pulse/load scheduling, live progress, geography buckets and event tape, one
-- shared list of place and ASN names, the daily tests' estimated hours, and whether each
-- country counts for a network.
CREATE TABLE IF NOT EXISTS schema_versions (
 version integer PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS providers (id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS products (
 product_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider text NOT NULL REFERENCES providers(id), name text NOT NULL, UNIQUE(provider,name)
);
-- Census/pulse reference data, seeded by proxy-index db-seed from configs/.
CREATE TABLE IF NOT EXISTS runner_regions (
 region_id smallint PRIMARY KEY, slug text NOT NULL UNIQUE,
 cloud text NOT NULL DEFAULT 'digitalocean', pattern text NOT NULL,
 enabled boolean NOT NULL DEFAULT true
);
-- census_time is the country's daily UTC census slot.
CREATE TABLE IF NOT EXISTS countries (
 country_code char(2) PRIMARY KEY CHECK(country_code ~ '^[A-Z]{2}$'), name text NOT NULL,
 region_id smallint NOT NULL REFERENCES runner_regions(region_id), census_time time NOT NULL,
 enabled boolean NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS countries_region ON countries(region_id);
-- Every target a stored pool or scope names. Ingest adds unknown targets unscheduled (no label,
-- census and pulse off); db-seed then fills in their schedule.
CREATE TABLE IF NOT EXISTS targets (
 target_id text PRIMARY KEY, label text, url text NOT NULL, engine text NOT NULL DEFAULT 'http',
 pass_condition text, timeout_ms integer NOT NULL DEFAULT 15000 CHECK(timeout_ms > 0),
 census boolean NOT NULL DEFAULT false,
 census_concurrency integer NOT NULL DEFAULT 1000 CHECK(census_concurrency > 0),
 pulse_enabled boolean NOT NULL DEFAULT true,
 pulse_interval_minutes integer NOT NULL DEFAULT 30 CHECK(pulse_interval_minutes > 0),
 pulse_requests integer NOT NULL DEFAULT 100 CHECK(pulse_requests > 0),
 pulse_concurrency integer NOT NULL DEFAULT 25 CHECK(pulse_concurrency > 0),
 sort_order integer NOT NULL DEFAULT 0
);
-- One always-on node per provider product per region.
CREATE TABLE IF NOT EXISTS nodes (
 node_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 provider text NOT NULL REFERENCES providers(id), product text NOT NULL, host text,
 token_hash text NOT NULL UNIQUE, status text NOT NULL DEFAULT 'offline',
 last_heartbeat timestamptz, version text, open_conns integer, req_per_s double precision,
 rss_mb integer, UNIQUE(region_id,provider,product),
 -- PULSE-STATE-2: the host's I/O pressure (PSI some avg10, %), available memory and pages
 -- swapped since the previous heartbeat (node_health).
 io_pressure double precision, mem_available_mb integer, swap_pages bigint
);
-- A batch is one synchronized slot: every provider in the region starts it at start_at. A 'load'
-- batch is the latency-under-load wave a pulse slot adds in the reference country (same
-- scheduled_for). retry_of names the census a provider-outage rerun replaces (one product).
-- A 'browser' batch is one browser-pulse slot of a browser target (schema v11).
CREATE TABLE IF NOT EXISTS test_batches (
 batch_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK(kind IN ('census','pulse','load','browser')),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 target_id text NOT NULL REFERENCES targets(target_id),
 scheduled_for timestamptz NOT NULL, start_at timestamptz NOT NULL,
 started_at timestamptz, finished_at timestamptz,
 status text NOT NULL DEFAULT 'scheduled'
  CHECK(status IN ('scheduled','running','done','failed','skipped','killed')),
 UNIQUE(kind,country_code,target_id,scheduled_for),
 retry_of bigint REFERENCES test_batches(batch_id)
);
CREATE INDEX IF NOT EXISTS test_batches_open ON test_batches(status)
 WHERE status IN ('scheduled','running');
CREATE INDEX IF NOT EXISTS test_batches_target ON test_batches(kind,target_id,scheduled_for);
CREATE TABLE IF NOT EXISTS ip_addresses (
 ip_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ip inet NOT NULL UNIQUE,
 CHECK(masklen(ip) = CASE family(ip) WHEN 4 THEN 32 ELSE 128 END)
);
CREATE TABLE IF NOT EXISTS product_ips (
 product_id integer REFERENCES products(product_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
 observation_count bigint NOT NULL CHECK(observation_count > 0),
 run_count bigint NOT NULL CHECK(run_count > 0),
 CHECK(first_seen <= last_seen), PRIMARY KEY(product_id,ip_id)
);
CREATE INDEX IF NOT EXISTS product_ips_reverse ON product_ips(ip_id,product_id);
-- A pool is the test profile without performance settings. Unknown or non-conforming keys stay
-- verbatim in extra as {"config": {...}, "target": {...}}; pool_details rebuilds the definition.
CREATE TABLE IF NOT EXISTS pool_stats (
 pool_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id),
 pool_hash text NOT NULL, unique_ips bigint NOT NULL DEFAULT 0,
 target_id text NOT NULL REFERENCES targets(target_id), target_url text, target_engine text, target_version text,
 proxy_protocol text, requested_country text, runner_location text,
 adapter text, adapter_version text, gateway_host text, gateway_port integer, account_pool text,
 parameters jsonb, extra_flags jsonb, session_policy text, ttl_seconds integer, extra jsonb,
 UNIQUE(product_id,pool_hash)
);
CREATE TABLE IF NOT EXISTS pool_ips (
 pool_id bigint REFERENCES pool_stats(pool_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 PRIMARY KEY(pool_id,ip_id)
);
-- A scope is a pool plus performance settings. Lifetime totals stay in metrics; the merged
-- latency histogram uses the same parallel step/count arrays as run_buckets.
CREATE TABLE IF NOT EXISTS scope_stats (
 scope_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 pool_id bigint NOT NULL REFERENCES pool_stats(pool_id),
 product_id integer NOT NULL REFERENCES products(product_id),
 target text NOT NULL REFERENCES targets(target_id), scope_hash text NOT NULL, metrics jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now(),
 concurrency integer NOT NULL, timeout_seconds numeric NOT NULL, requests_per_second numeric,
 histogram_steps integer[] NOT NULL DEFAULT '{}', histogram_counts bigint[] NOT NULL DEFAULT '{}',
 UNIQUE(product_id,target,scope_hash)
);
-- Exact membership per scope, as pool_ips per pool: each scope's unique_ips counts its new rows.
CREATE TABLE IF NOT EXISTS scope_ips (
 scope_id bigint REFERENCES scope_stats(scope_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 PRIMARY KEY(scope_id,ip_id)
);
-- summary omits run_id, status and timestamps whenever the columns reproduce them exactly.
CREATE TABLE IF NOT EXISTS test_runs (
 run_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, run_id uuid NOT NULL UNIQUE, scope_id bigint NOT NULL REFERENCES scope_stats(scope_id),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('completed','cancelled','failed')),
 summary_sha256 text NOT NULL, summary jsonb NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now(), batch_id bigint REFERENCES test_batches(batch_id)
);
CREATE INDEX IF NOT EXISTS test_runs_scope ON test_runs(scope_id,finished_at);
CREATE INDEX IF NOT EXISTS test_runs_batch ON test_runs(batch_id) WHERE batch_id IS NOT NULL;
-- Live counters of each provider's run in a batch; run_key links the ingested artifact.
CREATE TABLE IF NOT EXISTS batch_runs (
 batch_id bigint NOT NULL REFERENCES test_batches(batch_id),
 product_id integer NOT NULL REFERENCES products(product_id),
 node_id integer REFERENCES nodes(node_id),
 status text NOT NULL DEFAULT 'queued'
  CHECK(status IN ('queued','running','uploading','done','failed','skipped','killed')),
 started_at timestamptz, finished_at timestamptz, last_progress_at timestamptz,
 requests bigint NOT NULL DEFAULT 0, successes bigint NOT NULL DEFAULT 0,
 unique_ips bigint NOT NULL DEFAULT 0, challenges bigint NOT NULL DEFAULT 0,
 blocks bigint NOT NULL DEFAULT 0, timeouts bigint NOT NULL DEFAULT 0,
 proxy_errors bigint NOT NULL DEFAULT 0, bytes_up bigint NOT NULL DEFAULT 0,
 bytes_down bigint NOT NULL DEFAULT 0, stop_reason text, error text,
 run_key bigint UNIQUE REFERENCES test_runs(run_key), PRIMARY KEY(batch_id,product_id),
 -- PULSE-STATE-2: a run's own start when it differs from its batch's (a stalled node's run
 -- moved later, pulse_moves); NULL: its batch's start_at.
 start_at timestamptz
);
CREATE INDEX IF NOT EXISTS batch_runs_node ON batch_runs(node_id,status);
CREATE TABLE IF NOT EXISTS ip_observations (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 ip_id bigint NOT NULL REFERENCES ip_addresses(ip_id), count bigint NOT NULL CHECK(count > 0),
 first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
 CHECK(first_seen <= last_seen), PRIMARY KEY(run_key,bucket_start,ip_id)
) PARTITION BY RANGE(bucket_start);
-- Counting a time slice's distinct IPs across runs (schema v12) reads (bucket_start, ip_id).
CREATE INDEX IF NOT EXISTS ip_observations_time_ip ON ip_observations(bucket_start,ip_id);
-- Latency bins keep artifact order as parallel arrays; each step is the millisecond gap from the
-- previous bin (the first is absolute), which compresses far better than absolute values.
-- extra holds unknown counter keys and a non-canonical bucket_start label verbatim;
-- run_bucket_counters() rebuilds the artifact JSON. The six outcome counters are emitted only
-- when outcome_counters is true: artifacts older than v6 lack them and must rebuild without them.
CREATE TABLE IF NOT EXISTS run_buckets (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 completed bigint NOT NULL, successes bigint NOT NULL, response_body_bytes bigint NOT NULL,
 errors jsonb NOT NULL, status_codes jsonb NOT NULL,
 histogram_steps integer[] NOT NULL, histogram_counts integer[] NOT NULL, extra jsonb,
 challenges bigint NOT NULL DEFAULT 0, blocks bigint NOT NULL DEFAULT 0,
 timeouts bigint NOT NULL DEFAULT 0, proxy_errors bigint NOT NULL DEFAULT 0,
 bytes_up bigint NOT NULL DEFAULT 0, bytes_down bigint NOT NULL DEFAULT 0,
 outcome_counters boolean NOT NULL DEFAULT false,
 CHECK(cardinality(histogram_steps) = cardinality(histogram_counts)), PRIMARY KEY(run_key,bucket_start)
) PARTITION BY RANGE(bucket_start);
CREATE TABLE IF NOT EXISTS dbip_releases (
 release_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 text NOT NULL UNIQUE, metadata jsonb NOT NULL
);
-- One list of place names and one of ASN names, shared by enrichment and the calculated layer
-- (calculated_breakdowns); each name is stored once, and the all-NULL row is the unknown member.
CREATE TABLE IF NOT EXISTS place_names (
 place_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 country_code text, country text, region_code text, region text, city text,
 UNIQUE NULLS NOT DISTINCT(country_code,country,region_code,region,city)
);
CREATE INDEX IF NOT EXISTS place_names_city ON place_names(country_code,city);
CREATE TABLE IF NOT EXISTS asn_names (
 asn_name_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn bigint, name text, UNIQUE NULLS NOT DISTINCT(asn,name)
);
-- A location is a place at one coordinate; a network is an ASN name with the per-IP network
-- attributes. The *_fields views give both with their names, as the tables held them up to v8.
CREATE TABLE IF NOT EXISTS enrichment_locations (
 location_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 place_id integer NOT NULL REFERENCES place_names(place_id),
 latitude double precision, longitude double precision,
 UNIQUE NULLS NOT DISTINCT(place_id,latitude,longitude)
);
CREATE TABLE IF NOT EXISTS enrichment_networks (
 network_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn_name_id integer NOT NULL REFERENCES asn_names(asn_name_id),
 isp text, organization text, connection_type text, usage_type text,
 UNIQUE NULLS NOT DISTINCT(asn_name_id,isp,organization,connection_type,usage_type)
);
CREATE OR REPLACE VIEW enrichment_location_fields AS
SELECT l.location_id,p.country_code,p.country,p.region_code,p.region,p.city,l.latitude,l.longitude
FROM enrichment_locations l JOIN place_names p USING(place_id);
CREATE OR REPLACE VIEW enrichment_network_fields AS
SELECT n.network_id,n.isp,n.organization,a.asn,a.name AS asn_organization,n.connection_type,
 n.usage_type
FROM enrichment_networks n JOIN asn_names a USING(asn_name_id);
-- Outcome and latency per exit location x network x 5-minute bucket. Requests without an exit
-- address use the all-NULL location and network rows.
CREATE TABLE IF NOT EXISTS run_geo_buckets (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 location_id integer NOT NULL REFERENCES enrichment_locations(location_id),
 network_id integer NOT NULL REFERENCES enrichment_networks(network_id),
 completed bigint NOT NULL, successes bigint NOT NULL, challenges bigint NOT NULL,
 blocks bigint NOT NULL, timeouts bigint NOT NULL, bytes_up bigint NOT NULL,
 bytes_down bigint NOT NULL, histogram_steps integer[] NOT NULL, histogram_counts integer[] NOT NULL,
 CHECK(cardinality(histogram_steps) = cardinality(histogram_counts)),
 PRIMARY KEY(run_key,bucket_start,location_id,network_id)
) PARTITION BY RANGE(bucket_start);
CREATE TABLE IF NOT EXISTS ip_enrichment (
 ip_id bigint REFERENCES ip_addresses(ip_id), release_id integer REFERENCES dbip_releases(release_id),
 location_id integer NOT NULL REFERENCES enrichment_locations(location_id),
 network_id integer NOT NULL REFERENCES enrichment_networks(network_id),
 matched boolean, extra_attributes jsonb, PRIMARY KEY(ip_id,release_id)
);
CREATE INDEX IF NOT EXISTS ip_enrichment_location ON ip_enrichment(release_id,location_id);
CREATE INDEX IF NOT EXISTS ip_enrichment_network ON ip_enrichment(release_id,network_id);
CREATE OR REPLACE VIEW ip_enrichment_fields AS
SELECT e.ip_id,e.release_id,e.matched,l.country_code,l.country,l.region_code,l.region,l.city,
 l.latitude,l.longitude,n.isp,n.organization,n.asn,n.asn_organization,n.connection_type,n.usage_type,
 coalesce(e.extra_attributes,'{}'::jsonb) AS extra_attributes
FROM ip_enrichment e JOIN enrichment_location_fields l USING(location_id)
JOIN enrichment_network_fields n USING(network_id);
CREATE TABLE IF NOT EXISTS run_enrichments (
 run_key bigint REFERENCES test_runs(run_key), release_id integer REFERENCES dbip_releases(release_id),
 artifact_sha256 text NOT NULL, country_counts jsonb NOT NULL, PRIMARY KEY(run_key,release_id)
);
-- Lifetime, all-products overlap; each shared IP contributes once per provider pair.
CREATE TABLE IF NOT EXISTS provider_overlap (
 provider_a text REFERENCES providers(id), provider_b text REFERENCES providers(id),
 shared_ips bigint NOT NULL CHECK(shared_ips > 0), updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(provider_a < provider_b), PRIMARY KEY(provider_a,provider_b)
);
-- Readable joins for database viewers; views do not duplicate stored addresses.
CREATE OR REPLACE VIEW product_ip_details AS
SELECT p.provider,p.name AS product,a.ip,m.first_seen,m.last_seen,m.observation_count,m.run_count,
       m.product_id,m.ip_id
FROM product_ips m JOIN products p USING(product_id) JOIN ip_addresses a USING(ip_id);
CREATE OR REPLACE VIEW ip_enrichment_details AS
SELECT a.ip,r.sha256 AS dbip_release,
       e.matched,
       e.country_code,
       e.country,
       e.region_code,
       e.region,
       e.city,
       e.latitude,
       e.longitude,
       e.isp,
       e.organization,
       e.asn,
       e.asn_organization,
       e.connection_type,
       e.usage_type,
       e.extra_attributes,e.ip_id,e.release_id
FROM ip_enrichment_fields e JOIN ip_addresses a USING(ip_id) JOIN dbip_releases r USING(release_id);
-- Artifact JSON is rebuilt from typed columns; it is not stored a second time.
CREATE OR REPLACE FUNCTION artifact_time(timestamptz) RETURNS text LANGUAGE SQL STABLE AS
$$SELECT to_char($1 AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')$$;
CREATE OR REPLACE FUNCTION histogram(steps integer[], counts bigint[])
RETURNS TABLE(ms bigint, n bigint, i bigint) LANGUAGE SQL IMMUTABLE AS $$
SELECT sum(s) OVER (ORDER BY i),n,i FROM unnest(steps,counts) WITH ORDINALITY h(s,n,i)
$$;
CREATE OR REPLACE FUNCTION histogram_bins(steps integer[], counts bigint[]) RETURNS jsonb
LANGUAGE SQL IMMUTABLE AS $$
SELECT coalesce(jsonb_agg(jsonb_build_array(ms,n) ORDER BY i),'[]') FROM histogram(steps,counts)
$$;
CREATE OR REPLACE FUNCTION run_bucket_counters(b run_buckets) RETURNS jsonb
LANGUAGE SQL STABLE AS $$
SELECT jsonb_build_object('bucket_start',artifact_time(b.bucket_start),'completed',b.completed,
 'successes',b.successes,'errors',b.errors,'status_codes',b.status_codes,
 'response_body_bytes',b.response_body_bytes,'latency_histogram',jsonb_build_object(
 'format','ceil_ms_v1','bins',histogram_bins(b.histogram_steps,b.histogram_counts)))
 || CASE WHEN b.outcome_counters THEN jsonb_build_object('challenges',b.challenges,
     'blocks',b.blocks,'timeouts',b.timeouts,'proxy_errors',b.proxy_errors,
     'bytes_up',b.bytes_up,'bytes_down',b.bytes_down) ELSE '{}' END
 || coalesce(b.extra,'{}')
$$;
CREATE OR REPLACE VIEW pool_details AS
SELECT r.provider,r.name AS product,p.pool_hash,jsonb_build_object(
 'target',jsonb_build_object('id',p.target_id,'url',p.target_url,'engine',p.target_engine,
  'version',p.target_version) || coalesce(p.extra->'target','{}'),
 'config',jsonb_build_object('target',p.target_id,'provider',r.provider,'product',r.name,
  'requested_country',p.requested_country,'runner_location',p.runner_location)
  || CASE WHEN p.proxy_protocol IS NULL THEN '{}'
     ELSE jsonb_build_object('proxy_protocol',p.proxy_protocol) END
  || CASE WHEN p.adapter IS NULL THEN '{}' ELSE jsonb_build_object('provider_config',
     jsonb_build_object('adapter',p.adapter,'version',p.adapter_version,'gateway',
      jsonb_build_object('host',p.gateway_host,'port',p.gateway_port),'pool',p.account_pool,
      'parameters',p.parameters,'session_policy',p.session_policy,
      'effective_ttl_seconds',p.ttl_seconds,'extra_flags',p.extra_flags)) END
  || coalesce(p.extra->'config','{}')) AS definition,
 p.unique_ips,p.pool_id,p.product_id
FROM pool_stats p JOIN products r USING(product_id);
CREATE OR REPLACE VIEW scope_details AS
SELECT d.provider,d.product,s.target,s.scope_hash,jsonb_set(d.definition,'{config}',
 (d.definition->'config') || jsonb_build_object('concurrency',s.concurrency,
 'timeout_seconds',s.timeout_seconds,'requests_per_second',s.requests_per_second)) AS scope,
 s.metrics || jsonb_build_object('latency_histogram',jsonb_build_object('format','ceil_ms_v1',
 'bins',histogram_bins(s.histogram_steps,s.histogram_counts))) AS metrics,
 s.updated_at,s.scope_id,s.pool_id,s.product_id
FROM scope_stats s JOIN pool_details d USING(pool_id);
CREATE OR REPLACE FUNCTION stored_run_summary(wanted bigint) RETURNS jsonb
LANGUAGE SQL STABLE AS $$
SELECT jsonb_build_object('run_id',t.run_id,'status',t.status,
 'started_at',artifact_time(t.started_at),'finished_at',artifact_time(t.finished_at))
 || (t.summary - 'config_overrides') || jsonb_build_object(
 'config',(s.scope->'config') || (t.summary->'config_overrides'),
 'target',s.scope->'target',
 'buckets',coalesce((SELECT jsonb_agg(run_bucket_counters(b) ORDER BY b.bucket_start)
                    FROM run_buckets b WHERE b.run_key=wanted),'[]'),
 'latency_histogram',jsonb_build_object('format','ceil_ms_v1','bins',
                    coalesce((SELECT jsonb_agg(jsonb_build_array(ms,n) ORDER BY ms) FROM
                     (SELECT h.ms,sum(h.n)::bigint AS n FROM run_buckets b,
                      histogram(b.histogram_steps,b.histogram_counts) h
                      WHERE b.run_key=wanted GROUP BY h.ms) bins),'[]')))
FROM test_runs t JOIN scope_details s USING(scope_id) WHERE t.run_key=wanted
$$;
CREATE OR REPLACE VIEW test_run_details AS
SELECT t.run_id,p.provider,p.name AS product,s.target,t.started_at,t.finished_at,t.status,
       stored_run_summary(t.run_key) AS summary,s.scope_id,s.scope_hash,t.imported_at
FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id);
-- Live tape: census and pulse milestones, incidents and audit findings.
CREATE TABLE IF NOT EXISTS test_events (
 event_id bigint GENERATED ALWAYS AS IDENTITY, at timestamptz NOT NULL DEFAULT now(),
 kind text NOT NULL, batch_id bigint REFERENCES test_batches(batch_id),
 product_id integer REFERENCES products(product_id),
 country_code char(2) REFERENCES countries(country_code),
 target_id text REFERENCES targets(target_id),
 region_id smallint REFERENCES runner_regions(region_id),
 signal smallint NOT NULL DEFAULT 0 CHECK(signal IN (-1,0,1)),
 severity text NOT NULL DEFAULT 'info' CHECK(severity IN ('info','minor','major','critical')),
 data jsonb NOT NULL DEFAULT '{}', PRIMARY KEY(event_id,at)
) PARTITION BY RANGE(at);
CREATE INDEX IF NOT EXISTS test_events_at ON test_events(at DESC);
CREATE INDEX IF NOT EXISTS test_events_product ON test_events(product_id,at DESC);
-- The rare-value reads (schema v15): the non-routine rows (the API's notable read and its kind and
-- tab filters, whose clause repeats this predicate, store.routineSQL), and the country and target
-- filters. Partial, so the routine bulk of the tape costs them nothing.
CREATE INDEX IF NOT EXISTS test_events_notable ON test_events(at DESC,event_id DESC)
 WHERE NOT (left(kind,7)='census_' OR kind IN
 ('daily_test_started','daily_test_finished','estimate_hour_released','pulse_finished'));
CREATE INDEX IF NOT EXISTS test_events_country ON test_events(country_code,at DESC)
 WHERE country_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS test_events_target ON test_events(target_id,at DESC)
 WHERE target_id IS NOT NULL;
-- The live tape's counts (schema v13) per 30-minute bucket of `at`, product (0: no product) and
-- tape tab, kept by one statement trigger per insert into test_events (every partition fires
-- the parent's): good counts signal > 0, bad signal < 0. The API's /v1/events/stats reads it.
CREATE TABLE IF NOT EXISTS test_event_buckets (
 bucket_start timestamptz NOT NULL, product_id integer NOT NULL, category text NOT NULL,
 events integer NOT NULL, good integer NOT NULL, bad integer NOT NULL, last_at timestamptz NOT NULL,
 PRIMARY KEY(bucket_start,product_id,category)
);
-- The buckets recounted from test_events (the v13 fill, and the replay wipe's recount). The tab is
-- detect.category (the API's store.Category).
CREATE OR REPLACE VIEW test_event_bucket_counts AS
SELECT date_bin('30 minutes',at,'2000-01-01 00:00+00') AS bucket_start,
 coalesce(product_id,0) AS product_id,CASE WHEN left(kind,7)='census_'
 OR kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
 WHEN left(kind,6)='pulse_' THEN 'pulse' WHEN kind IN ('new_hosting_asn','ip_overlap') THEN 'audit'
 ELSE 'incident' END AS category,count(*)::integer AS events,
 (count(*) FILTER (WHERE signal>0))::integer AS good,
 (count(*) FILTER (WHERE signal<0))::integer AS bad,max(at) AS last_at
FROM test_events GROUP BY 1,2,3;
-- One upsert per statement, its keys in order (concurrent inserts lock rows in the same order); it
-- calls no function, so the roles that insert events need no EXECUTE right.
CREATE OR REPLACE FUNCTION test_event_buckets_add() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO test_event_buckets AS b
 SELECT date_bin('30 minutes',at,'2000-01-01 00:00+00'),coalesce(product_id,0),
  CASE WHEN left(kind,7)='census_'
  OR kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
  WHEN left(kind,6)='pulse_' THEN 'pulse' WHEN kind IN ('new_hosting_asn','ip_overlap') THEN 'audit'
  ELSE 'incident' END,count(*),count(*) FILTER (WHERE signal>0),count(*) FILTER (WHERE signal<0),
  max(at)
 FROM added GROUP BY 1,2,3 ORDER BY 1,2,3
 ON CONFLICT (bucket_start,product_id,category) DO UPDATE SET events=b.events+excluded.events,
  good=b.good+excluded.good,bad=b.bad+excluded.bad,last_at=greatest(b.last_at,excluded.last_at);
 RETURN NULL;
END$$;
DO $$BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='test_event_buckets_add'
  AND tgrelid='test_events'::regclass) THEN
  CREATE TRIGGER test_event_buckets_add AFTER INSERT ON test_events REFERENCING NEW TABLE AS added
  FOR EACH STATEMENT EXECUTE FUNCTION test_event_buckets_add();
 END IF;
END$$;
-- Time a product was down in a country: opened by a census that stalled (source 'census') or a
-- pulse 'down' event (source 'pulse'); ended_at stays NULL while it is still down. end_reason
-- (schema v16) is NULL when the data ended it, 'too_few_ips' when the country stopped counting
-- for the product (country_verdicts) while it was open.
CREATE TABLE IF NOT EXISTS outages (
 outage_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 region_id smallint REFERENCES runner_regions(region_id),
 source text NOT NULL CHECK(source IN ('census','pulse')),
 batch_id bigint REFERENCES test_batches(batch_id),
 started_at timestamptz NOT NULL, ended_at timestamptz, CHECK(ended_at >= started_at),
 end_reason text CHECK(end_reason IN ('too_few_ips'))
);
CREATE INDEX IF NOT EXISTS outages_product ON outages(product_id,started_at);
-- The latency-under-load reference country of each runner region for one UTC day, chosen by the
-- day's first pulse plan: the fastest country tested from the region over the trailing days
-- ('fastest'), an operator override, the configured default without enough history, or the
-- region's first enabled country. The load batches of the day run there.
CREATE TABLE IF NOT EXISTS load_references (
 day date NOT NULL, region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 basis text NOT NULL CHECK(basis IN ('fastest','override','default','first_enabled')),
 p50_ms integer, successes bigint, candidates integer NOT NULL DEFAULT 0,
 chosen_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(day,region_id)
);
CREATE INDEX IF NOT EXISTS outages_open ON outages(product_id,country_code) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS job_runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','ok','failed','skipped')),
 error text, duration_ms integer
);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job,started_at DESC);
CREATE TABLE IF NOT EXISTS alerts (
 alert_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL,
 severity text NOT NULL DEFAULT 'minor' CHECK(severity IN ('info','minor','major','critical')),
 message text NOT NULL, data jsonb NOT NULL DEFAULT '{}', dedupe_key text UNIQUE,
 occurrences integer NOT NULL DEFAULT 1, first_at timestamptz NOT NULL DEFAULT now(),
 last_at timestamptz NOT NULL DEFAULT now(), acknowledged_at timestamptz
);
-- Estimates (ESTIMATE-1): the formula's hours after one daily test of a network and country,
-- never mixed with the measured rows. Each row is one UTC hour: unique_ips = measured_ips x
-- hour_factor (that hour's formula factor over the measured hour's), requests likewise from the
-- request curve, day_unique_ips = measured_ips x the daily factor over the measured hour's.
-- run_key is the measured test and measured_at its start; a later test replaces the hours an
-- earlier one left unreleased. released_at stays NULL until the hour is released; then the row
-- never changes.
CREATE TABLE IF NOT EXISTS estimated_hours (
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL CHECK(country_code ~ '^[A-Z]{2}$'),
 day date NOT NULL, hour smallint NOT NULL CHECK(hour BETWEEN 0 AND 23),
 run_key bigint NOT NULL REFERENCES test_runs(run_key),
 measured_at timestamptz NOT NULL,
 measured_ips bigint NOT NULL, measured_requests bigint NOT NULL,
 hour_factor double precision NOT NULL, unique_ips double precision NOT NULL,
 requests double precision NOT NULL, day_unique_ips double precision NOT NULL,
 curve text NOT NULL CHECK(curve IN ('country','network')), formula_version integer NOT NULL,
 computed_at timestamptz NOT NULL DEFAULT now(), released_at timestamptz,
 PRIMARY KEY(product_id,country_code,day,hour)
);
CREATE INDEX IF NOT EXISTS estimated_hours_unreleased ON estimated_hours(day,hour)
 WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS estimated_hours_run ON estimated_hours(run_key);
CREATE INDEX IF NOT EXISTS estimated_hours_day ON estimated_hours(day);
-- Whether a country counts for a network (COUNTRY-MIN-IPS-1, the owner's decision of
-- 2026-11-15, schema v16): one verdict per daily test (a test_runs row whose summary has
-- test_kind 'daily'), written by its import in the same transaction. unique_ips is the test's
-- own count (its summary's unique_ips: the distinct exit IPs of its successful answers in the
-- requested country); the country counts when it is 10 or more. Since schema v19 (LOWAVAIL-1,
-- two days in a row) it also counts on its first test under 10 when it was normally big: its
-- previous verdict had 10 or more and its verdicts of the 7 days before reached 50 (counts is
-- set at import from the earlier verdicts: verdicts.RECORD). A verdict holds from the test's
-- start (decided_at) until the next daily test of the same product and country starts; before a
-- country's first daily test it does not count. Nothing measured is deleted: every published
-- figure leaves out what was measured while the country did not count. run_key names the test
-- without a foreign key: the archive purges old runs (archive/purge.py), and a verdict holds
-- until the next daily test, however old its own test is.
CREATE TABLE IF NOT EXISTS country_verdicts (
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL CHECK(country_code ~ '^[A-Z]{2}$'),
 decided_at timestamptz NOT NULL, run_key bigint NOT NULL UNIQUE,
 unique_ips bigint NOT NULL CHECK(unique_ips >= 0),
 counts boolean NOT NULL,
 PRIMARY KEY(product_id,country_code,decided_at)
);
CREATE TABLE IF NOT EXISTS settings (
 key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);

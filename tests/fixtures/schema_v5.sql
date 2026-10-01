-- Schema v5: one canonical address, product membership, shared enrichment, typed test profiles.
CREATE TABLE IF NOT EXISTS schema_versions (
 version integer PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS providers (id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS products (
 product_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider text NOT NULL REFERENCES providers(id), name text NOT NULL, UNIQUE(provider,name)
);
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
 target_id text NOT NULL, target_url text, target_engine text, target_version text,
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
 product_id integer NOT NULL REFERENCES products(product_id), target text NOT NULL,
 scope_hash text NOT NULL, metrics jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now(),
 concurrency integer NOT NULL, timeout_seconds numeric NOT NULL, requests_per_second numeric,
 histogram_steps integer[] NOT NULL DEFAULT '{}', histogram_counts bigint[] NOT NULL DEFAULT '{}',
 UNIQUE(product_id,target,scope_hash)
);
-- summary omits run_id, status and timestamps whenever the columns reproduce them exactly.
CREATE TABLE IF NOT EXISTS test_runs (
 run_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, run_id uuid NOT NULL UNIQUE, scope_id bigint NOT NULL REFERENCES scope_stats(scope_id),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('completed','cancelled','failed')),
 summary_sha256 text NOT NULL, summary jsonb NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS test_runs_scope ON test_runs(scope_id,finished_at);
CREATE TABLE IF NOT EXISTS ip_observations (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 ip_id bigint NOT NULL REFERENCES ip_addresses(ip_id), count bigint NOT NULL CHECK(count > 0),
 first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
 CHECK(first_seen <= last_seen), PRIMARY KEY(run_key,bucket_start,ip_id)
) PARTITION BY RANGE(bucket_start);
CREATE INDEX IF NOT EXISTS ip_observations_ip_time ON ip_observations(ip_id,bucket_start);
-- Latency bins keep artifact order as parallel arrays; each step is the millisecond gap from the
-- previous bin (the first is absolute), which compresses far better than absolute values.
-- extra holds unknown counter keys and a non-canonical bucket_start label verbatim;
-- run_bucket_counters() rebuilds the artifact JSON.
CREATE TABLE IF NOT EXISTS run_buckets (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 completed bigint NOT NULL, successes bigint NOT NULL, response_body_bytes bigint NOT NULL,
 errors jsonb NOT NULL, status_codes jsonb NOT NULL,
 histogram_steps integer[] NOT NULL, histogram_counts integer[] NOT NULL, extra jsonb,
 CHECK(cardinality(histogram_steps) = cardinality(histogram_counts)), PRIMARY KEY(run_key,bucket_start)
) PARTITION BY RANGE(bucket_start);
CREATE TABLE IF NOT EXISTS dbip_releases (
 release_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 text NOT NULL UNIQUE, metadata jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment_locations (
 location_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 country_code text, country text, region_code text, region text, city text,
 latitude double precision, longitude double precision,
 UNIQUE NULLS NOT DISTINCT(country_code,country,region_code,region,city,latitude,longitude)
);
CREATE INDEX IF NOT EXISTS enrichment_locations_city ON enrichment_locations(country_code,city);
CREATE TABLE IF NOT EXISTS enrichment_networks (
 network_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 isp text, organization text, asn bigint, asn_organization text,
 connection_type text, usage_type text,
 UNIQUE NULLS NOT DISTINCT(isp,organization,asn,asn_organization,connection_type,usage_type)
);
CREATE INDEX IF NOT EXISTS enrichment_networks_asn ON enrichment_networks(asn);
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
FROM ip_enrichment e JOIN enrichment_locations l USING(location_id)
JOIN enrichment_networks n USING(network_id);
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

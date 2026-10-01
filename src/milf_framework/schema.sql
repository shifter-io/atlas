-- Schema v4: one canonical address, product membership, shared enrichment.
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
CREATE TABLE IF NOT EXISTS pool_stats (
 pool_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id),
 pool_hash text NOT NULL, definition jsonb NOT NULL,
 unique_ips bigint NOT NULL DEFAULT 0, UNIQUE(product_id,pool_hash)
);
CREATE TABLE IF NOT EXISTS pool_ips (
 pool_id bigint REFERENCES pool_stats(pool_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 PRIMARY KEY(pool_id,ip_id)
);
CREATE TABLE IF NOT EXISTS scope_stats (
 scope_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 pool_id bigint NOT NULL REFERENCES pool_stats(pool_id),
 product_id integer NOT NULL REFERENCES products(product_id), target text NOT NULL,
 scope_hash text NOT NULL, scope jsonb NOT NULL, metrics jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(product_id,target,scope_hash)
);
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
CREATE TABLE IF NOT EXISTS run_buckets (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 counters jsonb NOT NULL, PRIMARY KEY(run_key,bucket_start)
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
-- Reconstruct the public summary without storing repeated buckets/histograms/configuration.
CREATE OR REPLACE FUNCTION stored_run_summary(wanted bigint) RETURNS jsonb
LANGUAGE SQL STABLE AS $$
WITH bins AS (
 SELECT (bin->>0)::bigint AS ms,sum((bin->>1)::bigint)::bigint AS n
 FROM run_buckets b CROSS JOIN LATERAL
 jsonb_array_elements(b.counters->'latency_histogram'->'bins') bin
 WHERE b.run_key=wanted GROUP BY 1
)
SELECT (t.summary - 'config_overrides') || jsonb_build_object(
 'config',(s.scope->'config') || (t.summary->'config_overrides'),
 'target',s.scope->'target',
 'buckets',coalesce((SELECT jsonb_agg(counters ORDER BY bucket_start)
                    FROM run_buckets WHERE run_key=wanted),'[]'::jsonb),
 'latency_histogram',jsonb_build_object('format','ceil_ms_v1','bins',
                    coalesce((SELECT jsonb_agg(jsonb_build_array(ms,n) ORDER BY ms) FROM bins),
                             '[]'::jsonb)))
FROM test_runs t JOIN scope_stats s USING(scope_id) WHERE t.run_key=wanted
$$;
CREATE OR REPLACE VIEW test_run_details AS
SELECT t.run_id,p.provider,p.name AS product,s.target,t.started_at,t.finished_at,t.status,
       stored_run_summary(t.run_key) AS summary,s.scope_id,s.scope_hash,t.imported_at
FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id);

-- Schema v2: one canonical address, product membership, shared enrichment.
CREATE TABLE IF NOT EXISTS schema_versions (
 version integer PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS providers (id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS products (
 product_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 provider text NOT NULL REFERENCES providers(id), name text NOT NULL, UNIQUE(provider,name)
);
CREATE TABLE IF NOT EXISTS ip_addresses (
 ip_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ip inet NOT NULL UNIQUE,
 CHECK(masklen(ip) = CASE family(ip) WHEN 4 THEN 32 ELSE 128 END)
);
CREATE TABLE IF NOT EXISTS product_ips (
 product_id bigint REFERENCES products(product_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
 observation_count bigint NOT NULL CHECK(observation_count > 0),
 run_count bigint NOT NULL CHECK(run_count > 0),
 CHECK(first_seen <= last_seen), PRIMARY KEY(product_id,ip_id)
);
CREATE INDEX IF NOT EXISTS product_ips_reverse ON product_ips(ip_id,product_id);
CREATE TABLE IF NOT EXISTS scope_stats (
 scope_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id bigint NOT NULL REFERENCES products(product_id), target text NOT NULL,
 scope_hash text NOT NULL, scope jsonb NOT NULL, metrics jsonb NOT NULL DEFAULT '{}',
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(product_id,target,scope_hash)
);
CREATE TABLE IF NOT EXISTS scope_ips (
 scope_id bigint REFERENCES scope_stats(scope_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 PRIMARY KEY(scope_id,ip_id)
);
CREATE TABLE IF NOT EXISTS test_runs (
 run_id uuid PRIMARY KEY, scope_id bigint NOT NULL REFERENCES scope_stats(scope_id),
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('completed','cancelled','failed')),
 summary_sha256 text NOT NULL, summary jsonb NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS test_runs_scope ON test_runs(scope_id,finished_at);
CREATE TABLE IF NOT EXISTS ip_observations (
 bucket_start timestamptz NOT NULL, run_id uuid NOT NULL REFERENCES test_runs(run_id),
 ip_id bigint NOT NULL REFERENCES ip_addresses(ip_id), count bigint NOT NULL CHECK(count > 0),
 first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
 CHECK(first_seen <= last_seen), PRIMARY KEY(bucket_start,run_id,ip_id)
) PARTITION BY RANGE(bucket_start);
CREATE INDEX IF NOT EXISTS ip_observations_run ON ip_observations(run_id);
CREATE INDEX IF NOT EXISTS ip_observations_ip_time ON ip_observations(ip_id,bucket_start);
CREATE TABLE IF NOT EXISTS run_buckets (
 bucket_start timestamptz NOT NULL, run_id uuid NOT NULL REFERENCES test_runs(run_id),
 counters jsonb NOT NULL, PRIMARY KEY(bucket_start,run_id)
) PARTITION BY RANGE(bucket_start);
CREATE TABLE IF NOT EXISTS dbip_releases (
 release_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sha256 text NOT NULL UNIQUE, metadata jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ip_enrichment (
 ip_id bigint REFERENCES ip_addresses(ip_id), release_id bigint REFERENCES dbip_releases(release_id),
 country_code text, region_code text, region text, city text, asn bigint, isp text,
 attributes jsonb NOT NULL, PRIMARY KEY(ip_id,release_id)
);
CREATE INDEX IF NOT EXISTS ip_enrichment_location ON ip_enrichment(release_id,country_code,city);
CREATE INDEX IF NOT EXISTS ip_enrichment_isp ON ip_enrichment(release_id,isp);
CREATE INDEX IF NOT EXISTS ip_enrichment_asn ON ip_enrichment(release_id,asn);
CREATE TABLE IF NOT EXISTS run_enrichments (
 run_id uuid REFERENCES test_runs(run_id), release_id bigint REFERENCES dbip_releases(release_id),
 artifact_sha256 text NOT NULL, country_counts jsonb NOT NULL, PRIMARY KEY(run_id,release_id)
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
SELECT a.ip,r.sha256 AS dbip_release,e.country_code,e.region_code,e.region,e.city,e.asn,e.isp,
       e.attributes,e.ip_id,e.release_id
FROM ip_enrichment e JOIN ip_addresses a USING(ip_id) JOIN dbip_releases r USING(release_id);
CREATE OR REPLACE VIEW test_run_details AS
SELECT t.run_id,p.provider,p.name AS product,s.target,t.started_at,t.finished_at,t.status,
       t.summary,s.scope_id,s.scope_hash,t.imported_at
FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id);

-- Transactional v5 → v6 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v5 or after migrate_v4_v5.sql from v4. It only adds tables, columns and foreign keys;
-- no stored JSON changes. Table definitions repeat schema.sql exactly (tests compare catalogs).
-- Census/pulse reference data, seeded by proxy-index db-seed from configs/.
CREATE TABLE runner_regions (
 region_id smallint PRIMARY KEY, slug text NOT NULL UNIQUE,
 cloud text NOT NULL DEFAULT 'digitalocean', pattern text NOT NULL,
 enabled boolean NOT NULL DEFAULT true
);
-- census_time is the country's daily UTC census slot.
CREATE TABLE countries (
 country_code char(2) PRIMARY KEY CHECK(country_code ~ '^[A-Z]{2}$'), name text NOT NULL,
 region_id smallint NOT NULL REFERENCES runner_regions(region_id), census_time time NOT NULL,
 enabled boolean NOT NULL DEFAULT true
);
CREATE INDEX countries_region ON countries(region_id);
-- Every target a stored pool or scope names. Ingest adds unknown targets unscheduled (no label,
-- census and pulse off); db-seed then fills in their schedule.
CREATE TABLE targets (
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
CREATE TABLE nodes (
 node_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 provider text NOT NULL REFERENCES providers(id), product text NOT NULL, host text,
 token_hash text NOT NULL UNIQUE, status text NOT NULL DEFAULT 'offline',
 last_heartbeat timestamptz, version text, open_conns integer, req_per_s double precision,
 rss_mb integer, UNIQUE(region_id,provider,product)
);
-- A batch is one synchronized slot: every provider in the region starts it at start_at.
CREATE TABLE test_batches (
 batch_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 kind text NOT NULL CHECK(kind IN ('census','pulse')),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 target_id text NOT NULL REFERENCES targets(target_id),
 scheduled_for timestamptz NOT NULL, start_at timestamptz NOT NULL,
 started_at timestamptz, finished_at timestamptz,
 status text NOT NULL DEFAULT 'scheduled'
  CHECK(status IN ('scheduled','running','done','failed','skipped','killed')),
 UNIQUE(kind,country_code,target_id,scheduled_for)
);
CREATE INDEX test_batches_open ON test_batches(status)
 WHERE status IN ('scheduled','running');
CREATE INDEX test_batches_target ON test_batches(kind,target_id,scheduled_for);
-- Every target already stored gets an unscheduled row, so the new foreign keys hold.
INSERT INTO targets(target_id,url,engine,census,pulse_enabled)
SELECT DISTINCT ON (target_id) target_id,coalesce(url,''),coalesce(engine,'http'),false,false
FROM (SELECT target_id,target_url AS url,target_engine AS engine FROM pool_stats
      UNION ALL SELECT target,NULL,NULL FROM scope_stats) t
ORDER BY target_id,url NULLS LAST,engine NULLS LAST;
ALTER TABLE pool_stats ADD FOREIGN KEY(target_id) REFERENCES targets(target_id);
ALTER TABLE scope_stats ADD FOREIGN KEY(target) REFERENCES targets(target_id);
ALTER TABLE test_runs ADD COLUMN batch_id bigint REFERENCES test_batches(batch_id);
CREATE INDEX test_runs_batch ON test_runs(batch_id) WHERE batch_id IS NOT NULL;
-- Live counters of each provider's run in a batch; run_key links the ingested artifact.
CREATE TABLE batch_runs (
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
 run_key bigint UNIQUE REFERENCES test_runs(run_key), PRIMARY KEY(batch_id,product_id)
);
CREATE INDEX batch_runs_node ON batch_runs(node_id,status);
-- Constant defaults: existing partitions are not rewritten. outcome_counters stays false for
-- every v5 bucket, so run_bucket_counters() keeps rebuilding the v5 JSON.
ALTER TABLE run_buckets ADD COLUMN challenges bigint NOT NULL DEFAULT 0,
 ADD COLUMN blocks bigint NOT NULL DEFAULT 0, ADD COLUMN timeouts bigint NOT NULL DEFAULT 0,
 ADD COLUMN proxy_errors bigint NOT NULL DEFAULT 0, ADD COLUMN bytes_up bigint NOT NULL DEFAULT 0,
 ADD COLUMN bytes_down bigint NOT NULL DEFAULT 0,
 ADD COLUMN outcome_counters boolean NOT NULL DEFAULT false;
-- Outcome and latency per exit location x network x 5-minute bucket. Requests without an exit
-- address use the all-NULL location and network rows.
CREATE TABLE run_geo_buckets (
 bucket_start timestamptz NOT NULL, run_key bigint NOT NULL REFERENCES test_runs(run_key),
 location_id integer NOT NULL REFERENCES enrichment_locations(location_id),
 network_id integer NOT NULL REFERENCES enrichment_networks(network_id),
 completed bigint NOT NULL, successes bigint NOT NULL, challenges bigint NOT NULL,
 blocks bigint NOT NULL, timeouts bigint NOT NULL, bytes_up bigint NOT NULL,
 bytes_down bigint NOT NULL, histogram_steps integer[] NOT NULL, histogram_counts integer[] NOT NULL,
 CHECK(cardinality(histogram_steps) = cardinality(histogram_counts)),
 PRIMARY KEY(run_key,bucket_start,location_id,network_id)
) PARTITION BY RANGE(bucket_start);
-- Live tape: census and pulse milestones, incidents and audit findings.
CREATE TABLE test_events (
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
CREATE INDEX test_events_at ON test_events(at DESC);
CREATE INDEX test_events_product ON test_events(product_id,at DESC);
-- Time a product was down in a country: opened by a census that stalled (source 'census') or a
-- pulse 'down' event (source 'pulse'); ended_at stays NULL while it is still down.
CREATE TABLE outages (
 outage_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 region_id smallint REFERENCES runner_regions(region_id),
 source text NOT NULL CHECK(source IN ('census','pulse')),
 batch_id bigint REFERENCES test_batches(batch_id),
 started_at timestamptz NOT NULL, ended_at timestamptz, CHECK(ended_at >= started_at)
);
CREATE INDEX outages_product ON outages(product_id,started_at);
CREATE INDEX outages_open ON outages(product_id,country_code) WHERE ended_at IS NULL;
CREATE TABLE job_runs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','ok','failed','skipped')),
 error text, duration_ms integer
);
CREATE INDEX job_runs_job ON job_runs(job,started_at DESC);
CREATE TABLE alerts (
 alert_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL,
 severity text NOT NULL DEFAULT 'minor' CHECK(severity IN ('info','minor','major','critical')),
 message text NOT NULL, data jsonb NOT NULL DEFAULT '{}', dedupe_key text UNIQUE,
 occurrences integer NOT NULL DEFAULT 1, first_at timestamptz NOT NULL DEFAULT now(),
 last_at timestamptz NOT NULL DEFAULT now(), acknowledged_at timestamptz
);
CREATE TABLE settings (
 key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);

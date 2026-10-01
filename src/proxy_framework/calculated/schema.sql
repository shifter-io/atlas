CREATE TABLE IF NOT EXISTS calculation_schema (version integer PRIMARY KEY, checksum text NOT NULL);
CREATE TABLE IF NOT EXISTS methodologies (
 methodology_id text PRIMARY KEY, definition jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS calculation_roles (
 run_key bigint PRIMARY KEY REFERENCES test_runs(run_key),
 role text NOT NULL CHECK(role IN ('exploratory','pool_sampling','benchmark','monitor'))
);
CREATE TABLE IF NOT EXISTS metric_evidence (
 evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id), cohort text NOT NULL,
 metric text NOT NULL, value double precision NOT NULL,
 period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
 valid_until timestamptz NOT NULL, source text NOT NULL, content_hash text NOT NULL UNIQUE,
 CHECK(period_end>period_start AND valid_until>=period_end)
);
CREATE INDEX IF NOT EXISTS metric_evidence_lookup ON metric_evidence(product_id,cohort,metric,period_end);
CREATE TABLE IF NOT EXISTS calculation_builds (
 build_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 methodology_id text NOT NULL REFERENCES methodologies(methodology_id),
 as_of timestamptz NOT NULL, input_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(methodology_id,as_of,input_hash)
);
CREATE TABLE IF NOT EXISTS calculated_windows (
 result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 build_id bigint NOT NULL REFERENCES calculation_builds(build_id),
 product_id integer NOT NULL REFERENCES products(product_id),
 cohort text NOT NULL, target text NOT NULL, window_hours integer NOT NULL,
 completed bigint NOT NULL, successes bigint NOT NULL, errors bigint NOT NULL,
 body_bytes bigint NOT NULL, success_rate double precision, timeout_rate double precision,
 p50 integer, p90 integer, p95 integer, p99 integer,
 unique_ips bigint, new_ips bigint, recurring_share double precision,
 countries integer, regions integer, cities integer, asns integer,
 coverage double precision NOT NULL, last_observed timestamptz,
 metadata jsonb NOT NULL,
 UNIQUE(build_id,product_id,cohort,target,window_hours)
);
CREATE TABLE IF NOT EXISTS calculated_breakdowns (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id),
 dimension text NOT NULL, dimension_key text NOT NULL,
 ips bigint NOT NULL, observations bigint NOT NULL, label jsonb NOT NULL,
 PRIMARY KEY(result_id,dimension,dimension_key)
);
CREATE TABLE IF NOT EXISTS calculated_series (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id),
 bucket_start timestamptz NOT NULL,
 completed bigint NOT NULL, successes bigint NOT NULL,
 p50 integer, p90 integer, p99 integer, unique_ips bigint,
 PRIMARY KEY(result_id,bucket_start)
);
CREATE TABLE IF NOT EXISTS calculated_scores (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id), profile text NOT NULL,
 score double precision, grade text, rank integer, previous_rank integer,
 state text NOT NULL, scored_at timestamptz, valid_until timestamptz,
 components jsonb NOT NULL, reasons jsonb NOT NULL,
 PRIMARY KEY(result_id,profile)
);
CREATE TABLE IF NOT EXISTS calculated_overlaps (
 build_id bigint REFERENCES calculation_builds(build_id), cohort text NOT NULL,
 window_hours integer NOT NULL, product_a integer REFERENCES products(product_id),
 product_b integer REFERENCES products(product_id), shared_ips bigint NOT NULL,
 ips_a bigint NOT NULL, ips_b bigint NOT NULL,
 PRIMARY KEY(build_id,cohort,window_hours,product_a,product_b), CHECK(product_a<product_b)
);
CREATE TABLE IF NOT EXISTS calculated_snapshots (
 build_id bigint PRIMARY KEY REFERENCES calculation_builds(build_id),
 content_hash text NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS current_calculation (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 build_id bigint NOT NULL REFERENCES calculated_snapshots(build_id)
);

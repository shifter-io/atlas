CREATE TABLE IF NOT EXISTS calculation_schema (version integer PRIMARY KEY, checksum text NOT NULL);
CREATE TABLE IF NOT EXISTS methodologies (
 methodology_id text PRIMARY KEY, definition jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS calculation_roles (
 run_key bigint PRIMARY KEY REFERENCES test_runs(run_key),
 role text NOT NULL CHECK(role IN ('exploratory','pool_sampling','benchmark','monitor'))
);
-- Evidence may arrive before any calculation of its cohort, so cohort is not a foreign key.
CREATE TABLE IF NOT EXISTS metric_evidence (
 evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id), cohort text NOT NULL,
 metric text NOT NULL, value double precision NOT NULL,
 period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
 valid_until timestamptz NOT NULL, source text NOT NULL, content_hash text NOT NULL UNIQUE,
 CHECK(period_end>period_start AND valid_until>=period_end)
);
CREATE INDEX IF NOT EXISTS metric_evidence_lookup ON metric_evidence(product_id,cohort,metric,period_end);
-- Comparable test conditions keyed by the 20-character criteria digest. Unknown or
-- non-conforming criteria stay verbatim in extra; cohort_criteria rebuilds the hashed object.
CREATE TABLE IF NOT EXISTS cohorts (
 cohort text PRIMARY KEY, kind text, country text, runner_location text, concurrency integer,
 timeout_seconds numeric, rate numeric, proxy_protocol text, session_policy text,
 ttl_seconds integer, parameters jsonb, flags jsonb, target_variant jsonb, extra jsonb
);
-- Payloads of unpublished builds are pruned (store.prune). After each publication store.expire
-- deletes every build except the current one, the newest published build per UTC day of the last
-- 30 days and the drafts whose payload prune kept.
CREATE TABLE IF NOT EXISTS calculation_builds (
 build_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 methodology_id text NOT NULL REFERENCES methodologies(methodology_id),
 as_of timestamptz NOT NULL, input_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), published boolean NOT NULL DEFAULT false,
 UNIQUE(methodology_id,as_of,input_hash)
);
-- Contract K2 (calculated v9, lists.py): a target-free window's full breakdown lists, written
-- once and shared by the builds whose windows reference it (calculated_windows.list_id). entries
-- holds each breakdown's row count, figures the window's since-independent pool figures and its
-- pool lists' entry counts; signature is NULL until CALC-LISTS-1b (NULLs never conflict).
-- created_build has no foreign key: a list outlives the build that wrote it. store.collect()
-- deletes the lists no window references, after store.discard() and store.expire().
CREATE TABLE IF NOT EXISTS calculated_lists (
 list_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id), cohort text NOT NULL,
 window_hours integer NOT NULL, signature text, content_hash text NOT NULL,
 entries jsonb NOT NULL, figures jsonb NOT NULL, created_build bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_id,cohort,window_hours,signature)
);
CREATE TABLE IF NOT EXISTS calculated_windows (
 result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 build_id bigint NOT NULL REFERENCES calculation_builds(build_id),
 product_id integer NOT NULL REFERENCES products(product_id),
 cohort text NOT NULL REFERENCES cohorts(cohort), target text NOT NULL, window_hours integer NOT NULL,
 completed bigint NOT NULL, successes bigint NOT NULL, errors bigint NOT NULL,
 body_bytes bigint NOT NULL, success_rate double precision, timeout_rate double precision,
 p50 integer, p90 integer, p95 integer, p99 integer,
 unique_ips bigint, new_ips bigint, recurring_share double precision,
 countries integer, regions integer, cities integer, asns integer,
 coverage double precision NOT NULL, last_observed timestamptz,
 readiness text NOT NULL CHECK(readiness IN ('ready','partial','unavailable')),
 challenge_rate double precision, block_rate double precision, uptime double precision,
 bytes_up bigint, bytes_down bigint,
 UNIQUE(build_id,product_id,cohort,target,window_hours),
 -- Kept from calculated v5 to v8, whose per-build breakdown partitions referenced it.
 UNIQUE(result_id,build_id),
 -- The window's lists (target '' only; NULL on a target's window).
 list_id bigint REFERENCES calculated_lists(list_id)
);
CREATE INDEX IF NOT EXISTS calculated_windows_list ON calculated_windows(list_id);
-- Coordinate-free breakdown labels, shared by every build: places are the main schema's
-- place_names (shared with enrichment locations), and an ASN label is an asn_names entry with a
-- usage type.
CREATE TABLE IF NOT EXISTS asn_labels (
 asn_label_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn_name_id integer NOT NULL REFERENCES asn_names(asn_name_id), usage_type text,
 UNIQUE NULLS NOT DISTINCT(asn_name_id,usage_type)
);
-- dimension: g city (geography), r state/region and c country reference place_names; a asn
-- references asn_labels; x country x ASN and y city x ASN reference both. position is the payload
-- order within the dimension. Request counters and percentiles come from run_geo_buckets; they
-- are NULL on rows calculated before calculated v3, which held IP counts only. Unpartitioned:
-- lists are shared by builds, and store.collect() deletes the rows of unreferenced lists, so
-- autovacuum runs at 1% of the table.
CREATE TABLE IF NOT EXISTS calculated_list_rows (
 list_id bigint NOT NULL REFERENCES calculated_lists(list_id), dimension "char" NOT NULL
  CHECK(dimension IN ('g','c','a','r','x','y')), position integer NOT NULL,
 place_id integer REFERENCES place_names(place_id),
 asn_label_id integer REFERENCES asn_labels(asn_label_id),
 ips bigint NOT NULL, observations bigint NOT NULL,
 completed bigint, successes bigint, challenges bigint, blocks bigint,
 p50 integer, p90 integer, p99 integer,
 CHECK((place_id IS NULL) = (dimension = 'a')
  AND (asn_label_id IS NULL) = (dimension IN ('g','c','r'))),
 PRIMARY KEY(list_id,dimension,position)
) WITH (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_insert_scale_factor = 0.01);
-- Memos of the incremental calculation (contract K6, incremental.py, hourly.py, fleetsql.py),
-- calculated v10: a run's figures per range of its buckets (release_id 0: unenriched), figures
-- of a set of runs in a time slot and its distinct IPs (keyed by the digest of their inputs),
-- each 5-minute bucket's distinct IPs over every run, and each exit IP's newest sighting. None
-- references a raw table, which ARCHIVE-1 empties; old entries are collected by age (created_at,
-- last_used, last_seen). CALC-INC-2 and CALC-FLEET-1 fill them.
CREATE TABLE IF NOT EXISTS calculated_run_ranges (
 run_key bigint NOT NULL, release_id integer NOT NULL, first_bucket timestamptz NOT NULL,
 last_bucket timestamptz NOT NULL, figures jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_key,release_id,first_bucket,last_bucket)
);
CREATE TABLE IF NOT EXISTS calculated_slot_figures (
 digest text PRIMARY KEY, figures jsonb NOT NULL, last_used timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS calculated_distinct (
 digest text PRIMARY KEY, counts jsonb NOT NULL, last_used timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS calculated_bucket_ips (
 bucket_start timestamptz PRIMARY KEY, runs_digest text NOT NULL, unique_ips bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS calculated_ip_last_seen (
 ip_id bigint PRIMARY KEY, last_seen timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS calculated_ip_last_seen_time ON calculated_ip_last_seen(last_seen);
CREATE TABLE IF NOT EXISTS calculated_series (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id),
 bucket_start timestamptz NOT NULL,
 completed bigint NOT NULL, successes bigint NOT NULL,
 p50 integer, p90 integer, p99 integer, unique_ips bigint,
 uptime double precision CHECK(uptime >= 0 AND uptime <= 1),
 PRIMARY KEY(result_id,bucket_start)
);
CREATE TABLE IF NOT EXISTS calculated_scores (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id), profile text NOT NULL,
 score double precision, grade text, rank integer, previous_rank integer,
 state text NOT NULL, scored_at timestamptz, valid_until timestamptz,
 PRIMARY KEY(result_id,profile)
);
CREATE TABLE IF NOT EXISTS score_reasons (
 reason_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, reason text NOT NULL UNIQUE
);
-- numeric keeps the JSON form of values and weights (100 versus 100.0) exact. weight is NULL
-- only for scores calculated before payloads recorded weights.
CREATE TABLE IF NOT EXISTS calculated_score_components (
 result_id bigint NOT NULL, profile text NOT NULL, component text NOT NULL,
 position smallint NOT NULL, state text NOT NULL CHECK(state IN ('ready','incomplete','unavailable')),
 weight numeric, value numeric, inputs jsonb,
 reason_id integer REFERENCES score_reasons(reason_id),
 PRIMARY KEY(result_id,profile,component),
 FOREIGN KEY(result_id,profile) REFERENCES calculated_scores(result_id,profile)
);
-- Reasons that components cannot derive. A product score lists, at position 0, every weighted
-- component that is not ready; negative positions precede that block, positive ones follow it.
CREATE TABLE IF NOT EXISTS calculated_score_reasons (
 result_id bigint NOT NULL, profile text NOT NULL, position smallint NOT NULL,
 reason_id integer NOT NULL REFERENCES score_reasons(reason_id),
 PRIMARY KEY(result_id,profile,position),
 FOREIGN KEY(result_id,profile) REFERENCES calculated_scores(result_id,profile)
);
CREATE TABLE IF NOT EXISTS calculated_overlaps (
 build_id bigint REFERENCES calculation_builds(build_id), cohort text NOT NULL REFERENCES cohorts(cohort),
 window_hours integer NOT NULL, product_a integer REFERENCES products(product_id),
 product_b integer REFERENCES products(product_id), shared_ips bigint NOT NULL,
 ips_a bigint NOT NULL, ips_b bigint NOT NULL,
 PRIMARY KEY(build_id,cohort,window_hours,product_a,product_b), CHECK(product_a<product_b)
);
CREATE TABLE IF NOT EXISTS calculated_snapshots (
 build_id bigint PRIMARY KEY REFERENCES calculation_builds(build_id),
 content_hash text NOT NULL, payload jsonb NOT NULL
);
-- Contract K1 (calculated v8, parts.py): payload holds the head of a build stored since v8 and
-- the whole payload of an older one; the head's product groups and runs are these rows, one
-- group per 'product' row, at most parts.RUNS_PER_PART runs per 'runs' row, in payload order.
-- One LIST partition per build (calculated_snapshot_parts_b<build_id>, created by
-- store.partition()), dropped with its snapshot by store.prune() and store.discard().
CREATE TABLE IF NOT EXISTS calculated_snapshot_parts (
 build_id bigint NOT NULL REFERENCES calculated_snapshots(build_id) DEFERRABLE INITIALLY DEFERRED,
 kind text NOT NULL CHECK(kind IN ('product','runs')), position integer NOT NULL CHECK(position>=0),
 part_id text NOT NULL, product_id integer, cohort text, content_hash text NOT NULL,
 part jsonb NOT NULL, CHECK((kind='product')=(product_id IS NOT NULL AND cohort IS NOT NULL)),
 PRIMARY KEY(build_id,kind,position)
) PARTITION BY LIST(build_id);
CREATE TABLE IF NOT EXISTS current_calculation (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 build_id bigint NOT NULL REFERENCES calculated_snapshots(build_id)
);
-- Readable reconstructions of the payload fragments the typed rows replace.
CREATE OR REPLACE VIEW cohort_criteria AS
SELECT cohort,jsonb_build_object('kind',kind,'country',country,'runner_location',runner_location,
 'concurrency',concurrency,'timeout_seconds',timeout_seconds,'rate',rate,
 'proxy_protocol',proxy_protocol,'session_policy',session_policy,'parameters',parameters,
 'flags',flags,'ttl_seconds',ttl_seconds,'target_variant',target_variant)
 || coalesce(extra,'{}') AS criteria
FROM cohorts;
-- ASN labels with their names, as asn_labels held them up to calculated v6.
CREATE OR REPLACE VIEW asn_label_names AS
SELECT l.asn_label_id,n.asn,n.name,l.usage_type FROM asn_labels l
JOIN asn_names n USING(asn_name_id);
-- A list's rows with their labels, by list_id; calculated_breakdown_details shows them per window.
CREATE OR REPLACE VIEW calculated_list_details AS
SELECT b.list_id,CASE b.dimension WHEN 'g' THEN 'geography' WHEN 'c' THEN 'country'
 WHEN 'r' THEN 'region' WHEN 'x' THEN 'country_asn' WHEN 'y' THEN 'city_asn' ELSE 'asn' END
 AS dimension,b.position,b.ips,b.observations,CASE b.dimension
 WHEN 'g' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code,'city',p.city)
 WHEN 'c' THEN jsonb_build_object('country_code',p.country_code,'country',p.country)
 WHEN 'r' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code)
 WHEN 'x' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
 WHEN 'y' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code,'city',p.city,
  'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
 ELSE jsonb_build_object('asn',a.asn,'name',a.name,'usage_type',a.usage_type) END AS label,
 b.completed,b.successes,b.successes::double precision/nullif(b.completed,0) AS success_rate,
 b.challenges,b.blocks,b.p50,b.p90,b.p99
FROM calculated_list_rows b LEFT JOIN place_names p USING(place_id)
LEFT JOIN asn_label_names a USING(asn_label_id);
CREATE OR REPLACE VIEW calculated_breakdown_details AS
SELECT w.result_id,d.dimension,d.position,d.ips,d.observations,d.label,d.completed,d.successes,
 d.success_rate,d.challenges,d.blocks,d.p50,d.p90,d.p99
FROM calculated_windows w JOIN calculated_list_details d USING(list_id);
CREATE OR REPLACE VIEW calculated_score_details AS
SELECT s.*,
 (SELECT jsonb_object_agg(c.component,jsonb_build_object('state',c.state,'value',c.value,
   'inputs',c.inputs,'reason',r.reason)) FROM calculated_score_components c
  LEFT JOIN score_reasons r USING(reason_id) WHERE (c.result_id,c.profile)=(s.result_id,s.profile))
 AS components,
 (SELECT CASE WHEN bool_and(c.weight IS NOT NULL) THEN jsonb_object_agg(c.component,c.weight) END
  FROM calculated_score_components c WHERE (c.result_id,c.profile)=(s.result_id,s.profile))
 AS weights,
 (SELECT coalesce(jsonb_agg(u.reason ORDER BY u.position,u.component_position),'[]') FROM (
   SELECT x.position,0 AS component_position,r.reason FROM calculated_score_reasons x
   JOIN score_reasons r USING(reason_id) WHERE (x.result_id,x.profile)=(s.result_id,s.profile)
   UNION ALL
   SELECT 0,c.position,coalesce(r.reason,c.component || ' not ready')
   FROM calculated_score_components c JOIN calculated_windows w USING(result_id)
   LEFT JOIN score_reasons r USING(reason_id) WHERE (c.result_id,c.profile)=(s.result_id,s.profile)
   AND w.target='' AND c.weight>0 AND c.state<>'ready') u) AS reasons
FROM calculated_scores s;
-- Light browse views: none of them reads a payload (payload_bytes is its stored, compressed size,
-- head and parts, read from the TOAST pointers without fetching the values).
CREATE OR REPLACE VIEW calculated_snapshots_list AS
SELECT b.build_id,b.as_of,b.created_at,b.methodology_id,b.published,
 b.build_id IN (SELECT build_id FROM current_calculation) AS is_current,
 s.content_hash,pg_column_size(s.payload)+coalesce((SELECT sum(pg_column_size(p.part))
  FROM calculated_snapshot_parts p WHERE p.build_id=b.build_id),0) AS payload_bytes
FROM calculation_builds b LEFT JOIN calculated_snapshots s USING(build_id);
CREATE OR REPLACE VIEW current_windows AS
SELECT p.provider,p.name AS product,w.*
FROM calculated_windows w JOIN products p USING(product_id)
WHERE w.build_id=(SELECT build_id FROM current_calculation);
CREATE OR REPLACE VIEW current_breakdowns AS
SELECT w.build_id,w.result_id,pr.provider,pr.name AS product,w.cohort,w.window_hours,
 CASE b.dimension WHEN 'g' THEN 'geography' WHEN 'c' THEN 'country' WHEN 'r' THEN 'region'
 WHEN 'x' THEN 'country_asn' WHEN 'y' THEN 'city_asn' ELSE 'asn' END AS dimension,b.position,
 p.country_code,p.country,p.region_code,p.region,p.city,a.asn,a.name AS asn_name,a.usage_type,
 b.ips,b.observations,b.completed,b.successes,
 b.successes::double precision/nullif(b.completed,0) AS success_rate,
 b.challenges,b.blocks,b.p50,b.p90,b.p99
FROM calculated_windows w JOIN calculated_list_rows b USING(list_id)
JOIN products pr USING(product_id)
LEFT JOIN place_names p USING(place_id) LEFT JOIN asn_label_names a USING(asn_label_id)
WHERE w.build_id=(SELECT build_id FROM current_calculation);

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
-- Payloads of unpublished builds are pruned (store.prune); published ones are kept.
CREATE TABLE IF NOT EXISTS calculation_builds (
 build_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 methodology_id text NOT NULL REFERENCES methodologies(methodology_id),
 as_of timestamptz NOT NULL, input_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), published boolean NOT NULL DEFAULT false,
 UNIQUE(methodology_id,as_of,input_hash)
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
 UNIQUE(build_id,product_id,cohort,target,window_hours)
);
-- Coordinate-free breakdown labels, shared by every build.
CREATE TABLE IF NOT EXISTS places (
 place_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 country_code text NOT NULL, country text, region_code text, region text, city text,
 UNIQUE NULLS NOT DISTINCT(country_code,country,region_code,region,city)
);
CREATE TABLE IF NOT EXISTS asn_labels (
 asn_label_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn bigint NOT NULL, name text, usage_type text, UNIQUE NULLS NOT DISTINCT(asn,name,usage_type)
);
-- dimension: g geography and c country reference places; a asn references asn_labels.
-- position is the payload order within the dimension.
CREATE TABLE IF NOT EXISTS calculated_breakdowns (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id),
 ips bigint NOT NULL, observations bigint NOT NULL,
 place_id integer REFERENCES places(place_id), asn_label_id integer REFERENCES asn_labels(asn_label_id),
 position integer NOT NULL, dimension "char" NOT NULL CHECK(dimension IN ('g','c','a')),
 CHECK((place_id IS NULL) = (dimension = 'a') AND (asn_label_id IS NULL) = (dimension <> 'a')),
 PRIMARY KEY(result_id,dimension,position)
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
CREATE OR REPLACE VIEW calculated_breakdown_details AS
SELECT b.result_id,CASE b.dimension WHEN 'g' THEN 'geography' WHEN 'c' THEN 'country' ELSE 'asn' END
 AS dimension,b.position,b.ips,b.observations,CASE b.dimension
 WHEN 'g' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code,'city',p.city)
 WHEN 'c' THEN jsonb_build_object('country_code',p.country_code,'country',p.country)
 ELSE jsonb_build_object('asn',a.asn,'name',a.name,'usage_type',a.usage_type) END AS label
FROM calculated_breakdowns b LEFT JOIN places p USING(place_id) LEFT JOIN asn_labels a USING(asn_label_id);
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

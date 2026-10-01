-- Transactional calculated v1 → v2 migration, applied by storage.migrate() after the base step.
-- Every v1 build has a snapshot; its payload supplies breakdown order and score weights.
-- store.MIGRATION_CHECKS compares the rebuilt JSON with these temporary v1 copies.
CREATE TEMP TABLE v1_windows ON COMMIT DROP AS SELECT result_id,cohort,metadata FROM calculated_windows;
CREATE TEMP TABLE v1_scores ON COMMIT DROP AS
SELECT result_id,profile,components,reasons FROM calculated_scores;
CREATE TEMP TABLE v1_breakdowns ON COMMIT DROP AS
SELECT result_id,dimension,ips,observations,label FROM calculated_breakdowns;
CREATE TEMP TABLE v1_payload_windows ON COMMIT DROP AS
SELECT w.result_id,w.target,win.body
FROM calculated_snapshots s CROSS JOIN LATERAL jsonb_array_elements(s.payload->'products') p
CROSS JOIN LATERAL jsonb_each(p->'windows') win(name,body)
JOIN calculated_windows w ON w.build_id=s.build_id AND w.product_id=(p->>'product_id')::integer
 AND w.cohort=p->>'cohort' AND w.window_hours=(win.body->'period'->>'hours')::integer;
CREATE TEMP TABLE v1_positions ON COMMIT DROP AS
SELECT v.result_id,d.dimension,e.i-1 AS position,e.v - 'ips' - 'observations' AS label
FROM v1_payload_windows v
CROSS JOIN LATERAL (VALUES ('geography','geo'),('country','countries_breakdown'),
 ('asn','asns_breakdown')) d(dimension,key)
CROSS JOIN LATERAL jsonb_array_elements(v.body->'pool'->d.key) WITH ORDINALITY e(v,i)
WHERE v.target='';
CREATE TEMP TABLE v1_weights ON COMMIT DROP AS
SELECT v.result_id,x.profile,x.score->'weights' AS weights FROM v1_payload_windows v
CROSS JOIN LATERAL (SELECT key AS profile,value AS score FROM jsonb_each(v.body->'scores')
 WHERE v.target='' UNION ALL SELECT 'balanced',v.body->'targets'->v.target->'score'
 WHERE v.target<>'') x;
CREATE OR REPLACE FUNCTION pg_temp.fits(v jsonb, kind text) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
SELECT CASE WHEN v IS NULL THEN false WHEN kind='json' OR jsonb_typeof(v)='null' THEN true
 WHEN kind='text' THEN jsonb_typeof(v)='string'
 WHEN kind='number' THEN jsonb_typeof(v)='number'
 WHEN kind='integer' AND jsonb_typeof(v)='number' AND v::text ~ '^-?[0-9]+$'
 THEN v::text::numeric BETWEEN -2147483648 AND 2147483647 ELSE false END
$$;
-- Cohorts: typed criteria, non-conforming or unknown keys verbatim in extra.
CREATE TABLE cohorts (
 cohort text PRIMARY KEY, kind text, country text, runner_location text, concurrency integer,
 timeout_seconds numeric, rate numeric, proxy_protocol text, session_policy text,
 ttl_seconds integer, parameters jsonb, flags jsonb, target_variant jsonb, extra jsonb
);
CREATE TEMP TABLE v1_criteria ON COMMIT DROP AS
SELECT DISTINCT ON (cohort) cohort,metadata->'criteria' AS c,ARRAY(SELECT k FROM (VALUES
 ('kind','text'),('country','text'),('runner_location','text'),('concurrency','integer'),
 ('timeout_seconds','number'),('rate','number'),('proxy_protocol','text'),('session_policy','text'),
 ('ttl_seconds','integer'),('parameters','json'),('flags','json'),('target_variant','json')) f(k,kind)
 WHERE pg_temp.fits(metadata->'criteria'->k,kind)) AS taken
FROM calculated_windows ORDER BY cohort,result_id;
INSERT INTO cohorts SELECT cohort,
 CASE WHEN 'kind'=ANY(taken) THEN c->>'kind' END,
 CASE WHEN 'country'=ANY(taken) THEN c->>'country' END,
 CASE WHEN 'runner_location'=ANY(taken) THEN c->>'runner_location' END,
 CASE WHEN 'concurrency'=ANY(taken) THEN (c->>'concurrency')::integer END,
 CASE WHEN 'timeout_seconds'=ANY(taken) THEN (c->>'timeout_seconds')::numeric END,
 CASE WHEN 'rate'=ANY(taken) THEN (c->>'rate')::numeric END,
 CASE WHEN 'proxy_protocol'=ANY(taken) THEN c->>'proxy_protocol' END,
 CASE WHEN 'session_policy'=ANY(taken) THEN c->>'session_policy' END,
 CASE WHEN 'ttl_seconds'=ANY(taken) THEN (c->>'ttl_seconds')::integer END,
 nullif(c->'parameters','null'),nullif(c->'flags','null'),nullif(c->'target_variant','null'),
 nullif(c - taken,'{}')
FROM v1_criteria;
ALTER TABLE calculated_windows ADD COLUMN readiness text;
UPDATE calculated_windows SET readiness=metadata->>'readiness';
ALTER TABLE calculated_windows ALTER COLUMN readiness SET NOT NULL,
 ADD CHECK(readiness IN ('ready','partial','unavailable')),
 ADD FOREIGN KEY(cohort) REFERENCES cohorts(cohort), DROP COLUMN metadata;
ALTER TABLE calculated_overlaps ADD FOREIGN KEY(cohort) REFERENCES cohorts(cohort);
-- v1 never recorded publication; every existing payload is kept as if published.
ALTER TABLE calculation_builds ADD COLUMN published boolean NOT NULL DEFAULT false;
UPDATE calculation_builds SET published=true WHERE build_id IN (SELECT build_id FROM calculated_snapshots);
-- Breakdowns: compact references to shared label tables, ordered as in the payload.
CREATE TABLE places (
 place_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 country_code text NOT NULL, country text, region_code text, region text, city text,
 UNIQUE NULLS NOT DISTINCT(country_code,country,region_code,region,city)
);
CREATE TABLE asn_labels (
 asn_label_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 asn bigint NOT NULL, name text, usage_type text, UNIQUE NULLS NOT DISTINCT(asn,name,usage_type)
);
INSERT INTO places(country_code,country,region_code,region,city)
SELECT DISTINCT label->>'country_code',label->>'country',label->>'region_code',label->>'region',
 label->>'city' FROM v1_breakdowns WHERE dimension<>'asn' ORDER BY 1,2,3,4,5;
INSERT INTO asn_labels(asn,name,usage_type)
SELECT DISTINCT (label->>'asn')::bigint,label->>'name',label->>'usage_type'
FROM v1_breakdowns WHERE dimension='asn' ORDER BY 1,2,3;
DROP TABLE calculated_breakdowns;
CREATE TABLE calculated_breakdowns (
 result_id bigint NOT NULL REFERENCES calculated_windows(result_id),
 ips bigint NOT NULL, observations bigint NOT NULL,
 place_id integer REFERENCES places(place_id), asn_label_id integer REFERENCES asn_labels(asn_label_id),
 position integer NOT NULL, dimension "char" NOT NULL CHECK(dimension IN ('g','c','a')),
 CHECK((place_id IS NULL) = (dimension = 'a') AND (asn_label_id IS NULL) = (dimension <> 'a')),
 PRIMARY KEY(result_id,dimension,position)
);
INSERT INTO calculated_breakdowns(result_id,ips,observations,place_id,asn_label_id,position,dimension)
SELECT b.result_id,b.ips,b.observations,p.place_id,a.asn_label_id,
 row_number() OVER (PARTITION BY b.result_id,b.dimension
  ORDER BY o.position NULLS LAST,b.ips DESC,b.label::text)-1,left(b.dimension,1)::"char"
FROM v1_breakdowns b
LEFT JOIN v1_positions o ON (o.result_id,o.dimension)=(b.result_id,b.dimension) AND o.label=b.label
-- NULL-safe, hashable tuple comparisons (as in storage._enrich).
LEFT JOIN places p ON b.dimension<>'asn'
 AND jsonb_build_array(p.country_code,p.country,p.region_code,p.region,p.city)
 = jsonb_build_array(b.label->>'country_code',b.label->>'country',b.label->>'region_code',
 b.label->>'region',b.label->>'city')
LEFT JOIN asn_labels a ON b.dimension='asn' AND jsonb_build_array(a.asn,a.name,a.usage_type)
 = jsonb_build_array((b.label->>'asn')::bigint,b.label->>'name',b.label->>'usage_type');
-- Scores: component rows, a reason lookup and the reasons components cannot derive.
CREATE TABLE score_reasons (
 reason_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, reason text NOT NULL UNIQUE
);
CREATE TABLE calculated_score_components (
 result_id bigint NOT NULL, profile text NOT NULL, component text NOT NULL,
 position smallint NOT NULL, state text NOT NULL CHECK(state IN ('ready','incomplete','unavailable')),
 weight numeric, value numeric, inputs jsonb,
 reason_id integer REFERENCES score_reasons(reason_id),
 PRIMARY KEY(result_id,profile,component),
 FOREIGN KEY(result_id,profile) REFERENCES calculated_scores(result_id,profile)
);
CREATE TABLE calculated_score_reasons (
 result_id bigint NOT NULL, profile text NOT NULL, position smallint NOT NULL,
 reason_id integer NOT NULL REFERENCES score_reasons(reason_id),
 PRIMARY KEY(result_id,profile,position),
 FOREIGN KEY(result_id,profile) REFERENCES calculated_scores(result_id,profile)
);
INSERT INTO score_reasons(reason) SELECT DISTINCT c.value->>'reason' FROM v1_scores s
CROSS JOIN LATERAL jsonb_each(s.components) c WHERE c.value->>'reason' IS NOT NULL ORDER BY 1;
-- Positions follow policy.KEYS, the insertion order of the payload components. Early payloads
-- have no weights: weight stays NULL, so all of their reasons are stored explicitly.
INSERT INTO calculated_score_components
SELECT s.result_id,s.profile,c.key,array_position(ARRAY['reliability','speed','uptime','coverage',
 'diversity','dcAsns','authenticity','value'],c.key)-1,c.value->>'state',
 (w.weights->>c.key)::numeric,(c.value->>'value')::numeric,nullif(c.value->'inputs','null'),r.reason_id
FROM v1_scores s JOIN v1_weights w USING(result_id,profile)
CROSS JOIN LATERAL jsonb_each(s.components) c LEFT JOIN score_reasons r ON r.reason=c.value->>'reason';
CREATE TEMP TABLE v1_reasons ON COMMIT DROP AS
SELECT s.result_id,s.profile,
 ARRAY(SELECT e FROM jsonb_array_elements_text(s.reasons) WITH ORDINALITY x(e,i) ORDER BY i) AS listed,
 ARRAY(SELECT coalesce(r.reason,c.component || ' not ready') FROM calculated_score_components c
  LEFT JOIN score_reasons r USING(reason_id) WHERE (c.result_id,c.profile)=(s.result_id,s.profile)
  AND w.target='' AND c.weight>0 AND c.state<>'ready' ORDER BY c.position) AS derived
FROM v1_scores s JOIN calculated_windows w USING(result_id);
CREATE TEMP TABLE v1_explicit ON COMMIT DROP AS
SELECT r.result_id,r.profile,e.reason,CASE WHEN e.i<=r.start THEN e.i-r.start-1
 ELSE e.i-r.start-cardinality(r.derived) END AS position
FROM (SELECT *,(SELECT min(o) FROM generate_series(0,cardinality(listed)-cardinality(derived)) o
  WHERE listed[o+1:o+cardinality(derived)]=derived) AS start FROM v1_reasons) r
CROSS JOIN LATERAL unnest(r.listed) WITH ORDINALITY e(reason,i)
WHERE e.i<=r.start OR e.i>r.start+cardinality(r.derived);
INSERT INTO score_reasons(reason) SELECT DISTINCT reason FROM v1_explicit
WHERE reason NOT IN (SELECT reason FROM score_reasons) ORDER BY 1;
INSERT INTO calculated_score_reasons
SELECT e.result_id,e.profile,e.position,r.reason_id FROM v1_explicit e JOIN score_reasons r USING(reason);
ALTER TABLE calculated_scores DROP COLUMN components, DROP COLUMN reasons;

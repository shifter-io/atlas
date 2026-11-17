-- Transactional calculated v18 → v19 migration (ATLAS-SCORE-SPARK-1), applied by storage.migrate()
-- after the earlier calculated steps: each network's Atlas Score per published build (schema.sql
-- describes it), filled from the retained published builds' heads as they were published (a build
-- before a methodology change keeps its scores). No other row is rewritten; store.V18_CHECKS
-- compares every latency day row with the copy taken by store.V18_SNAPSHOT.
CREATE TABLE IF NOT EXISTS calculated_score_history (
 provider text NOT NULL, kind text NOT NULL, period text NOT NULL, as_of timestamptz NOT NULL,
 score double precision, methodology_id text NOT NULL, PRIMARY KEY(provider,kind,period,as_of)
);
INSERT INTO calculated_score_history
SELECT DISTINCT ON (n->>'provider',n->>'kind',w.key,(s.payload->>'as_of')::timestamptz)
 n->>'provider',n->>'kind',w.key,(s.payload->>'as_of')::timestamptz,
 (w.value->'scores'->'balanced'->>'score')::double precision,s.payload->'methodology'->>'id'
FROM calculated_snapshots s JOIN calculation_builds b USING(build_id),
 jsonb_array_elements(CASE jsonb_typeof(s.payload->'networks') WHEN 'array'
 THEN s.payload->'networks' END) n,jsonb_each(n->'windows') w
WHERE b.published
ORDER BY n->>'provider',n->>'kind',w.key,(s.payload->>'as_of')::timestamptz,s.build_id DESC
ON CONFLICT DO NOTHING;

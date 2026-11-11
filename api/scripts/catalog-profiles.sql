-- The five ranking profiles of /v1/home, /v1/meta and /v1/rankings (site.ranking_profiles), in
-- the website's order. Label, description and weights are the website's
-- src/data/proxy-index/home.json profiles, copied verbatim: the owner's approved copy, so change
-- them only with the owner. tests/test_catalog_profiles.py fails unless balanced equals the
-- methodology's [weights] and latency-first its [profiles.latency_first]. A client re-ranks with
-- a profile from each row's breakdown; no profile score is published. A rerun updates the rows.
BEGIN;

INSERT INTO site.ranking_profiles(profile_id, label, description, weights, sort_order, is_demo)
SELECT v.*, false
FROM (VALUES
  ('balanced', 'Balanced',
   'The default. No single dimension can carry a provider on its own.',
   '{"reliability": 22, "speed": 14, "uptime": 10, "coverage": 12, "diversity": 8, "dcAsns": 10, "authenticity": 10, "value": 14}'::jsonb, 10),
  ('hard-targets', 'Hard targets',
   'For anti-bot, challenge pages and queues. Success rate is almost everything.',
   '{"reliability": 42, "speed": 8, "uptime": 8, "coverage": 8, "diversity": 6, "dcAsns": 8, "authenticity": 12, "value": 8}'::jsonb, 20),
  ('cost-first', 'Cost first',
   'For high-volume work on softer targets, where price per success dominates.',
   '{"reliability": 16, "speed": 8, "uptime": 6, "coverage": 8, "diversity": 6, "dcAsns": 6, "authenticity": 6, "value": 44}'::jsonb, 30),
  ('latency-first', 'Latency first',
   'For real-time lookups and price checks where round-trip time is the product.',
   '{"reliability": 16, "speed": 38, "uptime": 8, "coverage": 8, "diversity": 6, "dcAsns": 6, "authenticity": 6, "value": 12}'::jsonb, 40),
  ('compliance', 'Compliance',
   'For teams that must be able to explain where their exit IPs come from.',
   '{"reliability": 14, "speed": 6, "uptime": 10, "coverage": 6, "diversity": 8, "dcAsns": 18, "authenticity": 30, "value": 8}'::jsonb, 50)
) AS v(profile_id, label, description, weights, sort_order)
ON CONFLICT (profile_id) DO UPDATE
SET label = EXCLUDED.label, description = EXCLUDED.description, weights = EXCLUDED.weights,
    sort_order = EXCLUDED.sort_order, is_demo = false, updated_at = now();

COMMIT;

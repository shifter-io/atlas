-- Every framework target's category and difficulty for /v1/targets, /v1/meta and the dataset's
-- targets[] (site.targets). The categories are the website's (src/data/proxy-index/targets.json),
-- plus "AI answers" for the AI group and its sources. A browser target's difficulty is the
-- methodology's [browser_difficulty] (configs/methodology.toml), its scoring weight; ai_answers
-- takes its hardest source's. tests/test_catalog_targets.py fails when the two disagree.
-- label and pass_condition stay NULL so the framework's own win. A rerun updates the rows in place.
BEGIN;

INSERT INTO site.targets(target_id, category, difficulty, sort_order, is_demo)
SELECT v.*, false
FROM (VALUES
  ('generic_http', 'Baseline', 1, 10),
  ('generic_https', 'Baseline', 1, 20),
  ('amazon_pdp', 'E-commerce', 2, 30),
  ('google_search', 'Search', 3, 40),
  ('linkedin_public', 'Social', 3, 50),
  ('instagram_web', 'Social', 4, 60),
  ('cloudflare_challenge', 'Anti-bot', 4, 70),
  ('ai_answers', 'AI answers', 4, 80),
  ('ai_overview', 'AI answers', 4, 90),
  ('ai_chatgpt', 'AI answers', 4, 100),
  ('ai_gemini', 'AI answers', 3, 110)
) AS v(target_id, category, difficulty, sort_order)
ON CONFLICT (target_id) DO UPDATE
SET category = EXCLUDED.category, difficulty = EXCLUDED.difficulty, sort_order = EXCLUDED.sort_order,
    is_demo = false, updated_at = now();

COMMIT;

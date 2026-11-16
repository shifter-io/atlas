-- Every framework target's category and difficulty for /v1/targets, /v1/meta and the dataset's
-- targets[] (site.targets). The categories are target_category codes (/v1/codes): baseline, ecommerce,
-- search, social (with social_web, the social group, and its sources), anti_bot and ai_answers (the AI
-- group and its sources); the API also maps an older row's English name (Baseline, E-commerce, ...) to
-- its code. Every difficulty is the owner's fixed
-- value (2026-11-12), never derived from results: the methodology's [browser_difficulty] and
-- [http_difficulty] (configs/methodology.toml), the scoring weights. tests/test_catalog_targets.py
-- and tests/test_target_difficulty.py fail when they disagree.
-- label and pass_condition stay NULL so the framework's own win. A rerun updates the rows in place.
BEGIN;

INSERT INTO site.targets(target_id, category, difficulty, sort_order, is_demo)
SELECT v.*, false
FROM (VALUES
  ('generic_http', 'baseline', 1, 10),
  ('generic_https', 'baseline', 1, 20),
  ('amazon_pdp', 'ecommerce', 2, 30),
  ('google_search', 'search', 4, 40),
  ('linkedin_public', 'social', 5, 50),
  ('social_web', 'social', 3, 55),
  ('instagram_web', 'social', 3, 60),
  ('x_web', 'social', 3, 65),
  ('cloudflare_challenge', 'anti_bot', 4, 70),
  ('ai_answers', 'ai_answers', 5, 80),
  ('ai_overview', 'ai_answers', 5, 90),
  ('ai_chatgpt', 'ai_answers', 5, 100),
  ('ai_gemini', 'ai_answers', 5, 110)
) AS v(target_id, category, difficulty, sort_order)
ON CONFLICT (target_id) DO UPDATE
SET category = EXCLUDED.category, difficulty = EXCLUDED.difficulty, sort_order = EXCLUDED.sort_order,
    is_demo = false, updated_at = now();

COMMIT;

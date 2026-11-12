-- One-off for proxy_reviews (#940): the price history starts at the first true check.
-- Deletes every history row made from the old catalog (#847's first catalog and #927's
-- 2026-11-11 "previous check" rows), so the 2026-11-12 check shows as no price change.
-- Shifter's 2026-10-04 row is its verified live-page check (#847: "Verified from Shifter's
-- live pricing page on 2026-10-04"), kept and restated with the #927 rule: entry = the smallest
-- paid plan (Lite Spark, $2.00/GB), at scale $0.75/GB. Run as the catalog writer role, then
-- the pricing-<slug>.sql files (upserts). metric_evidence is not touched.
BEGIN;
DELETE FROM site.pricing_history
WHERE category='residential' AND effective_from < '2026-11-12'
  AND NOT (provider_slug='shifter' AND category='residential' AND effective_from='2026-10-04');
UPDATE site.pricing_history SET price_per_gb=2.0,price_per_gb_at_scale=0.75,updated_at=now()
WHERE provider_slug='shifter' AND category='residential' AND effective_from='2026-10-04';
COMMIT;

-- Transactional v18 → v19 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v18 or after the earlier steps (LOWAVAIL-1, #1181: two days in a row). country_verdicts.counts
-- is no longer generated from the verdict's own unique IPs: a country also counts on its first
-- daily test under 10 when it was normally big (its previous verdict had 10 or more, and its
-- verdicts of the 7 days before reached 50). Every stored verdict is set again from the ones
-- before it, as an import now sets it (verdicts.RECORD, which this statement repeats; a test
-- compares them). The incremental calculation's memos that a verdict rules on are emptied, so the
-- next calculation counts them again with the new verdicts. No other row changes. The
-- definitions repeat schema.sql exactly (tests compare catalogs).
ALTER TABLE country_verdicts ALTER COLUMN counts DROP EXPRESSION;
UPDATE country_verdicts v SET counts = v.unique_ips >= 10
 OR coalesce(h.previous >= 10 AND h.recent >= 50, false)
FROM (SELECT run_key,
 lag(unique_ips) OVER (PARTITION BY product_id,country_code ORDER BY decided_at) AS previous,
 max(unique_ips) OVER (PARTITION BY product_id,country_code ORDER BY decided_at
  RANGE BETWEEN interval '7 days' PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW) AS recent
 FROM country_verdicts) h
WHERE h.run_key = v.run_key AND v.counts IS DISTINCT FROM (v.unique_ips >= 10
 OR coalesce(h.previous >= 10 AND h.recent >= 50, false));
-- The calculation's memos record the verdict setting they were counted with (calculated_distinct):
-- forgetting it makes the next build empty the fleet IP memos and the geo rollups and count them
-- again (fleet_verdicts.ruled, rollups._ruled). A database without the calculated layer has none.
DO $$BEGIN
 IF to_regclass('calculated_distinct') IS NOT NULL THEN
  DELETE FROM calculated_distinct
   WHERE digest IN ('fleet-country-verdicts','rollups-country-verdicts');
 END IF;
END$$;

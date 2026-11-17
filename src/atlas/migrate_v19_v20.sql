-- Transactional v19 → v20 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v19 or after the earlier steps (COUNTRY-MIN-IPS-1, #1253). Whether a verdict counts is no
-- longer stored: every reader decides it from the verdict's unique IPs with the methodology's
-- country_min_ips (verdicts.RULE), so a changed minimum applies to every stored verdict and no
-- row is rewritten. The calculation's memos record the rule they were counted with
-- (verdicts.params()) and count again by themselves under a new one. No other row changes. The
-- definitions repeat schema.sql exactly (tests compare catalogs).
ALTER TABLE country_verdicts DROP COLUMN counts;

-- Transactional v15 → v16 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v15 or after the earlier steps (COUNTRY-MIN-IPS-1, #1137). It adds country_verdicts, whether a
-- country counts for a network: one row per daily test, its unique IPs and whether they reach
-- 10, filled here from every stored daily test as an import writes it (verdicts.RECORD, which
-- this statement repeats; a test compares them). It adds outages.end_reason, NULL on every
-- stored outage. No other row changes. The definitions repeat schema.sql exactly (tests compare
-- catalogs).
CREATE TABLE country_verdicts (
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL CHECK(country_code ~ '^[A-Z]{2}$'),
 decided_at timestamptz NOT NULL, run_key bigint NOT NULL UNIQUE,
 unique_ips bigint NOT NULL CHECK(unique_ips >= 0),
 counts boolean NOT NULL GENERATED ALWAYS AS (unique_ips >= 10) STORED,
 PRIMARY KEY(product_id,country_code,decided_at)
);
ALTER TABLE outages ADD COLUMN end_reason text CHECK(end_reason IN ('too_few_ips'));
INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips)
SELECT s.product_id,upper(p.requested_country),t.started_at,t.run_key,
 coalesce((t.summary->>'unique_ips')::bigint,0)
FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats p USING(pool_id)
WHERE t.summary->>'test_kind'='daily' AND upper(p.requested_country) ~ '^[A-Z]{2}$'
ORDER BY t.run_key ON CONFLICT DO NOTHING;

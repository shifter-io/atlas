-- Transactional v13 → v14 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v13 or after the earlier steps. It adds scope_ips, each scope's IP membership set
-- (as pool_ips per pool), and fills it once from the stored observations, so ingest counts the IPs
-- new to a scope with one key probe each instead of scanning ip_observations. migrate holds the
-- ingest lock exclusively, so no import adds observations between the fill and the commit. The
-- definition repeats schema.sql exactly (tests compare catalogs).
CREATE TABLE scope_ips (
 scope_id bigint REFERENCES scope_stats(scope_id), ip_id bigint REFERENCES ip_addresses(ip_id),
 PRIMARY KEY(scope_id,ip_id)
);
INSERT INTO scope_ips SELECT DISTINCT r.scope_id,o.ip_id FROM ip_observations o
JOIN test_runs r USING(run_key);

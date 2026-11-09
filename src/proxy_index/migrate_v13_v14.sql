-- Transactional v13 → v14 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v13 or after the earlier steps. It adds scope_ips, each scope's IP membership set
-- (as pool_ips per pool), and fills it once from the stored observations, so ingest counts the IPs
-- new to a scope with one key probe each instead of scanning ip_observations. migrate holds the
-- ingest lock exclusively, so no import adds observations between the fill and the commit.
-- The table is filled before its keys exist (one DISTINCT, no per-row index insert or foreign key
-- trigger; about 30M rows at the 7-day volume), then the keys are added: the result is the
-- definition in schema.sql exactly (tests compare catalogs). The memory settings last until the
-- migration commits.
SET LOCAL work_mem = '256MB';
SET LOCAL maintenance_work_mem = '1GB';
CREATE TABLE scope_ips (scope_id bigint, ip_id bigint);
INSERT INTO scope_ips SELECT DISTINCT r.scope_id,o.ip_id FROM ip_observations o
JOIN test_runs r USING(run_key);
ALTER TABLE scope_ips ADD PRIMARY KEY(scope_id,ip_id),
 ADD FOREIGN KEY(scope_id) REFERENCES scope_stats(scope_id),
 ADD FOREIGN KEY(ip_id) REFERENCES ip_addresses(ip_id);

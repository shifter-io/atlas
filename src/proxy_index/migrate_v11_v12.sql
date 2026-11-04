-- Transactional v11 → v12 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v11 or after the earlier steps. It replaces the never-scanned (ip_id, bucket_start)
-- index of ip_observations with (bucket_start, ip_id), for distinct-IP counts per time slice; no
-- stored row changes. The partitioned index covers every monthly and daily partition, and
-- building it holds a SHARE lock (no writes) on each partition until the transaction commits.
-- The definition repeats schema.sql exactly (tests compare catalogs).
DROP INDEX ip_observations_ip_time;
CREATE INDEX ip_observations_time_ip ON ip_observations(bucket_start,ip_id);

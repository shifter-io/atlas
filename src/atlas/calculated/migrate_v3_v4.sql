-- Older history has no per-bucket monitoring evidence and stays NULL.
ALTER TABLE calculated_series ADD COLUMN uptime double precision
 CHECK(uptime >= 0 AND uptime <= 1);

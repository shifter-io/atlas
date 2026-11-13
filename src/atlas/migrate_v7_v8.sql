-- Transactional v7 → v8 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v7 or after the earlier steps. It adds the daily latency-under-load reference
-- country of each runner region; no stored row changes. The definition repeats schema.sql
-- exactly (tests compare catalogs).
CREATE TABLE load_references (
 day date NOT NULL, region_id smallint NOT NULL REFERENCES runner_regions(region_id),
 country_code char(2) NOT NULL REFERENCES countries(country_code),
 basis text NOT NULL CHECK(basis IN ('fastest','override','default','first_enabled')),
 p50_ms integer, successes bigint, candidates integer NOT NULL DEFAULT 0,
 chosen_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(day,region_id)
);

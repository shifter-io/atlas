-- Transactional calculated v19 → v20 migration (CALC-SCALE-2), applied by storage.migrate() after
-- the earlier calculated steps: the per-day geo rollups and their marks (schema.sql describes
-- them), empty, and an index of the hour rollups by network; the next calculation's
-- rollups.fill() writes the days. No row is rewritten; store.V19_CHECKS compares the hour rollups'
-- count and counter sums with those store.V19_SNAPSHOT took. Run it while no calculation runs:
-- the index blocks writes to the hour rollups while it builds.
CREATE TABLE IF NOT EXISTS calculated_geo_days (
 day timestamptz NOT NULL, scope_id integer, product_id integer NOT NULL,
 location_id integer NOT NULL, network_id integer NOT NULL, fallback text, target text NOT NULL,
 run_key bigint, completed bigint NOT NULL, successes bigint NOT NULL, challenges bigint NOT NULL,
 blocks bigint NOT NULL, light_ms bigint[] NOT NULL, light_n bigint[] NOT NULL
);
CREATE INDEX IF NOT EXISTS calculated_geo_days_product ON calculated_geo_days(product_id,day);
CREATE TABLE IF NOT EXISTS calculated_geo_day_marks (day timestamptz PRIMARY KEY);
CREATE INDEX IF NOT EXISTS calculated_geo_hours_product ON calculated_geo_hours(product_id,hour);

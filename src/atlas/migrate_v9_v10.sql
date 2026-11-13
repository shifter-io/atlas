-- Transactional v9 → v10 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v9 or after the earlier steps. It adds the estimated hours of the daily tests
-- (ESTIMATE-1); no stored row changes. The definition repeats schema.sql exactly (tests compare
-- catalogs).
CREATE TABLE estimated_hours (
 product_id integer NOT NULL REFERENCES products(product_id),
 country_code char(2) NOT NULL CHECK(country_code ~ '^[A-Z]{2}$'),
 day date NOT NULL, hour smallint NOT NULL CHECK(hour BETWEEN 0 AND 23),
 run_key bigint NOT NULL REFERENCES test_runs(run_key),
 measured_at timestamptz NOT NULL,
 measured_ips bigint NOT NULL, measured_requests bigint NOT NULL,
 hour_factor double precision NOT NULL, unique_ips double precision NOT NULL,
 requests double precision NOT NULL, day_unique_ips double precision NOT NULL,
 curve text NOT NULL CHECK(curve IN ('country','network')), formula_version integer NOT NULL,
 computed_at timestamptz NOT NULL DEFAULT now(), released_at timestamptz,
 PRIMARY KEY(product_id,country_code,day,hour)
);
CREATE INDEX estimated_hours_unreleased ON estimated_hours(day,hour)
 WHERE released_at IS NULL;
CREATE INDEX estimated_hours_run ON estimated_hours(run_key);
CREATE INDEX estimated_hours_day ON estimated_hours(day);

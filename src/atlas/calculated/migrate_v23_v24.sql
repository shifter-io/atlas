-- Transactional calculated v23 → v24 migration (LATENCY-DELTA-1), applied by storage.migrate() after
-- the earlier calculated steps: the per-day latency store gains each day's basket countries (NULL
-- on every stored day) and the empty per-country latency days. The 30d latency change compares the
-- basket latency once 30 days of them are stored, its last 7 days or day until then
-- (latency_month.py). A nullable column and an empty table: no row is rewritten.
ALTER TABLE calculated_latency_days ADD COLUMN IF NOT EXISTS basket text[];
CREATE TABLE IF NOT EXISTS calculated_latency_country_days (
 provider text NOT NULL, kind text NOT NULL, target text NOT NULL, day timestamptz NOT NULL,
 country text NOT NULL, slots integer NOT NULL, ms integer[] NOT NULL,
 weights double precision[] NOT NULL, PRIMARY KEY(provider,kind,target,day,country)
);

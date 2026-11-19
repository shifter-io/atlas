-- Transactional calculated v17 → v18 migration (LATENCY-DAY-STORE-1), applied by storage.migrate()
-- after the earlier calculated steps: each network's latency per UTC day (schema.sql describes it),
-- empty; the next build fills it from the 30 days of raw rows it reads. No row is rewritten;
-- store.V17_CHECKS compares every unique IPs row with the copy taken by store.V17_SNAPSHOT.
CREATE TABLE IF NOT EXISTS calculated_latency_days (
 provider text NOT NULL, kind text NOT NULL, target text NOT NULL, day timestamptz NOT NULL,
 minimum integer NOT NULL, slots integer NOT NULL, ms integer[] NOT NULL,
 weights double precision[] NOT NULL, countries jsonb NOT NULL, digest text NOT NULL,
 as_of timestamptz NOT NULL, PRIMARY KEY(provider,kind,target,day)
);

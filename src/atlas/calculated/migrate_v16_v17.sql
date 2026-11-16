-- Transactional calculated v16 → v17 migration (CALC-SCALE-1), applied by storage.migrate()
-- after the earlier calculated steps: each network's unique IPs per hour, day, rolling 30 days and
-- month (schema.sql describes it), empty; the next hourly builds fill it. No row is rewritten;
-- store.V16_CHECKS compares every IP vector row with the copy taken by store.V16_SNAPSHOT.
CREATE TABLE IF NOT EXISTS calculated_unique_ips (
 provider text NOT NULL, kind text NOT NULL,
 period text NOT NULL CHECK(period IN ('hour','day','rolling30d','month')),
 starts timestamptz NOT NULL, unique_ips bigint NOT NULL, countries integer, regions integer,
 cities integer, asns integer, days integer, complete boolean NOT NULL DEFAULT false,
 rank integer, ranked_of integer, computed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(provider,kind,period,starts)
);

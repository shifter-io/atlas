-- Transactional calculated v15 → v16 migration (CALC-SCALE-1), applied by storage.migrate()
-- after the earlier calculated steps: the counts across product groups' state (schema.sql
-- describes it), empty, and a nullable column on the bucket memo. No row is rewritten; the next
-- hourly build fills the tables. store.V15_CHECKS compares every bucket memo row with the copy
-- taken by store.V15_SNAPSHOT.
ALTER TABLE calculated_bucket_ips ADD COLUMN IF NOT EXISTS scopes_digest text;
CREATE TABLE IF NOT EXISTS calculated_scope_ip_last_seen (
 ip_id bigint NOT NULL, scope bigint NOT NULL, last_seen timestamptz NOT NULL,
 PRIMARY KEY(ip_id,scope)
);
CREATE INDEX IF NOT EXISTS calculated_scope_ip_last_seen_time
 ON calculated_scope_ip_last_seen(last_seen);
CREATE TABLE IF NOT EXISTS calculated_ip_vectors (
 window_hours integer NOT NULL, scopes bigint[] NOT NULL, ips bigint NOT NULL,
 PRIMARY KEY(window_hours,scopes)
);
CREATE TABLE IF NOT EXISTS calculated_ip_vector_state (
 as_of timestamptz NOT NULL, windows integer[] NOT NULL, unbatched bigint[] NOT NULL
);

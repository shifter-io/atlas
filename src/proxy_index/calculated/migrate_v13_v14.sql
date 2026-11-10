-- Transactional calculated v13 → v14 migration (API-LOAD-STATEMENTS-1), applied by
-- storage.migrate() after the earlier calculated steps: calculated_snapshots gains api_head, the
-- part of a v4 head the API reads, which parts.write_api_head() writes when a build is published.
-- A nullable column without a default rewrites no row, and no stored build gets one: the API
-- reads a head without it as before, and the next publication writes it. store.V13_CHECKS
-- compares every snapshot with the copy taken by store.V13_SNAPSHOT.
ALTER TABLE calculated_snapshots ADD COLUMN IF NOT EXISTS api_head jsonb;

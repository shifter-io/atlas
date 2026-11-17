-- Transactional calculated v19 → v20 migration (SCORE-HISTORY-BACKFILL-1), applied by
-- storage.migrate() after the earlier calculated steps: each Atlas Score history row gains its
-- source (schema.sql describes it). Every stored row was written at its build's publication, so it
-- is 'build'; the key gains the source, so a recomputed row stands beside a published one. No row
-- is rewritten otherwise; store.V19_CHECKS compares every history row with the copy taken by
-- store.V19_SNAPSHOT.
ALTER TABLE calculated_score_history ADD COLUMN IF NOT EXISTS source text NOT NULL
 DEFAULT 'build' CHECK (source IN ('build','backfill'));
ALTER TABLE calculated_score_history DROP CONSTRAINT calculated_score_history_pkey,
 ADD PRIMARY KEY(provider,kind,period,as_of,source);

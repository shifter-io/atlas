-- Transactional v17 → v18 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v17 or after the earlier steps (PULSE-STATE-2, #1169 items 1 and 2). It adds batch_runs.start_at,
-- a run's own start when it differs from its batch's: a stalled pulse node's run moved to another
-- node starts later than the batch the other networks share, which keeps its start (the pulse
-- evaluator reads each run by its own start). NULL on every stored run (its batch's start_at). No
-- other row changes. The definitions repeat schema.sql exactly (tests compare catalogs).
ALTER TABLE batch_runs ADD COLUMN start_at timestamptz;

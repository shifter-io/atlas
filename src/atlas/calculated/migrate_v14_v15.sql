-- Transactional calculated v14 → v15 migration (COVERAGE-IPS-RANK-1), applied by
-- storage.migrate() after the earlier calculated steps: calculated_list_rows gains ips_rank and
-- ips_peers, which netlists.rank() writes on the rows of the network lists of each build written
-- from now on. Nullable columns without a default rewrite no row, and no stored list gets them:
-- the API reads a NULL rank as none, and the next build ranks its own network lists.
-- store.V14_CHECKS compares every list row with the copy taken by store.V14_SNAPSHOT.
ALTER TABLE calculated_list_rows ADD COLUMN IF NOT EXISTS ips_rank integer,
 ADD COLUMN IF NOT EXISTS ips_peers integer;

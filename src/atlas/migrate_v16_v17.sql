-- Transactional v16 → v17 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v16 or after the earlier steps (PULSE-STATE-2, #1169). It adds three nullable columns to nodes,
-- the host's pressure each heartbeat reports (node_health): io_pressure (PSI some avg10, %),
-- mem_available_mb and swap_pages (swapped in and out since the previous heartbeat). No row
-- changes. The definitions repeat schema.sql exactly (tests compare catalogs).
ALTER TABLE nodes ADD COLUMN io_pressure double precision, ADD COLUMN mem_available_mb integer,
 ADD COLUMN swap_pages bigint;

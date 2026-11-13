-- Transactional calculated v12 → v13 migration (LISTS-WRITE-1), applied by storage.migrate()
-- after the earlier calculated steps: calculated_list_rows.list_id loses its foreign key to
-- calculated_lists, whose per-row check made writing a build's lists take hours (list_write.py).
-- Dropping a constraint rewrites no row; store.V12_CHECKS compares each list's row count with the
-- copy taken by store.V12_SNAPSHOT and finds no row without its list.
-- IF EXISTS: a layer rebuilt from this schema's tables, as some tests do, never had it.
ALTER TABLE calculated_list_rows DROP CONSTRAINT IF EXISTS calculated_list_rows_list_id_fkey;

# The upload path's own database login (SEC-INGEST-ROLE-1, #1022)

The orchestrator's node API receives every run a node uploads and imports it (`POST /v1/results`,
`storage.ingest`). Until this change that import used the run-time login, which may change and
delete every table. With this change the import can use a login of its own, `atlas_ingest`,
which may only add a run: read and add the rows a run brings, update the lifetime counter columns
a new run moves, and make its own temporary tables. It may not delete, empty or alter any table,
read any other table, or create anything. Everything else in the orchestrator (the jobs, the
run states, the alerts, the admin API) keeps the run-time login.

What the login holds is listed once in `src/atlas/dbserver/ingest.py` and, as reviewed SQL, in
`scripts/db/ingest-role.sql`; `atlas db-server check` reports anything it lacks or holds beyond
that (`ingest_login`, `ingest_excess`, `ingest_role`, `ingest_grant`, and the role in
`temporary`). `tests/test_ingest_role.py` runs the real upload path under exactly these grants;
removing any single one of them makes an upload fail.

A run's first upload of a new day needs that day's partitions. The ingest login cannot create
them (that needs the tables' owner), so the orchestrator creates them over a second connection
with the run-time login, then goes on with the ingest login.

## Rollout (the owner or an operator, after merge and deploy; production keeps ingesting)

Nothing changes until step 3: without the new setting the orchestrator ingests with the run-time
login, as before. The commands below use placeholders; the server's own details are in the
owner's private runbook.

1. **Create the role.** Connected to the framework's database as the admin login if it has
   CREATEROLE, else as the server's superuser:
   - dry run (changes nothing; the last table shows what the role would hold):
     `psql "<admin connection>" -v ON_ERROR_STOP=1 -f scripts/db/ingest-role.sql`
   - apply: the same with `-v apply=1`
   - set its password, typed at the prompt and sent hashed:
     `psql "<admin connection>" -c '\password atlas_ingest'`
   - check: `atlas db-server check --database-file <admin file>` shows no `ingest_*` gap.
2. **Add the second database file** on the manager, next to the run-time one and like it
   (owner-only, mode 0600, read in place): the same `[postgres]` host, port, database and
   `sslmode`, with `user = "atlas_ingest"` and its password.
3. **Point the orchestrator at it and restart.** Add
   `ATLAS_INGEST_DATABASE_FILE=<path of the new file>` to the orchestrator's environment file
   (the unit's `EnvironmentFile`), then restart the orchestrator service. A file it cannot read
   stops the start (the service log names the error), so nothing half-works.
4. **Verify the uploads.** Within a few minutes: new runs reach `done` in the admin API, no new
   `ingest_failed` alert, and on the database server the ingest login's sessions appear
   (`SELECT count(*) FROM pg_stat_activity WHERE usename = 'atlas_ingest'` over a few minutes).
   An upload refused for a missing right fails its run with an `ingest_failed` alert and a
   `permission denied` reason in the run's error.
5. **Rollback** (any time): remove the `ATLAS_INGEST_DATABASE_FILE` line and restart the
   orchestrator: uploads use the run-time login again at once. Then, if the role should go:
   `scripts/db/ingest-role-rollback.sql`, dry run first, then `-v apply=1`, and delete the
   second database file.

What a bad step costs: when the ingest login cannot connect (a wrong password, a missing
role), the upload is answered 503 and the node keeps its file and retries, so after the fix (or
the rollback) the run is imported. When it connects but lacks a right, the import rolls back and
that run is failed (`ingest_failed` alert); nothing stored changes, and later runs import once
the right is back or the rollback is done.

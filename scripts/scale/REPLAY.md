# Replay of simulated days (REPLAY-1)

`replay.py` replays simulated days of real-data hours back to back, without waiting real time.
It runs through production's own code paths, in the staging database `proxy_reviews_staging`
(the same server as production, created empty), with the owner's go
(REPLAY-RUN-1, #614). Its input is the 24 hourly slices of SLICES-1 (`slices.py`) and the
modelled browser hour of BROWSER-MODEL-1 (`browser_model.py`). REPLAY-REPORT-1 (#611) turns its
state into the report ("Report" below).

## Usage

```sh
uv run python scripts/scale/replay.py --database proxy_reviews_staging.toml \
  --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>" --slices <slices dir> \
  --days 7 --first-day <YYYY-MM-DD> --out <dir> \
  [--hour-timeout 3000] [--siteapi http://127.0.0.1:<port>] [--siteapi-timeout 600] [--no-scope]
```

- `--first-day` is the date of simulated day 0; day d is dated `first-day + d`. The last
  simulated hour must end before now.
- The same command continues a stopped run from `<out>/state.json`. A state file of another
  database, first day or number of days is refused.
- `--siteapi` is a siteapi reading the target database (staging's, not production's), at its admin
  listener (`serve --admin-listen`; the public `/healthz` has no fingerprint): after
  each calculation the replay waits (up to `--siteapi-timeout` seconds) until its `/healthz` serves the new build. Without it, that wait
  is skipped.
- `--no-scope` runs the calculation without the systemd memory scope (where the user may not
  create scopes).
- Exit codes: 0 done, 2 refused (nothing written), 130 stopped (Ctrl-C or SIGTERM).

## Before the run: production's plan and its volume (REPLAY-FIX-2)

The replay plans with the target database's own settings, targets and countries. Staging is
seeded with the defaults (every country enabled, every pulse target every 30 minutes), which
plans far more than production does. So, first:

```sh
uv run python scripts/scale/copy_plan.py --from proxy_reviews.toml --to proxy_reviews_staging.toml
```

`copy_plan.py` reads production's scheduling inputs in one read-only transaction: the countries
(`enabled`, `census_time`, the region by its slug), the schedule fields of the targets
(`census`, `census_concurrency`, `pulse_enabled`, `pulse_interval_minutes`, `pulse_requests`,
`pulse_concurrency`) and the `settings` rows `scheduler.*`, `census.*`, `load.*` and `pulse.*`.
It writes them in one transaction to `proxy_reviews_staging` (`target.STAGING`) and refuses any
other database. Staging then plans what production plans: a country or target production does
not have is switched off in staging, and a settings row of those groups production does not have
is deleted (production uses the default). A country or target production schedules that staging
lacks, or a region staging lacks, is refused (seed staging first). It prints every row it
changed as a JSON line (`table`, `key`, `before`, `after`; `null`: no row); a second run changes
nothing.

Then the volume, before anything is written:

```sh
uv run python scripts/scale/replay.py --database proxy_reviews_staging.toml \
  --slices <slices dir> --days 7 --first-day <YYYY-MM-DD> --plan-only
```

`--plan-only` (`replay_plan.py`) reads the database in one read-only transaction and prints, for
each simulated hour, the runs it would plan by kind: `daily` (the tests of its slice), `pulse`
and `load` (the batch_runs rows of `batches.pulse_plan` at the hour's 5-minute moments) and
`browser` (BROWSER-MODEL-1's hour), then the totals of the `--days`. It counts with the planners'
own slot rules (`pulse_slots.spread`, `browser.slot_starts`) and the products that take part
once the replay's nodes are registered; it needs no `--owner-go` or `--out`. REPLAY-RUN-1 (#614)
posts this with its first status.

## Guards

Checked at every start, before anything is written:

- **Target:** `target.refusal(conn, owner_go=...)` (SCALE-TARGET-2): the go has the owner's form
  and a date not after today, the database is staging's (`proxy_reviews_staging`), and it holds
  no test run without the replay's marker. Production's `proxy_reviews` is refused, with or
  without the go. The go string, the database and the start time are kept
  in the state (`go`, `started_at`, and each resume's go in `resumed`), for the report.
- **Live scheduler:** a `job_runs` row started in the last 5 minutes means an orchestrator runs
  against this database (the check reads only the target database: production's scheduler never
  touches staging, so it catches an orchestrator pointed at staging). The replay refuses: the hourly `calculate` would build
  on the replay's data, and its planners would plan work for the replay's nodes.
- **Days:** the last simulated hour must have ended.

The replay writes no settings row. It reads the database's own settings and targets (copied
from production beforehand, above).

## One simulated hour

Hour h of day d (`replay_steps.py`):

1. **Daily tests.** Each test of `slice-<HH>.json` becomes a run directory, built as
   `cycle_steps.daily_directory` builds one (`cycle_steps.daily_files`, `daily_buckets`,
   `Steps.daily_summary`): `summary.json`, `ips.jsonl.gz`, `enriched-<release>.jsonl.gz` and
   `enrichment-<release>.json`. It carries the slice's real IPs, counts and enrichment, with
   timestamps moved to the simulated hour; its successes are the slice's request rows, its
   timeouts the slice's modelled `slot_errors`, and its bucket counters and latency are modelled
   as the cycle does (2 to 4 five-minute buckets, the network's latency bins). It is stored with
   `storage.ingest` (one transaction per run) and `compute.estimate_run` with the network's
   formula (`formulas/<network>.json`). A run counts as stored once both are done; any other
   failure of either retries the run (both are idempotent). A country the formula cannot
   estimate is noted (`not_estimated`), not retried.
2. **HTTP pulses.** `cycle_steps.Steps.pulses` with the replay's hooks: `batches.pulse_plan` at
   every 5 minutes of the hour on the database's settings and targets, then every planned pulse
   and load-wave run of the replay's nodes uploaded to an in-process node API (`POST
   /v1/results`, the real ingest). The runs are modelled (no real pulse data exists): 100
   requests, 0 to 3 timeouts, geo rows from the slice's enrichment of the country; a load-wave
   run also carries IP rows (the slice's IPs of the country), as its ingest requires. The
   timeouts are a geo row with no place and network; a country with no rows in the hour has
   that one row alone, with every request and the timeouts (a second row with the same empty
   key would be refused as a duplicate geo bucket).
3. **Browser.** BROWSER-MODEL-1's `browser_step` for the slices' networks.
4. **Release.** `cycle_steps.Steps.release`: the estimated hours that ended are released.
5. **Calculate.** `atlas calculate --as-of <hour end> --publish --incremental` in a child
   process under `measure.run_child` and `measure.py trace`. Where `systemd-run` exists, it runs
   under `systemd-run --scope -p MemoryMax=10G -p MemorySwapMax=0` (as in #150). Then the siteapi
   wait (above).

## Nodes

The replay registers its own nodes: for the HTTP pulses one node per network in every region of
an enabled country, named `scale-replay-<region>`; for the browser hour BROWSER-MODEL-1's node per
network on `browser-scalereplay` (a browser host name must be `browser-<letters and digits>`).
A node counts as the replay's when its id is in the state or its host is one of these names; the
browser nodes are recorded even when the hour fails after registering them. Where production
already has a node of that network in a region, the replay registers none there: `issue_token` would rotate production's token. Production's own nodes' runs stay queued
(production's reaper fails them later); the replay does not wait for them.

At the end, or on any stop, the replay's nodes are disabled: their token hash is replaced by a
value no token can match and their status set to `offline`. Their rows stay (their runs reference
them). A resumed run issues them new tokens. The batch planners still count a disabled node's
product as a participant: once production's scheduler runs again, it plans runs for these nodes
and its reaper fails them.

## Pipeline and order

- **Production overlap:** hour H+1's ingest (steps 1 to 4, in the replay's process) runs while
  hour H's calculation runs (the child process). A calculation never starts before its own
  hour's ingest has finished.
- **Strict order:** H+1's calculation waits until H's has succeeded, and H+2's ingest waits until
  H's calculation has succeeded (a pipeline depth of 1). A stuck hour delays every later hour.

## Timeout and retry

- A calculation **fails** when it runs over `--hour-timeout` (default 3000 s, production's
  `calculate.timeout_seconds` before CALC-LIMITS-1 and -2 raised it to 7200; its process group is
  killed) or exits non-zero (an OOM kill included). The calculation is one transaction: killed or failed, nothing is published and no
  data is lost. The replay pauses and retries the same hour; later hours wait.
- An ingest step **fails** when a run's files or ingest fail (`storage.ingest` rolls that run back
  alone), when a pulse or browser run is left open, or when the step raises. The hour's stored
  runs are kept; the retry ingests only the missing ones (by `run_id`).
- **Pause:** 5 minutes at first, doubling with each retry of the hour, up to 60.
- **Logged and counted:** every retry and its pause is a line of `<out>/events.jsonl` (`time`,
  `hour`, `step`, `reason`, `pause`) and is counted in the state (`counts`: retries,
  pause seconds, retries per step) for the report.
- An hour retried more than 3 times is flagged on stderr for the operator.
- The run ends when the days are done or the operator stops it; it can always be resumed.

## Database size

After every hour the state records `pg_database_size` of the database (`database_bytes`) and the
sum over every database the role can connect to (`all_databases_bytes`). There is no disk check
and no volume-size setting (owner, 2026-11-07): the owner's server monitoring alerts on disk.

## Statement timing

Two sources (REPLAY-STATS-1, #629). The client side is the base and is always recorded, so the
replay works on a database without `pg_stat_statements`. Where the database has it (staging's
does, owner 2026-11-07), the report takes its statement numbers from the server first.

**Client side**, always:

- the ingest steps in-process through `measure.Tracer` (the hour's `ingest.statements`);
- the calculation child under `measure.py trace` (the hour's `calculate.statements`).

Each is `measure.Tracer`'s summary: every `execute`, `executemany` and COPY (timed from its start
to the end of its block) with its wall time and rows (the cursor's rowcount; for a COPY, the rows
copied). Statements are grouped by shape: the first 200 characters with numbers and quoted
literals as `?`, so no value is kept. A summary has the statement count, the total seconds, the
5 slowest statements (shape, seconds, rows) and per shape its calls, seconds, maximum, rows and
a histogram of its times (buckets of a factor 1.1), from which the report adds up the run's p95
(within 5%).

**Server side**, when `measure.has_statements` finds `pg_stat_statements`. It needs no superuser
right or `pg_read_all_settings` (proxy_admin has neither): the extension exists in the database,
its view is on the search path, and `SELECT 1 FROM pg_stat_statements LIMIT 1` succeeds inside a
savepoint (it fails while the server hasn't loaded the library). It answers True or False for
any role.

- `measure.snapshot` reads this database's counters (`dbid`) per queryid: calls, total exec ms,
  rows and `max_exec_time`. Another role's statements (siteapi as `proxy_api`, for example),
  whose text and queryid this role can't read, are added up per role as `other role`.
- The hour's calculation runs beside the next hour's ingest, so the server can't tell their
  statements apart. Snapshots are therefore taken only while nothing else runs: at the run's
  start, after the first hour's ingest, and at the end of each hour's calculation (its retries,
  the siteapi wait and the next hour's ingest included). The windows between them never
  overlap. Each is the change of the counters (`statement_source.window`), with no reset, kept
  in the record of the hour it ends on, as `server`: `covers` names the hours' ingest and
  calculation it holds. The windows are: the first ingest; then each calculation with the next
  hour's ingest; then the last calculation alone.
- `max_exec_time` is cumulative, so it is kept as `max_ms_since_reset`, never as an hour's
  maximum.
- Texts are shapes, like the tracer's: 200 characters, literals as `?`.
- A stopped run loses its open window; the resumed run opens a new one at its start.

## Telling replay rows from the database's own

- **Markers:** every run the replay stores has the summary key `scale_replay` (the replay's id):
  daily tests, pulses and browser runs. Its nodes are named as above.
- **Baseline** (`replay_baseline.py`, in the state at the start): the row count of every table in
  `public` and `site`, the highest value of every identity column, and the full key sets of
  providers, products, pool_stats, scope_stats, nodes, targets, dbip_releases, place_names,
  asn_names, asn_labels, enrichment_locations, enrichment_networks and cohorts.
- **After each day** (`days_counted`): the counts again, per table the growth since the baseline,
  the replay's rows (reachable from its runs by `run_key` and from its nodes by `node_id`,
  `test_batches` through their runs) and the other growth (`job_runs`, alerts, ...), reported as
  the database's own.

## Outputs

`<out>` gets mode 700 and every file mode 600:

- `state.json`: the replay id, the go and every resume, the baseline, the nodes, per hour its
  steps' parts, start and end times of the ingest and the calculation, the calculation's
  attempts, its build id, the siteapi wait, the statements (and the server windows) and the
  sizes (the baseline has the size before the run), the day counts (with each table's size at
  the day's end) and the retry counts.
- `events.jsonl`: one line per retry.

- `report.md` and `report.json`: the report (below), written again after every hour.

The command prints the replay id and the hours done.

## Report

`replay_report.py <out>` (REPLAY-REPORT-1, #611) writes `report.md` and `report.json` from
`state.json` and `events.jsonl`; `replay.py` runs it after every hour, so a stopped run has one.
Days are numbered from 1. An hour's ingest resumed after a stop is traced from the resume on.

- **Per hour:** the daily tests; the rows inserted per table: what the ingest's plain INSERT and
  COPY statements added, from their rowcount (only tables of the baseline, so no staging table;
  the ingest writes replay rows only). Upserts (`ON CONFLICT DO UPDATE`, which the tracer flags
  from the full statement: product_ips, provider_overlap) count updated rows and an INSERT inside
  `WITH` (pool_ips) reports its SELECT's rows, so neither is counted per hour; the hardest and
  the lightest hour are by these rows. Then the slice's bytes (`slice-HH.json` and `.jsonl.gz`);
  the database's growth since the hour before (the baseline's size before the first hour); the
  ingest's wall time without its retries' pauses (those are the delay) and the p50, p95 and
  maximum per daily test;
  the calculation's seconds and peak RSS (its successful attempt); the slowest statement of the
  ingest or the calculation; the retries and their delay (`events.jsonl`).
- **Per day:** the sums; the replay rows per table, exact: the day's growth of the rows
  reachable from the replay's runs and nodes (`days_counted[d].replay`); the hourly calculation's
  p95 and maximum, the database size after the
  day and the server's total (all databases), in GB.
- **The hardest hour** (most rows written) **and the lightest**, by day and hour.
- **The last day** (day 7 of a full run): the sizes, each table's size with its partitions
  (`measure.sizes`, recorded at each day's end as `days_counted[d].table_bytes`) and
  production's own growth during the run (`other`: rows not reachable from replay rows).
- **Statement time per hour:** with `pg_stat_statements`, the server's calls, total ms, mean ms
  and rows of the hour's windows, and next to them the client's calls and wall seconds of the
  hour's ingest and calculation. Without it, the client's only, with a note.
- **The run's 20 slowest statement shapes:** with `pg_stat_statements`, by server time: total
  ms, calls, mean ms, rows and the maximum since the counters' reset (other roles' statements
  as `other role`). Always, by client wall time: calls, seconds, p95, maximum and rows. The
  report's first lines name the source; each table says which one it uses.
- **Labelled:** the modelled parts (byteful rows, HTTP pulses, browser data, bucket counters and
  latency) and the IP tables as a lower bound: the 7 days are copies of one day, so
  ip_addresses, ip_enrichment, product_ips and pool_ips stop growing after day 1, whereas in real
  data 30-60% of a day's IPs recur (#150).

It holds no IP, host name, credential or path: the slices' directory and the retry reasons are
left out, and statements are shapes.

## Wiping the replay (WIPE-TOOL-1)

`wipe_replay.py` removes exactly the replay's data from the database it ran in, before the launch
(LAUNCH-WIPE, #615). It reads the replay's `state.json`: its baseline, nodes and simulated days.

```sh
uv run python scripts/scale/wipe_replay.py --database proxy_reviews_staging.toml --state <out>/state.json
uv run python scripts/scale/wipe_replay.py --database proxy_reviews_staging.toml --state <out>/state.json \
  --apply --owner-go "owner-go LAUNCH-WIPE <YYYY-MM-DD>"
```

- **Dry run** (the default): the whole wipe runs in one transaction, which is then rolled back. It
  prints per table the rows it would delete (`deleted`), the counts it would leave, the tables
  that would differ from the baseline, the empty partitions and the build that would be current;
  the same JSON goes to `wipe-dry-run.json` (mode 600) beside the state.
- **`--apply`** needs the owner's go, `owner-go LAUNCH-WIPE <YYYY-MM-DD>` (a date not after
  today), and a dry run of the same replay. It runs the same transaction under the ingest and
  calculation advisory locks (817452901 and 817452902) and commits it only when every table's
  deleted rows equal the dry run's; any difference rolls everything back, as `archive/purge.py`
  does. It prints the per-table counts afterwards; `not_at_baseline` lists every table whose
  count differs from the baseline's.
- The transaction is REPEATABLE READ, so the counts see only its own deletes. Discarding the
  builds drops their snapshot partitions, which locks `calculated_snapshot_parts` until the
  transaction ends (the dry run's too): the siteapi's reads of it wait meanwhile.
- A state of another database is refused. Exit codes: 0 done, 2 refused or rolled back.

**What counts as the replay's:** runs whose summary has `scale_replay` (above the baseline's
highest run key), the replay's nodes (above the baseline's highest node id, host
`scale-replay-*` or `browser-scalereplay`, or listed in the state) and builds above the baseline.

**What goes, in foreign-key order:**

- Builds above the baseline, through `store_retention.discard` (snapshot partitions included),
  then the lists no window references. When one of them is current, the newest published
  baseline build left becomes current (none left: no current build).
- The runs with their test_events (of their batches, or dated in the simulated days),
  run_buckets, run_geo_buckets, ip_observations, run_enrichments, estimated_hours,
  calculation_roles, their daily tests' country_verdicts (schema v16) and batch_runs; the batches whose runs all belong to the replay's nodes, with
  their outages; the nodes (their token hash is in the row).
- The live tape's bucket counts (`test_event_buckets`, schema v13) of every 30-minute bucket a
  removed event fell in are counted again from the events left (`test_event_bucket_counts`):
  the insert trigger only ever adds.
- The calculation's caches of the replay: the run memos (`calculated_run_ranges`), the rollups
  and memos of the simulated days, the rollup hours written after the baseline (`through_run`
  above its highest run key), with their geo hours and pulse buckets, the distinct and slot
  memos used since the baseline, and the load references chosen since the baseline for the
  simulated days. The calculation rewrites any of these it misses.
- Lookup rows new since the baseline (not among its keys, or above its highest identity) that no
  remaining row references except a cache: scope_stats, pool_stats, products, providers,
  ip_addresses, dbip_releases, enrichment_locations, enrichment_networks, asn_labels,
  place_names, asn_names, cohorts and score_reasons. The cache rows that reference them go
  first: product_ips, pool_ips, scope_ips, provider_overlap and outages, and an IP's ip_enrichment and
  calculated_ip_last_seen.
- The provider overlaps the ingest updated since the baseline are counted again from the IPs both
  providers still have in product_ips; a pair with none left goes.
- The empty daily partitions of the simulated days.

**Never removed:** settings, countries, runner_regions, methodologies, targets, schema_versions,
calculation_schema, `site.*`, and any row of the baseline. The browser targets the replay's ingest
added stay; the output lists them (`targets_kept`). A row the replay changed in place (a
baseline IP's product_ips counts, for example) cannot be restored; with no test run in the
database at the baseline (the replay's guard), there is none.

## Tests

`tests/test_scale_wipe_replay.py` replays the fixture slice set for 2 hours on the test database
and checks the dry run's counts (equal to the replay's growth outside the protected tables, the
database unchanged), the refusals (no dry run, no go, a malformed or future go, another
database), the apply (every table back at its baseline count but the protected tables the replay
grew, those row by row, no current build), a production run ingested after the baseline that
survives with its lookups, a dry run whose counts differ that rolls back, and a current replay
build that gives way to the newest baseline build.

`tests/test_scale_replay.py` replays a fixture slice set (synthetic addresses only) on the test
database, standing in for staging: 2 days of 3 hours in order with the overlap (by
timestamps), the sizes, the timed statements, the baseline, the markers, the day counts, no
settings row and the nodes disabled at the end; a calculation that times out and fails, retried
with doubling pauses until it succeeds and flagged; an ingest failure that keeps the stored run;
a stopped run resumed from its state file (with a stand-in siteapi); and the refusals (a recent
`job_runs` row, a malformed go, unended days). `--hours-per-day` and `--pause` (hidden options)
shorten the days and the pauses for the tests.

`tests/test_scale_replay_plan.py` checks the pulse runs of an hour whose countries have no slice
rows (one empty geo key each, ingested through the node API), that `--plan-only`'s counts per
hour equal the batch_runs and daily runs the hour's steps then plan (with a 7-minute census
target: late slots and load waves), and that `--plan-only` prints the table in a read-only
transaction and writes nothing. `tests/test_scale_copy_plan.py` copies between two test
databases standing in for production and staging: the rows changed, a second run changing
nothing, production read in one read-only repeatable-read transaction, and the refusals (a
database other than staging, a scheduled target staging lacks).

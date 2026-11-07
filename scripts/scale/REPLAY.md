# Replay of simulated days (REPLAY-1)

`replay.py` replays simulated days of real-data hours back to back, without waiting real time.
It runs through production's own code paths, in production's database, with the owner's go
(REPLAY-RUN-1, #614). Its input is the 24 hourly slices of SLICES-1 (`slices.py`) and the
modelled browser hour of BROWSER-MODEL-1 (`browser_model.py`). REPLAY-REPORT-1 (#611) turns its
state into the report.

## Usage

```sh
uv run python scripts/scale/replay.py --database proxy_reviews.toml \
  --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>" --slices <slices dir> \
  --days 7 --first-day <YYYY-MM-DD> --out <dir> \
  [--hour-timeout 3000] [--siteapi http://127.0.0.1:<port>] [--siteapi-timeout 600] [--no-scope]
```

- `--first-day` is the date of simulated day 0; day d is dated `first-day + d`. The last
  simulated hour must end before now.
- The same command continues a stopped run from `<out>/state.json`. A state file of another
  database, first day or number of days is refused.
- `--siteapi` is production's siteapi: after each calculation the replay waits (up to
  `--siteapi-timeout` seconds) until its `/healthz` serves the new build. Without it, that wait
  is skipped.
- `--no-scope` runs the calculation without the systemd memory scope (where the user may not
  create scopes).
- Exit codes: 0 done, 2 refused (nothing written), 130 stopped (Ctrl-C or SIGTERM).

## Guards

Checked at every start, before anything is written:

- **Target:** `target.refusal(conn, owner_go=...)` (SCALE-TARGET-2): the go has the owner's form
  and a date not after today, the database is production's (`proxy_reviews`), and it holds no
  test run without the replay's marker. The go string, the database and the start time are kept
  in the state (`go`, `started_at`, and each resume's go in `resumed`), for the report.
- **Live scheduler:** a `job_runs` row started in the last 5 minutes means production's
  orchestrator runs against the database. The replay refuses: the hourly `calculate` would build
  on the replay's data, and its planners would plan work for the replay's nodes.
- **Days:** the last simulated hour must have ended.

The replay writes no settings row. It reads production's own settings and targets.

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
   every 5 minutes of the hour on production's settings and targets, then every planned pulse
   and load-wave run of the replay's nodes uploaded to an in-process node API (`POST
   /v1/results`, the real ingest). The runs are modelled (no real pulse data exists): 100
   requests, 0 to 3 timeouts, geo rows from the slice's enrichment of the country; a load-wave
   run also carries IP rows (the slice's IPs of the country), as its ingest requires.
3. **Browser.** BROWSER-MODEL-1's `browser_step` for the slices' networks.
4. **Release.** `cycle_steps.Steps.release`: the estimated hours that ended are released.
5. **Calculate.** `proxy-index calculate --as-of <hour end> --publish --incremental` in a child
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
  `calculate.timeout_seconds`; its process group is killed) or exits non-zero (an OOM kill
  included). The calculation is one transaction: killed or failed, nothing is published and no
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

Timed from the client side (owner, 2026-11-07: no dependence on `pg_stat_statements`):

- the ingest steps in-process through `measure.Tracer` (the hour's `ingest.statements`);
- the calculation child under `measure.py trace` (the hour's `calculate.statements`).

Each is the statements' wall time as `measure.Tracer` records it (calls, seconds, maximum).

## Telling replay rows from production's

- **Markers:** every run the replay stores has the summary key `scale_replay` (the replay's id):
  daily tests, pulses and browser runs. Its nodes are named as above.
- **Baseline** (`replay_baseline.py`, in the state at the start): the row count of every table in
  `public` and `site`, the highest value of every identity column, and the full key sets of
  providers, products, pool_stats, scope_stats, nodes, targets, dbip_releases, place_names,
  asn_names, asn_labels, enrichment_locations, enrichment_networks and cohorts.
- **After each day** (`days_counted`): the counts again, per table the growth since the baseline,
  the replay's rows (reachable from its runs by `run_key` and from its nodes by `node_id`,
  `test_batches` through their runs) and the other growth (`job_runs`, alerts, ...), reported as
  production's own.

## Outputs

`<out>` gets mode 700 and every file mode 600:

- `state.json`: the replay id, the go and every resume, the baseline, the nodes, per hour its
  steps' parts, start and end times of the ingest and the calculation, the calculation's
  attempts, its build id, the siteapi wait, the statements and the sizes, the day counts and the
  retry counts.
- `events.jsonl`: one line per retry.

REPLAY-REPORT-1 (#611) reads both for the report. The command prints the replay id and the
hours done.

## Wiping the replay (WIPE-TOOL-1)

`wipe_replay.py` removes exactly the replay's data from the database it ran in, before the launch
(LAUNCH-WIPE, #615). It reads the replay's `state.json`: its baseline, nodes and simulated days.

```sh
uv run python scripts/scale/wipe_replay.py --database proxy_reviews.toml --state <out>/state.json
uv run python scripts/scale/wipe_replay.py --database proxy_reviews.toml --state <out>/state.json \
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
  calculation_roles and batch_runs; the batches whose runs all belong to the replay's nodes, with
  their outages; the nodes (their token hash is in the row).
- The calculation's caches of the replay: the run memos (`calculated_run_ranges`), the rollups
  and memos of the simulated days, the rollup hours written after the baseline (`through_run`
  above its highest run key), with their geo hours and pulse buckets, the distinct and slot
  memos used since the baseline, and the load references chosen since the baseline for the
  simulated days. The calculation rewrites any of these it misses.
- Lookup rows new since the baseline (not among its keys, or above its highest identity) that no
  remaining row references except a cache: scope_stats, pool_stats, products, providers,
  ip_addresses, dbip_releases, enrichment_locations, enrichment_networks, asn_labels,
  place_names, asn_names, cohorts and score_reasons. The cache rows that reference them go
  first: product_ips, pool_ips, provider_overlap and outages, and an IP's ip_enrichment and
  calculated_ip_last_seen.
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
database, standing in for production: 2 days of 3 hours in order with the overlap (by
timestamps), the sizes, the timed statements, the baseline, the markers, the day counts, no
settings row and the nodes disabled at the end; a calculation that times out and fails, retried
with doubling pauses until it succeeds and flagged; an ingest failure that keeps the stored run;
a stopped run resumed from its state file (with a stand-in siteapi); and the refusals (a recent
`job_runs` row, a malformed go, unended days). `--hours-per-day` and `--pause` (hidden options)
shorten the days and the pauses for the tests.

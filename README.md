# Atlas

Atlas measures proxy networks and publishes what it finds. Census and pulse runs test each
network from many countries; every exit IP is enriched from a local IP database (country,
region, city and network operator); the calculation scores the networks on success, speed,
coverage and cost; and the snapshot API serves each published result to the Atlas website.
This repository holds the Python framework and CLI (`src/atlas/`) and the Go API (`api/`).

The framework is a standalone Python 3.12+ library and CLI for HTTP/SOCKS5 proxy measurements,
MIT licensed. A single run needs no database server or scheduler.

Open [the standalone HTML guide](docs/index.html) for setup, API examples,
measurement definitions, result contracts and the implemented PostgreSQL schema.

```sh
uv sync --locked --no-editable
uv run --no-editable atlas targets
# Set PROXY_URL privately in your environment first.
uv run --no-editable atlas run --target generic_http --requests 5000 --concurrency 100
uv run --no-editable atlas enrich results/RUN_ID
```

Shifter Residential: see [the adapter guide](docs/shifter-parameters.html).
Copy `credentials.toml.example` to `.secrets/credentials.toml`, replace the placeholders,
and set directory/file permissions to `700`/`600`. The private copy is excluded from
Git, packages and remote uploads. Then:

```sh
uv run --no-editable atlas run --provider-config configs/shifter.toml \
  --credentials-file .secrets/credentials.toml --target generic_http \
  --url http://ip-info.com/myip --requests 5000 --concurrency 100 --param country=us
```

Both targets support the same provider config; optional per-test parameters and
session policies are exposed through the CLI and `GrammarProxy`/`load_provider` Python API.
Each of the 18 supported providers has a public run config in `configs/<provider>.toml` and a
reviewed credential grammar in `src/atlas/providers/grammars/<provider>.toml`; see
[the provider guide](docs/providers.html). Parameters use the same names everywhere:
`country`, `region`, `city`, `zip`, `asn`, `session` and `ttl` (minutes).
A grammar's optional `[testing] protocol` sets how the network runs echo tests:
`https-only` runs plain-HTTP echo targets over HTTPS, `mixed` follows a gateway redirect from
HTTP to HTTPS and counts it in `redirects`; `atlas providers --protocol https-only` lists them.
Target URLs can be supplied with `--url` or `RunConfig(target_url=...)`.
In v0.2 custom transports must accept `execute(target, *, context=None)`.

All built-in HTTP/HTTPS tests use CPU-parallel worker processes by default.
`--processes 0` selects available cores; `--processes 4` selects four workers.
Concurrency and rate limits remain global (2,500 requests means 625 slots per worker
with four processes), with one exact census uniqueness set. This applies to normal
census/pulse runs and onboarding. See [the operating guide](docs/onboarding.html)
for CPU monitoring and cancellation behavior. A local CPU-only check is available:
`PYTHONPATH=src uv run python scripts/check-multicore.py` (no proxy traffic).

The locally supplied DB-IP MMDB belongs in `db-ip/` (or set `DBIP_MMDB`). It is
excluded from version control and distributions. It is separate from PostgreSQL:
MMDB provides optional offline enrichment; PostgreSQL persists history and cached lifetime metrics.
`atlas dbip update` keeps it current from the DB-IP CDN (see "Daily DB-IP update").

PostgreSQL: this project uses a dedicated local cluster on `127.0.0.1:55432`.
Run `uv run --no-editable python scripts/postgres_local.py start` after reboot.
CI creates a throwaway cluster with `init --ephemeral` (fsync, synchronous commit and full
page writes off); never use it for a cluster whose data must survive a crash.
Private database credentials are in `.secrets/database.toml`; the public template is
`database.toml.example`. Add `--database-file .secrets/database.toml` to `run` or
`enrich` for automatic ingestion, or use `atlas ingest results/RUN_ID`
with the same flag. Replaying an import does not double-count data. See the HTML
guide for initialization on a new machine and the schema.

Tests: `uv run --no-editable pytest`;
real database tests: `PG_TEST_ADMIN_FILE=.secrets/postgres-admin.toml uv run --no-editable pytest`;
scale benchmark: `uv run --no-editable python scripts/benchmark.py`.
Tier 1 is the CI check `tier1` (`.github/workflows/ci.yml`) on the exact head: every
test, database tests included, in parallel on one self-hosted CI server, for every
ready pull request, every merge-queue test and every owner dispatch. Tests
outside CI run on a disposable test server, never on a workstation. See the HTML guide.
CI time: every `tier1` and `api` run writes its 20 slowest tests and its test step's time
to the job summary and keeps them for 90 days as the artifact `ci-timing` (`api-timing`);
it warns when a test takes over 60 s or the step over 90 s, and fails when a test takes
90 s or more or the step over 240 s (`scripts/ci/timing.py`). The history across runs:
`python3 scripts/ci/timing_history.py [--workflow api] [--runs 30] [--event merge_group]
[--branch BRANCH]` (one `gh` call per run; neither workflow runs on pushes to `main`, so
`--event merge_group` shows what landed).

Schema v5 stores each IP once and references it from product membership, history and
versioned enrichment. Lifetime provider overlap is cached and deduplicated across products.
Test profiles (`pool_stats`, `scope_stats`), bucket counters and latency histograms
(`run_buckets`, as millisecond-gap/count arrays) are typed columns; only unknown fields use
`extra`. The views `pool_details` and `scope_details` and the function `run_bucket_counters()`
rebuild the original JSON, and every import verifies that the stored rows rebuild its summary.
See `docs/index.html#database` for keys, readable views and rebuild/backup details.

All 14 known enrichment fields use shared typed location/network records, including
connection/usage type, coordinates and organizations. `ip_enrichment_details` exposes them.
Only unknown future fields use `extra_attributes`. Numeric internal run keys reduce history
storage; external UUIDs and portable artifacts stay unchanged. `storage_read.read_summary()`
reconstructs full summaries without repeated bucket/histogram/configuration JSON.

Schema v6 adds the census/pulse tables: `runner_regions`, `countries` (daily UTC
`census_time`), `targets` (per-target pulse interval, requests and concurrency), `nodes`,
`test_batches`, `batch_runs` (live counters), `test_events`, `outages` (time a product was down
in a country, from a stalled census or a pulse `down` until the next healthy run there),
`job_runs`, `alerts` and `settings`. A census (`stop_rule` `unique_ratio`) that had successes and
then none for `stall_seconds` (setting `census.stall_seconds`, default 600) stops as `stalled`;
its summary carries `outage_started_at` (the last success) and its failures stay in the buckets. Runs gain typed outcome counters (`challenges`, `blocks`, `timeouts`,
`proxy_errors`, `bytes_up`, `bytes_down`) per bucket and an optional `geo.jsonl.gz` breakdown
stored in `run_geo_buckets` against the shared location/network ids. Artifacts without these
keys still import and rebuild unchanged. `atlas db-seed --database-file
.secrets/database.toml` upserts regions, countries and targets from `configs/regions.yaml`,
`configs/countries.json` and `configs/targets.toml`; it keeps census times, enabled flags and
target schedules once set (`--reset-times` restores the staggered default census slots;
`--only targets` upserts the targets alone and leaves regions and countries untouched).

Schema v7 (`migrate_v6_v7.sql`) adds the batch kind `load` (the load wave, see
below) and `test_batches.retry_of` (a census rerun after a provider outage names the census it
replaces); schema v8 (`migrate_v7_v8.sql`) adds `load_references`, the daily reference country of
each runner region; schema v9 (`migrate_v8_v9.sql`) moves place and ASN names into the shared
lists `place_names` and `asn_names`: `enrichment_locations` keeps a place id and coordinates,
`enrichment_networks` an ASN name id and the per-IP network attributes, every location and network
id is kept, and the views `enrichment_location_fields` and `enrichment_network_fields` give their
old columns (`ip_enrichment_fields` and `ip_enrichment_details` are unchanged). An import finds
each distinct location and network of its rows once, through the lists' unique keys (one index
entry each, never a read of a whole list, CALC-SCALE-1; `names.lookup()`); schema v10
(`migrate_v9_v10.sql`) adds `estimated_hours`; schema v11 (`migrate_v10_v11.sql`) adds the batch
kind `browser` (the browser pulse) and changes no row; schema v12 (`migrate_v11_v12.sql`) replaces
the `ip_observations` index `(ip_id, bucket_start)`, which no query used, with
`(bucket_start, ip_id)` for distinct-IP counts per time slice, and changes no row (building the
index blocks writes to `ip_observations`; dropping the old one then blocks reads and writes until
the migration commits, which follows at once); schema v13 (`migrate_v12_v13.sql`) adds
`test_event_buckets`, the live tape's event counts per 30-minute bucket, product and tape tab
(`good` counts `signal > 0`, `bad` `signal < 0`), kept by one statement-level `AFTER INSERT` trigger
on `test_events` per insert statement or `COPY` (every partition fires it), and fills it from the
stored events (the view `test_event_bucket_counts`) in the same transaction (creating the trigger blocks writes to `test_events` until
the migration commits); the API's `/v1/events/stats` reads it (`api/answers/events-stats.md`);
schema v14 (`migrate_v13_v14.sql`) adds `scope_ips`, each scope's IP membership set (as
`pool_ips` per pool), filled once from the stored observations in the migration's transaction:
ingest counts the IPs new to a scope (its `unique_ips`) by inserting the batch's pairs into it,
one key probe each, instead of scanning `ip_observations`, so the count no longer grows with the
history; schema v15 (`migrate_v14_v15.sql`, DB-EXPLAIN-1) adds three partial indexes to
`test_events` for the API's rare-value reads, which otherwise walk the whole tape:
`test_events_notable (at DESC, event_id DESC)` over the non-routine rows (the notable read, and the
`kinds` and `category` filters that admit no routine row, whose query repeats the predicate), and
`test_events_country (country_code, at DESC)` and `test_events_target (target_id, at DESC)` over the
rows that name one. No row changes; each index is built on every monthly partition and attached,
and partitions made later get it from the parent. The build took 25 ms on the staging week (17k events; plus 92 ms for `ANALYZE test_events`) and 0.57 s on a synthetic year of 1M events (`tests/events_index_measure.py`);
schema v16 (`migrate_v15_v16.sql`, COUNTRY-MIN-IPS-1, #1137) adds `country_verdicts`, whether a
country counts for a network: one row per daily test (its network, requested country, start
`decided_at`, its own `unique_ips` and `counts`, true from 10 IPs), written by the test's import
in the same transaction (`atlas/verdicts.py`) and filled in the migration from every stored daily
test; and `outages.end_reason` (NULL, or `too_few_ips` for an outage closed because its country
stopped counting). A verdict holds until the next daily test of the same network and country.
Schema v17 (`migrate_v16_v17.sql`, PULSE-STATE-2, #1169) adds three nullable `nodes` columns
each heartbeat fills with the node's host pressure (`atlas/node_health.py`): `io_pressure` (PSI
`some avg10`, %), `mem_available_mb` and `swap_pages` (swapped in and out since the previous
heartbeat); NULL from a node too old to send them. Schema v18 (`migrate_v17_v18.sql`, PULSE-STATE-2)
adds `batch_runs.start_at`, a run's own start when it differs from its batch's: a stalled pulse
node's run moved to another node (`orchestrator/pulse_moves.py`) gets its own later start while
its batch, shared by the region's other networks, keeps its start; the claim, the reaper and the
pulse evaluator read each run by its own start (NULL: its batch's). Schema v19
(`migrate_v18_v19.sql`, LOWAVAIL-1, #1181) makes `country_verdicts.counts` a stored column set by
the import from the verdicts before it (two days in a row: a normally big country also counts on
its first test under 10) and sets every stored verdict again the same way; it forgets the
calculation's verdict setting so the next build counts its memos again. `db-init` verifies v19
and refuses other schemas.
`atlas db-migrate --database-file .secrets/database.toml` upgrades an unmodified v5 to v18
database, or an unmodified v4 database (and calculated v1 layer) through v5 to v19, in
place, in one transaction that compares every rebuilt summary, scope, pool, bucket, location,
network and per-IP enrichment (from v6 every test batch, and from v4 every cohort, breakdown and
score) with its original before committing. Run it on every
database (production and staging) before starting the new orchestrator. Locally, run
`PYTHONPATH=src .venv/bin/python scripts/migrate_database.py`, which takes a pg_dump first.
The local `scripts/rebuild_database.py` backs up, stages, verifies and swaps a rebuild from
matching saved artifacts, retaining the previous database (it does not carry calculated builds).
See the HTML guide for its scope and prerequisites. Pool membership is
shared across performance settings; product lifetime summaries and detailed history remain
separate. Measurements: `artifacts/storage-optimization.html`.

## Calculated website data (v0.8)

The existing HTML guide now includes the calculated schema, API and command workflow.
Collection still runs independently; calculation requires PostgreSQL and never sends proxy traffic.

```sh
atlas calculate-init --database-file .secrets/database.toml
atlas run-role RUN_UUID --role pool_sampling --database-file .secrets/database.toml
atlas calculate --methodology configs/methodology.toml --publish --database-file .secrets/database.toml
atlas snapshot-export ../src/data/dataset.json --database-file .secrets/database.toml
atlas serve --database-file .secrets/database.toml
```

From the website root, `npm run build` exports the measured dataset and builds the site;
`astro dev --background` serves it locally. `MEASUREMENTS_URL` defaults to `http://127.0.0.1:8081`.
Refreshes use the same-origin `/api/measurements/v1/snapshots/current` endpoint.
`atlas serve` never sends a whole payload (a daily build's runs to hundreds of MB):
`/v1/snapshots/{current|id}` is the snapshot's summary (ids, times, readiness, methodology,
periods) with one page of its product-group index, and every resource (`products`, `series`,
`breakdowns`, `scores`, `fleet`, `runs`, ...) is paged with `?page=N` (`page`, `pages`, `total`
in the answer). `fleet` holds one entry per cohort and window, plus one per window with cohort
`*`: every product group together, each exit IP counted once however many networks or cohorts
saw it, success and light-load latency over all of them, and a `series` (5-minute buckets for
24h, days otherwise) with distinct IPs per bucket. Each answer is at most 200 KB (`calculated/answers.py`, enforced by
`tests/test_calculated_answers.py`) and is kept in memory per build, so a request reads the
database at most once per build plus a current-build check every 10 s.
The development API helper is `python scripts/measurements_local.py start|status|stop`.
Calculated schema v2 keeps cohorts, breakdown labels (`places`, `asn_labels`) and score
components/reasons (`calculated_score_components`, `calculated_score_reasons`, `score_reasons`)
as typed rows; `cohort_criteria`, `calculated_breakdown_details` and `calculated_score_details`
rebuild the payload fragments. Calculated schema v7 (`calculated/migrate_v6_v7.sql`) moves the
breakdown names into the main schema's shared lists: breakdown places are `place_names` rows
(the same ids as enrichment locations; `places` is gone) and an `asn_labels` row is an
`asn_names` id plus a usage type, shown with its ASN and name by the view `asn_label_names`.
`calculated_breakdown_details` and `current_breakdowns` keep their columns and values.
Calculated schema v9 (`calculated/migrate_v8_v9.sql`, contract K2) stores each target-free
window's full breakdowns as a list, `calculated_lists` (entries, since-independent pool figures,
content hash, signature) and `calculated_list_rows`, which the window names in
`calculated_windows.list_id`; `calculated_breakdowns` and its per-build partitions are migrated
(one list per window, signature NULL) and dropped. Lists outlive their build and are shared by
later builds: a daily group's window has list signature v1 (`lists.signatures`, the digest of the
implementation, methodology, window, country names and each run of the group in the window with
its summary hash, enrichment release and artifact hash, clipped bucket span and light-load flag),
and a build with a stored list's signature reuses it without writing rows. The list is still
computed and its content hash compared; a mismatch fails the calculation ("list signature is
incomplete") and the previous build keeps serving. The mismatch is permanent for that signature:
every later calculation with the same window inputs fails again until the runs leave the window
(up to 30 days for 30d) or `IMPLEMENTATION_ID` changes. The remedy is a code fix of the signature
(a release changes `IMPLEMENTATION_ID`); until then the calculation stays stopped and the current
build keeps serving. Census-pulse lists keep signature NULL; an incremental build's census-pulse
window whose list would be the one of its group's previous window (the same IP inputs, geo rows,
list figures and first-seen bins, as the 24h, 7d and 30d ones while the data is younger than a
day) names that list instead of writing the same rows again (CALC-SCALE-1). After
`discard` and `expire` the lists no window references are deleted (`store.collect`).
`calculated_list_details` shows a list's rows by `list_id`; the two breakdown views read the lists
and keep their columns. Snapshot payloads are kept for the current build, published builds
and the two newest unpublished drafts; older draft payloads are pruned on calculate/publish.
Calculated schema v10 (`calculated/migrate_v9_v10.sql`, contract K6) adds the incremental
calculation's memo tables, empty until CALC-INC-2 and CALC-FLEET-1 fill them. `atlas
calculate --incremental` (`calculated/incremental.py`) reads run metadata, buckets and list
signatures first, reuses a stored list for every window whose signature matches, and counts
every other window in the database (`calculated/sqlwindow.py`): its label combinations and geo
rows give its list, written with one COPY as soon as it is counted, so no observation row comes
back to Python for any window and a missed window keeps only its counts in memory. Such a list
also stores its window's IPs per 5-minute bin of their product's first sighting
(`figures.first_seen`, outside the content hash, for CALC-INC-2). The values
that move with the cutoff are counted in the database (`calculated/hourly.py`, `fleetsql.py`,
`rollups.py`), which still reads the window's observations for them until CALC-INC-2 and
CALC-FLEET-1 keep them in the memo tables. `calculate` is incremental by default (also
`--incremental`; the orchestrator's hourly job passes it explicitly); `calculate --full` is a
recount: the incremental calculation reusing no stored list, every window counted again in the
database and every list verified, so its memory follows the hourly build's (CALC-SCALE-1).
`calculate --oracle` (the Python `calculate()`'s default) is the full calculation, which reads and
aggregates every observation in Python as before (~5.5 KB a row: 56 GB at ~10 M rows) and is the
oracle the incremental tests compare with, byte for byte; it is for tests and small data. The
build keeps each bucket row's latency histogram in two integer arrays (`calculated/bins.py`), the
runs of a scope share one scope dict, and the input fingerprint hashes the window's runs, bucket
rows and observations one at a time (`calculated/fingerprints.py`). Measured on scale-generator data (6 networks × 60
countries × 8 days): at 0.42 M observations incremental took 221 s against full's 187 s (more
database time) with 2.9 GB peak against 5.0 GB; at 1.4 M full mode ran out of 8 GB, incremental
took 362 s and 3.7 GB.
The incremental calculation counts across groups (`calculated/fleetsql.py`, CALC-FLEET-1)
from memos: each 5-minute bucket's runs digest and distinct IPs (`calculated_bucket_ips`, the
`*` 24h series) and each IP's newest bucket (`calculated_ip_last_seen`), folded per bucket
whose runs changed, so a late run is counted too. The `*` window totals are the IPs last seen
in the window when no folded bucket is at or after the cutoff (the hourly build); an older
cutoff counts its window directly and changes no memo. The 7d/30d `*` series points are one
distinct count per daily slice (at most 4 at a time, in the calculation's snapshot), kept in
`calculated_distinct` under the slice's bounds and its buckets' run sets: a slice comes back a day
later one point earlier, so an hourly build counts only its newest slice and any slice a run
joined or left (CALC-SCALE-1). A one-group
cohort takes its group's pool count. In the hourly build larger cohorts and the per-kind window
totals are sums over the IP state (`calculated/ipstate.py`): each IP's newest sighting per scope
(`calculated_scope_ip_last_seen`, folded per bucket whose runs changed) and each window's IPs
counted by the scopes that saw them (`calculated_ip_vectors`), moved to the new cutoff through
the IPs of the folded buckets and those whose sighting left a window, so an hour reads only what
changed. An older cutoff counts a larger cohort again only when a member's runs in the window
changed (`calculated_distinct`). IPs, sightings and buckets older than 31 days are collected.
Since CALC-INC-2 the daily pools, new IPs, series IP counts and the estimates' distinct IPs come
from the memos (`calculated/hourly.py`): a run's figures per range of its buckets
(`calculated_run_ranges`: every single bucket when the run is first recorded, the whole run or a
24-hour edge's prefix or suffix the first time a slice needs it), the figures of a slice or point
holding two or more runs (`calculated_slot_figures`) and the distinct IPs of each estimate run set
(`calculated_distinct`), so an hour without a new test reads no observation for them. Every build
mode records runs the same way (full mode through `network.attach()`), and each run recorded for
the first time is checked by the late-run rule: when it saw an IP first that runs already
counted saw too, it lowered that IP's first sighting, so the ranges of its product's runs with a
bucket since then are counted again. Runs ingested after the newest recorded one are checked
whatever their window, so a run backfilled more than 30 days back is too; a run whose ingest
commits after a build that already recorded a newer run, and that no window holds, is not.
This module's memos unused for 2 days and the ranges of runs started over 31 days ago are deleted
once per build transaction.
Calculated schema v11 (`calculated/migrate_v10_v11.sql`, CALC-PULSE-1a) adds per-hour rollups of
`run_geo_buckets` (`calculated_geo_hours`, `calculated_geo_rollups`): the incremental calculation
reads its census-pulse geo rows (geo totals and light-load histograms) from them instead of
scanning the 30-day window (`calculated/rollups.py`); the full calculation keeps the 30-day query
as the oracle. Each rollup row sums one hour per product group, location, network, fallback country
and target. A build writes the hours it reads that have no rollup, and rewrites an hour holding a
run newer than its rollup (a late run) or a run outside a batch that a batch now owns. An hour a
window edge falls inside (a cutoff off the hour) is read from `run_geo_buckets`, and pulses that
ran during a census are subtracted from the rollups when read, so the rows equal the full mode's at
every cutoff. Rollups of hours over a day older than the window read are deleted. The first
incremental build after the migration fills the 30 days once, at about the cost of one full read.
Calculated schema v12 (`calculated/migrate_v11_v12.sql`, CALC-PULSE-1b) adds per-hour copies of the
batch runs' `run_buckets` (`calculated_pulse_buckets`, `calculated_pulse_rollups`): the incremental
calculation reads the request buckets of every census, pulse, load and browser run from them in one
query, instead of one scan of the 30-day window per product. A copy keeps every column but the
status codes, which nothing reads. Hours are copied, rewritten and collected as the geo rollups are;
`calculated_pulse_rollups.loose` records the runs outside a batch an hour held, so a run linked to a
batch since rewrites its hour. The census overlaps, pulse slots and tested spans come from one read
of the batch runs instead of three.
Calculated schema v13 (`calculated/migrate_v12_v13.sql`, LISTS-WRITE-1) drops the foreign key
of `calculated_list_rows.list_id`, whose per-row check made a first build's list writing take
hours; no row is rewritten. A product group's new lists are written with one INSERT, one label
lookup and one COPY (`calculated/list_write.py`).
Calculated schema v3 (`calculated/migrate_v2_v3.sql`, applied by `db-migrate`) adds request
success, challenges, blocks and p50/p90/p99 to the breakdowns, which now cover country, region,
city, ASN and country × ASN from `run_geo_buckets`, and challenge rate, block rate, uptime and
wire bytes to every window. Census, pulse and load runs (those linked to a test batch) of a
product form one `census_pulse` cohort. **Every request counts for every metric**: census, pulse
and load runs alike feed success, blocks, timeouts, exit IPs, the geography/ASN breakdowns and
bytes; run roles (`run-role`, default `pool_sampling` for census, `monitor` for pulse, `benchmark`
for load) are display labels only, and `minimum_requests` counts every request of a required
target. Uptime is outage time: 1 − downtime / (window × countries tested), with each country's
outages merged and clipped to the window; `uptime.batches/up` keep the pulse-slot counts for
reference (`[uptime]`). For these cohorts `minimum_coverage` (0.90) applies to pulse slots, not
5-minute buckets: the share of the product's `done` pulse runs with at least one request. A run
is only ever `failed` by our side (no node, never started, went silent, node shut down or erred,
upload rejected), so failed runs and operator-skipped/killed batches are not slots and never
count against the provider. **A provider that is down is data, not a gap**: a done pulse whose
requests all failed fills its slot, votes 0 on success, lowers uptime (and the pulse-slot
counts), opens an outage on the tape, and never makes the score N/A through coverage. A census
that ended because the provider was down (`no_success` or `stalled`) is rerun for that product
alone, once per UTC day per country and target, as soon as one of its pulses there gets through
(`census_retry` on the tape, `test_batches.retry_of`); a census our side failed is not.

**Time-weighted metrics.** A window is split into time slots: the cohort's pulse interval (the
shortest `pulse_interval_minutes` of the pulse-enabled targets) made a whole multiple of the
5-minute stored bucket, lcm(interval, 5): 30 minutes in production, 10 in staging, so no census
request is placed in a slot it did not run in. Every slot with observed requests weighs the
same: `metrics.success_rate` is the mean of the slot success rates, successes ÷ requests observed
in the slot (`request_success_rate` keeps successes ÷ requests). **Latency is measured on one
target only**, `latency_target` (default `generic_http`, the daily test's and census target):
the latency of normal requests and under load, the speed score and breakdown latency all come
from it.
Other targets count for success, blocks, timeouts, IPs, authenticity, DC ASNs, bytes and uptime;
their `targets.<id>.metrics.latency_ms` and `latency_under_load_ms` hold nulls, with
`latency_reason`. It has two latencies, each merging the slots' histograms after normalising
each to weight 1 and reading p50/p90/p99 from the merged distribution; a slot with fewer than
`[slots] min_latency_successes` (30) successes does not vote. Percentiles are never averaged.
**Under load** (`metrics.latency_under_load_ms`, the headline latency and the speed score): the
daily country tests (runs outside a batch, concurrency 2000, 500 for two networks), census and
load runs. **Normal requests** (`metrics.latency_ms`, light load): the pulses (concurrency 25).

**Census overlap.** A pulse or load run of a product that ran while a census of the same product
was running (any country: account and node are shared) is marked `during_census`, derived at
calculation time from the stored run start and finish times. It still fills its slot and counts
for uptime, outages, down slots and every request metric, but it is left out of both latencies
(breakdowns included), of the time-slot success (a slot votes with its other requests;
one left with only such pulses votes 0 if every request in it failed, else does not vote:
`time_slots.skipped_during_census`). Each window publishes `census_overlap`: `pulses` and
`targets.<id>` with `clean` and `during_census` (`runs`, `requests`, `successes`,
`request_success_rate`, `success_rate`, `latency_ms`), `load` (clean and during-census load
runs), `census_batches` and `basis`; `metrics.latency_reason` says why light-load latency is
missing.

**Load waves.** Where `load.enabled` is on (staging; production runs none), each product also
runs one wave of `census_concurrency` requests in every pulse slot of the census target in
**every runner region's reference country**: a batch of kind `load` with the pulse's
`scheduled_for`, started `load.delay_seconds` (60) after the pulses. It sends only the country,
like every scheduled test. The orchestrator picks each region's reference once per UTC day, with
the day's first pulse plan, and stores it in `load_references` (schema v8): the fastest enabled
country tested from the region over the trailing `load.reference_days` (7) — lowest light-load
p50 of its census-target pulses that did not run during a census, every product weighing the
same, at least `load.reference_min_successes` (300) successes, ties by country code
(`fastest`); an override (`load.reference_overrides`, `override`); without enough history the
region's default (`load.reference_defaults`: nyc3 US, fra1 DE, lon1 GB, ams3 NL, tor1 CA, sgp1
SG, blr1 IN, syd1 AU; staging fra1 RO; `default`); else its first enabled country
(`first_enabled`). A load run's requests count under load like a daily test's (LATENCY-LABELS-1:
no load factor scales the pulses any more); they count for every request metric but not for
slot coverage or uptime.
Snapshot payloads are `schema_version` 2: per product `census` (latest per country), `outages`
(latest 50 in 30 days) and `pricing`, per window `breakdowns`, `uptime` (`value`, `downtime_s`,
`countries`, `basis: "outage time"`, `batches`, `up`) and `latency` (`target`, `regions[]`:
`region`, `reference` of its newest load run, `chosen` (the daily `load_references` rows in the
window: `day`, `country`, `basis`, `p50_ms`, `successes`, `candidates`); `countries[]`: per
requested country its pulses' `region`, `latency_ms` (normal requests), `latency_under_load_ms`,
`slots` and `reason`), plus top-level `events`
(latest 200) and `traffic` (`by_test` census, pulse and load). Window and target `metrics` carry
`success_rate` (time-weighted), `request_success_rate`, `latency_ms` (normal requests: pulses),
`latency_under_load_ms` (daily tests, census and load runs), `under_load` (`slots` voting and
the `reason` when there is no value) and
`time_slots`; `benchmark` is kept as a copy of `metrics` for older readers.
Each window's `series[].uptime` publishes outage-time availability at five-minute resolution
for 24h and daily resolution for 7d/30d. Countries count in buckets intersecting a started
census/pulse run or recorded outage; overlapping outages are unioned and clipped per country.
Buckets without monitoring evidence are null. Calculated schema v4 persists this fraction in
`calculated_series.uptime`; `db-migrate` adds the nullable column while preserving older history.
Coverage evidence uses `breakdowns.city_asn`: actual location/network intersections with
distinct IP counts, ASN name/number, DB-IP usage type and light-load p50/p90/p99. Unclassified
exits remain in the intersection with null network fields; no country-ASN join or advertised
product type fills a gap. Calculated schema v5 stores these as dimension `y`, with nullable
ASN labels; `db-migrate` preserves existing calculated rows and published snapshots.
The published snapshot keeps only the top rows of each long list: every `breakdowns` dimension
(by completed requests) and the pool's `geo`, `countries_breakdown` and `asns_breakdown` (by IPs),
N per list from `calculated/policy.py` `PAYLOAD` or a `[payload]` methodology table. Each window
adds `breakdown_entries` and each pool `list_entries`, the full row count of every list ("25 of
61,497"). Counts, shares and metrics are computed from the full data first, and
`calculated_list_rows` keeps every row; the site API reads the full city x ASN and ASN lists
from there. The API's `breakdowns` resource returns the counts as `dimension_entries`.
**Which countries count** (COUNTRY-MIN-IPS-1, the owner's decision of 2026-11-15, #1137; the
methodology's `country_verdicts = true`): a network's country counts only while its latest
**daily test** there found at least 10 unique IPs (`country_verdicts`, schema v16: the test's own
`unique_ips`, the distinct exit IPs of its successful answers in the requested country), or that
test is its first under 10 and the country was normally big (two days in a row, LOWAVAIL-1,
schema v19: its previous test found 10 or more and its tests of the 7 days before reached 50), so
a single bad day never hides a big country's failures. The verdict holds from that test's start until the next daily test of the network and country
starts; before a country's first daily test it does not count. Everything measured for the
network in a country while it does not count (its daily tests, pulses, browser runs of that
country, latency, success, IPs, breakdowns, coverage, pulse slots, estimates, scores, the `*`
totals) is left out of every figure, outages are cut to the time it counted and uptime counts
only that time; a run without any country (no batch country, no requested one) never counts.
Nothing is deleted, and the countries keep being tested so they can count again.
The calculation decides each run once, in SQL (`atlas/verdicts.py`: the counting spans cut by UTC
day, one join). detect, the tape replay and the pulse evaluator judge only counted runs (no event
and no outage for the rest), the network-wide outage opens only in counting countries, and each
evaluator tick closes the open outages of a country that does not count at its verdict, with
`outages.end_reason = 'too_few_ips'`. The API lists each requested country's status
(`requestedCountryStatus`: `counts`, `too_few_ips` shown as "low availability", `no_exits` shown
as "no exits" when no IP was found at all, `no_daily_test`; LOWAVAIL-1, #1181).
**Low availability** (LOWAVAIL-1, the owner's decision of 2026-11-15): a country where a network
has almost no exits is never an error. Its daily test still runs every day, gently: it starts
at half of the IPs the country's last test found, 5 to 2,000 at once, and grows during the run
to half of the IPs seen so far; a test that finds no IP stops after 300 answers
(`daily/pace.py`, `runner.py`). The hourly pulses skip it (`daily/expected.py`: its latest
daily test found under 10 IPs and its failures are the network's own "no exit" answers, its
grammar's `no_inventory_statuses`, or hardly any, or timeouts in a country that is not
normally big; `country_status.py`), and the hour report lists it on one "expected" line. Three
safeguards keep a real problem visible: a normally big country needs two daily tests in a row
under 10, a network whose normally big countries mostly fail in the same hour is a network
problem, and login errors or refused connections are never "no exit". The data behind the
"no exit" answers is in `docs/reports/lowavail-answers.md`.
`detect.detect(conn, now)` writes the Live tape events (`[events]` thresholds, not part of the
methodology id). **When a pulse outage opens and closes** (PULSE-OUTAGE-CLOSE-1): an outage is a
product with no success at all in a country that counts for it (COUNTRY-MIN-IPS-1). A pulse `down` (a done run with no success while
the baseline was above `down_baseline`) is the target's incident on the tape; it opens the
product's outage in the country, from its last healthy pulse there, only when no other target
of that hour got through (one still running opens it with its own `down`), so HTTP working
while HTTPS fails is a `down` of the HTTPS series only. The outage ends with the product's
first later pulse there that gets through on any target (any success, even below the
baseline), at that pulse's start, with a `recovered` event; a pulse still below the baseline is
then judged afresh (`success_drop`). The pulse evaluator also closes, at each tick, every
per-country pulse outage whose product got through in the country after its last hour without
any success, at the start of the first pulse through, never at the evaluation time
(`orchestrator/pulse_stale.py`; a `recovered` event, `closed_by: "pulse_evaluate"`, for each
target that got through and is still `down` on the tape). A network-wide outage (`pulse_down`)
closes only on a healthy 5-minute window: while the network is not pulsed at all it stays open.
**Our proxy login refused** (PROXY-AUTH-ALERT-1, `proxy_login.py`): a run whose requests were
mostly (at least 50%) refused at login (`errors.proxy_auth`, the gateway's HTTP 407) measured
nothing. A 5-minute window in which at least half of a network's pulse requests, and of each of
at least 2 regions', were refused is not measured (no `pulse_down`, no recovery, open outages
untouched) and raises the major alert `proxy_login_refused`, one per network and hour, posted to
Slack, with the refused share, the regions and the first refused window (`since`); the network's
next measured window that is not refused acknowledges it (`orchestrator/pulse_login.py`). detect
judges no pulse run mostly refused (no `down`, no `success_drop`) and leaves it out of later
baselines, and a daily test mostly refused is not imported (its cell fails by our side; the
country keeps its previous verdict and estimates). `orchestrator/events.py` also writes `daily_test_started`, `daily_test_finished`
and `estimate_hour_released` (category `census`), `pulse_down` and `pulse_recovered` (`pulse`) and
`latency_rise` (`incident`); each helper documents its `data` fields. The API also serves live `GET /v1/events?product=&kind=&category=&since=&limit=`
and `GET /v1/traffic?test=&provider=&country=&region=&from=&to=`, and
`atlas report traffic --test census|pulse --database-file ...` prints GB per provider.
TOML is data-only and versioned by SHA-256. The balanced profile uses `[weights]`;
alternative profiles use their own weights. No overall score is emitted for missing required inputs.
The current run is explicitly a pool sample. Scoring weights remain a draft.

### Scores: ratio to the leader

Every sub-score compares a network with the best network of the same kind (`kind` in
`configs/methodology.toml`; without one, the product name, e.g. `residential`) in the same
window (24h, 7d or 30d). Higher-is-better values score `100 × yours ÷ best`; lower-is-better
values (speed: p50 latency under load; value: price per GB) are flipped to `100 × best ÷ yours`. Scores are clamped to 0–100,
so the leader of each component scores 100. Leaders are found again on every calculation; nothing
is stored between calculations. A network alone in its kind is its own leader and scores 100 on
every ready component. Only ready, freshly measured components set the best: incomplete, retained,
expired or historical scores never do.

| Component | Raw value | Better |
| --- | --- | --- |
| reliability | time-weighted success rate of every request, per target (blocks, challenges, timeouts are failures inside it) | higher |
| speed | p50 latency under load of the latency target (`generic_http`) | lower |
| uptime | outage-time uptime % (evidence as fallback) | higher |
| coverage | unique IPs per day | higher |
| diversity | countries, regions, cities, ASNs per day, mixed 0.30/0.20/0.25/0.25 (`[peer.diversity]`) | higher |
| dcAsns | clean share: 1 − share of exit IPs whose DB-IP usage type is in `hosting_usage_types` (datacenter products keep 100) | higher |
| authenticity | 100 × share of exit IPs whose DB-IP country is the country the requests asked for (`pool.geo_accuracy`) | higher |
| value | lowest list price per GB of the pool tested (`price_per_gb_lowest`) | lower |

Shares are per unique exit IP over every request in the window, and IPs without the DB-IP field
(country or usage type) are left out of numerator and denominator, so incomplete enrichment never
blocks a component (`pool.enrichment_coverage` and `classification_coverage` stay published).
Value is 100 × cheapest of the kind ÷ yours. List prices are effective-dated `metric_evidence`
(`price_per_gb_lowest` scores; `price_per_gb_entry` is the smallest plan's rate, published in
`pricing`) for the pool actually tested, naming the product by provider and product, with cohort
`"*"` (every cohort of the product). The Shifter Residential price list (Full Geo pool, observed
2026-10-03: $0.75/GB lowest, $1.00/GB entry) is in `configs/pricing/shifter-residential.json`;
load it with `atlas metric-evidence configs/pricing/shifter-residential.json
--database-file .secrets/database.toml` once the product exists. There is no blocklist term and
no claim-gap input; a block is the target refusing us (403, 429, 451, a block page), proxy-side
failures are proxy errors.

Reliability is scored per target against that target's leader and averaged over the required
targets (`[benchmark_weights]`); speed is the latency target's alone. A target score combines
its ratios 80/20 (`[target_weights]`); a target without latency scores on reliability.
Without a load test in the window, speed (and so the composite) has no value; the component's
reason says why.
Pool counts are the average of daily (24-hour) counts over the window's days with observations,
so a network measured for three days compares fairly with one measured for all 30; the window
totals stay in `pool` for display and the averages are in `pool.daily` (`days`, `unique_ips`,
`countries`, `regions`, `cities`, `asns`). The composite is the weighted mean of the eight
ratios with the profile's weights (balanced 22/14/10/12/8/10/10/14) and maps to a grade.

Each component's `inputs` makes its score reproducible: `{"value", "best", "peers"}` (the raw
value, the leader's raw value and the number of ready peers including itself); diversity has
`{"countries": {"value", "best"}, "regions": …, "cities": …, "asns": …, "peers"}`; product
reliability and speed have `{"targets": {"<target>": {"value", "best"}}, "peers"}`. A component
without a ready peer is compared with itself.

## Hourly limits and timings (INGEST-SCALE-1)

The hourly ingest and the calculation size themselves to the machine they run on
(`src/atlas/sizing.py`). Each limit is a setting in the `settings` table; left at its default
(`null`) it is derived from the machine's CPUs and available memory:

| Setting | Default | Small machine | Manager (48 CPUs, 94 GB) |
|---|---|---|---|
| `ingest.processes` | max(min(7, CPUs - 1), CPUs / 4) | at most 7 | 12 |
| `ingest.connections` | max(25, 3 x processes) | 25 | 36 |
| `ingest.memory_budget` | max(4 GiB, available / 8) | 4 GiB | about 11 GiB |
| `ingest.batch` | 20 (fixed) | 20 | 20 |
| `calculate.parallel_reads` | max(8, CPUs / 4), settable up to max(16, CPUs / 2) | 8 | 12 |
| `calculate.heavy_parallel` | max(2, parallel_reads / 4) | 2 | 3 |
| `calculate.workers` | 1 (fixed default, 1 to 32) | 1 | 1 |
| calculation scope `MemoryMax` | `ATLAS_CALCULATE_MEMORY_MAX=auto`: max(8G, available / 8) | 8G | 11G |

`calculate.workers` above 1 measures an incremental build's product groups in that many forked
worker processes (`calculated/parallel_measure.py`, CALC-SCALE-1): each imports the build's
snapshot on a read-only connection of its own for its products' geo rows and missed windows, and
returns its groups and their lists; the calculation writes them in the order one process would
(the same list ids) and stays the only writer, so the build is the same. The workers take the
place of the read helpers for the missed windows (the prefetcher is not started), so the database
sees at most `calculate.workers` of the build's reads at once.

A small machine (up to 31 CPUs and 32 GiB available) keeps the values used before. On the manager
each limit takes about a quarter of the CPUs or an eighth of the memory, so the APIs (two
blue/green colours per environment), Caddy, the orchestrator and the other environment's jobs keep
the rest. `calculate.work_mem`, `heavy_work_mem` and `max_parallel_memory` are the database
server's memory, not the manager's, and stay fixed. Every set of connections opened at once is
lowered to what the database server has free beyond `database.reserve` (20, or
`database.reserve_share` 0.25 of `max_connections`) and, when the login's role has a connection
limit (`rolconnlimit`), to what that limit leaves beyond `database.role_reserve` (10, for the
orchestrator's pool and the other jobs of the same login); the lowered number is printed as a
warning. Change a limit in the `settings` table (as for `dbip.dir` below:
`INSERT INTO settings(key,value) VALUES ('ingest.connections','30') ON CONFLICT(key) DO UPDATE
SET value=excluded.value,updated_at=now();`); `null` (or no row) is the machine's default.
`daily ingest --batch/--connections/--processes` and `calculate --parallel-reads` override them
for one run.

### Reading the hourly timings

Each stage prints one line, `timing: <part> <stage> <seconds> s [key=value ...]`:

- The hour (`daily hour`, its own transient unit): `hour run` (the test), then the ingest: the
  line `ingest: batch …, N connections, P processes, memory budget …` with the sizes used, then
  `ingest prepare` (verify, convert, spool, enrich), one `ingest group k/n` per batch (with its
  `runs`, `networks`, `retries` and `failed`), `ingest store`, `ingest events`, `ingest audits` and
  `ingest release`. Read them with `journalctl --user -u 'atlas-hourly-*' | grep timing:` on the
  Canada server (`atlas-staging-hourly-*` for staging; `journalctl -u 'atlas-daily-*'` on
  the old manager).
- The calculation (`atlas calculate`, a child of the orchestrator): `calculate rollups`,
  `calculate read`, `calculate build`, `calculate store`, `calculate prune`, and inside the
  build `calculate ip_state` (the hourly build's IP state, `buckets=` folded, `moved=0` when its
  counts were counted again from the sightings: CALC-SCALE-1) and `calculate workers` (each pool
  of calculate.workers: `step=measured` or `summarized`, its workers' largest and total private
  memory, `private_mb=` and `private_total_mb=`, the pages a forked worker copied from the
  build). The orchestrator logs
  them after each calculation: `journalctl --user -u atlas-orchestrator | grep timing:`
  (`atlas-staging-orchestrator` for staging; `-u atlas-orchestrator` on the old manager).

## Staging

`configs/staging.toml` is a small, fast profile for trying the census/pulse loop end to end:
RO and BG only, a census every 4 minutes (setting `census.every_minutes`, epoch-aligned slots;
unset means the daily `countries.census_time`), pulses every 2 minutes with the load wave in
fra1's reference country (default RO until a week of history picks the fastest; `[load]
reference_defaults`, setting `load.reference_defaults`), HTML reports in
`reports/staging/`, a token for the Shifter Residential node in fra1, and the staging
methodology `configs/staging-methodology.toml`: it `extends` `methodology.toml` and only sets
`minimum_requests = 1000` per target (production stays 4,000). A methodology file's `extends`
loads the named file (relative to it) first and merges its own settings over it.

```sh
atlas staging init --config configs/staging.toml          # --clean drops and recreates
atlas db-migrate --database-file .secrets/staging-database.toml   # an existing v6 one
atlas orchestrator --dev --database-file .secrets/staging-database.toml \
  --methodology configs/staging-methodology.toml
atlas calculate --database-file .secrets/staging-database.toml \
  --methodology configs/staging-methodology.toml                     # no --publish: a draft
atlas node --orchestrator http://127.0.0.1:8080 \
  --token-file .secrets/staging-tokens/shifter-residential-fra1.token \
  --provider shifter --product residential --region fra1 \
  --provider-config configs/shifter.toml --credentials-file .secrets/credentials.toml
```

`staging init` takes host, port, user and password from `.secrets/database.toml` and creates the
database (owned by that user) with `.secrets/postgres-admin.toml`; it refuses a name equal to
`database.toml`'s dbname or `proxy_reviews`. It writes `.secrets/staging-database.toml` and the
node tokens (mode 600), runs db-init and db-seed, disables every country outside `[scope]`, and
prints a JSON summary with the exact orchestrator (with `--methodology`), calculate and node
commands (`--secrets-dir` points it elsewhere). Re-running it rotates the node tokens.

The admin API (`/admin/api`) has its own listener, `--admin-host` (default `127.0.0.1`) and
`--admin-port` (default the orchestrator's port + 3: 8083 here); the node ports answer 404 for it.
The planners can be switched off without a restart (both when neither flag is given; also
`POST /admin/api/schedule/scheduler {"census": false}`); a planner that is off records its job
as `skipped`:

```sh
atlas scheduler off|on|status [--census|--pulse] --database-file .secrets/staging-database.toml
```

With the `reports.dir` setting, the orchestrator writes `batch-<id>.html` whenever a batch
closes and regenerates `index.html` (refreshes every 30 s); its `reports` job also rewrites the
index and a live page for every running batch every 30 s, from the counters nodes post (marked
"live counters", with elapsed time and the distance to the census stop rule). The same pages on demand:
`atlas report html --database-file F --out DIR [--batch ID]`. They are single
self-contained HTML files: per product status, stop reason, success rate, unique and new IPs,
challenges, blocks, timeouts, proxy errors, p50/p90/p99, MB, top cities and ASNs, and events.

## Nodes and the manager's certificate

On the campaign manager the orchestrator answers HTTPS only, with a self-signed certificate it
made for its own IP address (`campaign manager orchestrator-enable`, docs/campaign.html). The
nodes trust exactly that certificate, never a public CA in its place:

1. `orchestrator-enable --apply` copies the public certificate beside the inventory
   (`campaign-manager.orchestrator.crt`) and records its SHA-256 in the inventory's
   `orchestrator.cert_sha256` and the URL in `orchestrator.orchestrator_url`
   (`https://<manager>:8443`). Check the copy you hand on: `shasum -a 256 <file>` must print
   that SHA-256. The key never leaves the manager; the certificate is public.
2. Pulse nodes: put the file in the fleet root and set `fleet.json` `"ca_file":
   "campaign-manager.orchestrator.crt"` with `orchestrator_url` set to the recorded URL, then
   reconcile. Arming copies it to the node as `pulse/orchestrator-ca.crt` and starts
   `atlas node ... --ca-file` on it.
3. Browser hosts (the minis): copy the file into the host's state directory and add
   `--ca-file STATE/campaign-manager.orchestrator.crt` to `atlas browser-node`. They use
   the browser port, `https://<manager>:8444`, and present their own client certificate there
   (`--client-cert`, `--client-key`, from `campaign manager browser-cert`; docs/browser.html).
4. Any other node: `atlas node --orchestrator https://<manager>:8443 --ca-file <file> ...`.

With `--ca-file` the node checks the server's certificate against that file alone, and the
URL's address against the address in the certificate; any other certificate, or the right one
at another address, is refused before a request is sent (the node logs `register refused: the
orchestrator's TLS certificate is not the trusted one (--ca-file)` and keeps retrying). The file
must hold exactly one PEM certificate. Without `--ca-file` an https URL is checked against the
system's public CAs. Plain `http://` is accepted only to this machine (a local staging
orchestrator): to another host it is refused at start, with or without `--ca-file`, because the
node's token would cross the network in clear text. A new certificate on the manager (after
825 days, or a new manager) means copying the new file to every node again.

## Hourly pulses on on-demand droplets

The HTTP pulses (`generic_http` and `generic_https`, 100 requests per country at concurrency 25)
run once an hour (PULSE-ONDEMAND-1). The pulse nodes are not always-on: each hour every network
gets one temporary droplet per runner region serving any of its formula's countries (18 networks
x 8 regions = 144 today), armed as an `atlas node`, which runs all of those countries' pulses
back to back from the start of the hour (one country every 12 s, `pulse.step_seconds`; the
largest droplet, 57 countries, is done in about 12 minutes) and is retired and deleted the moment
its own results are stored, at the latest 25 minutes after its creation (PULSE-COMPACT-1). Every
country is sampled once in the first quarter of the hour (about HH:00 to HH:15); the rest of the
hour has no pulse data, which the pulse evaluator counts as neither down nor healthy. The results
are ordinary pulse batches (run role `monitor`), so uptime, outages and the normal-requests
latency are computed as before.

**Cost per day:** droplets are billed per minute. Each lives about 15 to 20 minutes (created at
HH:57, armed, its countries back to back, removed), so 144 droplets an hour cost about 9 to 12
USD a day (`s-1vcpu-1gb-amd`), instead of about 36 USD when each lived the whole hour.

- `atlas daily pulse-plan --formulas formulas [--start next-hour] [--max-droplets 144]` is the
  dry run: it prints the hour's cells (network, region, countries) and `droplets`, writes
  nothing and calls no cloud, and exits 3 when the hour's droplets alone are over the droplet
  cap (default 144, at most the account's 500). With no cloud call it cannot see the pulse
  droplets still up; `pulse-hour` counts those too.
- `atlas daily pulse-hour --root <pulse root> --formulas formulas` runs one hour on the manager:
  create and arm the hour's droplets (the pulse root's `fleet.json`), replace a droplet that
  never boots once (PULSE-REPLACE-1: the old one removed through the guarded teardown, a new
  one created and armed for the same cell; its region opens after it, because the region's
  networks share each country's pulse batch), open their pulses, and
  retire each node (status `retired`, so the offline job raises no alert for it) and remove its
  droplet as soon as its runs are stored, through the guarded teardown, 16 at once (about 12.5 s
  each, so a whole hour's 144 go in about 2 minutes, not 30; `campaign/pulse_removal.py`); at
  the hard stop (25 minutes after the job began,
  less 3 for the removal) every droplet left goes, whatever happened; one not verified gone
  opens the `pulse_teardown_failed` alert. The cap counts only the pulse droplets younger than
  30 minutes; an older one is a leftover of a finished hour: removed through its hour's ledger,
  never counted, and the leftovers share one `pulse_leftover` alert, so a leftover never blocks
  the next hour. A leftover in no hour ledger (listed with `in no hour ledger` in the alert's
  data) is never deleted automatically and the alert repeats every hour: check it in the
  DigitalOcean panel and remove it by hand. Nodes lost before their hour ends share one alert,
  `pulse_nodes_lost` (PULSE-NODE-RETIRE-1; node states in `orchestrator/node_offline.py`).
  A cell (network and region) still without an armed node hands its countries to the same
  network's armed droplet with the fewest countries in another region (`handed` in
  `pulse.json`): their runs go from there, in the countries' own batches (counted in their own
  region by the pulse evaluator). A cell whose network has no armed droplet left is `lost`, and
  the hour's lost cells share one alert, `pulse_cells_lost` ("pulse hour H: cells lost:
  network/region, ..."). The hour's `state`: `done` (every cell armed, every run stored or
  still uploading to the manager at the hard stop, every droplet gone), `partial` (cells lost or droplets left over), `timed_out` (a run still queued
  or running at the hard stop; runs only uploading to the manager do not count), `not_opened`
  or `failed`.
- Its timer, `atlas-pulse.timer` (HH:57 for the next hour), is installed off. Only
  `atlas campaign manager pulse-enable --apply --owner-go <file>` (the owner's go) turns the
  orchestrator to on-demand pulses (`pulse.on_demand`, the HTTP targets at 60 minutes) and starts
  it; `pulse-disable --apply` stops it.

Details: "Hourly pulses on on-demand droplets" in [the campaign guide](docs/campaign.html#pulse-hour).

## Checking an hour (VERIFY-HOUR-1)

`atlas daily verify-hour --formulas formulas --database-file <credentials> [--hour last-hour]
[--template <planned root>] [--json] [--no-cloud] [--do-token-file <file>]` prints one screen of
pass/fail for one UTC hour (default: the hour that just ended), per network and in total, and
changes nothing: its database connection is read-only and it only lists droplets and bucket
objects, never deletes them. Run it on the manager for the first hours after the hourly tests or
pulses are switched on, and from time to time after.

- **Daily tests:** the countries the planner put in the hour against the daily runs stored
  (done, failed or missing). **Pulses:** every country of the hour's pulse droplets on each HTTP
  pulse target against the stored pulse runs, with their requests, and the cells planned
  against those pulsed (a pulse stored in one of their countries), the others listed as lost (off while `pulse.on_demand` is
  off). **Browser:** the browser runs of the 24 hours to the hour's end, per network and
  target (`x_web` too), against those uploaded (off while `browser.enabled` is off); a skipped
  batch shows as `-`, and a done batch whose runs were all excluded later is not due.
- **Processing:** how long ago the newest stored result finished (at most 90 minutes), the
  observations and IPs stored in the hour per network with a daily cell due (pulses store none,
  so a network that only pulsed is not listed), whether the calculation covering the hour ran
  (its snapshot and age, the last `calculate` job) and the tape rows of the hour (every daily
  cell needs its `daily_test_finished`).
- **DB-IP:** the release the hour's runs were enriched with (at most 45 days old) and the runs
  without one. **Alerts:** the open alerts above `info` and the jobs failed since the hour began.
- **Droplets:** the daily test's workers and the pulse droplets up now, listed by their tags
  (the manager's DigitalOcean token, `--do-token-file` for another); one older than 90 minutes
  outlived its hour: an orphan, named in the report and never removed by it (a `campaign shot`
  or probe worker, same tag, that runs longer shows up too).
  `atlas daily droplets [--do-token-file <file>] [--cap 250]` runs only these orphan checks,
  plus the count of both tags' droplets against the cap (near the account's droplet limit), and
  prints one `<tag|count>\t<ok|failed>\t<detail>` line each, exit 3 when one failed: the Slack
  alerts read it (MONITOR-1, `docs/operations/canada-server.md`).
- **Archive:** with `archive.enabled` and `raw.spool_dir` on, every network that stored runs in
  the hour has its raw files in Spaces under that hour or the next one (a run is filed when it is
  imported; a daily import that ends after the next hour shows as missing); not due until 75
  minutes after the hour's end. `--no-cloud` skips both.

The first line says OK or FAIL and names the first problem; each section has one line; then the
network checks that are not ok. A missing result is "not due yet" (`-`, never a failure) until
30 minutes after the hour's end. It exits 0 when nothing failed and 3 otherwise; `--json` prints
every check. Details: "Checking an hour" in [the campaign guide](docs/campaign.html#verify-hour).

## Daily DB-IP update

DB-IP releases are dated files in one CDN directory: `<base URL>/dbip-full-YYYY-MM-DD.mmdb`
(the release date). `atlas dbip update --dest DIR --base-url URL` keeps
`DIR/dbip-full.mmdb` at the newest one. The file always has that one name.

- **Finding the release.** It probes (HEAD) from tomorrow (UTC) back to the installed release,
  or back `--lookback-days` (default 62) when none is installed, and takes the newest file that
  exists. A 404 means no release that day; any other answer fails the run.
- **Downloading.** It downloads only when the release's name, size, ETag or Last-Modified
  differs from the installed one. The download goes to a temporary file in `DIR`, never into
  the file in use.
- **Checking.** The size must match the announced length, be at least `--min-bytes` (default
  100 MiB) and at least half the installed release. The file must open with the MaxMind reader
  as a DB-IP database, and of one test address per public /8, at least `--min-coverage`
  (default 90 %) must have a country.
- **Installing.** It renames the checked file over `dbip-full.mmdb` in one step, so a reader sees
  the old file or the new one and never a partial one. A reader that has the old file open keeps
  it until it reopens. Earlier releases are not kept. The same bytes under new headers leave
  the file untouched.
- **Recording.** It records the release in `dbip_releases` (with `--database-file`, default
  `$ATLAS_DATABASE_FILE`). `release.json` holds the installed release, so a run on an
  unchanged release downloads nothing.

Every run prints one JSON health line (`"dbip": "updated" | "unchanged" | "failed"`). Exit
codes: 0 updated or unchanged, 2 configuration, 3 probe, download, check or recording failed (a
release installed but not recorded is recorded by the next run), 4 another update of the
directory is running.

The base URL is configuration, never in this repository: `--base-url`, `$DBIP_BASE_URL` or the
orchestrator's `dbip.base_url` setting. It must be https without a query, and a redirect is
followed only to another https URL. Errors name the file and the HTTP status or error type,
never the URL. Pulse droplets never update (PULSE-NO-DBIP-1): they keep their image's release
(IMAGE-DAILY keeps the image current, and the manager re-enriches each pulse upload with its
own), because an update within their 15 to 20 minutes rewrote the 1.34 GB file on 1 GB droplets
and starved them on disk; a `dbip_base_url` left in the pulse fleet's file is ignored.

**Each test keeps the locations it was saved with.** Enrichment is stored per run with the
release it used, and the calculation reads each run's observations with that run's own release
(its newest one, if it was enriched again). A new release therefore changes only the runs
enriched with it, never older ones. The snapshot's `dbip_releases` lists the ids of every
release its observations were labelled with, and `dbip_release` names the newest of them (null
when it used none); a release no observation in the window used, such as one the update
recorded before any run used it, is in neither.
A release is its file's sha256, not its name, so the replaced `dbip-full.mmdb` is a new release.
An IP seen by runs with different releases takes the labels of its lowest `run_key` in the window.

**On the manager** (where the orchestrator runs), the orchestrator's `dbip_update` job runs the
update daily at 05:23 UTC once the `dbip.dir` setting names the directory. A failure fails the
job and raises a `job_failed` alert; an unchanged release records the run as `skipped`. Run it
now with `POST /admin/api/jobs/run {"job": "dbip_update"}`.

```sql
INSERT INTO settings(key,value) VALUES ('dbip.dir','"/opt/atlas/db-ip"'),
  ('dbip.base_url','"https://<cdn host>/<directory>/"')
  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now();
```

Readers name the file: point `--mmdb` or `DBIP_MMDB` at `DIR/dbip-full.mmdb`. Without either,
`db-ip/dbip-full.mmdb` wins over other `db-ip/*.mmdb` files. `daily ingest` and `enrich` open the
database per run, so each run uses the release installed when it is enriched. A long-running
`atlas node` reopens the replaced file between runs only (a run keeps one release), with no
restart.

**On the pulse droplets** (PULSE-NO-DBIP-1), each node keeps the release baked into its image and
never updates it: a pulse droplet lives 15 to 20 minutes on 1 GB of RAM, and an update within that
time (the former `atlas-dbip-update.timer`) rewrote the 1.34 GB file the node reads for every
answer and starved the droplet on disk (2026-11-15). IMAGE-DAILY keeps the image's release
current, and the manager re-enriches every pulse upload with its own release (PULSE-ENRICH-1).
- Arming renames the image's MMDB to `db-ip/dbip-full.mmdb` when that name does not exist yet,
  and starts the node with `--mmdb .../db-ip/dbip-full.mmdb`.
- Arming stops and removes any `atlas-dbip-update` service and timer an earlier arming
  installed. A `dbip_base_url` left in `fleet.json` is ignored.
- Hourly campaign workers keep the release baked into their image (`campaign image build --mmdb
  DIR/dbip-full.mmdb`); rebuilding the image refreshes them.

**Worker code and the image** (IMAGE-AUTO-1). Every droplet gets its code over SSH when it is
armed: the push carries the wheel, which is installed into the image's venv (`--no-deps
--offline`) unless that exact SHA-256 is installed already, then `uv pip check` stops the start
when the wheel needs a dependency the image lacks. On the Canada server `scripts/server-setup.sh`
builds each commit's wheel into `<env>/wheels/` and names it in `wheels/current.json`; daily and
on-demand pulse hours ship that wheel (not the template's or `fleet.json`'s) and record its
commit and SHA-256 under `code` in the hour's outcome. So the image (OS, Python, the locked
dependencies, the DB-IP release) needs a rebuild only when a runtime dependency changes or the
DB-IP release is a month old, not for each code change. `atlas daily image` (read only, in the
Slack checks) says when: the image's build record (`requirements_sha256`, `mmdb_built_at`, in
`<env>/.secrets/campaign-images.json`; build there with `--inventory`) against `current.json`.
`atlas campaign image-daily` (IMAGE-DAILY-1) rebuilds the image on the manager when the
manager's DB-IP release (`<env>/db-ip/release.json`) differs from the image's, and points the daily
template and the pulse fleet at it between :15 and :45 past an hour (docs/operations/canada-server.md).

## Private network onboarding

See [the standalone onboarding operator guide](docs/onboarding.html) for manual 24-hour
campaigns, all eight server assignments, hourly FIFO scheduling, private JSONL exports,
and restart behavior. `atlas onboarding run` accepts `--concurrency` (default
1000) and `--timeout` in seconds (default 10). Onboarding never imports results into the
site database. Census rule v2, shared with Test #1, counts every completed request
(including failures) in its 1,000-request minimum sample and unique-IP ratio.
The hourly droplet campaign (`atlas campaign …`: plan, manager deploy, enable,
teardown) is described in [the campaign operating guide](docs/campaign.html).

## The data API (`api/`)

[`api/`](api/) holds `siteapi`, the Go service behind the Atlas website. It reads the
framework's PostgreSQL database (the measurements plus the site's catalog rows in the `site`
schema) and serves every answer the website shows as precomputed, precompressed JSON. It has
its own Go module, tests and CI workflow (`.github/workflows/api.yml`) and is not part of the
Python package. [Its README](api/README.md) covers building, running, deploying and testing it;
[`api/FIELDS.md`](api/FIELDS.md) maps every dataset field to its source. It moved here from the
`proxy-index-api` repository with its history. Rules for both parts: [`AGENTS.md`](AGENTS.md).

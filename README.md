# Proxy Index

Standalone Python 3.12+ library and CLI for HTTP/SOCKS5 proxy measurements.
MIT licensed. No database server or scheduler is required.

Open [the standalone HTML guide](docs/index.html) for setup, API examples,
measurement definitions, result contracts and the implemented PostgreSQL schema.

```sh
uv sync --locked --no-editable
uv run --no-editable proxy-index targets
# Set PROXY_URL privately in your environment first.
uv run --no-editable proxy-index run --target generic_http --requests 5000 --concurrency 100
uv run --no-editable proxy-index enrich results/RUN_ID
```

Shifter Residential: see [the adapter guide](docs/shifter-parameters.html).
Copy `credentials.toml.example` to `.secrets/credentials.toml`, replace the placeholders,
and set directory/file permissions to `700`/`600`. The private copy is excluded from
Git, packages and remote uploads. Then:

```sh
uv run --no-editable proxy-index run --provider-config configs/shifter.toml \
  --credentials-file .secrets/credentials.toml --target generic_http \
  --url http://ip-info.com/myip --requests 5000 --concurrency 100 --param country=us
```

Both targets support the same provider config; optional per-test parameters and
session policies are exposed through the CLI and `GrammarProxy`/`load_provider` Python API.
Each of the 18 supported providers has a public run config in `configs/<provider>.toml` and a
reviewed credential grammar in `src/proxy_index/providers/grammars/<provider>.toml`; see
[the provider guide](docs/providers.html). Parameters use the same names everywhere:
`country`, `region`, `city`, `zip`, `asn`, `session` and `ttl` (minutes).
A grammar's optional `[testing] protocol` sets how the network runs echo tests:
`https-only` runs plain-HTTP echo targets over HTTPS, `mixed` follows a gateway redirect from
HTTP to HTTPS and counts it in `redirects`; `proxy-index providers --protocol https-only` lists them.
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
`proxy-index dbip update` keeps it current from the DB-IP CDN (see "Daily DB-IP update").

PostgreSQL: this project uses a dedicated local cluster on `127.0.0.1:55432`.
Run `uv run --no-editable python scripts/postgres_local.py start` after reboot.
Private database credentials are in `.secrets/database.toml`; the public template is
`database.toml.example`. Add `--database-file .secrets/database.toml` to `run` or
`enrich` for automatic ingestion, or use `proxy-index ingest results/RUN_ID`
with the same flag. Replaying an import does not double-count data. See the HTML
guide for initialization on a new machine and the schema.

Tests: `uv run --no-editable pytest`;
real database tests: `PG_TEST_ADMIN_FILE=.secrets/postgres-admin.toml uv run --no-editable pytest`;
scale benchmark: `uv run --no-editable python scripts/benchmark.py`.
CI (`.github/workflows/ci.yml`) runs this whole Tier 1, database tests included, on the
shared self-hosted CI pool for every ready pull request and every push to `main`.
All source changes are maintained locally; `scripts/test-remote.sh` uploads a
dedicated copy to the Mac Mini and downloads evidence. See the HTML guide.

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
storage; external UUIDs and portable artifacts stay unchanged. `storage.read_summary()`
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
keys still import and rebuild unchanged. `proxy-index db-seed --database-file
.secrets/database.toml` upserts regions, countries and targets from `configs/regions.yaml`,
`configs/countries.json` and `configs/targets.toml`; it keeps census times, enabled flags and
target schedules once set (`--reset-times` restores the staggered default census slots).

Schema v7 (`migrate_v6_v7.sql`) adds the batch kind `load` (the latency-under-load wave, see
below) and `test_batches.retry_of` (a census rerun after a provider outage names the census it
replaces); schema v8 (`migrate_v7_v8.sql`) adds `load_references`, the daily reference country of
each runner region; schema v9 (`migrate_v8_v9.sql`) moves place and ASN names into the shared
lists `place_names` and `asn_names`: `enrichment_locations` keeps a place id and coordinates,
`enrichment_networks` an ASN name id and the per-IP network attributes, every location and network
id is kept, and the views `enrichment_location_fields` and `enrichment_network_fields` give their
old columns (`ip_enrichment_fields` and `ip_enrichment_details` are unchanged); schema v10
(`migrate_v9_v10.sql`) adds `estimated_hours`; schema v11 (`migrate_v10_v11.sql`) adds the batch
kind `browser` (the browser pulse) and changes no row; schema v12 (`migrate_v11_v12.sql`) replaces
the `ip_observations` index `(ip_id, bucket_start)`, which no query used, with
`(bucket_start, ip_id)` for distinct-IP counts per time slice, and changes no row (building the
index blocks writes to `ip_observations`; dropping the old one then blocks reads and writes until
the migration commits, which follows at once). `db-init` verifies v12 and refuses other schemas.
`proxy-index db-migrate --database-file .secrets/database.toml` upgrades an unmodified v5 to v11
database, or an unmodified v4 database (and calculated v1 layer) through v5 to v12, in
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
proxy-index calculate-init --database-file .secrets/database.toml
proxy-index run-role RUN_UUID --role pool_sampling --database-file .secrets/database.toml
proxy-index calculate --methodology configs/methodology.toml --publish --database-file .secrets/database.toml
proxy-index snapshot-export ../src/data/dataset.json --database-file .secrets/database.toml
proxy-index serve --database-file .secrets/database.toml
```

From the website root, `npm run build` exports the measured dataset and builds the site;
`astro dev --background` serves it locally. `MEASUREMENTS_URL` defaults to `http://127.0.0.1:8081`.
Refreshes use the same-origin `/api/measurements/v1/snapshots/current` endpoint.
`proxy-index serve` never sends a whole payload (a daily build's runs to hundreds of MB):
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
build keeps serving. Census-pulse lists keep signature NULL. After
`discard` and `expire` the lists no window references are deleted (`store.collect`).
`calculated_list_details` shows a list's rows by `list_id`; the two breakdown views read the lists
and keep their columns. Snapshot payloads are kept for the current build, published builds
and the two newest unpublished drafts; older draft payloads are pruned on calculate/publish.
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
target only**, `latency_target` (default `generic_http`, the census target): light-load and
under-load latency, the speed score, breakdown latency and the load factor all come from it.
Other targets count for success, blocks, timeouts, IPs, authenticity, DC ASNs, bytes and uptime;
their `targets.<id>.metrics.latency_ms` and `latency_under_load_ms` hold nulls, with
`latency_reason`. Light-load latency (`metrics.latency_ms`, pulses only: census and load runs run
at census concurrency) merges the slots' histograms after normalising each to weight 1 and reads
p50/p90/p99 from the merged distribution; a slot with fewer than `[slots]
min_latency_successes` (30) successes does not vote. Percentiles are never averaged.

**Census overlap.** A pulse or load run of a product that ran while a census of the same product
was running (any country: account and node are shared) is marked `during_census`, derived at
calculation time from the stored run start and finish times. It still fills its slot and counts
for uptime, outages, down slots and every request metric, but it is left out of light-load
latency (breakdowns included), of the time-slot success (a slot votes with its other requests;
one left with only such pulses votes 0 if every request in it failed, else does not vote:
`time_slots.skipped_during_census`), and of the load factor (it needs a clean load run and a
clean reference pulse; otherwise the region's newest earlier factor carries forward:
`latency.regions[].factors.excluded_during_census`). Each window publishes `census_overlap`: `pulses` and
`targets.<id>` with `clean` and `during_census` (`runs`, `requests`, `successes`,
`request_success_rate`, `success_rate`, `latency_ms`), `load` (clean and during-census load
runs), `census_batches` and `basis`; `metrics.latency_reason` says why light-load latency is
missing.

**Latency under load.** In every pulse slot of the census target, each product also runs one
wave of `census_concurrency` requests at census concurrency in **every runner region's
reference country**: a batch of kind `load` with the pulse's `scheduled_for`, started
`load.delay_seconds` (60) after the pulses so the reference pulse is not measured under its own
load wave. It sends only the country, like every scheduled test. The orchestrator picks each
region's reference once per UTC day, with the day's first pulse plan, and stores it in
`load_references` (schema v8): the fastest enabled country tested from the region over the
trailing `load.reference_days` (7) — lowest light-load p50 of its census-target pulses that did
not run during a census, every product weighing the same, at least
`load.reference_min_successes` (300) successes, ties by country code (`fastest`); an override
(`load.reference_overrides`, `override`); without enough history the region's default
(`load.reference_defaults`: nyc3 US, fra1 DE, lon1 GB, ams3 NL, tor1 CA, sgp1 SG, blr1 IN, syd1
AU; staging fra1 RO; `default`); else its first enabled country (`first_enabled`).
`load.enabled` switches the wave off. Per percentile, factor(R, t) = load latency ÷ pulse
latency of region R's reference country in slot t, and every country's under-load latency(C, t)
= its pulse latency(C, t) × factor(region of C, t). A slot without a usable load run (both need
`min_latency_successes`) uses the region's newest earlier factor in the window; without one that
country's slot has no value. The window value is the mean over slots and countries. Example:
fra1's reference RO, RO pulse 600 ms, RO load 900 ms → ×1.5; a Bulgarian pulse (tested from
fra1) of 800 ms → 1,200 ms under load. Census latency never makes a factor. The speed score uses the under-load p50; light-load latency is
published next to it. Load runs count for every request metric but not for light-load latency,
slot coverage or uptime.
Snapshot payloads are `schema_version` 2: per product `census` (latest per country), `outages`
(latest 50 in 30 days) and `pricing`, per window `breakdowns`, `uptime` (`value`, `downtime_s`,
`countries`, `basis: "outage time"`, `batches`, `up`) and `latency` (`target`, `regions[]`:
`region`, `reference` of its newest load run, `chosen` (the daily `load_references` rows in the
window: `day`, `country`, `basis`, `p50_ms`, `successes`, `candidates`) and `factors` (`slots`,
`excluded_during_census`, `mean`, `latest`); `countries[]`: per requested country its `region`,
light-load and under-load latency), plus top-level `events`
(latest 200) and `traffic` (`by_test` census, pulse and load). Window and target `metrics` carry
`success_rate` (time-weighted), `request_success_rate`, `latency_ms` (light load),
`latency_under_load_ms`, `under_load` (slot counts and the reason when there is no value) and
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
`detect.detect(conn, now)` writes the Live tape events (`[events]` thresholds, not part of the
methodology id). `orchestrator/events.py` also writes `daily_test_started`, `daily_test_finished`
and `estimate_hour_released` (category `census`), `pulse_down` and `pulse_recovered` (`pulse`) and
`latency_rise` (`incident`); each helper documents its `data` fields. The API also serves live `GET /v1/events?product=&kind=&category=&since=&limit=`
and `GET /v1/traffic?test=&provider=&country=&region=&from=&to=`, and
`proxy-index report traffic --test census|pulse --database-file ...` prints GB per provider.
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
load it with `proxy-index metric-evidence configs/pricing/shifter-residential.json
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
proxy-index staging init --config configs/staging.toml          # --clean drops and recreates
proxy-index db-migrate --database-file .secrets/staging-database.toml   # an existing v6 one
proxy-index orchestrator --dev --database-file .secrets/staging-database.toml \
  --methodology configs/staging-methodology.toml
proxy-index calculate --database-file .secrets/staging-database.toml \
  --methodology configs/staging-methodology.toml                     # no --publish: a draft
proxy-index node --orchestrator http://127.0.0.1:8080 \
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

The planners can be switched off without a restart (both when neither flag is given; also
`POST /admin/api/schedule/scheduler {"census": false}`); a planner that is off records its job
as `skipped`:

```sh
proxy-index scheduler off|on|status [--census|--pulse] --database-file .secrets/staging-database.toml
```

With the `reports.dir` setting, the orchestrator writes `batch-<id>.html` whenever a batch
closes and regenerates `index.html` (refreshes every 30 s); its `reports` job also rewrites the
index and a live page for every running batch every 30 s, from the counters nodes post (marked
"live counters", with elapsed time and the distance to the census stop rule). The same pages on demand:
`proxy-index report html --database-file F --out DIR [--batch ID]`. They are single
self-contained HTML files: per product status, stop reason, success rate, unique and new IPs,
challenges, blocks, timeouts, proxy errors, p50/p90/p99, MB, top cities and ASNs, and events.

## Daily DB-IP update

DB-IP releases are dated files in one CDN directory: `<base URL>/dbip-full-YYYY-MM-DD.mmdb`
(the release date). `proxy-index dbip update --dest DIR --base-url URL` keeps
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
  `$PROXY_INDEX_DATABASE_FILE`). `release.json` holds the installed release, so a run on an
  unchanged release downloads nothing.

Every run prints one JSON health line (`"dbip": "updated" | "unchanged" | "failed"`). Exit
codes: 0 updated or unchanged, 2 configuration, 3 probe, download, check or recording failed (a
release installed but not recorded is recorded by the next run), 4 another update of the
directory is running.

The base URL is configuration, never in this repository: `--base-url`, `$DBIP_BASE_URL`, the
orchestrator's `dbip.base_url` setting or the pulse fleet's `dbip_base_url`. It must be https
without a query, and a redirect is followed only to another https URL. Errors name the file and
the HTTP status or error type, never the URL. The URL is not a secret today (a public dated CDN
path), so it also appears in the pulse nodes' update unit
(`/etc/systemd/system/pi-dbip-update.service`, mode 0644) and in its process arguments. If the
CDN ever needs a token, that has to change first.

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
INSERT INTO settings(key,value) VALUES ('dbip.dir','"/opt/proxy-index/db-ip"'),
  ('dbip.base_url','"https://<cdn host>/<directory>/"')
  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now();
```

Readers name the file: point `--mmdb` or `DBIP_MMDB` at `DIR/dbip-full.mmdb`. Without either,
`db-ip/dbip-full.mmdb` wins over other `db-ip/*.mmdb` files. `daily ingest` and `enrich` open the
database per run, so each run uses the release installed when it is enriched. A long-running
`proxy-index node` reopens the replaced file between runs only (a run keeps one release), with no
restart.

**On the pulse droplets**, each node fetches the release itself from the CDN. Nothing is pushed
from the manager: the arming tar is read into memory on a 1 GB node, and the MMDB is over 1 GB.
- With `dbip_base_url` in `fleet.json`, arming installs `pi-dbip-update.timer`. It runs
  `proxy-index dbip update --dest /opt/proxy-index-worker/db-ip` 5 minutes after arming, then
  daily at 05:41 UTC plus up to 30 minutes of random delay, which spreads the downloads.
- Arming renames the image's MMDB to `db-ip/dbip-full.mmdb` when that name does not exist yet,
  and starts the node with `--mmdb .../db-ip/dbip-full.mmdb`. The node runs on the image's
  release until the first update and reopens the file after each one.
- A node holds one release (about 1.4 GB) plus one download in flight.
- Without `dbip_base_url`, arming disables and removes a timer an earlier arming installed.
  Nodes armed before `dbip_base_url` was set get the timer only when armed again (re-created).
- Hourly campaign workers keep the release baked into their image (`campaign image build --mmdb
  DIR/dbip-full.mmdb`); rebuilding the image refreshes them.

## Private network onboarding

See [the standalone onboarding operator guide](docs/onboarding.html) for manual 24-hour
campaigns, all eight server assignments, hourly FIFO scheduling, private JSONL exports,
and restart behavior. `proxy-index onboarding run` accepts `--concurrency` (default
1000) and `--timeout` in seconds (default 10). Onboarding never imports results into the
site database. Census rule v2, shared with Test #1, counts every completed request
(including failures) in its 1,000-request minimum sample and unique-IP ratio.
The hourly droplet campaign (`proxy-index campaign …`: plan, manager deploy, enable,
teardown) is described in [the campaign operating guide](docs/campaign.html).

# MILF framework

Standalone Python 3.12+ library and CLI for HTTP/SOCKS5 proxy measurements.
MIT licensed. No database server or scheduler is required.

Open [the standalone HTML guide](docs/index.html) for setup, API examples,
measurement definitions, result contracts and the implemented PostgreSQL schema.

```sh
uv sync --locked --no-editable
uv run --no-editable milf-framework targets
# Set PROXY_URL privately in your environment first.
uv run --no-editable milf-framework run --target generic_http --requests 5000 --concurrency 100
uv run --no-editable milf-framework enrich results/RUN_ID
```

Shifter Residential: see [the adapter guide](docs/shifter-parameters.html).
Copy `credentials.toml.example` to `.secrets/credentials.toml`, replace the placeholders,
and set directory/file permissions to `700`/`600`. The private copy is excluded from
Git, packages and remote uploads. Then:

```sh
uv run --no-editable milf-framework run --provider-config configs/shifter.toml \
  --credentials-file .secrets/credentials.toml --target generic_http \
  --url http://checkip.amazonaws.com/ --requests 5000 --concurrency 100 --param country=us
```

Both targets support the same provider config; optional per-test parameters and
session policies are exposed through the CLI and `ShifterProxy`/`load_provider` Python API.
Target URLs can be supplied with `--url` or `RunConfig(target_url=...)`.
In v0.2 custom transports must accept `execute(target, *, context=None)`.

The locally supplied DB-IP MMDB belongs in `db-ip/` (or set `DBIP_MMDB`). It is
excluded from version control and distributions. It is separate from PostgreSQL:
MMDB provides optional offline enrichment; PostgreSQL persists history and cached lifetime metrics.

PostgreSQL: this project uses a dedicated local cluster on `127.0.0.1:55432`.
Run `uv run --no-editable python scripts/postgres_local.py start` after reboot.
Private database credentials are in `.secrets/database.toml`; the public template is
`database.toml.example`. Add `--database-file .secrets/database.toml` to `run` or
`enrich` for automatic ingestion, or use `milf-framework ingest results/RUN_ID`
with the same flag. Replaying an import does not double-count data. See the HTML
guide for initialization on a new machine and the schema.

Tests: `uv run --no-editable pytest`;
real database tests: `PG_TEST_ADMIN_FILE=.secrets/postgres-admin.toml uv run --no-editable pytest`;
scale benchmark: `uv run --no-editable python scripts/benchmark.py`.
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
keys still import and rebuild unchanged. `milf-framework db-seed --database-file
.secrets/database.toml` upserts regions, countries and targets from `configs/regions.yaml`,
`configs/countries.json` and `configs/targets.toml`; it keeps census times, enabled flags and
target schedules once set (`--reset-times` restores the staggered default census slots).

`db-init` verifies v6 and refuses other schemas. `milf-framework db-migrate --database-file
.secrets/database.toml` upgrades an unmodified v5 database, or an unmodified v4 database (and
calculated v1 layer) through v5, in place, in one transaction that compares every rebuilt
summary, scope, pool and bucket (and from v4 every cohort, breakdown and score) with its
original JSON before committing. Locally, run
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
milf-framework calculate-init --database-file .secrets/database.toml
milf-framework run-role RUN_UUID --role pool_sampling --database-file .secrets/database.toml
milf-framework calculate --methodology configs/methodology.toml --publish --database-file .secrets/database.toml
milf-framework snapshot-export ../src/data/dataset.json --database-file .secrets/database.toml
milf-framework serve --database-file .secrets/database.toml
```

From the website root, `npm run build` exports the measured dataset and builds the site;
`astro dev --background` serves it locally. `MEASUREMENTS_URL` defaults to `http://127.0.0.1:8081`.
Refreshes use the same-origin `/api/measurements/v1/snapshots/current` endpoint.
The development API helper is `python scripts/measurements_local.py start|status|stop`.
Calculated schema v2 keeps cohorts, breakdown labels (`places`, `asn_labels`) and score
components/reasons (`calculated_score_components`, `calculated_score_reasons`, `score_reasons`)
as typed rows; `cohort_criteria`, `calculated_breakdown_details` and `calculated_score_details`
rebuild the payload fragments. Snapshot payloads are kept for the current build, published builds
and the two newest unpublished drafts; older draft payloads are pruned on calculate/publish.
Calculated schema v3 (`calculated/migrate_v2_v3.sql`, applied by `db-migrate`) adds request
success, challenges, blocks and p50/p90/p99 to the breakdowns, which now cover country, region,
city, ASN and country × ASN from `run_geo_buckets`, and challenge rate, block rate, uptime and
wire bytes to every window. Census and pulse runs (those linked to a test batch) of a product
form one `census_pulse` cohort; without a `run-role` they count as `pool_sampling` (census) and
`monitor` (pulse). Uptime is outage time: 1 − downtime / (window × countries tested), with each
country's outages merged and clipped to the window; `uptime.batches/up` keep the pulse-slot counts
for reference (`[uptime]`). For these
cohorts `minimum_coverage` (0.90) applies to pulse slots, not 5-minute buckets: the share of the
product's pulse batches (excluding operator-skipped/killed batches and runs failed with
`no_node`, `not_started` or `stale`) whose run finished `done` with at least one request.
Snapshot payloads are `schema_version` 2: per product `census` (latest per country) and `outages`
(latest 50 in 30 days), per window `breakdowns` and `uptime` (`value`, `downtime_s`, `countries`,
`basis: "outage time"`, `batches`, `up`), plus top-level `events` (latest 200) and `traffic`.
`detect.detect(conn, now)` writes the Live tape events (`[events]` thresholds, not part of the
methodology id). The API also serves live `GET /v1/events?product=&kind=&category=&since=&limit=`
and `GET /v1/traffic?test=&provider=&country=&region=&from=&to=`, and
`milf-framework report traffic --test census|pulse --database-file ...` prints GB per provider.
TOML is data-only and versioned by SHA-256. The balanced profile uses `[weights]`;
alternative profiles use their own weights. No overall score is emitted for missing required inputs.
The current run is explicitly a pool sample. Scoring weights remain a draft.

### Scores: ratio to the leader

Every sub-score compares a network with the best network of the same kind (`kind` in
`configs/methodology.toml`; without one, the product name, e.g. `residential`) in the same
window (24h, 7d or 30d). Higher-is-better values score `100 × yours ÷ best`; lower-is-better
values (speed p50 latency, cost) are flipped to `100 × best ÷ yours`. Scores are clamped to 0–100,
so the leader of each component scores 100. Leaders are found again on every calculation; nothing
is stored between calculations. A network alone in its kind is its own leader and scores 100 on
every ready component. Only ready, freshly measured components set the best: incomplete, retained,
expired or historical scores never do.

| Component | Raw value | Better |
| --- | --- | --- |
| reliability | benchmark success rate, per target | higher |
| speed | benchmark p50 latency, per target | lower |
| uptime | outage-time uptime % (evidence as fallback) | higher |
| coverage | unique IPs per day | higher |
| diversity | countries, regions, cities, ASNs per day, mixed 0.30/0.20/0.25/0.25 (`[peer.diversity]`) | higher |
| dcAsns | clean share, 1 − datacenter ASN share (datacenter products keep 100) | higher |
| authenticity | (1 − claim gap) × 100 − blocklist hits × 120 (`[authenticity]`) | higher |
| value | cost per 1,000 successful requests | lower |

Reliability and speed are scored per target against that target's leader, combined per target
80/20 (`[target_weights]`), and averaged over the required targets (`[benchmark_weights]`).
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
unset means the daily `countries.census_time`), pulses every 2 minutes, HTML reports in
`reports/staging/`, and a token for the Shifter Residential node in fra1.

```sh
milf-framework staging init --config configs/staging.toml          # --clean drops and recreates
milf-framework orchestrator --dev --database-file .secrets/staging-database.toml
milf-framework node --orchestrator http://127.0.0.1:8080 \
  --token-file .secrets/staging-tokens/shifter-residential-fra1.token \
  --provider shifter --product residential --region fra1 \
  --provider-config configs/shifter.toml --credentials-file .secrets/credentials.toml
```

`staging init` takes host, port, user and password from `.secrets/database.toml` and creates the
database (owned by that user) with `.secrets/postgres-admin.toml`; it refuses a name equal to
`database.toml`'s dbname or `proxy_reviews`. It writes `.secrets/staging-database.toml` and the
node tokens (mode 600), runs db-init and db-seed, disables every country outside `[scope]`, and
prints a JSON summary with the exact orchestrator and node commands (`--secrets-dir` points it
elsewhere). Re-running it rotates the node tokens.

The planners can be switched off without a restart (both when neither flag is given; also
`POST /admin/api/schedule/scheduler {"census": false}`); a planner that is off records its job
as `skipped`:

```sh
milf-framework scheduler off|on|status [--census|--pulse] --database-file .secrets/staging-database.toml
```

With the `reports.dir` setting, the orchestrator writes `batch-<id>.html` whenever a batch
closes and regenerates `index.html` (refreshes every 30 s); its `reports` job also rewrites the
index and a live page for every running batch every 30 s, from the counters nodes post (marked
"live counters", with elapsed time and the distance to the census stop rule). The same pages on demand:
`milf-framework report html --database-file F --out DIR [--batch ID]`. They are single
self-contained HTML files: per product status, stop reason, success rate, unique and new IPs,
challenges, blocks, timeouts, proxy errors, p50/p90/p99, MB, top cities and ASNs, and events.

# The profile of the real 24-hour data (SIM-PROFILE-1), the generated days (SIM-GEN-1) and their load (SIM-LOAD-1)

`profile.py` turns the private 24-hour test data into one statistical **profile**: aggregate
figures only, with no IP address, host name, credential or path. The generator (SIM-GEN-1)
draws 7 days from it. The days must be statistically close to the measured data and the same
**size** per run as the real runs. PERF-REPORT-1 prints the `real_24h` totals next to the
generated ones.

**The real profile is never committed or posted.** It is built where SIM-RUN-1 runs: on the
campaign manager, after the sources are copied there over SSH (files 600, folder 700). The
copies are deleted afterwards, as REPLAY-RUN-1 did with its slices. Never on a workstation, and
never in CI with real data.

## Usage

```sh
uv run python scripts/scale/profile.py build --enrich <LC-ENRICH-1 out dir> \
  --browser <BP-VERIFY-3 hours dir> [--browser <BP-VERIFY-4 hours dir>] \
  --out <profile.json> [--formulas formulas]
uv run python scripts/scale/profile.py reference --database <toml> \
  --runs <marker:<summary key> | batches:<id>-<id>[,<id>...]> --out <profile.json> \
  [--basis real|modelled]
```

- `build` writes `<profile.json>` (mode 600) and prints the number of networks and gaps.
  `--browser` is repeated for BP-VERIFY-4's hour folders, when given. All the folders are read
  into one set of runs: BP-VERIFY-3's first, then BP-VERIFY-4's.
- `reference` adds the `reference` section to the profile (it creates the file when it is
  missing). A later call replaces only the run kinds it read. `--basis` (default `real`) is
  written as each kind's `basis`. SIM-RUN-1 (#718) runs it twice, on staging only (nothing in
  this phase touches production):
  - with `--runs marker:scale_replay`, for REPLAY-RUN-1's real hour 0 (190 real daily tests),
    before that hour is wiped;
  - with `--runs batches:<id>` and `--basis modelled`, for one modelled browser bundle of
    BP-VERIFY-3's tries and bytes, ingested on staging through the real node API ingest.
- Exit 0 on success. Exit 1 with one line on stderr when a source does not have the expected
  shape (the line names the file), when `--enrich` is missing or holds no network folder with a
  `manifest.json` (the line names the folder; no file is written), or no run matches `--runs`.
  A folder with at least one network keeps the networks it lacks as `gaps`.

## Inputs

- **LC-ENRICH-1** (`--enrich`): one folder per network, with `manifest.json` (schema 2) and one
  `hour-<YYYYMMDD>T<HH>Z.jsonl.gz` per clock hour with IPs. The manifest's slot `00`–`23` gives
  the hour's status (`present`, `requests_no_ips`, `missing`), `hour_start`, `unique_ips`,
  `request_rows`, `requests` (attempted), `successes` and `errors` (per kind). Each line of an
  hour file is one distinct exit IP: `ip`, `seen_count`, `regions`, `countries` (request rows
  per requested country) and `enrichment` (DB-IP's 14 fields). A file whose records or request
  rows differ from its slot fails the build.
- **BP-VERIFY-3** (`--browser`): one folder per hour, each with the manager's `mgr.json`. Its
  `runs` lists every browser run the manager knew that hour: `batch`, `target`, `provider`
  (the network), `product`, `run_status`, `requests` (tries), `successes`, `challenges`,
  `blocks`, `timeouts`, `proxy_errors`, `bytes_down`, `run_key` and `test_runs`. Its
  `archive_sample` lists up to three archived bundles (`batch`, `network`, `bytes`). A run
  appears in every hour it was open: its state in the last folder (by name) is its final one.
  The hosts' `browser-<n>.json` files are not read.
- **Networks:** the ones with a formula (`formulas/*.json`, 18 today).

## The profile

```text
profile_version   1
networks          per network: product, hours, seen_count_log2, recurrence, places, browser
real_24h          per network: the measured 24-hour totals
gaps              what the sources lack
reference         per run kind: rows and bytes per run (profile.py reference)
```

Shares are fractions from 0 to 1, rounded to 6 decimals; `null` when nothing was measured.
A spread is `{count, p10, p50, p90, mean}` (nearest rank; `null` values when the count is 0).

### `networks.<network>`

- `product`: the manifest's product.
- `hours`: 24 entries, `00`–`23`, from the manifest slots and the hour files:
  - `hour`, `status` and `hour_start` (ISO, UTC);
  - `unique_ips`, `request_rows`, `attempted_requests`, `successes`, `errors` and
    `errors_by_kind`: the slot's figures (the manifest counts attempts and errors per hour,
    not per country);
  - `countries.<CC>`: per requested country, `unique_ips` and `request_rows` of that hour.
- `seen_count_log2`: how many (hour, IP) records served 1, 2-3, 4-7, ... request rows.
- `recurrence`:
  - `share` and `countries.<CC>`: the share of a run's IPs that the network's run of the same
    country also had `gap_hours` earlier. `pairs` counts the (earlier, later) hour pairs used.
  - `basis`: `24 h apart` when two hours are 24 h apart. Otherwise it is the measured gap
    closest to 24 h, and ties go to the shorter one. One 24-hour run has one file per clock
    hour, so its gaps are 1 to 23 h, or more when slots come from two runs: `hour_start` sets
    the gap. Fewer than two hours with IPs: `gap_hours` and the shares are null.
  - `other_country`: per requested country, the share of its distinct IPs over the 24 hours
    that were also seen for another requested country of the network; `share` pools them.
- `places.<CC>`: per requested country, the (country_code, region, city, asn, usage_type)
  tuples of DB-IP, counted over the (hour, IP) records of that country.
  - `top`: the 500 with the most records. Each has its `share`, `latitude`, `longitude` (of
    the first record seen) and `asn_name` (DB-IP's `asn_organization`).
  - `tail_share`: all the other tuples together.
  - `unmatched_share`: records DB-IP did not match (`matched` false, or no enrichment).
  - `ips`: the records counted. The shares of `top`, `tail_share` and `unmatched_share` add up
    to 1.

  These are DB-IP's public labels, never addresses.
- `browser.<target>.<engine>`: the stored runs of BP-VERIFY-3, by target and engine. The engine
  is the run's `engine` when the summary names one, otherwise the default engine (Camoufox),
  as for every run before the engine choice.
  - `runs`; `tries`: a spread of tries per run;
  - `outcomes`: `ok`, `challenge`, `block`, `timeout` and `proxy_error` counts. `login_wall`,
    `no_content`, `no_answer` and `other_error` are null: the manager's summary does not split
    them, so `unsplit` holds those tries. `reference.browser.targets` has all nine from the
    database. `ok_share` is ok over tries.
  - `page_load_ms`: `p50` and `p90` are null here, for the same reason. They are in
    `reference.browser.targets`.
  - `bytes_down`: the sum. `bytes_per_load`: a spread of each run's bytes_down over its tries.
  - `bundle_bytes`: a spread of the archived bundle bytes of the runs the manager sampled.
  - `ingest`: what the manager recorded of every run of the target, stored or not. It gives
    `runs`, `stored` (final status with a `test_runs` row or a run key), `test_runs` (the
    rows) and `statuses` (runs per final `run_status`). The figures above count the stored
    runs only.

### `real_24h.<network>`

- `unique_ips_hour_sum`: the slots' unique IPs added up. `unique_ips_distinct`: the distinct
  IPs of the 24 hours.
- `request_rows` and `attempted_requests`: the slots' sums, including hours without IPs.
- `browser_runs`, `browser_tries` and `browser_bytes_down`: the network's stored browser runs.
- `bundle_bytes_sampled` and `bundle_samples`: the sum and number of its sampled bundles.

The 24-hour onboarding run measured one run per network, region and hour. The profile keeps
these per-run sizes. It never turns that hourly volume into a daily one: production runs one
daily test per network × country per day, and each network × browser target once per 24 h
(`docs/api-web-map.md` section 14).

### `gaps`

One entry per thing a source lacks: `{source, network?, hour?, target?, what}`, with `source`
`enrich` or `browser`. Examples:
- an hour without IPs, such as byteful's 23 hours of `requests_no_ips`;
- a network without a folder, or a folder without a formula, which is not profiled;
- runs left out because they were not stored or were unfinished at the end;
- a network without a stored browser run (its targets still show the `ingest` of its runs);
- the two source limits above: the outcome split and page loads, and the sampled bundle bytes.

Nothing is invented to fill a gap.

### `reference.<kind>` (`daily`, `browser`)

Everything is read in one read-only, repeatable-read transaction. A run is `daily` when its
summary has `test_kind` `daily`, and `browser` when its batch is a browser batch. Other runs
are left out.
- `basis`: `real`, or `modelled` when the runs were ingested from a modelled bundle (the
  browser reference on staging). A modelled size is never a measured one.
- `runs_filter`, `database`, `read_at` and `runs`.
- `rows_per_run.<table>`: a spread over the runs, for `test_runs`, `run_buckets`,
  `run_geo_buckets`, `ip_observations`, `run_enrichments`, `ip_enrichment`, `ip_addresses`,
  `product_ips`, `pool_ips`, `estimated_hours`, `batch_runs` and `test_events`.
  - The tables keyed by `run_key` count the run's own rows.
  - The shared tables count the rows the run's observations reference: `ip_addresses` (its
    distinct IPs), `product_ips` (of its product), `pool_ips` (of its pool) and `ip_enrichment`
    (in its enrichment releases). This is the run's size, whether or not an earlier run added
    the row first. The day-over-day growth follows from `recurrence`.
  - `test_events` counts the events of the run's batch and product.
  - `networks.<network>` gives the same per network.
- `bytes`:
  - `tables.<table>`: `rows` (count(*)), `table_bytes` (with TOAST) and `index_bytes`, over
    all its partitions.
  - `per_run.<table>`: `table_bytes` and `index_bytes` of the kind's mean run, that is bytes
    per row times the mean rows per run. `basis` says so.
- `targets.<network>.<target>.<engine>` (browser only), from the runs' `run_buckets`:
  - `runs` and `tries`;
  - `outcomes`: all nine, as the calculation counts them (`calculated/browser.py`);
  - `ok_share`;
  - `page_load_ms` `p50` and `p90` of the successful tries.

## Privacy

IPs are only hashed in memory, with the process's salted 64-bit hash, to count distinct IPs
and recurrence. The hashes are never written. The output holds counts, shares, DB-IP labels,
network, target and country names, and nothing else. `tests/test_scale_profile.py` builds
LC-ENRICH-1 and BP-VERIFY-3 shaped fixtures with reserved addresses only (192.0.2.0/24,
2001:db8::/32). It checks that no input address, host name or path appears in the output bytes.

## Speed and memory

One pass per hour file, gzip read line by line, and one network in memory at a time. A
network's memory is 8 bytes per (hour, country, IP), plus a set of one hour's IPs while
recurrence is counted. Its places are bounded by its distinct DB-IP tuples. The PR (#700)
gives the time per GB of hour files, and the peak memory, measured on a disposable server with
a 1 GB fixture.

## The generated days (SIM-GEN-1)

`simulate_model.py` turns the profile and production's plan into the runs of 7 days, hour by
hour, at production's cadence and size. `simulate_check.py` checks them against the profile.
Nothing here touches a database but `load_plan`, and nothing is written but the run files a
caller asks for.

```python
plan = simulate_model.load_plan(database, date(2026, 11, 2))  # once; pulses=True adds them
hour = simulate_model.hour(profile, plan, seed, day, hour)  # day 1..7, hour 0..23 UTC
simulate_check.size(profile, hours), simulate_check.stats(profile, hours)
```

### Cadence

The cadence is `docs/api-web-map.md` section 14, an assumption decided from the code and
configs: one daily test per network × country per day; one 100-try browser run per network ×
target per day; no HTTP pulse fleet, so no pulses, census or load waves unless switched on.

- **Daily tests:** every network × country whose formula `schedule` hour is this hour, in
  `daily/planner.py` `plan_hour`'s cells (3,848 a day for the 18 formulas, 442 at 13:00 UTC).
  `hour.daily` holds the tests; `hour.reports()` gives each cell's `region`, `countries`,
  `status`, `requests`, `unique_ips`, `started_at` and `finished_at`, the keys
  `daily/ingest.py` `emit_cell` reads. `test.write(directory)` writes the run directory as
  `cycle_steps.daily_directory` builds one: `summary.json`, `ips.jsonl.gz`,
  `enriched-<release>.jsonl.gz` and `enrichment-<release>.json`, for `storage.ingest`; the
  test's `lane.formula` is what `estimates.compute.estimate_run` takes.
- **Browser runs:** the entries of each host's queue (`orchestrator/browser_queue.py`, read,
  never changed) that start this hour, with the plan's browser settings (`plan = "queue"`,
  `queue_period_minutes = 1440`, 100 tries; by default
  `configs/browser-pulse.production.example.toml`: 3 hosts × 6 networks × 8 targets, 144 runs a
  day). `run.bundle(run_row, seed, outbox)` seals it through `browser_model.bundle` for its
  planned batch run row. The bundle is BROWSER-MODEL-1's Camoufox bundle; the run's `engine`
  is the settings' engine of the target, which the checks group by.
- **Pulses:** none unless the plan is loaded with `pulses=True`. Then every pulse target
  (`targets` with `pulse_enabled`, every 30 minutes in `configs/targets.toml`) gets a pulse per
  network × enabled country and slot, with `replay_steps`' geo model: 0 to 3 timeouts and 47
  to 92 place rows sharing 100 requests.

### The plan

`load_plan(database, start, formulas, browser, pulses=False)` reads the database once, as
`replay_plan.py` does: the enabled countries (with an enabled runner region) and, with
`pulses`, the `pulse_enabled` targets with their minutes. It adds every `formulas/*.json` and
the browser settings, which must be in queue mode. The result is a plain value (`Plan`); day
`d` starts at `start + d - 1` 00:00 UTC.

### Size of a run

- **Unique IPs** of a daily test: a country the profile requested takes its `unique_ips` at the
  test hour (`networks.<n>.hours.<HH>.countries.<CC>`). Any other country takes its formula's
  figure at its test hour (`synthetic.measured`), and a country the formula lacks the network's
  10th-percentile country (`synthetic._fallback`). Each day varies it by up to ±10%
  (`JITTER`).
- **Request rows per IP** follow `seen_count_log2`; an IP is in 1 to all of the test's 2 to 4
  five-minute buckets (never more than its rows), one `ip_observations` row each. The number
  of buckets turns over the lanes, so a day averages 3 a test.
- **Errors** are timeouts at the profile hour's error share (errors over attempts), varied by up
  to ±10% a day; without a profile hour, one per `ERROR_SHARE` successes. Latency bins are
  synthetic's, around a network median that varies by up to ±10% a day.
- **Rows per table** (`test.tables()`), as the ingest and the estimate store them: 1
  `test_runs` and `run_enrichments`; the buckets; the observation rows; one `ip_addresses`,
  `ip_enrichment`, `product_ips` and `pool_ips` row per IP (the run's size, as `reference`
  counts it); the estimated hours `estimates.compute.hours` gives; no `run_geo_buckets`,
  `batch_runs` or `test_events`.
- **A browser run** has the target's tries (100). Its outcomes are the reference's nine counts
  (`reference.browser.targets`), else the profile's (the `unsplit` tries as `other_error`),
  else BROWSER-MODEL-1's hour, apportioned with one try moved a day (about a point). Its bytes
  follow `bytes_per_load`'s mean (±10% a day), its successful page loads a lognormal through
  the reference's p50 and p90 (else 2.5 s and 4.5 s; the median ±3% a day), and its bundle
  bytes `bundle_bytes`' mean (±5% a day): each failed try carries a screenshot of incompressible
  bytes, together the mean less a 16 kB bundle without one. `run.tables()` counts the
  `test_runs` and `batch_runs` row; the ingest decides the others.

### Addresses and recurrence

Every network × scheduled country is a lane with its own block of IP indexes, as in
`synthetic.py`: 3 of every 4 indexes are IPv4 addresses in the reserved block 240/4 while that
space lasts, the others IPv6 in 2001:db8::/32. No real address is used. Day `d`'s window starts the
network's `recurrence.share` of its size before the end of day `d − 1`'s, so that share of its
IPs were seen the day before and the rest are new; a network without one draws it from
`REUSE` (30–60%).

A window position's place and request rows come from evenly spread (Weyl) sequences, so a
test takes each share almost exactly and an IP keeps its place from day to day. Places are the
country's `places.<CC>.top` tuples by share, then 200 synthetic tail places (private ASNs
4200000000 and up, names `Synthetic …`) for `tail_share`, then unmatched. A country the profile
did not request has only tail places and synthetic's 0.5% unmatched.

### Determinism

The same profile, plan and seed give equal hours and byte-identical run files, whether an hour
is generated alone or with others: every figure is a hash of the seed and its names, and each
lane's block is fixed by the plan.

### Checks

Both return `{"pass", "compared", "checks"}`. Each check carries its group, `deviation`,
`limit` and `pass`, never an absolute figure. Nothing compared does not pass.

- `size(profile, hours)`:
  - per run kind × network (daily), or network × target × engine (browser), each metric's
    runs against the reference: their p50 inside its p10–p90 band (`in_band`), their mean
    within 10% of its mean (`deviation` relative). The daily metrics are the rows per table of
    `reference.daily.networks.<n>` (else `reference.daily`). The browser metrics are `tries`,
    `bytes_per_load` and `bundle_bytes` of the profile's target entry, and the rows of
    `reference.browser`.
  - per kind and day, the day's rows per table against the reference's mean × the day's runs
    (within 10%).
- `stats(profile, hours)`, for one generated day, per network:
  - requests per IP against `seen_count_log2`: total variation distance at most 0.05;
  - `country`, `asn` and `city` shares over the profile's requested countries (top 500, tail,
    unmatched): the IP-weighted distance at most 0.05;
  - recurrence of the tests after day 1 against `recurrence.share`: within 5 points;
  - per target and engine, each outcome's rate against the expected counts: within 2 points;
    the successful tries' page-load p50 against the reference's: within 10%.

`tests/test_scale_simulate_model.py` builds a synthetic profile (2 networks, 3 countries, 2
browser targets) and checks the cadence, the sizes, the ingest of a generated test and bundle,
determinism, recurrence, pulses only when switched on, and both checks passing on generated
days and failing on shifted ones.

## Loading the generated days (SIM-LOAD-1)

`simulate.py` loads the generated days into the staging database `proxy_reviews_staging`
through production's real processing and insert paths, then calculates and reports. Nothing
waits for real time: the hours are generated ahead while earlier ones are stored.

```sh
uv run python scripts/scale/simulate.py --database <staging toml> \
  --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>" --profile <profile.json> --days 7 \
  --first-day <YYYY-MM-DD> --out <dir> [--jobs 6] [--batch-daily 20] [--connections 25] \
  [--memory-budget 4] [--pulses] [--siteapi URL] [--plan-only] [--no-scope]
```

- The same command resumes from `<out>/state.json`; another database, first day, number of
  days, `--pulses` or profile is refused. Exit 0 done, 2 refused (nothing written), 130
  stopped (Ctrl-C or SIGTERM).
- **Guards**, checked before any write, are the replay's (`replay.guard`): `target.refusal`
  with the owner's go accepts `proxy_reviews_staging` only (production is refused, with or
  without a go); no `job_runs` row of the last 5 minutes (a live scheduler); and the extra hour
  after the days must have ended.
- Every run's summary carries `scale_replay` with the load's own id, and the state holds the
  baseline, the nodes and the days (the extra hour's day included), so `wipe_replay.py
  --state <out>/state.json` removes exactly what the load wrote.
- `--plan-only` reads the plan in one read-only transaction and writes nothing. It prints per
  simulated day the runs by kind (daily, browser, and pulse only with `--pulses`), then per
  table the rows and the table and index GB the profile's `reference` gives those runs (its
  mean per run × the runs), and the totals.

### The cadence

One daily test per network × country per day; one 100-try browser run per network × target
per day; no HTTP pulse fleet, so no pulses, census or load waves unless switched on
(`docs/api-web-map.md` section 14). `--pulses` switches them on at the pulse targets' cadence.

### Generation ahead, storage in order

`--jobs` worker processes (spawned, never forked) build the model once each and write their
share of an hour's daily tests as run directories under `<out>/generated/hour-<n>/`, at most 2
hours ahead of the hour being stored. The main process stores the hours strictly in order and
deletes each hour's directories once it is stored (all of them when the load ends). The model's
seed is the load's id, so a resumed load generates the same hours again.

The workers read the browser settings file by path: loaded settings do not pickle (their dict
`inputs` are read-only mappings). Their plan carries the file's path and SHA-256, taken before
the main process loads it for its own planning, and each worker loads the file once when it
starts; a file changed since then is refused (`SettingsChanged`) and the load stops.

### One simulated hour

replay.py's step loop runs each step until it is done; a failed part is recorded in
`events.jsonl`, paused (doubling from `--pause`) and retried. Client statements are traced
(`measure.Tracer`) and, with `pg_stat_statements`, the server's windows are kept per hour.

1. **daily**: the hour's `daily/planner.py` cells, each a cell as `daily ingest` prepares one,
   go through `daily/ingest.store_cells`: whole cells about `--batch-daily` tests at a time,
   each batch in one `storage.ingest_many` and one `compute.estimate_runs`, `--connections`
   batches at once (below), then each cell's `daily_test_started` and `daily_test_finished`
   (`emit_cell`), once (`<out>/daily-ingest.json` holds the hour's cells). `state.json`
   records each batch's run ids as it commits (the daily step's `groups`), written by the
   main process only; a stopped hour stores again only the runs not stored yet (by run id).

   **Parallel connections** (INGEST-PARALLEL-1, `storage_parallel.py`): each connection's
   TCP receive window caps it at about 0.6 MB/s over the 77 ms link to the database, so the
   hour's batches go over `--connections` (default 25; 40 and more where the server has the slots) connections at once, one worker thread
   and one connection each. `--connections 1` is the serial path, statement for statement.
   - *The locks, narrowed.* An import holds the ingest lock (817452901) shared, so imports run
     side by side while a purge, seed, migration or rebuild (which take it exclusively) waits
     for them, and a lock per run (817452904 with the run id's hash), so two imports of one run
     still go one after the other. Its runs' own rows (temp tables, runs, buckets, geo rows,
     addresses, observations, enrichment, and each run's summary as stored) go in under no
     other lock. Each network's counts take that network's lock (817452906 with the provider's
     hash, the batch's networks in one order): the IPs new to each pool (`pool_ips`), to the
     provider (before its `product_ips` are added) and to each scope, and the scope totals
     (`scope_stats`). Batches of different networks never wait for each other there. Only the
     last statement before COMMIT, the `provider_overlap` increments for the batch's IPs new to
     their provider, takes the global overlap lock (817452905): milliseconds. Each is read
     after its lock (READ COMMITTED), so it sees every import committed before, as the serial
     path does.
   - *No deadlocks.* Every multi-row insert into a table imports share goes in key order, and
     two batches of one network are not stored at the same time while one of another network
     can go instead. A deadlock (40P01) or serialization failure (40001) rolls back only that
     batch, retried 3 times with backoff on a new connection; one that still fails is retried
     one country at a time after the others (`daily/batches.py`).
   - *Caps*, printed at the start of each hour: the connections are capped so that 10 of the
     server's `max_connections` slots stay free for production, and so that the batches in
     flight, estimated at 20 times their run directories' bytes, stay within `--memory-budget`
     GB (default 4, inside the 6G memory cap). A batch is never split: N goes down instead. A
     capped count is a warning, not an error.
2. **browser**: one browser node per network of the settings' hosts, bound to its host's
   name (the tokens are issued once a process, never saved). The real planner
   (`orchestrator/browser.py`, queue mode) runs at each run's start with the model's settings
   given in-process; no settings row is written. Each planned run's bundle (the model's tries,
   sealed for its host) is uploaded through the in-process node API into `browser_ingest`,
   with a temporary local archive deleted after the hour.
3. **pulses**, only with `--pulses`: the replay's pulse step, with the model's geo rows.
4. **release**: `cycle_steps.Steps.release`, the estimated hours that ended.

The nodes are disabled when the load ends, however it ends.

### The calculation

After the last hour, `proxy-index calculate --as-of <end> --publish` runs as a child under
`measure.py trace`, in a systemd memory scope as the replay's (unless `--no-scope`). Then one
more simulated hour is loaded and `calculate --incremental --publish` runs for it, so the
report has the hourly calculation's time at the days' volume. A failed calculation is paused
and retried. With `--siteapi`, each waits until its `/healthz` serves the build. The 24h and 7d
figures come from the daily tests and the released hours through the existing windows.

### The report

`<out>/report.md` and `report.json` (mode 600, rewritten when the load ends or stops) hold
names and counts only, no IP, host name or path:
- per day the runs by kind, and the rows inserted per second of ingest;
- the size report at the end of each simulated day and after the calculations, for every
  table whose rows or bytes changed (public and site, partitions included): its rows,
  `pg_table_size` and `pg_indexes_size`, the growth, and the expected figure (the profile's
  `reference` per run × that day's runs);
- per day the generated unique IPs, requests, browser runs, tries and bundle bytes next to the
  profile's `real_24h` totals; the basis of every figure is named in `basis`;
- wall time per step; the calculations' time, peak memory and attempts;
- `simulate_check.size` over the stored days and `simulate_check.stats` of day 2 (day 1 when
  there is one), per network.

`tests/test_scale_simulate.py` loads the synthetic profile of `test_scale_simulate_model.py`
(2 networks × 3 countries × 2 browser targets) for 2 days, a stop and resume against an
uninterrupted load, pulses switched on, `--plan-only` and the refusals. With the production
example settings (`configs/browser-pulse.production.example.toml`, dict inputs) it checks that
the workers' plan pickles, that a changed settings file is refused, and that one hour loads the
same rows with `--jobs 1` and `--jobs 2`.

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
overlap           per pair of networks: the IPs they share (SIM-OVERLAP-1)
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
  - `top`: the 500 with the most records, or up to 2,000 when the 500 leave more than 10% of
    the records in the tail (a long-tailed network), so the model draws that tail from real
    places. Each has its `share`, `latitude`, `longitude` (of the first record seen) and
    `asn_name` (DB-IP's `asn_organization`).
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

### `overlap` (SIM-OVERLAP-1)

The IPs two networks share over the 24 hours, from the hour files: resellers and the same
residential device sold through several pools. Counts and shares only, never an address.
- `basis`: what is counted, the distinct IPs of the 24 hours.
- `pairs`: one entry per pair of networks with at least one shared IP, by name; a pair that
  shares none is absent.
  - `networks`: the two names, in order;
  - `shared` and `shares.<network>`: the distinct IPs both had, and that count over each
    network's distinct IPs (any requested country);
  - `countries.<CC>`: per country both requested with a shared IP, the same over the IPs that
    served that country's requests (an IP is in a country when it served it at least one
    request row). A country both requested that is absent shared none.

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
- `buckets` (daily only, and per network in `networks.<network>`; SIM-MODEL-3), from the
  runs' 5-minute buckets:
  - `per_run`: runs per number of `run_buckets` rows, such as `{"1": 30, "2": 50}`;
  - `per_ip`: per a run's number of buckets, its IPs by the buckets each was seen in (its
    `ip_observations` rows), such as `{"2": {"1": 800, "2": 200}}`.
- `releases` (daily only): the dates of the DB-IP releases the runs were enriched with, from
  each release's file name (`dbip-full-YYYY-MM-DD`), else its build's day.
- `targets.<network>.<target>.<engine>` (browser only), from the runs' `run_buckets`:
  - `runs` and `tries`;
  - `outcomes`: all nine, as the calculation counts them (`calculated/browser.py`);
  - `ok_share`;
  - `page_load_ms` `p50` and `p90` of the successful tries.

## Privacy

IPs are only hashed in memory, with the process's salted 64-bit hash, to count distinct IPs,
recurrence and the IPs networks share. The hashes are never written. The output holds counts,
shares, DB-IP labels, network, target and country names, and nothing else.
`tests/test_scale_profile.py` builds LC-ENRICH-1 and BP-VERIFY-3 shaped fixtures with reserved
addresses only (192.0.2.0/24, 2001:db8::/32). It checks that no input address, host name or
path appears in the output bytes.

## Speed and memory

One pass per hour file, gzip read line by line, and one network in memory at a time. A
network's memory is 8 bytes per (hour, country, IP), plus a set of one hour's IPs while
recurrence is counted. Its places are bounded by its distinct DB-IP tuples. For the overlap,
each network's distinct IPs per country stay (8 bytes each, every network's together), and
one merge of them all counts the pairs at the end. The PR (#700) gives the time per GB of hour
files, and the peak memory, measured on a disposable server with a 1 GB fixture.

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
- **Request rows per IP** follow `seen_count_log2`.
- **Buckets** (SIM-MODEL-3, `simulate_shape.py`): a test's five-minute buckets follow the
  reference's `buckets.per_run` of its network (else of all daily runs), spread over the
  network's lanes and days. An IP is in as many buckets as `buckets.per_ip` gives an IP of a
  run with that many buckets, one `ip_observations` row each, never in more than its request
  rows. Its request rows and its buckets follow one quantile, so an IP with more rows is in
  more buckets. Without a reference, 2 to 4 buckets a test and an IP in 1 to all of them.
- **DB-IP release:** production enriches each run with the release installed at its ingest
  (`enrichment.enrich_run` on `dbip-full.mmdb`, which `dbip update` replaces when the CDN has
  a newer dated file), and stores an IP's enrichment once per release (`ip_enrichment`'s key
  is the IP and the release, inserted with `ON CONFLICT DO NOTHING`). DB-IP publishes one
  release a month. So the model has one release a month, on the day of the month of the
  reference's newest release (else the 1st): a day's tests take the newest release on or
  before that day. Only on a day with a new release is every IP enriched again; otherwise only
  the new IPs add `ip_enrichment` rows. The enrichment files name the release
  `dbip-full-YYYY-MM-DD.mmdb`.
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
  per page load follow the `bytes_per_load` spread (SIM-MODEL-3): a log-normal through its
  p50, as wide as its p10 and p90. Each day of a week takes a different seventh of it, in an
  order fixed by the seed, so a week's runs of a network × target have the spread's median and
  5 of 7 lie between its p10 and p90. A network whose runs recorded no bytes has a p50 of 0: it
  takes the spread of the other network with the median p50 for the target and engine, and
  only without any BP-VERIFY-2's mean, a p50 alone (`simulate_browser.bytes_spread`). Its
  successful page loads a lognormal through
  the reference's p50 and p90 (else 2.5 s and 4.5 s; the median ±3% a day), and its bundle
  bytes `bundle_bytes`' mean (±5% a day): each failed try carries a screenshot of incompressible
  bytes, together the mean less a 16 kB bundle without one. `run.tables()` counts the
  `test_runs` and `batch_runs` row; the ingest decides the others.

### Addresses and recurrence

Every network × scheduled country is a lane with its own block of IP indexes, as in
`synthetic.py`: 3 of every 4 indexes are IPv4 addresses in the reserved block 240/4 while that
space lasts, the others IPv6 in 2001:db8::/32. No real address is used; the IPs two networks
share come from shared lanes ("Shared IPs" below). Day `d`'s window starts the
network's `recurrence.share` of its size before the end of day `d − 1`'s, so that share of its
IPs were seen the day before and the rest are new; a network without one draws it from
`REUSE` (30–60%).

A window position's place and request rows come from evenly spread (Weyl) sequences, so a
test takes each share almost exactly and an IP keeps its place from day to day. Places are the
country's `places.<CC>.top` tuples (up to 2,000 for a long tail), then 200 synthetic tail
places (private ASNs 4200000000 and up, names `Synthetic …`) for `tail_share`, then unmatched.
The tuples are grouped by city (SIM-MODEL-3): the first sequence picks the city, each city one
slice; a second one picks the tuple inside the city, ordered by its ASN's share in the
country. A long tail's city then takes its share even when its tuples are many and small, and
an ASN takes about one slice inside every city. A country the profile
did not request has only tail places and synthetic's 0.5% unmatched.

### Shared IPs (SIM-OVERLAP-1)

Each pair of the profile's `overlap`, in each country both networks schedule, takes a share of
each network's IPs: the country's `countries.<CC>.shares` when the profile has it, none in a
country both requested without one, and the network's `shares` in any other country (one the
profile did not request for both). Where both shares are above 0 the pair has a **shared
lane** in the country (`simulate_overlap.py`): its own block of IP indexes after the
networks' lanes, synthetic addresses only. A pair the profile lacks shares nothing, and no IP
is shared across countries.

- **A day's shared IPs** are the shared lane's window, and both networks' tests take all of
  it, so each address appears in both. Its size is the geometric mean of the two networks'
  share of their test sizes, so each side is within half the two sizes' mismatch of its share
  (within about 10% with the ±10% daily jitter), never more than the network's part of its
  lane (below). A test takes the rest of its size from its own lane.
- **A lane whose shares add up to more than 1** has them scaled down to 1, and each pool's
  most is its part of the lane's shares, so a test's shared IPs never exceed its size.
- **Recurrence holds for shared IPs too:** a shared window slides from day to day like a
  lane's, at the lower `recurrence.share` of the two networks, and the own window recurs what
  the network's share still needs, so a test's recurrence is its network's as before. A shared
  IP keeps its window position from day to day, and so its place and its quantile.
- **Places:** each network draws a shared IP's place from its own country places at the
  shared position, so its measured places hold. The ingest stores an IP's enrichment once per
  release (`ON CONFLICT DO NOTHING`), so the database keeps the first network's.
- **Rows:** `ip_addresses` and `ip_enrichment` are keyed by the IP, so a shared IP adds one
  row there for the two tests; `product_ips` and `pool_ips` one per network.

### Determinism

The same profile, plan and seed give equal hours and byte-identical run files, whether an hour
is generated alone or with others: every figure is a hash of the seed and its names, and each
lane's block is fixed by the plan (a shared lane's by the plan and the profile's `overlap`).

### Checks

Both return `{"pass", "compared", "checks"}`. Each check carries its group, `deviation`,
`limit` and `pass`, never an absolute figure. Nothing compared does not pass. Every rule
follows the owner's notes on cadence and size (2026-11-08) and `docs/api-web-map.md` section
14: real data from another schedule is compared per run or per try, never per day.

- `size(profile, hours, model, sizes)` (SIM-CHECK-2):
  - **browser tries per run** (`tries_per_run`) against the plan, the target's tries in the
    queue settings (100 per network × target a day), within 10%. The note
    `browser_tries_day` puts a generated day's tries beside BP-VERIFY-3's (`real_24h`) and
    names the different schedule: BP-VERIFY-3 is a reference per try only;
  - **bytes per page load** (`bytes_per_load_p50`): the runs' p50 against
    `simulate_browser.bytes_per_load`, within 10%; `basis` says whose figure it is
    (`network`, `target` or `BP-VERIFY-2`). Outcomes and page loads are in `stats`;
  - **daily ratios** per network (`ratio`), within 10% of the reference's
    (`reference.daily.networks.<n>`, else `reference.daily`): observation rows per unique
    IP, enrichment rows per IP, buckets per run, and `product_ips`, `pool_ips` and
    `scope_ips` rows per IP of the run (a table neither side counts is skipped: today
    `scope_ips`, which only `day_rows` compares). Absolute rows per run
    are not compared across networks: a run's size follows its country;
  - **unique IPs** per network and day (`unique_ips`) against the sum of its formulas'
    figures over its scheduled countries (`Lane.planned`), each varied by the day's jitter,
    within 10%;
  - with `sizes` (state.json's snapshots, the baseline as day `0`), **a day's rows**
    (`day_rows`) per table, within 10%. The append-only tables (`ip_observations`,
    `run_buckets`, `test_runs`, `estimated_hours`, `test_events`) expect the runs × the
    reference's rows per run of their kind, and `ip_observations` the day's IPs × its
    observations per IP. The new-IP tables (`ip_addresses`, `ip_enrichment`, `product_ips`,
    `pool_ips`, `scope_ips`) expect the day's new IPs × their rows per IP (1 where the
    reference lacks the table): all of day 1's IPs, and on a later day each network's IPs ×
    (1 − its recurrence). `ip_addresses` and `ip_enrichment` count a new IP two networks share
    once (each test half of its new shared IPs). `ip_enrichment` expects all the day's IPs on
    a day with a new DB-IP release (day 1 and each monthly release day), else the new IPs. Rows
    the ingest does not write (`calculated_*`, `job_runs`, …) and a table's rows on the day it
    appeared (a migration's back-fill, such as v14's
    `scope_ips`) are left out and listed in `apart`, by day.
- `stats(profile, hours)`, for one generated day, per network:
  - requests per IP against `seen_count_log2`: total variation distance at most 0.05;
  - `country`, `asn` and `city` shares over the profile's requested countries: the
    IP-weighted distance at most 0.05, on the same support for both sides: the attribute's
    50 values with the most share in the profile, one tail bucket for every other value
    (real or synthetic) and unmatched. A network whose profile places hold fewer than 5,000
    IPs is reported with `insufficient_reference` and passes;
  - recurrence of the tests after day 1 against `recurrence.share`: within 5 points;
  - the IPs a pair shares (`shared`, kind `overlap`, SIM-OVERLAP-1): per pair of the
    profile's `overlap` and per network of it, the share of its tests' IPs that the other
    network's tests of the same day and country also had, against the profile's share, within
    20% relative. It is grouped per country the profile measured (`country`), and over the
    countries that take the network's figure (`country` null). A group whose profile figure
    has fewer than 1,000 shared IPs is reported with `insufficient_reference` and passes;
  - per target and engine, each outcome's rate against the expected counts: within 2 points;
    the successful tries' page-load p50 against the reference's: within 10%.

`tests/test_scale_simulate_model.py` builds a synthetic profile (2 networks, 3 countries, 2
browser targets) and checks the cadence, the sizes, the ingest of a generated test and bundle,
determinism, recurrence, pulses only when switched on, and both checks passing on generated
days and failing on shifted ones. `tests/test_scale_simulate_overlap.py` (SIM-OVERLAP-1)
builds a profile's overlap from hour files without an address. On a synthetic profile with 3
networks and 2 shared pairs it checks that the generated pairs share at the profile's shares,
per country and over the network, that no other pair shares an IP, that shared IPs recur, the
`shared` check passing and failing a shifted profile, the new-IP rows counting a shared IP
once, and a lane's shares above 1 scaled down. `tests/test_scale_simulate_check.py` covers
each rule of SIM-CHECK-2, the bytes per load and the long tail on the same profile.
`tests/test_scale_simulate_shape.py` (SIM-MODEL-3) checks the buckets per run and per IP
against a reference's distributions, the monthly release with a week of one lane through the
real ingest (`ip_enrichment` grows by the new IPs, and by every IP on the release day), a run's
bytes per load against a wide spread's median and band, and a fragmented long tail's city and
ASN distances.

## Loading the generated days (SIM-LOAD-1)

`simulate.py` loads the generated days into the staging database `proxy_reviews_staging`
through production's real processing and insert paths, then calculates and reports. Nothing
waits for real time: the hours are generated ahead while earlier ones are stored.

```sh
uv run python scripts/scale/simulate.py --database <staging toml> \
  --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>" --profile <profile.json> --days 7 \
  --first-day <YYYY-MM-DD> --out <dir> [--jobs 6] [--ahead 2] [--spool-gb 20] \
  [--batch-daily 20] [--connections 25] [--processes P] [--store-ahead 6] \
  [--memory-budget 4] [--pulses] [--siteapi URL] [--plan-only] [--no-scope]
```

- The same command resumes from `<out>/state.json`; another database, first day, number of
  days, `--pulses` or profile is refused. The store and generation options (`--connections`,
  `--processes`, `--store-ahead`, `--jobs`, `--ahead`, `--spool-gb`) may change: `options`
  holds the first start's and each `resumed` entry its own. Exit 0 done, 2 refused (nothing written), 130
  stopped (Ctrl-C or SIGTERM).
- **Guards**, checked before any write, are the replay's (`replay.guard`): `target.refusal`
  with the owner's go accepts `proxy_reviews_staging`, and production's `proxy_reviews` only with
  the production go, `--i-know-this-is-production` and `PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1`
  ("Production as a second staging database" below); no `job_runs` row of the last 5 minutes
  (a live scheduler); and the extra hour after the days must have ended.
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
share of an hour's daily tests as run directories under `<out>/generated/hour-<n>/`, up to
`--ahead` hours (default 2; up to 168 to use idle cores) ahead of the hour being stored, two
hours generating at a time, while the generated hours not stored yet take less than
`--spool-gb` GB (default 20; the generator pauses at the cap, an hour still generating
counted as large as the generated ones on average). Each worker's share of an hour ends with
its manifest (`part-<k>-of-<jobs>.json`): every file's size and SHA-256, recorded at write,
and the tests' figures. The main process stores the hours strictly in order and deletes each
hour's directories once it is stored. A stopped load keeps the rest: the next start reuses
every share whose files match its manifest and writes the others again (all of them go once
the load ends). The model's seed is the load's id, so a resumed load generates the same hours
again. `state.json`'s `counts` add up `generated_tests`, `generation_cpu_seconds` (the
workers' CPU) and `reused_parts`.

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
   records each batch's run ids as the hour's step sees it committed (the daily step's
   `groups`), written by the main process only; a stopped hour stores again only the runs not
   stored yet (by run id).

   **Parallel connections** (INGEST-PARALLEL-1, `storage_parallel.py`): each connection's
   TCP receive window caps it at about 0.6 MB/s over the 77 ms link to the database, so the
   batches go over `--connections` (default 25; 40 and more where the server has the slots)
   connections at once, one worker thread and one connection each. `--connections 1` is the
   serial path, statement for statement.
   - *Worker processes* (INGEST-PROCESSES-1): preparing a batch's rows costs about 0.4 s of
     client CPU per daily test, and one Python process uses one core however many threads
     wait on the network. The N connections therefore go over `--processes` P worker
     processes (spawned; default `min(7, CPUs − 1, N)`), each of about N / P threads, which
     receive only the batch's run-directory paths, estimates and the credentials path. P = 1
     is the thread pool of one process, statement for statement. The main process hands the
     batches out (never two of one network at once while one of another network can go), to
     the least busy process; a process that dies fails only its batches in flight, named by
     their run ids, which are then retried one country at a time, while the others go on.
     The client statement trace (`measure.Tracer`) sees only P = 1's statements.
   - *Store ahead* (`--store-ahead`, default 6): an hour has only 8 to 20 batches, too few
     to keep N connections busy, so the load keeps one pool of connections open over all its
     hours and gives it, at each hour's start, that hour's batches and then those of the next
     `--store-ahead` − 1 hours already generated. The hours still finish in order: an hour's
     events, its release and its `state.json` mark come once all its batches have committed
     and the hour before is done, and a resume starts from the oldest hour not done (the
     batches of later hours stored before the stop are no-ops then). A batch stored ahead
     stores its estimated hours too, so an earlier hour's release can announce estimated
     hours of a test stored ahead once they have ended.
   - *The locks, narrowed.* An import takes its locks at the start, before any write, in one
     order: the ingest lock (817452901) shared, so imports run side by side while a purge, seed,
     migration or rebuild (which take it exclusively) waits for them; its networks' locks
     (817452906 with each provider's hash, sorted), so imports of one network go one after the
     other while imports of different networks never wait for each other; its runs' locks
     (817452904 with each run id's hash), so two imports of one run go one after the other.
     Only the last statement before COMMIT, the `provider_overlap` increments for the batch's
     IPs new to their provider, takes the global overlap lock (817452905): milliseconds. Each
     network's counts (`pool_ips`, the IPs new to the provider before its `product_ips` are
     added, each scope's new IPs and totals) are read after its lock (READ COMMITTED), so they
     see every import of the network committed before, as the serial path does. With N
     connections, at most as many are busy as there are networks with a batch pending (18
     networks: at most 18).
   - *No deadlocks.* Every multi-row insert into a table imports share goes in key order, and
     two batches of one network are not stored at the same time while one of another network
     can go instead. A deadlock (40P01) or serialization failure (40001) rolls back only that
     batch, retried 3 times with backoff on a new connection; one that still fails is retried
     one country at a time after the others (`daily/batches.py`).
   - *Caps*, printed when the pool opens: the connections are capped so that a quarter of
     the server's `max_connections` slots (at least 20) stay free for production, and the
     batches in flight over all processes, estimated at 20 times their run directories'
     bytes, stay within `--memory-budget` GB (default 4, inside the 6G memory cap). A batch is
     never split: it waits for room instead. A capped count is a warning, not an error.
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

## Checking and re-storing a stored load (SIM-CHECK-2)

```
simulate.py check --out <run dir> --database <staging toml> --profile <profile.json> \
  [--formulas formulas] [--browser-settings <settings>]
simulate.py --rerun browser --days 7 ... (the load's own arguments) [--apply]
```

- `check` runs both checks again from `<out>/state.json`, and from the plan and the stored
  browser runs, which it reads in one read-only transaction: `size` over the stored days with
  the day sizes state.json kept, `stats` of day 2 (day 1 alone: its own). The browser checks
  measure each stored run's tries and bytes as the calculation reads them (`run_buckets`'
  `completed` and `bytes_down`), so runs stored by an earlier model are measured as stored;
  the daily figures are the ones state.json recorded at the load. It refuses another database or a profile whose
  SHA-256 is not the load's, and writes `checks.json` and `checks.md` (mode 600) beside the
  report, nothing else. It never loads or reloads data.
- `--rerun browser` stores the load's browser runs of the simulated days again with the current
  model; the daily tests stay as stored. It takes the load's arguments, the same guards and
  the owner's go (staging only), and needs the load's `state.json`.
  - Without `--apply`, a dry run: in one transaction it deletes the browser batches of the
    simulated days that hold a run of the load and are run by its nodes only (a batch the
    planner made ahead without a run stays), their runs (marked with the load's id) and
    every row that references them (`test_events`, `outages`, the runs' buckets,
    observations, enrichments, estimated hours and calculation roles, `batch_runs`), recounts
    the live tape's buckets as `wipe_replay.py` does, rolls it all back, and writes the rows
    per table to `rerun-browser-dry-run.json`.
  - With `--apply` after a dry run: the same delete, committed only when its counts equal the
    dry run's; the dry run's file then goes, so a later rerun needs its own. Then every simulated hour's browser runs (about 1,008 for 7 days) go through
    the load's browser step again (`rerun_browser` in the hour's record), resumable with the
    same command; the nodes are disabled at the end. The extra hour keeps its runs.
  - A stop between the delete's commit and state.json's save leaves no record of the delete:
    the next `--apply` is refused until a new dry run (which then deletes nothing) records it.
  - A recalculation follows only when the operator runs one.

## Real hours (REAL-2DAY-1)

A small real-data test: each simulated day's daily tests carry the real records of one of the
24-hour real tests' hours (LC-ENRICH-1's hour files), instead of the model's addresses. Only
the dates are relabelled; the IPs are real. It checks the overlap between networks on real
shared IPs, and every other endpoint.

```sh
uv run python scripts/scale/simulate.py --database <toml> --owner-go "<go>" \
  --profile <profile.json> --days 2 --first-day <YYYY-MM-DD> --out <dir> \
  --real-hours 12,0 --source <LC-ENRICH-1 out dir> [--connections 25] [--plan-only]
uv run python scripts/scale/simulate_check.py real-overlap --source <LC-ENRICH-1 out dir> \
  --hours 12,0 --out <overlap.json> [--formulas formulas]
```

- `--real-hours H1,H2,...` names the real clock hour of each simulated day (one per `--days`),
  and goes with `--source`. Day N's daily tests are one per network × requested country of
  the network's hour-HN file whose formula schedules the country. Each holds that country's
  records with `countries[CC]` > 0: the IP, `countries[CC]` as its request rows, the
  enrichment (DB-IP's 14 fields) and the record's DB-IP release (the manifest's
  `dbip_release` as its metadata when the SHA-256 is the same). One test has one release.
- The rest is the model's, as in the generated days: the test runs at the country's formula
  hour of the simulated day, its buckets, latency bins and timeouts follow `daily_directory`'s
  rules and the profile, and the browser runs are the queue's (one 100-try run per network ×
  target a day, BROWSER-MODEL-1). No pulses, census or load waves. No synthetic shared lanes
  (SIM-OVERLAP-1): the real IPs carry the overlap. The extra hour after the days has no daily
  test.
- A network without the hour's file gets no test that day (byteful has only its 16:00 hour), a
  country no formula schedules gets none, and a source folder without a formula is not read.
  `--plan-only` and `report.md` name them under "Real hours"; state.json keeps them as `real`.
- The source is read where it is, never copied: every hour file it uses is read once to count
  its IPs per country and checked against its manifest slot (SHA-256, distinct IPs, request
  rows, each record's countries against its `seen_count`), then once more by the worker
  processes for the records, through `slices.py`'s reader (each record's fields). A source
  that does not match its manifest, or holds no folder of a formula's network (a mistyped
  `--source`), is refused before any write. Real hour files are never
  copied into the repository or CI; the tests use reserved addresses only.
- The store, the guards (`target.py`'s scratch targets, and staging with the owner's go), the
  marker, the resume and `--plan-only` are the load's above; another `--real-hours` resumes
  nothing (`state.json` keeps them). The `stats` check is skipped (the profile's shapes are
  not the real records'); `size` runs. `simulate.py check` refuses a real-hours load.
- `real-overlap` reads the same records (the same formulas, countries and checks) and writes,
  mode 600, per network its product and distinct IPs, and per pair of networks the distinct
  IPs both had: over every real hour together (`7d` and `30d`) and over the last one (`24h`).
  Counts only, never an IP. The run compares it with `/v1/overlap` of the same windows.
- `--real-hour-override <network>=<H1>,<H2|none>` (owner, 2026-11-10; repeatable, one per
  network) gives one network its own real hour of each day instead of `--real-hours`', `none`
  for no test that day: `byteful=16,none` makes byteful's day 1 its real 16:00 hour and gives it
  no test on day 2. It goes with `--real-hours`, names one hour or `none` for each of the
  `--days`, and a network once. A `none` day is not a missing hour. `--plan-only` and
  `report.md` name every override under "Real hours" ("override byteful: day 1: 16, day 2:
  none"); state.json keeps them (`real_hour_overrides`, and `real.overrides`), and a resume with
  other overrides is refused. `real-overlap` takes the same option so its counts match the load.

## Score inputs (SCORE-DATA-1)

Owner decision, 2026-11-11: the stored days of a load (the #815 two real days) also carry every
input the Index Score needs, with uptime and outages: modelled test data added on top of them.
The daily tests give coverage, diversity, hosting share and authenticity; this mode adds what a
pulse fleet would have produced: the HTTP pulses of `generic_http` and `generic_https`, the load
waves (speed), down pulses (outages, uptime), and the live tape over them. Value (the price
evidence) is VALUE-TEST-1's, not this mode's.

The order (#840 runs it so): store, tape, audits, calculate.

```sh
uv run python scripts/scale/simulate.py <the load's own arguments> --add-scored-inputs \
  [--pulse-days byteful=1,2] [--plan-only]
uv run proxy-index daily tape --database-file <toml> --from <the first day, 00:00+00:00> \
  --to <the day after the last, 00:00+00:00> [--dry-run]
uv run proxy-index daily audits --database-file <toml> --from <same> --to <same>
uv run proxy-index calculate --database-file <toml> --as-of <the day after the last, 00:00> \
  --publish
```

- **The report** (SCORE-DATA-1b). `--plan-only` and the run each write `score-inputs.md` in the
  load's `--out` (mode 600) and add its totals to their JSON on stdout (`summary`: batches, rows
  and runs per kind, outages, the open one, the minutes). Per day and network it gives the
  batches, runs and rows by kind (`pulse generic_http`, `pulse generic_https`, `load`; a run is
  a test run, a batch run and a bucket, a load run adds its IP rows), the load factor (the
  network's of that day), the outage plan (network, day, tier, country count, start, minutes,
  still open at the end), the uptime each window will show, the pulse days (`default`, or the
  `--pulse-days` given, with the day each pulse day's countries come from), and the minutes:
  estimated at the load's insert rate (`report.json` `days[].rows_per_second`) and, for the
  run, taken. Counts and the plan only: never an IP, host or path.
- **The tape** (`proxy-index daily tape`, SCORE-DATA-1b; fast since TAPE-FAST-1). The pulses
  alone make no outage: the live tape's detector (`detect.py`) opens and closes them, so the
  outages, and with them the uptime, the calculation's outage lists and `/v1/changelog`'s
  incidents (#844), come from the tape: #840 runs it before its calculations. The command sets
  detect's watermark just before `--from` and judges every batch that finished in `[from, to)`
  once, in time order, with detect's own judging code; its events are the detector's
  (`pulse_finished`, `down`, `recovered`, `success_drop`, `latency_spike`, `block_spike`, the
  hosting audits), each stamped with the moment of the `--step-minutes` (5) step its batch
  finished in plus `settle_seconds`, as detect's minute calls stamp them.
  - **Fast** (`detect_replay.py`): one transaction per hour of batches, the watermark moved in
    it, so a stopped run resumes from the last hour it committed. At the start it reads, with a
    few set-based queries, each judged series' (country, target, network) last 48 pulses before
    the range and their medians, the series' incident events of the last 7 days, the pulse
    events it compares with, the open census outages and the hosting ASNs already reported.
    Each hour is then a fixed number of statements, however many runs it has: the hour's
    batches, their runs, the pulses up to their slots, the runs' medians, the hosting audit and
    one insert of the hour's events; only an outage's opening and closing and a recovery's
    "still down elsewhere?" check are statements of their own. At the real shape (691,200
    pulse runs over two days, 18 networks; `scripts/scale/tape_measure.py` on a throwaway
    database, 2026-11-11) the two days took 86 seconds on the database's own host, 2 seconds an
    hour, with 43 statements an hour (99 at most, 2,156 in all): about 2 minutes at 9 ms a
    round trip, where the step replay took about 10 hours.
  - **Step** (`daily.tape.replay(..., fast=False)`, for comparison): `detect()` every step,
    each call its own transaction, 7 to 9 round trips a pulse run. Both give the same events and
    outages (`tests/test_detect_replay.py`), and a range begun by one and finished by the other
    matches either alone.

  It prints one JSON line: the events per kind and network, the range's outages (and the open
  ones) per network, the steps and the seconds. `--dry-run` takes every hour in one
  transaction and rolls it back. Run again over the same range it adds nothing (the detector
  skips a batch it judged and judges a silent one the same way). Another `detect()` moving the
  watermark meanwhile (a running orchestrator) stops it (exit 2). It refuses production's
  `proxy_reviews` (exit 2) without #817's three, as `daily audits` does. An outage row has the
  incident's start, end (none while open: ongoing), country and region; an incident over
  several countries is the outages that share a network and start, and the lowest slot success
  is not stored (a pulse outage's down pulses have none: 0).

- **Additive and resumable.** It takes the load's `--out` and its arguments, and refuses (exit
  2, nothing written) unless `state.json` is that load's (database, first day, days, hours, the
  profile's SHA-256, real hours and overrides) and every hour of it is stored, or when the days
  already hold pulse or load runs it did not write. The guards are the load's: `target.py`'s
  (#817 on production: `--owner-go`, `--i-know-this-is-production`,
  `PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1`), no orchestrator running, the days ended. It writes
  only pulse and load batches, their runs and the days' `load_references`; the daily and
  browser runs are neither touched nor reloaded.
- It writes the days hour by hour (all 24 hours of each day, whatever the load's hours a day):
  the hour's batches (`ON CONFLICT DO NOTHING` on their slot), its pulse runs, its load runs
  (through `storage.ingest_many`, 250 runs a transaction, which skips a run id it has), then
  their batch runs done with their counters and the batches done. Run ids are a pure function
  of the load's seed, the kind, network, country, target and slot. `state.json` keeps the
  hours done under `score_inputs`; a stop resumes at the next hour, and an hour stopped half-way
  is written again whole, storing nothing twice. Run again when done, it writes nothing.
- `--plan-only` reads in one read-only transaction and prints, as JSON, per day the rows to
  write (batches, runs, batch runs, buckets, IP observations), per network its pulse and load
  runs and each target's requests, the outage plan, the uptime each network will show per
  window and each network's pulse days (each with the day its countries and IPs come from).
  Nothing is written to the database (only the report, below). Never an IP address. Its load
  runs use each region's override or default
  reference country; the run itself chooses day 2's from day 1's pulses (`orchestrator/references.py`),
  so day 2's load-run count may differ a little.
- **Test data.** Every added run's summary carries `"synthetic": true`, `"score_inputs":
  "score-inputs-1"` and the load's `scale_replay` id (`wipe_replay.py` removes them with the
  load). Batches and batch runs have no column for a mark; they are known by their runs.
  `clean_target.py` empties batches, batch runs, runs, events, outages and load references at
  launch with the rest.
- **Pulses** (the census/pulse cohort: batch-linked runs) at each pulse target's interval
  (30 minutes), in each enabled country where the network has real IPs that day (its daily
  tests' countries). A pulse has `pulse_requests` (100) requests, more when a network's pulses
  of a day would complete fewer than 2 × `minimum_requests` (8,000) per target. Its success
  rate and latency follow the network's daily tests (their success rate, p50 and p90), within
  ±3 % a slot; one slot a network and day has 2.5 × the latency (a latency event).
- **Load waves.** In each slot of the census target (`generic_http`), one `load` batch per
  runner region in its reference country, 75 seconds after the slot (`pulse.lead_seconds` +
  `load.delay_seconds`), with a run of `census_concurrency` (1,000) requests for each network
  that pulses there, at least `[slots] min_latency_successes` (30) successes, and the network's
  latency × its load factor (1.1 to 1.8: per network, moved by up to ±0.2 each day). Each day's `load_references` are chosen
  and stored by `orchestrator/references.py` at the day's start, as on the orchestrator's first
  slot (the default, then the fastest of the earlier pulses). A network without real IPs in a
  region's reference country gets no load run there. Load waves never fail.
- **Exit IPs.** A load run's IP rows are up to 100 of the network's real IPs of that day and
  requested country (a sample of 2,000 read from its stored daily tests, never written
  anywhere else); a pulse stores none (its distinct IPs are capped by the real ones). Nothing
  synthetic enters the pools: unique IPs, ASNs, countries, overlap pairs and the fleet totals
  stay as they were.
- **Outages** (the plan in `--plan-only` and in `state.json`, `score_inputs.plan`). A down
  pulse is a done run with requests and no success, in both targets of a country, so the
  tape's `detect()` opens an outage at the last healthy pulse and closes it at the next healthy
  one. The published uptime is 1 − downtime ÷ (window × countries tested), and the countries
  tested are those with a started pulse or census batch run (`scheduled.py` `tested_spans`;
  daily tests have no batch): a network's pulse countries. So each outage is sized against them:
  every network has one outage on each pulse day, after 8 healthy pulses, in as many of the
  day's countries as the 24h uptime it aims at needs. On its worst day it is its tier's: 30 to
  90 minutes aiming at 99.5 to 99.95 % (most networks), 1 to 4 hours at 99.0 to 99.5 % (3 or
  4 networks), 6 to 10 hours at 98 to 99 % (1 or 2 networks); on its other days a calm one, 30
  to 90 minutes at 99.7 to 99.95 %. The worst days alternate between the networks (half on day
  one, half on day two) and the load factors move by day, so ranks change between #840's
  builds at the end of each day. The first long or major network that pulses on the last day
  keeps that day's outage open at the end ("ongoing"). No two networks show the same uptime (24h
  to 2 decimals, 7d and 30d to 4): a colliding network's outage takes one more country, or a
  slot, until they differ.
- **byteful** (no daily test on day 2) gets no pulse or load run that day: its 24h score is
  empty (`no_run_in_window`), and its 7d and 30d scores expire too (its newest measurement is
  more than `score_valid_hours`, 24, before the as-of). `--pulse-days byteful=1,2` gives it
  pulses on day 2 too, from its day-1 countries and IPs. `--plan-only` (`pulse_days`) and
  `state.json` say which way it ran; a resume with other `--pulse-days` is refused.
- **The as-of rule.** The calculation's as-of must be within 24 hours (`score_valid_hours`) of
  each network's newest measurement, or its score expires: use the end of the last day.

## Production as a second staging database (PROD-AS-STAGING-1)

Owner decision, 2026-11-10: "the real-data two-day test (#815) uses the production database
`proxy_reviews` as a second staging database. Production is not live yet, the owner has
approved the shared role and credentials", and "clean the db and make the db structure up to
date". `proxy_reviews_staging` (the 91 GB speed dataset) stays as it is.

```sh
export PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1
GO="owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>"
uv run python scripts/scale/clean_target.py --database <production toml> --out <dir> \
  --owner-go "$GO" --i-know-this-is-production            # dry run: read clean-dry-run.json
uv run python scripts/scale/clean_target.py --database <production toml> --out <dir> \
  --owner-go "$GO" --i-know-this-is-production --apply    # within the hour
uv run proxy-index db-migrate --database-file <production toml>   # the structure up to date
uv run python scripts/scale/simulate.py --database <production toml> --owner-go "$GO" \
  --i-know-this-is-production --real-hours 12,0 --real-hour-override byteful=16,none ...
```

- The guard (`target.py`) accepts `proxy_reviews` only with all three: the dated go, the
  acknowledgement and the environment variable. It prints a banner to stderr and records the
  go, the database, the role and the UTC time in the state (`go.production`) and the report
  (`production`). Without all three it refuses as before, naming the three.
- **Cleaning a target.** `clean_target.py` empties every table of the public schema except
  the kept ones, with one `TRUNCATE ... RESTART IDENTITY` (no `CASCADE`: a kept table that
  referenced an emptied one would fail it, never be emptied with it), and drops the emptied
  tables' partitions. Emptied: the measurement tables (runs, buckets, geo buckets,
  observations, IPs, enrichment, memberships, pools, scopes, products' measurement rows,
  estimated hours, batches, batch runs, test events and their buckets, outages, nodes, job
  runs, alerts, load references) and every calculated output (builds, snapshots and parts,
  lists and rows, windows, scores, series, overlaps, memos, roles, cohorts, labels). **Kept**,
  row for row: `settings`, `countries`, `targets`, `runner_regions`, `methodologies`,
  `schema_versions`, `calculation_schema`, `providers`, `products` (the seed) and the whole
  `site` schema (the catalog); the dry run's `keep` lists them.
- The dry run (the default) runs it in one REPEATABLE READ transaction and rolls it back. The
  transaction first locks the emptied tables, then takes the ingest's and the calculation's
  advisory locks, so its counts are the rows the delete removes. It prints, and writes to
  `<out>/clean-dry-run.json` (mode 600), per table the rows it would delete (`deleted`), the kept
  tables' rows (`kept`), the partitions and the go.
- `--apply` needs that file, of the same database and from the last hour; it runs the same
  transaction and commits only when every table's deleted rows equal the dry run's, else rolls
  back. Both refuse while a `job_runs` row started in the last 5 minutes (an orchestrator
  writes there), as `replay.py` does, and a database neither production nor staging.
- On `proxy_reviews_staging` it takes its own go, `--owner-go "owner-go CLEAN-STAGING
  <YYYY-MM-DD>"`, never the replay's or production's.

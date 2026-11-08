# The profile of the real 24-hour data (SIM-PROFILE-1)

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
  shape (the line names the file), or no run matches `--runs`.

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

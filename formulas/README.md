# Hour formulas

One JSON file per network (`<network>.json`) describes how many distinct exit IPs the network
showed in each UTC clock hour of a 24-hour campaign: in total, and per requested country. It also
gives the daily test schedule, meaning the hour at which each country is tested.
[SUMMARY.md](SUMMARY.md) has one row per network.

The files are public. They hold only counts, factors, hours and identifiers of the source data:
no IP address, hostname, credential or local path.

## How a file is made

```sh
# 1. Count (private input, one process, low priority): verified per-hour and per-country counts.
nice -n 19 python -m proxy_index.formula aggregate --enrich-dir <enrichment out dir> --out <counts dir>
# 2. Build the public files from the counts.
python -m proxy_index.formula build --aggregates <counts dir> --out formulas --version <n>
```

The input of `aggregate` is the hour enrichment of a campaign: one directory per network, holding
`manifest.json` (schema 2) and one gzip hour file per clock hour. Each hour file has one record per
distinct exit IP, with that IP's request rows per requested country. Every hour file is checked
against its manifest slot as it is read, and any mismatch stops the run; nothing is skipped or
repaired. The checks are:

- the compressed sha256;
- the record count equals the slot's `unique_ips`, with every IP distinct;
- the sum of `seen_count` equals the slot's `request_rows`;
- each record's per-country counts sum to its `seen_count`.

The counts files contain no IPs and stay private with the input. `aggregate` keeps an existing
counts file unless it is given `--force`. `build` is deterministic: the output has no timestamps,
so the same counts and version always give the same bytes. Increase `--version` whenever the input
campaign or the rules change.

## Definitions

- **Hour**: the UTC clock hour (00-23) of the round's scheduled start.
- **Country**: the requested country (the test's country parameter), not the country of the exit
  IP in a geolocation database.
- **Unique IPs**: the distinct exit IPs of the hour (or day) that served at least one request.
- **Requests**: request rows, meaning successful requests that reported an exit IP.
  `attempted_requests` (network level only) also counts the requests that failed or returned no IP.
- **Peak hour**: the hour with the most unique IPs. Ties go to the hour with more requests, then
  to the earlier hour.
- **Low hour**: the hour with the fewest unique IPs. Ties go to the hour with fewer requests, then
  to the earlier hour. A `missing` hour (no request at all) is never the peak or the low.
- **Hour factor**: the hour's unique IPs divided by the peak hour's unique IPs, so the peak is
  exactly `1.0`. A `missing` hour's factor is `null`. A `requests_no_ips` hour counts as 0 IPs, so
  its factor is `0.0`, and it is flagged in `status`.
- **Daily factor**: the whole-day unique IPs (the union of the 24 hours) divided by the peak hour's
  unique IPs. The day can be larger than any single hour because IPs rotate.

Factors are stored at full float precision. `round(factor * peak-hour unique IPs)` gives back each
measured hour and the whole day exactly (for a country, each hour whose `hour_basis` is `country`
or `country_low_sample`).

## Fallback to the network curve

A country uses its own curve only when its own data is large enough. Otherwise it uses the
network's all-country curve: its `curve` is `"network"` and `fallback_reason` says why. This
happens when either:

- its peak hour has fewer than `rules.min_peak_unique_ips` unique IPs (default 100), or
- it has IPs in fewer than `rules.min_hours_with_ips` hours (default 20).

The hour floor is capped at the network's own number of hours with IPs. Such a country keeps its
own measured counts, peak hour and low hour, but its `hour_factors`, `daily_factor` and `test_hour`
come from the network curve.

The 100-IP floor keeps the sampling noise of an hour to about 10% (1/√100). That is below the
day's swing of the network curves in version 1, where the low hour has a median 0.78 times the
peak's IPs (0.43 to 0.82 across the 17 networks with IPs in all 24 hours).

## Missing and low-sample country hours

A country on its own curve can still lack one of the network's covered hours (an hour in which the
network itself reported IPs, status `present`). The sample of a country hour is its request rows,
compared with the median request rows of the country's covered hours:

- no request row at all: `hour_basis` is `network_missing`;
- fewer than `rules.min_sample_share` (0.25) times the median: `network_low_sample`;
- fewer than `rules.low_sample_share` (0.5) times the median: `country_low_sample`, a flag only;
- otherwise `country`.

A `network_missing` or `network_low_sample` hour did not measure the pool, so its factor comes from
the network curve, scaled to the country's level: the network's unique IPs that hour, times the
country's unique IPs over the network's in the country's other covered hours, divided by the
country's peak-hour IPs, and at most 1.0 so that the measured peak stays the largest factor. Such an hour is never the country's peak or low hour, and its own counts
stay in `unique_ips` and `requests`. A `country_low_sample` hour keeps its own factor. The estimate
of a fallback hour scales the measured requests by the hour factor, not by the country's request
curve. A country on the network curve has `hour_basis` `network` in every covered hour; an hour
the network did not cover has `hour_basis` null and the network's factor (null or `0.0`).

In version 1 such hours had a factor of 0.0 or near it (shifter: US hour 08 had no US request row,
CA hours 18 and 23 about a twentieth of the usual rows), so an estimate for that hour was close to
0. Version 2 is the first built with these rules.

## Daily test schedule

`schedule` maps each country to its `test_hour`, the peak hour of the curve it uses. Testing at
the peak measures a pool at its largest. The hour factors then scale that one daily test to any
other hour, and the daily factor scales it to the whole day.

A network's formula is only as complete as the campaign behind it. Every new network gets a full
24-hour test first, and its formula is built from that test. A network whose `network_curve` has
fewer than 24 hours with IPs needs a new 24-hour campaign before its schedule means more than "the
hour that had data". In version 1 that network is **byteful**: its proxy answered every hour, but
reported exit IPs only in hour 16. Its other 23 hours are `requests_no_ips`, so every byteful
country is scheduled at 16.

## Latency

`latency.available` is `false`. The hour files hold one record per exit IP with its request count,
but no per-request latency. Per-cell percentiles from elsewhere cannot be merged exactly into hour
or country percentiles. The daily test measures latency.

## Schema (`schema` = `"proxy-index-hour-formula"`, `schema_version` = 1)

Lists marked [24] are indexed by UTC hour 0-23.

| Field | Type | Meaning |
|---|---|---|
| `schema`, `schema_version` | string, int | This document's format. |
| `formula_version` | int | Version of this network's formula (input and rules). |
| `network`, `product` | string | The network and its product (`residential`). |
| `source.campaign_runs` | [string] | Every campaign run id that contributed a round. |
| `source.hour_runs` | [24] of [string] | The run ids behind each hour. |
| `source.enrichment_manifest_sha256` | string | sha256 of the input `manifest.json`. |
| `source.dbip_release_sha256` | string | sha256 of the IP database release used by the enrichment. |
| `rules` | object | Hour and country basis, request definition, fallback thresholds, hour sample shares (`min_sample_share`, `low_sample_share`). |
| `latency` | object | `available` (bool) and `note`. |
| `network_curve.status` | [24] string | `present`, `requests_no_ips` (requests answered, no exit IP reported) or `missing`. |
| `network_curve.unique_ips`, `.requests`, `.attempted_requests` | [24] int | The hour's counts. |
| `network_curve.day_unique_ips` | int | Union of the day's IPs. |
| `network_curve.hours_with_ips` | int | Hours with at least one IP. |
| `network_curve.peak_hour`, `.low_hour` | int | See Definitions. |
| `network_curve.hour_factors` | [24] number or null | See Definitions. |
| `network_curve.daily_factor` | number | See Definitions. |
| `countries.<CC>.unique_ips`, `.requests` | [24] int | The country's own counts. |
| `countries.<CC>.day_unique_ips`, `.hours_with_ips` | int | The country's own day union and hours with IPs. |
| `countries.<CC>.peak_hour`, `.low_hour` | int | The country's own measured peak and low hour. |
| `countries.<CC>.curve` | string | `country` (own curve) or `network` (fallback). |
| `countries.<CC>.fallback_reason` | string or null | Why the network curve is used. |
| `countries.<CC>.hour_factors`, `.daily_factor`, `.test_hour` | as above | From the curve used. |
| `countries.<CC>.hour_basis` | [24] string or null | `country`, `country_low_sample`, `network_missing`, `network_low_sample` or `network`; see Missing and low-sample country hours. |
| `schedule` | object | Country to test hour (UTC). |

Country codes are the campaign's requested codes (ISO 3166-1 alpha-2).

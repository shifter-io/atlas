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
nice -n 19 python -m atlas.formula aggregate --enrich-dir <enrichment out dir> --out <counts dir>
# 2. Build the public files from the counts.
python -m atlas.formula build --aggregates <counts dir> --out formulas --version <n>
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
  to the earlier hour. An hour without IPs is never the peak or the low.
- **Hour factor**: the hour's unique IPs divided by the peak hour's unique IPs, so the peak is
  exactly `1.0`. An hour in which the campaign saw no exit IP, `missing` (no request at all) or
  `requests_no_ips` (requests answered, no exit IP reported), is no data: its factor is `null`,
  never `0.0`, and `network_curve.no_data_reasons` says why. Its estimate is "not estimated"
  (null figures, never stored), so no sum or average counts it as 0. Version 1 published `0.0`
  for `requests_no_ips` hours. A borrowed curve (below) has a factor for every hour instead.
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
country's peak-hour IPs, and at most 1.0 so that the measured peak stays the largest factor.
Such an hour is never the country's peak or low hour, and its own counts stay in `unique_ips` and
`requests`. A `country_low_sample` hour keeps its own factor. The estimate
of a fallback hour scales the measured requests by the hour factor, not by the country's request
curve. A country on the network curve has `hour_basis` `network` in every covered hour; an hour
the network did not cover has `hour_basis` null and a null factor.

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
hour that had data". That network is **byteful**: its proxy answered every hour, but reported
exit IPs only in hour 16. Its other 23 hours are `requests_no_ips`, so in version 1 every byteful
country was scheduled at 16 with a factor of 0.0 in every other hour. Since version 2 it has a
borrowed curve (below).

## Borrowed curve (temporary)

`build --borrow <network>` gives a network whose campaign measured too few hours the average
24-hour shape of all the other networks in the build (the donors). Version 2 borrows for
**byteful** only, until its own 24-hour data is fixed; rebuilding without `--borrow` then gives it
its own curve again. The current files are built with:

```sh
python -m atlas.formula build --aggregates <counts dir> --out formulas --version 2 --borrow byteful
```

- A curve's shape is its hour factors divided by their mean, so each donor weighs the same
  whatever its size. The borrowed shape S[h] of an hour is the mean of the donors' shapes that
  hour.
- A country uses the shape of the donors that have it on their own curve (`curve` `country`) when
  at least `borrowed.min_country_networks` (3) donors do; its `curve` is then `country`. Otherwise
  it uses the all-country shape, and its `curve` is `network`.
- The measured hour m (the measured peak; for byteful hour 16, its only hour with IPs) keeps its
  real unique IPs M and request rows R. Every other hour h is estimated as M x S[h] / S[m] (and
  R x S[h] / S[m]): `estimated_unique_ips` and `estimated_requests`.
- The peak hour (and test hour) is the hour where S is highest, the low hour where it is lowest,
  and the hour factors are S[h] / max(S).
- The daily factor is the median of the donors' daily factors (each a whole day over its own peak
  hour, so here a day over the borrowed peak hour). A country on its own donors' shape takes the
  median of those donors' daily factors for it; otherwise the all-country median. The median, not
  the mean, so one network with an unusually large day does not raise every borrowed day: in
  version 2 the 17 donors' daily factors run from 2.25 to 11.29, with a median of 3.11 and a mean of
  3.88.
- The measured counts (`unique_ips`, `requests`, `status`, `no_data_reasons`) stay as measured.
- The document has `borrowed` (`source`, `note` "borrowed: average of all networks, temporary
  until <network> data is fixed", donor `networks`, `min_country_networks`). Each country's
  `fallback_reason` repeats the note, and every `hour_basis` is `borrowed`, so the estimates scale
  requests by the hour factor. SUMMARY.md marks the network "(borrowed curve)".

Worked example (made-up numbers). The donors' average shape has S[16] = 1.10, a highest
S[20] = 1.25 and a lowest S[04] = 0.80, and their daily factors are 2.0, 2.4, 2.6 and 9.0. The
network measured M = 1,000 unique IPs in hour 16. Then:

| Hour | Estimated unique IPs | Hour factor |
|---|---|---|
| 16 (measured) | 1,000 | 1.10 / 1.25 = 0.88 |
| 20 (peak) | 1,000 x 1.25 / 1.10 = 1,136 | 1.0 |
| 04 (low) | 1,000 x 0.80 / 1.10 = 727 | 0.80 / 1.25 = 0.64 |

The daily factor is the median of the donors' daily factors, (2.4 + 2.6) / 2 = 2.5 (their mean,
4.0, is pulled up by the one 9.0), so the day is about 1,136 x 2.5 = 2,840 unique IPs. A daily test
at the test hour 20 that measures 1,100 IPs gives 1,100 x 0.64 = 704 for hour 04.

## Even schedule (proxy-cheap)

proxy-cheap holds few simultaneous connections (owner decision 2026-11-15, PROXY-CHEAP-BUDGET-1):
everything that tests it in one hour shares 250 connections, so its daily tests cannot sit on one
hour (199 of its 216 countries were on 13:00 UTC). Version 3 of its formula is its version 2 with
an even schedule, written by:

```sh
python -m atlas.formula spread --formulas formulas --network proxy-cheap
python -m atlas.formula best-hours --formulas formulas/ --out formulas/best-hours/
```

- A country's best hour is the hour most other networks chose as their best hour for it
  (`best-hours`' rule), counting only their countries without a `fallback_reason` (on their own
  curve); borrowed formulas (byteful) are no donors. 180 of the 216 countries have one; the other
  36 small territories have none.
- At most ceil(216 / 24) = 9 countries an hour, each as few hours from its best hour as possible:
  the least total distance around the clock (a min-cost transportation). Countries without a best
  hour fill the hours left over, the quietest ones. Of the countries sharing a best hour, those
  most donors agree on keep it (ties: the country code). The result is deterministic: every hour
  has 9 countries, the total shift is 228 hours over the 180 countries, at most 9 hours (JE, best
  hour 20, is tested at 05). The command prints the 24-hour table, the total shift and the
  countries moved most.
- The hour curves are borrowed from the other networks (as `build --borrow`, above), so an
  estimate scales each test from its own hour. The day estimate stays the formula's own: proxy-
  cheap's 24-hour data is complete, so each curve's daily factor is rescaled to the borrowed peak
  (old factor x M / E, M the measured peak's unique IPs, E the borrowed peak's estimate). The
  note is "borrowed: average of all networks; proxy-cheap is tested on an even 24-hour
  schedule".
- `spread` records the rule, `capacity`, each country's `best_hours` (null without one),
  `total_shift`, `max_shift` and, as `built`, the daily factors version 2 was built with (the network's and those of its own-curve countries: byteful's borrowed medians took them as a donor); `schedule` and each `test_hour` are the spread hours. A rebuild
  from the counts (`build`) replaces all of it, so rerun `spread` after one; it refuses a formula
  that is already borrowed or spread.

## Best hours

`formulas/best-hours/<network>.json` gives each country's best hour: the UTC clock hour with the
most unique live IPs, where an IP is live in an hour when it answered at least once in it. The
replay puts each country's daily test in that hour, and production starts a country's tests at
it. The files are made from the formulas (counts only, no IP):

```sh
python -m atlas.formula best-hours --formulas formulas/ --out formulas/best-hours/
```

- Over all 24 hours of the country's own `unique_ips`, the best hour has the most unique IPs.
  Ties go to more request rows (`requests`), then to the earlier hour. Unlike `peak_hour`, no
  low-sample hour is left out; in version 2 the two agree for every own-curve country.
- A borrowed formula (byteful) has IPs in too few hours for that. Each of its countries takes the
  hour most of the other networks chose as their best hour for the same country (the mode). Ties
  go to the larger sum of the choosing networks' unique IPs at that hour, then to the earlier
  hour. A country no other network has keeps the formula's borrowed `peak_hour`.
- A country with no IP in any hour has no best hour and is listed under `no_ips`.
- The daily planner still reads `schedule` (`test_hour`), which is the network curve's peak for a
  country on the network-curve fallback.

| Field | Type | Meaning |
|---|---|---|
| `schema`, `schema_version` | string, int | `"atlas-best-hours"`, 1. |
| `network`, `rule` | string | The network and the rule above in words. |
| `hours` | object | `"00"` to `"23"`, each a list of country codes (empty when none); every country is in exactly one hour. |
| `countries.<CC>.hour` | int | The best hour (UTC). |
| `countries.<CC>.unique_ips` | int | The network's own measured unique IPs of the country in that hour (for a borrowed formula often 0). |
| `countries.<CC>.basis` | string | `own` (its own curve) or `borrowed` (the other networks' choice). |
| `countries.<CC>.donors` | int | Borrowed only: how many other networks have the country with IPs; 0 for `own`. |
| `no_ips` | [string] | Countries with no IP in any hour. |

## Latency

`latency.available` is `false`. The hour files hold one record per exit IP with its request count,
but no per-request latency. Per-cell percentiles from elsewhere cannot be merged exactly into hour
or country percentiles. The daily test measures latency.

## Schema (`schema` = `"atlas-hour-formula"`, `schema_version` = 1)

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
| `countries.<CC>.hour_basis` | [24] string or null | `country`, `country_low_sample`, `network_missing`, `network_low_sample`, `network` or `borrowed`; see Missing and low-sample country hours. |
| `network_curve.no_data_reasons` | [24] string or null | Why the campaign has no data for the hour (its factor is null, unless the curve is borrowed); null for an hour with IPs. |
| `borrowed` | object | Present only for a borrowed curve: `source`, `note`, donor `networks`, `min_country_networks`. |
| `spread` | object | Present only for an even schedule (proxy-cheap): `rule`, `daily_factor`, `capacity`, `best_hours`, `total_shift`, `max_shift`, `built`; see Even schedule. |
| `network_curve.estimated_unique_ips`, `countries.<CC>.estimated_unique_ips` | [24] number | Borrowed curve only: the anchored hourly estimate (the measured hour exact). |
| `schedule` | object | Country to test hour (UTC). |

Country codes are the campaign's requested codes (ISO 3166-1 alpha-2).

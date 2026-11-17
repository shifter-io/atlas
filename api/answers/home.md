# `/v1/home`

The Atlas home's live dashboard in one request per range: the market panel, the four
insight cards, every network's panel with its score curve and sparks, each tape tab's newest rows and the
ranking profiles. Before this answer these were only in the dataset file (`/v1/dataset`, about
9 MB) or took about 37 requests. Map: `docs/api-web-map.md`, section 1 and section 11, W6
(HOME-4, HOME-6 to HOME-18, HOME-21, HOME-22, HOME-25, HOME-32, HOME-33, HOME-43, HOME-51) and
its follow-up W6b, the insight cards (HOME-27, HOME-29 to HOME-31, HOME-35, HOME-47).

- **Route:** `GET /v1/home?range=`; `range` is `24h`, `7d` or `30d` (default `30d`). Another
  parameter answers 400 `unknown_parameter`, a bad range 400 `invalid_parameter` (`api/README.md`, "Endpoints").
- **Bodies:** one per range, stored as `/v1/home/<range>`, computed once per build.
- **Size:** about 34 KB per range at the QA-1 scale (18 measured networks, 21 rows) and 32 KB
  with the week's 18 networks, every network priced and every insight card full
  (`TestHomeAtQA1Size` logs both), against the 200 KB cap.
- **Code:** `api/internal/build/home_answer.go`, the insight cards in `home_insights.go`. It
  reads only what the build already holds: the measured market views (`measuredMarket`), the
  window statistics, the rankings (W2), the target benchmarks, the all-networks answer, the
  overlap facts (W5) and the dataset's targets, tape and profiles.

## Fields

| Field | Type | Source |
|---|---|---|
| `generatedAt`, `range` | ISO time, range id | The build time and the request. |
| `market` | object | The All market panel (below). |
| `insights` | object | The four insight cards (below). |
| `networks[]` | object | Every network, in the rankings' order (`/v1/rankings?range=`), below. |
| `times` | list of number lists | Every distinct times list of the networks' series, once (SPARK-TIMES-1): each time a number, in minutes before the build's as-of time (the windows' end, `generatedAt` when a snapshot is published; the unit of the network chart's `t` and the rankings' `trendT`), oldest first. A network series' points map to `times[<its timesIndex>]` (SERIES-TIMES-INDEX-1), so networks on the same schedule share one list. Every network `successScoreCurve` of the range uses the same entry, the range's curve axis (`networks[]`, below). The answer has no English point labels (TIME-LABELS-REMOVE-1, #988): the website words a time from the number. |
| `tape[]` | `TapeEvent` (`id`, `kind`, `event`, `providerSlug`, `providerName`, `product`, `signal`, `agoSeconds`, `at`, `source`, `na`, `country`, `region`, `target`, `severity`, `values`) | The newest 10 rows of each tape tab (`kind` `census`, `pulse`, `incident`, `audit`, the website's tab ids) among the dataset's tape (the measured test events, one row per incident as in `/v1/events` (`answers/events.md`, EVENTS-MERGE-1: an outage seen by two targets is one row), mapped by `TapeFromEvents`, and the editorial rows), merged newest first: at most 40 rows, counted after the incidents are merged. A tab without events has no rows; rows of another `kind` are left out. The build reads each tab's newest 10 events with one query per tab (`store.Inputs.TapeHeads`, HOME-TAPE-1; 40 for the incident and audit tabs, so 10 merged rows are read whole), so a burst of one tab (the hourly census releases) never empties the others. A client ages them from `at`. Each row carries the event's `country`, `region`, `target` and `severity` and its numbers in `values`, the same object as the event's `/v1/events` row (`FIELDS.md`, "Tape and event values", TAPE-VALUES-1); `title`, `detail` and `value` stay until the website reads them. `/v1/networks/{slug}` `tape[]` takes the same quota among that network's rows (NETWORK-TAPE-QUOTA-1, `FIELDS.md`). |
| `profiles[]` | `id`, `weights`, `na` | `Dataset.profiles` (`site.ranking_profiles`). No `label` (#989): a client words a profile by `id` (`/v1/codes` set `ranking_profile`). |
| `na`, `naReason`, `naReasons` | paths, codes | The market's and insights' nulls, and `profiles` or `insights.hardestRates` when empty, each with its reason (`answers/README.md`, "Missing data"): no list rate is `catalog` (`market.cheapest`, `insights.valueAll.best`), no latency test `not_measured` (`market.medianLatency`, `market.fastest`). Each `networks[]` row, tape row and profile carries its own. |

### `market`

Every figure follows the range. The fleet is every network with a published headline group.

| Field | Type | Source |
|---|---|---|
| `requests` | integer | The all-networks answer's `ranges.<r>.requests` (`/v1/networks/all`): every product group's completed requests, the same figure as the hero (HOME-52). |
| `observedDays` | days | The fleet history's oldest point to the window end, rounded up; 0 under a day; null when the history covers the range (the old site's `observedDays`). |
| `networks` | integer | Networks with a published headline group. |
| `targets` | integer | Targets with `source` `measured` (`targetCounts.measured` of `/v1/rankings` and `/v1/meta`). |
| `successScoreCurve` | `{values, times}` | The fleet success curve: **24 points for 24h, one per UTC hour; 7 for 7d and 30 for 30d, one per UTC day**, the newest hour or day partial with what exists (CURVE-HOURLY-DAILY-1, #1301; was 110 points for 24h and every 5-minute and day point otherwise). Each value is `1 + 9 ×` the bucket's rate, on the 1-10 scale, absolute as `successScore`, 2 decimals ("Size"): the rate is every fleet network's history points in the hour or day pooled by requests, Σ successes ÷ Σ completed requests (each point is a series row's request ratio, weighted by its completed requests, not a plain mean of the points). Null in a bucket without requests (a gap, never 0); no other minimum. `times` is each bucket's start in minutes before the build's as-of time (SPARK-TIMES-1). The rule: `FIELDS.md`, "Score curves". The rate history `curve` was removed on 2026-11-14 (SUCCESS-CURVE-1, #1072). |
| `medianLatency` | ms | The median of the networks' headline latency p50 (`measuredMarket` `fleet.medianLatency`). `medianSuccess` was removed on 2026-11-14 (#900): `successScore` is its score. |
| `successScore` | `{score, status, basis}` | The market's 1-10 success score, absolute: `score` = `1 + 9 ×` the median of the networks' range success rates (internal, not served), 1 decimal (91.9% gives 9.3); `status` `ranked`, or null with `score` when that median is null; `basis` `"median_success_rate"`. Not a median of the networks' scores, which are distances from the best network of the kind. `FIELDS.md`, "Success scores". |
| `uniqueIps` | integer | The all-networks answer's `ranges.<r>.uniqueIps`: the snapshot's every-group fleet entry, each exit IP once. Null without that entry. |
| `uniqueIpsCurve` | `{values, times}`, 24 points (24h), 7 (7d), 30 (30d) | The fleet's distinct exit IPs per UTC clock hour (24h) or UTC day (7d, 30d) over the whole range, each IP once however many networks saw it (the every-group fleet entry's `ip_slots`), measured like `uniqueIps`, so `uniqueIps` is at least any point. `times` is each slot's start in minutes before the build's as-of time (SPARK-TIMES-1); the last slot is the current hour or day, partial (empty in a build at its start); the same UTC hours and days, and `times`, as `successScoreCurve` (#1301). `null` where a slot has no observed IP (before the data starts too), never 0. Empty lists for a snapshot calculated before it. Not the sum of the networks' chart `pool` points, which are estimates and count a shared IP once per network (`FIELDS.md`, "Unique IPs per hour or day"; FLEET-POOL-SERIES-1, #1302). |
| `bandwidthGb` | GB (10⁹ bytes) | The sum of the headline windows' `metrics.bytes_up + bytes_down` ÷ 10⁹, so the sum of `networks[].bandwidthGb`; null when a network has no group. |
| `latencySpark` | `{values, times}`, 24 points for 24h, 7 for 7d, 30 for 30d | The market latency per UTC hour (24h) or day (7d, 30d) over the whole range, on `successScoreCurve`'s times (CURVE-HOURLY-DAILY-1, #1301), with the same definition as `medianLatency`: each value is the median of the fleet networks' latency p50 of that hour or day (each network's own series row: the pulses' normal requests on `generic_http`, timed by the calculator over the hour's or day's rows, its merged histograms, SERIES-HOURLY-DAILY-1), networks without one skipped; null with fewer than 3 such networks, and before the data starts. Whole ms ("Size"). It was 24 slots picked from the 5-minute history, each the mean of the networks pulsed in that 5-minute slot, so which networks happened to be pulsed decided the value. `successSpark` was removed on 2026-11-14 (#900): `successScoreSpark` replaces it. `times` is each bucket's start in minutes before the build's as-of time, as the answer's `times` (SPARK-TIMES-1). |
| `successScoreSpark` | `{values, times, statuses}`, 24 / 7 / 30 points | `market.successScore`'s rule per UTC hour or day over the whole range, on `successScoreCurve`'s times (#1301): `1 + 9 ×` the median of the fleet networks' rates of the hour or day (each network's requests-weighted rate then), 1 decimal; each status `ranked`, or null with the value in an hour or day without a rate (was 24 points picked from the 5-minute history). `FIELDS.md`, "Success scores instead of success rates". |
| `successScoreDelta` | score points | The market success history's change on the 1-10 scale: 9 × (the closing fifth's mean rate − the opening fifth's), as a network's success change (internal) compares them, 2 decimals; unobserved points left out; null below two observed points. |
| `fastest` | `{slug, name, value}` or null | The network with the lowest latency p50 (value in ms); the first in catalog order wins a tie. |
| `mostReliable` | `{slug, name, score, status}` or null | The network with the highest overall `successScore` in the range (#898), a tie (also across kinds) to the higher success rate (internal), then the first in catalog order; `score` and `status` are its score's. Without any score: the network with the highest success rate, with a null `score`. Its `value` (the rate) was removed on 2026-11-14 (#900). |
| `cheapest`, `dearest` | `{slug, name, value}` or null | The lowest and highest list rate (`pricePerGbAtScale`, else `pricePerGb`, in $/GB); unpriced networks are left out; null when none is priced. |
| `spreadAll` | `{value, best, worst}` or null | The 8-target bills of `insights.valueAll` (VALUE-ALL-2): `value` = `insights.valueAll.spreadX`, `best` and `worst` = `{slug, name}` of `insights.valueAll.best` and `worst`. Null without a complete priced row (`incomplete_targets`; `catalog` when no network is priced). The HTTP bill's `spread` was removed on 2026-11-13 (item 14, `FIELDS.md`). |
| `authenticity` | fraction | Over the range's non-datacenter windows, `pool.geo_accuracy` (exit IPs in the requested country) weighted by `pool.unique_ips`. Null when none publishes it. |
| `countries` | integer | The all-networks answer's `ranges.<r>.exitCountries`: distinct exit countries in the range. |
| `incidents` | integer | Σ the networks' `stats.<r>.incidentCount`. |
| `misSold` | fraction | Over the same windows as `authenticity`, `pool.datacenter_asn_share` (exit IPs on hosting ASNs) weighted by `pool.unique_ips`. Datacenter networks are left out: they are not sold as residential. |
| `duplication` | fraction | The residential overlap market's `duplicatedShare` (`/v1/overlap?kind=residential&range=`): the share of the pools' summed IPs counted twice. Null with `insufficient_data` when the snapshot publishes no residential fleet entry (`*:residential`), as that answer's. |
| `likelyResellers` | integer | How many residential networks have a likely upstream under the /v1/overlap rule (`market.likelyResellers` there). |

### `insights`

The old site's `overviewInsights` (`overview.ts`) for the range. Like the old site, the cards
read the measured networks only, from their ranking rows of the range (`/v1/rankings?range=`,
W2). Every list may be empty; a figure the build does not have is null, never invented.

| Field | Type | Source |
|---|---|---|
| `authenticity[]` | `{slug, name, value}` | The top 5 measured networks by `authenticity` (the share of exit IPs in the requested country), highest first, a null value last; datacenter networks are left out. |
| `hardest` | `{id, difficulty, engine}` or null | The measured target (`source` `measured`) with the highest difficulty: the catalog's, else its scoring weight, as `/v1/targets` gives it; the first in the dataset's order wins a tie. Null when no measured target has a difficulty; a catalog-only target is never chosen. `engine` is `http` or `browser`. No `label` or `passCondition` (#989): a client words the target by `id` (`/v1/codes` set `target`). |
| `hardestRates[]` | `{slug, name, engine, scores}` | Every measured network's result on `hardest` in the range (its headline window's target benchmark, as `profile.targets[]` of `/v1/networks/{slug}`), highest first; a network without a rate is left out, and the list is empty when `hardest` is null. `engine` is the browser engine whose tries make the figure (the one with the most completed tries; null when the snapshot names none), `http` on a network target. `scores` is `{success: {score, rank, rankedOf, status}}`, that engine's rank-based success score (a network target's own success score, `http`); null without scores. |
| `exclusivity[]` | `{slug, name, value, exclusiveIps, exclusiveScore, poolIps, upstream}` | The top 5 measured networks by `exclusiveScore` (the 1-10 score of their exclusive IPs against the leader of their kind, `FIELDS.md` "Exclusive score"), highest first, an equal score by name, a null score last (EXCLUSIVE-SCORE-1, #1242; was by `exclusiveShare`, so a small pool with a high share beat a big pool with more IPs of its own). `value` is still `exclusiveShare` (the share of the pool no other network of its kind shares). `exclusiveIps`, `exclusiveScore` and `poolIps` (the range's `uniqueIps`, and `exclusiveIps` that × `exclusiveShare`; EXCLUSIVE-ESTIMATE-1, #1283) are the network's on `/v1/overlap?kind=<its kind>&range=`; null without that row's. `upstream` is `{slug, name, shareOfOurs}`, the network's `likelyUpstream` there (W5); null when it has none. |
| `valueAll` | object | The "Best value for money" card: every measured residential network's bill per 1,000 successful requests over the 8 card targets (below, VALUE-ALL-1). It replaced the plain-HTTP `value`, removed on 2026-11-13 (item 14, `FIELDS.md`). |

### `insights.valueAll`

Each measured residential network's cost per 1,000 successful requests, averaged with equal
weights over the card targets (`api/internal/build/value_all.go`). One target's cost in the range's
residential window is

    costPer1k = listRate × (bytes_up + bytes_down) ÷ 10^9 ÷ successes × 1000

with `listRate` the `$ / GB` the `/v1/targets` cells use: the cost basis, the price of the
network's first plan of 200 GB or more, else of its largest plan (COST-BASIS-200GB-1, `api/FIELDS.md`,
"The cost basis"), not the displayed `pricePerGb` or `pricePerGbAtScale`; and GB = 1,000,000,000 bytes, the GB networks sell (the framework's `traffic.py`). The bytes are
every finished try's, failed ones too, so the cost carries the traffic that failed tries use. Not
rounded: the API serves the full number (`api/FIELDS.md`, "The 8-target bill").

**Test days: bytes are generated, not measured; the website should mark the card as test data.**

| Field | Type | Source |
|---|---|---|
| `unit`, `gbBytes`, `weighting` | `"usd_per_1k_successes"`, `1000000000`, `"equal"` | Constants. |
| `targets` | target ids | The card targets (`source` `measured`, outside any group), in dataset order: today `generic_http`, `generic_https`, `amazon_pdp`, `google_search`, `linkedin_public`, `social_web`, `cloudflare_challenge`, `ai_answers`. |
| `best`, `worst` | `{slug, name, costPer1k}` or null | The first and the last complete priced row (a lower bound counts as complete); null without one. |
| `spreadX` | ratio or null | `worst.costPer1k ÷ best.costPer1k`; null without a complete priced row or when `best` is 0. |
| `incomplete` | integer | Priced rows missing a target. |
| `unpriced` | integer | Rows without a list rate. |
| `rows[]` | object | Every measured residential network (below). |

Each row:

| Field | Type | Source |
|---|---|---|
| `slug`, `name` | | The ranking row. |
| `priceBasis`, `basisPlanGb` | `first_200gb_plus` or `largest_below_200gb`, GB; or null | Which plan the row's cost basis is and its size (`api/FIELDS.md`, "The cost basis"); null (`catalog`) without basis evidence. |
| `byTarget.<id>` | `{costPer1k, tries, successes, mbPerTry}` | Every card target's cost in the window (with the methodology's `[value] browser_window`, a browser card target's over that window: the 24h row costs its browser targets over the last 7 days, COVERAGE-ALERT-1). `tries` is `completed`, `successes` the window's successes, `mbPerTry` the bytes ÷ tries ÷ 10^6 (null without tries or bytes); these are set whenever the data exists, also without a list rate. |
| `targetsCounted` | integer | Card targets with a cost of their own (the published figure, as `costPer1k`). |
| `missingTargets` | target ids | Card targets without a cost of their own for a reason other than `catalog` (the published figure, as `costPer1k`). |
| `lowSampleTargets` | target ids | Counted targets with fewer than 20 successes: still in the bill, for the website to flag. |
| `costPer1k` | $ or null | The plain mean of the card targets' costs: the one the range's value component publishes (VALUE-ALL-RANK-1), a lower bound when a target has no success (`costIsLowerBound`); in a build from before it, the API's own, only when every one has a cost. |
| `costIsLowerBound` | boolean | `true` when `costPer1k` counts a target without a success at the highest cost any residential network has on it in the window (`FIELDS.md`, "The lower-bound bill"): the network costs at least that much. `false` otherwise. |
| `carriedOverTargets` | `{<id>: hours}` | The browser card targets with no run in the range whose cost comes from their run in the last 7 days, with the hours since that run (`FIELDS.md`, `carriedOverTargetsAllTargets`); `{}` when none. |
| `partialCostPer1k` | $ or null | The mean over the counted targets, set only when some but not all have a cost (null by contract otherwise). |
| `na`, `naReason`, `naReasons` | paths, codes | The row's own nulls, paths relative to the row: `costPer1k` is `catalog` without a list rate, else `incomplete_targets`; each `byTarget.<id>.costPer1k` has its own reason, the first that applies: `no_run_in_window` (no try), `no_successes` (tries but no success), `not_measured` (no bytes), `catalog` (no list rate). |

`rows` order, stable: complete priced rows (a lower bound included) by `costPer1k`, cheapest first; then incomplete priced
rows, most `targetsCounted` first, then `partialCostPer1k`, cheapest first; then unpriced rows.
The home's own `na` lists a null `best`, `worst` or `spreadX` (`incomplete_targets` when every
priced row misses a target, `catalog` when none is priced) and never the rows' paths.

An example row (illustrative values from the 30d test days, cut to 6 decimals here only; the API
serves the full numbers):

```json
{"slug":"geonode","name":"Geonode","costPer1k":1.267928,"partialCostPer1k":null,
 "targetsCounted":8,"missingTargets":[],"lowSampleTargets":[],
 "byTarget":{
  "generic_http":{"costPer1k":0.000215,"tries":2388378,"successes":1781113,"mbPerTry":0.0006},
  "generic_https":{"costPer1k":0.000215,"tries":1046400,"successes":778481,"mbPerTry":0.0006},
  "amazon_pdp":{"costPer1k":1.432586,"tries":100,"successes":85,"mbPerTry":4.51},
  "google_search":{"costPer1k":1.246936,"tries":100,"successes":32,"mbPerTry":1.4779},
  "linkedin_public":{"costPer1k":1.301805,"tries":100,"successes":28,"mbPerTry":1.35},
  "social_web":{"costPer1k":1.953566,"tries":100,"successes":51,"mbPerTry":3.6901},
  "cloudflare_challenge":{"costPer1k":2.263126,"tries":100,"successes":40,"mbPerTry":3.3528},
  "ai_answers":{"costPer1k":1.944976,"tries":300,"successes":98,"mbPerTry":2.3532}},
 "na":[]}
```

A complete row's `na` is empty and it has no `naReason` or `naReasons`. A lower-bound row has the
same shape: Byteful on the same data has `costPer1k` set with `costIsLowerBound` true,
`missingTargets` `["google_search","cloudflare_challenge"]` (both `no_successes` in `byTarget`),
`lowSampleTargets` `["linkedin_public"]` (2 successes), `targetsCounted` 6 and `partialCostPer1k`
the mean of its other 6. `/v1/rankings` rows and the network answer's `stats.<range>` carry the
same figures as `costPer1kAllTargets`, `costIsLowerBound`, `partialCostPer1kAllTargets`,
`targetsCountedAllTargets` and `missingTargetsAllTargets` (VALUE-ALL-PARTIAL-1, VALUE-ALL-RANK-1,
`FIELDS.md`), from the same published figures, so they never disagree. A null bill is N/A with
its reason, never 0.

### `networks[]`

The scalar figures are the network's ranking row of the range (`/v1/rankings?range=`, W2), as
it is there; only the score curve, the sparks, `lightLatencyP50`, `underLoadLatencyP50`, `claimed`, `claimShare`
and `incidents` are added, with the row's own `na`, `naReason` and `naReasons`.

| Field | Source |
|---|---|
| `slug`, `name`, `kind`, `standing`, `score`, `grade`, `breakdown` | The ranking row. |
| `bandwidthGb` | The ranking row's: the range window's `metrics.bytes_up + bytes_down` ÷ 10⁹ (GB, decimal); the rows sum to `market.bandwidthGb`. Null when the window recorded no bytes (`no_run_in_window` without a run, `not_measured` with runs but no bytes). |
| `requests`, `observedDays`, `latencyP50`, `latencyP90`, `latencyP99`, `latencyDeltaPct`, `latencyDeltaBasis`, `latencyDeltaAsOf`, `uniqueIps`, `countries`, `cities`, `listRate`, `valueRankAllTargets`, `authenticity`, `datacenterAsnShare`, `datacenterIps`, `browserOverall`, `successScore`, `successScoreDelta` | The ranking row (null `browserOverall` when the range has none). `successScore` is the network's 1-10 success score, `{score, rank, rankedOf, status}`: its daily tests' success by requests scored by its distance from the best network of the kind (SUCCESS-DAILY-DISTANCE-1), null in a build without one (`FIELDS.md`, "Success scores"); the best network of the kind and range reads 10 and the others keep their measured distance (`FIELDS.md`, "The best network reads 10"); its `targets` and `weights` are left out here for room (the network page's `stats.<r>.successScore` and `/v1/targets` serve them). `successScoreDelta` is its `score` minus the previous published build's, in score points, unrounded; null with `no_score_to_compare` when either build has no ranked score (`FIELDS.md`, "Success score deltas"). `datacenterIps` is `datacenterAsnShare`'s numerator, the range's exit IPs with a hosting usage type (`FIELDS.md`, window statistics), null with the share's reason. |
| `lightLatencyP50` | `stats.<r>.lightLatencyP50`: the light-load latency (the pulses'), equal to `latencyP50` since HEADLINE-PULSE-1. |
| `underLoadLatencyP50` | The ranking row's `underLoadLatencyP50`: the daily tests' latency under load, display only (HEADLINE-PULSE-1). |
| `claimed` | The catalog's claimed pool size (`site.trust_signals.claimed_pool_size`); null when not filled. |
| `claimShare` | `uniqueIps ÷ claimed`; null when either is unknown. |
| `incidents` | `stats.<r>.incidentCount`. |
| `successScoreCurve` | `{values, timesIndex}` (`timesIndex` the index of its times list in the answer's `times`, SERIES-TIMES-INDEX-1): the network's measured success history (internal: the range window's own rows since #1301, no finer window merged in) as **24 hourly points for 24h, 7 and 30 daily points (UTC days) for 7d and 30d** (CURVE-HOURLY-DAILY-1, #1301; was the range's grid of every history time, thinned to 84 for 24h), each `1 + 9 ×` the hour's or day's rate, the network's own series rows of the range's window pooled by requests (Σ successes ÷ Σ completed), absolute as `market.successScore`, the newest bucket partial. One time axis for every network: every curve has the same `timesIndex` and length, `null` in an hour or day without requests (its gaps); a network with no history has an empty curve. Values to 2 decimals ("Size"). The absolute scale tracks the network's own history over time; `successScoreSpark` scores each hour or day as the headline (`successScore`) is scored. The rate history `curve` was removed on 2026-11-14 (SUCCESS-CURVE-1, #1072). |
| `latencySpark` | `{values, timesIndex}`, on `successScoreCurve`'s axis and `timesIndex` (24 / 7 / 30 points over the whole range, #1301; was at most 22 points picked from the history): the network's latency p50 of each UTC hour or day, its own series row (the calculator's merged histograms, SERIES-HOURLY-DAILY-1), in whole ms ("Size"); null in an hour or day without a row, and in one with several rows (a snapshot from before SERIES-HOURLY-DAILY-1, whose 5-minute percentiles are never averaged). Empty for a network with no history. `successSpark` was removed on 2026-11-14 (#900). |
| `successScoreSpark` | `{values, timesIndex, statuses}`, on `successScoreCurve`'s axis and `timesIndex` (24 / 7 / 30 points, #1301; was at most 22 picked points): each UTC hour's or day's Success x/10, computed exactly as the row's `successScore` (SUCCESS-POINTS-1): the calculator's rule over that hour's or day's daily tests (success by requests, `max(floor, 10 − slope × (best − yours))`, 100-request minimum), the best the kind's best network then, so a steady network's line reads its headline. It was the point's rank among the networks with a point then, on all its requests, which put 1.5 to 4.4 points under a 9.9 headline. A value is null unless its status is `ranked`: `no_ranked_targets` without 100 daily-test requests in the hour or day, `not_enough_data` without a row (or with several: a snapshot from before #1301). Values to 2 decimals ("Size"). |
| `atlasScoreSpark` | `{values, statuses, timesIndex}` (`timesIndex` an index into the answer's `times`): the network's Atlas Score trend, its ranking row's `atlasScoreSpark` (`/v1/rankings`, times `atlasScoreSparkT`): the Balanced composite at 1 decimal, 24 hourly points for 24h, 7 and 30 daily ones for 7d and 30d, the last the row's own `score`. A value is null unless its status is `ranked` (`no_history`: no published build's score for that hour or day; `not_scored`: that build published none). Empty lists for a snapshot from before it. `FIELDS.md`, "Atlas Score trend" (ATLAS-SCORE-SPARK-1). |

Without a published group the series are the window statistics' own (empty for a network with
no measurement).

## Size

Every answer is at most 200 KB (`build.MaxAnswerBytes`). HOME-SIZE-1 (#905): production's
build 17 made `/v1/home` 7d 215,345 bytes with #898's score sparks, and siteapi refused it. Two
changes bring it to 155,942 bytes (24h 116,165; gzip 7d about 28 KB):

- The rows' `successScore` is `{score, rank, rankedOf, status}`, without `targets` and `weights`
  (−7,858 bytes per range); the network page (`stats.<r>.successScore`) and `/v1/targets` serve them.
- The chart series are rounded, as they are drawn and not read as figures: `curve` values
  to 4 decimals (0.01-point resolution; the score curves since #1072 to 2 decimals), `latencySpark` to whole
  ms, the rows' `successScoreSpark` to 2 decimals, for the rows and `market`. Headline figures
  (`successScore.score`, `successScoreDelta`, latencies) stay unrounded.

SPARK-TIMES-1 (#964) adds each point's time (`times`). One `times` list per network curve
would have made the full-tape fixture's 7d 198,147 bytes (over its 190 KB guard; 178,777
without times); the networks' curves on one shared grid make it smaller instead (one label
and times list for all of them, `null` at a network's gaps): with the full tape 24h 103,297
(was 118,476), 7d 134,989 bytes (was 178,777); without it 84,037 and 115,729 bytes.
SERIES-TIMES-INDEX-1 (#985) adds `timesIndex` to the four series of each network: about
1,100 bytes per range (with the full tape 24h 104,397, 7d 136,089 bytes; without it 85,137
and 116,829 bytes).
TIME-LABELS-REMOVE-1 (#988) removes the English point labels (the answer's `labels`, the
market series' `labels` and each network series' `labels` index): at the size test's shape
(below) 24h 76,104, 7d 102,760 and 30d 102,761 bytes; the QA-1 shape's home 58,287 bytes.

CURVE-HOURLY-DAILY-1 (#1301) serves the score curves as 24 hourly or 7 and 30 daily points (were
84 network and 110 market points for 24h, about 250 in 7d and 30d at the size test's shape): at
that shape 24h 71,287, 7d 70,190 and 30d 72,652 bytes; with the full tape 89,815, 88,718 and
91,180 bytes.

`TestEveryHomeBodyFitsWithRoom` (`home_size_test.go`) builds 18 networks at the week shape with
each network's own series gaps (`testscale.Shape.Gaps`: a times list per network, as in
production; about 250 curve points in 7d and 30d) and fails when a range is over 128 KB (180 KB
before #988).


TAPE-VALUES-1 (#906) adds `country`, `region`, `target`, `severity` and `values` (the 8 keys of
every row and the kind's own) to each tape row: about 240 bytes a row, 336 to 574 at the real
shape, so a full 40-row tape grows from 13,443 to 22,966 bytes. `TestEveryHomeBodyWithAFullTapeFits`
(`home_tape_size_test.go`) builds the same fixture with a full tape (`testscale.TapeEvents`: each
tab's newest 10 rows, of the real kinds and raw data) and holds every range at most 190 KB
(194,560 bytes), about 10 KB under the 204,800-byte cap; it logs the tapes' bytes and each range's
total (24h 128,438, 7d 187,983, 30d 187,984 bytes). The fixture overstates production: against
its 7d of 187,983 bytes, production's 7d is estimated at about 165.5 KB (155,942 + 9,523).
The tape rows' `title`, `detail` and `value` were removed on 2026-11-13 (#962), so the bytes above are a little smaller now.

# `/v1/home`

The Proxy Index home's live dashboard in one request per range: the market panel, the four
insight cards, every network's panel with its curve and sparks, each tape tab's newest rows and the
ranking profiles. Before this answer these were only in the dataset file (`/v1/dataset`, about
9 MB) or took about 37 requests. Map: `docs/api-web-map.md`, section 1 and section 11, W6
(HOME-4, HOME-6 to HOME-18, HOME-21, HOME-22, HOME-25, HOME-32, HOME-33, HOME-43, HOME-51) and
its follow-up W6b, the insight cards (HOME-27, HOME-29 to HOME-31, HOME-35, HOME-47).

- **Route:** `GET /v1/home?range=`; `range` is `24h`, `7d` or `30d` (default `30d`). Another
  parameter, or a bad range, answers 400.
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
| `labels` | list of label lists | Every distinct label list of the networks' series, once. A series' `labels` is its index here, so networks on the same schedule share one list. Each label is the old site's `bucketLabel` ("now", "5 min ago", "3h 10m ago", "2 days ago") of the point's age before its window's end. |
| `tape[]` | `TapeEvent` (`id`, `kind`, `event`, `providerSlug`, `providerName`, `product`, `title`, `detail`, `signal`, `value`, `agoSeconds`, `at`, `source`, `na`, `country`, `region`, `target`, `severity`, `values`) | The newest 10 rows of each tape tab (`kind` `census`, `pulse`, `incident`, `audit`, the website's tab ids) among the dataset's tape (the measured test events, one row per incident as in `/v1/events` (`answers/events.md`, EVENTS-MERGE-1: an outage seen by two targets is one row), mapped by `TapeFromEvents`, and the editorial rows), merged newest first: at most 40 rows, counted after the incidents are merged. A tab without events has no rows; rows of another `kind` are left out. The build reads each tab's newest 10 events with one query per tab (`store.Inputs.TapeHeads`, HOME-TAPE-1; 40 for the incident and audit tabs, so 10 merged rows are read whole), so a burst of one tab (the hourly census releases) never empties the others. A client ages them from `at`. Each row carries the event's `country`, `region`, `target` and `severity` and its numbers in `values`, the same object as the event's `/v1/events` row (`FIELDS.md`, "Tape and event values", TAPE-VALUES-1); `title`, `detail` and `value` stay until the website reads them. |
| `profiles[]` | `id`, `label`, `description`, `weights`, `na` | `Dataset.profiles` (`site.ranking_profiles`). |
| `scoreDefinitions` | `{rank, market, status}` | Plain sentences for the 1-10 scores' tooltips, as on `/v1/meta` (`answers/meta.md`). |
| `na`, `naReason`, `naReasons` | paths, codes | The market's and insights' nulls, and `profiles` or `insights.hardestRates` when empty, each with its reason (`answers/README.md`, "Missing data"): no list rate is `catalog` (`market.cheapest`, `insights.value.best`), no latency test `not_measured` (`market.medianLatency`, `market.fastest`). Each `networks[]` row, tape row and profile carries its own. |

### `market`

Every figure follows the range. The fleet is every network with a published headline group.

| Field | Type | Source |
|---|---|---|
| `requests` | integer | The all-networks answer's `ranges.<r>.requests` (`/v1/networks/all`): every product group's completed requests, the same figure as the hero (HOME-52). |
| `observedDays` | days | The fleet history's oldest point to the window end, rounded up; 0 under a day; null when the history covers the range (the old site's `observedDays`). |
| `networks` | integer | Networks with a published headline group. |
| `targets` | integer | Targets with `source` `measured` (`targetCounts.measured` of `/v1/rankings` and `/v1/meta`). |
| `curve` | `{values, labels}` | The fleet success history (request-weighted mean of every network's measured history per age), 110 points for 24h, every point otherwise (`measuredMarket` `headlineSpark`). |
| `medianSuccess`, `medianLatency` | fraction, ms | The median of the networks' range success rate and headline latency p50 (`measuredMarket` `fleet.medianSuccessRate`, `fleet.medianLatency`). |
| `successScore` | `{score, status, basis}` | The market's 1-10 success score, absolute: `score` = `1 + 9 × medianSuccess`, 1 decimal (91.9% gives 9.3); `status` `ranked`, or null with `score` when `medianSuccess` is null; `basis` `"median_success_rate"`. Not a median of the networks' scores, which are ranks (always about 5.5). `FIELDS.md`, "Success scores". |
| `uniqueIps` | integer | The all-networks answer's `ranges.<r>.uniqueIps`: the snapshot's every-group fleet entry, each exit IP once. Null without that entry. |
| `bandwidthGb` | GB (10⁹ bytes) | The sum of the headline windows' `metrics.bytes_up + bytes_down` ÷ 10⁹, so the sum of `networks[].bandwidthGb`; null when a network has no group. |
| `latencySpark`, `successSpark` | `{values, labels}`, 24 points | The fleet latency and success histories, without their unobserved ends. |
| `successScoreSpark` | `{values, labels, statuses}`, the points of `successSpark` | `successSpark` on the 1-10 scale, absolute as `successScore`: each value is `1 + 9 ×` the point's rate, 1 decimal; each status `ranked`, or null with the value at a point without a rate. `labels` are `successSpark.labels`. `FIELDS.md`, "Success scores instead of success rates". |
| `successScoreDelta` | score points | The market success history's change on the 1-10 scale: 9 × (the closing fifth's mean rate − the opening fifth's), as a network's `successDeltaPts` compares them, 2 decimals; unobserved points left out; null below two observed points. |
| `fastest` | `{slug, name, value}` or null | The network with the lowest latency p50 (value in ms); the first in catalog order wins a tie. |
| `mostReliable` | `{slug, name, value, score, status}` or null | The network with the highest overall `successScore` in the range (#898), a tie (also across kinds) to the higher success rate, then the first in catalog order; `score` and `status` are its score's, `value` its success rate (fraction, kept until #900). Without any score: the network with the highest success rate, with a null `score`. |
| `cheapest`, `dearest` | `{slug, name, value}` or null | The lowest and highest list rate (`pricePerGbAtScale`, else `pricePerGb`, in $/GB); unpriced networks are left out; null when none is priced. |
| `spread` | `{value, best, worst}` | The residential bills for one million successes (cost per 1,000 successes × 1,000), unpriced networks left out: `value` is the dearest ÷ the cheapest, `best` and `worst` are `{slug, name}` of both (null without a priced residential network). |
| `spreadAll` | `{value, best, worst}` or null | The 8-target bills of `insights.valueAll` (VALUE-ALL-2): `value` = `insights.valueAll.spreadX`, `best` and `worst` = `{slug, name}` of `insights.valueAll.best` and `worst`. Null without a complete priced row (`incomplete_targets`; `catalog` when no network is priced). `spread` stays the HTTP bill's. |
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
| `value.rows[]` | `{slug, name, bill}` | Every measured residential network's bill for one million successful requests (`costPer1kSuccess × 1000`, in $), cheapest first; an unpriced network's `bill` is null and it comes last. |
| `value.best`, `value.worst` | `{slug, name, bill}` or null | The cheapest and the dearest priced row; null without a priced residential network. |
| `value.spreadX` | ratio | `worst.bill ÷ best.bill`; null without a priced residential network. |
| `value.unpriced` | integer | How many rows are unpriced: they are left out of `best`, `worst` and `spreadX`. |
| `authenticity[]` | `{slug, name, value}` | The top 5 measured networks by `authenticity` (the share of exit IPs in the requested country), highest first, a null value last; datacenter networks are left out. |
| `hardest` | `{id, label, difficulty, passCondition, engine}` or null | The measured target (`source` `measured`) with the highest difficulty: the catalog's, else its scoring weight, as `/v1/targets` gives it; the first in the dataset's order wins a tie. Null when no measured target has a difficulty; a catalog-only target is never chosen. `engine` is `http` or `browser`. |
| `hardestRates[]` | `{slug, name, successRate, engine, scores}` | Every measured network's success rate on `hardest` in the range (its headline window's target benchmark, as `profile.targets[]` of `/v1/networks/{slug}`), highest first; a network without a rate is left out, and the list is empty when `hardest` is null. `engine` is the browser engine whose tries make the figure (the one with the most completed tries; null when the snapshot names none), `http` on a network target. `scores` is `{success: {score, rank, rankedOf, status}}`, that engine's rank-based success score (a network target's own success score, `http`); null without scores. |
| `exclusivity[]` | `{slug, name, value, upstream}` | The top 5 measured networks by `exclusiveShare` (the share of the pool no other network of its kind shares), highest first, a null value last. `upstream` is `{slug, name, shareOfOurs}`, the network's `likelyUpstream` in the range on `/v1/overlap?kind=<its kind>&range=` (W5); null when it has none. |
| `valueAll` | object | The "Best value for money" card: every measured residential network's bill per 1,000 successful requests over the 8 card targets (below, VALUE-ALL-1). `value` above stays as it was. |

### `insights.valueAll`

Each measured residential network's cost per 1,000 successful requests, averaged with equal
weights over the card targets (`api/internal/build/value_all.go`). One target's cost in the range's
residential window is

    costPer1k = listRate × (bytes_up + bytes_down) ÷ 10^9 ÷ successes × 1000

with `listRate` the `$ / GB` the `/v1/targets` cells use (`pricePerGbAtScale`, else `pricePerGb`)
and GB = 1,000,000,000 bytes, the GB networks sell (the framework's `traffic.py`). The bytes are
every finished try's, failed ones too, so the cost carries the traffic that failed tries use. Not
rounded: the API serves the full number (`api/FIELDS.md`, "The 8-target bill").

**Test days: bytes are generated, not measured; the website should mark the card as test data.**

| Field | Type | Source |
|---|---|---|
| `unit`, `gbBytes`, `weighting` | `"usd_per_1k_successes"`, `1000000000`, `"equal"` | Constants. |
| `targets` | target ids | The card targets (`source` `measured`, outside any group), in dataset order: today `generic_http`, `generic_https`, `amazon_pdp`, `google_search`, `linkedin_public`, `instagram_web`, `cloudflare_challenge`, `ai_answers`. |
| `best`, `worst` | `{slug, name, costPer1k}` or null | The first and the last complete priced row; null without one. |
| `spreadX` | ratio or null | `worst.costPer1k ÷ best.costPer1k`; null without a complete priced row or when `best` is 0. |
| `incomplete` | integer | Priced rows missing a target. |
| `unpriced` | integer | Rows without a list rate. |
| `rows[]` | object | Every measured residential network (below). |

Each row:

| Field | Type | Source |
|---|---|---|
| `slug`, `name` | | The ranking row. |
| `byTarget.<id>` | `{costPer1k, tries, successes, mbPerTry}` | Every card target's cost in the window. `tries` is `completed`, `successes` the window's successes, `mbPerTry` the bytes ÷ tries ÷ 10^6 (null without tries or bytes); these are set whenever the data exists, also without a list rate. |
| `targetsCounted` | integer | Card targets with a cost. |
| `missingTargets` | target ids | Card targets without a cost for a reason other than `catalog`. |
| `lowSampleTargets` | target ids | Counted targets with fewer than 20 successes: still in the bill, for the website to flag. |
| `costPer1k` | $ or null | The plain mean of the card targets' costs, only when every one has a cost. |
| `partialCostPer1k` | $ or null | The mean over the counted targets, set only when some but not all have a cost (null by contract otherwise). |
| `na`, `naReason`, `naReasons` | paths, codes | The row's own nulls, paths relative to the row: `costPer1k` is `catalog` without a list rate, else `incomplete_targets`; each `byTarget.<id>.costPer1k` has its own reason, the first that applies: `no_run_in_window` (no try), `no_successes` (tries but no success), `not_measured` (no bytes), `catalog` (no list rate). |

`rows` order, stable: complete priced rows by `costPer1k`, cheapest first; then incomplete priced
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
  "instagram_web":{"costPer1k":1.953566,"tries":100,"successes":51,"mbPerTry":3.6901},
  "cloudflare_challenge":{"costPer1k":2.263126,"tries":100,"successes":40,"mbPerTry":3.3528},
  "ai_answers":{"costPer1k":1.944976,"tries":300,"successes":98,"mbPerTry":2.3532}},
 "na":[]}
```

A complete row's `na` is empty and it has no `naReason` or `naReasons`. An incomplete row has the
same shape: Byteful on the same data has `costPer1k` null with `incomplete_targets`,
`missingTargets` `["google_search","cloudflare_challenge"]` (both `no_successes`),
`lowSampleTargets` `["linkedin_public"]` (2 successes), `targetsCounted` 6 and `partialCostPer1k`
the mean of its other 6.

### `networks[]`

The scalar figures are the network's ranking row of the range (`/v1/rankings?range=`, W2), as
it is there; only the curve, the sparks, `schedule`, `lightLatencyP50`, `claimed`, `claimShare`
and `incidents` are added, with the row's own `na`, `naReason` and `naReasons`.

| Field | Source |
|---|---|
| `slug`, `name`, `kind`, `standing`, `score`, `grade`, `breakdown`, `tagline` | The ranking row. |
| `schedule` | What a measured network's product is measured on ("measured on a census, pulse and load schedule", the generated tagline's last part), also when a catalog tagline replaces the generated one; null otherwise. |
| `bandwidthGb` | The ranking row's: the range window's `metrics.bytes_up + bytes_down` ÷ 10⁹ (GB, decimal); the rows sum to `market.bandwidthGb`. Null when the window recorded no bytes (`no_run_in_window` without a run, `not_measured` with runs but no bytes). |
| `requests`, `observedDays`, `successRate`, `successDeltaPts`, `latencyP50`, `latencyP90`, `latencyP99`, `latencyDeltaPct`, `latencyNote`, `uniqueIps`, `countries`, `cities`, `listRate`, `valueRank`, `authenticity`, `datacenterAsnShare`, `browserOverall`, `successScore`, `successScoreDelta` | The ranking row (null `browserOverall` when the range has none). `successScore` is the network's overall 1-10 success score, `{score, rank, rankedOf, status, targets, weights}`, null in a build without one (`FIELDS.md`, "Success scores"). `successScoreDelta` is its `score` minus the previous published build's, in score points, unrounded; null with `no_score_to_compare` when either build has no ranked score (`FIELDS.md`, "Success score deltas"). |
| `lightLatencyP50` | `stats.<r>.lightLatencyP50`: the light-load latency of an every-request window. |
| `claimed` | The catalog's claimed pool size (`site.trust_signals.claimed_pool_size`); null when not filled. |
| `claimShare` | `uniqueIps ÷ claimed`; null when either is unknown. |
| `incidents` | `stats.<r>.incidentCount`. |
| `curve` | `{values, labels}`: the network's measured success history (finer windows replace overlapping daily points), 84 points for 24h, every point otherwise. |
| `successSpark`, `latencySpark` | `{values, labels}`, at most 22 points each of the measured success and latency histories (fewer when the history is shorter). |
| `successScoreSpark` | `{values, labels, statuses}`, the points of `successSpark` and its `labels` (the same index into the answer's `labels`): each point's 1-10 success score among the home's networks of the same kind with a point at the same time (the snapshot's series buckets), by the rank rule of `scores.success` with the point's requests (100-request minimum, `all_similar`, `too_few_networks` below 3 such networks). A value is null unless its status is `ranked`; `statuses[i]` says why (`all_similar`, `not_enough_data`, `too_few_networks`), for a "no score" hover. A network without a headline snapshot product has no request counts: every point is `not_enough_data`. |

Without a published group the series are the window statistics' own (empty for a network with
no measurement).

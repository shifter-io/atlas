# `/v1/home`

The Proxy Index home's live dashboard in one request per range: the market panel, the four
insight cards, every network's panel with its curve and sparks, the first tape rows and the
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
| `tape[]` | `TapeEvent` (`id`, `kind`, `providerSlug`, `providerName`, `product`, `title`, `detail`, `signal`, `value`, `agoSeconds`, `at`, `source`, `na`) | The first 25 rows of the dataset's tape: the measured test events mapped by `TapeFromEvents`, newest first, then the editorial rows. A client ages them from `at`. |
| `profiles[]` | `id`, `label`, `description`, `weights`, `na` | `Dataset.profiles` (`site.ranking_profiles`). |
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
| `uniqueIps` | integer | The all-networks answer's `ranges.<r>.uniqueIps`: the snapshot's every-group fleet entry, each exit IP once. Null without that entry. |
| `bandwidthGb` | GB (1024³ bytes) | The headline windows' `metrics.bytes_up + bytes_down`; null when a network has no group. |
| `latencySpark`, `successSpark` | `{values, labels}`, 24 points | The fleet latency and success histories, without their unobserved ends. |
| `fastest`, `mostReliable` | `{slug, name, value}` or null | The network with the lowest latency p50 (value in ms) and the highest success rate (fraction); the first in catalog order wins a tie. |
| `cheapest`, `dearest` | `{slug, name, value}` or null | The lowest and highest list rate (`pricePerGbAtScale`, else `pricePerGb`, in $/GB); unpriced networks are left out; null when none is priced. |
| `spread` | `{value, best, worst}` | The residential bills for one million successes (cost per 1,000 successes × 1,000), unpriced networks left out: `value` is the dearest ÷ the cheapest, `best` and `worst` are `{slug, name}` of both (null without a priced residential network). |
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
| `hardestRates[]` | `{slug, name, successRate, engine, scores}` | Every measured network's success rate on `hardest` in the range (its headline window's target benchmark, as `profile.targets[]` of `/v1/networks/{slug}`), highest first; a network without a rate is left out, and the list is empty when `hardest` is null. `engine` is the browser engine whose tries make the figure (the one with the most completed tries; null when the snapshot names none), `http` on a network target. `scores` is `{success: {score, rank, rankedOf, status}}`, that engine's rank-based success score; null on a network target or without scores. |
| `exclusivity[]` | `{slug, name, value, upstream}` | The top 5 measured networks by `exclusiveShare` (the share of the pool no other network of its kind shares), highest first, a null value last. `upstream` is `{slug, name, shareOfOurs}`, the network's `likelyUpstream` in the range on `/v1/overlap?kind=<its kind>&range=` (W5); null when it has none. |

### `networks[]`

The scalar figures are the network's ranking row of the range (`/v1/rankings?range=`, W2), as
it is there; only the curve, the sparks, `schedule`, `lightLatencyP50`, `claimed`, `claimShare`
and `incidents` are added, with the row's own `na`, `naReason` and `naReasons`.

| Field | Source |
|---|---|
| `slug`, `name`, `kind`, `standing`, `score`, `grade`, `breakdown`, `tagline` | The ranking row. |
| `schedule` | The last " · " part of a measured network's tagline ("measured on a census, pulse and load schedule"); null otherwise. |
| `requests`, `observedDays`, `successRate`, `successDeltaPts`, `latencyP50`, `latencyP90`, `latencyP99`, `latencyDeltaPct`, `latencyNote`, `uniqueIps`, `countries`, `cities`, `listRate`, `valueRank`, `authenticity`, `datacenterAsnShare`, `browserOverall` | The ranking row (null `browserOverall` when the range has none). |
| `lightLatencyP50` | `stats.<r>.lightLatencyP50`: the light-load latency of an every-request window. |
| `claimed` | The catalog's claimed pool size (`site.trust_signals.claimed_pool_size`); null when not filled. |
| `claimShare` | `uniqueIps ÷ claimed`; null when either is unknown. |
| `incidents` | `stats.<r>.incidentCount`. |
| `curve` | `{values, labels}`: the network's measured success history (finer windows replace overlapping daily points), 84 points for 24h, every point otherwise. |
| `successSpark`, `latencySpark` | `{values, labels}`, at most 22 points each of the measured success and latency histories (fewer when the history is shorter). |

Without a published group the series are the window statistics' own (empty for a network with
no measurement).

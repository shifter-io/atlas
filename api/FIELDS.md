# Dataset fields and where they come from

Every field of `/v1/dataset` (the site's `Dataset`, `src/lib/types.ts`) with its
source. The columns are:

- **Demo**: where the four `is_demo` fixtures get the value. They were seeded from
  `fixtures/demo/design-demo.json` (formerly `src/data/design-demo.json`).
- **Measured**: where a real provider gets it. Snapshot paths are relative to the
  provider's product in the published calculation (`public.calculated_snapshots.payload`
  via `public.current_calculation`). `w` is `windows["30d"]` and `day` is
  `windows["24h"]`.
- **N/A for measured**: whether the field is null for a real provider with a
  published snapshot.
  - **always**: no source exists yet.
  - **catalog**: null until the named `site.*` column is filled.
  - **data**: null only when the snapshot has no value.
- **NaN in TS**: marked ✱ where `src/lib/presentation.ts` / `window.ts` use `NaN`
  (the site's internal unavailable sentinel). In JSON those values are `null`, as
  `JSON.stringify(NaN)` is. The site's loader (`src/lib/site-api.ts`) turns a `null`
  in a numeric field back into `NaN`, except the `number | null` fields listed under
  "Null by contract" (and `claimedPoolSize`), and leaves `measurement`, `framework`
  and `snapshot` as published. Every formatter in `src/lib/format.ts` shows both
  `null` and `NaN` as `N/A`.

Today the real database has the four fixtures and no published snapshot, so the
only N/A paths are each fixture's `website` and the dataset's `snapshot.*` block
(`/v1/coverage`).

General rules:

- Every field is always present. Missing → `null`. Lists are never `null`; an empty
  list a measured provider never collected (plans, IP samples, leak checks, price
  history, best-for...) is listed in `na`. `incidents` is the exception once the
  snapshot publishes outages (v2): an empty list there means no outage, so it is
  not N/A.
- `na` paths: dots for object keys, `[]` for array elements, `targets[<id>]` and
  `products[<kind>]` for keyed lists, `productMeasurements.<category>.…`.
- Null by contract (never in `na`):
  - `pricing.plans[].gb` / `.ips`: per-IP / per-GB plans.
  - `overlap.likelyUpstream` for fixtures: no reseller signature. It *is* N/A for
    measured providers.
  - `measurement` and `framework` for fixtures.
  - tape `at` for non-measured rows.
  - review `invoiceMonth`, `linkedRunId`, `providerReply`.
  - community run `rejectionReason`, `overlapShare`.
  - changelog `providerSlug`, `providerName`, `delta` for fleet-wide entries.
- Reasons for every N/A path are in `/v1/coverage` (`build/na.go` `reasons`).

## Dataset (top level)

| Field | Demo | Measured / API | N/A |
|---|---|---|---|
| `schemaVersion` | constant `0.6.0` (the contract version; equals the fixture's) | same | never |
| `generatedAt` | `site.datasets.generated_at` (as `toISOString`) | snapshot `as_of` verbatim when a snapshot is published (as `presentationDataset`), else the demo's, else read time | never |
| `seed` | `site.datasets.seed` | (no seed for measured-only data) | null when there are no fixtures, by design |
| `simulated` | true while any `is_demo` provider exists | false otherwise | never |
| `runId` | `site.datasets.run_id` | `preview-<snapshot_id>` when published (as `presentationDataset`) | data |
| `collectorVersion` | `site.datasets.collector_version` | snapshot `implementation_id` when there are no fixtures | data |
| `windowHours` | `site.datasets.window_hours` | 24 | never |
| `targets[]` | `site.targets` | plus `public.targets` (label, pass_condition) for framework targets without a site row | `category`, `difficulty`: catalog (`site.targets`) |
| `scopes[]` | `site.scopes` | same (catalog) | catalog |
| `weights` | `site.score_weights` | snapshot `methodology.settings.weights` when published (the weights its scores used) | data |
| `profiles[]` | `site.ranking_profiles` | same (catalog) | catalog |
| `totals` | computed (below) | computed | data |
| `providers[]` | below | below | |
| `tape[]` | `site.tape_events` | `public.test_events` (newest 200, mapped by the port of `tape.ts tapeFromEvents`); measured rows first, then editorial and demo rows | never |
| `changelog[]` | `site.changelog` | same | never |
| `reviews[]` | `site.reviews` | same | never |
| `communityRuns[]` | `site.community_runs` | same | never |
| `snapshot` *(added)* | | `{available, snapshotId, schemaVersion, asOf, generatedAt, refreshSeconds, methodologyId, readiness, contentHash}` of the published calculation | all null while nothing is published |
| `na` *(added)* | | dataset-level N/A paths (meta, weights, totals, snapshot) | |

### totals (FleetTotals)

Computed at build time with the formulas of `scripts/prepare-design-demo.mjs`. They
run over the **fleet basis**: the fixtures while any exist, matching the site, which
never mixes simulated and measured numbers in a fleet figure (`window.ts
fleetWindow`). Otherwise they run over the measured providers. The site uses the
same basis (`data.ts fleetProviders`/`fleetBasis`, `FleetWindow.basis`) for the
fleet windows, the Overview's fleet views and insight cards, the Pricing page
and the Targets page. With an empty fleet, `fleetViews[].fastest`/`mostReliable`
are `{provider: null, stats: null}`, `cheapest`/`dearest` (picked by the lowest list
rate, `listRate`: `pricePerGbAtScale`, else `pricePerGb`; unpriced networks last) have a null provider and
a null cost, and `bills.best`/`worst` are null. The demo round-trip
test proves the computed values equal the fixture's stored ones.

| Field | Formula (fleet basis) |
|---|---|
| `providers` | count of **all** providers (as `presentationDataset`) |
| `requests24h`, `uniqueIps24h`, `bandwidthGb24h` | sums of `performance.requests24h`, `pool.uniqueIps24h`, `performance.bandwidthMb24h / 1024` |
| `countries` | distinct `geo[].countryCode` |
| `medianLatency`, `medianSuccessRate` | median of `performance.latencyP50` / `performance.successRate` |
| `cheapestCostPer1kSuccess`, `dearestCostPer1kSuccess`, `priceSpreadX`, `residentialSpreadX` | min/max/ratio of `pricing.costPer1kSuccess` (residential primary kind for the last) |
| `incidents24h` | incidents with `startedAgoSeconds <= 86400` |
| `misclassifiedShare` | Σ(uniqueIps24h × trust.datacenterAsnShare) / Σ uniqueIps24h |
| `distinctIpsAcrossFleet`, `fleetDuplicationShare` | pool sum minus pairwise `overlap.overlaps[].sharedIps` |
| `likelyResellers` | providers with `overlap.likelyUpstream` |
| `reviews`, `verifiedReviews`, `communityRuns` | over **all** records: published reviews, invoice- or run-verified, accepted runs |

With no providers, everything except the record counts is null (`na`).

## Provider

| Field | Demo | Measured | N/A for measured | NaN in TS |
|---|---|---|---|---|
| `source` *(always present)* | `"demo"` | `"measured"` | never | |
| `slug` | `site.providers.slug` | `site.providers.slug` linked by `framework_provider`, else `public.providers.id` | never | |
| `name` | `site.providers.name` | catalog, else snapshot product `name`, else the capitalised id | never | |
| `tagline` | `site.providers.tagline` | catalog, else generated `"<Kind> proxies · measured HTTP pool sample"` (presentation.ts) | never with a snapshot | |
| `kinds` | `site.providers.kinds` | catalog, else the measured categories (headline first) | never | |
| `primaryKind` | `site.providers.primary_kind` | catalog when that category is measured, else the category with the most 30d requests | never | |
| `hq` | `site.providers.hq` | `site.providers.hq` | catalog (TS used the placeholder `"Not verified"`) | |
| `founded` | `site.providers.founded` | `site.providers.founded` | catalog | ✱ |
| `claim` | `site.providers.claim` | `site.providers.claim` | catalog (TS hard-coded `"Country-US rotating residential sample"`) | |
| `verdict`, `strengths`, `weaknesses` | `site.providers.*` | catalog, else generated from the snapshot exactly as presentation.ts (completed requests, successes, unique IPs, census totals, untested HTTPS/uptime/sticky sessions) | without a snapshot | |
| `bestFor` | `site.providers.best_for` | `site.providers.best_for` | catalog | |
| `website` *(added)* | `site.providers.website` | `site.providers.website` | catalog; the fixtures have none either | |
| `products[]` | `site.provider_products` | catalog rows, else one entry per measured category (`kind` = default product, `name` = category label, `measured: true`, `url`/`priceLabel`/`note` null) plus unmeasured framework products | `url`, `priceLabel`, `note`: catalog (TS hard-coded Shifter's URL and `"Not collected"`) | |
| `measuredProduct` | `site.providers.measured_product` | catalog if it belongs to the headline category, else the category default (`residential_rotating`, `isp_static`, `datacenter`, `mobile`) | never | |
| `productMeasurements.<category>` | one per `site.measurements` row (+ `site.pricing`, `site.trust_signals`) | one per measured snapshot category (the product with the most 30d requests when a provider has several cohorts) | as below | |
| `score.composite` / `rank` / `previousRank` | `site.providers.score_*` | `w.scores.balanced.score` / `rank` / `previous_rank` | data (until a ready score) | ✱ |
| `score.grade` | `site.providers.score_grade` | `w.scores.balanced.grade` | data (TS used `"—"`, now `"N/A"`) | |
| `score.breakdown.*` | `site.providers.score_breakdown` (scored by `src/lib/score-curves.ts` against the other demo providers of the same kind) | `w.scores.balanced.components.<key>.value`; all 8 keys always present. Each is a 0–100 ratio to the best network of the same proxy kind (100 × yours ÷ best, or best ÷ yours where lower is better), computed by the framework; the raw value, the leader's value and the peer count stay in the snapshot at `components.<key>.inputs` (`{value, best, peers}`; diversity per dimension; product-level reliability/speed per target), reachable through `measurement` | data | ✱ |
| `pool`, `performance`, `pricing`, `trust`, `targets`, `series`, `ipSamples`, `incidents`, `geo`, `asns`, `overlap` | the headline category's measurement; `overlap` from `site.providers.headline_overlap` when the fixture's differs | the headline category's measurement | as below | |
| `reviews` (summary) | computed from `site.reviews` (`generate-demo-data.ts summariseReviews`) | same | `mean`, `verifiedMean` when there are no (verified) reviews (the generator wrote 0) | ✱ |
| `measurement` *(TS optional; always present)* | null | the raw snapshot `ProductResult` of the headline product, unchanged | | |
| `framework` *(added)* | null | `{provider: public.providers.id, products: [public.products.name]}` | | |
| `na` *(added)* | | | | |

### Measurement blocks (top level and each `productMeasurements` entry)

Demo values come from the JSONB documents in `site.measurements`
(`pool`, `performance`, `targets`, `series`, `ip_samples`, `incidents`, `geo`, `asns`,
`overlap`) and the typed `site.pricing`, `site.pricing_plans` and `site.trust_signals`
rows.

| Field | Measured source | N/A for measured | NaN in TS |
|---|---|---|---|
| `pool.uniqueIps24h` / `uniqueIps30d` | `day.pool.unique_ips` / `w.pool.unique_ips` | data | |
| `pool.claimedPoolSize` | `site.trust_signals.claimed_pool_size` | catalog | (null) |
| `pool.churnRate`, `recurringShare` | `w.pool.churn_rate`, `recurring_share` | data | ✱ |
| `pool.countries`, `regions`, `cities`, `asns` | `w.pool.*` | data | |
| `pool.requestsPerIp` | none | always | ✱ |
| `performance.successRate` | `w.metrics.success_rate` (every-request snapshots: the mean of the time-slot success rates; `request_success_rate`, successes ÷ requests, stays in `measurement`) | data | ✱ |
| `performance.successRateDelta24h`, `successRateDelta30d` | not mapped (the per-window `success_delta` is in `/v1/providers/{slug}` `windows[].successDeltaPts`) | always | ✱ |
| `performance.latencyDelta24h`, `ttfbP50` | none | always | ✱ |
| `performance.latencyP50`/`P90`/`P99` | the latency target's (`generic_http`, product rule: the only target with a latency) headline latency: every-request snapshots (metrics with `time_slots`) `w.targets.generic_http.metrics.latency_under_load_ms`, null without a load test; older snapshots its `latency_ms`. Null without a `generic_http` target; the window's own latency (a mix of targets) is never used | data | ✱ |
| `performance.captchaRate`, `blockRate` | `w.metrics.challenge_rate`, `block_rate` (snapshot v2) | data (always for a v1 snapshot) | ✱ |
| `performance.timeoutRate` | `w.metrics.timeout_rate` | data | ✱ |
| `performance.uptimePct` | `w.metrics.uptime × 100` | data | ✱ |
| `performance.downtimeMinutes24h` | `day.uptime.downtime_s / day.uptime.countries / 60`, 1 dp, when `basis` is `"outage time"` (the capacity-weighted downtime: uptime = 1 − minutes / window minutes) | data (always for a v1 snapshot) | ✱ |
| `performance.bandwidthMb24h` | not mapped (the snapshot has `metrics.bytes_down`) | always | ✱ |
| `performance.requests24h` | `day.metrics.completed` | data | |
| `pricing.model`, `trial`, `costBasis` | `site.pricing.model`, `trial`, `cost_basis`; `model` is `"per_gb"` and `costBasis` a generated sentence when the snapshot publishes prices | catalog (TS used `"per_gb"`, `"Not collected"` and a sentence) | |
| `pricing.pricePerGb` (entry rate, smallest plan), `pricePerGbAtScale` (lowest list rate) | the snapshot's `products[].pricing.entry_per_gb` / `lowest_per_gb` (every-request snapshots); they win over `site.pricing.price_per_gb` / `price_per_gb_at_scale` | catalog / snapshot | ✱ |
| `pricing.minCommit`, `avgPayloadKb`, `headlinePriceRank`, `valueRank` | `site.pricing.*` | catalog | ✱ |
| `pricing.costPer1kSuccess` | `site.pricing.cost_per_1k_success`; else `wireCostPer1k(listRate, w.metrics)`: (bytes_up + bytes_down) / completed × 1000 / 1024³ GB × the lowest list rate (`pricePerGbAtScale`, else `pricePerGb`) / `request_success_rate` (`success_rate` in older snapshots), 4 dp; else `avg_payload_kb × 1000 / 1024² GB × price_per_gb / successRate` (4 dp). A generated `costBasis` names the inputs | catalog / data (needs a list rate and wire bytes or a payload) | ✱ |
| `pricing.plans[].catalog` | `site.pricing_plans.catalog` JSONB | Optional published offer: pool, description, lowest advertised rate, tier (null for untiered pools), advertised unit rate, original price/rate, bandwidth label, billing period, included features, order URL, source and check date. Omitted for legacy plans. Measured prices remain scoped to the tested pool. | |
| `pricing.plans[]` | `site.pricing_plans` | catalog (`na: pricing.plans` while empty) | |
| `trust.datacenterAsnShare` | `w.pool.datacenter_asn_share` (every-request snapshots: share of exit IPs whose DB-IP usage type is a hosting type, unknowns excluded), else `site.trust_signals` | data | ✱ |
| `trust.claimGap` | older snapshots: `w.pool.datacenter_asn_share` (as presentation.ts); every-request snapshots (`pool.shares_basis` set) publish no claim gap: null unless `site.trust_signals.claim_gap` | data / catalog | ✱ |
| `trust.residentialAuthenticity` | every-request snapshots: `w.pool.geo_accuracy`, the share of exit IPs whose DB-IP country is the requested one (the authenticity raw value); else `site.trust_signals` | data / catalog | ✱ |
| `trust.blacklistHitRate`, `stickinessHoldRate` | `site.trust_signals.*` | catalog / always (not measured by the framework) | ✱ |
| `trust.stickinessAdvertised`, `kycVerified`, `consentDisclosed` | `site.trust_signals.*` | catalog (TS used `"Not tested"`, `false`, `false`) | |
| `trust.leakChecks[]` | `site.trust_signals.leak_checks` | always (not run by the framework) | |
| `targets[<id>]` | one row per dataset target. `successRate`, `captchaRate`, `blockRate`, `requests` come from `w.targets[id].metrics` (`requests` is 0 when unmeasured); `latencyP50`/`P90` are set for `generic_http` only (its headline latency, under load in every-request snapshots) and null for every other target | data (every unmeasured target is listed) | ✱ |
| `targets[<id>].delta24h` | none | always | ✱ |
| `series.success24h`/`success30d` | completed buckets of `day`/`w` `.series` → `{t: minutes before as_of, v: success_rate}` | data | ✱ (values) |
| `series.latency24h` | `day.series` p50/p90/p99 when the window's latency is the HTTP target's alone (`windowLatencyIsHTTP`: `time_slots.latency_targets` is `["generic_http"]`, or no other target has requests), else null | data | ✱ |
| `series.latency30d`, `uniqueIps24h`, `uniqueIps30d` | `series[].latency_ms.p50` (same rule) / `unique_ips` | data | ✱ |
| `series.pricePerGb90d` | none | always | |
| `ipSamples[]` | not published for measured providers (presentation.ts leaves it empty) | always | |
| `incidents[]` | the product's `outages` (snapshot v2, newest 50 in 30d): `id` `outage:<product id>:<country>:<started_at>`, `startedAgoSeconds = as_of − started_at`, `durationMinutes = duration_s / 60` (an open outage runs to `as_of`), `severity` `"major"`, `scope` the country code, `summary` "Down in US, detected by pulse." (", still ongoing" while open), `impact = 1 / w.uptime.countries`, `troughSuccessRate` null | `incidents` always for a v1 snapshot; `incidents[].troughSuccessRate` always; `impact` without tested countries | ✱ |
| `geo[]` | `w.pool.countries_breakdown` with `share = ips / unique_ips`, plus `w.breakdowns.country` p50/success rate (v2) | `latencyP50`/`successRate` data (always for v1) | ✱ |
| `asns[]` | `w.pool.asns_breakdown` (whole; see *Capped snapshots*), with `usage_type` mapped to hosting/mobile/business/eyeball | data | |
| `overlap.exclusiveShare`, `sharedShare` | `w.overlap.exclusive_share`, `shared_share`; uniqueness is relative to the measured cohort. An observed pool with zero measured peers is 100% exclusive and 0% shared (`compared_products: 0`); no observed pool remains null | data (needs observed IPs) | ✱ |
| `overlap.overlaps[]` | snapshot `overlap[]` rows for the product's cohort and 30d: `sharedIps`, `shareOfOurs = shared/ips_ours`, `shareOfTheirs`; `requestsBothSides` null | data / always for `requestsBothSides` | |
| `overlap.likelyUpstream` | none (reseller detection is not computed) | always | |

### Every-request snapshots

The shared review Coverage evidence table reads `measurement.windows[range].breakdowns.city_asn`.
Each row is one observed location/network intersection: `name`/`asn` fill ASN / ISP,
`usage_type` supplies the observed network type, and `p50` is light-load median latency.
Distinct IPs are that intersection's `ips`, not the city total repeated for every ASN.
The snapshot field passes through the site API whole (see *Capped snapshots*). Older snapshots keep their
location counts and matching `breakdowns.city` latency, with unknown ASN/type rendered N/A.

### Capped snapshots

The published snapshot keeps the top rows of each long list (proxy-index
`calculated/slim.py`) and records every list's full row count in
`windows[*].breakdown_entries` and `windows[*].pool.list_entries`; totals such as
`pool.unique_ips`, `countries` and `asns` are counted from every row, and the typed
`public.calculated_breakdowns` rows keep the whole lists. The country lists stay whole
in the snapshot. The two other lists the site reads whole, `breakdowns.city_asn`
(Coverage evidence) and `pool.asns_breakdown` (`asns[]`), are read back from the same
build's typed rows (`public.calculated_breakdown_details` joined to
`public.calculated_windows`) in the same read transaction (`store/longtail.go`), in
the payload's row shape and order, for every window whose entry count says the list
was capped and matches the typed rows. Otherwise the list is used as published.
`/v1/snapshot` serves the published (capped) snapshot.

`countryLatencyRanks[kind][range][country_code]` is calculated by the site API from
the selected snapshot product's `breakdowns.country` p50, with one vote per measured
provider and proxy category. Peers must have the same cohort, reporting period and
country; demo data and missing/invalid latency or zero-success rows are excluded.
`rank` is 1 plus the number of peers with a strictly lower p50 (ties share a rank),
`peers` counts valid comparisons including self, and `p50` preserves the country
percentile. Missing values are null and listed in `na`. Coverage evidence shows
this country rank beneath each available local latency; its tooltip gives the
country p50 and peer count. City/ASN percentiles are never averaged for ranking.

`tileRanks[kind][range]` and `placeRanks[kind][range]` are calculated once per build
(`build/ranks.go`); pages and `/api/geo` serve the cached values and do no database
work. Peers are the measured products of the same category, cohort and reporting
period (demo data gets neither field). A rank is `{rank, peers}`: `rank` is 1 plus the
number of peers strictly better (ties share a rank, SQL `RANK()`), `peers` counts the
valid values including self, and a missing value has `rank` null.
- `tileRanks` ranks the window's `pool.unique_ips`, `countries`, `regions`, `cities`
  and `asns` (higher is better, counted when above zero) and the headline latency p50
  (lower is better, counted when not negative) as `uniqueIps`, `countries`, `regions`,
  `cities`, `asns` and `latency`.
- `placeRanks` has `countries`, `regions`, `cities` and `asns` maps from the
  `breakdowns.country`, `region`, `city` and `asn` rows, keyed by the upper-case
  country code, `CC|region_code|region`, `CC|region_code|region|city` and the decimal
  ASN. Each value is the compact array `[ipsRank, ipsPeers, latencyRank,
  latencyPeers]`: IPs higher is better (counted when above zero), p50 lower is better
  (counted when not negative and the row has successes). Rows sharing a key add their
  IPs and keep the p50 of the row with most successes. A list the snapshot capped is
  ranked whole from the build's typed rows (`store/placerows.go`, read in the same
  transaction) when their count matches `breakdown_entries`; otherwise as published.
  `/api/geo` rows carry their place's ranks as `placeRanks`.

The framework's every-request scoring (proxy-index branch `score-from-all-requests`) keeps
`schema_version` 2 and adds fields; the API reads both shapes. A window's
`metrics` with `time_slots` is every-request: `success_rate` is time-weighted
(mean of slot rates), `request_success_rate` is successes ÷ requests,
`latency_ms` is light load (pulses only), `latency_under_load_ms` the latency
under load (null with `under_load.reason`). `pool.shares_basis` marks per-IP
shares (`geo_accuracy`, `datacenter_asn_share`); products carry `pricing`
(`lowest_per_gb`, `entry_per_gb`, `effective_from`, `valid_until`, `sources`).
The window `latency` block (load reference, factors, per-country light and
under-load latency), `tests` (requests per test kind), `time_slots`,
`coverage_slots.down` and the light-load breakdown percentiles are passed
through unchanged in `measurement` for the network page. `latency.countries[]`
has one entry per requested country and target (`target`); the page shows the
`generic_http` ones. Time slots are lcm(pulse interval, 5 min): 30 minutes in
production, 10 in staging. The slow-down factor's references are read by
`every-request.ts loadReferences`: `latency.regions[]` (snapshot 69 on: per
runner region its `reference`, the daily `chosen[]` picks and `factors` with
`excluded_during_census`), and the older single `latency.reference` /
`latency.factors` as one row. `latency.countries[]` carries `region` (older
payloads: `target`, filtered to `generic_http`). `census_overlap` (pulses and
load waves clean vs during a census of the same network), `metrics.latency_reason`
and `time_slots.skipped_during_census` pass through for the network page.

Measured copy follows the data (Go `measuredProvider`, TS `measuredProvider`):
the tagline names the schedule ("measured on a census, pulse and load
schedule" for every-request snapshots, "census and pulse" for scheduled older
ones, else "measured HTTP pool sample"); the verdict ends with the MILF Score
and grade once ready, adding "alone in its kind so far" when every component
was scored against one ready peer (`inputs.peers` = 1, Go `snap.Score.Peers`);
the weaknesses say "Billed usage not collected (list prices only)" when the
snapshot publishes list prices, else "Pricing and billed usage not collected".
The network page shows such a score as "#1 of 1 in its kind" with the note
(`every-request.ts kindStanding`); the framework itself leaves it unranked. Products' `priceLabel`
is the list-rate range (`jsnum.PriceRange`, format.ts `priceRange`), e.g.
`$0.75–$1.00/GB`. Test input: `fixtures/measured/snapshot-every-request.json`
(`scripts/every-request-fixture.mjs`).

### Window statistics of a measured provider

`windowStats` (`/v1/overview` leaderboard `windows`, `/v1/providers/{slug}`
`windows`) reads the snapshot window: `successRate = metrics.success_rate`,
`latencyP50/P90/P99` the `generic_http` target's headline latency (under load
in every-request snapshots, else `latency_ms`), `lightLatencyP50/P90/P99` its
light-load `latency_ms` in every-request snapshots (null otherwise, and always
for demo networks), `latencyTarget` `"generic_http"` (null for demo networks),
the `latency` series only where the window's latency is that target's alone, `costPer1kSuccess = wireCostPer1k(listRate, metrics)` of that window
(so a priced measured network enters `costBoards` and the fleet bills),
`uptimePct = metrics.uptime × 100` and
`downtimeMinutes` as `performance.downtimeMinutes24h` above, for that window.
With outages published (v2), `bands` and `incidentCount` come from `incidents`
exactly as for a demo network (each outage's overlap with the window, as a
fraction of it, labelled `major · <country>`), so the charts shade real outages.
For a v1 snapshot `incidentCount` and `downtimeMinutes` are null and `bands` is
empty.

`availability` is the per-bucket fractional availability history, mapped from
`measurement.windows.<range>.series[].uptime` without interpolation. Buckets
are five minutes for 24h and one day for 7d/30d. The calculator unions and clips
recorded outages per country, using countries with a started census/pulse run
or an active outage in that bucket. Buckets without monitoring evidence, and
older snapshots without this field, remain null. Demo histories use the same
weighted incident intervals as their totals. The shared Availability metric
card displays this series for every tested product category.

## Records

| Entity | Fields | Source |
|---|---|---|
| `TapeEvent` | `id`, `kind`, `providerSlug`, `providerName`, `product`, `title`, `detail`, `signal`, `value`, `agoSeconds` + *added* `at`, `source`, `na` | Measured: `public.test_events`, mapped by the port of `tape.ts`. `agoSeconds` is relative to the snapshot's `generated_at` (or `generatedAt`). Demo: `site.tape_events`, `agoSeconds = demo generatedAt − occurred_at`. `providerName` comes from the provider list. |
| `ChangeEvent` | all + `source`, `na` | `site.changelog`; `providerName` from the provider list |
| `UserReview` | all + `source`, `na` | `site.reviews` (`posted_at` → `agoSeconds`) |
| `CommunityRun` | all + `source`, `na` | `site.community_runs` (typed headline numbers, JSONB `targets`/`successSeries`/`latencySeries`); `providerName` from the provider list |
| `TargetMeta` | `id`, `label`, `category`, `passCondition`, `difficulty` + `source`, `na` | `site.targets`, falling back to `public.targets` |
| `TestScope` | all + `na` | `site.scopes` (`targets` is `"all"` when `all_targets`, else `target_ids`) |
| `RankingProfile` | all + `na` | `site.ranking_profiles` (`weights` JSONB, all 8 keys always present) |

`agoSeconds` of fixture rows is always relative to the fixture's own
`generatedAt`. Rows from different sources therefore keep the ages they were
written with, exactly as the site does today.

## Deviations from the TypeScript

`internal/build/reference_test.go` enforces these. The test runs the site's own
`presentation.ts`, `window.ts`, `overview.ts` and `tape.ts` and allows only these
differences:

1. **Placeholders become null.** For measured providers, `hq`, `claim`,
   `pricing.model`/`trial`/`costBasis`, `trust.stickinessAdvertised`/`kycVerified`/`consentDisclosed`,
   `products[0].url`/`priceLabel`/`note` and `score.grade` are null (and listed in `na`)
   instead of presentation.ts's placeholder copy or Shifter-specific constants. A
   `site.*` catalog value fills them.
2. **Series ages are minutes.** presentation.ts computes `t` as milliseconds / 60
   (1,000× too large). The `SeriesPoint` contract and `window.ts` use minutes, so
   the API publishes minutes.
3. **No leaked `kind`/`product` on measured providers.** presentation.ts spreads
   the measurement into the provider. The API keeps the Provider contract
   (`primaryKind`/`measuredProduct`).
4. **Snapshot overlap is mapped.** `overlap.exclusiveShare`/`sharedShare`/`overlaps`
   come from the snapshot instead of being left unavailable.
5. **`pow`-derived values may differ in the last bit.** In practice that is
   `windowStats.churnRate` for 7d/30d. JavaScript `**` uses the platform libm,
   so the site itself differs across platforms. The tolerance is 1e-14 relative.
6. **The score breakdown has all 8 keys** (null when the snapshot has no such
   component). presentation.ts copies only the components present.
7. **Several products per provider** become one provider with one
   `productMeasurements` entry per category. presentation.ts emits one provider per
   snapshot product, so a provider with two products would get two entries with
   the same slug.
8. **The tape is merged.** `Dataset.tape` holds the measured events from
   `public.test_events` first, then the editorial and demo rows (what
   `LiveTape.astro` shows). In the site today, `dataset.tape` holds only the demo
   rows. `source` tells the two apart.
9. **Search.** The meta of an unscored network is `"Unranked · N/A"`, and there is
   a new `Countries` group.

`measuredTotals` uses the same fields as `totals`, aggregated by the API over
`source = "measured"` providers only. Overview insight descriptions read these
totals even while demo providers remain in the database.

`measuredMarket` supplies the All market panel without simulated inputs: three
window views with measured requests, pool sizes, latency, success, observed wire
bytes, history and price picks; `authenticity` is the exit-IP-weighted country
match and `targetCount` counts framework targets. Missing measurements stay null.

Measured `WindowStats.successDeltaPts` and `latencyDeltaPct` compare the closing
fifth with the opening fifth of observed samples in the selected window. Finer
24-hour history replaces overlapping daily buckets; missing samples are omitted.
Success is a percentage-point difference; latency is a percentage change in the
HTTP headline basis (under load for every-request snapshots). Two observations
are required; an under-load comparison never substitutes light-load latency.

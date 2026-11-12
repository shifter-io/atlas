# Dataset fields and where they come from

Every field of `/v1/dataset` (the website's `Dataset`, `src/lib/types.ts` in the website repository) with its
source. The columns are:

- **Measured** (or **Source**): where a provider gets it. There are no demo
  providers: the four design fixtures (`is_demo` rows, once loaded by `seed-demo`)
  are gone, and `is_demo` rows left in a database are never read. Snapshot paths
  are relative to the provider's product in the published calculation (`public.calculated_snapshots.payload`
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
  "Null by contract" (and `claimedPoolSize`), and leaves `framework`
  and `snapshot` as published. Every formatter in `src/lib/format.ts` shows both
  `null` and `NaN` as `N/A`.

General rules:

- Every field is always present. Missing → `null`. Lists are never `null`; an empty
  list a measured provider never collected (plans, IP samples, leak checks, price
  history, best-for...) is listed in `na`. `incidents` is the exception once the
  snapshot publishes outages (v2): an empty list there means no outage, so it is
  not N/A.
- `na` paths: dots for object keys, `[]` for array elements, `targets[<id>]` and
  `products[<kind>]` for keyed lists, `productMeasurements.<category>.…`.
- Null by contract (never in `na`, except in the page answers, which list
  them with a reason code: MEASURED-FIELDS-1, below):
  - `pricing.plans[].gb` / `.ips`: per-IP / per-GB plans.
  - `framework` for a catalog row not linked to a framework provider.
  - tape `at` for non-measured rows.
  - review `invoiceMonth`, `linkedRunId`, `providerReply`.
  - community run `rejectionReason`, `overlapShare`.
  - changelog `providerSlug`, `providerName`, `delta` for fleet-wide entries.
  - a `ChangeEntry`'s nulls (`/v1/changelog` and the network answer's
    `changelog[]`): `network` of a fleet-wide entry, `title` and `detail` of
    an incident, `delta` without a figure, `incident` of every other kind, and
    an incident's `troughSuccessRate`, `impact` and `durationMinutes` (null
    while ongoing). The shape is the website's `ChangeEntry`, which has no `na`
    (api/answers/changelog.md); the network answer lists them in its own `na`
    (`changelog[].title`, ...) with `not_applicable`.
  - target `group` for a target that is not a source of a group target.
  - In the page answers (api/answers/README.md, "Missing data"), the network
    profile lists each plan's null `gb`, `ips` and `catalog.tier` by the plan's
    name (`pricing.plans[<name>].ips`): `not_applicable` for a plan without an
    IP count (sold by GB), a per-IP plan's `gb` and an untiered pool's `tier`,
    `catalog` for a plan sold by GB without a GB amount; and a null
    `observedDays` (stats, rankings rows) is listed with `history_covers_range`,
    or `no_run_in_window` without a run.
  - page answers: an overlap
    row's `likelyUpstream` and the home's `insights.exclusivity[].upstream`
    without a reseller signature, the all-networks answer's `profile`, and a
    browser score's `score`, `rank` and `rankedOf` (and a target's
    `scoreStatus.<engine>.rankedOf`) unless its `status` is `ranked`.
- Browser targets' fields (`outcomes`, `pageLoadP50`/`P90`, `sources`, `engine`, `engines`, `scores`) are written
  only on browser targets' entries; a network target's entry has exactly its
  older fields (see "Browser targets" below).
- Reasons for every N/A path are in `/v1/coverage` (`build/na.go` `reasons`).
  The page answers added from Phase 3 also give each path a reason code
  beside their `na` (`naReason`, `naReasons`; api/answers/README.md, "Missing
  data").
- An empty Index Score says why (SCORE-REASONS-1): `score`, `grade`, `rank`
  and each `breakdown.<component>` have the code of the calculator's reason
  (`scores.balanced.reasons`, `components.<component>.reason`), the first of:
  `no_pulse_tests` (no finished pulse tests, so no uptime), `no_load_test`
  (no load test for the speed), `required_target_missing` (a required target
  has no ready result), `no_price_evidence` (no verified price per GB, so no
  value), `few_requests` (too few requests), `low_time_coverage` (the
  requests cover too little of the window) and `expired` (the measurements
  are too old). `catalog` and `no_run_in_window` come first;
  `insufficient_data` is left for a reason none of these match.

## Dataset (top level)

| Field | Source | N/A |
|---|---|---|
| `schemaVersion` | constant `0.6.0` (the contract version) | never |
| `generatedAt` | snapshot `as_of` verbatim when a snapshot is published (as `presentationDataset`), else read time | never |
| `seed` | none (the seed was the removed fixtures') | null, by design |
| `simulated` | always false (it was true while demo fixtures existed) | never |
| `runId` | `preview-<snapshot_id>` when published (as `presentationDataset`) | data |
| `collectorVersion` | snapshot `implementation_id` | data |
| `windowHours` | 24 | never |
| `targets[]` | `site.targets` (`api/scripts/catalog-targets.sql`), plus `public.targets` (label, pass_condition, engine) for framework targets without a site row; ordered by `sort_order` (a framework target's `public.targets.sort_order`, else its catalog row's; ties keep catalog rows first, in their order) | `category`, `difficulty`: catalog (`site.targets`); `engine`: catalog-only targets |
| `scopes[]` | `site.scopes` (`api/scripts/catalog-scopes.sql`) | catalog |
| `weights` | `site.score_weights`, replaced by the snapshot `methodology.settings.weights` when published (the weights its scores used) | data |
| `profiles[]` | `site.ranking_profiles` (`api/scripts/catalog-profiles.sql`: the five website profiles; a client re-ranks with a profile from each row's `breakdown`) | catalog |
| `totals` | computed (below) | data |
| `providers[]` | below | |
| `tape[]` | `public.test_events`, one row per incident (an outage seen by two targets' pulses of a slot is one row, `api/answers/events.md` EVENTS-MERGE-1) (200 rows, newest first: the newest non-routine rows up to 100, then the newest of any kind, as `/v1/events` picks them, plus each tab's newest 10 events not among them, `store.Inputs.TapeHeads`; mapped by the port of `tape.ts tapeFromEvents`); measured rows first, then the editorial `site.tape_events` rows | never |
| `changelog[]` | `site.changelog` | never |
| `reviews[]` | `site.reviews` | never |
| `communityRuns[]` | `site.community_runs` | never |
| `pages` *(added)* | every page answer the site draws, by the request that serves it (see "Page answers in the dataset file") | never |
| `snapshot` *(added)* | `{available, snapshotId, schemaVersion, asOf, generatedAt, refreshSeconds, methodologyId, readiness, contentHash}` of the published calculation | all null while nothing is published |
| `na` *(added)* | dataset-level N/A paths (meta, weights, totals, snapshot) | |

### totals (FleetTotals)

Computed at build time with the formulas of `scripts/prepare-design-demo.mjs` (a website
script, removed with the website; it is in the old `proxy-index-api` repository's history,
in the parent of the commit that removed the website). They
run over the **fleet basis**: every provider (all are measured or catalog rows;
the demo fixtures that once formed a separate basis are gone, so
`FleetWindow.basis` is always `"measured"`). With an empty fleet, the
`measuredMarket` views' `fastest`/`mostReliable`
are `{provider: null, stats: null}`, `cheapest`/`dearest` (picked by the lowest list
rate, `listRate`: `pricePerGbAtScale`, else `pricePerGb`; unpriced networks last) have a null provider and
a null cost, and `bills.best`/`worst` are null.

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

| Field | Measured | N/A for measured | NaN in TS |
|---|---|---|---|
| `source` *(always present)* | `"measured"` | never | |
| `slug` | `site.providers.slug` linked by `framework_provider`, else `public.providers.id` | never | |
| `name` | catalog (`site.providers.name`: the brand's own spelling, read on its website; `api/scripts/catalog-networks.sql`), else snapshot product `name`, else the capitalised id; every answer that names a network uses this | never | |
| `tagline` | catalog (`site.providers.tagline`: the network's own one-line description of its residential proxies, quoted from its website; `api/scripts/catalog-networks.sql`), else generated `"<Kind> proxies · <schedule>"` (presentation.ts) | never with a snapshot | |
| `kinds` | catalog, else the measured categories (headline first) | never | |
| `primaryKind` | catalog when that category is measured, else the category with the most 30d requests | never | |
| `hq` | `site.providers.hq` | catalog (TS used the placeholder `"Not verified"`) | |
| `founded` | `site.providers.founded` | catalog | ✱ |
| `claim` | `site.providers.claim` | catalog (TS hard-coded `"Country-US rotating residential sample"`) | |
| `verdict`, `strengths`, `weaknesses` | catalog, else generated from the snapshot exactly as presentation.ts (completed requests, successes, unique IPs, census totals, untested HTTPS/uptime/sticky sessions) | without a snapshot | |
| `bestFor` | `site.providers.best_for` | catalog | |
| `website` *(added)* | `site.providers.website` | catalog | |
| `products[]` | catalog rows, else one entry per measured category (`kind` = default product, `name` = category label, `measured: true`, `url`/`priceLabel`/`note` null) plus unmeasured framework products | `url`, `priceLabel`, `note`: catalog (TS hard-coded Shifter's URL and `"Not collected"`) | |
| `measuredProduct` | catalog if it belongs to the headline category, else the category default (`residential_rotating`, `isp_static`, `datacenter`, `mobile`) | never | |
| `productMeasurements.<category>` | one per measured snapshot category (the product with the most 30d requests when a provider has several cohorts) | as below | |
| `score.composite` / `rank` / `previousRank` | `w.scores.balanced.score` / `rank` / `previous_rank` | data (until a ready score) | ✱ |
| `score.grade` | `w.scores.balanced.grade` | data (TS used `"—"`, now `"N/A"`) | |
| `score.breakdown.*` | `w.scores.balanced.components.<key>.value`; all 8 keys always present. Each is a 0–100 ratio to the best network of the same proxy kind (100 × yours ÷ best, or best ÷ yours where lower is better), computed by the framework; the raw value, the leader's value and the peer count stay in the snapshot at `components.<key>.inputs` (`{value, best, peers}`; diversity per dimension; product-level reliability/speed per target) | data | ✱ |
| `pool`, `performance`, `pricing`, `trust`, `targets`, `series`, `ipSamples`, `incidents`, `geo`, `asns`, `overlap` | the headline category's measurement | as below | |
| `reviews` (summary) | computed from `site.reviews` (`summariseReviews` of the website's `scripts/generate-demo-data.ts`, in the old `proxy-index-api` repository's history before the website was removed) | `mean`, `verifiedMean` when there are no (verified) reviews (the generator wrote 0) | ✱ |
| `framework` *(added)* | `{provider: public.providers.id, products: [public.products.name]}` | | |
| `na` *(added)* | | | |

### Measurement blocks (top level and each `productMeasurements` entry)

Catalog values come from the typed `site.pricing`, `site.pricing_plans` and
`site.trust_signals` rows (`site.measurements`, the removed fixtures' documents, is
not read).

| Field | Measured source | N/A for measured | NaN in TS |
|---|---|---|---|
| `pool.uniqueIps24h` / `uniqueIps30d` | `day.pool.unique_ips` / `w.pool.unique_ips` | data | |
| `pool.claimedPoolSize` | `site.trust_signals.claimed_pool_size` | catalog | (null) |
| `pool.churnRate`, `recurringShare` | `w.pool.churn_rate`, `recurring_share` | data | ✱ |
| `pool.countries`, `regions`, `cities`, `asns` | `w.pool.*` | data | |
| `pool.requestsPerIp` | none | always | ✱ |
| `performance.successRate` | `w.metrics.success_rate` (every-request snapshots: the mean of the time-slot success rates; `request_success_rate`, successes ÷ requests, is not served) | data | ✱ |
| `performance.successRateDelta24h`, `successRateDelta30d` | not mapped (the per-window `success_delta` is in `/v1/networks/{slug}` `stats[].successDeltaPts`) | always | ✱ |
| `performance.latencyDelta24h`, `ttfbP50` | none | always | ✱ |
| `performance.latencyP50`/`P90`/`P99` | the latency target's (`generic_http`, product rule: the only target with a latency) headline latency: every-request snapshots (metrics with `time_slots`) `w.targets.generic_http.metrics.latency_under_load_ms`; without a load test in the window (production runs none, docs/api-web-map.md section 14) its light-load `latency_ms`, the daily tests' own (API-NA-2); older snapshots its `latency_ms`. Null without a `generic_http` target; the window's own latency (a mix of targets) is never used | data | ✱ |
| `performance.latencyMean` *(added)* | the `mean` key (milliseconds, framework #161) of the same headline latency object as `latencyP50`, by the same rule (under load: each light-load slot's mean × the slot's mean factor, load-run mean ÷ reference-pulse mean, averaged as the percentiles are). Like the percentiles it comes from the 1 ms ceiling histograms, so it can be up to about 1 ms above the mean of the raw latencies. Per-row lists and `series` latency points carry no mean | data: `latencyP50`'s cases, or a snapshot written before the mean | |
| `performance.captchaRate`, `blockRate` | `w.metrics.challenge_rate`, `block_rate` (snapshot v2) | data (always for a v1 snapshot) | ✱ |
| `performance.timeoutRate` | `w.metrics.timeout_rate` | data | ✱ |
| `performance.uptimePct` | `w.metrics.uptime × 100` | data | ✱ |
| `performance.downtimeMinutes24h` | `day.uptime.downtime_s / day.uptime.countries / 60`, 1 dp, when `basis` is `"outage time"` (the capacity-weighted downtime: uptime = 1 − minutes / window minutes) | data (always for a v1 snapshot) | ✱ |
| `performance.bandwidthMb24h` | not mapped (the snapshot has `metrics.bytes_down`) | always | ✱ |
| `performance.requests24h` | `day.metrics.completed` | data | |
| `pricing.model`, `trial`, `costBasis` | `site.pricing.model`, `trial`, `cost_basis`; `model` is `"per_gb"` and `costBasis` a generated sentence when the snapshot publishes prices | catalog (TS used `"per_gb"`, `"Not collected"` and a sentence) | |
| `pricing.pricePerGb` (entry rate, smallest plan), `pricePerGbAtScale` (lowest list rate) | the snapshot's `products[].pricing.entry_per_gb` / `lowest_per_gb` (every-request snapshots); they win over `site.pricing.price_per_gb` / `price_per_gb_at_scale` | catalog / snapshot | ✱ |
| `pricing.minCommit`, `valueRank` | `site.pricing.*` | catalog | ✱ |
| `pricing.avgPayloadKb` | `site.pricing.avg_payload_kb`; else the 30d window's wire bytes per request: (bytes_up + bytes_down) / completed / 1024, 2 dp, the same wire bytes `costPer1kSuccess` reads | catalog / data (`not_measured` without wire bytes) | ✱ |
| `pricing.headlinePriceRank` | `site.pricing.headline_price_rank`; else the competition rank of the network's list rate (`pricePerGbAtScale`, else `pricePerGb`) among the list rates of the networks of its kind, cheapest first, ties sharing the better rank (as `valueRank` ranks cost) | catalog (no list rate) | ✱ |
| `pricing.costPer1kSuccess` | `site.pricing.cost_per_1k_success`; else `wireCostPer1k(listRate, w.metrics)`: (bytes_up + bytes_down) / completed × 1000 / 1024³ GB × the lowest list rate (`pricePerGbAtScale`, else `pricePerGb`) / `request_success_rate` (`success_rate` in older snapshots), 4 dp; else `avg_payload_kb × 1000 / 1024² GB × price_per_gb / successRate` (4 dp). A generated `costBasis` names the inputs | catalog / data (needs a list rate and wire bytes or a payload) | ✱ |
| `pricing.plans[].catalog` | `site.pricing_plans.catalog` JSONB | Optional published offer: pool, description, lowest advertised rate, tier (null for untiered pools), advertised unit rate, original price/rate, bandwidth label, billing period, included features, order URL, source and check date. Omitted for legacy plans. Measured prices remain scoped to the tested pool. | |
| `pricing.plans[]` | `site.pricing_plans` | catalog (`na: pricing.plans` while empty) | |
| `trust.datacenterAsnShare` | `w.pool.datacenter_asn_share` (every-request snapshots: share of exit IPs whose DB-IP usage type is a hosting type, unknowns excluded), else `site.trust_signals` | data | ✱ |
| `trust.claimGap` | older snapshots: `w.pool.datacenter_asn_share` (as presentation.ts); every-request snapshots (`pool.shares_basis` set) publish no claim gap: null unless `site.trust_signals.claim_gap` | data / catalog | ✱ |
| `trust.residentialAuthenticity` | every-request snapshots: `w.pool.geo_accuracy`, the share of exit IPs whose DB-IP country is the requested one (the authenticity raw value); else `site.trust_signals` | data / catalog | ✱ |
| `trust.blacklistHitRate`, `stickinessHoldRate` | `site.trust_signals.*` | `not_measured` without a catalog value (not measured by the framework) | ✱ |
| `trust.stickinessAdvertised`, `kycVerified`, `consentDisclosed` | `site.trust_signals.*` | catalog (TS used `"Not tested"`, `false`, `false`) | |
| `trust.leakChecks[]` | `site.trust_signals.leak_checks` | `not_measured` without a catalog value (not run by the framework) | |
| `targets[<id>]` | one row per dataset target. `successRate`, `captchaRate`, `blockRate`, `requests` come from `w.targets[id].metrics` (`requests` is 0 when unmeasured); `latencyP50`/`P90`: `generic_http`'s headline latency (under load in every-request snapshots); every other HTTP target's (`generic_https`) display-only light-load `latency_ms`, from its own successful pulses and runs outside a batch that did not run during a census, with the latency target's slot rule (null when no time slot has `[slots] min_latency_successes`, 30, of them; HTTPS-LATENCY-1). Display only: no headline, load factor, Speed or score reads it. Null on browser targets and in older snapshots (before every-request scoring, or written before HTTPS-LATENCY-1) | data (every unmeasured target is listed) | ✱ |
| `targets[<id>].delta24h` | none | always | ✱ |
| `targets[<id>].outcomes`, `pageLoadP50`/`P90`, `sources`, `engine`, `engines`, `scores` | browser targets only, from `w.targets[id].browser` (below) | data | ✱ |
| `browserOverall` | the network's overall browser score per engine, from `w.browser_overall` ("Browser scores" below); not written when the snapshot has none | data | ✱ |
| `series.success24h`/`success30d` | completed buckets of `day`/`w` `.series` → `{t: minutes before as_of, v: success_rate}` | data | ✱ (values) |
| `series.latency24h` | `day.series` p50/p90/p99 when the window's latency is the HTTP target's alone (`windowLatencyIsHTTP`: `time_slots.latency_targets` is `["generic_http"]`, or no other target has requests), else null | data | ✱ |
| `series.latency30d`, `uniqueIps24h`, `uniqueIps30d` | `series[].latency_ms.p50` (same rule) / `unique_ips` | data | ✱ |
| `series.pricePerGb90d` | none | always | |
| `ipSamples[]` | never served: exit addresses are not published (the database holds real exit addresses and the API is public), and no synthetic sample stands in | always (`not_published`) | |
| `incidents[]` | the product's `outages` (snapshot v2, newest 50 in 30d): `id` `outage:<product id>:<country>:<started_at>`, `startedAgoSeconds = as_of − started_at`, `durationMinutes = duration_s / 60` (an open outage runs to `as_of`), `severity` `"major"`, `scope` the country code, `summary` "Down in US, detected by pulse." (", still ongoing" while open), `impact = 1 / w.uptime.countries`, `troughSuccessRate` null | `incidents` always for a v1 snapshot; `incidents[].troughSuccessRate` always; `impact` without tested countries | ✱ |
| `geo[]` | `w.pool.countries_breakdown` with `share = ips / unique_ips`, plus `w.breakdowns.country` p50/success rate (v2) | `latencyP50`/`successRate` data (always for v1) | ✱ |
| `asns[]` | `w.pool.asns_breakdown` (whole; see *Capped snapshots*), with `usage_type` mapped to hosting/mobile/business/eyeball | data | |
| `overlap.exclusiveShare`, `sharedShare` | `w.overlap.exclusive_share`, `shared_share`; uniqueness is relative to the measured cohort. An observed pool with zero measured peers is 100% exclusive and 0% shared (`compared_products: 0`); no observed pool remains null | data (needs observed IPs) | ✱ |
| `overlap.overlaps[]` | snapshot `overlap[]` rows for the product's cohort and 30d: `sharedIps`, `shareOfOurs = shared/ips_ours`, `shareOfTheirs`; `requestsBothSides` null | data / always for `requestsBothSides` | |
| `overlap.likelyUpstream` | none (reseller detection is not computed) | always | |

### Capped snapshots

A window records every list's full row count in `windows[*].breakdown_entries`;
totals such as `pool.unique_ips`, `countries` and `asns` are counted from every row.
The lists the site reads whole, the country, region, city, ASN and City × ASN rows
and `pool.asns_breakdown` (`asns[]`), are read from the build's network lists (see
"Payload v4 (network summaries)"). No endpoint serves the published snapshot or a summary of it (`/v1/snapshot` is gone);
the per-page answers below carry what pages need from it.

`countryLatencyRanks[kind][range][country_code]` is calculated by the site API from
the selected snapshot product's `breakdowns.country` p50, with one vote per measured
provider and proxy category. Peers must have the same cohort, reporting period and
country; missing/invalid latency or zero-success rows are excluded.
`rank` is 1 plus the number of peers with a strictly lower p50 (ties share a rank),
`peers` counts valid comparisons including self, and `p50` preserves the country
percentile. Missing values are null and listed in `na`. Coverage evidence shows
this country rank beneath each available local latency; its tooltip gives the
country p50 and peer count. City/ASN percentiles are never averaged for ranking.

`tileRanks[kind][range]` is calculated once per build (`build/ranks.go`); pages serve
the cached values and do no database work. Peers are the measured products of the same category, cohort and reporting
period. A rank is `{rank, peers}`: `rank` is 1 plus the
number of peers strictly better (ties share a rank, SQL `RANK()`), `peers` counts the
valid values including self, and a missing value has `rank` null.
- `tileRanks` ranks the window's `pool.unique_ips`, `countries`, `regions`, `cities`
  and `asns` (higher is better, counted when above zero) and the headline latency p50
  (lower is better, counted when not negative) as `uniqueIps`, `countries`, `regions`,
  `cities`, `asns` and `latency`.

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
`coverage_slots.down` and the light-load breakdown percentiles are read for
the window statistics and are not served raw. `latency.countries[]`
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
`$0.75–$1.00/GB`. Test input: `api/fixtures/measured/snapshot-every-request.json`
(`api/scripts/every-request-fixture.mjs`).

### Window statistics of a measured provider

`windowStats` (`/v1/rankings` rows, `/v1/networks/{slug}` `stats` and its
`/chart` answers) reads the snapshot window: `successRate = metrics.success_rate`,
`latencyP50/P90/P99` the `generic_http` target's headline latency (under load
in every-request snapshots with a load test in the window, else `latency_ms`:
without a load test, the daily tests' own), `lightLatencyP50/P90/P99` its
light-load `latency_ms` beside a latency under load (null otherwise),
`latencyTarget` `"generic_http"`,
the `latency` series only where the window's latency is that target's alone, `costPer1kSuccess = wireCostPer1k(listRate, metrics)` of that window
(so a priced measured network gets a `valueRank` and enters the fleet bills),
`uptimePct = metrics.uptime × 100` and
`downtimeMinutes` as `performance.downtimeMinutes24h` above, for that window.
With outages published (v2), `bands` and `incidentCount` come from `incidents`
(each outage's overlap with the window, as a
fraction of it, labelled `major · <country>`), so the charts shade real outages.
For a v1 snapshot `incidentCount` and `downtimeMinutes` are null and `bands` is
empty.

Per-range figures of the same window (WINDOW-FIGURES-1; every one null for a
network without a snapshot product, and wherever the window has no value):

| Field | Source |
|---|---|
| `captchaRate`, `blockRate` | `metrics.challenge_rate`, `block_rate` |
| `requestSuccessRate` | `metrics.request_success_rate`: successes ÷ requests, beside the time-weighted `successRate` (null before every-request snapshots) |
| `countries`, `regions`, `cities`, `asns` | `pool.countries`, `regions`, `cities`, `asns` |
| `datacenterAsnShare` | `pool.datacenter_asn_share` |
| `exclusiveShare`, `sharedShare` | `overlap.exclusive_share`, `shared_share`, by the rule of `overlap.exclusiveShare` above: an observed pool (`pool.unique_ips` > 0) with `compared_products: 0` is 1 and 0 (older snapshots publish null for it); no observed pool stays null |
| `observedDays` | days from the range's first history point (the merged history behind `successDeltaPts`) to `period.end`, rounded up; 0 below one day; null when the history covers the range or there is none (the old site's "the last N days" / "the last <1 day") |
| `latencyUnderLoad` | true when `latencyP50` is under load (an every-request window with a load test); false when the latency shown is light load (`latency_ms` of an older snapshot, or of an every-request window without a load test: production's daily tests); null without latency |
| `latencyNote` | every-request.ts `latencyNote`: the target's label (`"HTTP"` for `generic_http`), plus `", under load"` when the headline is under load (`lightLatencyP50` set beside it: `"HTTP, under load"`); null without `latencyTarget` |
| `wireGbPer1k` | `(metrics.bytes_up + bytes_down) ÷ completed × 1000 ÷ 1024³`: GB of wire traffic per 1,000 requests, the figure `costPer1kSuccess` and `pricing.costBasis` are built on |

`availability` is the per-bucket fractional availability history, mapped from
the snapshot's `windows.<range>.series[].uptime` without interpolation. Buckets
are five minutes for 24h and one day for 7d/30d. The calculator unions and clips
recorded outages per country, using countries with a started census/pulse run
or an active outage in that bucket. Buckets without monitoring evidence, and
older snapshots without this field, remain null. The shared Availability metric
card displays this series for every tested product category.

## Browser targets

Browser targets (`engine = "browser"`, browser pulse) are measured per target
only: their runs never enter a network's window metrics, scores, rankings or
fleet figures, so those are the same with and without them. The snapshot
publishes each one at `w.targets.<id>.browser` (absent on network targets, and
in every snapshot before the browser pulse).

**Cards.** Show the targets whose `group` is null as cards: the Generic HTTP and
HTTPS targets plus the browser cards (`amazon_pdp`, `google_search`,
`linkedin_public`, `instagram_web`, `cloudflare_challenge`, `ai_answers`). A
group target (`ai_answers`) is measured over the union of its sources' tries
and is never scheduled itself; its `sources` is its breakdown. Each source
(`ai_overview`, `ai_chatgpt`, `ai_gemini`) is also listed as a target, with its
`group` set, so it is not a card of its own.

On a browser target's `targets[<id>]` entry (network answers, providers and
`productMeasurements`), besides `successRate`, `captchaRate`, `blockRate` and
`requests` as for every target:

| Field | Unit | Source (`w` = the 30d window) | N/A |
|---|---|---|---|
| `outcomes.ok`, `challenge`, `block`, `loginWall`, `timeout`, `proxyError`, `noContent`, `noAnswer`, `otherError` | fraction of the completed tries (they sum to 1) | `w.targets[id].browser.outcome_rates` (`login_wall`, `proxy_error`, `no_content`, `no_answer`, `other_error`) | the target is not measured for the product (or no try completed) |
| `pageLoadP50`, `pageLoadP90` | milliseconds, successful tries only | `w.targets[id].browser.page_load_ms.p50` / `.p90` | not measured, or no successful try |
| `sources[]` | group targets only (not written on any other target) | one entry per `w.targets[id].browser.sources` id, in that order | |
| `sources[].target`, `label` | | the source's id and its dataset target label | `label`: no label for the source |
| `sources[].successRate`, `requests` | fraction, tries | `w.targets[source].metrics.success_rate` / `.completed` (`requests` is 0 when unmeasured) | `successRate`: the source is not measured for the product |
| `sources[].outcomes.*` | fraction | `w.targets[source].browser.outcome_rates` | as `outcomes` |
| `engine` | `"camoufox"` or `"chrome"`: the browser engine whose tries make `outcomes`, `pageLoadP50`/`P90`, `successRate`, `captchaRate`, `blockRate` and `requests` (the one with the most completed tries in the window; ties go to Camoufox) | `w.targets[id].browser.engine` | not measured, or a snapshot from before the engine split |
| `engines` | an object by engine name, each engine's own figures, never added together (`{}` when the snapshot has none) | `w.targets[id].browser.engines` | |
| `engines.<name>.requests`, `successes` | tries | `w.targets[id].browser.engines.<name>.completed` / `.successes` | |
| `engines.<name>.outcomes.*` | fraction | `w.targets[id].browser.engines.<name>.outcome_rates` | no completed try of the engine |
| `engines.<name>.pageLoadP50`, `pageLoadP90` | milliseconds, successful tries only | `w.targets[id].browser.engines.<name>.page_load_ms.p50` / `.p90` | no successful try of the engine |
| `scores.success`, `challenge`, `block` | `engine`'s rank-based scores ("Browser scores" below), next to the raw rates | `w.targets[id].browser.scores` | `scores`: not measured, or a snapshot from before target scores |
| `engines.<name>.scores.*` | that engine's scores, ranked among that engine's networks only | `w.targets[id].browser.engines.<name>.scores` | as `scores` |

### Browser scores

TARGET-SCORES-1 (the calculator's `calculated/target_scores.py`, K-BP6) ranks
the networks per browser target, window and engine, and never ranks Chrome
against Camoufox. Each metric is `{score, rank, rankedOf, status}`:

| Field | Meaning |
|---|---|
| `score` | 1 to 10: `1 + 9 × (M − rank) / (M − 1)`, so the best network gets 10 and the last 1; null unless `status` is `ranked` |
| `rank` | the network's rank among the M ranked networks (1 is best); ties share the average of their ranks, so it may be fractional; null unless `ranked` |
| `rankedOf` | M, the number of networks ranked for that target, window and engine; null unless `ranked` |
| `status` | `ranked`; `not_enough_data` (this network has fewer than 100 completed tries of the engine in the window); `too_few_networks` (fewer than 3 networks are ranked); `all_similar` (the best and worst ranked rates are too close to tell apart: under the larger of 3 points and 1.96 standard errors of their difference) |

`success` ranks the `ok` share (higher is better), `challenge` the challenge
share and `block` the hard-block share (lower is better). The snapshot's
`rate` is not repeated: it is `outcomes.ok`, `challenge` and `block`.

`browserOverall.<engine>.success` (network answers, providers and
`productMeasurements`, from `w.browser_overall.<engine>`) is the network's
overall: `score` is the mean of its ranked targets' success scores weighted by
each target's difficulty (1 to 5), `rank` and `rankedOf` its place among the
networks with an overall, `status` the snapshot's, `targets` the ids it
averaged (`ai_answers` counts once for the AI sources; `all_similar`,
`not_enough_data` and `too_few_networks` targets are left out) and `weights`
each one's difficulty. There is no overall for `challenge` or `block`.

Not served yet (SUCCESS-SCORE-API-1 maps them): the snapshot also has a
success score for each HTTP target, `w.targets.generic_http|generic_https
.scores.success` (the same shape and rule, ranked on `metrics.success_rate`,
this page's `successRate`), and one overall success score per network,
`w.success_overall {score, rank, ranked_of, status, targets: {id: score},
weights: {id: weight}}`: the HTTP targets at weight 1 and the browser targets
(their main engine's score) at their difficulty, ranked as `browserOverall`.
`score`, `rank` and `rankedOf` are null when the network has no overall.

On the two test days (the #815 load on `proxy_reviews`) the browser outcome shares are
generated test data (BROWSER-SPREAD-1, model `per-network-v1`, scripts/scale/SIMULATE.md
"Each network's own browser outcomes"): each network has its own mix per target and engine,
its success share up to 18 points from the measured base, in the real networks' order where
it is known. AI Overview and ChatGPT had no answer measured in that load, so their Camoufox
success is centred on the owner's real measured rate instead (BROWSER-AI-SPREAD-1, a private
input, SIMULATE.md "A real base for the AI sources"), and they and `ai_answers` are ranked.
In production the shares are the measured ones.

`latencyP50`/`P90` stay null on browser targets: latency is the HTTP
targets' only (the Generic HTTP target's headline, other HTTP targets' display
only), and browser cards show `pageLoadP50`/`P90` instead. A browser
target never counts as another target in the window-latency rule
(`windowLatencyIsHTTP`).

## Records

| Entity | Fields | Source |
|---|---|---|
| `TapeEvent` | `id`, `kind`, `providerSlug`, `providerName`, `product`, `title`, `detail`, `signal`, `value`, `agoSeconds` + *added* `event`, `at`, `source`, `na` | Measured (`source` `measured`): `public.test_events`, mapped by the port of `tape.ts`; `kind` is the event's category (`census`, `pulse`, `incident` or `audit`) and `event` its raw kind (`ip_overlap`, `pulse_down`, `daily_test_finished`, ...; the framework's `estimate_hour_released` is published as `hour_released`, so no answer names the estimate); `at` is the event's time and `agoSeconds` is relative to the snapshot's `generated_at` (or `generatedAt`). Editorial (`source` `editorial`): `site.tape_events`, `kind` and `event` the row's `kind`, `at = occurred_at`, `agoSeconds = generatedAt − occurred_at`. `providerName` comes from the provider list. The dataset's `tape` is both in one time order (`at`), newest first; at the same time a measured row comes first. A measured row stands for every row of its incident (EVENTS-MERGE-1): its `id` is the incident's largest event id. |
| `ChangeEvent` | all + `source`, `na` | `site.changelog`; `providerName` from the provider list |
| `ChangeEntry` | `id`, `kind`, `network`, `title`, `detail`, `delta`, `unit`, `severity`, `agoSeconds`, `source`, `incident` (no `na`) | The website's changelog entry (`changelog-data.ts`), built from the outages, the window figures, `site.pricing_history` and `site.changelog`: `/v1/changelog` and the network answer's `changelog[]` (api/answers/changelog.md) |
| `UserReview` | all + `verificationLabel`, `source`, `na` | `site.reviews` (`posted_at` → `agoSeconds`); `verificationLabel` is the old site's `verificationLabels` text of `verification`: `Invoice verified`, `Run verified`, `Unverified`, `In review` (an unknown value as it is) |
| `CommunityRun` | all + `source`, `na` | `site.community_runs` (typed headline numbers, JSONB `targets`/`successSeries`/`latencySeries`); `providerName` from the provider list |
| `TargetMeta` | `id`, `label`, `category`, `passCondition`, `difficulty` + `source`, `engine`, `group`, `na` | `site.targets`, falling back to `public.targets`; `engine` is `public.targets.engine` (`"http"` or `"browser"`, null for a catalog-only target); `group` is the group target the snapshot names in `w.targets[id].browser.group` (null when none, by contract) |
| `TestScope` | all + `na` | `site.scopes` (`targets` is `"all"` when `all_targets`, else `target_ids`) |
| `RankingProfile` | all + `na` | `site.ranking_profiles` (`weights` JSONB, all 8 keys always present) |

`agoSeconds` of every `site.*` record is the dataset's `generatedAt` (the snapshot's
`as_of`, else the read time) minus its timestamp, never negative.

## Deviations from the TypeScript

The Go port differs from the website's `presentation.ts`, `window.ts`,
`overview.ts` and `tape.ts` only in these ways. A parity test enforced them while
the website lived in the old `proxy-index-api` repository; it went with the website.

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
   `public.test_events` first, then the editorial `site.tape_events` rows (what
   `LiveTape.astro` shows).
9. **Search.** The index is the website palette's (`pi-palette.tsx`), not the
   old site's: `net:` and `act:` ids, `/proxy-index/...` links, the residential
   networks with their taglines, the card targets only, the Proxy Index pages,
   the dataset and range actions. There is no `Countries` group, no apply, theme
   or copy action, no `startingPointIds` and no `maxResults` (the palette caps
   only each group).

`measuredTotals` uses the same fields as `totals`, aggregated by the API over
every provider without the record counts (with the demo fixtures gone, every
provider is `source = "measured"`).

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

## Per-page answers

These small answers (each at most 200 KB, `build.MaxAnswerBytes`) are computed
once per build from the published snapshot and the dataset's providers. Numbers
the API does not have are `null`, as everywhere else.

### Page answers in the dataset file

The dataset file holds every answer below that a page of the site draws, so a
download alone regenerates them (owner decision on DATASET-SIZE-1: the dataset
carries what the pages use). `pages` maps each request to its body, byte for byte
what the server serves for it: `/v1/networks`, `/v1/rankings?range=<r>` for 24h, 7d
and 30d (and `&kind=<kind>` for each proxy kind), `/v1/networks/<slug>?kind=<kind>` for every network and category (the
all-networks answer is `/v1/networks/all?kind=all`) and
`/v1/networks/<slug>/chart?kind=<kind>&range=<r>`. The request without `kind`
serves the network's `primaryKind`. The country answers are in it too:
`/v1/countries` and `/v1/countries/<CC>` (contract K11).

The page answers added from Phase 3 (`api/answers/`) are in it under their
requests with every parameter spelled out (DATASET-PAGES-2):

- `/v1/targets?kind=<kind>&range=<r>` for each proxy kind and range, and
  `&engine=<engine>` (`camoufox`, `chrome`) for each engine view; the request
  without `kind` and `range` is `kind=residential&range=30d`;
- `/v1/overlap?kind=<kind>&range=<r>` for each kind built (residential always,
  else the kinds measured) and range;
- `/v1/home?range=<r>` and `/v1/meta`;
- `/v1/changelog?range=<r>` for each range;
- `/v1/events/stats?range=<r>` for each range. The per-network bodies
  (`&network=<slug>`) are not in it. The server builds these answers again on
  every events refresh; the file's are the build's that wrote it, as for every
  page below;
- `/v1/networks/<slug>/pricing-history?kind=<kind>` for each network and proxy
  kind, and `/v1/networks/<slug>/pricing-history` (the body without `kind`,
  `kindDefault` true) for each network, `all` included.

Not in it: the paged reviews and community runs (`.../reviews`, `.../runs`),
like the paged place lists (`.../places`, `.../cities`); search and the N/A
manifest, which no page of the site's design draws (API-DATA-DESIGN-1).
`TestTheDatasetFileRegeneratesEveryPageAnswer` checks the file against the
answers. A rebuild that keeps the file (see "The dataset file" in `README.md`)
keeps its pages too: they are the build's that wrote it.

### `/v1/meta`

The reference data every page shares; every field in [answers/meta.md](answers/meta.md).
Each `ranges[]` entry is the range list's (`build.Ranges`) plus `hours`: the length of the
methodology window the range reads, from the published snapshot's
`methodology.settings.windows` (`configs/methodology.toml` `windows = [24, 168, 720]`), so 24,
168 and 720; null, and listed in `na` as `ranges[].hours`, for a range the snapshot has no window
for or when nothing is published. `windowHours` is the shortest window (24): the window a
24-hour figure covers; null and in `na` when nothing is published.

### `/v1/rankings?range=24h|7d|30d&kind=`

The list pages' rankings in their final order, so a page renders its list
(home leaderboard, network directory, compare table, true cost, the strip) at
once, without a request per network. One row per network. Without `kind` the
rows are every network with its headline product group; `kind` (`residential`,
`datacenter`, `mobile` or `isp`, else 400) lists only the networks whose
`kinds` hold it, each with that kind's product group (its score, window
statistics, pricing and trust; `kind` is the kind asked for). A kind no network
was measured in answers an empty list. One answer per range and kind is built
per build (3 + 12), each under 200 KB.

`position` is the list order: measured
networks by the `balanced` score of their product group for that
range (`windows[range].scores.balanced`: `score`, `grade`, `rank`,
`previous_rank`, the component values as `breakdown`, `peers` from the
components' `inputs.peers`), highest first; networks without a score follow by
name. `rank` stays the framework's rank among the ready networks of the same
kind, so two kinds can each have a `rank` 1. `standing {rank, peers}` is
the old site's `kindStanding`: `rank`, or 1 for a network with a score alone in
its kind (`peers` 1), which the framework leaves unranked ("#1 of 1"). A
catalog network without a snapshot product has no score or rank.

Row fields, from that range's `windowStats` (see "Window statistics of a
measured provider"): `successRate`, `successDeltaPts`, `latencyP50`, `latencyP90`,
`latencyP99` (the headline basis: under load with a load test in the window, else light load),
`latencyDeltaPct`, `uniqueIps` (the group's distinct IPs), `requests`,
`uptimePct`, `downtimeMinutes`, `costPer1kSuccess`, every per-range figure
(`captchaRate`, `blockRate`, `requestSuccessRate`, `countries`, `regions`,
`cities`, `asns`, `datacenterAsnShare`, `exclusiveShare`, `sharedShare`,
`observedDays`, `latencyUnderLoad`, `latencyNote`, `wireGbPer1k`; all of that
range, null without a snapshot product), `trend` (the window's success history
thinned to at most 26 values) and `trendT` (each trend point's time, minutes
before the build). `valueRank` is the position on the cost board of the row's
kind (cheapest `costPer1kSuccess` first, null when unpriced). Each row lists
its nulls in `na` with their reasons (`naReason`, `naReasons`; api/answers/README.md,
"Missing data"), and the answer its own (`cost.*`, `profiles` when empty).

From the provider's blocks (the kind's with `kind`): `tagline`; `listRate`
(`pricePerGbAtScale`, else `pricePerGb`), `pricePerGb`, `pricePerGbAtScale`,
`priceLabel` (the list-rate range, as a product's), `trial` (`site.pricing.trial`),
`hasTrial` (the trial text is not "none"; null without one), `trialKind` read
from the trial text: `none`, `sales` (a sales call or contact), `paid` (a price
or a refund window), `card` (free, card required), else `free`; null without
one; `authenticity` (`trust.residentialAuthenticity`), `kycVerified`,
`consentDisclosed`, `stickinessAdvertised`, `stickinessHoldRate`,
`blacklistHitRate`. `browserOverall.<engine>.success {score, rank, rankedOf,
status}` is the range's `windows[r].browser_overall` ("Browser scores" below),
null when the range has none.

The answer also carries `targets {all, measured, cards}` (the dataset's
targets, the measured ones, and the cards: measured targets of no group, as
`/v1/meta` `targetCounts`), the dataset's
`weights` and `profiles`, `fleet {requests, successes}` (the range's summed
over the rows listed; a network without a value adds nothing) and `cost`, the
true-cost summary over the rows listed with a `costPer1kSuccess`: `cheapest`
and `dearest` (`{slug, name, costPer1kSuccess}`, ties in list order), `median`,
`spreadX` (dearest ÷ cheapest) and `priced` (how many); `cheapest`, `dearest`,
`median` and `spreadX` are null when none is priced.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","range":"24h","profile":"balanced",
 "targets":{"all":19,"measured":17,"cards":10},"weights":{"reliability":22,"speed":14,"...":0},"profiles":[],
 "fleet":{"requests":1840000,"successes":1802000},
 "cost":{"cheapest":{"slug":"net00","name":"Net 00","costPer1kSuccess":0.31},"median":0.52,
  "dearest":{"slug":"net07","name":"Net 07","costPer1kSuccess":1.12},"spreadX":3.61,"priced":12},
 "networks":[
 {"position":1,"slug":"net00","name":"Net 00","source":"measured","kind":"residential","kinds":["residential"],
  "tagline":"Residential proxies in 190 countries","rank":1,"peers":18,"standing":{"rank":1,"peers":18},"previousRank":2,
  "score":91.4,"grade":"A","breakdown":{"reliability":96,"speed":88,"uptime":null,"...":null},
  "successRate":0.982,"successDeltaPts":0.4,"latencyP50":1113,"latencyP90":2210,"latencyP99":4120,"latencyDeltaPct":-2.1,
  "uniqueIps":15320,"requests":102000,"uptimePct":99.9,"downtimeMinutes":1.4,"costPer1kSuccess":0.31,"valueRank":1,
  "listRate":3,"pricePerGb":4,"pricePerGbAtScale":3,"priceLabel":"$3.00–$4.00/GB","trial":"1 GB free, no card",
  "hasTrial":true,"trialKind":"free","authenticity":0.97,"kycVerified":true,"consentDisclosed":null,
  "stickinessAdvertised":"30 min","stickinessHoldRate":0.94,"blacklistHitRate":0.02,
  "captchaRate":0.01,"blockRate":0.004,"requestSuccessRate":0.981,"countries":212,"regions":1800,"cities":9100,"asns":2400,
  "datacenterAsnShare":0.012,"exclusiveShare":0.71,"sharedShare":0.29,"observedDays":null,"latencyUnderLoad":true,
  "latencyNote":"HTTP, under load","wireGbPer1k":0.09,"trend":[0.97,0.98,0.99],"trendT":[1380,720,0],
  "browserOverall":{"camoufox":{"success":{"score":7.4,"rank":2,"rankedOf":9,"status":"ranked"}}}},
 {"position":21,"slug":"net20","name":"Net 20","source":"measured","kind":"residential","kinds":["residential"],
  "tagline":null,"rank":null,"peers":null,"standing":{"rank":null,"peers":null},"previousRank":null,"score":null,
  "grade":null,"breakdown":{"reliability":null,"...":null},"successRate":null,"...":null,"valueRank":null,
  "listRate":null,"priceLabel":null,"trial":null,"hasTrial":null,"trialKind":null,"trend":[],"trendT":[],
  "browserOverall":null}]}
```

### `/v1/networks/all`

Every network together, as one more network in the shape of
`/v1/networks/{slug}`: `network` (`slug`, `kind` `"all"`, `kinds` every tested
category, `rank`/`score` null; `uniqueIps`, `countries`, `latencyP50`,
`successRate` of the 30d range), `networks` (how many networks published a
product group), `productGroups`, `requestedCountries`, `days` and `ranges`.

- Summed over every product group, exactly: `requests`, `successes`,
  `successRate` (successes ÷ requests), per day as well.
- Distinct, from the snapshot's every-group `fleet` entry of the range
  (`cohort` `"*"`, proxy-index `fleet_totals`): `uniqueIps` (`unique_ips`: an
  exit IP seen by several networks or requested countries counts once),
  `latencyP50/P90/P99` (`metrics.latency_ms`: light-load latency over every
  group, merged as within a cohort) and each day's `uniqueIps` (the 30d entry's
  `series[].unique_ips`). A snapshot without that entry leaves them null; they
  are never a sum of the networks' figures, and the summed `ipsSum` totals are
  null here.
- Places: `exitCountries`, `lists` and the top 50 `countries` are the union of
  every network's places (so `lists.country`, `.region`, `.city`, `.asn` are the
  distinct places seen); a place row adds the networks' requests and its
  `ipsSum` adds their IPs (an upper bound, as on a network's rows).
  `/v1/networks/all/places?dimension=&range=&q=&page=` pages them.

A network's own answer has the same distinct fields: with one product group in
the category they are that group's (`pool.unique_ips`, `metrics.latency_ms`,
`series[].unique_ips`); with several they are null, because the groups' IPs
can repeat and percentiles do not add up.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","kind":"all","networks":18,"productGroups":720,"requestedCountries":["AA","AB","..."],
 "network":{"slug":"all","name":"All networks","source":"measured","kind":"all","kinds":["residential"],"rank":null,"score":null,
  "uniqueIps":4321,"countries":240,"latencyP50":1113,"successRate":0.981},
 "days":[{"day":"2026-10-05","requests":120000,"successes":117700,"successRate":0.981,"ipsSum":null,"uniqueIps":2210}],
 "ranges":{"24h":{"requests":4000,"successes":3925,"successRate":0.981,"ipsSum":null,"uniqueIps":1630,
  "latencyP50":1113,"latencyP90":1395,"latencyP99":3292,"exitCountries":240,
  "countries":[{"key":"US","countryCode":"US","country":"United States","ipsSum":5100,"requests":900,"successes":880,
   "successRate":0.978,"latencyP50":980}],
  "lists":{"country":240,"region":1800,"city":9000,"asn":2500}},"7d":{"...":null},"30d":{"...":null}}}
```

### `/v1/networks/{slug}?kind=` (the network page)

Besides the summed totals and places above, a network's answer carries what
`/v1/providers/{slug}` used to, without its long lists:

- `profile`: the Provider's presentation (`source`, `slug`, `name`, `tagline`,
  `kinds`, `primaryKind`, `hq`, `founded`, `claim`, `verdict`, `strengths`,
  `weaknesses`, `bestFor`, `website`, `products`, `measuredProduct`, `score`,
  `reviews`, `framework`) and the category's measured blocks as in
  `productMeasurements.<kind>` (`pool`, `performance`, `pricing`, `trust`,
  `targets`, `ipSamples`, `incidents`, `overlap`) plus `tileRanks.<kind>`, with
  its own `na` (the profile's null paths, such as `pricing.listRate`) and
  reasons (api/answers/README.md, "Missing data").
  Left out: `geo` and `asns` (paged by `/places`), `series` (the chart
  answers) and `countryLatencyRanks` (the place rows carry their ranks). The whole
  provider stays in `/v1/dataset`. Beside the Provider's fields (NETWORK-EXTRAS-1):
  - `score.peers`: the ready networks of its kind the score was set against
    (`ScoreView.Peers`, the snapshot's `components.*.inputs.peers`); null when
    unknown. `score.standing {rank, peers, note}`: the rankings rows' `standing`
    (the framework's rank, or 1 for a scored network alone in its kind) and
    the old site's `kindStanding` label as `note`: `#1 of 1 in its kind`,
    `#3 of 18 in its kind`, `#3` without peers, or `Unranked`.
  - `pricing.listRate` (`pricePerGbAtScale`, else `pricePerGb`), `pricing.hasTrial`
    and `pricing.trialKind` (`free`, `card`, `paid`, `sales` or `none`): the
    rules of the rankings rows (CMP-12), null without a price or a trial text.
  - `schedule`: the schedule part of the generated tagline (`measured on a
    census, pulse and load schedule`, `measured on a census and pulse
    schedule` or `measured HTTP pool sample`) of the category's snapshot
    product, also when a catalog tagline replaces the generated one; null for
    a category that is not measured.
  - `protocol {label, targets, summary, schedule, cadence}`: the measured
    product's label (`Rotating residential`, ...), the ids of the category's
    `targets` with requests, the product's `site.scopes` `summary` and its
    `cadence` as `schedule` (null without a scope row), and `cadence`, the
    production cadence (`docs/api-web-map.md` section 14): "One daily test per
    country and one browser run per target per day". The bucket resolution
    stays `stats[r].range.resolution`.
  - `reviews` (`count`, `verifiedCount`, `mean`, `distribution`, `verifiedMean`)
    summarises the answer's reviews (the kind's, below), plus `runVerifiedCount`,
    the reviews with verification `run_verified` ("backed by a submitted run";
    `verifiedCount` also counts `invoice_verified`). `/v1/dataset`'s provider
    `reviews` still counts every product.
- `stats`: `windowStats` of that category per range (see "Window statistics of
  a measured provider", with its per-range figures `captchaRate` to
  `wireGbPer1k`) without `success`, `availability`, `latency`, `pool`
  and `bands`, plus:
  - `ranks`: `successRate`, `uptimePct`, `datacenterAsnShare`, `valueRank`
    and `listRate`, each `{rank, peers}` among the networks of the answer's
    kind in that range: the rows of `/v1/rankings?range=&kind=`, computed once
    per build (`build/ranks.go`). Ranks are competition ranks (ties share a
    rank, as `tileRanks`): success rate and uptime highest first, hosting
    share, cost per 1,000 successes (`valueRank`) and list $/GB lowest first.
    `peers` counts the networks with a value; `rank` is null without one.
  - `alternative {slug, name, successRate, latencyP50, cheaperBy}`: the old
    site's pick (`provider-view.ts` `cheaperAndBetter[0]`): the first network
    of the kind's ranking (in its order) whose `costPer1kSuccess` is lower and
    whose success rate is at most 2 points below this network's;
    `cheaperBy = 1 − its cost ÷ this network's`. Null when none qualifies or
    this network has no cost or success rate.
  - `claimShare`: the range's `uniqueIps` ÷ `profile.pool.claimedPoolSize`
    (the old site's "pool vs claim"), null without a claimed pool size.
  - `na`, `naReason`, `naReasons`: the range's nulls and their reasons
    (api/answers/README.md, "Missing data").
- the network's newest `reviews` (published), `communityRuns`, `changelog`
  and `tape` rows, at most 20 each (`build.RecordCap`; the lists are newest
  first), and `records`, the full length of each list. The whole history
  stays in `/v1/dataset`. Reviews, community runs and tape rows are the
  answer's kind only (NET-RECORDS-KIND): a review's and a run's product kind
  (`residential_rotating`, ...) of that category, a tape row's `product`
  equal to it. The changelog has no product and is the network's: the
  `ChangeEntry` rows of `/v1/changelog?range=30d` whose `network.slug` is the
  network (its incidents, figure moves, price changes and `site.changelog`
  rows), so the two answers never disagree; `records.changelog` counts them
  all, including any `/v1/changelog` cuts (api/answers/changelog.md).

The answer's own nulls (`network.*`, `days[]`, `ranges.*`) and its empty
`reviews` and `communityRuns` are in its `na`, with reasons; each record
carries its own `na`.

The all-networks answer has `profile: null`, empty `stats` and empty records.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","kind":"residential","productGroups":40,"network":{"slug":"net00","...":null},
 "days":[],"ranges":{"24h":{"...":null}},
 "profile":{"source":"measured","slug":"net00","name":"Net 00","primaryKind":"residential","score":{"composite":91.4,"rank":1,"...":null,
  "peers":18,"standing":{"rank":1,"peers":18,"note":"#1 of 18 in its kind"}},"schedule":"measured on a census, pulse and load schedule",
  "protocol":{"label":"Rotating residential","targets":["generic_http"],"summary":null,"schedule":null,"cadence":"One daily test per country and one browser run per target per day"},
  "pool":{"uniqueIps24h":1630,"...":null},"performance":{"successRate":0.981,"latencyP50":1113,"...":null},
  "pricing":{"pricePerGb":4,"pricePerGbAtScale":3,"...":null,"listRate":3,"hasTrial":true,"trialKind":"card"},
  "trust":{"...":null},"targets":[],"ipSamples":[],"incidents":[],"overlap":{"...":null},"tileRanks":{"24h":{"...":null}},"na":[]},
 "stats":{"24h":{"range":{"id":"24h","...":null},"successRate":0.981,"latencyP50":1113,"uniqueIps":1630,"uptimePct":99.9,"...":null,
  "ranks":{"successRate":{"rank":1,"peers":18},"uptimePct":{"rank":2,"peers":18},"datacenterAsnShare":{"rank":4,"peers":18},
   "valueRank":{"rank":5,"peers":15},"listRate":{"rank":6,"peers":15}},
  "alternative":{"slug":"net04","name":"Net 04","successRate":0.975,"latencyP50":1210,"cheaperBy":0.31},"claimShare":null}},
 "reviews":[],"communityRuns":[],"changelog":[],"tape":[],"records":{"reviews":0,"communityRuns":0,"changelog":0,"tape":0}}
```

### `/v1/networks/{slug}/chart?kind=&range=`

The histories `windowStats` draws for that category and range: `success`
(`{t, v}` success rate), `availability` (`{t, v}`), `latency`
(`{t, p50, p90, p99}`), `latencyUnderLoad` (`{t, p50, p90, p99}` from
`series[].latency_under_load_ms`, a point per bucket, null values where a
bucket has none or the window's latency is not `generic_http`'s alone),
`pool` (`{t, v}` distinct IPs) and `bands` (outages).
`t` is minutes before the build's as-of time; points are five minutes apart
for 24h and a day apart otherwise. `labels` holds, per history (`success`,
`availability`, `latency`, `latencyUnderLoad`, `pool`), the old site's label
of each point in order (format.ts `bucketLabel` of `t`: `"now"`,
`"25 min ago"`, `"3h 5m ago"`, `"1 day ago"`, `"6 days ago"`). For `all` the series is the every-group
fleet entry's (`series[]`: `success_rate`, `latency_ms` light-load, distinct
`unique_ips` per bucket, buckets without requests left out); `availability`
`latencyUnderLoad` and `bands` stay empty there, since uptime is a
per-product fact and the fleet entry has no latency under load.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","network":"net00","kind":"residential","range":"24h",
 "success":[{"t":1435,"v":0.98}],"availability":[{"t":1435,"v":1}],"latency":[{"t":1435,"p50":1110,"p90":1390,"p99":3290}],
 "pool":[{"t":1435,"v":212}],"bands":[],"latencyUnderLoad":[{"t":1435,"p50":1450,"p90":1770,"p99":3970}],
 "labels":{"success":["23h 55m ago"],"availability":["23h 55m ago"],"latency":["23h 55m ago"],"latencyUnderLoad":["23h 55m ago"],"pool":["23h 55m ago"]}}
```

## IP numbers from the hour formula (API-ESTIMATES-1)

A daily test's product group whose windows (24h, 7d, 30d) hold released
estimated hours carries the framework's `estimated` block beside its measured
figures (proxy-index `estimates/window.py`). The API serves every IP count it
derives from such a window as the hour formula's daily estimate, a plain whole
number (rounded half up) in the same fields as before: no answer has an
`estimated` key, flag or label, and the window's measured distinct IP count is
not served for that window (contract K8, owner decision Q6).

| API value | From the window's `estimated` block |
|---|---|
| A window's unique IPs: provider `pool.uniqueIps24h`/`uniqueIps30d`, window statistics `uniqueIps`, network totals `uniqueIps` and `ipsSum`, the unique-IP tile and its rank, the dataset totals built from them | `unique_ips.value` |
| A place or breakdown row's IPs: `geo[].ips` (and its share), `asns[].ips` (and its share), network and country place rows `ipsSum` (country, region, city, ASN), their ranks, `/v1/countries` `ipsSum` | the measured row's IPs × `breakdown_scale` |
| The 24h IP chart (`series.uniqueIps24h`, chart and window `pool`) | one point per released hour (`series[]`, `t` from the hour's start) |
| A 7d/30d daily IP point (`series.uniqueIps30d`, chart and window `pool`, network `days[]`) | the measured day's IPs × `breakdown_scale`, like a place row (hourly figures are not added up: an IP seen in several hours would count once per hour) |

A daily group's cohort is one requested country, so its per-country figure
(`countries[]`) is its window figure. Requests, success, latency, uptime,
outages and overlap stay measured. A window without the block (census-pulse
groups, a window with no released hour) keeps its measured IPs, and so do a
row or daily point when the block has no `breakdown_scale`, and the
every-group fleet figures of `/v1/networks/all`.

A window with no run of its group (no completed request in it) has no IP
figure (NO-RUN-WINDOW-1): its unique IPs, `ipsSum` and IP chart points are
null or empty, with the reason `no_run_in_window` (`answers/README.md`,
"Missing data"), even when the block holds an estimate for it. A window with a
run keeps the mapping above.

Coverage rows (`/v1/networks/{slug}/coverage`, `lists.*.city_asn`) follow the same
mapping as any breakdown row: their `ips` are the measured row's IPs × `breakdown_scale`.

The tape keeps a row per released hour (event kind `estimate_hour_released`,
an event, not a value), titled "Hourly update for HH:00 UTC" ("Hourly update
released" when the hour is missing) with that hour's IPs as its value: no
answer text names the estimate (owner decision on the API-ESTIMATES-1 review): its
raw kind on a tape row (`event`) is `hour_released`.
The `/v1/events` rows keep the framework's event kind name.

**One row per incident (EVENTS-MERGE-1).** The detector writes one event per
target, so an outage seen by the `generic_http` and `generic_https` pulses of
a slot is two events. Every list a page reads (`/v1/events` without
`target=`, the dataset's and so the home and network tapes) shows the rows of
one non-routine kind with a target and the same product, country, region and
pulse slot (the batch's `scheduled_for`, else the same `at`) as one row: the
`generic_http` row, else the first written, with the group's largest `id` and
`targets` (new on `/v1/events` rows: the group's targets, sorted; a single
row's own target, `[]` without one). `/v1/events/stats` counts merged rows.
Routine rows (census, `pulse_finished`) stay one per target. The stored
events are untouched.

## Payload v4 (network summaries) (API-SUM-1)

A build whose head has `schema_version` 4 and `networks` (proxy-index calculated
contract K9) is read from its head and its network lists (contract K10) only: no
product part and no group list. The head is read without its `parts` count, its
`overlap` rows (a network's are its windows' `overlap.peers`) and its per-cohort
`fleet` rows; the every-group `fleet` entries (cohort `*`) stay. That is the head's
`api_head`, stored when the build is published (calculated v14); a build published
before v14 has none, and the API builds the same head from its stored one. The API serves
no build before v4: while the current build is older, every data route answers
503 and `/healthz` has `status` `unavailable` and `reason`
`calculation older than payload v4: recalculate` (README, "Endpoints").

Each network entry is served as one product group of the payload, so every answer
keeps its fields and rules. Per network and proxy kind there is exactly one group:

| Field the build reads | From the network entry |
|---|---|
| `id`, `cohort` | As published: `<provider>:<kind>:network`, `network` (incident ids `outage:<id>:…` carry it) |
| `product_id`, `provider`, `name`, `kind`, `product`, `census`, `pricing`, `outages` | As published (the headline product's) |
| `criteria.country` | The one requested country when `requested_countries` has exactly one, else null |
| A window's scalars, `metrics`, `uptime`, `pool` figures, `targets`, `series`, `scores.balanced`, `success_delta`, `estimated`, `overlap` shares | As published |
| `breakdowns.country`, `pool.countries_breakdown` | Every country row of the window's network list |
| `breakdowns.city_asn` | Every City × ASN row of the list |
| `pool.asns_breakdown` | The 50 ASN rows with most IPs (most IPs first, then Python's `str()` order) |
| Region, city and ASN place rows (`/places`, place ranks, country pages) | Every row of the list, read whole |
| `breakdown_entries` | The list's row counts |
| The snapshot's 30d `overlap` rows (`overlaps` of the ownership card) | One row per pair of networks of a kind and window, from `overlap.peers`, with cohort `network`; peers with equal shared IPs keep the networks' order, then their `overlap.peers` order |

A list row keeps IPs, requests, successes and p50 only: `observations`,
`challenges`, `blocks`, `p90` and `p99` of these rows are null (no answer reads
them).

## Payload v4 list answers (API-COVERAGE-1)

- A network answer's `productGroups` is its summary's `criteria.groups` (a summary
  without it counts as one), and `requestedCountries` its
  `criteria.requested_countries`, upper case and sorted; the all-networks answer
  sums the groups and joins the countries;
- A network's provider `asns` is every ASN row of its 30d network list with IPs,
  most IPs first (then Python's `str()` order).

A build reads every list answer from the compact network lists, their rows read
from the database when needed (API-MEM-1): it keeps no place row per list row. A place list (`/places`, `/v1/countries/{cc}/cities`
and `/regions`, `/v1/networks/all/places`) is its networks' compact lists, merged
and ordered as before (rows of one place key add up; most IPs first, then key,
then network and kind) only when a page is asked for. The build keeps each list's
row count and its first page without `q` as rows (at most 50 a list, about 6 MB
at QA-1; a Coverage list's first page likewise). Every page, the first included,
is rendered when first asked, at fast compression, and kept in the version's page
cache (1,024 pages) until the next build; a first page the cache drops is rendered
again from the rows the build keeps, without merging the list. The Coverage answers
read the City × ASN rows from the list, in list order.

Requests, success rate and latency of a place row (MEASURED-FIELDS-1): a country row
(`ranges.<range>.countries[]`, `/places?dimension=country`, country pages) carries the
requests, successes and light-load p50 of the tests that requested that country (daily tests
record their requests per run, not per exit address: the calculator counts a run without
exit-place rows towards its requested country, merged into that country's row) and of the
pulses' rows that exited there. A region, city or ASN row has requests and latency only where
a test records them per place (pulses); for daily tests it has `requests: 0` and a null
`successRate` and `latencyP50`, listed in the page's `na` with `not_measured_per_place`
(api/answers/README.md). A `.../places` page carries `na`, `naReason` and `naReasons` for its
rows (`rows[].successRate`, `rows[].latencyP50`); a Coverage row's null `latencyP50` has the
same reason.

On the two test days (the #815 load on `proxy_reviews`) these rows are generated test data
(PLACE-LATENCY-TEST-1, model `place-v1`, scripts/scale/SIMULATE.md "Per-place buckets of the
daily tests"). Each daily test's requests, successes and latency are split over its exit
places in the requested country. So the region, city and ASN rows' `requests`, `successes`
and `latencyP50`, and the Coverage rows' `latencyP50` (`rows[i][8]`), are set where the row
has successes, and null where it has none. The country rows keep their requests and
successes. In production these rows stay as above: `requests: 0`, a null latency,
`not_measured_per_place`.

Latency on the two test days is generated test data too (LATENCY-TRUTH-1, model
`real-median-v1`, scripts/scale/SIMULATE.md "Real latency medians on the stored days"). Each
network's level follows its real successes-only median, so the light-load and under-load
`latencyP50`/`P90`/`P99`, the `speed` breakdown and its rank, and the place, Coverage and tape
latency figures keep the real order of the networks. Exceptions: a network without a real
median keeps a generated level, and networks within about 7 % of each other can swap places
under load, since the load factor (1.4) moves by up to ±0.05 per network and day. Everywhere,
the latency counts successful requests only; a failed request has no latency.

Answers of the network lists (contract K11):

| Path | Answer |
|---|---|
| `/v1/networks/{slug}/coverage?kind=&range=&q=&page=` | One page (50 rows) of the network's City × ASN rows with IPs (the headline group's list), most IPs first, ties in list order: `network`, `kind`, `range`, `query`, `page`, `pageSize`, `total`, `pages`, `rows`. A row is `[countryCode, country, region, city, asn, asnName, usageType, ips, latencyP50, countryLatencyRank, countryLatencyPeers]`: IPs follow K8 (× `breakdown_scale`, rounded half up), `latencyP50` is the row's p50 (null when it has none), and the rank and peers are the provider's `countryLatencyRanks` value of the row's country (null rank, 0 peers when the country has none). `q` keeps rows where every word occurs, case-insensitive, in the country code, country, region, city, `AS<n>` (or the bare ASN number) or ASN name; with `q` the answer also has `totals` `{ips, countries, cities, asns}` over every matching row (IPs summed as the rows show them, distinct country codes, cities and ASN numbers). |
| `/v1/countries/{cc}/regions?range=&q=&page=` | One page of the country's regions across networks, in the shape and rules of `/cities`. |
| `/v1/places/region/{key}?range=` and `/v1/places/city/{key}?range=` | `generatedAt`, `dimension`, `key`, `range` and `rows`: every network's row in that region or city (the place key of the place lists, `CC\|region_code\|region` or `CC\|region_code\|region\|city`, URL-escaped), in list order, with `ipsSumRank` and `latencyRank` among the networks of the same kind, as a country answer ranks them. 404 when no network has that key in that range. |

In a City × ASN row (NET-COUNTRY-RANK-NOTE, NET-USAGE-LABEL):

- `countryLatencyRank` and `countryLatencyPeers` are the row's **country's**
  latency rank among the networks of the same kind, cohort and period, the
  same for every row of that country; they are not a rank of the City × ASN
  row itself. A page that prints the rank under the row's latency should say
  it is the country's.
- `usageType` is the DB-IP usage type of the ASN as enrichment stored it
  (lower case, null when unknown); the API serves no display label. The
  values seen are `residential`, `consumer`, `business`, `cellular` and
  `hosting` (the calculator also treats `datacenter` as hosting:
  `HOSTING_USAGE_TYPES`). A page maps them, for example `residential` and
  `consumer` to "Residential", `hosting` and `datacenter` to "Hosting",
  `cellular` to "Mobile" and `business` to "Business".

## The dataset file of a v4 build (API-DATASET-2, DATASET-SIZE-2)

A v4 build's dataset file holds what the pages use (contract K11, the owner's
rule on DATASET-SIZE-1): every page answer, and of every network list its first
page, its exact total and its page count (owner decision on DATASET-SIZE-2,
2026-11-06: option 1). Every further page, a search and the lists built across
networks are read from the API, which still serves every page.

- **`providers`:** from the network summaries. A network's City × ASN rows are
  its Coverage answer's (`lists.*.city_asn` holds its first page).
- **`pages`:** as before (see "Page answers in the dataset file"), plus
  `/v1/countries` and every `/v1/countries/<CC>`, byte for byte.
- **`lists`:** between `pages` and `snapshot`:
  `lists[slug][kind][range][dimension]`, ranges `24h`, `7d` and `30d`, dimensions
  `country`, `region`, `city`, `asn` and `city_asn` in that order. Each is one
  list, `{"total": <rows>, "pages": <pages>, "rows": [<row>, ...]}`: `total` and
  `pages` are the list answer's own (`pages` = `total` ÷ 50, rounded up, 0 for an
  empty list) and `rows` its first page (at most 50 rows), each row an array:

| Dimension | Row | List |
|---|---|---|
| `country` | `[countryCode, country, ipsSum, requests, successes, latencyP50]` | `/v1/networks/{slug}/places?kind=&dimension=&range=` without `q` |
| `region` | `[countryCode, country, regionCode, region, ipsSum, requests, successes, latencyP50]` | The same |
| `city` | `[countryCode, country, regionCode, region, city, ipsSum, requests, successes, latencyP50]` | The same |
| `asn` | `[asn, asnName, ipsSum, requests, successes, latencyP50]` | The same |
| `city_asn` | `[countryCode, country, region, city, asn, asnName, usageType, ips, latencyP50, countryLatencyRank, countryLatencyPeers]` | `/v1/networks/{slug}/coverage?kind=&range=` without `q`: the City × ASN rows with IPs of the network's headline list, most IPs first (ties in list order) |

A place row is the answer's row (merged across the network kind's lists, in
the answer's order: most IPs first, then key) with the fields the place rows of
`/places` show, in that answer's key order, without the derived `key` and
`successRate` and without the fields a dimension never has. Its `countryCode`
is the upper-case code of the key; IPs are the served values (K8: × the window's
`breakdown_scale`, rounded half up); `requests` is the row's completed requests;
a missing number or name is `null`. Networks (by slug) and their kinds are in
the order the all-networks list merges them, the snapshot's; a published group
of a provider kept off the site (`listed = false`) has no network and so no
lists here, although the all-networks list counts it.

How a reader regenerates a list's first page (`TestTheDatasetFileHoldsEveryListsFirstPage`
does exactly this, from the file alone, and reads every further page from the
answers):

- **Place key:** `country`: the country code; `region`: `CC|regionCode|region`;
  `city`: `CC|regionCode|region|city`; `asn`: the number.
- **`successRate`:** `successes ÷ requests` when both are known and requests are
  above 0, else null.
- **The page:** `scope` `<slug>/<kind>`, `query` `""`, `page` 1, `pageSize` 50,
  and the list's `total`, `pages` and rows; a Coverage page has `network` and
  `kind` instead of `scope`.
- **Page 2 and on, searches (`q`), `/v1/networks/all/places`,
  `/v1/countries/{cc}/cities` and `/regions` and `/v1/places/{dimension}/{key}`:**
  from the API. The file no longer holds every row, so it cannot regenerate them.

The file's lists are the first pages the build keeps (`build.DatasetLists`), so
writing it reads no list row from the database. It is compressed at gzip level 6
and brotli quality 5 (4 MB window). At SUM-MEM-1's run A shape (18 networks ×
249 countries, `TestTheRunADatasetFileIsSmallAndQuickToWrite`) the file is about
9.2 MB (gzip 0.5 MB) and is written, with its `.gz` and `.br`, in about 0.3 s; the
test fails above 15 MB or 2 s. With every list row it was about 0.5 GB.

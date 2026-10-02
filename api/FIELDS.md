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
  list a measured provider never collected (plans, IP samples, incidents, leak checks,
  price history, best-for...) is listed in `na`.
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
fleetWindow`). Otherwise they run over the measured providers. The demo round-trip
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
| `score.breakdown.*` | `site.providers.score_breakdown` | `w.scores.balanced.components.<key>.value`; all 8 keys always present | data | ✱ |
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
| `performance.successRate` | `w.metrics.success_rate` | data | ✱ |
| `performance.successRateDelta24h`, `successRateDelta30d` | not mapped (the per-window `success_delta` is in `/v1/providers/{slug}` `windows[].successDeltaPts`) | always | ✱ |
| `performance.latencyDelta24h`, `ttfbP50` | none | always | ✱ |
| `performance.latencyP50`/`P90`/`P99` | `w.metrics.latency_ms.p50/p90/p99` | data | ✱ |
| `performance.captchaRate`, `blockRate` | `w.metrics.challenge_rate`, `block_rate` (snapshot v2) | data (always for a v1 snapshot) | ✱ |
| `performance.timeoutRate` | `w.metrics.timeout_rate` | data | ✱ |
| `performance.uptimePct` | `w.metrics.uptime × 100` | data | ✱ |
| `performance.downtimeMinutes24h`, `bandwidthMb24h` | not mapped (the snapshot has `uptime.downtime_s` and `metrics.bytes_down`) | always | ✱ |
| `performance.requests24h` | `day.metrics.completed` | data | |
| `pricing.model`, `trial`, `costBasis` | `site.pricing.model`, `trial`, `cost_basis` | catalog (TS used `"per_gb"`, `"Not collected"` and a sentence) | |
| `pricing.pricePerGb`, `pricePerGbAtScale`, `minCommit`, `avgPayloadKb`, `headlinePriceRank`, `valueRank` | `site.pricing.*` | catalog | ✱ |
| `pricing.costPer1kSuccess` | `site.pricing.cost_per_1k_success`, else derived as `avg_payload_kb × 1000 / 1024² GB × price_per_gb / successRate` (rounded to 4 dp), with a generated `costBasis` | catalog (needs $/GB and payload) | ✱ |
| `pricing.plans[]` | `site.pricing_plans` | catalog (`na: pricing.plans` while empty) | |
| `trust.datacenterAsnShare`, `claimGap` | `w.pool.datacenter_asn_share` (both, as presentation.ts), else `site.trust_signals` | data | ✱ |
| `trust.residentialAuthenticity`, `blacklistHitRate`, `stickinessHoldRate` | `site.trust_signals.*` | catalog / always (not measured by the framework) | ✱ |
| `trust.stickinessAdvertised`, `kycVerified`, `consentDisclosed` | `site.trust_signals.*` | catalog (TS used `"Not tested"`, `false`, `false`) | |
| `trust.leakChecks[]` | `site.trust_signals.leak_checks` | always (not run by the framework) | |
| `targets[<id>]` | one row per dataset target. `successRate`, `captchaRate`, `blockRate`, `latencyP50`, `latencyP90`, `requests` come from `w.targets[id].metrics` (`requests` is 0 when unmeasured) | data (every unmeasured target is listed) | ✱ |
| `targets[<id>].delta24h` | none | always | ✱ |
| `series.success24h`/`success30d` | completed buckets of `day`/`w` `.series` → `{t: minutes before as_of, v: success_rate}` | data | ✱ (values) |
| `series.latency24h` | `day.series` p50/p90/p99 | data | ✱ |
| `series.latency30d`, `uniqueIps24h`, `uniqueIps30d` | `series[].latency_ms.p50` / `unique_ips` | data | ✱ |
| `series.pricePerGb90d` | none | always | |
| `ipSamples[]` | not published for measured providers (presentation.ts leaves it empty) | always | |
| `incidents[]` | not mapped (outages are in `measurement.outages`) | always | |
| `geo[]` | `w.pool.countries_breakdown` with `share = ips / unique_ips`, plus `w.breakdowns.country` p50/success rate (v2) | `latencyP50`/`successRate` data (always for v1) | ✱ |
| `asns[]` | `w.pool.asns_breakdown`, with `usage_type` mapped to hosting/mobile/business/eyeball | data | |
| `overlap.exclusiveShare`, `sharedShare` | `w.overlap.exclusive_share`, `shared_share` | data (needs a peer in the cohort) | ✱ |
| `overlap.overlaps[]` | snapshot `overlap[]` rows for the product's cohort and 30d: `sharedIps`, `shareOfOurs = shared/ips_ours`, `shareOfTheirs`; `requestsBothSides` null | data / always for `requestsBothSides` | |
| `overlap.likelyUpstream` | none (reseller detection is not computed) | always | |

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

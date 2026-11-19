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
- Times (CONSISTENCY-1, #982): every time in every answer, list page, events answer,
  `/healthz` and the dataset file is RFC 3339 in UTC with `Z` and milliseconds
  (`2026-11-11T14:09:42.371Z`, `snap.JSISO`; the snapshot's and the database's times are
  rewritten to it, to the millisecond); date-only fields (`checkedAt`, `invoiceMonth`)
  stay `YYYY-MM-DD`. `/v1/events` `since` still reads every ISO form; a `since` of whole
  milliseconds stands for the end of its millisecond, so polling since the newest `at`
  never returns that event again. `TestEveryTimeHasOneForm` walks every body.
- `na` paths: dots for object keys, `[]` for array elements, `targets[<id>]` and
  `products[<kind>]` for keyed lists, `productMeasurements.<category>.…`.
- Null by contract (never in `na`, except in the page answers, which list
  them with a reason code: MEASURED-FIELDS-1, below):
  - `pricing.plans[].gb` / `.ips`: per-IP / per-GB plans.
  - `framework` for a catalog row not linked to a framework provider.
  - tape `at` for non-measured rows.
  - tape `country`, `region`, `target`, `severity` (an event without one, every
    editorial row) and every null in a tape or event row's `values`.
  - review `invoiceMonth`, `linkedRunId`, `providerReply`.
  - community run `rejectionReason`, `overlapShare`.
  - changelog `providerSlug`, `providerName`, `delta` for fleet-wide entries.
  - a `ChangeEntry`'s nulls (`/v1/changelog` and the network answer's
    `changelog[]`): `network` of a fleet-wide entry, `incident` of every other kind, and
    an incident's `impact` and `durationMinutes` (null
    while ongoing). The shape is the website's `ChangeEntry`, which has no `na`
    (api/answers/changelog.md); the network answer lists them in its own `na`
    (`changelog[].incident`, ...) with `not_applicable`. A null in an entry's
    `values` (CHANGELOG-VALUES-1) has its reason in the entry's own
    `naReasons` (`{"values.plan": "not_published"}`, `catalog` for a
    `site.changelog` row's), and the network answer lists it with the same code.
  - an incident's `endedAt` while `ongoing` (`profile.incidents[]`, every
    changelog `incident`).
  - target `group` for a target that is not a source of a group target.
  - In the page answers (api/answers/README.md, "Missing data"), the network
    profile lists each plan's null `gb`, `ips`, `catalog.tier` and `audience` by
    the plan's index (`pricing.plans[3].ips`; by its name before PRICING-CODES-1):
    `not_applicable` for a plan without an IP count (sold by GB), a per-IP plan's
    `gb` and an untiered pool's `tier`, `catalog` for a plan sold by GB without a
    GB amount and for a plan without an audience; the trial numbers' nulls
    (`pricing.trialPrice`, ...) with `not_applicable` without a trial
    (`trialKind` `none`), for a credit's `trialGb` and for the `trialDays` of a
    trial in numbers (no time limit), else `catalog`; and a null
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
- Reasons for every N/A path are in `/v1/coverage` (`build/na_reasons.go`
  `reasons`): a reason code and the `source` that would fill it (a `site.*`
  table or column, `public.targets`, `snapshot` or null; COVERAGE-REASONS-1).
  The page answers added from Phase 3 also give each path a reason code
  beside their `na` (`naReason`, `naReasons`; api/answers/README.md, "Missing
  data").
- An empty Index Score says why (SCORE-REASONS-1): `score`, `grade`, `rank`
  and each `breakdown.<component>` have the code of the calculator's reason
  (`scores.balanced.reasons`, `components.<component>.reason`), the first of:
  `no_pulse_tests` (no finished pulse tests, so no uptime), `few_successes`
  (no pulse slot with enough successes for the speed, SPEED-PULSE-1),
  `no_load_test` (no load test for the speed, in snapshots before
  SPEED-PULSE-1), `required_target_missing` (a required target
  has no ready result), `no_price_evidence` (no verified price per GB, so no
  value), `few_requests` (too few requests), `low_time_coverage` (the
  requests cover too little of the window) and `expired` (the measurements
  are too old). `catalog` and `no_run_in_window` come first;
  `insufficient_data` is left for a reason none of these match.

## Removed on 2026-11-14 (event fields)

EVENTS-OLD-REMOVE-1 (#993). A `/v1/events` row has the live tape row's shape: `kind`
is the group, changed in place, `event` the code, `values` the numbers. The website
stopped reading these with its W6 switch; `TestNoRemovedProseField` keeps them out of
every `/v1/events` row.

| Field | Where it was | Read instead |
|---|---|---|
| `events[].category` (`"incident"`) | `/v1/events` | `events[].kind`, now the group |
| `events[].data` (the event's raw stored data) | `/v1/events` | `events[].values` |
| `events[].provider` (the framework provider id) | `/v1/events` | `events[].providerSlug`, `events[].providerName` |
| the query `category=` (now 400 `unknown_parameter`) | `/v1/events` | `kind=<group>` |
| an event code in the query `kind=` (now 400 `invalid_parameter`) | `/v1/events` | `event=<code>` |

## Removed on 2026-11-14 (profile text)

PROFILE-TEXT-REMOVE-1 (#991). The network page's provider facts hold no English
but the network's own names and what users wrote: the website writes them from
codes and numbers (its commit fd82dbd7, its switch W4).
`TestNoRemovedProseField` (`internal/server/prose_scan_test.go`) keeps them out of
every answer, list page and the dataset file.

**Removed**:

| Field | Where it was | Read instead |
|---|---|---|
| `hq` (`"Vilnius, Lithuania"`) | `/v1/networks/{slug}` `profile`; dataset `providers[]` (and its `pages`) | `hqCountry`, `hqCity` |
| `bestFor[]` (`"Web scraping"`) | the same | `useCases[]` (`use_case` codes) |
| `protocol.label` (`"Rotating residential"`), `protocol.summary` | `/v1/networks/{slug}` `profile` (and dataset `pages`) | `profile.measuredProduct`; the scope's `/v1/meta` `scopes[]` |
| `pricing.trial` (the text, `"3-day free trial (100 MB)"`) | `/v1/networks/{slug}` `profile`; dataset `providers[]` and `providers[].productMeasurements.<kind>` | `trialKind`, `hasTrial`, `trialPrice`, `trialCredit`, `trialGb`, `trialDays`, `trialNeedsCard` |
| `trust.leakChecks[].detail` | the same | `trust.leakChecks[].name`, `.passed` |
| `verificationLabel` (`"Invoice verified"`) | `/v1/networks/{slug}/reviews` `rows[]`; `/v1/networks/{slug}` `reviews[]`; dataset `reviews[]` (and its `pages`) | `verification` |
| `monthlySpend` (`"$100"`) | the same | `monthlySpendMin`, `monthlySpendMax` |
| `region` (`"eu"`) | `/v1/networks/{slug}/runs` `rows[]`; `/v1/networks/{slug}` `communityRuns[]`; dataset `communityRuns[]` (and its `pages`) | `country` |

**Changed in place**:

- A review's `useCase` is a `use_case` code (`web_scraping`; `/v1/codes`), read
  from `site.reviews.use_case` (any case, spaces and hyphens as underscores); null
  (and in the review's `na`) when the text is no code.
- A community run's `rejectionReason` is a `run_rejection_reason` code
  (`too_few_requests`, `collector_outdated`, `duplicate`, `inconsistent_results`,
  `other`), read from `site.community_runs.rejection_reason` the same way; a reason
  that is no code is `other`; null when the run has none (null by contract: only
  rejected runs have one).
- A plan's `name` (`pricing.plans[].name`) is the network's own plan name
  (`"Starter"`, `"Spark"`, `"Intro"`) or null, never our description (`"Billed monthly
  2 GB"`, `"Pay as you go"`; `"Intro (new customers, one-time)"` is now `"Intro"`); a plan's
  `catalog.pool` is the network's own pool name (`"Full Geo"`, `"Premium
  residential"`) or null, never a list of kinds (`"Residential rotating"`): the
  kinds are `catalog.poolKinds`. Both nulls are listed in `na` with the reason
  `not_named_by_provider` (the provider's page gives the plan no name, or names no
  pool; PLAN-NULL-REASON-1, #1060: no longer null by contract, so no field is empty
  without a reason).
  `api/scripts/pricing-<slug>.sql` write them; `scripts/pricing_handoff.py` keeps
  the rule for the next price check.

## Removed on 2026-11-14 (changelog text)

CHANGELOG-TEXT-REMOVE-1 (#990). The website writes every changelog line from the
entry's `event` code and `values` (its commit 7ea75831, its switch W3), with numbers
in the visitor's format and country names from the ISO codes; for a coded event it
reads none of these fields. `TestNoRemovedProseField` keeps them out of every
answer, list page and the dataset file's `pages`.

| Field | Where it was | Read instead |
|---|---|---|
| `title` (`{key, params}`: `pool_grew`, `{"v1": "30.0%"}`) | `/v1/changelog` `entries[]`; `/v1/networks/{slug}` `changelog[]`; the dataset file's `pages` copies | `event` (the same code) and `values` |
| `detail` (`{key, params}`: `not_seen_7d`, `{"v1": "Mayotte, Syria"}`) | the same | `values` (`lostCountryCodes`, `uniqueIps`, `fromRate`, `toRate`, ...) |
| `delta` (1 dp) | the same | `values.delta` or `values.deltaPct` (the same number) |
| `unit` (`pts`, `%`, `countries`) | the same | the unit of the `values` key in `/v1/codes` set `change_event` |

A catalog row (`site.changelog`, `event` `catalog`) loses its `delta` and `unit` too;
its headline and detail were already not served (#962). It keeps `kind`, `network`,
`severity`, `agoSeconds`, `source` and its kind's `values`, each null (`catalog`). The
dataset file's own `changelog[]` rows (`ChangeEvent`) keep `delta` and `unit`.
The code sets `change_title`, `change_detail` and `change_unit` are gone from
`/v1/codes` with the fields they coded.

## Removed on 2026-11-14 (time labels)

TIME-LABELS-REMOVE-1 (#988). Every point of every series has its time in minutes
(SPARK-TIMES-1, #964; a home network series by `timesIndex`, #985), and the website
draws its own axis ticks and range words from `days` and `hours` (its commits f50630d3,
31ccf584, 59cc4614 and 4df85b9a, its switch W1). The English point labels (format.ts
`bucketLabel`: "now", "5 min ago", "23h 45m ago", "2 days ago"), the axis words and the
range's display word are gone. `TestNoRemovedProseField` keeps `labels`, `axis` and
`short` out of every answer, list page and the dataset file.

| Field | Where it was | Read instead |
|---|---|---|
| `labels` (the answer's label lists) | `/v1/home` (and dataset `pages`) | `times` |
| `market.curve.labels`, `market.latencySpark.labels`, `market.successScoreSpark.labels` | `/v1/home` (and dataset `pages`) | the same series' `times` (`curve` itself went on 2026-11-14, #1072: `successScoreCurve.times`) |
| `networks[].curve.labels`, `.latencySpark.labels`, `.successScoreSpark.labels` (an index) | `/v1/home` (and dataset `pages`) | the same series' `timesIndex` (`curve` itself went on 2026-11-14, #1072: `successScoreCurve.timesIndex`) |
| `headlineSpark.labels`, `latencySpark.labels` | dataset `measuredMarket.views[]` | the same series' `times` |
| `range.axis` (`["24h ago", "18h", "12h", "6h", "now"]`) | `/v1/networks/{slug}` `stats.<r>.range`; dataset `measuredMarket.views[].range`, `.fleet.range`, `.fastest.stats.range`, `.mostReliable.stats.range` (and `pages`) | `range.id`, `range.days` |
| `range.short` (`"24h"`) | the same | `range.id` |
| `ranges[].axis`, `ranges[].short` | `/v1/meta` (and dataset `pages`) | `ranges[].id`, `.days`, `.hours` |

## Removed on 2026-11-14 (pricing codes)

PRICING-CODES-1 (#987). The website reads neither; `TestNoRemovedProseField` keeps
them out of every answer, list page and the dataset file.

| Field | Where it was | Read instead |
|---|---|---|
| `pricing.plans[].catalog.bandwidth` (`"1 TB"`) | `/v1/networks/{slug}` `profile`; dataset `providers[]` and `providers[].productMeasurements.<kind>` | the plan's `gb` |
| `pricing.plans[].catalog.period` (`"/month"`, `"one-time"`, `"/30 days"`) | the same | `catalog.billing` |

## Removed on 2026-11-13 (item 14)

HTTP-COST-RETIRE-1 (#950). The plain-HTTP cost per 1,000 successes (one cheap
target's wire bytes, for example Geonode 0.0002) is not a network's real cost:
the all-targets cost (`costPer1kAllTargets`, `insights.valueAll`,
`cost.allTargets`, `market.spreadAll`; VALUE-ALL-1, VALUE-ALL-2,
VALUE-ALL-PARTIAL-1) replaced it, and the website no longer reads the old
fields. `TestNoRetiredHTTPCostField` (`internal/server/http_cost_scan_test.go`)
fails if any of them comes back in an answer, a list page or the dataset file.

**Removed** (no longer in any answer or the dataset file):

| Field | Where it was | Read instead |
|---|---|---|
| `costPer1kSuccess` (the network's plain-HTTP cost) | `/v1/rankings` `networks[]`; `/v1/networks/{slug}` `stats.<r>` and `profile.pricing`; the dataset file's `providers[].pricing`, `productMeasurements.<kind>.pricing` and `measuredMarket.views[].fastest`/`mostReliable.stats` | `costPer1kAllTargets` (same places: rankings rows, `stats.<r>`, `profile.pricing`) |
| `insights.value` (`rows[]`, `best`, `worst`, `spreadX`, `unpriced`) | `/v1/home` | `insights.valueAll` |
| `market.spread` (`value`, `best`, `worst`) | `/v1/home` | `market.spreadAll` |
| `cost.cheapest`, `cost.dearest`, `cost.median`, `cost.spreadX`, `cost.priced` (the HTTP summary) | `/v1/rankings` | `cost.allTargets` (the same five fields) |
| `costBasis` (the text of the HTTP basis) | `/v1/networks/{slug}` `profile.pricing`; dataset `providers[].pricing` | `costBasisAllTargets`, which itself went in the prose removal ("Removed on 2026-11-13 (prose)"): read `costPer1kAllTargets`, `priceBasis`, `basisPlanGb` |
| `wireGbPer1k` (the HTTP cost's input) | `/v1/rankings` `networks[]`; `/v1/networks/{slug}` `stats.<r>`; dataset | `bandwidthMb` / `bandwidthGb` (the traffic itself) |
| `cheapestCostPer1kSuccess`, `dearestCostPer1kSuccess`, `priceSpreadX`, `residentialSpreadX` | dataset file `totals` and `measuredTotals` | `/v1/rankings` `cost.allTargets` |
| `bills` (`all[]`, `best`, `worst`, `spreadX`) | dataset file `measuredMarket.views[]` | `/v1/home` `insights.valueAll` |
| `valueRank` (the plain-HTTP cost's rank; VALUE-ALL-RANK-1, #951) | `/v1/rankings` `networks[]`; `/v1/home` `networks[]`; `/v1/networks/{slug}` `stats.<r>.ranks` and `profile.pricing` (the catalog `site.pricing.value_rank`); the dataset file's `providers[].pricing` | `valueRankAllTargets` (rankings and home rows, `stats.<r>.ranks.valueRankAllTargets`), the rank of the 8-target bill the value component scores |

**Updated in place** (same field, new basis):

| Field | Now |
|---|---|
| `stats.<r>.alternative` (`/v1/networks/{slug}`: `slug`, `name`, `successScore`, `latencyP50`, `cheaperBy`) | the first network of the kind's ranking whose all-targets bill (`costPer1kAllTargets`) is lower, with an internal success rate (not served) at most 2 points below; `cheaperBy = 1 − its all-targets bill ÷ this network's`. A lower-bound bill (`costIsLowerBound`, VALUE-ALL-RANK-1) is compared as it is when it is this network's: a network below it is cheaper for certain, and `cheaperBy` is then at least that much; another network's lower-bound bill is never the alternative, since its true bill may be higher. Null with its reason in `naReasons` (ALT-REASON-1): `catalog` without a list rate, `no_run_in_window` without a run, `incomplete_targets` when this network's all-targets bill is null, `no_cheaper_peer` when no network qualifies (it is the cheapest of its kind in the range, or every cheaper one is worse). |

**Kept**, with why (each figure of `answer_na.go` `costFigures` included):

| Field | Why |
|---|---|
| `costPer1kSuccess` of one target: `/v1/targets` `networks[].cells.<target>`, `/v1/networks/{slug}` `profile.targets[]`, and `costRank` | one target's cost, a real meaning |
| `valueRank` (rankings and home rows, `stats.<r>.ranks.valueRank`, `profile.pricing.valueRank`) | the rank the Index Score's value component uses; issue B (#951) moves both to the all-targets cost together |
| `/v1/home` `market.cheapest`, `market.dearest` (and the dataset's `measuredMarket.views[].cheapest`/`dearest`) | the lowest and highest list rate ($/GB), not a cost per success |
| `cost.allTargets.cheapest`/`dearest` `.costPer1kSuccess`, `.median`, `.spreadX`, `.priced` | the all-targets summary: its ends' `costPer1kSuccess` holds the all-targets bill |
| `insights.valueAll` `best`, `worst`, `spreadX`, `rows[]`; `market.spreadAll` | the all-targets replacements |
| `costPer1kAllTargets`, `partialCostPer1kAllTargets`, `valueRankAllTargets` | the all-targets cost (its plain-words basis `costBasisAllTargets` went in the prose removal, below) |
| `headlinePriceRank`, `listRate`, `avgPayloadKb` | list-rate rank, the rate costs use (the cost basis since COST-BASIS-200GB-1) and the catalog's payload size: not the HTTP cost |
| `value` (`market.cheapest.value` and the other market picks) | the pick's figure; only `insights.value` went |

The internal plain-HTTP cost stays inside the build as `valueRank`'s input until #951; it is
never served.

## Removed on 2026-11-13 (prose)

API-PROSE-REMOVAL-1 (#962). The English sentences the API once served (taglines,
claims, verdicts, notes, labels of ranges, scope text) are gone: the website
builds every line from codes and values since its commit 168c7957.
`TestNoRemovedProseField` (`internal/server/prose_scan_test.go`) fails if any of
them comes back in an answer, a list page or the dataset file;
`TestNoRemovedProseFieldLive` runs the same scan on a served URL
(`SITEAPI_SCAN_URL`).

**Removed** (no longer in any answer or the dataset file):

| Field | Where it was | Read instead |
|---|---|---|
| `tagline`, `claim`, `verdict`, `strengths`, `weaknesses` | `/v1/networks/{slug}` `profile`; `/v1/home` and `/v1/rankings` `networks[]` (`tagline`); dataset `providers[]` | the measured figures (`score`, `successScore`, `uniqueIps`, ...) and `claimed`, `claimedPoolSize`, `claimShare` |
| `schedule`, `protocol.schedule`, `protocol.cadence` | `profile` (`schedule`, `protocol`); `/v1/home` `networks[]` (`schedule`) | `/v1/meta` `scopes[].cadence` |
| `latencyNote` | `/v1/home` and `/v1/rankings` `networks[]`; `stats.<r>`; dataset `measuredMarket.views[]` | `latencyTarget`, `latencyUnderLoad` |
| `priceLabel` | rankings rows; `profile.products[]`; dataset | `pricePerGb`, `pricePerGbAtScale` |
| `trial` (the text) | `/v1/rankings` `networks[]` | `trialKind`, `hasTrial` (the profile's text went on 2026-11-14, #991) |
| `stickinessAdvertised` | rankings rows; `profile.trust`; dataset `providers[].trust` | nothing (`stickinessHoldRate` stays) |
| `costBasisAllTargets` | `profile.pricing`; dataset `providers[].pricing` | `costPer1kAllTargets`, `costIsLowerBound`, `priceBasis`, `basisPlanGb` |
| `products[].note`, `pricing.plans[].note`, `pricing.plans[].catalog.features`, `pricing.plans[].catalog.poolDescription` | `profile`; dataset `providers[]` | the plan's numbers |
| `score.standing.note` | `profile.score.standing` | `standing.rank`, `standing.peers` |
| `incidents[].scope`, `incidents[].summary` (also `changelog[].incident`) | `profile.incidents[]`; `/v1/changelog` `entries[].incident`; dataset `providers[].incidents[]` | `countryCodes`, `countries`, `severity`, `ongoing`, `durationMinutes`, `impact` |
| `title`, `detail`, `value` of a tape or event row | `/v1/home` `tape[]`; network `tape[]`; `/v1/events` `events[]`; dataset `tape[]` | `event`, `country`, `values` |
| `scoreDefinitions` | `/v1/home`, `/v1/rankings`, `/v1/meta` | nothing (website copy) |
| `profiles[].description` | `/v1/home`, `/v1/rankings`, `/v1/meta` | `profiles[].id` |
| `range.label`, `range.bucket`, `range.resolution`, `range.phrase` (and `ranges[]` of `/v1/meta`) | `stats.<r>.range`; `/v1/meta` `ranges[]`; dataset `measuredMarket.views[]` | `id`, `days` (`short` and `axis` went too: "Removed on 2026-11-14 (time labels)") |
| `basis` | `/v1/networks/{slug}/pricing-history` | nothing |
| a network item's `subtitle` | `/v1/search-index` | nothing: the endpoint is removed (SEARCH-NEUTRAL-1, #981) |
| `headline`, `detail` of a catalog row | dataset `changelog[]` | `title`, `detail` codes (below) |
| a scope's `label`, `summary`, `rationale` and its `cadence` and `geoCoverage` sentences (META-SCOPES-CODES-1, #966) | `/v1/meta` `scopes[]`; dataset `scopes[]` | `scopes[].id`, `cadence` (numbers), `geoCoverage` (a code), `countries` (below) |
| `labels` (`success`, `availability`, `latency`, `latencyUnderLoad`, `pool`: "23h 55m ago") (UNREAD-TEXT-1, #977) | `/v1/networks/{slug}/chart`, `/v1/networks/all/chart` | each point's `t` (minutes before `generatedAt`) |
| an outage band's `label` ("major · AD") (#977) | chart `bands[]`; dataset `measuredMarket.views[].fastest.stats.bands[]`, `.mostReliable.stats.bands[]` | `bands[].severity` (an `incident_severity` code), `bands[].country` (ISO code) |
| an AI source's `label` ("AI answers: ChatGPT") (#977) | `profile.targets[].sources[]`; dataset `providers[].targets[].sources[]`, `providers[].productMeasurements.<kind>.targets[].sources[]` | `sources[].target`, named by `/v1/targets` |
| a product's `name` ("Residential") (#977) | `profile.products[]`; dataset `providers[].products[]` | `products[].kind` |
| the all-networks `network.name` ("All networks"): now null, `not_applicable` (#977) | `/v1/networks/all` | `network.slug` (`all`) |

**Changed in place**:

- A changelog entry's `title` and `detail` became codes here (`title.key`,
  `detail.key` and their `params`); they were removed on 2026-11-14 with `delta`
  and `unit` ("Removed on 2026-11-14 (changelog text)"): an entry's `event` and
  `values` say the same (api/answers/changelog.md, "Event codes").

- `/v1/meta` `scopes[]` are codes and numbers (META-SCOPES-CODES-1, #966): `id`,
  `targets` (ids), `countries`, `cadence` `{dailyTestsPerNetworkCountryPerDay,
  browserTriesPerTargetPerDay, pulseIntervalMinutes}`, `geoCoverage` `{code,
  countries}` (`all_usable_countries`, `provider_inventory`, `restricted`) and the
  four booleans; the shape and an example are in `api/answers/meta.md`. Each null
  has its reason in the scope's `naReason`/`naReasons` (#994): `not_measured` for
  the countries of a scope no published network measures, `no_pulse_fleet` for the
  pulse interval of a snapshot without pulse batches, `catalog` for an unknown
  `geo_coverage` or an empty boolean.

**Kept, the website reads them**: only the names, ids and codes kept by rule: `name`,
`providerName`, a plan's own name, region, city and ASN names, the catalog's own `pool`
and `tier` (country names went with PLACE-CODES-1, below). Gone since:
`targets[].passCondition` and `profiles[].label` with #989, `category` became a code
("Removed on 2026-11-14 (target text)"); the plan catalog's `period` and `bandwidth` with
#987; the home's series `labels` and the range `axis` and `short` with #988 ("Removed on
2026-11-14 (time labels)"); `hq`, `profile.bestFor[]`, `profile.pricing.trial`,
`profile.protocol.label` and `.summary` and `trust.leakChecks[].detail` with #991
("Removed on 2026-11-14 (profile text)").

**The English guard** (ENGLISH-GUARD-1, #976): `TestNoEnglishInAnswers`
(`internal/server/english_scan_test.go`) reads every string of the same bodies,
the error answers and `/healthz`, and fails on a sentence, a written-out number,
a relative time or a display word outside the name fields. The English still
served is listed, with the task that removes it, in
`internal/server/testdata/english-pending/`; each task deletes its lines.
`TestNoEnglishInAnswersLive` runs it on a served URL (`SITEAPI_SCAN_URL`).

## Removed on 2026-11-13 (place codes)

PLACE-CODES-1 (#978). A country is its ISO 3166-1 code only: every language has
the names (browsers' `Intl.DisplayNames`), so the API sends no English country
name. Region, city and network-operator (ASN) names stay: they are names as the
location source writes them. None of these answers is read by the website.

**Removed** (each place keeps its `countryCode`, a country row its `key`):

| Field | Where it was | Read instead |
|---|---|---|
| `rows[].country` | `/v1/networks/{slug}/places` (every dimension), `/v1/networks/all/places`, `/v1/countries/{cc}/cities` and `/regions`, `/v1/places/{region\|city}/{key}` | `rows[].countryCode` |
| `ranges.<range>.countries[].country` | `/v1/networks/{slug}`, `/v1/networks/all` (and their dataset `pages`) | `countryCode` |
| `countries[].country` | `/v1/countries` (and dataset `pages`) | `countryCode` |
| `country`, `ranges.<range>[].country` | `/v1/countries/{cc}` (and dataset `pages`) | `countryCode` |
| `geo[].country` | dataset `providers[].geo[]`, `providers[].productMeasurements.<kind>.geo[]` | `geo[].countryCode` |
| the country-name position of every row | dataset `lists.<slug>.<kind>.<range>.<dimension>.rows[][]` | the list's `columns` (below) |

**Added**: `columns` on `/v1/networks/{slug}/coverage` and on every dataset-file
list: the names of the rows' positions (codes of the `place_column` set,
`api/CODES.md`), so a reader takes each value by its name. `country=XX` on
`/v1/networks/{slug}/coverage` and `/places` (below).

**Changed**: `q` on `/coverage` and `/places` matches codes and served names
(country code, region, city, ASN name, `AS<number>`), no longer English country
names; a page sends a typed country name as `country=`.

The Coverage row's country name (position 1, column `country`) stayed until
the website read the rows by `columns`: "Removed on 2026-11-14 (coverage
country)", below.

A place `key` (`US|NY|New York|New York`) is an opaque id: use it in URLs, never
parse it. `regionCode` joined to `countryCode` with a hyphen (`US` and `NY`:
`US-NY`) is the region's ISO 3166-2 code.

## Removed on 2026-11-14 (target text)

TARGET-TEXT-REMOVE-1 (#989). Targets and ranking profiles are ids and codes in
every answer: the website words them from its own translations keyed by `id` and
by the category code (its commit e858eca8, its switch W2). Their English stays in
the catalog (`site.targets`, `site.ranking_profiles`, `public.targets`) and in
`/v1/codes` (sets `target` and `ranking_profile`, reference text only).
`TestNoRemovedProseField` (`internal/server/prose_scan_test.go`) fails if one
comes back.

**Removed**:

| Field | Where it was | Read instead |
|---|---|---|
| `targets[].label` | `/v1/targets`, `/v1/meta`; dataset `targets[]` (and its `pages`) | `targets[].id` |
| `targets[].passCondition` | `/v1/targets`, `/v1/meta`; dataset `targets[]` (and its `pages`) | `targets[].id` |
| `insights.hardest.label`, `insights.hardest.passCondition` | `/v1/home` (and dataset `pages`) | `insights.hardest.id` |
| `profiles[].label` | `/v1/home`, `/v1/meta`, `/v1/rankings`; dataset `profiles[]` (and its `pages`) | `profiles[].id` |

**Changed in place**: `targets[].category` (`/v1/targets`, `/v1/meta`, dataset
`targets[]`) is a `target_category` code: `baseline`, `ecommerce`, `search`,
`social`, `anti_bot`, `ai_answers` (was `"Baseline"`, `"E-commerce"`, `"Search"`,
`"Social"`, `"Anti-bot"`, `"AI answers"`). `api/scripts/catalog-targets.sql` writes
the codes; the build maps an older row's English name to its code, so the answers
are codes before the catalog is refreshed. Any other value is null (`catalog`).

## Removed on 2026-11-14 (coverage country)

COVERAGE-COUNTRY-REMOVE-1 (#992). The last English country name: a Coverage row
is its country code only. The website reads the rows by `columns`, writes the
country's name from `countryCode` and sends a typed country name as `country=`
(its commit c550c8c1, its switch W5). The dataset file's `city_asn` rows never
had the name, so they do not change. The English guard
(`TestNoEnglishInAnswers`) fails if the name comes back.

**Removed**:

| Field | Where it was | Read instead |
|---|---|---|
| `rows[][1]`, column `country` (`columns[1]`) | `/v1/networks/{slug}/coverage` (every range, page, `q` and `country=`) | `countryCode` (`rows[][0]`); every later value moves one position left: read them by `columns` |
| code `country` | `/v1/codes` set `place_column` | none: no list has the column |

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
| `targets[]` | `site.targets` (seeded by every deploy, `atlas db-seed --only targets`, the same rows as `api/scripts/catalog-targets.sql`; `category` as a `target_category` code; a build is refused while a group, source or browser target of the published methodology lacks its row, category or difficulty, SEED-ON-DEPLOY-1), plus `public.targets` (engine; its label and pass_condition only for `/v1/codes`) for framework targets without a site row; ordered by `sort_order` (a framework target's `public.targets.sort_order`, else its catalog row's; ties keep catalog rows first, in their order) | `category`, `difficulty`: catalog (`site.targets`); `engine`: catalog-only targets |
| `scopes[]` | `site.scopes` (`api/scripts/catalog-scopes.sql`) | catalog |
| `weights` | `site.score_weights`, replaced by the snapshot `methodology.settings.weights` when published (the weights its scores used) | data |
| `profiles[]` | `site.ranking_profiles` (`api/scripts/catalog-profiles.sql`: the five website profiles; a client re-ranks with a profile from each row's `breakdown`) | catalog |
| `totals` | computed (below) | data |
| `providers[]` | below | |
| `tape[]` | `public.test_events`, one row per incident (an outage seen by two targets' pulses of a slot is one row, `api/answers/events.md` EVENTS-MERGE-1) (200 rows, newest first: the newest non-routine rows up to 100, then the newest of any kind, as `/v1/events` picks them, plus each tab's newest 10 events and each network product's newest 10 of every tab (NETWORK-TAPE-QUOTA-1) not among them, `store.Inputs.TapeHeads`; mapped by the port of `tape.ts tapeFromEvents`); measured rows first, then the editorial `site.tape_events` rows | never |
| `changelog[]` | `site.changelog` | never |
| `reviews[]` | `site.reviews` | never |
| `communityRuns[]` | `site.community_runs` | never |
| `pages` *(added)* | every page answer the site draws, by the request that serves it (see "Page answers in the dataset file") | never |
| `snapshot` *(added)* | `{available, snapshotId, schemaVersion, asOf, generatedAt, refreshSeconds, methodologyId, methodologyUrl, readiness, contentHash}` of the published calculation; `methodologyUrl` is `/v1/methodology/<methodologyId>.json`, the rules whose SHA-256 is `methodologyId` (`api/README.md`, "Methodology files"; METHODOLOGY-FILE-1, #1179) | all null while nothing is published; `methodologyUrl` also when the API holds no rules for the id (`/v1/coverage` reason `methodology_file_unknown`, source `public.methodologies`) |
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
rate, `pricePerGbAtScale`, else `pricePerGb`, the displayed price, not the cost basis; unpriced networks last) have a null provider and
a null cost (`bills` was removed on 2026-11-13, item 14).

| Field | Formula (fleet basis) |
|---|---|
| `providers` | count of **all** providers (as `presentationDataset`) |
| `requests24h`, `uniqueIps24h`, `bandwidthGb24h` | sums of `performance.requests24h`, `pool.uniqueIps24h`, `performance.bandwidthMb24h / 1000` (decimal GB, see "Bandwidth" below) |
| `countries` | distinct `geo[].countryCode` |
| `medianLatency` | median of `performance.latencyP50` (`medianSuccessRate` was removed on 2026-11-14, #900) |
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
| `kinds` | catalog, else the measured categories (headline first) | never | |
| `primaryKind` | catalog when that category is measured, else the category with the most 30d requests | never | |
| `hqCountry` *(added)* | `site.providers.hq_country`: the headquarters' ISO 3166-1 code (`LT`), PROVIDER-CODES-1 | catalog | |
| `hqCity` *(added)* | `site.providers.hq_city`: the headquarters' city, a name (`Vilnius`); null where the catalog names no city (a country alone, or a state) | catalog | |
| `founded` | `site.providers.founded` | catalog | ✱ |
| `useCases[]` *(added)* | `site.providers.use_cases`: codes of the `use_case` set (`/v1/codes`), in the network's own order, each once; `[]` with `useCases` in `na` (`catalog`) when the row has none | catalog | |
| `website` *(added)* | `site.providers.website` | catalog | |
| `products[]` | catalog rows, else one entry per measured category (`kind` = default product, `measured: true`, `url` null) plus unmeasured framework products; no `name` (UNREAD-TEXT-1, #977: `kind` is the code) | `url`: catalog (TS hard-coded Shifter's URL) | |
| `measuredProduct` | catalog if it belongs to the headline category, else the category default (`residential_rotating`, `isp_static`, `datacenter`, `mobile`) | never | |
| `productMeasurements.<category>` | one per measured snapshot category (the product with the most 30d requests when a provider has several cohorts) | as below | |
| `score.composite` / `rank` / `previousRank` | `w.scores.balanced.score` / `rank` / `previous_rank` | data (until a ready score) | ✱ |
| `score.grade` | `w.scores.balanced.grade` | data (TS used `"—"`, now `"N/A"`) | |
| `score.breakdown.*` | `w.scores.balanced.components.<key>.value`; all 8 keys always present. Each is a 0–100 ratio to the best network of the same proxy kind (100 × yours ÷ best, or best ÷ yours where lower is better), computed by the framework; the raw value, the leader's value and the peer count stay in the snapshot at `components.<key>.inputs` (`{value, best, peers}`; diversity per dimension; product-level reliability/speed per target). `value` is proportional to the 8-target bill `costPer1kAllTargets` instead (methodology `[value] basis = "rank_all_targets"`, VALUE-ALL-RANK-1; since VALUE-FAIL-1, the owner's choice of 2026-11-18, `50 + 50 × log(median ÷ bill) ÷ log 4` kept between 0 and 100, the median being the bills' of the N networks of its kind and range with one: the median bill scores 50, each halving of a bill adds 25 points, 4 times cheaper or dearer than the median is 100 or 0, equal bills score the same; until then 100 × (N − rank + 1) ÷ N); the network's `valueRankAllTargets` is still its place among those N; null without a price (not counted in N). A network with no success on a card target is still priced and ranked, by a lower bound ("The lower-bound bill" below). Its inputs are `{value (the cost basis $/GB), cost_per_1k_all_targets, partial_cost_per_1k_all_targets, targets_counted_all_targets, missing_targets_all_targets, cost_is_lower_bound, browser_window, carried_over_all_targets, stale, page_bytes_all_targets (each card target's standard page, VALUE-FAIL-1), rank, ranked (N), peers}`; the API serves the bill, its figures and the rank from them (a build from before VALUE-ALL-RANK-1, without `cost_is_lower_bound`, gets the API's own `billOver` bill and rank) | data | ✱ |
| `pool`, `performance`, `pricing`, `trust`, `targets`, `series`, `ipSamples`, `incidents`, `geo`, `asns`, `overlap` | the headline category's measurement | as below | |
| `reviews` (summary) | computed from `site.reviews` (`summariseReviews` of the website's `scripts/generate-demo-data.ts`, in the old `proxy-index-api` repository's history before the website was removed) | `mean`, `verifiedMean` when there are no (verified) reviews (the generator wrote 0) | ✱ |
| `framework` *(added)* | `{provider: public.providers.id, products: [public.products.name]}` | | |
| `na` *(added)* | | | |

### Measurement blocks (top level and each `productMeasurements` entry)

Catalog values come from the typed `site.pricing`, `site.pricing_plans` and
`site.trust_signals` rows (`site.measurements`, the removed fixtures' documents, is
not read).

**List prices and their evidence (#927).** `api/scripts/pricing-<slug>.sql` and
`configs/pricing/<slug>-residential.json` are written by `scripts/pricing_handoff.py` from
a pricing check (the check's file is read at run time, never committed). Each plan's
`catalog` carries `poolKinds` (the pool's kinds, `proxy_kind` codes: `["residential",
"mobile"]` for a residential and mobile pool), `unitRate` (the per-GB price paid without
typing a code), `originalUnitRate`, `originalPrice`, `billing` (`payg`, `30_day`, `monthly`,
`annual`), `audience` (served as the plan's `audience`), `discountSource` (`sale`,
`auto_coupon` with its `code`, `annual_billing`, `subscription`, `volume`, or null), `url`,
`source` and `checkedAt`; a code the buyer must type is the plan's `note` and is not applied.
`site.pricing_history` holds the true dates: one row per true check (`effective_from` = the
check's day, `source` = the page URL). It starts at the first true check, 2026-11-12
(#940): the rows made from the earlier catalog were deleted
(`api/scripts/pricing-history-reset-2026-11-12.sql`), so no false price change is shown.
Shifter also keeps its verified 2026-10-04 check, restated with the same rule (entry $2.00,
Lite Spark; at scale $0.75), equal to 2026-11-12. The generator keeps the committed true
checks and adds the new one. The value component reads the newest price evidence
with `period_start` ≤ the build's as-of (`window_scoring.pricing`). The two test days'
builds have as-of 2026-11-10, so the 2026-11-12 evidence keeps #847's `period_start`
2026-11-08 and `valid_until` 2027-01-01; with equal dates the newer `evidence_id` wins, and
its `source` says "observed 2026-11-12". In production, evidence is dated by its observation.
`pricing.pricePerGb` and `pricePerGbAtScale` come from the calculation snapshot when it
publishes prices (below), so they follow new evidence in the next calculation build; plans
and `pricing-history` follow the catalog load.

| Field | Measured source | N/A for measured | NaN in TS |
|---|---|---|---|
| `pool.uniqueIps24h` / `uniqueIps30d` | `day.pool.unique_ips` / `w.pool.unique_ips` | data | |
| `pool.claimedPoolSize` | `site.trust_signals.claimed_pool_size` | catalog | (null) |
| `pool.churnRate`, `recurringShare` | `w.pool.churn_rate`, `recurring_share` | data | ✱ |
| `pool.countries`, `regions`, `cities`, `asns` | `w.pool.*` | data | |
| `pool.requestsPerIp` | none | always | ✱ |
| `performance.successScoreDelta24h`, `successScoreDelta30d` *(added)* | the 24h and 30d windows' `success_overall.score − success_overall.previous_score` ("Success score deltas" below) | data: no ranked score in this build or the previous one, or a snapshot from before `previous_score` | |
| `performance.latencyDelta24h` | the 24h window's `latency_day_delta` at 1 dp, the same value as `stats.24h.latencyDeltaPct` (the day rule, its common-country comparison, else the last value kept: LATENCY-ALWAYS-1, "latencyDeltaPct" below); `latencyDelta24hAsOf` *(added)* the last UTC day it compares | data: the network never had a value (the day rule's reason, `needs_two_days` or `day_not_covered`) | ✱ |
| `performance.ttfbP50` | none | always | ✱ |
| `performance.latencyP50`/`P90`/`P99` | the latency target's (`generic_http`, product rule: the only target with a latency) headline latency: its `latency_ms`, the latency of normal requests (the pulses, concurrency 25; HEADLINE-PULSE-1, was the latency under load when the window had a daily test); older snapshots their only `latency_ms`. The latency under load is display only (`underLoadLatencyP50/P90/P99`). Null without a `generic_http` target; the window's own latency (a mix of targets) is never used | data | ✱ |
| `performance.latencyMean` *(added)* | the `mean` key (milliseconds, framework #161) of the same headline latency object as `latencyP50`, by the same rule (the mean of the same equal-weight mix of slot histograms the percentiles read). Like the percentiles it comes from the 1 ms ceiling histograms, so it can be up to about 1 ms above the mean of the raw latencies. Per-row lists and `series` latency points carry no mean | data: `latencyP50`'s cases, or a snapshot written before the mean | |
| `performance.timeoutRate` | `w.metrics.timeout_rate` | data | ✱ |
| `performance.uptimePct` | `w.metrics.uptime × 100`: uptime over the measured time (`[uptime] basis = "measured_time"`): 1 − the countries' downtime, each clipped to its measured span (its first tested run's start to its last one's end, inside the window) and, with the methodology's `country_verdicts`, to the time the country counted for the network (COUNTRY-MIN-IPS-1), ÷ the sum of those spans; null when the window has no measured time | data | ✱ |
| `performance.downtimeMinutes24h` | `day.uptime.downtime_s / day.uptime.countries / 60`, 1 dp, when `basis` is `"outage time"` (the capacity-weighted downtime of the whole window, not clipped to the measured time; unchanged by the measured-time uptime) | data (always for a v1 snapshot) | ✱ |
| `performance.bandwidthMb24h` | `(day.metrics.bytes_up + bytes_down) ÷ 10⁶`: the 24h window's wire traffic in MB (see "Bandwidth" below) | data: no bytes in the 24h window | ✱ |
| `performance.requests24h` | `day.metrics.completed` | data | |
| `pricing.model` | `site.pricing.model`; `"per_gb"` when the snapshot publishes prices (`costBasis` was removed on 2026-11-13, item 14; the `trial` text on 2026-11-14, #991: `site.pricing.trial` is read only for `trialKind`) | catalog (TS used `"per_gb"`) | |
| `pricing.trialPrice`, `trialCredit`, `trialGb`, `trialDays`, `trialNeedsCard` *(added, PRICING-CODES-1)* | `site.pricing.trial_price` (US dollars the buyer pays: 0 for a free trial or a credit), `trial_credit` (US dollars of credit: 0 for none), `trial_gb`, `trial_days` (null: no time limit), `trial_needs_card` (boolean; null: not known); written from the trial text by `api/scripts/pricing-<slug>.sql` (`"$5 trial includes 10GB for 3 days"` → 5, 0, 10, 3, null) | catalog (all null without a trial or without the numbers; the page answers' reasons above) | |
| `pricing.pricePerGb` (entry rate, smallest plan), `pricePerGbAtScale` (lowest list rate) | the snapshot's `products[].pricing.entry_per_gb` / `lowest_per_gb` (every-request snapshots); they win over `site.pricing.price_per_gb` / `price_per_gb_at_scale` | catalog / snapshot | ✱ |
| `pricing.minCommit` | `site.pricing.*` | catalog | ✱ |
| `pricing.avgPayloadKb` | `site.pricing.avg_payload_kb`; else the 30d window's wire bytes per request: (bytes_up + bytes_down) / completed / 1024, 2 dp, the same wire bytes `costPer1kSuccess` reads | catalog / data (`not_measured` without wire bytes) | ✱ |
| `pricing.priceBasis`, `pricing.basisPlanGb` *(added, COST-BASIS-200GB-1)* | the snapshot's `products[].pricing.price_basis` (`first_200gb_plus` or `largest_below_200gb`) and `basis_plan_gb`: which plan the cost basis is and its GB ("The cost basis" below) | `catalog` (no basis evidence) | ✱ |
| `pricing.headlinePriceRank` | `site.pricing.headline_price_rank`; else the competition rank of the network's list rate (`pricePerGbAtScale`, else `pricePerGb`) among the list rates of the networks of its kind, cheapest first, ties sharing the better rank (as `valueRankAllTargets` ranks the 8-target bill) | catalog (no list rate) | ✱ |
| `pricing.costPer1kSuccess` (internal since 2026-11-13, item 14: not served) | `site.pricing.cost_per_1k_success`; else `wireCostPer1k(listRate, w.metrics)`: (bytes_up + bytes_down) / completed × 1000 / 10⁹ (GB = 1,000,000,000 bytes, the GB networks sell) × the cost basis (`listRate`, "The cost basis" below) / `request_success_rate` (`success_rate` in older snapshots), unrounded (VALUE-ALL-2: no longer 4 dp nor 1024³); else `avg_payload_kb × 1000 / 1024² GB × price_per_gb / successRate` (4 dp) | catalog / data (needs a list rate and wire bytes or a payload) | ✱ |
| `pricing.costPer1kAllTargets` | the 30d 8-target bill (VALUE-ALL-2): the plain mean over the card targets of `targetCost` (`price × standard page × completed ÷ 10⁹ ÷ successes × 1000` on `w.targets[id].metrics`, unrounded: every try, failed ones too, costs the target's standard page, VALUE-FAIL-1; "The 8-target bill" below), at the cost basis (`listRate`); the same figure as `/v1/home` `insights.valueAll.rows[].costPer1k`. Since VALUE-ALL-RANK-1 the figure the 30d value component publishes (`components.value.inputs.cost_per_1k_all_targets`), a lower bound when a card target has no success; the API's own only for a build from before it, then set only when every card target has a cost | `catalog` (no list rate) / `incomplete_targets` (a card target without a cost) | ✱ |
| `pricing.costIsLowerBound` | `true` when `costPer1kAllTargets` is a lower bound (`components.value.inputs.cost_is_lower_bound`, "The lower-bound bill"); `false` otherwise and in a build from before VALUE-ALL-RANK-1. Never null | — | ✱ |
| `pricing.plans[].catalog` | `site.pricing_plans.catalog` JSONB, without its `features`, `poolDescription`, `bandwidth`, `period` and `audience` | Optional published offer: pool and its kinds (`poolKinds`), lowest advertised rate, tier (null for untiered pools), advertised unit rate, original price/rate, billing, order URL, source and check date. Omitted for legacy plans. Measured prices remain scoped to the tested pool. A null `pool` (the provider names none) is in `na` with `not_named_by_provider` (#1060). | |
| `pricing.plans[].audience` *(added, PRICING-CODES-1)* | the plan catalog's `audience`: `all`, or `new_customers` for a plan only new customers may buy (`"Intro (new customers, one-time)"`); a `plan_audience` code | catalog | |
| `pricing.plans[]` | `site.pricing_plans` | catalog (`na: pricing.plans` while empty) | |
| `pricing.plans[].name` | `site.pricing_plans.name`: the network's own plan name, or null where the network names none (#991: never our description) | `not_named_by_provider` (#1060; the website draws the plan from its size and billing) | |
| `trust.datacenterAsnShare` | `w.pool.datacenter_asn_share` (every-request snapshots: share of exit IPs whose IP database usage type is a hosting type, unknowns excluded), else `site.trust_signals` | data | ✱ |
| `trust.claimGap` | older snapshots: `w.pool.datacenter_asn_share` (as presentation.ts); every-request snapshots (`pool.shares_basis` set) publish no claim gap: null unless `site.trust_signals.claim_gap` | data / catalog | ✱ |
| `trust.residentialAuthenticity` | every-request snapshots: `w.pool.geo_accuracy`, the share of exit IPs whose IP database country is the requested one (the authenticity raw value); else `site.trust_signals` | data / catalog | ✱ |
| `trust.blacklistHitRate`, `stickinessHoldRate` | `site.trust_signals.*` | `not_measured` without a catalog value (not measured by the framework) | ✱ |
| `trust.kycVerified`, `consentDisclosed` | `site.trust_signals.*` | catalog (TS used `false`, `false`) | |
| `trust.leakChecks[]` | `site.trust_signals.leak_checks`: each check's `name` and `passed` (its text `detail` is not read, #991) | `not_measured` without a catalog value (not run by the framework) | |
| `targets[<id>]` | one row per dataset target. `requests` comes from `w.targets[id].metrics` (`requests` is 0 when unmeasured); `latencyP50`/`P90`: `generic_http`'s headline latency (normal requests, the pulses'); every other HTTP target's (`generic_https`) display-only light-load `latency_ms`, from its own successful pulses and runs outside a batch that did not run during a census, with the latency target's slot rule (null when no time slot has `[slots] min_latency_successes`, 30, of them; HTTPS-LATENCY-1). Display only: no headline, load factor, Speed or score reads it. Null on browser targets and in older snapshots (before every-request scoring, or written before HTTPS-LATENCY-1) | data (every unmeasured target is listed) | ✱ |
| `targets[<id>].delta24h` | none | always | ✱ |
| `targets[<id>].outcomes`, `pageLoadP50`/`P90`, `sources`, `engine`, `engines` | browser targets only, from `w.targets[id].browser` (below) | data | ✱ |
| `browserOverall` | the network's overall browser score per engine, from `w.browser_overall` ("Browser scores" below); not written when the snapshot has none | data | ✱ |
| `successScore` *(added)* | the network's 1-10 success score over the 8 card targets, from `w.success_overall` ("Success scores" below); not written when the snapshot has none | data | |
| `targets[<id>].scores` | the target's rank-based scores: a browser target's (below), and `scores.success`, `challenge` and `block` on `generic_http` and `generic_https` (`w.targets[id].scores`, "Success scores" below) | null without scores in the snapshot | |
| `series.latency24h` | `day.series` p50/p90/p99 when the window's latency is the HTTP target's alone (`windowLatencyIsHTTP`: `time_slots.latency_targets` is `["generic_http"]`, or no other target has requests), else null | data | ✱ |
| `series.latency30d`, `uniqueIps24h`, `uniqueIps30d` | `series[].latency_ms.p50` (same rule) / `unique_ips` | data | ✱ |
| `series.pricePerGb90d` | none | always | |
| `ipSamples[]` | never served: exit addresses are not published (the database holds real exit addresses and the API is public), and no synthetic sample stands in | always (`not_published`) | |
| `incidents[]` | the product's `outages` (snapshot v2, newest 50 in 30d): `id` `outage:<product id>:<country>:<started_at>`, `startedAgoSeconds = as_of − started_at`, `durationMinutes = duration_s / 60` (an open outage runs to `as_of`; a pulse network outage is never open while we do not measure: a 5-minute window without any pulse data of the network ends it at that window's start, `outages.end_reason` `not_measured`, and it opens only in the countries the network was pulsed in during its last pulse interval, so the time not measured is in no incident, downtime or uptime figure, OUTAGE-NOT-MEASURED-1, #1277), `severity` `"major"`, `impact = 1 / w.uptime.countries`; *(added, CHANGELOG-VALUES-1)* the same facts as a changelog `incident`, made by one function (`incidentFacts`): `countries` 1, `countryCodes` `[<country>]`, `kind` `down` (a pulse outage) or `census_stall` (`source` `census`: a stalled census), `detectedBy` the outage's `source` (`pulse` or `census`), `ongoing` (no `ended_at`), `startedAt` and `endedAt` (ISO, ms, `Z`; `endedAt` null while ongoing, else `ended_at` or `started_at + duration_s`) | `incidents` always for a v1 snapshot; `incidents[].troughSuccessRate` always; `impact` without tested countries | ✱ |
| `geo[]` | `w.pool.countries_breakdown` with `share = ips / unique_ips`, plus `w.breakdowns.country` p50 (v2) | `latencyP50` data (always for v1) | ✱ |
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

The framework's every-request scoring (atlas branch `score-from-all-requests`) keeps
`schema_version` 2 and adds fields; the API reads both shapes. A window's
`metrics` with `time_slots` is every-request: `success_rate` is time-weighted
(mean of slot rates), `request_success_rate` is successes ÷ requests,
`latency_ms` is the latency of normal requests (pulses, concurrency 25),
`latency_under_load_ms` the latency under load (the daily tests, census and
load runs; null with `under_load.reason`). `pool.shares_basis` marks per-IP
shares (`geo_accuracy`, `datacenter_asn_share`); products carry `pricing`
(`lowest_per_gb`, `entry_per_gb`, `effective_from`, `valid_until`, `sources`).
The window `latency` block (load reference, per-country normal and under-load
latency), `tests` (requests per test kind), `time_slots`,
`coverage_slots.down` and the light-load breakdown percentiles are read for
the window statistics and are not served raw. `latency.countries[]`
has one entry per requested country and target (`target`); the page shows the
`generic_http` ones. Time slots are lcm(pulse interval, 5 min): 30 minutes in
production, 10 in staging. `latency.regions[]` lists per runner region its
load wave's `reference` and the daily `chosen[]` picks (older snapshots also
`factors`, the load factor that scaled pulses before LATENCY-LABELS-1). `latency.countries[]` carries `region` (older
payloads: `target`, filtered to `generic_http`). `census_overlap` (pulses and
load waves clean vs during a census of the same network), `metrics.latency_reason`
and `time_slots.skipped_during_census` pass through for the network page.
`time_slots.cut_at_start` (WINDOW-EDGE-SLOT-1, 0 or 1) counts the slot cut by the
window start, which does not vote on `success_rate` (its requests still count in
`completed` and `request_success_rate`); the API does not read it.

The generated English copy (tagline, verdict, strengths, weaknesses, the products'
`priceLabel` and the standing note) is no longer served: removed on 2026-11-13
(prose). A score alone in its kind still has `standing {rank: 1, peers: 1}`
(the framework itself leaves it unranked). Test input: `api/fixtures/measured/snapshot-every-request.json`
(`api/scripts/every-request-fixture.mjs`).

### Window statistics of a measured provider

`windowStats` (`/v1/rankings` rows, `/v1/networks/{slug}` `stats` and its
`/chart` answers) reads the snapshot window: `latencyP50/P90/P99` the `generic_http` target's headline latency, its
`latency_ms` (normal requests, the pulses', concurrency 25; HEADLINE-PULSE-1; a network's over
its country basket since LATENCY-BASKET-1, `latencyBasketCovered` below),
`lightLatencyP50/P90/P99` the same `latency_ms`, always equal to the headline (kept so
that no client breaks; null where the headline is),
`latencyTarget` `"generic_http"`,
the `latency` series only where the window's latency is that target's alone, `costPer1kSuccess = wireCostPer1k(listRate, metrics)` of that window
(internal, not served since 2026-11-13),
`uptimePct = metrics.uptime × 100` (over the measured time, as `performance.uptimePct`
above: a 7d window with 2 measured days and 13.2 min down is 1 − 13.2 ÷ 2,880 = 99.54%, not
1 − 13.2 ÷ 10,080) and `downtimeMinutes` as `performance.downtimeMinutes24h` above, for that
window.
With outages published (v2), `bands` and `incidentCount` come from `incidents`
(each outage's overlap with the window, as a
fraction of it, with its `severity`, an `incident_severity` code, and its `country`, an
ISO code; UNREAD-TEXT-1, #977), so the charts shade real outages.
For a v1 snapshot `incidentCount` and `downtimeMinutes` are null and `bands` is
empty.

Per-range figures of the same window (WINDOW-FIGURES-1; every one null for a
network without a snapshot product, and wherever the window has no value):

| Field | Source |
|---|---|
| `countries`, `regions`, `cities`, `asns` | `pool.countries`, `regions`, `cities`, `asns` |
| `datacenterAsnShare` | `pool.datacenter_asn_share` |
| `datacenterIps` | the share's numerator (CHART-SCORE-DCIPS-1): the window's exit IPs whose usage type is a hosting one, `pool.datacenter_asn_share × classification_coverage × unique_ips` (host ÷ known × known ÷ unique IPs × unique IPs, `calculated/poolfig.py`) rounded to the whole number it is; in a window with hour-formula figures served × `breakdown_scale` as a breakdown row's IPs (K8, "IP numbers from the hour formula"). `datacenterIps` ÷ the IPs with a usage type (`classification_coverage × unique_ips`, scaled alike) is `datacenterAsnShare`. Null with the share's reason when the share is null, and null (`insufficient_data`) in an older snapshot without `classification_coverage` |
| `exclusiveShare`, `sharedShare` | `overlap.exclusive_share`, `shared_share`, by the rule of `overlap.exclusiveShare` above: an observed pool (`pool.unique_ips` > 0) with `compared_products: 0` is 1 and 0 (older snapshots publish null for it); no observed pool stays null |
| `exclusiveIps`, `exclusiveScore` | the range's `/v1/overlap?kind=<the window's kind>` row of the network: its exclusive IPs (`poolIps × exclusiveShare`, rounded, `poolIps` the range's `uniqueIps`; #1283) and their 1-10 score ("Exclusive score" below); null where that row has none, and without a snapshot product |
| `observedDays` | days from the range's first history point (the merged history behind `successScoreDelta`) to `period.end`, rounded up; 0 below one day; null when the history covers the range or there is none (the old site's "the last N days" / "the last <1 day") |
| `latencyBasketCovered`, `latencyBasketSize` *(added, LATENCY-BASKET-1)* | the headline latency's country basket (the owner's decision of 2026-11-19, #1385; the methodology's `[latency]`): `latencyBasketSize` its countries (38), `latencyBasketCovered` how many of them have a latency of the network's own in the range (`generic_http`'s `latency_ms.latency_basket` `size` and `covered`), for "N of 38 countries" beside the latency. `latencyP50/P90/P99`, `latencyMean`, `lightLatencyP50/P90/P99`, the `latency` series, every `latencySpark` and Speed (`breakdown.speed`, so the score) are the plain mean over the basket countries of the network's latency in each, a country it lacks counting as the slowest network of its kind there ("Network headline latency over a country basket" on the methodology page). Also on `/v1/rankings` and `/v1/home` `networks[]`. Null without a snapshot product and in a snapshot calculated without the basket (`insufficient_data`) |
| `latencyUnderLoad` | whether `latencyP50` is under load: false whenever there is a latency, since the headline is the latency of normal requests (`latency_ms`, the pulses'; HEADLINE-PULSE-1: it was true with a daily test, census or load run of `generic_http` in the window); null without latency |
| `underLoadLatencyP50`, `underLoadLatencyP90`, `underLoadLatencyP99` *(added, HEADLINE-PULSE-1)* | `generic_http`'s latency under load, `latency_under_load_ms` (the daily country tests, concurrency 2000, 500 for two networks, census and load runs): display only, no score, rank, delta or spark reads it. Null in a window without one (every-request snapshots only) with `insufficient_data`. `/v1/home` `networks[]` carries `underLoadLatencyP50` too |
| `costPer1kAllTargets` | the range's 8-target bill (VALUE-ALL-2): the plain mean over the card targets of `targetCost` on the window's `targets[id].metrics` at the row's list rate, unrounded. Since VALUE-ALL-RANK-1 the calculation publishes it with the range's value component and the API serves that figure (`/v1/home` `insights.valueAll` the same); a lower bound when a card target has no success (`costIsLowerBound`). Null with `catalog` (no list rate) or `incomplete_targets` (a card target without a cost for another reason: no run or no wire bytes; in a build from before VALUE-ALL-RANK-1, also no success) |
| `costIsLowerBound` | `true` when `costPer1kAllTargets` is a lower bound ("The lower-bound bill"), else `false` (also in a build from before VALUE-ALL-RANK-1); null without a snapshot product |
| `carriedOverTargetsAllTargets` | COVERAGE-ALERT-1 (#1216): with the methodology's `[value] browser_window = "7d"` the 24h bill costs its browser card targets over the last 7 days and its HTTP targets over its own 24 hours (the 7d and 30d ranges are unchanged); this object lists each browser card target that had no run in the range but one in those 7 days, with the hours since that run (`{"amazon_pdp": 72.0}`): its figure is carried over, and the website shows a small "carried over" note. `{}` when none (every target ran in the range, or a build without the key); null without a snapshot product. With no run in 7 days the target is missing (`missingTargetsAllTargets`) as before |
| `successRateAllTargets` *(added)* | SUCCESS-ALL-1 (#1338, the owner's choice of 2026-11-18): the success rate behind `costPer1kAllTargets`, a fraction from 0 to 1: the plain mean over the 8 card targets of each one's successes ÷ completed tries, the same tries the bill prices (with `[value] browser_window` the 24h range's browser targets are the last 7 days', as the bill's), so each target weighs as in the bill. Not total successes ÷ total tries, which the HTTP targets' millions of tries would decide (Proxy-Cheap, 17 Nov production: 0.47, while its plain-HTTP rate is 0.83). The website's true-cost table shows it in its Success column. It is the one success rate served since SUCCESS-RATE-REMOVE-1 (#900). The headline `successScore` reads the same tries per target but scores each by its distance from the best network before averaging them (SUCCESS-ALL-8-1), so the two orders can differ. Null with `incomplete_targets` when a card target has no try in the range, and without a snapshot product. Also `/v1/home` `insights.valueAll.rows[].successRateAllTargets`, the same figure |
| `partialCostPer1kAllTargets` | VALUE-ALL-PARTIAL-1: when some but not all card targets have their own cost, the same mean over the ones that do (`/v1/home` `insights.valueAll.rows[].partialCostPer1k`, the same published figure); set beside a lower-bound bill too; null by contract when every target has a cost, unpriced, or no target has a cost |
| `targetsCountedAllTargets` | how many card targets have a cost (all of them when `costPer1kAllTargets` is set); null without a snapshot product |
| `missingTargetsAllTargets` | the card target ids without a cost for a measured reason (no run, no success, no wire bytes; each target's own reason is `/v1/targets` or `insights.valueAll.rows[].naReasons`), in dataset order; `[]` when the bill is complete or only the list rate is missing; null without a snapshot product |
| `successScoreDelta` | `success_overall.score − success_overall.previous_score`: the range's overall success score minus the previous published build's ("Success score deltas" below) |
| `challengeScore` | `challenge_overall` (CHALLENGE-OVERALL-1, #900): the range's overall 1-10 challenge score, `successScore`'s shape `{score, rank, rankedOf, status, targets, weights}` on the targets' challenge scores (an HTTP target's `scores.challenge`, a browser target's main engine's): their difficulty-weighted mean, ranked among the networks of the kind (10 the fewest challenges), shifted as the success scores so the best reads 10 ("The best network reads 10"). A null `score`, `rank` and `rankedOf` have `status` (`no_ranked_targets`) as their reason; null in a snapshot from before it (`insufficient_data`). Display only: no score, rank or cost reads it |

**Bandwidth** (BANDWIDTH-PER-NETWORK-1). A network's bandwidth in a window is
the data its tests moved through it, both directions: `metrics.bytes_up +
bytes_down`, the bytes the market's `bandwidthGb` sums.
Units are decimal, as the cost per 1,000 successes: `bandwidthGb` = bytes ÷ 10⁹
and `bandwidthMb` = bytes ÷ 10⁶. `stats.<r>.bandwidthMb` is that window's,
`profile.performance.bandwidthMb24h` the 24h window's (with pulses but no daily
test, the pulses' bytes), and the `/v1/home` and `/v1/rankings` rows'
`bandwidthGb` the range window's, so the rows of a range sum to its
`market.bandwidthGb`. Null only when the window recorded no bytes:
`no_run_in_window` without a run in it, `not_measured` with runs but no bytes. A
network without a snapshot product keeps its catalog figure. Display only: no
score, rank or cost reads it.

**Traffic per target** (TARGET-TRAFFIC-1). `/v1/networks/{slug}`
`ranges.<r>.targets[]` is one `{target, requests, bandwidthMb}` per target, in
`profile.targets[]`' order (the 11 targets), for 24h, 7d and 30d;
`profile.targets[].bandwidthMb` is the same figure in the profile's range
(30d). `requests` is the target's completed requests (a browser target's
tries) and `bandwidthMb` its measured wire bytes, `windows[r].targets[id].metrics.bytes_up +
bytes_down` ÷ 10⁶: decimal MB, as `stats.<r>.bandwidthMb` (GB = MB ÷ 1000,
the GB of the cost per GB), never estimated from `wireGbPer1k`. Both add up
over the network's product groups, as `ranges.<r>.requests`. Totals:

- `generic_http` + `generic_https` = `stats.<r>.bandwidthMb`: the network's
  window covers its HTTP targets' runs only (daily tests, pulses, load).
- Browser targets never enter the network totals (methodology
  `[target_groups]` comment, `calculated/browser.py`), so the 8 card targets
  add up to the network total plus the browser traffic; no field serves that
  sum.
- A group target (`social_web`, `ai_answers`) is the sum of its sources
  (`instagram_web` and `x_web`; `ai_overview`, `ai_chatgpt`, `ai_gemini`): a
  total counts the group or its sources, never both. The snapshot keeps a group's figures of one engine (the one with the
  most tries), so the API adds the sources up instead; with one engine the two
  are the same.

A target with no request in the range has `requests: 0` and `bandwidthMb: null`
with `no_run_in_window`; one with requests but no recorded bytes
`not_measured`, as is every target of a network without a snapshot product.

### The 8-target bill (`insights.valueAll`, VALUE-ALL-1)

`/v1/home`'s `insights.valueAll` (`api/answers/home.md`) bills each measured residential network
per 1,000 successful requests over the 8 card targets, equal weights. One target's cost in the
range's residential window is `listRate × page × completed ÷ 10^9 ÷ successes × 1000`, not
rounded, `listRate` being the cost basis ("The cost basis" below) and `page` the target's
**standard page** (VALUE-FAIL-1, the owner's choice of 2026-11-18): the median wire bytes per try
(`(bytes_up + bytes_down) ÷ completed`) of the kind's networks that measured the target, which the
value component publishes (`inputs.page_bytes_all_targets`). Every try, failed ones too, costs
one standard page: a failure is a retry, and a network whose failures move few bytes is no
cheaper for them. A build from before VALUE-FAIL-1 has no pages: each target then costs its own
`(bytes_up + bytes_down)` instead of `page × completed`. **GB here is 10^9 bytes**, the GB networks sell, as in `bandwidthGb` (see "Bandwidth") and,
since VALUE-ALL-2, the per-target `costPer1kSuccess`: for a window whose `request_success_rate` is
successes ÷ completed it is `wireCostPer1k`'s formula, unrounded. The bytes
of `page` are every finished try's (failed ones too, as `aggregate.py` sums them). The row's bill is the plain
mean of the 8 costs, null (`incomplete_targets`) when one is missing. Since VALUE-ALL-RANK-1 the
calculation publishes each range's bill, partial bill, counted and missing targets and
`cost_is_lower_bound` with the value component (`calculated/`, the same rule, checked against this
one by `TestValueAllCrossCheck` on a shared fixture to 1e-9), and every answer serves those; a
target without a success then counts at the kind's highest cost on it ("The lower-bound bill").
The API's own computation (`billOver`) stays for a build from before it and for each target's
detail (`byTarget`), which shows each target's own cost only, at the published standard pages
(`mbPerTry` stays the network's own): a target counted at the kind's highest cost in a lower-bound
bill keeps a null cost there (`no_successes`).

### The cost basis (COST-BASIS-200GB-1, item 15)

Every cost is priced at one rate per network, its **cost basis**: the price of its first plan of
200 GB or more. The owner's rule (2026-11-13):

1. The network's standard rotating residential product (#927). Automatic discounts count; a code
   the buyer must type does not.
2. The smallest published plan with at least 200 GB, billed monthly (30-day plans included) or
   pay as you go; when both exist at that size, the plan listed first on the pricing page
   (PRICE-BASIS-FIXES-1, 2026-11-13; it was the cheaper of the two). Annual commitments do not
   count.
3. Without a published plan of 200 GB or more: the network's largest published plan with a stated
   GB.
4. A custom or "contact us" tier (a quote, such as IPRoyal's "Custom plans (from 10 TB)" or an
   "as low as ..., get a quote" rate without a plan size) counts nowhere: not the basis, nor
   `pricePerGbAtScale`, the served plans or the pricing history
   (PRICE-BASIS-FIXES-1).

The calculation reads it from the `metric_evidence` metrics `price_per_gb_basis` and
`price_basis_plan_gb` (written by `scripts/pricing_handoff.py` into
`configs/pricing/<slug>-residential.json`) under the methodology's `[value] price =
"first_200gb_plan"`, and publishes `products[].pricing.basis_per_gb`, `basis_plan_gb` and
`price_basis` (`first_200gb_plus` when the plan has 200 GB or more, else `largest_below_200gb`).
A network without that evidence is unpriced: its value component is null with the reason
`no_basis_price`, never priced at another rate.

The API serves the basis as `listRate` ("the rate costs use": rankings rows, `profile.pricing`,
`ranks.listRate`), with `priceBasis` and `basisPlanGb` beside it on the rankings rows, the network
page's and the dataset's `pricing`, and `insights.valueAll.rows[]`. It prices every cost:
`costPer1kAllTargets` and the partial and lower-bound bills, `valueRankAllTargets` and the Index
Score's value component, `insights.valueAll`, `cost.allTargets`, `market.spreadAll`, the cheaper
alternative and each target's `costPer1kSuccess` (`/v1/targets` cells, `profile.targets[]`). There
is no separate basis price field: `listRate` changed meaning in place. The displayed prices do not
change: `pricePerGb` (the smallest paid plan), `pricePerGbAtScale` (the lowest rate),
`headlinePriceRank` (ranked on `pricePerGbAtScale`, else `pricePerGb`), the market's `cheapest`
and `dearest` and the pricing history.

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
`linkedin_public`, `social_web`, `cloudflare_challenge`, `ai_answers`). A group
target (`social_web`, "Social media web app": Instagram and X public profiles;
`ai_answers`) is measured over the union of its sources' tries and is never
scheduled itself; its `sources` is its breakdown. Each source (`instagram_web`,
`x_web`; `ai_overview`, `ai_chatgpt`, `ai_gemini`) is also listed as a target,
with its `group` set, so it is not a card of its own.

Every figure of a browser target in a range counts **whole runs**: a browser run
(one batch of 100 tries) counts in the range (24h, 7d, 30d, each of `days[]`) that
holds its finish time, all its tries, outcomes and page loads, or none, never cut
by the range's start (BROWSER-WHOLE-RUN-1, #1266; the methodology's
`browser_window_rule = "whole_run"`). A run finishing after the range's end is not
in it; one that started before the 30 days of raw rows is in no range.

On a browser target's `targets[<id>]` entry (network answers, providers and
`productMeasurements`), besides `requests` as for every target:

| Field | Unit | Source (`w` = the 30d window) | N/A |
|---|---|---|---|
| `outcomes.challenge`, `block`, `loginWall`, `timeout`, `proxyError`, `noContent`, `noAnswer`, `otherError` | fraction of the completed tries (they sum to 1) | `w.targets[id].browser.outcome_rates` (`login_wall`, `proxy_error`, `no_content`, `no_answer`, `other_error`) | the target is not measured for the product (or no try completed) |
| `pageLoadP50`, `pageLoadP90` | milliseconds, successful tries only | `w.targets[id].browser.page_load_ms.p50` / `.p90` | not measured, or no successful try |
| `sources[]` | group targets only (not written on any other target) | one entry per `w.targets[id].browser.sources` id, in that order | |
| `sources[].target` | | the source's id (`/v1/targets` names it; no `label`, UNREAD-TEXT-1, #977) | |
| `sources[].requests` | tries | `w.targets[source].metrics.completed` (0 when unmeasured) | |
| `sources[].outcomes.*` | fraction | `w.targets[source].browser.outcome_rates` | as `outcomes` |
| `engine` | `"camoufox"` or `"chrome"`: the browser engine whose tries make `outcomes`, `pageLoadP50`/`P90` and `requests` (the one with the most completed tries in the window; ties go to Camoufox) | `w.targets[id].browser.engine` | not measured, or a snapshot from before the engine split |
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
| `score` | 1 to 10: `1 + 9 × (M − rank) / (M − 1)`, then shifted so the best network reads 10 ("The best network reads 10" below); null unless `status` is `ranked` |
| `rank` | the network's rank among the M ranked networks (1 is best); ties share the average of their ranks, so it may be fractional; null unless `ranked` |
| `rankedOf` | M, the number of networks ranked for that target, window and engine; null unless `ranked` |
| `status` | `ranked`; `not_enough_data` (this network has fewer than the methodology's `browser_min_requests`, 100, completed tries of the engine in the window: SCORE-BROWSER-MIN-1, #1134, and BROWSER-MIN-100-1, the owner's decision of 2026-11-16, #1232, was 500; such a network is no peer of the ranking, not one of the 3 below, and the target counts in none of its overalls); `too_few_networks` (fewer than 3 networks are ranked). There is no `all_similar` any more (SUCCESS-TIE-1): a tie is ranked, a shared first place (below) |

`success` ranks the `ok` share (higher is better), `challenge` the challenge
share and `block` the hard-block share (lower is better). The `ok` share is
internal (removed from `outcomes` on 2026-11-14, #900); the snapshot's `rate` is
not repeated: it is that share, `challenge` and `block`.

A tie is a shared first place, on every metric (CHALLENGE-BLOCK-TIE-1 for
`challenge` and `block`, SUCCESS-TIE-1 for `success`): when the best and worst
ranked rates are too close to tell apart (under the larger of 3 points and 1.96
standard errors of their difference), every ranked network gets `score` 10,
`rank` 1, `rankedOf` M and `status` `ranked`. The status `all_similar` is never
served. A near-tie inside that noise counts too: on a target where one network
has 1.1% blocks and another 0.2% (0.9 points apart, under 3), both get 10.
`not_enough_data` and `too_few_networks` stay as they are. Challenge and block
scores feed no overall. A tied success target now counts in the overalls below
as a 10 at its difficulty weight (it was left out before), which pulls each
network's overall toward 10: (Σ w·s + w_tie × 10) ÷ (Σ w + w_tie). When every
network has the same ranked targets their order does not change; when some lack
a target (`not_enough_data`) ranks can move. The Index Score, its ranks and
Speed read no target score, so they do not move.

### The best network reads 10

SUCCESS-SHIFT-1 (#948): in each group of success scores the API adds the same
offset to every network's score so that the best reads 10:
`shown = score + (10 − the group's best score)`. The gaps between networks stay
as measured, and the order, `rank`, `rankedOf`, `status` and nulls do not
change. Nothing goes above 10 (the offset is 10 − the best) or below 1 (every
score is at least 1 and the offset at least 0); a group whose best is already
10 has offset 0, and an all-null group is unchanged. The calculation's payload
keeps the measured scores; the API shifts them when it builds its answers, so
the methodology id does not change.

| Group (one offset each) | Fields |
|---|---|
| overall success: kind × range | `successScore` on `/v1/home`, `/v1/rankings`, `/v1/targets` and overlap rows; `profile.successScore`; `stats.<r>` and `ranges.<r>` `.successScore`; the dataset file. The calculation already shifts it so the best reads 10 (SUCCESS-ALL-8-1: its days and chart points too), so the API's offset is 0; its `targets` map, distances from each target's best, is not shifted |
| overall challenge: kind × range | `challengeScore` on `/v1/rankings` rows and `stats.<r>` (and its `targets` map, each with its target's challenge offset; CHALLENGE-OVERALL-1) |
| per day: kind × day | `days[].successScore` |
| browser overall: kind × range × engine | `browserOverall.<engine>.success.score` |
| per target: kind × range × target × metric × engine | `scores.success`, `.challenge` and `.block` in `/v1/targets` cells, `profile.targets[].scores` and `.engines.<engine>.scores`, `sources[].scores`, `insights.hardestRates[].scores`, the dataset file |
| per place: kind × range × dimension × place | the place rows' `successScore` |
| per community run: the run and the kind's networks | `/v1/runs` `successScore` |

Why: a difficulty-weighted overall is a mean of target scores and reaches 10
only for a network that is best everywhere (30d build 21: Oxylabs 8.05, offset
+1.95), and tied leaders share the average of their ranks (16 networks tied
on `google_search` block read 6.03, offset +3.97). Each overall has its own
offset, so a shown challenge overall is not the weighted mean of the shown
target scores unless one network leads everywhere.

Not shifted (absolute, 1 + 9 × a rate): `market.successScore` and its spark,
the target fleet's `fleet.<engine>.successScore`, `/v1/runs`
`agreement.successScore` and `/v1/events/stats` `pass.successScore`. The Index
Score, its grades, ranks and Speed read no 1-to-10 success score, so they do
not move. `successScoreDelta` and `scores.success.delta` stay the network's
measured change ("Success score deltas" below).

`browserOverall.<engine>.success` (network answers, providers and
`productMeasurements`, from `w.browser_overall.<engine>`) is the network's
overall: `score` is the mean of its ranked targets' success scores weighted by
each target's difficulty (1 to 5), `rank` and `rankedOf` its place among the
networks with an overall, `status` the snapshot's, `targets` the ids it
averaged (`social_web` counts once for Instagram and X, `ai_answers` once for the AI sources; `not_enough_data` and
`too_few_networks` targets are left out, a tied one counts as a 10) and `weights`
each one's difficulty. There is no overall for `challenge` or `block`.

The snapshot also has scores for each HTTP target,
`w.targets.generic_http|generic_https.scores.success`, `.challenge` and
`.block` (the same shape and rules, ranked on `metrics.success_rate`,
`challenge_rate` and `block_rate`, internal rates; challenge and block since CHALLENGE-BLOCK-TIE-1), and one
success score per network over the card targets, `w.success_overall`; the API serves both
("Success scores" below).
`score`, `rank` and `rankedOf` are null when the network has no overall.

On the two test days (the #815 load on `proxy_reviews`) the browser outcome shares are
generated test data (BROWSER-SPREAD-1, model `per-network-v1`, scripts/scale/SIMULATE.md
"Each network's own browser outcomes"): each network has its own mix per target and engine,
its success share up to 18 points from the measured base, in the real networks' order where
it is known. AI Overview and ChatGPT had no answer measured in that load, so their Camoufox
success is centred on the owner's real measured rate instead (BROWSER-AI-SPREAD-1, a private
input, SIMULATE.md "A real base for the AI sources"), and they and `ai_answers` are ranked.
In production the shares are the measured ones.

### Target difficulty

TARGET-DIFFICULTY-FIXED-1 (#901): every target's difficulty, 1 (easy) to 5
(hard), is a **fixed value the owner set** (2026-11-12). It is never derived
from results (success rates, ranks or outcomes) and changes only when the owner
says so. One source of truth, `configs/methodology.toml`:

| target | difficulty | table |
|---|---|---|
| `generic_http`, `generic_https` | 1 | `[http_difficulty]` |
| `amazon_pdp` | 2 | `[browser_difficulty]` |
| `social_web` and its sources `instagram_web`, `x_web` | 3 | `[browser_difficulty]` |
| `google_search`, `cloudflare_challenge` | 4 | `[browser_difficulty]` |
| `linkedin_public` | 5 | `[browser_difficulty]` |
| `ai_answers` and its sources `ai_overview`, `ai_chatgpt`, `ai_gemini` | 5 | `[browser_difficulty]` |

The site catalog (`targets[].difficulty`: every deploy seeds it from these tables,
`atlas db-seed --only targets`; `api/scripts/catalog-targets.sql` holds the same rows)
holds the same values; `tests/test_target_difficulty.py` fails when the
methodology, the catalog or the served head differ from them. Both tables are
part of the methodology id. Where they weigh:

- `scoreWeight` (`/v1/meta`, `/v1/targets` `targets[]`) is the target's weight
  in the **browser** overall (`browserOverall`): the eight browser targets'
  `[browser_difficulty]`. `generic_http` and `generic_https` stay null: they
  are not part of the browser overall at all.
- In the **all-targets** challenge overall (`challengeScore`), each browser
  target weighs its `[browser_difficulty]` and each HTTP target its
  `[http_difficulty]`, 1.
- The **success score** (`successScore`, SUCCESS-ALL-8-1) reads no difficulty:
  the 8 card targets weigh the same (`successScore.weights`, 1 each).
- `home_insights.go` fills a target's missing catalog difficulty from these
  weights: config, not results.

### Success scores

SUCCESS-SCORE-CALC-1 (the calculator, #880) and SUCCESS-SCORE-API-1 (#881): the
website shows a 1-to-10 success score where it showed a success rate. The
success rate was removed from every answer on 2026-11-14 (#900) and stays
internal; the Index Score, its breakdown, Speed and the ranks do not change.

- **A network target's score**, `scores.success = {score, rank, rankedOf,
  status}` on `generic_http` and `generic_https` (the browser cells' shape,
  with `challenge` and `block` beside it since CHALLENGE-BLOCK-TIE-1, ranked on
  the internal challenge and block rates), from `w.targets[id].scores.success`:
  the networks of a kind ranked on the target's success rate (internal) by the rule and statuses of "Browser scores" above. `/v1/targets`
  gives these targets `scoreStatus.http`, the status their networks share.
- **The network's overall score**, `successScore = {score, rank, rankedOf,
  status, targets, weights}` on every network row (`/v1/rankings`
  and `/v1/overlap` `networks[]`, `/v1/networks` rows, and
  `/v1/networks/{slug}` `network`, `profile`, `ranges.<r>` and `stats.<r>`;
  `/v1/targets` `networks[]` without `targets` and `weights`, which its cells
  and `targets[].scoreWeight` already give: its body has no room for them;
  `/v1/home` `networks[]` without them too, HOME-SIZE-1 (#905), whose chart
  series are also rounded: `answers/home.md`, "Size"),
  from `w.success_overall`, the network's success on the **8 card targets,
  each scored by its distance from the best network, equal weights**
  (the owner's decision of 2026-11-19, SUCCESS-ALL-8-1; methodology
  `[success_score]` basis `card_targets`). Per range and card target (plain
  web `generic_http`, secure web `generic_https`, `amazon_pdp`, `social_web`,
  `google_search`, `cloudflare_challenge`, `linkedin_public`, `ai_answers`;
  `/v1/targets` `targets[].card`), the same tries the all-targets bill
  `costPer1kAllTargets` prices and `successRateAllTargets` reads: an HTTP
  target's every request, a browser target's runs on its main engine, a group
  (`social_web`, `ai_answers`) over its sources' runs, never a source alone;
  with `[value] browser_window` the 24h range's browser targets are the last
  7 days', as the bill's. The target's rate is successes ÷ completed tries
  and its score `max(1, 10 − (best − yours))`, the rates in percentage
  points, unrounded, `best` the highest rate among the networks of the kind
  with tries on it in the range: each target's best reads 10 and each point
  behind it costs 1. The network's score is the plain mean of its target
  scores, each target 12.5% (`weights` 1 each; not their difficulty, the
  owner's correction of 2026-11-19), plus one offset for
  the kind and range so the best network reads 10 ("The best network reads
  10"; the calculation applies it, so its days and chart points agree).
  `targets` is `{target: score}` of the targets it counted (not shifted) and
  `weights` `{target: 1}` of the same targets. A target with tries
  and no success counts, at 0%; a target with **no try** in its window is left
  out, the weights renormalised over the others, and `status` is
  `partial_targets` (the missing targets are the card targets not in
  `targets`). `rank` and `rankedOf` place the score among the networks of the
  kind with one (ties share the average place); `status` is `ranked`,
  `partial_targets` (ranked too), or `no_ranked_targets` (null `score`,
  `rank`, `rankedOf`) without a try on any card target in the range. Display
  only: the Index Score's reliability reads each target's time-slot success
  rate, never this. Six of the eight targets are browser targets (75% of the
  weight) measured with a few hundred tries a network a week, so the score
  moves more from day to day than a plain-web score. The all-networks answer
  has none (null). Until 2026-11-19 (SUCCESS-DAILY-DISTANCE-1, #1229) it was
  the daily country tests' `generic_http` alone.
- **A place row's score**, `successScore = {score, rank, rankedOf, status}` on
  the rows of `/v1/places/{region|city}/{key}`: computed by the API,
  as `latencyRank` is, ranking the networks of the same kind at the place on
  the row's success rate (internal) with the same formula, ties, statuses and minimums,
  `not_enough_data` below 100 requests at the place. A daily test's place row
  has no requests of its own (`not_measured_per_place`), so it is
  `not_enough_data`. A test checks the API's rule against the calculator's on
  the same cases (`api/fixtures/scores/success-score-cases.json`). A network's
  own place lists (`/v1/networks/{slug}/places`) carry the same score since
  #898 ("Success scores instead of success rates" below); `/coverage` ranks
  places, not networks, and has none. A
  country's rows (`/v1/countries/{cc}`) have none either: those answers are in
  the dataset file, one per exit country, and a score on each row would grow
  it by about 1 MB at 18 networks.
- **The market's score**, `/v1/home` `market.successScore = {score, status,
  basis}`, is absolute: the networks' scores are relative to the best network
  of the kind, so their median says nothing about the market. `score`
  is `1 + 9 ×` the median network's success rate (internal: `market.medianSuccess` was
  removed on 2026-11-14, #900) to 1 decimal: 91.9% gives 9.3. `basis` is
  `"median_success_rate"`; `status` is `ranked`, or null (with `score`) when
  that median is null, for the same reason.
- **Tooltips**: `scoreDefinitions = {rank, market, status, value}` (removed on 2026-11-13, prose; website copy now). They were plain sentences for a rank-based score (`rank`:
  "the best network in each group reads 10; the others keep their measured
  distance", #948), the market score, each status and the Index Score's value component
  (`breakdown.value`: "1 to 100 by place among the priced networks; the
  cheapest per 1,000 successful requests is 100.").

**Nulls.** A null `score`, `rank` or `rankedOf` of a `successScore` has its
`status` as its reason code in the entity's `na`/`naReason`/`naReasons`. A null
`successScore` or `scores` (a build from before the scores) has the row's usual
code: `insufficient_data` with a run in the window, else `no_run_in_window` or
`not_measured`. In `/v1/targets` the cell score's own nulls are explained by
its status, as for browser cells. A place row (`/v1/places/{region|city}/{key}`,
an answer without `na`) carries the reason inline only: its `successScore`
always has its `status`.

### Exclusive score

EXCLUSIVE-SCORE-1 (#1242, the owner's request of 2026-11-16) and
EXCLUSIVE-ESTIMATE-1 (#1283, the owner's decision of 2026-11-16). `exclusiveIps`
is a network's exit IPs that no other measured network of its kind saw in the
range, on the same pool every other answer shows: `poolIps × exclusiveShare`,
rounded, where `poolIps` is the range's `uniqueIps` (`stats.<r>.uniqueIps`, the
hour formula of K8, `poolBasis` `unique_ips`). The share is the measured one:
the overlap is counted per network, not per country, so the network's measured
share scales the network's `uniqueIps`. Shifter 30d at the 2026-11-16 19:10
build: 29.8% of 6,908,022 unique IPs is 2,056,332 only here (the measured
count was 832,416 of 2,796,410 observed IPs).

The measured figures stay: `observedPoolIps` is the pool the shares were
counted on (the overlap rows' `ips_ours`, the window's distinct IPs of the
network's runs, the set `pool.unique_ips` counts; the window's
`pool.unique_ips` for a network with no pair) and `observedExclusiveIps`
`observedPoolIps × exclusiveShare`, rounded (the `exclusiveIps` served before
#1283). A window without the hour formula's figure (census-pulse groups, no
released hour) has `poolIps = observedPoolIps` and the old `poolBasis`
(`overlap_ips_ours`, or `window_unique_ips` with no pair), as its `uniqueIps`
is its measured pool too. `exclusiveIps + sharedShare × poolIps = poolIps`
(within rounding), and each pair's `sharedIps` is on the network's own pool:
`shareOfOurs × poolIps`, rounded, so the two rows of one pair can differ
(each network's `uniqueIps` has its own hour formula) while `shareOfOurs`,
`shareOfTheirs`, `label`, `likelyUpstream` and every share stay the measured
ones. The market's counts (`sumOfPools`, `sharedIps`, `distinctIps`) are on
the same pools: `sumOfPools` is Σ `poolIps`, `observedSumOfPools` Σ
`observedPoolIps`, and `sharedIps` and `distinctIps` are the measured counts ×
`sumOfPools ÷ observedSumOfPools`, so `duplicatedShare` is the measured one.

`exclusiveScore = {score, rank, rankedOf, status}` (`successScore`'s shape
without `targets` and `weights`) puts it on the 1-10 scale against the leader
of the kind in the range, as the Index Score's ratio-to-leader components:
`score = max(1, 10 × exclusiveIps ÷ the kind's highest exclusiveIps)`,
unrounded. The leader reads 10 and a network with no exclusive IP 1 (every
network of the kind reads 1 when none has one). `rank` places it by
`exclusiveIps` among the networks of the kind with a value, a tie sharing the
average place (as `successScore`); `rankedOf` is how many have a value, and
`status` is `ranked`. A network without `exclusiveIps` has a null
`exclusiveScore` with `exclusiveIps`' reason (`insufficient_data`, or
`no_run_in_window` without a run); so has every network of a kind where none
has a value. A network alone in its kind is its own leader: 10, rank 1 of 1.
Served on `/v1/overlap` `networks[]`, every per-range figure (`/v1/rankings`
rows, `/v1/networks/{slug}` `stats.<r>`), `/v1/networks` rows and
`/v1/networks/{slug}` `network` (the headline group's 30d; the all-networks
answer's are null, `not_applicable`), `/v1/home` `insights.exclusivity[]`
(which it orders) and the dataset file's copies. Display only: no Index Score
component, rank or cost reads it.

### Success score deltas

SUCCESS-SCORE-DELTA-1 (#899): the website shows how a success score moved
since the last build, in score points, in place of the success rate's change in
percentage points (`successDeltaPts`, `successRateDelta24h`/`30d`, removed on
2026-11-14, #900). Like `previousRank`, a delta compares two published
builds, not two periods: the calculator keeps, beside each success score, the
previous published build's score for the same network, range and kind (and
target) as `previous_score`, and the API serves this build's score minus it,
unrounded (the website shows 1 decimal).

A delta is the change of the measured (unshifted) score: the previous score
moves with this build's offset ("The best network reads 10" above), so the
leader's own move does not change another network's delta. The difference of
two builds' shown scores can differ from it by the change of the offset.

| Field | Where | Source |
|---|---|---|
| `successScoreDelta` | `/v1/home` and `/v1/rankings` `networks[]`, `/v1/networks/{slug}` `stats.<range>` | the range's `w.success_overall.score − previous_score` |
| `profile.performance.successScoreDelta24h`, `successScoreDelta30d` | `/v1/networks/{slug}` `profile.performance` (and the providers' `performance` in the dataset file) | the same, of the 24h and 30d windows |
| `scores.success.delta` | `/v1/targets` `networks[].cells.<target>.scores.success`, `/v1/networks/{slug}` `profile.targets[].scores.success` and its `engines.<engine>.scores.success` | the target's `scores.success.score − previous_score` (an HTTP target's `w.targets[id].scores`, a browser target's main engine copy `w.targets[id].browser.scores`; the main engine's own `engines.<engine>.scores` shares it, another engine's is null: the calculator keeps no previous score for it) |

A delta is null when this build or the previous one has no ranked score for the
network, range (and target): the first build after a round with no earlier
build, a score that is not `ranked` now or was not then, or a snapshot written
before `previous_score` (the key is absent, read as null). The page answers list
it with the reason `no_score_to_compare` (`no_run_in_window` without a run, as
every figure); `/v1/coverage` gives the same code with the source `snapshot`. The place rows' and the
market's scores, which the API computes, have no previous build and no delta;
nor do `/v1/home` `insights.hardestRates[].scores.success` (its shape is
unchanged).

SCORE-DELTA-VERIFY-1 (#965): a delta is never a default. It is not the change
against the previous window of the same length: a 7d delta compares this
build's 7-day score with the previous build's 7-day score, whose windows
overlap by all but one day. A missing previous score is null as above, never 0;
a delta of `0` (JSON writes 0.0 as `0`) is a real equal score. Equal scores are
common, since each target's score comes from its rank among the networks: a
network whose rank moved on no target keeps exactly the same overall score. One
rank place on one target moves an overall by 9 ÷ (ranked networks − 1) × the
target's weight ÷ the weights' sum: with 18 networks and weights summing to 25,
0.02 points on a weight-1 target, 0.11 on a weight-5 one. A website threshold of
±0.1 is a move of more than 0.1 points since the previous calculation.

`latencyP50`/`P90` stay null on browser targets: latency is the HTTP
targets' only (the Generic HTTP target's headline, other HTTP targets' display
only), and browser cards show `pageLoadP50`/`P90` instead. A browser
target never counts as another target in the window-latency rule
(`windowLatencyIsHTTP`).

### Success scores instead of success rates

SUCCESS-SCORE-ALL-1 (#898), SUCCESS-RATE-REMOVE-1 (#900): success, CAPTCHA and
block rate percentages are no longer served in any answer, list page or the
dataset file; they were removed on 2026-11-14 (SUCCESS-RATE-REMOVE-1, #900). Only
the 1-to-10 scores stay. The rates stay internal: the scores, the cost per
success and `attemptsPerSuccess` still compute from them (`attemptsPerSuccess`
from the request success rate since #1117). The Index
Score, ranks, Speed and every other figure are unchanged. This table maps each
removed % field to the score that replaces it.

| removed field (removed 2026-11-14) | field with its score | status |
|---|---|---|
| `/v1/home` `market.medianSuccess` | `market.successScore` {score, status, basis} | live |
| `market.successSpark` (`values`, hourly rates) | `market.successScoreSpark` = {values, times, statuses}: 1 + 9 × each point's rate (absolute, as `market.successScore`), 1 decimal; `times` (SPARK-TIMES-1) is `market.successSpark`'s | live |
| `market.mostReliable.value` (its success rate; `{slug, name}` stay) | `market.mostReliable` = {slug, name, score, status}: now the network with the highest `successScore` in the range, a tie (also across kinds) to the higher success rate; `value` (its rate) was removed on 2026-11-14. Without any score: the highest rate, with a null `score`. `fastest`, `cheapest`, `dearest` are unchanged. | live |
| the market success delta (the website's, from the spark) | `market.successScoreDelta` = 9 × the change of the market success history (closing fifth minus opening fifth, as a network's `successDeltaPts`), 2 decimals; null below two observed points | live |
| home/rankings `networks[].successRate` | `networks[].successScore` | live |
| `networks[].requestSuccessRate` | `networks[].successScore` (the same score; the request rate is not shown) | live |
| `/v1/home` `networks[].successSpark` | `networks[].successScoreSpark` = {values, timesIndex, statuses}: per hour or day, the network's Success x/10 computed as its `successScore` (SUCCESS-POINTS-1, #1301; was its rank-based score among the home's networks of its kind with a point at that time); `timesIndex` is `successSpark`'s (the same index into the answer's `times`) | live |
| `/v1/home` `market.curve`, `networks[].curve` (the success-rate histories, every point for 7d and 30d, 110 market and 84 network points for 24h; removed with SUCCESS-CURVE-1, #1072) | `market.successScoreCurve` = {values, times}, `networks[].successScoreCurve` = {values, timesIndex}: one point per UTC hour (24h) or day (7d, 30d) since CURVE-HOURLY-DAILY-1 (#1301, "Score curves"; were the same points on the same times and grid), each 1 + 9 × the point's rate (absolute, as `market.successScore`), 2 decimals, `null` at a gap; no `statuses` | live |
| `/v1/rankings` `networks[].trend` (success rates) | `networks[].successScoreSpark` = {values, statuses}: one score per point of the removed `trend` (its time is `trendT`, which stays), ranked among the rows of the kind at that time. Since SUCCESS-POINTS-1 (#1301) each point is the calculator's point score, the window's Success x/10 rule over the card target tries of the trailing window ending at that hour or day (SUCCESS-ALL-8-1). | live |
| `/v1/networks/{slug}/chart` `success[].v` (every point, 288 for 24h) | `success[].score`, `success[].status` on each point: the network's rank-based score among the networks of its kind with a point at that time, as `/v1/rankings` `successScoreSpark` (every point since #1301), 2 decimals; null `score` with its `status` where the point has no data. The all-networks chart's fleet points have neither key (no per-network points to rank). Since SUCCESS-POINTS-1 (#1301) each point is the calculator's point score, the window's Success x/10 rule over the card target tries of the trailing window ending at that hour or day (SUCCESS-ALL-8-1). | live |
| `networks[].successDeltaPts`, `stats.<r>.successDeltaPts`, `profile.performance.successRateDelta24h` / `successRateDelta30d` | `successScoreDelta` (same places): score now minus the previous build's score for the same range | live |
| `/v1/networks/{slug}` `network.successRate`, `ranges.<r>.successRate`, `stats.<r>.successRate`, `profile.performance.successRate` | `.successScore` (same places), `profile.successScore` | live |
| `days[].successRate` (daily series) | `days[].successScore` {score, rank, rankedOf, status, basis, targets}: per day, the network's success score from that day's card target tries, the window `successScore`'s rule on that day (`basis` `"card_targets"`, "Days' scores" below) | live |
| place lists' `successRate` (`/v1/networks/{slug}/places?dimension=country\|region\|city\|asn`, `ranges.<r>.countries[]`) | `successScore` {score, rank, rankedOf, status} per row: the network's rank among the networks of its kind at that place (as `/v1/places/{dim}/{key}` rows) | live |
| `/v1/places/{dim}/{key}` rows' `successRate` | `successScore` | live |
| `/v1/networks/{slug}` `stats.<r>.alternative.successRate` | `alternative.successScore` {score, rank, rankedOf, status}: the alternative network's own score in the range (its ranking row's) | live |
| community runs' `successRate` (`/v1/networks/{slug}` `communityRuns[]`, `/v1/runs` `rows[]`, the dataset's `communityRuns[]`) | `successScore` {score, rank, rankedOf, status}: the run's rate ranked, as a network row, among the measured networks that sell its kind, on that kind's 30-day rate and requests (also a network's second kind; its own network left out); the run's requests are its 100-request minimum | live |
| `/v1/runs` `agreement.successRate` (median of the accepted runs) | `agreement.successScore` {score, status, basis}: 1 + 9 × the median (absolute, as the market), `basis` `"median_success_rate"` | live |
| `/v1/targets` `cells.<t>.successRate`, `profile.targets[].successRate`, `insights.hardestRates[].successRate` | `scores.success` | live |
| `/v1/targets` `targets[].fleet.<engine>.meanSuccess` (`http`, `chrome`, `camoufox`) | `fleet.<engine>.successScore` {score, status, basis}: 1 + 9 × meanSuccess (absolute, as the market), `basis` `"mean_success_rate"` | live |
| `profile.targets[].sources[].successRate` (AI sources) | `sources[].scores` {success, challenge, block}: the source target's own browser scores (`scores.success` is its success score) | live |
| `outcomes.ok` share (`profile.targets[].outcomes`, `cells[].outcomes`, `engines.<e>.outcomes`, `sources[].outcomes`) | `scores.success`. The other shares (challenge, block, timeout, others) are the outcome mix, not success rates, and stay. Only `ok` was removed (the code `ok` left the `/v1/codes` set `outcome`). | live |
| incidents' `troughSuccessRate` (`profile.incidents[]`, `changelog[].incident`, `/v1/changelog` entries) | no score: a dip of one network has no rank. The website words an incident from `kind` (`down`, `census_stall`), severity, impact and duration. | removed, no score |
| `/v1/events/stats` `pass.rate` | `pass.successScore` {score, status, basis}: 1 + 9 × rate (absolute, as the market), `basis` `"success_rate"` | live |
| tape and `/v1/events` rows' `values.value`, `values.baseline` and `values.baselineRate` on `pulse_finished`, `down`, `success_drop`, `recovered` (success shares; removed with SUCCESS-CURVE-1, #1072) | no score: `values.successes` of `values.requests` (counts) stay | removed, no score |
| `/v1/overlap` network rows | no success rate there; `successScore` (#881). Changelog movers carry ranks only, with no rate. | live |
| the dataset file: every field above | the same score fields (its `pages` are the answers above) | live |

**Rules.** A rank-based score (per network) uses the formula, ties, minimums
and statuses of `scores.success` ("Success scores" above): `ranked` (a tie is a
shared first place, 10), `not_enough_data`, `too_few_networks`.
- *Places and days*: `MIN_REQUESTS` is 100 requests at the place or on the
  day. Fewer than 3 networks with enough requests there give
  `too_few_networks`; this network under the minimum gives `not_enough_data`.
- *Statuses*: every score spark has `statuses`, one per point: a null value's
  status says why it has no score (`not_enough_data`,
  `too_few_networks`; a point without a rate is `not_enough_data` on a
  network's spark and null on the market's),
  so a hover can say "no score".
- *Spark points*: a point is ranked among the networks with a point at the same
  time (the snapshot's series buckets, so the networks' points line up). The
  series carries each point's requests (`completed`), so a point applies the
  100-request minimum and the tie rule as a place does. A point with
  fewer than 3 such networks is null with `too_few_networks`. A network
  without a headline snapshot product has no request counts: its points are
  `not_enough_data`.
- *Absolute* (the market, a target's fleet, the events' pass): a median or
  mean of rank scores is always about 5.5, so these are 1 + 9 × the rate, as
  `market.successScore`. 

Computed in the API from the build it holds: no calculation change and no
round. A null score in a spark or the market delta has its status (or the
answer's usual code) as its reason in the entity's `na`.

**Place rows' and days' scores.** On a network's own place lists
(`/v1/networks/{slug}/places?dimension=country|region|city|asn`, every range
and page, with or without `q`) and in `ranges.<r>.countries[]`, each row's
`successScore` is the network's score among the networks of the same kind at
that place, as `/v1/places/{dim}/{key}` rows: under 100 requests (or no rate)
`not_enough_data`; fewer than 3 networks with 100 requests there
`too_few_networks`; else `ranked` (networks too close to tell apart share
first place, 10). A null `score`, `rank` or `rankedOf` is listed in the page's `na`
with the status as its reason. A country's rows and lists and the
all-networks answer's lists carry none. The build keeps, per place, only the
places where 3 or more networks have 100 requests (the status and each
network's rank), and a row's score is set when its page is cut. In the
dataset file's `lists`, each place row ends with its score, status first:
`["ranked", score, rank, rankedOf]` or `[status]`.

**Days' scores (DAY-SCORE-1, #916).** Each `days[]` row's `successScore` =
`{score, rank, rankedOf, status, basis: "card_targets", targets}` is the same
score as the window's `successScore` ("Success scores" above), computed by the
calculation on that day (the day's card target tries as a 24h range's, its
browser targets over the 7 days ending at the day's end; the best network of
the kind that day reads 10): the snapshot network's
`days[].success_overall`. A day is a slice of the 30d window a day apart from
its start, as its series points, so the last day is the 24h window's span and
its score is the 24h `successScore`. The last day also holds the daily tests the
24h range reaches back to (COUNTRIES-24H-SPAN-1, #1278), which started on the day
before it and count there too: a sum over `days[]` counts such a test twice.
- *Per day and network*, each card target's success by tries that day,
  scored by its distance from the best network of the kind that day, their
  plain mean shifted so the day's best reads 10, and placed
  among the networks with one.
- *`targets`* lists the targets the day counted, sorted (`status`
  `partial_targets` when a card target had no try; none on a day without any
  try: then `score`, `rank` and `rankedOf` are null and `status` is
  `no_ranked_targets`).
- *Null*: the all-networks answer's days (`not_applicable`), and a day the
  snapshot has no score for (a build from before #916).

It replaced #898's per-day rank of the HTTP-only daily success rate, which
showed another number than the window's under the same name; until
SUCCESS-DAILY-DISTANCE-1 (#1229) a day was the all-targets rank mean
(`basis` `"all_targets"`), then until SUCCESS-ALL-8-1 the day's daily tests
(`basis` `"daily_tests"`). Display only: no Index Score, rank or Speed reads it.

**The scan.** `TestEverySuccessRateHasItsScore` (`api/internal/server`)
walks every answer, a page of every network's place lists and the dataset file
of a fixture build (with a community run), and fails on any success rate
without its score; an exemption that exempts nothing fails it too. Its
exemptions, each with why: a network's chart series (`/v1/charts/...`,
`/v1/networks/{slug}/chart`, and a community run's (the run has no success series since #900), not in
this table), the all-networks answer (`/v1/networks/all`: no network to rank
it against), a country's rows (`/v1/countries/{cc}`, #881: no room in the
dataset file), `/v1/coverage` (field names, not values), and
the dataset file's raw sections (`providers[]`, `measuredMarket`, `totals`,
`measuredTotals`: the old site's records, served with their scores by the
answers above).

### Atlas Score trend (ATLAS-SCORE-SPARK-1)

`atlasScoreSpark` is the network's **Atlas Score** (the composite `score`, the
**Balanced** profile: the score that ranks the leaderboard) over the range, next to
`successScoreSpark`, which is unchanged. It is served on `/v1/rankings`
`networks[]` (`{values, statuses}`, times in `atlasScoreSparkT`), `/v1/home`
`networks[]` (`{values, statuses, timesIndex}`, its times in the answer's `times`) and
`/v1/networks/{slug}` `ranges.<r>` (`{values, statuses, times}`; null for the
all-networks answer or a network without a product of the kind).

- **Points**: 24h has **24**, one per hour; 7d has **7** and 30d **30**, one per UTC
  day; oldest first. Each value is a score at **1 decimal, 0-100**.
- **One point**: the network's score of that range **as the build of that hour
  published it** (24h: the newest published build in the hour ending at the point's
  time), or **as the last published build of that UTC day published it** (7d, 30d).
  The last point is always the answer's own build (its `score`), so the trend ends
  at the score shown next to it. Times are minutes before the build's as-of time:
  for 24h each hour back (60, 120, ...); for 7d and 30d each earlier day's end
  (UTC midnight after it).
- **Null**: a value is null unless its status is `ranked`. `no_history`: no
  published build's score is stored for that hour or day (before the history began
  on deploy, or no build then). `not_scored`: that build published no Atlas Score
  for the network (as a null `score`). The status codes are in `/v1/codes`
  `score_status`.
- **One methodology** (SCORE-HISTORY-BACKFILL-1, #1303): a trend shows only points
  of the methodology its own build was calculated with (`methodology.id`). A point of
  an earlier methodology (in production: every build before the 17 Nov 2026 05:06
  UTC recount, among them the 14, 15 and 16 Nov day ends) is never mixed in: it is
  `no_history` unless it was recomputed. SCORE-HISTORY-1: a methodology that differs
  from the build's only in settings no Atlas Score reads (today `[success_score]`,
  the Success x/10's: SUCCESS-ALL-8-1) counts as the same, so such a change keeps
  every point. A change the Atlas Score reads (a weight, the Speed or latency rule)
  is a break: the earlier points read `no_history` until the back-fill restates
  them under the new methodology.
- **Published or recomputed**: a point is the score its build published, or, where
  no published build of the methodology exists for that hour or day, the score
  **recomputed** afterwards by `atlas calculate-backfill-scores`. A recomputed point
  is the real calculation run at that point's time (the build's cutoff, `HH:MM` on
  the builds' grid; a day's point at its last hour, `23:MM` UTC) over the facts
  stored at the recompute, under the methodology current then. Its windows are the
  usual ones ending at that time: 24h the 24 hours before it, 7d and 30d the 168 and
  720 hours before it. It equals what a build at that time would have published,
  except that it keeps no earlier build's score (a build retains its previous
  build's score while its own is incomplete) and that it counts late runs stored
  since. The history marks each row `source` `build` or `backfill`. A published
  point always wins over a recomputed one of the same time, and is never replaced.
  The answers serve both alike, in the same shape.
- **History**: the calculator records each published build's scores in
  `calculated_score_history` (calculated schema v19; its `source` since v21), keeps
  31 whole UTC days and draws each build's trend from it
  (`calculated/score_history.py`). The migration fills it from the builds retention
  kept (the current build and each UTC day's last build of the last 30 days), so on
  deploy the 7d and 30d trends have those days and the 24h trend only its own hour.
  The 24h trend fills within a day, 7d within 7 days, 30d within 30 days; the
  backfill fills the missing hours and days at once (`calculated/score_backfill.py`):
  production's are computed on a copy of its database and imported.
  Hours and days are UTC; a point before the first stored test stays `no_history`.
- A snapshot from before it has no trend: the lists are empty (`[]`).

## Records

| Entity | Fields | Source |
|---|---|---|
| `TapeEvent` | `id`, `kind`, `providerSlug`, `providerName`, `product`, `signal`, `agoSeconds` + *added* `event`, `at`, `source`, `na`, `country`, `region`, `target`, `severity`, `values` | Measured (`source` `measured`): `public.test_events`, mapped by the port of `tape.ts`; `kind` is the event's category (`census`, `pulse`, `incident` or `audit`) and `event` its raw kind (`ip_overlap`, `pulse_down`, `daily_test_finished`, ...; the framework's `estimate_hour_released` is published as `hour_released`, so no answer names the estimate); `at` is the event's time and `agoSeconds` is relative to the snapshot's `generated_at` (or `generatedAt`). Editorial (`source` `editorial`): `site.tape_events`, `kind` and `event` the row's `kind`, `at = occurred_at`, `agoSeconds = generatedAt − occurred_at`. `providerName` comes from the provider list. The dataset's `tape` is both in one time order (`at`), newest first; at the same time a measured row comes first. A measured row stands for every row of its incident (EVENTS-MERGE-1): its `id` is the incident's largest event id. `country`, `region`, `target` and `severity` are the event's, as `/v1/events` serves them, and `values` its numbers ("Tape and event values", below); on an editorial row the four are null and every key of `values` is null. |
| `ChangeEvent` | all + `source`, `na` | `site.changelog`; `providerName` from the provider list |
| `ChangeEntry` | `id`, `kind`, `network`, `severity`, `agoSeconds`, `source`, `incident` (no `na`) + *added* `values`, `naReasons` (CHANGELOG-VALUES-1) + *added* `event` (CHANGELOG-EVENT-1, #983); `title`, `detail`, `delta`, `unit` removed ("Removed on 2026-11-14 (changelog text)", #990) | The website's changelog entry (`changelog-data.ts`), built from the outages, the window figures, `site.pricing_history` and `site.changelog`: `/v1/changelog` and the network answer's `changelog[]` (api/answers/changelog.md). `values` holds only its kind's keys (`pool` `uniqueIps`, `previousUniqueIps`, `deltaPct`; `price` `fromRate`, `toRate`, `deltaPct`, `plan`; `performance` `metric`, `value`, `previous`, `delta`; `coverage` `countries`, `previousCountries`, `delta`, `lostCountryCodes`, `comparedRange`; `authenticity` `value`, `previous`, `delta`; `methodology` `fromId`, `toId`; `incident` none), `naReasons` the reason of each null one (left out when none). `event` is the change's code (`change_event`: a move's code, `incident` or `catalog`), whose catalog entry lists the `values` keys it fills. `incident` *adds* `countries`, `countryCodes`, `kind`, `detectedBy`, `ongoing`, `startedAt`, `endedAt` (as `profile.incidents[]`). `agoSeconds` is when the change was seen: a performance or pool entry's newest slot or day compared, not the build's time; a pool entry needs two complete sides; the body's `movers[]` compare each network's rank with its rank at the range's start in the kept builds (CHANGELOG-REAL-1, #1314; api/answers/changelog.md, "When an entry happened", "Complete pool comparisons"). |
| `UserReview` | all + `source`, `na`; `lang`, `monthlySpendMin`, `monthlySpendMax` (PROVIDER-CODES-1); no `monthlySpend` or `verificationLabel` (#991) | `site.reviews` (`posted_at` → `agoSeconds`); `lang` is `site.reviews.lang`, the BCP 47 language of what the user wrote, and `monthlySpendMin`, `monthlySpendMax` are `monthly_spend_min`, `monthly_spend_max` in US dollars, each null when unknown; `useCase` is `use_case` read as a `use_case` code, null when it is none (#991) |
| `CommunityRun` | all + `source`, `na`; `country` (PROVIDER-CODES-1); no `region` (#991) | `site.community_runs` (typed headline numbers, JSONB `targets`/`successSeries`/`latencySeries`); `providerName` from the provider list; `country` is `site.community_runs.country`, an ISO code (null when unknown); `rejectionReason` is `rejection_reason` as a `run_rejection_reason` code, `other` for a reason that is none (#991) |
| `TargetMeta` | `id`, `category` (a `target_category` code), `difficulty` + `source`, `engine`, `group`, `na` (no `label`, `passCondition`: #989) | `site.targets`, falling back to `public.targets`; `engine` is `public.targets.engine` (`"http"` or `"browser"`, null for a catalog-only target); `group` is the group target the snapshot names in `w.targets[id].browser.group` (null when none, by contract) |
| `TestScope` | `id`, `targets`, `countries`, `cadence`, `geoCoverage`, the four booleans + `na` (#966) | `site.scopes` (`targets` is the measured targets' ids when `all_targets`, else `target_ids`; `geoCoverage.code` is `geo_coverage`); `countries` and `geoCoverage.countries` from the published product groups (null, `not_measured`, without a measuring network); the cadence numbers are the configuration's: 1 daily test per network and country a day (one `formulas/*.json` `schedule` hour per country), 100 browser tries per network and target a day (the browser queue's `tries` × 1440 ÷ `every_minutes`; 1 before META-SCOPES-FIX-1, never deployed), `pulseIntervalMinutes` the snapshot's scoring `slots.pulse_interval_minutes` when it has a pulse batch, else null (`no_pulse_fleet`); each null's reason in `naReason`/`naReasons` (#994, `api/answers/meta.md`) |
| `RankingProfile` | `id`, `weights` + `na` (no `label`: #989) | `site.ranking_profiles` (`weights` JSONB, all 8 keys always present) |

`agoSeconds` of every `site.*` record is the dataset's `generatedAt` (the snapshot's
`as_of`, else the read time) minus its timestamp, never negative.

### Tape and event values (TAPE-VALUES-1)

Every tape row (`/v1/home` `tape[]`, the network answer's `tape[]`, the dataset's
`tape`) and every `/v1/events` row carries `values`: the event's numbers, made by one
function (`build/event_values.go` `eventValues`) from its raw kind and `data`, so a
tape row and the same event's `/v1/events` row always agree. No answer serves the raw
`data` (`/v1/events` `data` went in #993): its `baseline` is a success share on `down`,
`success_drop` and `recovered` but milliseconds on `latency_spike`, so the normalised
numbers have their own object. Every `values` object, of every kind, carries the same 8 keys
`{requests, successes, p50, baseline, value, uniqueIps, sharedIps, hour}`, so a
client reads one key set, plus its kind's own keys below; the success kinds `pulse_finished`,
`down`, `success_drop` and `recovered` carry 6 of them, without `value` and `baseline`
(their success shares, removed on 2026-11-14, SUCCESS-CURVE-1, #1072: read `successes` of
`requests`); a key the kind or its
raw data lacks is null, never left out (null by contract: a tape row's `na` lists
none of `values`, `country`, `region`, `target` or `severity`). `value` is the
row's numeric headline (column "`value`"; the text `value` was removed on 2026-11-13, prose). `p50`, `previousP50` and `baseline` (where it is a latency)
are ms; every share is 0 to 1; `hour` is an ISO time, the start of the event's hour,
wherever its `data` has one (a release's `day` and `hour`, an audit's `hour`; #984).
`durationSeconds` was `durationS` until 2026-11-13 (#984).

| `event` (raw kind) | category | `values` (beyond the 8) | `value` | from `data` |
|---|---|---|---|---|
| `pulse_finished` | pulse | `{challenges, blocks, previousP50, previousSuccesses, previousRequests}` | none: the success share went on 2026-11-14 (#1072) | no `baseline` (a pulse has no baseline, only the previous pulse; it was always null); `previousP50` (ms), `previousSuccesses` and `previousRequests` are `previous.p50`, `previous.successes` and `previous.requests`: the previous pulse's counts, never its rate (TAPE-VALUES-2, #912). Events stored before 2026-11-12 do not carry the previous pulse's counts: there both are null (reason `not_measured`), never derived from the stored rate |
| `latency_spike` | incident | | p50 in ms (raw `value`) | `baseline` is the series' baseline p50 in ms; `p50` falls back to `value` |
| `down`, `success_drop`, `recovered` | incident | on `recovered` `durationSeconds` | none: the success share went on 2026-11-14 (#1072) | no `baseline` (it was always null) and no `baselineRate` (the raw `baseline`, the baseline success share, went on 2026-11-14, #1072); `durationSeconds` is a census outage's length (null after a pulse incident) |
| `block_spike` | incident | `{blockShare, baselineBlockShare}` | the block share (raw `value`) | `baseline` is null; `blockShare` is `value`, `baselineBlockShare` the raw `baseline` |
| `estimate_hour_released` (served as `hour_released`) | census | `{countries}` | the unique IPs | `hour` from `day` and `hour` (`"2026-11-02T07:00:00.000Z"`); `countries` is a count |
| `daily_test_started` | census | `{countries}` | the countries count | the length of `countries` |
| `daily_test_finished` | census | `{countries, durationSeconds}` | the unique IPs | `countries` is the length of `countries`; a failed test is `signal` -1, severity `minor` |
| `ip_overlap` | audit | `{share, peers, runs}` | the shared share (raw `value`, as `share`) | `sharedIps` and `uniqueIps` are `shared_ips` and `unique_ips`; `hour` is the audited hour (`data.hour`); `share` is `value` (0 to 1); `peers` is how many other networks share addresses; `runs` is the hour's test runs of the network (null for a census slot's overlap) |
| `new_hosting_asn` | audit | `{asn}` | the IPs on that ASN (raw `ips`) | null today: the raw data holds no IP count |
| `pulse_down` | pulse | `{errors, windowMinutes, regions}` | null (the text reads "Down") | |
| `pulse_recovered` | pulse | `{durationSeconds}` | null (the text reads "Recovered") | |
| `latency_rise` | incident | | p50 in ms | both ms (`baseline` the daily test's p50) |
| `census_finished` | census | `{durationSeconds}` | the unique IPs | |
| `census_started`, `census_retry`, `census_skipped`, `node_offline`, `no_node`, any other kind | | | null | no numbers: the 8 keys, all null |

`title`, `detail` and `value` were removed on 2026-11-13 (prose). The text left
is names (`providerName`,
`product`), ids (`id`, `kind`, `event`, `providerSlug`, `country`, `region`,
`target`), enums (`severity`, `source`) and `at`.

## Deviations from the TypeScript

The Go port differs from the website's `presentation.ts`, `window.ts`,
`overview.ts` and `tape.ts` only in these ways. A parity test enforced them while
the website lived in the old `proxy-index-api` repository; it went with the website.

1. **Placeholders become null.** For measured providers, `hqCountry`,
   `pricing.model`, `trust.kycVerified`/`consentDisclosed`,
   `products[0].url` and `score.grade` are null (and listed in `na`)
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
9. **Search.** The website's palette is its own (`search.json.ts`):
   `/v1/search-index` is removed (SEARCH-NEUTRAL-1, #981), and `/v1/search`
   answers typed items (`type`, `id`, `name`) of the residential networks and
   the card targets, with no English: no group, title, subtitle, keywords,
   meta, link, icon, command, page or action item.

`measuredTotals` uses the same fields as `totals`, aggregated by the API over
every provider without the record counts (with the demo fixtures gone, every
provider is `source = "measured"`).

`measuredMarket` supplies the All market panel without simulated inputs: three
window views with measured requests, pool sizes, latency, success, observed wire
bytes, history and price picks; `authenticity` is the exit-IP-weighted country
match and `targetCount` counts framework targets. Missing measurements stay null.
A view's `fleet.bandwidthGb` is the sum over the networks' headline windows of
`(metrics.bytes_up + bytes_down) ÷ 10⁹` (decimal GB, see "Bandwidth" below),
the same figure as `/v1/home` `market.bandwidthGb`.

Measured `WindowStats` `successScoreDelta` (from the internal success change) compares the closing
fifth with the opening fifth of observed samples in the selected window. Finer
24-hour history replaces overlapping daily buckets; missing samples are omitted.
Success is a percentage-point difference. Two observations are required.

`latencyDeltaPct` (LATENCY-DELTA-24H-1, LATENCY-DELTA-PERIOD-1) compares whole days
(since LATENCY-BASKET-1 it compares the network's pooled pulse latency, not the basket
`latencyP50`: its own change, with no penalty, both periods by the same rule; a basket-based
change is a follow-up)
instead: every country has one daily test a day at its own hour, so only a whole day holds
every country. It is the headline latency p50 (the pulses' latency of normal requests, the
countries counted those the pulses tested; HEADLINE-PULSE-1; the latency under load never
enters it) of the last whole UTC day against the day before it (24h), of the last 7 whole UTC
days against the 7 before them (7d), and of the last 30 against the 30 before them (30d), in
percent at 1 dp, positive when slower. The calculator makes it once a day at 03:00 UTC, after
the last daily tests of the day before have finished, over the whole days before that hour
(snapshot: each window's `latency_day_delta`, its `days` the two periods,
`calculated/latency_days.py`), so every build of a day serves the same value and a
half-tested day never makes one. Each period passes the figure's own rule (time slots with
`min_latency_successes` successes) and has at least 90% of the countries tested over the two
periods, each with `min_latency_successes` successes in it. Otherwise it is null with the
`naReasons` code `needs_two_days` when the network has no test before the earlier period (it
is new), or `day_not_covered`; `not_published` from a calculation before it.

A range always compares the longest whole periods the network's own data allows
(LATENCY-DELTA-FALLBACK-1): when its two periods do not pass, 30d compares the last 7 whole
days with the 7 before, and 7d or 30d the last whole day with the day before (the 24h rule).
So a network with under 14 days of tests shows the day rule in 7d and 30d, from 14 days the
7-day comparison in 7d and 30d, and from 60 days the 30-day comparison in 30d. Each network
falls back on its own history. LATENCY-ALWAYS-1 (the owner's rule: always a value): a pair of
periods that fails only the 90% country rule (one day or week partly tested, such as Shifter's
15 Nov 2026 with its 15:00 hour excluded: 159 countries against 205) is compared on the
countries tested in both periods, each with `min_latency_successes` successes, both p50s made of
only their rows, when those are at least half of the countries tested over the two periods and
at least 5 (`COMMON_SHARE`, `COMMON_MIN`), before the next shorter periods; the basis stays the
periods' (`day`, `week`). The 30-day comparison has no such step: the per-day store keeps each
day's merged latency, not each country's, so it falls back to the weeks. When no pair gives a
value, the network keeps the last value computed for it (the current build's, carried on build
after build, `calculated/latency_carry.py`) with its own basis; `latencyDeltaAsOf` is the last
UTC day (`YYYY-MM-DD`) the compared periods cover: the day before the calculation's day for a
fresh value, earlier for a carried one. The value is null only when the network never had one,
with the day rule's reason (`needs_two_days`, `day_not_covered`), always in
`naReasons.latencyDeltaPct` (never folded into the entity's `naReason`, #1296); then
`latencyDeltaBasis` and `latencyDeltaAsOf` are null with the same reason. `needs_two_periods`
comes only from a snapshot calculated before LATENCY-DELTA-FALLBACK-1. `latencyDeltaBasis` beside it names the
periods compared: `day`, `week` or `month` (`/v1/codes` set `latency_delta_basis`), null when
`latencyDeltaPct` is null (with its reason) or does not come from this rule (a network
without a snapshot product). It is served wherever `latencyDeltaPct` is: `/v1/rankings` and
`/v1/home` `networks[]`, `/v1/networks/{slug}` `stats.<r>` and the dataset file, and so is
`latencyDeltaAsOf`. The
calculator reads 30 days of raw rows, so the 30-day comparison takes the 30 days before them
from its per-day latency store (LATENCY-DAY-STORE-1, `calculated/latency_store.py`, filled
from the raw rows since its first calculation): a network live since 14 Nov 2026 gets its
first on about 13 Jan 2027, and its first 7-day comparison on about 28 Nov 2026. The
changelog `performance` entries compare the same two periods, their `values.basis` the same
code.

## Per-page answers

These small answers (each at most 200 KB, `build.MaxAnswerBytes`) are computed
once per build from the published snapshot and the dataset's providers. Numbers
the API does not have are `null`, as everywhere else.

### Point times (SPARK-TIMES-1)

Every point of every spark or series has its time as a number next to its value, in the
same order: **minutes before the build's as-of time** (the range windows' `period.end`,
which is `generatedAt` when a snapshot is published, the reference of the tape's
`agoSeconds`), oldest first. No series has English point labels: the chart answers'
went with UNREAD-TEXT-1 (#977), the home's and the dataset file's with
TIME-LABELS-REMOVE-1 (#988, "Removed on 2026-11-14 (time labels)").

| Answer | Times |
|---|---|
| `/v1/home` networks' `successScoreCurve`, `latencySpark`, `successScoreSpark`, `atlasScoreSpark` | `timesIndex` (SERIES-TIMES-INDEX-1, #985): the index of the series' list in the answer's `times` (one entry per distinct list); every network `successScoreCurve` is on the range's one axis (its UTC hours or days, "Score curves" below), `null` at a network's gaps (`answers/home.md`) |
| `/v1/home` `market.successScoreCurve`, `uniqueIpsCurve`, `latencySpark`, `successScoreSpark` | `times`, beside `values` (`uniqueIpsCurve`: each slot's start, and `partialFirst`, `partialLast`) |
| dataset file `measuredMarket.views[]` `headlineSpark`, `latencySpark` | `times`, beside `values` |
| `/v1/networks/{slug}/chart` (and `/v1/charts/...`) histories | each point's `t` (the same unit) |
| `/v1/rankings` `networks[].trend`, `successScoreSpark` | `trendT` (the same unit) |
| `/v1/rankings` `networks[].atlasScoreSpark` | `atlasScoreSparkT` (the same unit; ATLAS-SCORE-SPARK-1) |
| `/v1/networks/{slug}` `ranges.<r>.atlasScoreSpark` | `times`, beside `values` |

`/v1/networks/{slug}`, `/v1/networks/all`, `/v1/targets` and `/v1/overlap` have no
labelled series; a network's own chart answer keeps its own points (no `null` padding).

### Success score points (SUCCESS-TRAILING-1)

Every success score point (`/v1/home` `networks[].successScoreSpark` and `market.successScoreSpark`,
`/v1/rankings` `successScoreSpark`, `/v1/networks/{slug}/chart` `success[].score`) is its headline's
rule over the **trailing window of the range's length ending at the point**: for 24h, the 24 hours
ending at the end of the point's hour; for 7d and 30d, the 7 or 30 days ending at the end of its UTC
day (#1301, the owner's requirement of 17 Nov 2026).

- A network's point is the calculator's (`series[].success_score`): the window's Success x/10 rule
  (SUCCESS-ALL-8-1: each card target's distance from the best, their plain mean, shifted so the
  best network reads 10) over that trailing window's card target tries (a 24h point's browser
  targets over the 7 days ending at the point), the best network the kind's best over the same
  window. `status` `partial_targets` keeps the score. The market's is `1 + 9 ×` the median of the fleet networks' trailing success rates
  (`series[].trailing_success_rate`, the window's time-slot mean over that window), as
  `market.successScore` is of their window rates.
- So the **newest point is the headline**, exactly, and a steady network is a flat line at its
  headline. Neighbouring 24h points share 23 of their 24 hours.
- Why trailing: a point over its own hour or day alone (the rule of 17 Nov, a few hours long) was
  too noisy. An hour holds only the few countries whose daily test ran then (each country is tested
  once a day), and the hour's best network was the one that tested easy countries; at 1 point per
  percentage point below the best, a steady network read 1.0 in some hours.
- A trailing window reaching before the raw rows the calculator keeps (30 days; a 30d point's window
  reaches 60 days back) has the rows that exist.
- **A point needs one whole day of the network's data** in its trailing window
  (SUCCESS-FULL-DAY-1): each country is tested once a day, so less than a day holds only a few
  countries. A point ending less than 24 hours after the network's first HTTP request (the days before
  its data starts, and the day it starts in) is null with `not_enough_data`, never a score, and does
  not count for the best of the kind. On staging the 7d and 30d first-day points of networks live
  since 14 Nov 18:00 read 1.0 from six hours of tests; they are now empty. A network with less than
  a day of data has an empty newest point while its headline may have a value.

### Score curves (CURVE-HOURLY-DAILY-1)

`/v1/home` `market.successScoreCurve` and every `networks[].successScoreCurve` (#1301, the
owner's request of 17 Nov 2026: no 5-minute points) have one point per **UTC hour for 24h (24
points)** and one per **UTC day for 7d and 30d (7 and 30 points)**, oldest first; the newest
hour or day is partial, with what exists at the build's as-of time. Before it the market curve
had 110 points for 24h and the networks' 84 (thinned 5-minute points), and 7d and 30d every
point (about 294).

- **Points in a bucket**: the curve reads the range window's own series rows (`measuredHistory`;
  no finer window is merged in since PR 3 of #1301). Since SERIES-HOURLY-DAILY-1 the calculator
  writes one row per UTC hour (24h) or day, so each bucket is one row. A snapshot from before it
  has 5-minute rows (24h) and 24-hour rows from the window's start (7d, 30d): each counts in the
  hour or day that holds its middle. Rows before the oldest bucket are left out (the range's
  partial oldest hour or day).
- **The home's other series** (#1301): `market.latencySpark` (the median of the fleet
  networks' p50 of each hour or day, at least 3 networks), `market.successScoreSpark` (1 + 9
  × the median of the networks' rates of each hour or day, `market.successScore`'s rule), and
  each network's `latencySpark` (its row's p50; never an average of percentiles: a bucket with
  several rows is null) and `successScoreSpark` (its row's point score, the window's Success x/10
  rule over the card target tries of the trailing window ending at that hour or day, SUCCESS-POINTS-1) are on the same axis and `times`.
- **Value**: `1 + 9 × Σ successes ÷ Σ completed` over the bucket's rows, as `market.successScore`,
  2 decimals. Each row's rate is its request ratio (`series[].success_rate`, the same rows
  and counting-country rules as the points before) and it weighs its `completed` requests, so a
  heavy slot outweighs a light one; it is not a plain mean of the points. The market pools every
  fleet network's rows of the bucket (request-weighted over networks); a network's curve reads
  its own. A network without a snapshot product (window statistics only, no request counts)
  weighs each point 1.
- **Gaps**: a bucket without a completed request is `null`, never 0. No minimum beyond one request.
- **Times**: each bucket's start in minutes before the build's as-of time (SPARK-TIMES-1); the
  networks' curves share one `times` list (SERIES-TIMES-INDEX-1): the number of lists does not grow.

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

### Codes (`/v1/codes`)

Every code an answer sends is in one registry (`api/internal/build/codes_*.go`, CODES-CATALOG-1),
served as `/v1/codes` and written to [CODES.md](CODES.md) with the conventions every answer
follows; every field in [answers/codes.md](answers/codes.md). A task that adds a code, or a
field that holds codes, registers it there in the same change (`build.RegisterCodeSet` from an
`init()` in its area's file), and `TestEveryServedCodeIsRegistered` fails on a value at a
registered path that is not a code of its set. `/v1/version` and `/v1/codes` carry `contract`,
the date of the last breaking change; [CHANGES.md](CHANGES.md) lists every field added, changed
or removed under its date.

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
the old site's `kindStanding` number: `rank`, or 1 for a network with a score alone in
its kind (`peers` 1), which the framework leaves unranked ("#1 of 1"). A
catalog network without a snapshot product has no score or rank.

Row fields, from that range's `windowStats` (see "Window statistics of a
measured provider"): `latencyP50`, `latencyP90`,
`latencyP99` (the headline basis: normal requests, the pulses'; HEADLINE-PULSE-1),
`latencyDeltaPct`, `uniqueIps` (the group's distinct IPs), `requests`,
`bandwidthGb` (the window's wire bytes ÷ 10⁹, see "Bandwidth"), `uptimePct`, `downtimeMinutes`, every per-range figure
(`countries`, `regions`,
`cities`, `asns`, `datacenterAsnShare`, `datacenterIps`, `exclusiveShare`, `sharedShare`,
`exclusiveIps`, `exclusiveScore`, `observedDays`, `latencyUnderLoad`, `underLoadLatencyP50`/`P90`/`P99`, `costPer1kAllTargets`,
`partialCostPer1kAllTargets`, `targetsCountedAllTargets`, `missingTargetsAllTargets`, `costIsLowerBound`,
`successScore`, `successScoreDelta`; all of that range, null without a snapshot
product), `trendT` (each point's time, minutes
before the build; the success history `trend` was removed on 2026-11-14, #900) and
`successScoreSpark` (`{values, statuses}`, each point's Success x/10 over the card target tries of the trailing window ending at that hour or day, computed as the row's `successScore` (SUCCESS-POINTS-1, #1301); "Success
scores instead of success rates"), and `atlasScoreSpark` (`{values, statuses}`, the Atlas Score
trend, its times in `atlasScoreSparkT`; "Atlas Score trend"). `valueRankAllTargets` is the rank the row's range score
publishes with its value component (`components.value.inputs.rank`, VALUE-ALL-RANK-1), so
`breakdown.value` and `valueRankAllTargets` always agree: the competition rank
of `costPer1kAllTargets` (a lower bound counted as it is) among the networks of
the row's kind with one, cheapest first, ties sharing 1 + the number strictly
cheaper; null with `catalog` or `incomplete_targets` as `costPer1kAllTargets`.
A snapshot from before VALUE-ALL-RANK-1 (no `cost_is_lower_bound` in the value
component's inputs) gets the API's own rank of its own bill instead, among the
rows of the same kind that have one. The plain-HTTP `valueRank` is no longer
served (VALUE-ALL-RANK-1): one value rank, on the 8-target bill.

**The lower-bound bill** (VALUE-ALL-RANK-1, the owner's rule). A network
whose window has no success on a card target (Proxy-Cheap and Byteful in the
30d test data: none on `google_search` and `cloudflare_challenge`) has no cost
of its own there. The calculation counts each such target at the highest cost
per 1,000 successful requests any network of the same kind has on it in the
window, and bills the mean over all 8 with those substitutes: the network
costs at least that much. It is served as `costPer1kAllTargets` with
`costIsLowerBound: true` and ranked by it (`valueRankAllTargets`, so near the
bottom), beside the partial figure over the targets it has,
`partialCostPer1kAllTargets`, with `targetsCountedAllTargets` (6) and
`missingTargetsAllTargets` (`["google_search","cloudflare_challenge"]`). A
client shows it as "at least $X" (and may show "6 of 8 targets"):

```json
{"slug":"proxy-cheap","costPer1kAllTargets":19.4,"costIsLowerBound":true,"valueRankAllTargets":17,
 "partialCostPer1kAllTargets":11.28,"targetsCountedAllTargets":6,
 "missingTargetsAllTargets":["google_search","cloudflare_challenge"]}
```

The bill stays null, never 0, with `incomplete_targets` only when a card
target has no cost for another reason (no run or no wire bytes), and with
`catalog` without a list rate. A build from before VALUE-ALL-RANK-1 has no
lower bound: the API's own bill is null with `incomplete_targets` whenever a
card target has no cost, no success included.

Each row lists
its nulls in `na` with their reasons (`naReason`, `naReasons`; api/answers/README.md,
"Missing data"), and the answer its own (`cost.*`, `profiles` when empty).

From the provider's blocks (the kind's with `kind`): `listRate`
(the cost basis, "The cost basis"), `priceBasis`, `basisPlanGb`, `pricePerGb`, `pricePerGbAtScale`,
`hasTrial` (the trial text is not "none"; null without one), `trialKind` read
from the trial text: `none`, `sales` (a sales call or contact), `credit` (free
credit, PRICING-CODES-1), `paid` (a price or a refund window), `card` (free,
card required), else `free`; null without one; `authenticity` (`trust.residentialAuthenticity`), `kycVerified`,
`consentDisclosed`, `stickinessHoldRate`,
`blacklistHitRate`. `browserOverall.<engine>.success {score, rank, rankedOf,
status}` is the range's `windows[r].browser_overall` ("Browser scores" below),
null when the range has none.

The answer also carries `targets {all, measured, cards}` (the dataset's
targets, the measured ones, and the cards: measured targets of no group, as
`/v1/meta` `targetCounts`), the dataset's
`weights` and `profiles`, `fleet {requests, successes}` (the range's summed
over the rows listed; a network without a value adds nothing) and `cost`, the
true-cost summary. `cost.allTargets` is the summary over the rows listed with a
`costPer1kAllTargets`: `cheapest` and `dearest` (`{slug, name, costPer1kSuccess}`,
ties in list order; their `costPer1kSuccess` holds the 8-target bill), `median`,
`spreadX` (dearest ÷ cheapest) and `priced` (how many); `cheapest`, `dearest`,
`median` and `spreadX` are null when none is priced, with `incomplete_targets`
when a row is priced (`catalog` otherwise). The plain-HTTP summary beside it
(`cost.cheapest` ... `cost.priced`) was removed on 2026-11-13 (item 14).

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","range":"24h","profile":"balanced",
 "targets":{"all":19,"measured":17,"cards":10},"weights":{"reliability":22,"speed":14,"...":0},"profiles":[],
 "fleet":{"requests":1840000,"successes":1802000},
 "cost":{"allTargets":{"cheapest":{"slug":"net00","name":"Net 00","costPer1kSuccess":0.42},"median":0.71,
  "dearest":{"slug":"net07","name":"Net 07","costPer1kSuccess":1.52},"spreadX":3.62,"priced":12}},
 "networks":[
 {"position":1,"slug":"net00","name":"Net 00","source":"measured","kind":"residential","kinds":["residential"],
  "rank":1,"peers":18,"standing":{"rank":1,"peers":18},"previousRank":2,
  "score":91.4,"grade":"A","breakdown":{"reliability":96,"speed":88,"uptime":null,"...":null},
  "latencyP50":1113,"latencyP90":2210,"latencyP99":4120,"latencyDeltaPct":-2.1,
  "uniqueIps":15320,"requests":102000,"uptimePct":99.9,"downtimeMinutes":1.4,"valueRankAllTargets":1,
  "listRate":3,"pricePerGb":4,"pricePerGbAtScale":3,
  "hasTrial":true,"trialKind":"free","authenticity":0.97,"kycVerified":true,"consentDisclosed":null,
  "stickinessHoldRate":0.94,"blacklistHitRate":0.02,
  "countries":212,"regions":1800,"cities":9100,"asns":2400,
  "datacenterAsnShare":0.012,"exclusiveShare":0.71,"sharedShare":0.29,"observedDays":null,"latencyUnderLoad":false,
  "underLoadLatencyP50":1450,"underLoadLatencyP90":1770,"underLoadLatencyP99":3970,
  "costPer1kAllTargets":0.42,"partialCostPer1kAllTargets":null,
  "targetsCountedAllTargets":8,"missingTargetsAllTargets":[],"costIsLowerBound":false,"trendT":[1380,720,0],
  "browserOverall":{"camoufox":{"success":{"score":7.4,"rank":2,"rankedOf":9,"status":"ranked"}}}},
 {"position":21,"slug":"net20","name":"Net 20","source":"measured","kind":"residential","kinds":["residential"],
  "rank":null,"peers":null,"standing":{"rank":null,"peers":null},"previousRank":null,"score":null,
  "grade":null,"breakdown":{"reliability":null,"...":null},"...":null,"valueRankAllTargets":null,
  "listRate":null,"hasTrial":null,"trialKind":null,"trend":[],"trendT":[],
  "browserOverall":null}]}
```

### `/v1/networks/all`

Every network together, as one more network in the shape of
`/v1/networks/{slug}`: `network` (`slug`, `kind` `"all"`, `kinds` every tested
category, `rank`/`score` null; `name` null, `not_applicable` in the answer's `na`:
UNREAD-TEXT-1, #977; `uniqueIps`, `countries`, `latencyP50`
of the 30d range), `networks` (how many networks published a
product group), `productGroups`, `requestedCountries`, `days` and `ranges`.

- Summed over every product group, exactly: `requests`, `successes`, per day as well.
- Distinct, from the snapshot's every-group `fleet` entry of the range
  (`cohort` `"*"`, atlas `fleet_totals`): `uniqueIps` (`unique_ips`: an
  exit IP seen by several networks or requested countries counts once),
  `latencyP50/P90/P99` (`metrics.latency_ms`: the latency of normal requests
  over every group, merged as within a cohort) and each day's `uniqueIps` (the 30d entry's
  `series[].unique_ips`). A snapshot without that entry leaves them null; they
  are never a sum of the networks' figures, and the summed `ipsSum` totals are
  null here.
- Places: `exitCountries`, `lists` and the top 50 `countries` are the union of
  every network's places (so `lists.country`, `.region`, `.city`, `.asn` are the
  distinct places seen); a place row adds the networks' requests and its
  `ipsSum` adds their IPs (an upper bound, as on a network's rows).
  `/v1/networks/all/places?dimension=&range=&q=&page=` pages them.

- Targets: `targets[]`, each target's `requests` and `bandwidthMb` summed over
  every group ("Traffic per target" above).

A network's own answer has the same distinct fields: with one product group in
the category they are that group's (`pool.unique_ips`, `metrics.latency_ms`,
`series[].unique_ips`, and `ranges.<r>.datacenterIps`, its stats'
`datacenterIps`); with several they are null, because the groups' IPs
can repeat and percentiles do not add up.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","kind":"all","networks":18,"productGroups":720,"requestedCountries":["AA","AB","..."],
 "network":{"slug":"all","name":null,"source":"measured","kind":"all","kinds":["residential"],"rank":null,"score":null,
  "uniqueIps":4321,"countries":240,"latencyP50":1113},
 "days":[{"day":"2026-10-05","requests":120000,"successes":117700,"ipsSum":null,"uniqueIps":2210}],
 "ranges":{"24h":{"requests":4000,"successes":3925,"ipsSum":null,"uniqueIps":1630,
  "latencyP50":1113,"latencyP90":1395,"latencyP99":3292,"exitCountries":240,
  "countries":[{"key":"US","countryCode":"US","ipsSum":5100,"requests":900,"successes":880,
   "latencyP50":980}],
  "lists":{"country":240,"region":1800,"city":9000,"asn":2500},
  "targets":[{"target":"generic_http","requests":3400,"bandwidthMb":2.72},{"target":"generic_https","requests":600,"bandwidthMb":0.48},
   {"target":"amazon_pdp","requests":0,"bandwidthMb":null},{"...":null}]},"7d":{"...":null},"30d":{"...":null}}}
```

### `/v1/networks/{slug}?kind=` (the network page)

Besides the summed totals and places above, a network's answer carries what
`/v1/providers/{slug}` used to, without its long lists:

- `profile`: the Provider's presentation (`source`, `slug`, `name`,
  `kinds`, `primaryKind`, `hqCountry`, `hqCity`, `founded`, `useCases`, `website`, `products`, `measuredProduct`, `score`,
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
    unknown. `score.standing {rank, peers}`: the rankings rows' `standing`
    (the framework's rank, or 1 for a scored network alone in its kind); its
    `note` ("#1 of 18 in its kind") was removed on 2026-11-13 (prose).
  - `pricing.listRate` (the cost basis, "The cost basis"), `pricing.hasTrial`
    and `pricing.trialKind` (`free`, `card`, `paid`, `credit`, `sales` or `none`): the
    rules of the rankings rows (CMP-12), null without a price or a trial text.
  - `protocol {targets}`: the ids of the category's `targets` with requests.
    Its `label` and `summary` were removed on 2026-11-14 (#991: the product is
    `measuredProduct`, its scope `/v1/meta` `scopes[]`), its `schedule` and
    `cadence` and the profile's `schedule` on 2026-11-13 (prose): the cadence is
    `/v1/meta` `scopes[].cadence`.
  - `reviews` (`count`, `verifiedCount`, `mean`, `distribution`, `verifiedMean`)
    summarises the answer's reviews (the kind's, below), plus `runVerifiedCount`,
    the reviews with verification `run_verified` ("backed by a submitted run";
    `verifiedCount` also counts `invoice_verified`). `/v1/dataset`'s provider
    `reviews` still counts every product.
- `stats`: `windowStats` of that category per range (see "Window statistics of
  a measured provider", with its per-range figures `countries` to
  `successScoreDelta`) without `success`, `availability`, `latency`, `pool`
  and `bands`, plus:
  - `ranks`: `uptimePct`, `datacenterAsnShare`,
    `valueRankAllTargets` and `listRate`, each `{rank, peers}` among the networks of the answer's
    kind in that range: the rows of `/v1/rankings?range=&kind=`, computed once
    per build (`build/ranks.go`). Ranks are competition ranks (ties share a
    rank, as `tileRanks`): uptime highest first, hosting
    share, the 8-target bill (`valueRankAllTargets`) and `listRate` (the cost basis) lowest first.
    `peers` counts the networks with a value; `rank` is null without one.
    `valueRankAllTargets` is the row's `valueRankAllTargets` with the
    `ranked` count its value component publishes (`components.value.inputs`,
    VALUE-ALL-RANK-1); in a build from before it, `peers` counts the rows of
    the kind with the API's own rank. The plain-HTTP `ranks.valueRank` is gone.
  - `alternative {slug, name, successScore, latencyP50, cheaperBy}`
    (`successScore`: the alternative's own score in the range, #898): the old
    site's pick (`provider-view.ts` `cheaperAndBetter[0]`): the first network
    of the kind's ranking (in its order) whose all-targets bill
    (`costPer1kAllTargets`) is lower and whose success rate (internal, not served) is at most 2 points
    below this network's; `cheaperBy = 1 − its bill ÷ this network's` (updated
    in place on 2026-11-13, item 14: the plain-HTTP cost before). This
    network's lower-bound bill is compared as it is (`cheaperBy` is then at
    least that); another's is never the alternative. Null when none qualifies
    (`no_cheaper_peer` in `naReasons`), this network has no all-targets bill
    (`incomplete_targets`), no list rate (`catalog`) or no run
    (`no_run_in_window`) (ALT-REASON-1).
  - `claimShare`: the range's `uniqueIps` ÷ `profile.pool.claimedPoolSize`
    (the old site's "pool vs claim"), null without a claimed pool size.
    `claimShare` can exceed 1 when more distinct IPs are observed than the
    network claims; the API does not cap it; a page may clamp its display.
  - `attemptsPerSuccess` (SUCCESS-RATE-REMOVE-1, #900; ATTEMPTS-REQUESTS-1,
    #1117): the requests one success takes in the range, its requests ÷
    successes (unrounded; 0.8 of them succeeding is 1.25): every
    non-browser request of the range, the daily tests' and the
    `generic_http`/`generic_https` pulses', so it is `ranges.<r>.requests` ÷
    `ranges.<r>.successes` and the rate every cost per success divides by
    (the window's `request_success_rate`; `success_rate` in a snapshot
    without one). It is not 1 ÷ the time-slot success rate the success
    score reads, in which every hour weighs the same however many requests
    it held. Null without a rate (the figures' reasons: `not_measured`,
    `no_run_in_window`), and `no_successes` when no request succeeded.
    Display only: no score, rank or cost reads it.
  - `na`, `naReason`, `naReasons`: the range's nulls and their reasons
    (api/answers/README.md, "Missing data").
- the network's newest `reviews` (published), `communityRuns` and
  `changelog` rows, at most 20 each (`build.RecordCap`; the lists are newest
  first), its `tape`, and `records`, the full length of each list. The
  `tape` is the newest 10 rows of each tape tab among the network's rows of
  the dataset's tape (NETWORK-TAPE-QUOTA-1), merged newest first, so at most
  40 measured rows: `census` (`daily_test_started`, `daily_test_finished`,
  `hour_released`, `census_*`), `pulse` (`pulse_*`), `incident` (`down`,
  `recovered`, `success_drop`, `latency_spike`, `block_spike` and the other
  incident kinds) and `audit` (`ip_overlap`, `new_hosting_asn`), the tabs of
  `/v1/home` `tape[]` and `/v1/events?kind=`. A tab without events has
  no rows (no padding). An editorial row (`site.tape_events`) of another
  `kind` is kept the same way, the newest 10 of its kind. The count is of
  merged rows (EVENTS-MERGE-1). The build reads each network product's
  newest 10 events of every tab from the 30 days before the newest event
  (one window query partitioned by product and tab, `store.Inputs.TapeHeads`;
  40 for the tabs whose rows merge), so a burst of one kind (13 `ip_overlap`
  rows in a minute) never empties the other tabs. The whole history
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

Size (NETWORK-PAGE-SIZE-1, #938): every network answer is at most 180 KB, about 20 KB under
the 200 KB cap (`build.MaxAnswerBytes`), so a page nears the cap in a test and not in
production, as `/v1/home` did (#905). `TestEveryNetworkBodyFitsWithRoom`
(`api/internal/build/network_size_test.go`, fixture in `network_scale_test.go`) builds 18
networks at production's largest counts or more: 230 requested countries each with its
status, the week's 14 targets on two engines in every range and on every day's score, the
top 50 exit countries per range with 6-digit counts and an IP rank, 17 overlap peers, 35
priced plans with their catalog, 40 tape rows, 20 changelog rows and 30 incidents. It fails
when an answer (the all-networks one too) is over 180 KB or a network's is short of one of
those counts, and logs each answer's size and the largest one's bytes per top-level,
`profile` and `ranges.30d` key. On 2026-11-15 the fixture's largest answer was 165,873 bytes
(profile 70,669, ranges 35,409, tape 18,530, requestedCountryStatus 12,297, days 11,797);
production's largest, Geonode, was 128,344 bytes (profile 49,804, of which plans 15,624 and
`na` 14,972; ranges 34,316; tape 18,105; requestedCountryStatus 11,833). Cheapest cuts if a
page nears the limit: the profile `na`'s per-target engine score paths
(`targets[<id>].engines.<engine>.scores.<metric>.{score,rank,rankedOf}`: 232 of Geonode's
355 paths, 11,271 bytes), which one path per null score would say; and each range's 50
country rows (about 10 KB per range; Geonode's 7d and 30d ranges were identical).

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","kind":"residential","productGroups":40,"network":{"slug":"net00","...":null},
 "days":[],"ranges":{"24h":{"...":null}},
 "profile":{"source":"measured","slug":"net00","name":"Net 00","primaryKind":"residential","score":{"composite":91.4,"rank":1,"...":null,
  "peers":18,"standing":{"rank":1,"peers":18}},
  "protocol":{"targets":["generic_http"]},
  "pool":{"uniqueIps24h":1630,"...":null},"performance":{"latencyP50":1113,"...":null},
  "pricing":{"pricePerGb":4,"pricePerGbAtScale":3,"...":null,"listRate":3,"hasTrial":true,"trialKind":"card"},
  "trust":{"...":null},"targets":[],"ipSamples":[],"incidents":[],"overlap":{"...":null},"tileRanks":{"24h":{"...":null}},"na":[]},
 "stats":{"24h":{"range":{"id":"24h","...":null},"latencyP50":1113,"uniqueIps":1630,"uptimePct":99.9,"...":null,
  "ranks":{"uptimePct":{"rank":2,"peers":18},"datacenterAsnShare":{"rank":4,"peers":18},
   "valueRankAllTargets":{"rank":5,"peers":15},"listRate":{"rank":6,"peers":15}},
  "alternative":{"slug":"net04","name":"Net 04","latencyP50":1210,"cheaperBy":0.31},"claimShare":null}},
 "reviews":[],"communityRuns":[],"changelog":[],"tape":[],"records":{"reviews":0,"communityRuns":0,"changelog":0,"tape":0}}
```

### `/v1/networks/{slug}/chart?kind=&range=`

The histories `windowStats` draws for that category and range: `success`
(`{t, score, status}`; the point's success rate `v` was removed on 2026-11-14, #900. SUCCESS-POINTS-1 (#1301): the
point's Success x/10, computed exactly as the window's `successScore`: the calculator's
rule (SUCCESS-ALL-8-1: the plain mean of the card targets' distances from the best) over
the card target tries of the trailing window ending at that hour or day, the best network the kind's
best in that hour or day, so a steady network's points read its headline; as `/v1/rankings`
`successScoreSpark` (every point since #1301), to 2 decimals. `score` is null unless
`status` is `ranked` or `partial_targets`: `no_ranked_targets` without a card target try in the
trailing window, `not_enough_data` in a snapshot from before it (until #1301 it was the point's rank
among the networks with a point then, on all its requests); "Success scores instead of
success rates"), `availability` (`{t, v}`), `latency`
(`{t, p50, p90, p99}`), `latencyUnderLoad` (`{t, p50, p90, p99}` from
`series[].latency_under_load_ms`, a point per bucket, null values where a
bucket has none or the window's latency is not `generic_http`'s alone),
`pool` (`{t, v}` distinct IPs) and `bands` (outages: `{from, to, severity, country}`,
`from` and `to` fractions of the range, `severity` an `incident_severity` code, `country`
an ISO code or null).
`t` is minutes before the build's as-of time; there is one point per UTC hour
for 24h (at most 24) and one per UTC day for 7d and 30d (at most 7 and 30), the newest
partial: the calculator's series rows, each hour's or day's requests merged, its latency
percentiles from the merged histograms and its distinct IPs counted over it
(SERIES-HOURLY-DAILY-1, #1301; they were 288 five-minute points for 24h and 24-hour slots
from the window's start otherwise). `partialFirst`, `partialLast`, `poolPartialFirst` and
`poolPartialLast` mark an oldest or newest point that covers only part of its hour or day
("Partial first and last points", below). An hour or day without data has no point. There are no point labels (UNREAD-TEXT-1, #977):
a site writes "3h ago" from `t`. For `all` the series is the every-group
fleet entry's (`series[]`: `success_rate`, `latency_ms` of normal requests, distinct
`unique_ips` per bucket, buckets without requests left out); `availability`
`latencyUnderLoad` and `bands` stay empty there, since uptime is a
per-product fact and the chart reads no fleet latency under load; its `success`
points are `{t}` only, without a score.

```json
{"generatedAt":"2026-11-03T09:00:00.000Z","network":"net00","kind":"residential","range":"24h",
 "success":[{"t":1435,"score":7.75,"status":"ranked"}],"availability":[{"t":1435,"v":1}],"latency":[{"t":1435,"p50":1110,"p90":1390,"p99":3290}],
 "pool":[{"t":1435,"v":212}],"bands":[{"from":0.4,"to":0.45,"severity":"major","country":"AD"}],
 "latencyUnderLoad":[{"t":1435,"p50":1450,"p90":1770,"p99":3970}]}
```

### Unique IPs per hour or day: the market curve and the chart pool (FLEET-POOL-SERIES-1)

`/v1/home` `market.uniqueIpsCurve` (`{values, times, partialFirst, partialLast}`) is the fleet's distinct exit
IPs, every IP of every network's counted runs once however many networks saw it (a set union,
the snapshot's every-group fleet entry `*` `ip_slots`), over the whole window:

- **24h: the pool size over the trailing 24 hours at each hour** (FLEET-POOL-SERIES-3, the
  owner's decision of 17 Nov 2026). Each of the 24 points stands for a UTC clock hour and counts
  the distinct IPs of the 24 hours that end at that hour's end; the newest, the current hour,
  ends at the as-of time, so it is the range's own `market.uniqueIps`. A point is not the IPs of
  that hour's tests: each network x country is tested once a day at its scheduled hour, so a
  per-hour count followed the test schedule (production, 16 Nov: 1.2 M at 19:00, 25 k at
  05:00), while every 24 hours hold each country's test once. The line moves when the pool
  grows or shrinks.
- **7d and 30d: one point per UTC day** (7 and 30), the distinct IPs of that day; the newest is
  today, unfinished.

`times` is each slot's start in minutes before the build's as-of time. The last slot is the
hour or day holding the window's last instant: it ends at the as-of time (its start is 10
minutes back in a build 10 minutes into its hour; a build at an hour's or day's start ends with
that whole hour or day); the first starts 23 hours (6, 29 days) before it. These are the UTC
hours and days of the window's series points (`aggregate.series_axis()`, SERIES-HOURLY-DAILY-1)
and of `successScoreCurve` (CURVE-HOURLY-DAILY-1, #1301): the same `times`. `partialLast` is `true` when the last point is
today's unfinished day of a 7d or 30d curve, which holds only the `times[last]` minutes before
the as-of time (a line drawn to it drops at the end: the website marks or leaves it out), and
`false` for 24h (every point counts a whole 24 hours) and for an empty curve
(FLEET-POOL-SERIES-2); `partialFirst` is `true` when the first 7d or 30d point with IPs has only
null points before it, the day the data started (FLEET-POOL-SERIES-4; `false` for 24h). A point whose span has no observed IP is `null` (before the data starts
too), never 0. The points are measured, like `market.uniqueIps` (the same fleet entry's count
over the range): no hour formula, which has no IP sets to take a union of. So a point is at
most the sum of the networks' measured IPs over its span; `market.uniqueIps` is at least any
7d or 30d point (the days of the range add up to at least it: an IP seen on two days counts
once in the range) and equals the newest 24h point (the 24h points before it are the earlier
24-hour windows, so they may be larger or smaller). They are not on the scale of the
networks' chart `pool` points, which are estimates. Empty lists for a snapshot calculated
before it.

What one `pool` point of `/v1/networks/{slug}/chart` is (`{t, v}`, `t` its start; since
SERIES-HOURLY-DAILY-1, #1311):
- 24h, a daily-test group with released hours: one point per UTC clock hour, not rolling and
  not measured: the hour formula's estimate for that hour summed over the network's countries
  (each country's daily test: its measured IPs × that hour's factor ÷ its tested hour's
  factor). An hour is released once it has ended, so the current hour has no point and the
  newest is the last whole hour; an hour without an estimate has no point (never 0). Without
  released hours (census-pulse groups): the measured distinct IPs of each UTC hour, null where
  it has no requests.
- 7d and 30d: one point per UTC day (00:00 to 24:00 UTC), not cumulative and not rolling: the
  group's distinct exit IPs measured in that day (an IP seen on two days counts in both) × the
  window's breakdown scale (its estimate ÷ its measured IPs, "IP numbers from the hour
  formula"), null for a day without requests (before the data starts too); the newest is
  today, unfinished.
- The chart answer's `poolPartialFirst` and `poolPartialLast` mark these ends ("Partial first
  and last points" below). `/v1/networks/all/chart` `pool` is the every-group fleet's measured
  distinct IPs of each UTC hour or day (each IP once), hours and days without requests left out:
  the per-hour market line is `market.uniqueIpsCurve`. Adding the networks' `pool` points counts
  an IP every network saw once per network.

#### Partial first and last points (FLEET-POOL-SERIES-4)

Each chart answer carries four values-only flags (the series keep their shape):
`partialFirst` and `partialLast` for `success`, `availability`, `latency` and `latencyUnderLoad`
(their points are the window's UTC hours or days), `poolPartialFirst` and `poolPartialLast` for
`pool`. A `…Last` flag is `true` when the newest point's hour or day holds the build's as-of time,
so it covers only its `t` minutes (today's day in a 7d chart built at 19:10 covers 1,150 of
1,440 minutes, and the line drops there): the website marks it or leaves it out. A `…First` flag
is `true` when the oldest point with data has only empty points before it in the range, so the
data started (or resumed) during that hour or day (production: Shifter's 14 Nov holds only the
hours from about 17:30). A range whose first point already has data reads `false`, even on the
one day the data's first day is the range's first day: the series has no finer time. The 24h
`pool` of released hours reads `false` for both: each point is a whole hour's estimate (its
first released hour too, even when the data started during that hour), and the current hour has
none. `/v1/networks/all/chart` serves only the fleet's hours or days with requests: its
`…First` flags come from the fleet's series (those without requests count as empty), its
`…Last` flags from the newest point it serves (an empty current day leaves yesterday's whole
point unflagged), the pool's the same. `market.uniqueIpsCurve`
has `partialFirst` and `partialLast` with the same meaning for its 7d and 30d days (`false` for
its rolling 24h points). API-only: no recount.

The calculation counts each closed slot's span once, after the slot closes, and remembers it
with the series' day slices (`calculated_distinct`); the spans ending at the as-of time (the
24h window itself and today) are the IPs last seen since they started
(`calculated_ip_last_seen`, the same read as the range totals). So an hourly build counts one
span of 24 hours (about 3 s on production), and the first build after a UTC midnight one day.

## IP numbers from the hour formula (API-ESTIMATES-1)

A daily test's product group whose windows (24h, 7d, 30d) hold released
estimated hours carries the framework's `estimated` block beside its measured
figures (atlas `estimates/window.py`). The API serves every IP count it
derives from such a window as the hour formula's daily estimate, a plain whole
number (rounded half up) in the same fields as before: no answer has an
`estimated` key, flag or label, and the window's measured distinct IP count is
not served for that window (contract K8, owner decision Q6).

| API value | From the window's `estimated` block |
|---|---|
| A window's unique IPs: provider `pool.uniqueIps24h`/`uniqueIps30d`, window statistics `uniqueIps`, network totals `uniqueIps` and `ipsSum`, the unique-IP tile and its rank, the dataset totals built from them | `unique_ips.value` |
| A place or breakdown row's IPs: `geo[].ips` (and its share), `asns[].ips` (and its share), window statistics `datacenterIps` (and `ranges.<r>`, rankings and home rows), network and country place rows `ipsSum` (country, region, city, ASN), their ranks, `/v1/countries` `ipsSum` | the measured row's IPs × `breakdown_scale` |
| The 24h IP chart (`series.uniqueIps24h`, chart and window `pool`) | one point per released hour (`series[]`, `t` from the hour's start) |
| A 7d/30d daily IP point (`series.uniqueIps30d`, chart and window `pool`, network `days[]`) | the measured day's IPs × `breakdown_scale`, like a place row (hourly figures are not added up: an IP seen in several hours would count once per hour) |

A test's figure is a whole day's unique IPs, so a 24h window counts each
requested country once: its most recent daily test with a released hour in
the window. The previous day's test, whose later hours also reach into the
window, is left out of the 24h figure (UNIQUE-IPS-24H-1, #1117; before, both
counted and the 24h figure could reach about twice a day's). The 7d and 30d
windows count every test, an IP seen on several days once. The 24h IP chart
keeps one point per released hour of either test.

A test counts for, and is scaled by, the hour it was scheduled for (the daily
campaign's slot), not the hour its run started: a catch-up or a country that
waited on its droplet keeps its own hour (ESTIMATE-SCHED-1, #1152). Its day
figure is the measured IPs x the daily factor over that hour's factor, never
over a factor below 0.5, so no test is scaled by more than twice its daily
factor (IPRoyal's catch-up of 2026-11-15 ran at a night hour of 0.14).

The window figure is never more than the window's successful requests (its
`metrics.successes`, the daily tests and pulses of the group: the `successes`
served beside it). A capped window scales its countries' figures by the same
ratio, and its `breakdown_scale` follows, so place rows, tiles and ranks are
capped alike (ESTIMATE-CAP-1, #1152).

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
an event, not a value) with that hour's IPs in `values`: no
answer text names the estimate (owner decision on the API-ESTIMATES-1 review): its
raw kind on a tape row (`event`) is `hour_released`.
A `/v1/events` row has the tape row's shape since #993: `kind` the group (`census`),
`event` the tape's code (`hour_released`), and `event=hour_released` filters them (#984).

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

**Paging back and `more` (EVENTS-BEFORE-1).** `/v1/events` lists rows newest
first by `at`, then `id`. `before=<id>` gives the next `limit` rows below the
row with that id (with every filter, `since` too), newest first, so a client
pages back by passing each page's last row id. A merged row stands where its
group's newest row stands (its `id` is the group's largest, which may stand
lower), and a page never splits a group, so a boundary never repeats or skips a
row. A `before` that names no event answers 400 `unknown_cursor`; `before` with
`after` answers 400 `conflicting_parameters`. `more` is true when matching rows exist below the page
(on a newest-first page: the default, `since`, `kind`, `event`, `before`;
on the default prioritized pick, when a matching row is left out); with
`after` (oldest first) it keeps telling that rows follow `cursor`.

## Payload v4 (network summaries) (API-SUM-1)

A build whose head has `schema_version` 4 and `networks` (atlas calculated
contract K9) is read from its head and its network lists (contract K10) only: no
product part and no group list. The head is read without its `parts` count, its
`overlap` rows (a network's are its windows' `overlap.peers`) and its per-cohort
`fleet` rows; the every-group `fleet` entries (cohort `*`) stay. That is the head's
`api_head`, stored when the build is published (calculated v14); a build published
before v14 has none, and the API builds the same head from its stored one. The API serves
no build before v4: while the current build is older, every data route answers
503 (error `unavailable`, `reason` `payload_before_v4`) and `/healthz` has
`status` `unavailable` and `reason` `payload_before_v4` (README, "Endpoints").

Each network entry is served as one product group of the payload, so every answer
keeps its fields and rules. Per network and proxy kind there is exactly one group:

| Field the build reads | From the network entry |
|---|---|
| `id`, `cohort` | As published: `<provider>:<kind>:network`, `network` (incident ids `outage:<id>:…` carry it) |
| `product_id`, `provider`, `name`, `kind`, `product`, `census`, `pricing`, `outages` | As published (the headline product's) |
| `criteria.country` | The one requested country when `requested_countries` has exactly one, else null |
| A window's scalars, `metrics`, `uptime`, `pool` figures, `targets`, `series`, `scores.balanced`, `success_delta`, `latency_day_delta`, `estimated`, `overlap` shares | As published |
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
- `requestedCountryStatus[]` (COUNTRY-MIN-IPS-1, #1137, #1253) is each requested country's
  `{country, status, uniqueIps}` from the summary's `criteria.country_status`, in
  country order: `status` (code set `country_status`) `counts` when the network's latest
  daily test there found at least the methodology's `country_min_ips` unique IPs (1 since
  #1253, was 10), or when it is the first test under that minimum of a country that had 50 or
  more (two days in a row, LOWAVAIL-1, #1181: so `counts` can come with `uniqueIps` under the
  minimum, even 0), `too_few_ips` when it found fewer, shown as "low availability" (the
  country's data is left out of every figure until a daily test finds the minimum),
  `no_exits` when it found none at all, shown as "no exits" (left out too), `no_daily_test`
  before the first one (left out too); `uniqueIps` that test's unique IPs (null without
  one). Over several groups a country counts when it counts in one (the most IPs of those),
  else it is `too_few_ips` when a group found some IPs, then `no_exits`, then `no_daily_test`;
  the all-networks answer joins every network's, so there a country `counts` when it counts
  for any network. Empty from a build without the methodology's `country_verdicts`;
- The 24h range's places (COUNTRIES-24H-SPAN-1, #1278) follow the latest daily test, as in 7d
  and 30d: a network's country whose latest daily test before the cutoff counts and started in
  the hour before the 24 hours (25 to 24 hours back: the previous test of the cutoff's hour,
  whose next one is imported after the build, about 50 minutes into the hour) keeps that test in
  the 24h range as a whole. Its exit IPs, places and requests are in the 24h `countries`,
  `regions`, `cities`, `asns` (`exitCountries`, `pool.countries` and the rest), the country,
  region, city, ASN and City × ASN rows, the Coverage rows, the requests and request success
  and the success score's plain-web tries (`success_overall`), so a country tested once a day counts in
  24h whenever its latest verdict at the range's end counts (`requestedCountryStatus`
  `counts`). A country whose latest test does not count is not reached back to. Its time slots
  start before the range, so they do not vote on the time-weighted success rate (as any slot
  cut by the range's start); the 5-minute series, the daily averages and the distinct IPs
  counted over several groups (a network's `uniqueIps`, the `*` totals) stay inside the 24
  hours. A network's last day (`days[]`) is its 24h range, with the same tests. 7d and 30d do
  not change;
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

Requests and latency of a place row (MEASURED-FIELDS-1): a country row
(`ranges.<range>.countries[]`, `/places?dimension=country`, country pages) carries the
requests, successes and light-load p50 of the tests that requested that country (daily tests
record their requests per run, not per exit address: the calculator counts a run without
exit-place rows towards its requested country, merged into that country's row) and of the
pulses' rows that exited there. A region, city or ASN row has requests and latency only where
a test records them per place (pulses); for daily tests it has `requests: 0` and a null
`latencyP50`, listed in the page's `na` with `not_measured_per_place`
(api/answers/README.md). A `.../places` page carries `na`, `naReason` and `naReasons` for its
rows (`rows[].latencyP50`); a Coverage row's null `latencyP50` has the
same reason.

On the two test days (the #815 load on `proxy_reviews`) these rows are generated test data
(PLACE-LATENCY-TEST-1, model `place-v1`, scripts/scale/SIMULATE.md "Per-place buckets of the
daily tests"). Each daily test's requests, successes and latency are split over its exit
places in the requested country. So the region, city and ASN rows' `requests`, `successes`
and `latencyP50`, and the Coverage rows' `latencyP50` (`rows[i][7]`), are set where the row
has successes, and null where it has none. The country rows keep their requests and
successes. In production these rows stay as above: `requests: 0`, a null latency,
`not_measured_per_place`.

Latency on the two test days is generated test data too (LATENCY-TRUTH-1, model
`real-median-v1`, scripts/scale/SIMULATE.md "Real latency medians on the stored days"). Each
network's level follows its real successes-only median, so the light-load and under-load
`latencyP50`/`P90`/`P99`, the `speed` breakdown and its rank, and the place, Coverage and tape
latency figures keep the real order of the networks. Exceptions: a network without a real
median keeps a generated level, and networks within about 7 % of each other can swap places
under load, since the generated load runs' factor (1.4) moves by up to ±0.05 per network and
day. Everywhere,
the latency counts successful requests only; a failed request has no latency.

Answers of the network lists (contract K11):

Every page of them that is not a body the build or an LRU holds is cut on request, as are
`/v1/search` and the events answers built on request. At most `--uncached-limit` such answers
(default half the processors, at least 1) are computed at once, and the events fallback's
database reads use at most `--db-requests` connections (default 2). A request that gets neither
within 100 ms answers 503 `busy` with `Retry-After: 1`: retry it then. Cached answers never wait
(SEC-COST-1; the reasons for both defaults are in README, Caching: the guard on uncached work).

| Path | Answer |
|---|---|
| `/v1/networks/{slug}/coverage?kind=&range=&q=&country=&page=` | One page (50 rows) of the network's City × ASN rows with IPs (the headline group's list), most IPs first, ties in list order: `network`, `kind`, `range`, `query`, `page`, `pageSize`, `total`, `pages`, `columns`, `rows`. `columns` names a row's positions (PLACE-CODES-1): `[countryCode, region, city, asn, asnName, usageType, ips, latencyP50, latencyRank, latencyPeers, ipsRank, ipsPeers]` (12 values, below; the country's name went with #992: write it from `countryCode`); read the values by these names. A row is an array of these values: IPs follow K8 (× `breakdown_scale`, rounded half up), `latencyP50` is the row's p50 (null when it has none), the country rank and peers are the provider's `countryLatencyRanks` value of the row's country (null rank, 0 peers when the country has none), and `ipsRank`, `ipsPeers` are the row's own IP rank at its City × ASN, with or without `q`. `q` keeps rows where every word occurs, case-insensitive, in the country code, region, city, `AS<n>` (or the bare ASN number) or ASN name (not the country's name, PLACE-CODES-1); `country` (a two-letter code, any case; 400 otherwise) keeps that country's rows; with `q` or `country` the answer also has `totals` `{ips, countries, cities, asns}` over every matching row (IPs summed as the rows show them, distinct country codes, cities and ASN numbers). |
| `/v1/countries/{cc}/regions?range=&q=&page=` | One page of the country's regions across networks, in the shape and rules of `/cities`. |
| `/v1/places/region/{key}?range=` and `/v1/places/city/{key}?range=` | `generatedAt`, `dimension`, `key`, `range` and `rows`: every network's row in that region or city (the place key of the place lists, an opaque id: pass it URL-escaped as a list row gives it, never parse it), in list order, with `ipsSumRank` and `latencyRank` among the networks of the same kind, as a country answer ranks them. 404 when no network has that key in that range, known from the place keys the build gathers, without reading a row (SEC-COST-1). |
| `/v1/networks/{slug}/places?kind=&dimension=country\|region\|city\|asn&range=&q=&country=&page=` | `q` keeps rows whose code, region, city or ASN name or `AS<number>` contain every word (not the country's name); `country` (a two-letter code, any case; 400 otherwise) keeps that country's rows (an ASN row has none, so none is kept). Each row also has `ipsRank: {rank, peers}` (COVERAGE-IPS-RANK-1), next to its `successScore`: the network's IP rank at that place, from the same stored values as a Coverage row's `ipsRank` and `ipsPeers` (below). The key is absent where the build has no stored rank: a list written before calculated v15, a row without IPs, a list merged from several network lists (the all-networks lists), a country's lists and the rows that have `ipsSumRank` instead (`/v1/countries/{cc}`, `/v1/places/{dim}/{key}`). It is also on the network answer's `ranges.<r>.countries[]`, which are the first rows of its country list. The dataset file's place rows do not carry it. |

A City × ASN row's 12 values, by position:

| # | Name | Meaning |
|---|---|---|
| 0 | `countryCode` | The place's country code. |
| 1 | `region` | The region (state) name. |
| 2 | `city` | The city name. |
| 3 | `asn` | The ASN number (null when unknown). |
| 4 | `asnName` | The ASN's name. |
| 5 | `usageType` | The ASN's usage type (below). |
| 6 | `ips` | The network's distinct exit IPs there (K8: × `breakdown_scale`). |
| 7 | `latencyP50` | The row's latency p50 in ms (null when none). |
| 8 | `latencyRank` | The row's **country's** latency rank (below). |
| 9 | `latencyPeers` | The networks ranked in that country (below). |
| 10 | `ipsRank` | This network's place by distinct exit IPs among the networks seen at the same country, region, city and ASN, in the same kind and range: 1 = most IPs. |
| 11 | `ipsPeers` | The number of networks with IPs at that place, this one included. |

`ipsRank` follows `rankOf` (`api/internal/build/ranks.go`): 1 + the number of
networks with strictly more IPs, so ties share the better rank (500, 500 and
100 IPs rank 1, 1 and 3, all of 3 peers), the same rule as `/v1/places`'
`ipsSumRank`. A place this network alone is seen at is 1 of 1, which is
truthful (a page may hide a rank of 1 of 1). The calculation computes both once
per build when it writes the network lists (calculated v15, `ips_rank` and
`ips_peers` of `calculated_list_rows`, `calculated/ips_rank.sql`: SQL
`rank()` and `count(*)` over the build's network lists of the same kind and
window, by the stored, unscaled IPs). A place is the API's place key
(`placeKeyOf`: country code, region code, region, city, ASN number), not one
stored label: a network's rows of one place (an ASN under two usage types or
names, a country under two names) are summed first, and each of them carries
the place's rank. So the API does no ranking for them. Every Coverage row has IPs, so both
are set on a v15 build; on a list written before v15 both are null.

In a City × ASN row (NET-COUNTRY-RANK-NOTE, NET-USAGE-LABEL):

- `latencyRank` and `latencyPeers` are the row's **country's**
  latency rank among the networks of the same kind, cohort and period, the
  same for every row of that country; they are not a rank of the City × ASN
  row itself. A page that prints the rank under the row's latency should say
  it is the country's.
- `usageType` is the IP database usage type of the ASN as enrichment stored it
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
  list, `{"total": <rows>, "pages": <pages>, "columns": [<name>, ...], "rows": [<row>, ...]}`:
  `total` and `pages` are the list answer's own (`pages` = `total` ÷ 50, rounded
  up, 0 for an empty list), `columns` the names of a row's positions (codes of
  `place_column`; a scored place row adds `successScore`) and `rows` its first
  page (at most 50 rows), each row an array without the country's name
  (PLACE-CODES-1):

| Dimension | Row | List |
|---|---|---|
| `country` | `[countryCode, ipsSum, requests, successes, latencyP50, successScore]` | `/v1/networks/{slug}/places?kind=&dimension=&range=` without `q` |
| `region` | `[countryCode, regionCode, region, ipsSum, requests, successes, latencyP50, successScore]` | The same |
| `city` | `[countryCode, regionCode, region, city, ipsSum, requests, successes, latencyP50, successScore]` | The same |
| `asn` | `[asn, asnName, ipsSum, requests, successes, latencyP50, successScore]` | The same |
| `city_asn` | `[countryCode, region, city, asn, asnName, usageType, ips, latencyP50, latencyRank, latencyPeers, ipsRank, ipsPeers]` | `/v1/networks/{slug}/coverage?kind=&range=` without `q`: the City × ASN rows with IPs of the network's headline list, most IPs first (ties in list order) |

A place row is the answer's row (merged across the network kind's lists, in
the answer's order: most IPs first, then key) with the fields the place rows of
`/places` show, in that answer's key order, without the derived `key` and without the fields a dimension never has. Its `countryCode`
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

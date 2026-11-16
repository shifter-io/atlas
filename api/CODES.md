# Codes

Written from the codes registry (`api/internal/build/codes_*.go`) by `TestCodesDocIsCurrent`;
do not edit it by hand: change the registry and run that test with `GOLDEN_WRITE=1`.

Every code the API sends, where it appears, its parameters and one English sentence for
documentation only. `GET /v1/codes` serves the same list, with the catalog sets' codes of the
current build, so a site in any language can translate every code. Contract: **2026-11-16**, the
date of the last breaking change (`/v1/version` and `/v1/codes` `contract`;
what changed on each date: `api/CHANGES.md`).

## Conventions

- An answer holds only ids, slugs, codes, raw numbers, ISO 8601 times, ISO country codes, URLs,
  booleans and null: never a sentence, a written-out number, a plural or a relative time.
- Names stay names: networks, the provider's own plan, tier and pool names, place and
  network-operator names, and what users wrote in their reviews.
- Keys are camelCase; codes are lowercase snake_case; ids and slugs of catalog rows may be
  kebab-case (`cost-first`).
- Every field is present; a missing value is null, listed in its entity's `na` with a reason
  code (`naReason`, `naReasons`).
- `*Rate` and `*Share` are ratios from 0 to 1, `*Pct` percents from 0 to 100, `*Pts`
  percentage points; new fields use ratios.
- A duration carries its unit in its name; the series times `t`, `trendT` and `times` are minutes
  before `generatedAt`.
- Money is a plain number in US dollars (`currency` in `/v1/codes`).
- Times are RFC 3339 in UTC with `Z` and at most milliseconds; dates without a time are `YYYY-MM-DD`.
- A message is `{code, params}` (an error is `{"error": {code, params}}`); a record (a tape
  row, an event, a changelog entry) is `kind` (its group), `event` (its code) and
  `values` (its parameters).

## How to read a set

`Used in` lists the fields that hold the set's codes: a route (`*` for every answer and
the dataset file), then a path in the body. `[]` is every element of a list, `<id>` any key
of an object, `<key>` the keys themselves, and `**` any depth (never inside `naReasons`, whose keys are
paths, or the N/A report's `sections`, whose keys are section names). A parameter's type is one
of `number`, `integer`, `ratio`, `points`, `ms`, `seconds`, `minutes`, `usd`, `usd_per_gb`, `gb`,
`mb`, `count`, `country`, `country_list`, `code:<set>`, `timestamp`, `date`, `name`,
`string`, `string_list`, `path` (an error's echo of the request); its unit,
when the type does not say it, is a code of the `unit` set. A catalog set's codes are the
rows of the build's catalog, so only `GET /v1/codes` lists them.

## Sets

### `asn_kind`

Used in: `* **.asns[].kind`

| Code | Parameters | Meaning |
|---|---|---|
| `eyeball` |  | A consumer network. |
| `hosting` |  | A hosting network. |
| `mobile` |  | A mobile network. |
| `business` |  | A business network. |

### `bill_unit`

Used in: `/v1/home insights.valueAll.unit`

| Code | Parameters | Meaning |
|---|---|---|
| `usd_per_1k_successes` |  | US dollars per 1,000 successful requests. |

### `bill_weighting`

Used in: `/v1/home insights.valueAll.weighting`

| Code | Parameters | Meaning |
|---|---|---|
| `equal` |  | Every target counts the same. |

### `billing`

Used in: `* **.pricing.plans[].catalog.billing`

| Code | Parameters | Meaning |
|---|---|---|
| `monthly` |  | Billed every month. |
| `payg` |  | Paid as you go, once. |
| `30_day` |  | Valid for 30 days. |
| `annual` |  | Billed every year. |

### `change_event`

Used in: `/v1/changelog entries[].event`, `/v1/networks/{slug} changelog[].event`

| Code | Parameters | Meaning |
|---|---|---|
| `success_rose` | `metric` (code:change_metric), `value` (ratio), `previous` (ratio), `delta` (number, points) | Success rose by {delta} points. |
| `success_fell` | `metric` (code:change_metric), `value` (ratio), `previous` (ratio), `delta` (number, points) | Success fell by {delta} points. |
| `latency_rose` | `metric` (code:change_metric), `value` (ms), `previous` (ms), `delta` (number, percent) | Latency rose by {delta}. |
| `latency_fell` | `metric` (code:change_metric), `value` (ms), `previous` (ms), `delta` (number, percent) | Latency fell by {delta}. |
| `pool_grew` | `uniqueIps` (count), `previousUniqueIps` (count), `deltaPct` (number, percent) | The pool grew by {deltaPct}. |
| `pool_shrank` | `uniqueIps` (count), `previousUniqueIps` (count), `deltaPct` (number, percent) | The pool shrank by {deltaPct}. |
| `countries_lost` | `countries` (count), `previousCountries` (count), `delta` (integer, count), `lostCountryCodes` (country_list), `comparedRange` (code:range) | {delta} countries lost: not seen in the last {comparedRange}: {lostCountryCodes}. |
| `geo_accuracy_rose` | `value` (ratio), `previous` (ratio), `delta` (number, points) | Location accuracy rose by {delta} points. |
| `geo_accuracy_fell` | `value` (ratio), `previous` (ratio), `delta` (number, points) | Location accuracy fell by {delta} points. |
| `price_raised` | `fromRate` (usd_per_gb), `toRate` (usd_per_gb), `deltaPct` (number, percent), `plan` (name) | The list price rose by {deltaPct}. |
| `price_cut` | `fromRate` (usd_per_gb), `toRate` (usd_per_gb), `deltaPct` (number, percent), `plan` (name) | The list price fell by {deltaPct}. |
| `incident` |  | An outage: its facts are in the entry's incident block. |
| `catalog` |  | A hand-written entry: its kind's values keys, each null. |

### `change_kind`

Used in: `/v1/changelog entries[].kind`, `/v1/networks/{slug} changelog[].kind`

| Code | Parameters | Meaning |
|---|---|---|
| `incident` |  | An outage. |
| `price` |  | A list price changed. |
| `pool` |  | The pool grew or shrank. |
| `performance` |  | Success or latency moved. |
| `authenticity` |  | Location accuracy moved. |
| `coverage` |  | Countries were lost. |
| `methodology` |  | The methodology changed. |

### `change_metric`

Used in: `/v1/changelog entries[].values.metric`, `/v1/networks/{slug} changelog[].values.metric`

| Code | Parameters | Meaning |
|---|---|---|
| `success` |  | The success rate. |
| `latency` |  | The latency. |

### `change_severity`

Used in: `/v1/changelog entries[].severity`, `/v1/networks/{slug} changelog[].severity`

| Code | Parameters | Meaning |
|---|---|---|
| `info` |  | Neither better nor worse. |
| `good` |  | A change for the better. |
| `bad` |  | A change for the worse. |

### `country_status`

Used in: `/v1/networks/{slug} requestedCountryStatus[].status`

| Code | Parameters | Meaning |
|---|---|---|
| `counts` |  | Its latest daily test there found 10 or more unique IPs, or it is the first test under 10 of a country that had 50 or more: the country counts. |
| `too_few_ips` |  | Low availability: its latest daily test there found fewer than 10 unique IPs (the network has almost no exits there): the country's data is left out. |
| `no_exits` |  | No exits: its latest daily test there found no unique IP at all: the country's data is left out. |
| `no_daily_test` |  | No daily test there yet: the country's data is left out. |

### `discount_source`

Used in: `* **.pricing.plans[].catalog.discountSource`

| Code | Parameters | Meaning |
|---|---|---|
| `sale` |  | A sale. |
| `volume` |  | A volume discount. |
| `annual_billing` |  | Paying for a year. |
| `auto_coupon` |  | A coupon the shop applies by itself. |
| `subscription` |  | Subscribing instead of paying once. |

### `duplication_basis`

Used in: `/v1/overlap market.basis`, `/v1/home market.duplication.basis`

| Code | Parameters | Meaning |
|---|---|---|
| `kind_fleet` |  | The kind's networks together. |

### `engine`

Used in: `* **.targets[].engine`, `/v1/targets engine`, `/v1/targets networks[].cells.<id>.engine`, `/v1/targets networks[].cells.<id>.otherEngines[]`, `* **.browserOverall.<key>`, `* **.engines.<key>`, `/v1/home insights.hardest.engine`

| Code | Parameters | Meaning |
|---|---|---|
| `http` |  | Plain HTTP requests, no browser. |
| `chrome` |  | A Chrome browser. |
| `camoufox` |  | A Camoufox (Firefox) browser. |
| `browser` |  | Any browser engine. |

### `error`

Used in: `* error.code`

| Code | Parameters | Meaning |
|---|---|---|
| `not_found` | `path` (path) | Nothing is at {path}. |
| `method_not_allowed` | `method` (string) | The method {method} is not served: use GET or HEAD. |
| `unknown_parameter` | `name` (string) | The route takes no parameter {name}. |
| `invalid_parameter` | `name` (string), `value` (string), `allowed` (string_list), `format` (code:error_format) | {value} is no valid {name}: one of {allowed}, or a value in {format}. |
| `out_of_range` | `name` (string), `value` (integer), `min` (integer), `max` (integer) | {name} must be between {min} and {max}, not {value}. |
| `too_long` | `name` (string), `max` (integer) | {name} is longer than {max} bytes. |
| `too_many_values` | `name` (string), `max` (integer) | {name} takes at most {max} values. |
| `unknown_cursor` | `name` (string), `value` (string) | The cursor {name}={value} names no event. |
| `conflicting_parameters` | `names` (string_list) | The parameters {names} cannot be combined. |
| `unavailable` | `reason` (code:unavailable_reason) | Not served now: {reason}. |
| `busy` |  | The server is busy computing other answers; try again after Retry-After seconds. |
| `rate_limited` |  | Too many answers computed on request from one address; try again after Retry-After seconds. |
| `internal_error` |  | The answer could not be read; try again. |

### `error_format`

Used in: `* error.params.format`

| Code | Parameters | Meaning |
|---|---|---|
| `country` |  | A two-letter country code. |
| `event_id` |  | An event id: a whole number from 0. |
| `positive_integer` |  | A whole number. |
| `iso_time` |  | An ISO 8601 time or date. |
| `slug` |  | A network slug: lowercase letters, digits and hyphens. |
| `kind` |  | A kind: lowercase letters, digits and underscores. |
| `text` |  | Text in UTF-8 without control characters, in a query that parses. |

### `event`

Used in: `* **.tape[].event`, `/v1/events events[].event`

| Code | Parameters | Meaning |
|---|---|---|
| `daily_test_started` | `countries` (count), `value` (count) | A daily test started in {countries} countries. |
| `daily_test_finished` | `countries` (count), `requests` (count), `uniqueIps` (count), `durationSeconds` (seconds), `value` (count) | A daily test finished: {requests} requests, {uniqueIps} addresses in {durationSeconds}. |
| `census_started` |  | A census started. |
| `census_finished` | `requests` (count), `uniqueIps` (count), `durationSeconds` (seconds), `value` (count) | A census finished: {requests} requests, {uniqueIps} addresses in {durationSeconds}. |
| `census_retry` |  | A census is tried again. |
| `census_skipped` |  | A census was skipped. |
| `hour_released` | `hour` (timestamp), `uniqueIps` (count), `countries` (count), `value` (count) | An hour's figures were released: {uniqueIps} addresses in {countries} countries. |
| `pulse_finished` | `requests` (count), `successes` (count), `p50` (ms), `challenges` (count), `blocks` (count), `previousP50` (ms), `previousSuccesses` (count), `previousRequests` (count) | A pulse test finished: {successes} of {requests} succeeded. |
| `pulse_down` | `requests` (count), `errors` (count), `windowMinutes` (minutes), `regions` (count) | A pulse failed: {errors} of {requests} requests in {windowMinutes} over {regions} regions. |
| `pulse_recovered` | `durationSeconds` (seconds) | Pulses succeed again after {durationSeconds}. |
| `down` | `requests` (count), `successes` (count), `p50` (ms) | The network is down: {successes} of {requests} requests succeeded. |
| `recovered` | `requests` (count), `successes` (count), `p50` (ms), `durationSeconds` (seconds) | The network recovered after {durationSeconds}. |
| `success_drop` | `requests` (count), `successes` (count), `p50` (ms) | Success fell: {successes} of {requests} requests succeeded. |
| `latency_spike` | `requests` (count), `successes` (count), `p50` (ms), `baseline` (ms), `value` (ms) | Latency spiked to {p50} against a baseline of {baseline}. |
| `latency_rise` | `p50` (ms), `baseline` (ms), `value` (ms) | Latency rose to {p50} against a baseline of {baseline}. |
| `block_spike` | `requests` (count), `successes` (count), `p50` (ms), `blockShare` (ratio), `baselineBlockShare` (ratio), `value` (ratio) | Blocks rose to {blockShare} against a baseline of {baselineBlockShare}. |
| `ip_overlap` | `share` (ratio), `sharedIps` (count), `uniqueIps` (count), `peers` (count), `runs` (count), `value` (ratio), `hour` (timestamp) | {sharedIps} of {uniqueIps} addresses are shared with {peers} networks. |
| `new_hosting_asn` | `asn` (integer), `value` (count) | A new hosting network {asn} appeared among the exits. |
| `node_offline` |  | A test node went offline. |
| `no_node` |  | No test node was available. |

### `event_group`

Used in: `* **.tape[].kind`, `/v1/events events[].kind`, `/v1/events/stats totals.byCategory.<key>`, `/v1/events/stats buckets[].byCategory.<key>`, `/v1/events/stats heatmap[].byCategory.<key>`

| Code | Parameters | Meaning |
|---|---|---|
| `census` |  | The daily tests, censuses and hourly figures. |
| `pulse` |  | The pulse tests. |
| `incident` |  | Outages and sudden changes. |
| `audit` |  | Pool audits: shared addresses and new hosting networks. |

### `event_severity`

Used in: `* **.tape[].severity`, `/v1/events events[].severity`

| Code | Parameters | Meaning |
|---|---|---|
| `info` |  | Routine. |
| `minor` |  | Worth a look. |
| `major` |  | Serious. |
| `critical` |  | The network is down. |

### `geo_coverage`

Used in: `* **.scopes[].geoCoverage.code`

| Code | Parameters | Meaning |
|---|---|---|
| `all_usable_countries` |  | Every country the network can serve. |
| `provider_inventory` |  | The countries the provider lists in its inventory. |
| `restricted` | `countries` (count) | A fixed set of {countries} countries. |

### `grade`

Used in: `* **.grade`, `/v1/meta grades[].grade`

| Code | Parameters | Meaning |
|---|---|---|
| `A+` | `min` (number, score_0_100) | Grade A+: a score of at least {min}. |
| `A` | `min` (number, score_0_100) | Grade A: a score of at least {min}. |
| `B+` | `min` (number, score_0_100) | Grade B+: a score of at least {min}. |
| `B` | `min` (number, score_0_100) | Grade B: a score of at least {min}. |
| `C+` | `min` (number, score_0_100) | Grade C+: a score of at least {min}. |
| `C` | `min` (number, score_0_100) | Grade C: a score of at least {min}. |
| `D` | `min` (number, score_0_100) | Grade D: a score of at least {min}. |

### `health_status`

Used in: `/healthz status`

| Code | Parameters | Meaning |
|---|---|---|
| `ok` |  | Serving. |
| `degraded` |  | Serving, with a problem. |
| `starting` |  | Not serving yet. |
| `unavailable` |  | Not serving. |

### `incident_detected_by`

Used in: `/v1/changelog entries[].incident.detectedBy`, `/v1/networks/{slug} changelog[].incident.detectedBy`, `* **.incidents[].detectedBy`

| Code | Parameters | Meaning |
|---|---|---|
| `pulse` |  | The pulse tests. |
| `census` |  | A census. |

### `incident_kind`

Used in: `/v1/changelog entries[].incident.kind`, `/v1/networks/{slug} changelog[].incident.kind`, `* **.incidents[].kind`

| Code | Parameters | Meaning |
|---|---|---|
| `down` |  | The network failed its tests. |
| `census_stall` |  | A census stalled. |

### `incident_severity`

Used in: `/v1/changelog entries[].incident.severity`, `/v1/networks/{slug} changelog[].incident.severity`, `* **.incidents[].severity`, `* **.bands[].severity`

| Code | Parameters | Meaning |
|---|---|---|
| `minor` |  | Short and narrow. |
| `moderate` |  | Between minor and major. |
| `major` |  | Long or wide. |

### `na_reason`

Used in: `* **.naReason`, `* **.naReasons.<id>`, `/v1/coverage fields.<id>.reason`, `/v1/coverage/{slug} sections.<id>[].reason`, `/v1/coverage dataset.sections.fields[].reason`, `/v1/coverage targets[].sections.fields[].reason`, `/v1/coverage scopes[].sections.fields[].reason`, `/v1/coverage profiles[].sections.fields[].reason`

| Code | Parameters | Meaning |
|---|---|---|
| `catalog` |  | The catalog has no row for it. |
| `not_measured` |  | Not measured in production's cadence. |
| `not_applicable` |  | It has no meaning for this row. |
| `history_covers_range` |  | The history covers the whole range. |
| `not_published` |  | Never published. |
| `not_measured_per_place` |  | Not measured per place. |
| `no_run_in_window` |  | Measured, but no run in this window. |
| `few_successes` |  | Too few light-load successes for a latency. |
| `no_successes` |  | Tries but no success. |
| `incomplete_targets` |  | A target of the bill has no cost. |
| `no_cheaper_peer` |  | No cheaper network of the kind with a success rate close enough. |
| `no_pulse_fleet` |  | No pulse batch in the published snapshot. |
| `not_named_by_provider` |  | The provider names no plan or pool. |
| `methodology_file_unknown` |  | The API holds no rules file for this methodology id. |
| `no_evidence` |  | No monitoring evidence in the window. |
| `no_score_to_compare` |  | No earlier score to compare with. |
| `no_pulse_tests` |  | Too few finished pulse tests. |
| `no_load_test` |  | No load test in the window. |
| `needs_two_days` |  | The 24h latency change needs two whole days of daily tests. |
| `day_not_covered` |  | A day the 24h latency change compares has too few tested countries or successes. |
| `required_target_missing` |  | A required target has no ready result. |
| `no_price_evidence` |  | No verified, dated price per GB. |
| `few_requests` |  | Fewer requests than the methodology requires. |
| `low_time_coverage` |  | Its requests cover too little of the window. |
| `expired` |  | Its measurements are too old. |
| `insufficient_data` |  | Not enough data for another stated reason. |
| `not_enough_data` |  | A place's score: too few tries to score. |
| `too_few_networks` |  | A place's score: too few networks to rank. |

### `na_source`

Used in: `/v1/coverage fields.<id>.source`, `/v1/coverage/{slug} sections.<id>[].source`, `/v1/coverage dataset.sections.fields[].source`, `/v1/coverage targets[].sections.fields[].source`, `/v1/coverage scopes[].sections.fields[].source`, `/v1/coverage profiles[].sections.fields[].source`

| Code | Parameters | Meaning |
|---|---|---|
| `snapshot` |  | The framework's published calculation. |
| `site.providers.hq_country` |  | The catalog's country of the network's headquarters. |
| `site.providers.hq_city` |  | The catalog's city of the network's headquarters. |
| `site.providers.use_cases` |  | The catalog's use cases of the network. |
| `site.providers.founded` |  | The catalog's founding year of the network. |
| `site.providers.website` |  | The catalog's website of the network. |
| `site.pricing` |  | The catalog's pricing row of the network and kind. |
| `site.pricing.price_per_gb` |  | The catalog's entry list rate per GB. |
| `site.pricing.price_per_gb_at_scale` |  | The catalog's lowest list rate per GB. |
| `site.pricing.min_commit` |  | The catalog's minimum commitment. |
| `site.pricing.trial` |  | The catalog's trial. |
| `site.pricing.trial_price` |  | The catalog's price of the trial. |
| `site.pricing.trial_credit` |  | The catalog's credit the trial gives. |
| `site.pricing.trial_gb` |  | The catalog's traffic of the trial. |
| `site.pricing.trial_days` |  | The catalog's length of the trial. |
| `site.pricing.trial_needs_card` |  | The catalog's card requirement of the trial. |
| `site.pricing_plans` |  | The catalog's plans. |
| `site.pricing_history` |  | The catalog's price history. |
| `site.trust_signals.claimed_pool_size` |  | The network's own claim of its pool size. |
| `site.trust_signals.blacklist_hit_rate` |  | The catalog's blocklist hit rate. |
| `site.trust_signals.stickiness_hold_rate` |  | The catalog's sticky session hold rate. |
| `site.trust_signals.leak_checks` |  | The catalog's leak checks. |
| `site.trust_signals.kyc_verified` |  | The catalog's identity check of customers. |
| `site.trust_signals.consent_disclosed` |  | The catalog's disclosure of exit owners' consent. |
| `site.reviews` |  | The published reviews. |
| `site.targets.category` |  | The catalog's category of the target. |
| `site.targets.difficulty` |  | The catalog's difficulty of the target. |
| `site.score_weights` |  | The catalog's score weights. |
| `site.scopes` |  | The catalog's test scope row. |
| `public.targets` |  | The framework's target list. |
| `public.methodologies` |  | The framework's methodology rules, by id. |

### `outcome`

Used in: `* **.outcomes.<key>`

| Code | Parameters | Meaning |
|---|---|---|
| `challenge` |  | A challenge page (a captcha) stood in the way. |
| `block` |  | The site blocked the request. |
| `loginWall` |  | The site asked to log in. |
| `timeout` |  | The request took too long. |
| `proxyError` |  | The proxy failed the request. |
| `noContent` |  | The page loaded without the expected content. |
| `noAnswer` |  | The AI answer page gave no answer. |
| `otherError` |  | Any other error. |

### `overlap_label`

Used in: `/v1/overlap networks[].label`

| Code | Parameters | Meaning |
|---|---|---|
| `fully_exclusive` |  | No address shared with another network. |
| `mostly_exclusive` |  | Few addresses shared. |
| `mixed` |  | Many addresses shared. |
| `resale` |  | Most addresses shared: likely a reseller. |

### `place_column`

Used in: `/v1/networks/{slug}/coverage columns[]`, `/v1/dataset lists.<id>.<id>.<id>.<id>.columns[]`

| Code | Parameters | Meaning |
|---|---|---|
| `countryCode` |  | The place's ISO 3166-1 country code. |
| `regionCode` |  | The region's code within its country: with countryCode, the ISO 3166-2 code (US and NY: US-NY). |
| `region` |  | The region's name, as the location source writes it. |
| `city` |  | The city's name, as the location source writes it. |
| `asn` |  | The network operator's AS number. |
| `asnName` |  | The network operator's name, as the location source writes it. |
| `usageType` |  | The network operator's usage type, as the location source writes it. |
| `ips` |  | The network's distinct exit addresses there. |
| `ipsSum` |  | The network's distinct exit addresses there, summed over its product groups. |
| `requests` |  | The completed requests there. |
| `successes` |  | The successful requests there. |
| `latencyP50` |  | The median latency there, in ms. |
| `latencyRank` |  | The network's latency rank in the row's country among the networks of its kind. |
| `latencyPeers` |  | The networks ranked in the row's country. |
| `ipsRank` |  | The network's rank by distinct exit addresses at the place among the networks of its kind. |
| `ipsPeers` |  | The networks with addresses at the place. |
| `successScore` |  | The row's success score: [status], or [status, score, rank, rankedOf] when ranked. |

### `place_dimension`

Used in: `/v1/networks/{slug}/places dimension`, `/v1/dataset lists.<id>.<id>.<id>.<key>`

| Code | Parameters | Meaning |
|---|---|---|
| `country` |  | Countries. |
| `region` |  | Regions. |
| `city` |  | Cities. |
| `asn` |  | Networks (ASNs). |
| `city_asn` |  | Cities by network. |

### `plan_audience`

Used in: `* **.pricing.plans[].audience`

| Code | Parameters | Meaning |
|---|---|---|
| `all` |  | Every buyer. |
| `new_customers` |  | New customers only. |

### `pool_basis`

Used in: `/v1/overlap networks[].poolBasis`

| Code | Parameters | Meaning |
|---|---|---|
| `overlap_ips_ours` |  | The pool the overlap comparison measured. |
| `window_unique_ips` |  | The window's unique addresses. |

### `price_basis`

Used in: `* **.priceBasis`

| Code | Parameters | Meaning |
|---|---|---|
| `first_200gb_plus` |  | The first plan of 200 GB or more. |
| `largest_below_200gb` |  | The largest plan below 200 GB. |

### `pricing_model`

Used in: `* **.pricing.model`

| Code | Parameters | Meaning |
|---|---|---|
| `per_gb` |  | Sold by the gigabyte. |
| `per_ip` |  | Sold by the address. |
| `per_port` |  | Sold by the port. |
| `per_request` |  | Sold by the request. |

### `product_kind`

Used in: `* **.scopes[].id`, `* **.measuredProduct`, `* **.products[].kind`, `* **.communityRuns[].product`, `/v1/dataset providers[].productMeasurements.<id>.product`, `/v1/networks/{slug}/runs rows[].product`

From the build's catalog (site.scopes: label and summary): its codes are listed in `GET /v1/codes`.

### `proxy_kind`

Used in: `* **.kinds[]`, `* **.primaryKind`, `* **.networks[].kind`, `* **.tape[].product`, `* **.framework.products[]`, `/v1/dataset providers[].productMeasurements.<key>`, `/v1/dataset providers[].productMeasurements.<id>.kind`, `/v1/targets kind`, `/v1/overlap kind`, `/v1/networks/{slug}/runs kind`, `/v1/countries/{cc} ranges.<id>[].kind`, `* **.pricing.plans[].catalog.poolKinds[]`

| Code | Parameters | Meaning |
|---|---|---|
| `residential` |  | Residential proxies: home connections. |
| `datacenter` |  | Datacenter proxies: server addresses. |
| `mobile` |  | Mobile proxies: cellular connections. |
| `isp` |  | ISP proxies: static addresses registered to a consumer network. |

### `proxy_kind_or_all`

Used in: `/v1/networks/{slug} kind`, `/v1/networks/{slug} network.kind`, `/v1/networks/{slug}/chart kind`

| Code | Parameters | Meaning |
|---|---|---|
| `residential` |  | Residential proxies: home connections. |
| `datacenter` |  | Datacenter proxies: server addresses. |
| `mobile` |  | Mobile proxies: cellular connections. |
| `isp` |  | ISP proxies: static addresses registered to a consumer network. |
| `all` |  | Every kind together: the all-networks answer. |

### `range`

Used in: `* range`, `* **.range.id`, `/v1/meta ranges[].id`, `/v1/networks/{slug} ranges.<key>`, `/v1/networks/{slug} stats.<key>`, `/v1/countries/{cc} ranges.<key>`, `/v1/changelog entries[].values.comparedRange`, `/v1/networks/{slug} changelog[].values.comparedRange`

| Code | Parameters | Meaning |
|---|---|---|
| `24h` |  | The last 24 hours. |
| `7d` |  | The last 7 days. |
| `30d` |  | The last 30 days. |

### `ranking_profile`

Used in: `/v1/meta profiles[].id`, `/v1/rankings profile`, `/v1/rankings profiles[].id`

From the build's catalog (site.ranking_profiles: label and description): its codes are listed in `GET /v1/codes`.

### `review_verification`

Used in: `* **.reviews[].verification`, `/v1/networks/{slug}/reviews rows[].verification`

| Code | Parameters | Meaning |
|---|---|---|
| `invoice_verified` |  | Verified by an invoice. |
| `run_verified` |  | Verified by a community run. |
| `unverified` |  | Not verified. |
| `pending` |  | In review. |

### `run_rejection_reason`

Used in: `* **.communityRuns[].rejectionReason`, `/v1/networks/{slug}/runs rows[].rejectionReason`

| Code | Parameters | Meaning |
|---|---|---|
| `too_few_requests` |  | Too few requests. |
| `collector_outdated` |  | The collector was out of date. |
| `duplicate` |  | A copy of another run. |
| `inconsistent_results` |  | Results that disagree with each other. |
| `other` |  | Another reason. |

### `run_status`

Used in: `* **.communityRuns[].status`, `/v1/networks/{slug}/runs rows[].status`

| Code | Parameters | Meaning |
|---|---|---|
| `accepted` |  | Accepted. |
| `pending` |  | In review. |
| `rejected` |  | Rejected. |

### `score_basis`

Used in: `* **.successScore.basis`

| Code | Parameters | Meaning |
|---|---|---|
| `daily_tests` |  | The network's daily tests' success by requests, scored by its distance from the best network of its kind. |
| `median_success_rate` |  | The median network's success rate. |
| `mean_success_rate` |  | The mean success rate of the networks. |
| `success_rate` |  | The success rate of the tests themselves. |

### `score_component`

Used in: `* **.breakdown.<key>`, `/v1/meta weights.<key>`, `/v1/meta profiles[].weights.<key>`, `/v1/rankings weights.<key>`, `/v1/rankings profiles[].weights.<key>`

| Code | Parameters | Meaning |
|---|---|---|
| `reliability` |  | Success on the required targets. |
| `speed` |  | Latency of normal requests (the pulses). |
| `uptime` |  | Availability in the pulse tests. |
| `coverage` |  | Countries served. |
| `diversity` |  | Spread of the exit addresses over networks. |
| `dcAsns` |  | Few datacenter networks among the exits. |
| `authenticity` |  | Exits where the network says they are. |
| `value` |  | Price per GB. |

### `score_status`

Used in: `* **.successScore.status`, `* **.scores.<id>.status`, `* **.statuses[]`, `* **.browserOverall.<id>.success.status`, `/v1/targets targets[].scoreStatus.<id>.status`, `/v1/networks/{slug}/chart success[].status`, `/v1/home market.mostReliable.status`

| Code | Parameters | Meaning |
|---|---|---|
| `ranked` |  | Scored and ranked among the networks of its kind. |
| `not_enough_data` |  | Too few tries to score. |
| `too_few_networks` |  | Too few networks of the kind to rank. |
| `no_ranked_targets` |  | None of its targets is ranked. |

### `search_item_type`

Used in: `/v1/search items[].type`

| Code | Parameters | Meaning |
|---|---|---|
| `network` |  | A network: the id is its slug. |
| `target` |  | A test target: the id is its target id. |

### `source`

Used in: `* **.tape[].source`, `* **.targets[].source`, `* **.networks[].source`, `* **.communityRuns[].source`, `* **.providers[].source`, `* **.provider.source`, `* network.source`, `* profile.source`, `/v1/changelog entries[].source`, `* **.changelog[].source`, `/v1/coverage/{slug} source`, `/v1/networks/{slug}/runs rows[].source`

| Code | Parameters | Meaning |
|---|---|---|
| `measured` |  | Measured by the Atlas tests. |
| `catalog` |  | From the catalog only: not measured. |
| `editorial` |  | Written by hand by the editors. |

### `target`

Used in: `* **.targets[].target`, `* **.sources[].target`, `/v1/targets targets[].sources[]`, `* **.tape[].target`, `* **.latencyTarget`, `* **.targets[].group`, `/v1/meta targets[].id`, `/v1/targets targets[].id`, `/v1/targets networks[].cells.<key>`, `/v1/home insights.valueAll.targets[]`, `/v1/home insights.valueAll.rows[].missingTargets[]`, `/v1/home insights.valueAll.rows[].byTarget.<key>`, `/v1/home insights.hardest.target`

From the build's catalog (site.targets: label and pass condition): its codes are listed in `GET /v1/codes`.

### `target_category`

Used in: `* **.targets[].category`

| Code | Parameters | Meaning |
|---|---|---|
| `baseline` |  | Baseline: a plain page any proxy should load. |
| `ecommerce` |  | E-commerce. |
| `search` |  | Search. |
| `social` |  | Social. |
| `anti_bot` |  | Anti-bot: a page behind a bot challenge. |
| `ai_answers` |  | AI answers. |

### `trial_kind`

Used in: `* **.trialKind`

| Code | Parameters | Meaning |
|---|---|---|
| `none` |  | No trial. |
| `sales` |  | A trial through a sales contact. |
| `credit` |  | Free credit to spend. |
| `paid` |  | A paid trial or a refund window. |
| `card` |  | A free trial that needs a card. |
| `free` |  | A free trial. |

### `unavailable_reason`

Used in: `* error.params.reason`, `/healthz reason`

| Code | Parameters | Meaning |
|---|---|---|
| `payload_before_v4` |  | The published calculation is older than payload v4: recalculate. |
| `cache_not_built` |  | The server has not built its answers yet. |
| `dataset_file_missing` |  | The dataset file cannot be read. |
| `events_query_failed` |  | The events could not be read from the database. |

### `unit`

Used in: `/v1/codes sets[].values[].params[].unit`

| Code | Parameters | Meaning |
|---|---|---|
| `ratio` |  | A share from 0 to 1. |
| `percent` |  | A percent from 0 to 100. |
| `points` |  | Percentage points: the difference of two percents. |
| `ms` |  | Milliseconds. |
| `seconds` |  | Seconds. |
| `minutes` |  | Minutes. |
| `days` |  | Days. |
| `usd` |  | US dollars. |
| `usd_per_gb` |  | US dollars per gigabyte. |
| `gb` |  | Gigabytes. |
| `mb` |  | Megabytes. |
| `kb` |  | Kilobytes. |
| `count` |  | A whole number of things. |
| `score_1_10` |  | A score from 1 to 10. |
| `score_0_100` |  | A score from 0 to 100. |

### `unserved`

Used in: `/healthz unserved[].code`

| Code | Parameters | Meaning |
|---|---|---|
| `build_failed` |  | The page's answer failed to build. |
| `not_found` |  | The page answers 404. |
| `no_network_page` |  | A published product has no network. |
| `not_served` |  | The page answers another status than 200 (status). |

### `use_case`

Used in: `* **.useCases[]`, `* **.reviews[].useCase`, `/v1/networks/{slug}/reviews rows[].useCase`

| Code | Parameters | Meaning |
|---|---|---|
| `web_scraping` |  | Collecting data from websites. |
| `price_monitoring` |  | Watching prices and assortments. |
| `ad_verification` |  | Checking where and how ads appear. |
| `seo_monitoring` |  | Tracking search engine optimization. |
| `serp_tracking` |  | Tracking search result pages. |
| `brand_protection` |  | Finding misuse of a brand. |
| `review_monitoring` |  | Watching customer reviews. |
| `market_research` |  | Studying markets and competitors. |
| `ai_training_data` |  | Collecting data to train AI models. |
| `ai_agents` |  | Letting AI agents browse the web. |
| `travel_fares` |  | Collecting travel fares. |
| `social_media` |  | Managing social media accounts. |
| `multi_accounting` |  | Running many accounts at once. |
| `cybersecurity` |  | Security research and testing. |
| `localized_testing` |  | Seeing content as people in other places see it. |
| `uptime_monitoring` |  | Checking that websites are up and fast. |
| `anonymous_browsing` |  | Browsing without revealing one's address. |

## Event data

No answer serves an event's stored data (`/v1/events` `events[].data` went in #993): an
event's `values` are made from these keys of it only, none for an event not listed. A key
marked (code) is read only when its value is a code.

| Event | Data keys |
|---|---|
| `daily_test_started` | `region`, `countries` |
| `daily_test_finished` | `region`, `countries`, `status`, `requests`, `unique_ips`, `duration_s` |
| `census_started` | `region`, `start_at` |
| `census_finished` | `region`, `requests`, `successes`, `unique_ips`, `success_rate`, `duration_s`, `stop_reason` (code), `status`, `bytes_up`, `bytes_down` |
| `census_retry` | `region`, `retry_of`, `reason` (code), `pulse_batch_id` |
| `census_skipped` | `region`, `reason`, `previous_batch_id`, `scheduled_for`, `status`, `runs_killed`, `requests`, `successes`, `unique_ips` |
| `hour_released` | `day`, `hour`, `formula_version`, `unique_ips`, `countries` |
| `pulse_finished` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value`, `previous`, `reason`, `status`, `runs_killed`, `unique_ips` |
| `pulse_down` | `window_start`, `threshold`, `outage_ids`, `window_minutes`, `errors`, `requests`, `regions` |
| `pulse_recovered` | `down_since`, `duration_s`, `outage_ids` |
| `down` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value`, `since`, `source`, `batch_id` |
| `recovered` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value`, `incident`, `source`, `since`, `duration_s`, `outage_batch_id` |
| `success_drop` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value` |
| `latency_spike` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value` |
| `latency_rise` | `p50`, `baseline`, `baseline_source` |
| `block_spike` | `region`, `batch_finished_at`, `requests`, `successes`, `success_rate`, `p50`, `challenges`, `blocks`, `baseline`, `value` |
| `ip_overlap` | `region`, `value`, `shared_ips`, `unique_ips`, `peers`, `hour`, `runs` |
| `new_hosting_asn` | `region`, `asn`, `name`, `usage_type` |
| `node_offline` | `region`, `provider`, `product`, `last_heartbeat` |
| `no_node` | `region`, `provider`, `product`, `kind`, `batches`, `countries` |

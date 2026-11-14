# API changes by contract date

`/v1/version` and `/v1/codes` serve `contract`, the date of the API's last breaking change
(`build.Contract`). Each section below is one contract date, newest first: one line per field,
`added`, `changed` or `removed`, with its endpoint and, for a change, the old and the new form.
A task that changes a served field adds its lines here, and moves the contract date when the
change is breaking (`AGENTS.md`, "Changing an API field").

## 2026-11-14

- removed: `/v1/changelog` `entries[].title`, `entries[].detail`, `entries[].delta`, `entries[].unit`; `/v1/networks/{slug}` `changelog[].title`, `changelog[].detail`, `changelog[].delta`, `changelog[].unit`; the same in the dataset file's `pages` copies, catalog rows (`event` `catalog`) included (CHANGELOG-TEXT-REMOVE-1, #990; read `event` and `values`: `values.delta` or `values.deltaPct` is the old `delta`, its unit is the `values` key's in set `change_event`).
- removed: `/v1/codes` sets `change_title`, `change_detail` and `change_unit` (#990; their fields are gone).
- changed: `/v1/networks/{slug}` `na[]` no longer lists `changelog[].title`, `changelog[].detail` or `changelog[].delta` (#990; the fields are gone).
- removed: `/v1/targets` `targets[].label`; `/v1/meta` `targets[].label`; dataset file `targets[].label` (TARGET-TEXT-REMOVE-1, #989; read `targets[].id`).
- removed: `/v1/targets` `targets[].passCondition`; `/v1/meta` `targets[].passCondition`; dataset file `targets[].passCondition` (#989; `targets[].id`).
- changed: `/v1/targets`, `/v1/meta` and dataset file `targets[].category`: an English name (`"E-commerce"`) → a `target_category` code (`ecommerce`; also `baseline`, `search`, `social`, `anti_bot`, `ai_answers`) (#989).
- removed: `/v1/home` `insights.hardest.label` and `insights.hardest.passCondition` (#989; `insights.hardest.id`).
- removed: `/v1/home`, `/v1/meta`, `/v1/rankings` and dataset file `profiles[].label` (#989; `profiles[].id`).
- added: `/v1/codes` set `target_category` (#989); sets `target` and `ranking_profile` keep the catalog's English reference.
- added: `/v1/networks/{slug}` `profile.pricing.trialPrice` (USD the buyer pays; 0 for a free trial or a credit), `trialCredit` (USD of credit; 0 for none), `trialGb`, `trialDays` (null: no time limit), `trialNeedsCard` (boolean or null); the same on dataset file `providers[].pricing` and `providers[].productMeasurements.<kind>.pricing` (PRICING-CODES-1, #987; `trial` and `hasTrial` stay until #991).
- changed: `trialKind` (`/v1/rankings` `networks[]`, `/v1/networks/{slug}` `profile.pricing`): new code `credit` for a trial of free credit; oxylabs `paid` → `credit` (#987).
- added: `/v1/networks/{slug}` `profile.pricing.plans[].audience` (`plan_audience` code: `all`, `new_customers`) and `profile.pricing.plans[].catalog.poolKinds[]` (`proxy_kind` codes); the same on dataset file `providers[].pricing.plans[]` and `providers[].productMeasurements.<kind>.pricing.plans[]` (#987; `name` and `catalog.pool` stay until #991).
- removed: `pricing.plans[].catalog.bandwidth` (read the plan's `gb`) and `pricing.plans[].catalog.period` (read `catalog.billing`) on `/v1/networks/{slug}` `profile` and dataset file `providers[]` (#987).
- changed: `/v1/networks/{slug}` `profile.na[]` and `profile.naReasons` keys: a plan by its index, `pricing.plans[3].ips`, was by its name, `pricing.plans[PAYG 1,000 to 4,999 GB].ips`; a plan's null `audience` is listed (`catalog`), and the trial numbers' nulls (`not_applicable` without a trial, for a credit's GB and for the days of a trial without a time limit; else `catalog`) (#987).
- added: `/v1/codes` set `plan_audience`; set `trial_kind` gains `credit`; set `proxy_kind` is used in `pricing.plans[].catalog.poolKinds[]`; `na_source` codes `site.pricing.trial_price`, `site.pricing.trial_credit`, `site.pricing.trial_gb`, `site.pricing.trial_days`, `site.pricing.trial_needs_card` (#987).

## 2026-11-13

- added: error code `busy` (503, `Retry-After: 1`, `no-store`) on every answer computed on request (list pages and searches, place pages, `/v1/search`, events answers) while the server computes as many as it may; a made-up place key answers 404 without reading rows (SEC-COST-1, #1018).
- changed: `/v1/events` `events[].data`: only the keys registered for the event in the codes registry (`api/CODES.md`, "Event data"); every other key is dropped, among them `node_offline` `host` and `node_id`, `census_finished` `error`, a `census_finished` `stop_reason` or `census_retry` `reason` that is not a code, and every key of an event code not registered (`{}`). Every key the fixtures and the test week served before stays (SEC-EVENTS-SAFE-1, #1015; the website reads none of the dropped keys, so the contract date does not move).
- changed: `/v1/events` `events[].values`, `/v1/home` and `/v1/networks/{slug}` `tape[].values`, dataset `tape[].values`: only the event code's parameters and the keys every row carries; no key served today is dropped (#1015).
- changed: `/v1/events` query `kind=` and `event=`: more than 32 values → 400 `too_many_values` (new error code); a value that is no event code (nor, for `kind=`, a group) → 400 `invalid_parameter` with the allowed codes, was an empty answer; `kind=` reads an event code as its raw kind, as `event=` does (`kind=hour_released` matches the hour releases) (#1015).
- changed: `/v1/events` query `product=` and `target=` longer than 100 bytes → 400 `too_long` (#1015).
- added: `/v1/changelog` `entries[].event`, `/v1/networks/{slug}` `changelog[].event` and the dataset file's `pages` copies: the change's code, a code of the `change_event` set (CHANGELOG-EVENT-1, #983).
- added: the same entries' `values.lostCountryCodes` (ISO codes) and `values.comparedRange` (a `range` code) on `coverage` entries (#983).
- added: the same entries' `values.deltaPct` on `price` entries (#983).
- added: code set `change_event` (`/v1/codes`, `api/CODES.md`) (#983).
- changed: `/v1/dataset`, `/v1/files/{name}`, `/v1/version`, `/v1/coverage` and `/v1/coverage/{slug}` answer any query parameter with 400 `unknown_parameter` (were: ignored); every route answers a path or parameter with a control character or invalid UTF-8, or a query that does not parse, with 400 `invalid_parameter` (format `text`, added), and a path with a `.` or `..` segment or `//` with 404 (was: a 301 to the cleaned path) (SEC-PARAMS-1, #1012).
- added: `/v1/networks/{slug}` `profile.hqCountry` (ISO code), `profile.hqCity` (a name, or null), `profile.useCases[]` (`use_case` codes); dataset file `providers[].hqCountry`, `providers[].hqCity`, `providers[].useCases[]` (PROVIDER-CODES-1, #986; `hq` and `bestFor` stay until #991).
- added: `/v1/networks/{slug}/reviews` `rows[].lang` (BCP 47), `rows[].monthlySpendMin`, `rows[].monthlySpendMax` (USD); the same on `/v1/networks/{slug}` `reviews[]` and dataset file `reviews[]` (#986).
- added: `/v1/networks/{slug}/runs` `rows[].country` (ISO code); the same on `/v1/networks/{slug}` `communityRuns[]` and dataset file `communityRuns[]` (#986).
- added: code sets `use_case` (17 codes) and `run_rejection_reason` (5 codes; `rejectionReason` holds them from #991 on) (#986).
- added: `na_source` codes `site.providers.hq_country`, `site.providers.hq_city`, `site.providers.use_cases` (#986).
- added: `/v1/home` `networks[].curve.timesIndex`, `networks[].successSpark.timesIndex`, `networks[].latencySpark.timesIndex`, `networks[].successScoreSpark.timesIndex` (the index of the series' times in the answer's `times`, equal to its `labels`; also in the dataset file's `pages` copies; `labels` goes in #988 once the website reads `timesIndex`) (SERIES-TIMES-INDEX-1, #985).
- changed: `/v1/codes` set `source`, code `measured`: its text names the product Atlas, `"Measured by the Atlas tests."` (RENAME-ATLAS-1, #1008).
- changed: `/v1/files/dataset-<sha256>.json` `Content-Disposition` file name: `shifter-atlas-dataset.json`, was named after the old product name (#1008).
- removed: public `/healthz` `buildMs`, `loadMs`, `assembleMs`, `renderMs`, `fingerprint`, `generatedAt`, `providers`, `lastCheck`, `lastError`, `lastErrorAt`, `builds`, `failures`, `searchCacheEntries`, `eventsCacheEntries`, `unserved`, `unservedMore`, `siteGaps`, `coldLists`, `binary`, `failedFingerprint`, `failedError`, `failedAt`, `retryAt`; they stay on the loopback admin listener's `/healthz` (`siteapi serve --admin-listen`) (SEC-HEALTH-1, #1011; `/healthz` is for operators, not the website, so the contract date does not move).
- added: public `/healthz` `revision` (the binary's git revision; was `binary.revision`) (#1011).
- changed: `/healthz` with any query parameter: the report → 400 `unknown_parameter` (#1011).
- added: `/v1/events` `events[].event`: the event code, as a tape row's `event` (`hour_released` for `estimate_hour_released`; #984).
- added: `/v1/events` query `event=<code>[,<code>…]`; `kind=` also takes the groups `census`, `pulse`, `incident`, `audit` (#984).
- changed: `/v1/events` query `q=`: matches the network name and slug, the event code, the country and the target, no longer the tape's English text (#984).
- changed: `/v1/events` `events[].values.hour`, `/v1/home` and `/v1/networks/{slug}` `tape[].values.hour`, dataset `tape[].values.hour`: filled wherever the event's `data.hour` is (an `ip_overlap`'s audited hour), was null there (#984).
- changed: `values.durationS` → `values.durationSeconds` on `/v1/events` `events[]`, `/v1/home` and `/v1/networks/{slug}` `tape[]`, dataset `tape[]` (#984).
- added: `GET /v1/codes` (every code set the API sends; `api/answers/codes.md`, `api/CODES.md`).
- added: `/v1/version` `contract`.
- removed: `/v1/networks/{slug}/chart` and `/v1/networks/all/chart` `labels` (`success`, `availability`, `latency`, `latencyUnderLoad`, `pool`); each point's `t` stays (UNREAD-TEXT-1, #977).
- changed: outage bands, `/v1/networks/{slug}/chart` `bands[]` and dataset file `measuredMarket.views[].fastest.stats.bands[]`, `.mostReliable.stats.bands[]`: `label` (`"major · AD"`) removed; `severity` (an `incident_severity` code) and `country` (ISO code) added (#977).
- removed: `/v1/networks/{slug}` `profile.targets[].sources[].label`; dataset file `providers[].targets[].sources[].label`, `providers[].productMeasurements.<kind>.targets[].sources[].label`; `sources[].target` stays (#977).
- removed: `/v1/networks/{slug}` `profile.products[].name`; dataset file `providers[].products[].name`; `products[].kind` stays (#977).
- changed: `/v1/networks/all` `network.name`: `"All networks"` → null, `not_applicable` (#977).
- added: `/v1/meta` and dataset file `scopes[].naReason`, `scopes[].naReasons` (each scope null's reason; #994).
- added: `na_reason` code `no_pulse_fleet` (a scope's `cadence.pulseIntervalMinutes` without a pulse batch; #994).
- changed: `/v1/meta` `scopes[].geoCoverage.countries`: the scope's tested-country count (null, `not_measured`, without a measuring network), was 6 for `restricted` and null otherwise (#994).
- changed: `/v1/meta` `scopes[].cadence.pulseIntervalMinutes`: the snapshot's pulse interval when it has a pulse batch, was always null (#994).
- changed: `/v1/coverage` `dataset.sections.fields[].reason`, `targets[].sections.fields[].reason`,
  `scopes[].sections.fields[].reason`, `profiles[].sections.fields[].reason`, `fields.<path>.reason` and
  `/v1/coverage/{slug}` `sections.<section>[].reason`: an English sentence (`"Editorial field: no
  site.providers.hq"`) → a code of the `na_reason` set (`"catalog"`).
- added: `/v1/coverage` `dataset.sections.fields[].source`, `targets[].sections.fields[].source`,
  `scopes[].sections.fields[].source`, `profiles[].sections.fields[].source`, `fields.<path>.source` and
  `/v1/coverage/{slug}` `sections.<section>[].source`: a code of the `na_source` set
  (`site.providers.hq`, `snapshot`) or null.
- removed: `/v1/coverage` `targets[].name`, `scopes[].name`, `profiles[].name` (the `id` stays).
- removed: `/v1/networks/{slug}/places` and `/v1/networks/all/places` `rows[].country` (PLACE-CODES-1, #978; read `countryCode`).
- removed: `/v1/networks/{slug}` and `/v1/networks/all` `ranges.<range>.countries[].country` (#978; `countryCode`).
- removed: `/v1/countries` `countries[].country` (#978; `countryCode`).
- removed: `/v1/countries/{cc}` `country` and `ranges.<range>[].country` (#978; `countryCode`).
- removed: `/v1/countries/{cc}/cities` and `/regions` `rows[].country` (#978; `countryCode`).
- removed: `/v1/places/{region|city}/{key}` `rows[].country` (#978; `countryCode`).
- removed: dataset `providers[].geo[].country` and `providers[].productMeasurements.<kind>.geo[].country` (#978; `countryCode`).
- removed: dataset `lists.<slug>.<kind>.<range>.<dimension>.rows[][]` the country-name position (#978; read the rows by `columns`).
- added: `/v1/networks/{slug}/coverage` `columns` (#978; the names of the row positions, `place_column` codes).
- added: dataset `lists.<slug>.<kind>.<range>.<dimension>.columns` (#978).
- added: `country=` on `/v1/networks/{slug}/coverage` and `/places` (#978; a two-letter code, any case; 400 otherwise).
- changed: `q` on `/v1/networks/{slug}/coverage` and `/places` (#978): matches codes, region, city, ASN names and `AS<number>`; old: also English country names.
- changed: `/v1/networks/{slug}/coverage` `totals` (#978): also with `country=` alone; old: only with `q`.
- changed: every 4xx and 5xx body of every route: `{"error": "<sentence>"}` → `{"error": {"code", "params"}}`, a code of the `error` set (`api/CODES.md`); statuses and headers unchanged (TYPED-ERRORS-1, #980).
- added: code sets `error`, `error_format`, `unavailable_reason`, `unserved` (`/v1/codes`, `api/CODES.md`) (#980).
- changed: `/healthz` `reason`: `"calculation older than payload v4: recalculate"` → `payload_before_v4` (#980).
- changed: `/healthz` `unserved[]`: English lines → `{path, code, status, product}`; `"and N more"` → `unservedMore` (added, integer) (#980).
- changed: `/healthz` `fingerprint`: a string holding JSON → the JSON object (#980).
- removed: `GET /v1/search-index` (answers 404; the website's palette is its own; SEARCH-NEUTRAL-1, #981).
- changed: `/v1/search` `{query, tokens, total, sections: [{group, items}]}` → `{query, total, items: [{type, id, name}]}`: `type` a `search_item_type` code, `id` the slug or target id, `name` the network's name or null (#981).
- removed: `/v1/search` `tokens`, `sections[].group` and every item's `group`, `title`, `subtitle`, `href`, `icon`, `meta`, `keywords`, `command`, `download`; its page and action items (#981).
- changed: every time of every answer, list page, events answer, `/healthz` (also inside `fingerprint`) and the dataset file (`generatedAt`, `asOf`, `builtAt`, `/v1/events` `at` and `updatedAt`, `/v1/events/stats` `start`, `end`, `lastAt`, the tape's `at`, ...): `2026-11-11T14:09:42.371728+00:00`, `2026-11-09T04:02:32+00:00` or nanoseconds → `2026-11-11T14:09:42.371Z` (UTC, `Z`, milliseconds; the same instant to the millisecond); date-only fields unchanged (CONSISTENCY-1, #982).
- changed: `/v1/events` `since`: a `since` of whole milliseconds stands for the end of its millisecond; every form read before is still read (#982).
- changed: `/v1/networks/{slug}/runs` `rows[]`, `/v1/networks/{slug}` `communityRuns[]` and dataset file `communityRuns[]`: `deltaSuccessPts` → `successDeltaPts`, `deltaLatencyPct` → `latencyDeltaPct` (same values; #982).

# `/v1/changelog`

Every network's changes in one range, newest first, and the networks whose rank moved: the
website's changelog page (`/proxy-index/changelog`, `changelog/changelog-data.ts`
`ChangeRecord`), field for field. Until this answer the page read a static file exported from
the old reviews site (CHANGELOG-API-1).

- **Route:** `GET /v1/changelog?range=`. `range` is `24h`, `7d` or `30d` (default `30d`);
  another value answers 400 `invalid_parameter`, any other parameter 400 `unknown_parameter`
  (`api/README.md`, "Endpoints").
- **Bodies:** one per range and build, stored as `/v1/changelog/<range>`, with the same ETag,
  304s, gzip, brotli and cache lifetime as every other answer, and in the dataset file's
  `pages` as `/v1/changelog?range=<range>`.
- **Size:** at most `build.ChangelogMaxEntries` (300) entries, and entries of at most
  `build.ChangelogEntryBytes` (176 KB) together; the oldest are cut first, so a body is always
  under `build.MaxAnswerBytes` (200 KB). The network answers count every entry (below).
- **Code:** `api/internal/build/changelog.go` (the record, movers and cut),
  `changelog_entries.go` (incidents, prices, catalog rows) and `changelog_figures.go`
  (performance, pool, coverage, authenticity). It reads only what the build already holds.
- **`/v1/networks/{slug}`** `changelog[]` is the same entries of that network in the 30d
  record (before the cut), newest first, at most 20 (`build.RecordCap`), and
  `records.changelog` their full count, so the two answers never disagree.

## Body

| Field | Type | What |
|---|---|---|
| `generatedAt` | ISO time | The build's time (the snapshot's `as_of`). |
| `runId` | text or null | `Dataset.runId` (`preview-<snapshot id>`), null without a snapshot. |
| `weekSeconds` | integer | 604800: the span of the page's week counts. |
| `entries[]` | `ChangeEntry` | Every change of the range, newest first (by `agoSeconds`, then `id`). |
| `movers[]` | `Mover` | Every network whose rank moved, the largest moves first (then by slug). |

`ChangeEntry`:

| Field | Type | What |
|---|---|---|
| `id` | text | Stable within a build: `chg-outage-<product>-<started_at>`, `chg-<success\|latency\|pool\|coverage\|authenticity>-<slug>`, `chg-price-<slug>-<kind>-<day>`, or the catalog row's `change_id`. |
| `kind` | `incident`, `price`, `pool`, `performance`, `authenticity`, `coverage` or `methodology` | Ranking entries are the page's own, made from `movers`. |
| `event` | code (`change_event`) | What changed (CHANGELOG-EVENT-1, #983): a move's code (`pool_shrank`), `incident` for an incident, `catalog` for a `site.changelog` row. With `values` it is enough to write both lines in any language (event codes, below). |
| `network` | `{slug, name}` or null | The network and its name as the provider list names it (the catalog's name when it has one); null for a fleet-wide catalog row. |
| `severity` | `info`, `good` or `bad` | Good when the move is for the better; an incident is `info` when minor, else `bad`. |
| `agoSeconds` | number | Seconds from the change to `generatedAt`. |
| `source` | text | `measured`. |
| `incident` | object or null | An incident's own fields (below); null otherwise. |
| `values` | object | The entry's numbers and codes (CHANGELOG-VALUES-1): only the keys of its kind (below). |
| `naReasons` | object, left out when empty | The reason code of each null `values` key, by its path (`values.plan`). |

An entry has no `title`, `detail`, `delta` or `unit` (removed on 2026-11-14,
CHANGELOG-TEXT-REMOVE-1, #990): the website writes both lines from `event` and `values`
(its W3 switch), the move's number is `values.delta` or `values.deltaPct`, and the codes
catalog gives each `values` key's unit. A catalog row (`site.changelog`) serves neither its
English headline and detail (#962) nor its `delta` and `unit`: its `kind`, `network`,
`severity` and `agoSeconds` stay.

`incident`: `severity` (`minor`, `moderate`, `major`),
`impact` (0-1 or null) and `durationMinutes` (null while ongoing). An incident has no
`scope` and no `summary` (removed on 2026-11-13, #962): the page writes them from the
numbers and codes (CHANGELOG-VALUES-1), the same as each outage's in the network answer's
`profile.incidents[]` (one function, `incidentFacts`, makes both):

| Field | Type | What |
|---|---|---|
| `countries` | integer | How many countries the incident took. |
| `countryCodes` | text[] | Their ISO codes, sorted. |
| `kind` | `down` or `census_stall` | `down` for an outage a pulse opened, `census_stall` for one a stalled census opened (`source` `census`). The outages hold no other kind; `success_drop`, `latency_spike` and `block_spike` are the events' kinds (`/v1/events`, the tape's `values`), which open no outage. |
| `detectedBy` | `pulse` or `census` | The first outage's `source`. |
| `ongoing` | boolean | Whether any of its outages is still open. |
| `startedAt` | ISO time | The first outage's start. |
| `endedAt` | ISO time or null | The last outage's end (`ended_at`, else `started_at + duration_s`); null while ongoing. |

`values`, by `kind`:

| `kind` | `values` | What |
|---|---|---|
| `pool` | `{uniqueIps, previousUniqueIps, deltaPct}` | The unique IPs per slot of the closing fifth (`uniqueIps`) and the opening fifth (`previousUniqueIps`) of the range's pool series, whole numbers: the two the move compares. `deltaPct` is the move in percent (1 dp). |
| `price` | `{fromRate, toRate, deltaPct, plan}` | The list rate before and after, USD per GB, and `deltaPct` the move in percent (1 dp; #983). `plan` is null (`naReasons`: `not_published`): `site.pricing_history` names no plan. |
| `performance` | `{metric, value, previous, delta, basis}` | `metric` `success` or `latency`. For `success`, the success rate of the closing fifth (`value`) and the opening fifth (`previous`) as shares 0 to 1 (4 dp), `delta` in points, `basis` null (`naReasons`: `not_applicable`); for `latency`, the two periods' median latency in ms (whole), `delta` in % (1 dp), and `basis` the periods compared, `day`, `week` or `month` (`/v1/codes` set `latency_delta_basis`, the range's `latencyDeltaBasis`; LATENCY-DELTA-FALLBACK-1). |
| `coverage` | `{countries, previousCountries, delta, lostCountryCodes, comparedRange}` | The countries with IPs in the longer window (`previousCountries`), those of them still seen in the recent one (`countries`), and `delta` (minus the countries lost). `lostCountryCodes` is the lost countries' ISO codes, sorted, and `comparedRange` the recent window (`24h` or `7d`, a `range` code) they were not seen in (#983). |
| `authenticity` | `{value, previous, delta}` | The geo accuracy of the recent window (`value`) and the longer one (`previous`), shares 0 to 1 (4 dp); `delta` in points (1 dp). |
| `methodology` | `{fromId, toId}` | Methodology ids. Only a `site.changelog` row logs a method change and it carries sentences, so both are null (`catalog`). |
| `incident` | `{}` | The numbers are in its `incident` block. |

A `site.changelog` row has its kind's keys, each null with the reason `catalog` (`plan`:
`not_published`). The same function builds every entry once per build, so `/v1/changelog`
and the network answer's `changelog[]` carry the same `values`; the network answer lists a
null one in its `na` (`changelog[].values.plan`) with the same code.

`Mover`: `slug`, `name`, `source`, `rank`, `previousRank`, `delta` = `previousRank − rank`
(positive is up) and `reasonId`, the `id` of the network's newest non-incident entry in the
same body, or null.

## Sources and thresholds

Every threshold is a constant in `changelog.go`.

| Kind | Comes from | Logged when |
|---|---|---|
| `incident` | Each product's outages (`products[].outages`, the network answer's `profile.incidents`). Outages of one product that overlap in time are one incident (its countries, from the first start to the last end; ongoing when any is). | It overlaps the range. `severity`: `minor` when it lasts under 30 minutes (`IncidentMinorMinutes`) and takes under 5% of the tested countries (`IncidentMinorShare`); `major` when it lasts over 2 hours (`IncidentMajorMinutes`) or takes over 25% (`IncidentMajorShare`); else `moderate`. An ongoing incident is measured to `generatedAt`. `impact` is its countries ÷ the countries the product was tested in over 30 days (`windows.30d.uptime.countries`). `troughSuccessRate` was removed on 2026-11-14 (#900): the page words an incident from `kind`, `severity`, `impact` and `durationMinutes`. |
| `performance` | The network's headline product, its range's success change (internal since 2026-11-14, #900; the answer serves `successScoreDelta`) and `latencyDeltaPct` (the network answer's `stats.<range>`: the last whole UTC day against the day before it for `24h`, the last 7 (30) whole UTC days against the 7 (30) before for `7d` (`30d`), made once a day at 03:00 UTC, its `values.value` and `previous` the two periods' latency p50s; until a network's data holds two such periods, the longest shorter ones it allows (30d the last 7 whole days, then the last day; 7d the last day), named by `values.basis`; `FIELDS.md`). | The success rate moved at least 2 points (`PerformanceSuccessPts`) or the median latency at least 15% (`PerformanceLatencyPct`): one entry each. |
| `pool` | The unique IPs per slot of the range's pool series (the chart's `pool`), closing fifth against opening fifth, in percent. | At least 10% (`PoolChangePct`). |
| `coverage` | The countries with IPs in the products' country breakdown: the recent window against the longer one it ends (24h against 7d for `24h`; 7d against 30d for `7d` and `30d`). The snapshot publishes no earlier period for these figures, so only countries lost lately can be seen: a country gained shows in both windows. | A country with IPs in the longer window has none in the recent one. |
| `authenticity` | `pool.geo_accuracy` (exit IPs in the requested country), recent window against the longer one, as for coverage. | It moved at least 2 points (`AuthenticityPts`). |
| `price` | `site.pricing_history` with the snapshot's pricing as its newest step, as `/v1/networks/{slug}/pricing-history` reads them (the lowest list rate, else the entry rate). | The rate in force changed on a day in the range; `agoSeconds` from that day's start. |
| `methodology` | A `site.changelog` row of kind `methodology`. The API reads one published calculation, so it cannot see the methodology id (`/v1/meta`) change by itself: a method change is logged with a catalog row. | Its `occurred_at` is in the range. |
| catalog rows | `site.changelog`: its `severity` (another value is `info`) as it is; kind `pricing` is the page's `price`. Its headline, detail, delta and unit are not served. | `occurred_at` in the range. |
| `movers` | Each network's balanced-score rank in the 30d window and its `previous_rank` (the snapshot's), the same in every range's body. | Both are known and differ. Previous ranks exist once two scored builds have run. |

Performance, pool, coverage and authenticity entries are dated at the window's end
(`agoSeconds` 0 for a build of the current snapshot).

## Event codes

Every `event` the API writes, with the `values` keys it fills (CHANGELOG-EVENT-1, #983). The
codes catalog (`/v1/codes` set `change_event`, `api/CODES.md`) gives each key's type and unit
and one English reference sentence. A `delta` or `deltaPct` is signed (negative for a fall);
a sentence words its size. The old `title` and `detail` codes (`change_title`, `change_detail`)
and the `change_unit` codes are gone with those fields (#990).

| `event` | `kind` | `values` |
|---|---|---|
| `success_rose`, `success_fell` | performance | `metric` `success`, `value`, `previous` (0-1), `delta` (points), `basis` (null) |
| `latency_rose`, `latency_fell` | performance | `metric` `latency`, `value`, `previous` (ms), `delta` (%), `basis` (`day`, `week`, `month`) |
| `pool_grew`, `pool_shrank` | pool | `uniqueIps`, `previousUniqueIps`, `deltaPct` |
| `countries_lost` | coverage | `countries`, `previousCountries`, `delta`, `lostCountryCodes`, `comparedRange` |
| `geo_accuracy_rose`, `geo_accuracy_fell` | authenticity | `value`, `previous` (0-1), `delta` (points) |
| `price_raised`, `price_cut` | price | `fromRate`, `toRate` (USD per GB), `deltaPct`, `plan` |
| `incident` | incident | `{}`: its facts are in `incident` |
| `catalog` | the row's | its kind's keys, each null (`naReasons`) |

An incident has no `scope` or `summary`: its countries are `incident.countryCodes`
and `countries`, its severity `severity`, and `ongoing`, `durationMinutes` and `impact` say the rest.

## Tests

`api/internal/build/changelog_test.go`: the golden `testdata/changelog.golden.json` of a
fixture with an ongoing and a closed incident, an older one, a performance, pool, coverage,
authenticity and price change, a network and a fleet-wide catalog row and two movers (one with
a reason, one without); the figures behind it; an empty build (`entries: []`, `movers: []`);
the cut by count and by size; the network answer's `changelog[]` against the answer's own
entries in a full build; the query; the severity bands and the outage merge.
`changelog_shape_test.go` decodes every body with the website's shape: no field missing, none
extra (no `title`, `detail`, `delta` or `unit`), each of its type, and each entry's `values` keys its kind's, every null one with its
reason and the parameters of its `event` (a catalog row's all null). `changelog_values_test.go`:
one case per kind's `values` and `event`; every other event (each move's other direction, a
coverage loss against 24h, a price raise) made and registered, with its severity; an ongoing and a closed
incident over several countries, a census one, and the profile's `incidents[]` of the same
outages with the same facts. `TestEveryAnswerFitsTheCap` and `TestEveryV4Answer`
(`answers-v4-scale/changelog.json`) cover the samples, and `siteapi bench` times the page
`/proxy-index/changelog`.

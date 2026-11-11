# `/v1/changelog`

Every network's changes in one range, newest first, and the networks whose rank moved: the
website's changelog page (`/proxy-index/changelog`, `changelog/changelog-data.ts`
`ChangeRecord`), field for field. Until this answer the page read a static file exported from
the old reviews site (CHANGELOG-API-1).

- **Route:** `GET /v1/changelog?range=`. `range` is `24h`, `7d` or `30d` (default `30d`);
  another value or any other parameter answers 400.
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
| `network` | `{slug, name}` or null | The network and its name as the provider list names it (the catalog's name when it has one); null for a fleet-wide catalog row. |
| `title`, `detail` | `{key, params}` or null | A fixed English sentence (`key`, below) with placeholders: `{name}` is the network's name (the page fills it), `{v1}`, `{v2}` are in `params`, every value a string. An incident has neither; a catalog row without a detail has no `detail`. |
| `delta` | number (1 dp) or null | The move: points, percent or countries (`unit`); null for an incident and a catalog row without one. |
| `unit` | text | `pts`, `%`, `countries`, the catalog row's, or `""`. |
| `severity` | `info`, `good` or `bad` | Good when the move is for the better; an incident is `info` when minor, else `bad`. |
| `agoSeconds` | number | Seconds from the change to `generatedAt`. |
| `source` | text | `measured`. |
| `incident` | object or null | An incident's own fields (below); null otherwise. |

`incident`: `severity` (`minor`, `moderate`, `major`), `scope` (one country's name, or
`<n> countries`), `summary` (a fixed sentence, below), `troughSuccessRate` (0-1 or null),
`impact` (0-1 or null) and `durationMinutes` (null while ongoing). The page translates
`severity`, `scope` and `summary`.

`Mover`: `slug`, `name`, `source`, `rank`, `previousRank`, `delta` = `previousRank − rank`
(positive is up) and `reasonId`, the `id` of the network's newest non-incident entry in the
same body, or null.

## Sources and thresholds

Every threshold is a constant in `changelog.go`.

| Kind | Comes from | Logged when |
|---|---|---|
| `incident` | Each product's outages (`products[].outages`, the network answer's `profile.incidents`). Outages of one product that overlap in time are one incident (its countries, from the first start to the last end; ongoing when any is). | It overlaps the range. `severity`: `minor` when it lasts under 30 minutes (`IncidentMinorMinutes`) and takes under 5% of the tested countries (`IncidentMinorShare`); `major` when it lasts over 2 hours (`IncidentMajorMinutes`) or takes over 25% (`IncidentMajorShare`); else `moderate`. An ongoing incident is measured to `generatedAt`. `impact` is its countries ÷ the countries the product was tested in over 30 days (`windows.30d.uptime.countries`). `troughSuccessRate` is the lowest success rate of a series slot that overlaps it, from the finest window with one (24h 5-minute slots, then 7d and 30d days). |
| `performance` | The network's headline product, its range's `successDeltaPts` and `latencyDeltaPct` (the network answer's `stats.<range>`: the closing fifth of the range against its opening fifth). | The success rate moved at least 2 points (`PerformanceSuccessPts`) or the median latency at least 15% (`PerformanceLatencyPct`): one entry each. |
| `pool` | The unique IPs per slot of the range's pool series (the chart's `pool`), closing fifth against opening fifth, in percent. | At least 10% (`PoolChangePct`). |
| `coverage` | The countries with IPs in the products' country breakdown: the recent window against the longer one it ends (24h against 7d for `24h`; 7d against 30d for `7d` and `30d`). The snapshot publishes no earlier period for these figures, so only countries lost lately can be seen: a country gained shows in both windows. | A country with IPs in the longer window has none in the recent one. |
| `authenticity` | `pool.geo_accuracy` (exit IPs in the requested country), recent window against the longer one, as for coverage. | It moved at least 2 points (`AuthenticityPts`). |
| `price` | `site.pricing_history` with the snapshot's pricing as its newest step, as `/v1/networks/{slug}/pricing-history` reads them (the lowest list rate, else the entry rate). | The rate in force changed on a day in the range; `agoSeconds` from that day's start. |
| `methodology` | A `site.changelog` row of kind `methodology`. The API reads one published calculation, so it cannot see the methodology id (`/v1/meta`) change by itself: a method change is logged with a catalog row. | Its `occurred_at` is in the range. |
| catalog rows | `site.changelog`: `headline` is the title's key, `detail` the detail's (no params), `delta`, `unit`, `severity` (another value is `info`) as they are; kind `pricing` is the page's `price`. | `occurred_at` in the range. |
| `movers` | Each network's balanced-score rank in the 30d window and its `previous_rank` (the snapshot's), the same in every range's body. | Both are known and differ. Previous ranks exist once two scored builds have run. |

Performance, pool, coverage and authenticity entries are dated at the window's end
(`agoSeconds` 0 for a build of the current snapshot).

## Templates

Every `key` the API writes (a catalog row's own sentences aside), so the website can translate
each one. `{name}` is the entry's network; `{v1}` and `{v2}` are formatted figures.

| Kind | `title.key` | `detail.key` | Params |
|---|---|---|---|
| performance | `{name} success rate rose {v1} points`, `{name} success rate fell {v1} points` | `Success rate is now {v1}.` | title `v1` `3.2`; detail `v1` `95.0%` |
| performance | `{name} median latency rose {v1}`, `{name} median latency fell {v1}` | `Median latency is now {v1} ms.` | title `v1` `18.0%`; detail `v1` `1,113` |
| pool | `{name} pool grew {v1}`, `{name} pool shrank {v1}` | `{v1} unique IPs in this window.` (null when the range has no unique IPs) | title `v1` `30.0%`; detail `v1` `4,321` |
| coverage | `{name} lost {v1} country`, `{name} lost {v1} countries` | `Not seen in the last 24 hours: {v1}.`, `Not seen in the last 7 days: {v1}.` | title `v1` `2`; detail `v1` `Poland, Spain` (country names) |
| authenticity | `{name} geo accuracy rose {v1} points`, `{name} geo accuracy fell {v1} points` | `{v1} of exit IPs are in the requested country.` | title `v1` `5.0`; detail `v1` `90.0%` |
| price | `{name} raised list pricing {v1}`, `{name} cut list pricing {v1}` | `List rate went from {v1} to {v2} per GB.` | title `v1` `25.0%`; detail `v1` `$2.00`, `v2` `$1.50` |

Incident `summary`: `Requests stopped succeeding in the affected countries.` and, while
ongoing, `Requests stopped succeeding in the affected countries; still ongoing.`. Incident
`severity`: `minor`, `moderate`, `major`. Incident `scope`: a country name (from the products'
country breakdown, else the country answers, else its code) or `<n> countries` with the number
filled in.

## Tests

`api/internal/build/changelog_test.go`: the golden `testdata/changelog.golden.json` of a
fixture with an ongoing and a closed incident, an older one, a performance, pool, coverage,
authenticity and price change, a network and a fleet-wide catalog row and two movers (one with
a reason, one without); the figures behind it; an empty build (`entries: []`, `movers: []`);
the cut by count and by size; the network answer's `changelog[]` against the answer's own
entries in a full build; the query; the severity bands and the outage merge.
`changelog_shape_test.go` decodes every body with the website's shape: no field missing, none
extra, each of its type. `TestEveryAnswerFitsTheCap` and `TestEveryV4Answer`
(`answers-v4-scale/changelog.json`) cover the samples, and `siteapi bench` times the page
`/proxy-index/changelog`.

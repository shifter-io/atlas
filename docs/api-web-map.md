# Website pages and the API: a field-by-field map

Every piece of data the Proxy Index section of the Shifter website shows, and the `siteapi`
endpoint and response field that serves it (or should). The API adapts to the website's
design, never the reverse. This map is the input of the milestone that makes the API serve
every Proxy Index page, fast, at production volume: each gap below becomes an issue.

- **Website:** the Shifter Astro site, branch `proxy-index` (commit `32be40ad`), read only. Paths
  are relative to the website root. The pages read static mock files today
  (`src/data/proxy-index/*.json`), written by `scripts/proxy-index/export-*.mts` from the old
  proxy reviews site's libraries over a `siteapi dump`. The files come from three exports on
  different days (`home.json` 2026-10-03, `networks.json` 2026-10-05, `targets.json`,
  `changelog.json` and `methodology.json` 2026-09-26), so pages disagree (JSON-LD `dateModified`
  too, HC-13). They make no API request at run time;
  the only runtime fetches are the website's own routes (`/proxy-index/search.json`,
  `/proxy-index/live-tape.json`).
- **API:** `api/` at `main` `4d7dca9` (after #689 and #690, the rank-based browser target
  scores). Endpoints: `api/README.md` "Endpoints"; fields: `api/FIELDS.md`.
- **Pages in scope:** `/proxy-index`, `/proxy-index/targets`, `/proxy-index/networks`,
  `/proxy-index/networks/<network>` (example `nimbus-residential`), `/proxy-index/compare`,
  `/proxy-index/overlap`, `/proxy-index/true-cost`, `/proxy-index/live-tape`, plus the search
  palette and the dock every one of them shows. The section's other pages are listed at the
  end of the home page's part (section 2) and are out of scope.

## How to read the tables

One section per page, one table per visible part of the page, in page order. Columns:

| Column | Meaning |
|---|---|
| Shown on page | The element: number, label, chart series, table column, sort, filter, tooltip, badge, empty or N/A state. |
| Website data field | Where the page reads it today: mock file → JSON path, or the TS expression. |
| Unit / format | As drawn (%, ms, $, count, 1 dp, compact `195k`, `N/A`). |
| Filters, sorts, ranges | The interaction that changes it (range dock 24h / 7d / 30d, sort menu, search, paging, network picker). |
| API endpoint → field | The answer and its field path, e.g. `/v1/rankings?range= → networks[].successRate`. |
| Status | One of the four below. |

| Status | Meaning |
|---|---|
| `exists` | An API answer carries this value, in this meaning and shape. |
| `exists-but-different-shape` | The API has the value, but in another shape, unit, window or answer (for example only in the 9 MB dataset file, or only one network per request), or the page would have to derive it the way the old site's libraries did. The Notes say what the API should add. |
| `missing` | No API field carries it. The Notes name the source that could fill it (snapshot path, framework table, `site.*` catalog table) or "no source yet". |
| `editorial` | Static copy, not data. Pure prose is grouped in one row; numbers written into copy are listed and say which field should serve them. |

Shorthands used in several sections:

- **RK** = `/v1/rankings?range=<r>` → `networks[]` (one row per network, list order).
- **NP** = `/v1/networks/{slug}?kind=<kind>` → `profile` (one network per request, up to 200 KB).
- **NA** = `/v1/networks/{slug}?kind=` without `profile` (the summed `ranges`, `days`, `countries`, `lists`).
- **CH** = `/v1/networks/{slug}/chart?kind=&range=` (the histories of one range).
- **ALL** = `/v1/networks/all` (every network together).
- **DSF** = the dataset file `/v1/dataset` redirects to (about 9.2 MB, gzip 0.5 MB at 18 networks):
  `providers[]`, `targets[]`, `totals`, `measuredTotals`, `measuredMarket`, `pages`, `lists`.
- **EV** = `/v1/events?product=&kind=&category=&since=&limit=`.

## The API's serving model, and what "fast" means here

- Every answer is computed once per build (one consistent read of the published calculation,
  the catalog and the newest events), rendered, compressed (identity, gzip, brotli) and held in
  memory; a request only looks up bytes (`api/README.md`, "Caching"). Request-time database work
  is limited to two cases: a `/v1/events` filter the in-memory window cannot answer, and a
  long-list page whose rows are not in the row cache (`calculated_list_rows`; `--row-cache`,
  512 MiB by default, which a rebuild fills with its lists' rows before it serves).
- So a page is fast when (1) every number it draws is in a precomputed answer, (2) it needs few
  answers per view (not one per network), (3) each answer is small (at most 200 KB,
  `build.MaxAnswerBytes`), and (4) the two request-time reads have indexes and plans that stay
  fast at production volume. The build itself (database read, assembly, compression) and the
  hourly calculation behind it must keep up with the data's hourly period at 7 to 30 days of
  production data.
- Many gaps below are therefore not missing numbers but missing **answers**: the value exists
  per network (NP) or only in the 9 MB dataset file, while the page needs it for every network
  at once. Each such gap proposes one precomputed answer per page part.

## Rank-based browser target scores (#689, #690)

The website does not show scores yet and starts migrating now; it keeps the percentages while
it does. Wherever a page shows a per-target rate (success, challenge or CAPTCHA, block,
outcomes, page load) or a per-network browser figure, the tables list the score next to it:

| Score | API field | Next to |
|---|---|---|
| Per target, the network's main engine | `targets[<id>].scores.success` / `.challenge` / `.block` = `{score, rank, rankedOf, status}` (NP, DSF) | `successRate` / `captchaRate` / `blockRate` (`outcomes.ok` / `.challenge` / `.block`) |
| Per target, one engine | `targets[<id>].engines.<engine>.scores.*` | `engines.<engine>.outcomes.*`, `.pageLoadP50` / `P90` |
| Which engine | `targets[<id>].engine` (`chrome` or `camoufox`), `engines` keys | every browser figure |
| Network overall | `browserOverall.<engine>.success` = `{score, rank, rankedOf, status, targets, weights}` (NP `profile`, DSF) | a network's mean target success |

- `score` is 1 to 10 (`1 + 9 × (M − rank) / (M − 1)`), `rank` may be fractional (ties share the
  average), `rankedOf` is M; `score`, `rank` and `rankedOf` are null unless `status` is
  `ranked`; the other statuses are `not_enough_data`, `too_few_networks` and `all_similar`
  (`api/FIELDS.md`, "Browser scores"). HTTP targets (`generic_http`, `generic_https`) have a
  success score only (`scores.success`, #881, below).
- Today the scores are served for the 30d window only, per network (NP) or in the dataset
  file; no list answer (RK, `/v1/networks`) carries `browserOverall`. The gaps `SC-1` to `SC-4`
  in the summary cover this.

**Success scores (SUCCESS-SCORE-API-1, #881).** The website replaces the success rate (%)
with a 1-to-10 success score everywhere; the rates stay in every answer beside the scores
(`api/FIELDS.md`, "Success scores"):

| Score | API field | Replaces |
|---|---|---|
| Per HTTP target | `cells.generic_http.scores.success`, `cells.generic_https.scores.success` = `{score, rank, rankedOf, status}` on `/v1/targets` (and `targets[<id>].scores.success` on NP, DSF, `/v1/home` `insights.hardestRates[].scores`); `targets[].scoreStatus.http` | the cells' `successRate` |
| Network overall, every target | `successScore` = `{score, rank, rankedOf, status, targets, weights}` on `/v1/home`, `/v1/rankings`, `/v1/targets`, `/v1/overlap` `networks[]`, `/v1/networks` rows, NP `network`, `profile`, `ranges.<r>`, `stats.<r>` | a network's `successRate` |
| Network at a place | `rows[].successScore` = `{score, rank, rankedOf, status}` on `/v1/places/{region\|city}/{key}` | the row's `successRate` |
| Market | `/v1/home` `market.successScore` = `{score, status, basis}`, `1 + 9 × medianSuccess` | the "median success" badge |
| Tooltips | `scoreDefinitions` = `{rank, market, status}` on `/v1/meta`, `/v1/home`, `/v1/rankings` | |

A network's own place lists (`/v1/networks/{slug}/places`, `/coverage`) rank places, not
networks, so they keep `successRate` and have no score.

## 1. /proxy-index (Part 1: the `ProxyIndexHome` island) — live dashboard: hero, market panel, insights, leaderboard, live tape

Source files read (website root):
- src/pages/proxy-index/index.astro (only to see how the island is mounted: `client:load`; the page's SEO uses `DATA.generatedAt`, `DATA.targets`)
- src/components/proxy-index/proxy-index-home.tsx, pi-hero.tsx, pi-market.tsx, pi-leaderboard.tsx, pi-insights.tsx, pi-tape.tsx, pi-charts.tsx, pi-network-strip.tsx, pi-network-cards.tsx, pi-profile-preview.tsx, pi-ui.tsx, pi-data.ts, pi-palette.tsx (imported by the island), pin-columns.ts (layout only)
- src/pages/proxy-index/search.json.ts and dataset.json.ts (the two website routes the island calls or links)
- src/data/proxy-index/home.json (every key)
- scripts/proxy-index/export-home.mts (provenance of each key)

API files read (repo): api/README.md (Endpoints), api/FIELDS.md (whole), api/internal/model/{model.go,dataset.go,target.go}, api/internal/build/{types.go,rankings.go,measured_market.go,fleet.go,window.go,measured_trends.go,measured.go,pages.go,profile.go,tape.go,events.go,allnetworks.go}, api/internal/store/events.go, api/internal/server/server.go.

Conventions used below:
- `w` = `windowOf(list, range)`: the window object for the range picked in the dock (default `30d`).
- "dataset file" = the body `/v1/dataset` redirects to (`/v1/files/dataset-<sha>.json`, about 9 MB at 18 networks). A value that is only there is marked `exists-but-different-shape` (right value, wrong place for a per-view page: no per-page answer carries it).
- `MEASURED` = `home.json networks[]` filtered to `kind === "residential"`, sorted by `score.composite` desc, with `standing` overwritten to `{rank: i+1, peers: length}` because `SHOW_DEMO = true` (pi-data.ts). With `SHOW_DEMO = false` it is `home.json measured[]` (slugs) mapped to networks, residential only. Today it holds 4 rows: 3 demo networks (`nimbus-residential`, `orbitnode`, `kestrel-net`) + `shifter`. The API has no demo networks, so in the API version `MEASURED` = `/v1/rankings?range=` rows with `kind == "residential"`.
- All data is bundled at build time from home.json: the island makes no data fetch except the search index (row 18). It never polls `/v1/version`.

---

### A. Island shell: time dock, keyboard, command palette (proxy-index-home.tsx, pi-palette.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Range segmented control "Last 24h / Last 7d / Last 30d" (phones: "24h/7d/30d") | `RANGES = ["24h","7d","30d"]`, `DEFAULT_RANGE = "30d"` (pi-data.ts) | enum | Sets `range` for hero, market, insights, leaderboard. Dock shows only while the dashboard div intersects (rootMargin −35%) | `/v1/rankings?range=`, `/v1/networks/{slug}/chart?range=`; range ids match `build.Ranges` (`24h`,`7d`,`30d`) | exists | Range metadata (`label`, `phrase`, `bucket`, `axis`) is in every `stats[r].range` of `/v1/networks/{slug}` but the website hard-codes its labels (`RANGE_LABEL`). |
| 2 | "Live" dot + label in the dock | hard-coded in proxy-index-home.tsx | text | none | `/v1/version → builtAt` / any answer's `generatedAt` | missing | The doc comment promises "the freshness of the data" but nothing data-driven is shown. The API has the freshness (`generatedAt`, `/v1/version builtAt`); the website should show it and poll `/v1/version` (README: "Open pages poll /v1/version"). |
| 3 | Search trigger "Search networks" + ⌘K / Ctrl K badge; ⌘K toggles, `/` opens | none (platform detection) | — | keyboard | — | editorial | — |
| 4 | Palette "Networks" group: name, `tagline · Measured/Demo`, meta `"{composite} · #{rank}"` or "Unranked", monogram | `MEASURED[].name`, `.tagline`, `.source`, `.score.composite`, `.standing.rank` (fallback `score.rank`), `.monogram` | score 1 dp | Ranked token search (title 120/90, keywords 55, subtitle 45, fuzzy 18 + group bonus), 5 per group, "Top networks" = first 5 before typing; picking a network dispatches `pi:scope` (opens it in the market panel) and scrolls to `#market` | `/v1/search?q=` → `sections[].items[]` (similar scoring with different constants: Pages bonus 3 vs 5, keyword word-start 30 checked after the subtitle vs 55 checked before it, loose match span under 3× vs 2× the token length, 16-result cap; meta `"Unranked · N/A"` for unscored) or `/v1/search-index`; names/score/rank also `/v1/rankings` | exists-but-different-shape | The website scores locally over bundled data; `/v1/search` does it server-side (16 results, 5 per group) and adds a `Countries` group. Tagline per network is only in `/v1/networks/{slug} → profile.tagline` and the dataset file. Monogram is derived (`name.slice(0,2).toUpperCase()`). |
| 5 | Palette "Pages" (13 Index pages), "Sections" (`market`, `insights`, `leaderboard`, `tape`, All networks), "Actions" (Download dataset, Copy link, Show last 24h/7d/30d), "Recent" (localStorage `pi-palette-recent`, 4 ids) | constants `PAGES`, `sec(...)`, actions in pi-palette.tsx | — | range actions call `onRange` and scroll to `#market` | `/v1/search-index` lists 8 old-site pages (`/`, `/networks`, `/compare`, `/overlap`, `/pricing`, `/targets`, `/methodology`, `/changelog`; no Alternatives, Live tape, Guides, Contribute, Disclosure) and the actions apply, theme and copy (no dataset download, no range actions) | editorial | Website pages list is its own navigation; not data. |
| 6 | Palette "Guides" and "Targets" groups; which networks have a review | `fetch('/proxy-index/search.json?locale=')` once per visit (on open, or 2.5 s after idle), `{reviews: slug[], entries: [{kind guide/blog/target, id, title, subtitle, keywords, href}]}` | — | retried on next open if failed | `/v1/search-index` → targets group (labels from `targets[]`); guides/reviews are not in the API | exists-but-different-shape | Runtime fetch (website route, `max-age=300`). Targets come from the website's own `TARGETS` (targets-data.ts), not from the API; API's target list is `/v1/dataset → targets[]` / `/v1/search-index`. Guides and review pages are website content (editorial). |

### B. Hero (pi-hero.tsx, pi-network-strip.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 7 | Badge "Proxy Index", h1 "Proxy networks, measured in the open.", subtitle with link to /proxy-index/networks, HIGHLIGHTS "Equal testing / No paid ranks / Open data / Custom weights" | hard-coded | text | — | — | editorial | h1 is SSR (LCP). |
| 8 | "{N} requests tested in the last 24 hours/7 days/30 days" (CountUp roll) | `DATA.run[range].requests` (export: old-site `window.ts fleetWindows[].requests`, sum of every provider's window requests incl. demo) | integer, `toLocaleString en-US` | follows range | `/v1/networks/all → ranges.<r>.requests` (summed over every product group) | exists | Mock: the hero sums only the four demo networks (24h 187,263 = helix-mobile 47,318 + nimbus-residential 48,830 + orbitnode 43,837 + kestrel-net 47,278; 30d 5,617,890) and leaves out Shifter, the one measured network; the market panel's 195,275 (row 19) is Shifter alone. Two old-site helpers (`fleetWindows` vs `measuredMarketViews`) give the two totals; with the API both rows read one field (HOME-52). The hero phrase "in the last 30 days" ignores `observedDays`, while the market tile says "the last <1 day" (HOME-5). Also `/v1/dataset → measuredMarket.views[].fleet.requests`. |
| 9 | CTA "See the rankings" (#leaderboard), "Download the dataset" | link `/proxy-index/dataset.json` (website route that returns home.json as an attachment) | — | — | `/v1/dataset` (302 to the dataset file) | exists-but-different-shape | The download today is the 3.4k-line home snapshot, not the API dataset. Decide whether the button should point to the API dataset (9 MB) or a home answer. |
| 10 | ScoreWindow rows (top 5): rank, monogram, name, score bar (`composite/100`), score `fixed(1)`, grade with tone | `BOARD = MEASURED.slice(0,5)`: `.standing.rank`, `.monogram`, `.name`, `.score.composite`, `.score.grade`; tone `gradeTone(grade)` (A→pos, B→info, C→warn, N/A→flat, else neg) | 0–100, 1 dp | Highlight cycles every 2,600 ms while visible (`useLiveWhileVisible`); not range-dependent (always the 30d composite) | `/v1/rankings?range=30d → networks[].{position, rank, peers, score, grade, name, slug}` | exists-but-different-shape | Rankings' `score`/`grade` are per range (`windows[range].scores.balanced`); the website shows the 30d `provider.score` for every range. Choose: keep 30d, or follow the dock. Rank: see row 46. The rank part: RK `rank` is null for a network the framework leaves unranked and RK has no "#1 of 1" rule (row 46, HOME-19). |
| 11 | "Score breakdown" for the highlighted network: 8 categories, label + rounded value + bar | `n.breakdown.{reliability,speed,uptime,coverage,diversity,dcAsns,authenticity,value}`; labels `DATA.scoreLabels[k]` | 0–100 ratio to the best of the kind, `Math.round` | cycles with row 10 | `/v1/rankings?range=30d → networks[].breakdown.*` (profile `balanced`); also `/v1/networks/{slug} → profile.score.breakdown` | exists | Labels: `scoreLabels` (Reliability, Speed, Availability, Coverage, Diversity, DC ASNs, Authenticity, Value) came from the old site's data.ts; not served by the API (keys match `model.BreakdownKeys`). Treat as editorial i18n copy. |
| 11a | ScoreWindow empty / N/A states: rank "–" when `standing.rank` is null, score "N/A" with a 0-width bar, each breakdown value "N/A" with a 0-width bar | `b.standing.rank ?? "–"`, `missing(score)`, `missing(v)` | "–", "N/A" | — | `/v1/rankings?range=30d → networks[].{rank, score, breakdown.*}` (null), each null's reason in `naReason`/`naReasons` (row 78) | exists | The rank prints "–", not "N/A" or "Unranked" as on the strip and leaderboard; one null rule for rank across the page (HOME-53). |
| 12 | StatPill "{N} Networks" | `MEASURED.length` | count | — | `/v1/rankings → networks[]` filtered `kind == "residential"`, length | exists | Today 4 (3 demo + Shifter). |
| 13 | StatPill "{N} Targets" | `DATA.targets` = `dataset.targets.length` (8: framework + catalog targets) | count | — | `/v1/dataset → targets[]` length; or `measuredMarket.targetCount` (framework targets only) | exists-but-different-shape | Only in the dataset file. Inconsistent with the market panel's "{targets} targets" (`market.windows[].targets` = 2, framework only). Propose one `targets` count (measured targets) in a home answer. |
| 14 | NetworkStrip "Ranked by Index Score across {N} networks"; marquee per network: monogram, name, score, "#rank"/"Unranked" | `MEASURED[]`: `.monogram`, `.name`, `.score.composite`, `.standing.rank` | 1 dp | CSS loop, repeated to ≥12 items; static | `/v1/rankings?range=30d → networks[].{name, score, rank}` | exists-but-different-shape | Same rank caveat as row 46. RK `rank` is null where the old `kindStanding` showed #1 of 1 (HOME-19). |
| 14a | ScoreWindow title bar: "Shifter Proxy Index" and a pulsing "Live" label | hard-coded in `pi-hero.tsx` | text | none | `/v1/version → {revision, builtAt}`; every answer's `generatedAt`; dataset file `snapshot.asOf`, `snapshot.refreshSeconds` | missing | Always says "Live", even on a weeks-old export. Same issue as the dock (row 2) and the `IndexHero` window (12.9): HC-12. |

### C. "Live dashboard / The market, measured" section header

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 15 | Headings and "Every figure below follows the time window you pick…" | hard-coded | text | — | — | editorial | — |

### D. Market panel (pi-market.tsx): scope strip, request tile, gauge tile, four metric cards

Scope strip:

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 16 | Tab "All market" + "{count} networks" | `MEASURED.length` | count | tab `all` (default) | `/v1/rankings` residential rows length (`/v1/networks/all → networks` counts every network that published a product group, of any kind, so it is not this count) | exists | — |
| 17 | One tab per network: monogram, name, "#{rank} of {peers} · {score}" or "Unranked · {score}" | `MEASURED[]`: `.monogram`, `.name`, `.standing.{rank,peers}`, `.score.composite` | 1 dp | Click or palette (`pi:scope` event) sets scope; rail scroll buttons; order = MEASURED order | `/v1/rankings?range=30d → networks[].{name, rank, peers, score}` | exists-but-different-shape | `standing` is the old-site `kindStanding` (a lone scored network is "#1 of 1"); rankings' `rank` is the framework rank (null for that lone network, e.g. Shifter `score.rank: null`, `standing {1,1}`). See row 46. |

All-market scope (`marketPanel`, data `DATA.market` + `DATA.market.windows[range]`):

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 18 | Label "Requests in the last 24 hours / 7 days / 30 days", or "the last {n} days" / "the last <1 day" when history is shorter | `w.observedDays` (export `observedDays()`: span from the first `measuredHistory` point to the window `period.end`, ceil days; 0 = under a day; null when it covers the range) | days | follows range | none | missing | Source: snapshot `windows[r].period.end` and the earliest history point (API `measuredHistory`, build/measured_trends.go). Propose `observedDays` per range on `/v1/networks/all → ranges.<r>` and on every network's `stats.<r>`. |
| 19 | Big number: requests (CountUp) | `w.requests` (old `view.fleet.requests`) | integer | follows range | `/v1/networks/all → ranges.<r>.requests`; also `/v1/dataset → measuredMarket.views[r].fleet.requests` | exists | — |
| 20 | Sub "across {networks} networks and {targets} targets" | `w.networks` (= measured leaderboard length), `w.targets` (= `measuredMarket.targetCount`, framework targets) | counts | — | networks: count of `/v1/rankings` residential rows (as row 16); targets: `/v1/dataset → measuredMarket.targetCount` | exists-but-different-shape | targetCount only in the dataset file; the API counts every target with `source == "measured"` (the framework targets, AI sources included), not the 2 of the mock (HOME-4). |
| 21 | Zap icon chip (no score for the market) | — | — | — | — | editorial | — |
| 22 | Area sparkline (96 px), hover tooltip "{pct} {bucket label}" | `w.curve` = old `view.headlineSpark` `{values, labels}`: fleet success history, request-weighted mean of networks' measured histories per age bucket; 24h thinned to 110 points, 7d/30d all points (7 / 30 daily; finer 5-min points replace overlapping days) | fraction → `pct(1 dp)`; labels "5 min ago", "1 day ago", "now" | follows range; hover | `/v1/dataset → measuredMarket.views[r].headlineSpark.{values,labels}` (same build, `measureView`) | exists-but-different-shape | Only in the dataset file. `/v1/networks/all/chart?range=` serves the fleet `success` series instead (every-group fleet entry, `t` minutes, buckets without requests left out, 288 points for 24h) — a different aggregation and no labels. Mock has 6 points for every range (< 1 day of history). |
| 23 | Badge "{value} median success" (tone pos) | `w.medianSuccess` (old `fleet.medianSuccessRate`: median of networks' window success) | fraction, `pct` | follows range | `/v1/dataset → measuredMarket.views[r].fleet.medianSuccessRate` | exists-but-different-shape | `/v1/networks/all → ranges.<r>.successRate` is the pooled successes ÷ requests, not a median. |
| 24 | Meta "{networks} networks · {targets} targets · {ips} unique IPs · {gb} GB transferred" | `w.networks`, `w.targets`, `w.uniqueIps` (old `fleet.uniqueIps`: sum of networks' unique IPs), `w.bandwidthGb` (bytes up+down ÷ 1024³, rounded to 3 dp then `toLocaleString`) | compact (16.98k → "17k"), GB | follows range | uniqueIps: `/v1/networks/all → ranges.<r>.uniqueIps` (distinct across networks); `/v1/dataset → measuredMarket.views[r].fleet.{uniqueIps, bandwidthGb}` | exists-but-different-shape | API per-page uniqueIps is distinct (an IP seen by two networks once), the website shows a sum. bandwidthGb: `/v1/home → market.bandwidthGb`, now in decimal GB (bytes ÷ 10⁹, BANDWIDTH-PER-NETWORK-1), and each network's own on `/v1/home` and `/v1/rankings` `networks[].bandwidthGb` (the rows sum to the market's). Mock: 0.0925 GB renders as "0.092". |
| 25 | Gauge 270°, label "Authentic", value, caption "Exit IPs in the requested country"; tone info if > 0.9 else warn | `DATA.market.authenticity` (old `dataset.measuredMarket.authenticity`: exit-IP-weighted country match, non-datacenter kinds) | fraction, `pct` | not range-dependent | `/v1/dataset → measuredMarket.authenticity` | exists-but-different-shape | Only in the dataset file; 24h IP weights for a 30d gauge. Propose on the home answer. |
| 26 | Gauge stat "Countries" | `DATA.market.countries` (old `measuredTotals.countries`: distinct `geo[].countryCode`) | count | not range-dependent | `/v1/dataset → measuredTotals.countries`; per range `/v1/networks/all → ranges.<r>.exitCountries` | exists-but-different-shape | exitCountries is per range (distinct places seen), the website shows one 30d figure. |
| 27 | Gauge stat "Incidents" | `DATA.market.incidents` (old `measuredTotals.incidents24h`) | count | not range-dependent | `/v1/dataset → measuredTotals.incidents24h` | exists-but-different-shape | Dataset file only; 24h count shown under a 30d window. Per network the API has `stats.<r>.incidentCount`; a fleet per-range count is missing. |
| 28 | Gauge stat "Mis-sold" (amber) | `DATA.market.misSold` (old `measuredTotals.misclassifiedShare` = Σ uniqueIps24h × datacenterAsnShare ÷ Σ uniqueIps24h) | fraction, `pct` | not range-dependent | `/v1/dataset → measuredTotals.misclassifiedShare` | exists-but-different-shape | Dataset file only. |
| 29 | Card "Median latency" value + caption "{name} fastest at {value}" + sparkline (32 px, hover) | `w.medianLatency` (old `fleet.medianLatency`: median of networks' headline p50), `w.fastest.{name,value}` (lowest window `latencyP50`), `w.latencySpark` (24 points, unobserved ends trimmed) | ms (`≥1000` → "1.59 s") | follows range | `/v1/dataset → measuredMarket.views[r].{fleet.medianLatency, fastest.provider.name, fastest.stats.latencyP50, latencySpark}` | exists-but-different-shape | Dataset file only. `/v1/networks/all → ranges.<r>.latencyP50` is light-load fleet p50, a different figure. Mock: latencySpark has a null point (gap kept). |
| 30 | Card "Median success" + "{name} leads at {value}" + sparkline | `w.medianSuccess`, `w.mostReliable.{name,value}` (highest window success), `w.successSpark` (24 points) | `pct` | follows range | `/v1/dataset → measuredMarket.views[r].{fleet.medianSuccessRate, mostReliable.provider.name, mostReliable.stats.successRate, successSpark}` | exists-but-different-shape | Dataset file only; leader could be derived from `/v1/rankings` (`successRate` max). |
| 31 | Card "Cheapest / GB" + "{cheapest name} · most expensive {dearest value}" | `w.cheapest.{name,value}`, `w.dearest.value` (lowest / highest list rate: `pricePerGbAtScale` else `pricePerGb`) | `money` ($0.75) | follows range (value is not range-dependent) | `/v1/dataset → measuredMarket.views[r].{cheapest, dearest}.{provider.name, cost}` | exists-but-different-shape | Dataset file only; `/v1/rankings` has no list rate. Caption names the cheapest network next to the dearest price (website copy quirk). |
| 32 | Card "Residential spread" "{x}x" + "{worst} vs {best}, same job" (tone warn) | `w.spread.{value,best,worst}` (old `view.bills.spreadX` and names) | `x.toFixed(1)` | follows range | `/v1/dataset → measuredMarket.views[r].bills.{spreadX, best.provider.name, worst.provider.name}`; derivable from `/v1/rankings → networks[].costPer1kSuccess` (residential, max ÷ min) | exists-but-different-shape | VALUE-ALL-2: the 8-target spread is `/v1/home → market.spreadAll.{value,best,worst}` (`insights.valueAll`'s), beside the HTTP `market.spread`. |
| 32a | Metric card spark hidden when its series is empty (the card keeps value and caption) | `card.series && card.series.values.length > 0` | — | follows range | `/v1/dataset → measuredMarket.views[r].{latencySpark, successSpark}.values` (empty when no point) | exists-but-different-shape | Empty state; differs from row 80 (fewer than 2 points: dashed line). Same source gap as HOME-15, HOME-16. |

One-network scope (`networkPanel`, data `n` = the MEASURED network, `w = n.windows[range]`):

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 33 | Label "Requests in {phrase}" (observed-days aware) | `w.observedDays` | days | follows range | none | missing | As row 18, per network. |
| 34 | Big number: requests | `w.requests` (old `windowStats.requests` = `metrics.completed`) | integer | follows range | `/v1/networks/{slug} → stats.<r>.requests` (also `ranges.<r>.requests`) | exists | One request per network (18 for the strip). |
| 35 | Sub "{schedule} · {standing}" | `w.schedule` = last " · " part of the tagline for measured networks; standing as row 17 | text | — | `/v1/networks/{slug} → profile.tagline` (client splits); standing `/v1/rankings → rank, peers` | exists-but-different-shape | Propose a `schedule` field (the API builds the tagline from it: "measured on a census, pulse and load schedule"). |
| 36 | Score "{composite}" + grade pill (gradeTone) | `n.score.composite`, `n.score.grade` | 1 dp | not range-dependent | `/v1/networks/{slug} → profile.score.{composite, grade}` or `/v1/rankings?range= → score, grade` (per range) | exists | See row 10 on 30d vs per-range. |
| 37 | Area sparkline (96 px, hover pct + label) | `w.curve`: old `sparkSeries(success history, at most 84 for 24h, else all points; the measured network has 6 five-minute points)`; history = `measuredHistory` (30d daily merged with finer 7d/24h points) | fraction | follows range | `/v1/networks/{slug}/chart?range= → success[].{t,v}` | exists-but-different-shape | Chart serves the range's own `series` (288 five-minute points for 24h, daily otherwise), not the merged `measuredHistory`, no labels, not thinned. Propose a `spark` block per range (up to 84 / 22 points + labels) on a home answer, or a `history` series on `/chart`. |
| 38 | Badge "{value} success rate", tone `successTone` (> 0.8 pos, > 0.6 warn, else neg, N/A flat) | `w.successRate` | `pct` | follows range | `/v1/networks/{slug} → stats.<r>.successRate`; `/v1/rankings → successRate` | exists | Thresholds are website-side. |
| 39 | Meta "{ips} exit IPs · {countries} countries · billed usage unavailable" | `w.uniqueIps`, `w.countries` (= `p.pool.countries`, 30d, same for every range) | compact, count | follows range (countries does not) | `stats.<r>.uniqueIps`; `profile.pool.countries` (or per range `ranges.<r>.exitCountries`) | exists | "billed usage unavailable" is hard-coded copy (API weaknesses carry "Billed usage not collected"). |
| 40 | Gauge "Non-hosting" + caption "Exit IPs in the requested country (DB-IP)" | `n.authenticity` = `trust.residentialAuthenticity` | fraction | not range-dependent | `/v1/networks/{slug} → profile.trust.residentialAuthenticity` | exists | Label says "Non-hosting" but the value is the country match (geo accuracy); website copy issue. |
| 41 | Gauge stats "Countries", "Incidents", "DC ASNs" (amber) | `w.countries`, `n.incidents` (= `p.incidents.length`, null for a measured network whose snapshot has no outages), `n.dcAsnShare` (= `trust.datacenterAsnShare`) | count, count, `pct` | incidents not range-dependent | `profile.pool.countries`; `profile.incidents[]` length (30d, newest 50) or `stats.<r>.incidentCount`; `profile.trust.datacenterAsnShare` | exists | incidents: count client-side, or use the per-range `incidentCount`. |
| 42 | Card "Latency p50" + delta "−1.98%" (green when < 0) + caption: "{latencyTarget} · light load {lightP50}" or "· p90 {p90}", else "p90 … · p99 …" + sparkline (up to 22 points: `sparkSeries(…, 22, …)` keeps at most 22; fewer when the history is shorter: 7 daily points for 7d, and the measured network has 6 five-minute points in every window, 5 for its latency spark) | `w.latencyP50`, `w.latencyDeltaPct`, `w.latencyTarget` (old `latencyNote(stats)`, e.g. "HTTP, under load"), `w.lightLatencyP50`, `w.latencyP90`, `w.latencyP99`, `w.latencySpark` | ms | follows range | `stats.<r>.{latencyP50, latencyDeltaPct, lightLatencyP50, latencyP90, latencyP99}` exist; `stats.<r>.latencyTarget` is the id `"generic_http"`; spark: `/chart?range= → latency[].p50` | exists-but-different-shape | latencyTarget: API gives the target id, the website shows a sentence; propose `latencyNote` (or the website maps id + under-load flag to copy). Spark: chart latency only where the window latency is the HTTP target's alone, unthinned, not merged history. |
| 43 | Card "Success rate" + delta "+1.47 pts" + caption "{n} requests completed" + sparkline (up to 22 points: `sparkSeries(…, 22, …)` keeps at most 22; fewer when the history is shorter: 7 daily points for 7d, and the measured network has 6 five-minute points in every window, 5 for its latency spark) | `w.successRate`, `w.successDeltaPts`, `w.completed` (export key not in the TS interface), `w.successSpark` | pct, pts (2 dp) | follows range | `stats.<r>.{successRate, successDeltaPts, requests}`; spark `/chart → success` | exists-but-different-shape | Values exist; only the spark differs (as row 37): up to 22 points of the merged history. `completed` = `stats.requests`. |
| 44 | Card "$ / GB" (tone pos when value rank 1) + caption "#{rank} of {total} on value · cheapest is {value}" or "Verified pricing not collected" | `w.listRate` (`pricePerGbAtScale` else `pricePerGb`), `w.valueRank` (old cost board rank by `costPer1kSuccess`), `MEASURED.length`, `w.cheapestRate` (min list rate over every provider) | `money(2 dp)` | follows range (rank does) | listRate: `profile.pricing.{pricePerGbAtScale, pricePerGb}`; valueRank: `/v1/rankings?range= → valueRank`; cheapest: `/v1/dataset → measuredMarket.views[r].cheapest.cost` | exists-but-different-shape | Client derives listRate; `total` is the network count, not the number of priced networks. Propose `listRate` on rankings rows and the network profile, and `cheapestListRate` on the home answer. VALUE-ALL-2: the 8-target value rank is `/v1/rankings → networks[].valueRankAllTargets` (by `costPer1kAllTargets`), the bill `profile.pricing.costPer1kAllTargets` with `costBasisAllTargets`; costs are unrounded, GB = 10⁹ bytes. |
| 45 | Card "Pool vs claim" (when claimed pool known; tone > 0.5 pos, > 0.1 warn, else neg) + "{observed} observed against {advertised} advertised"; else card "Unique exit IPs" + "{cities} cities across {countries} countries" | `w.claimShare` = `stats.uniqueIps ÷ pool.claimedPoolSize`, `w.claimed`, `w.uniqueIps`, `w.cities` (= `p.pool.cities`), `w.countries` | pct / compact / count | follows range | `stats.<r>.uniqueIps`, `profile.pool.claimedPoolSize` (catalog `site.trust_signals.claimed_pool_size`), `profile.pool.cities`, `profile.pool.countries` | exists-but-different-shape | claimShare is a client division (the IP count follows the hour formula, FIELDS "IP numbers"); claimedPoolSize is null until catalog-filled. |

### E. Insight deck (pi-insights.tsx) — four cards; lead clamped to 3 lines, click expands; "Method" hint

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 46 | (Rank used everywhere: hero, strip, scope tabs, leaderboard) "#{rank} of {peers}" | `n.standing.{rank,peers}` (SHOW_DEMO: list position / list length; else old `kindStanding`, which makes a lone scored network "#1 of 1") | integer | — | `/v1/rankings → networks[].{position, rank, peers}` | exists-but-different-shape | `rank` is the framework rank among ready networks of the kind (null when the framework leaves it unranked); the "#1 of 1" rule lives only in Go `ScoreView.KindStanding()` (used by search meta). Propose `standing: {rank, peers}` on rankings rows applying that rule, so every surface prints the same rank. |
| 47 | Card 1 "Best value for money" lead: "Over {phrase}, {best} delivers it for {bill}, a {spread}x saving on {worst}…"; fallback lead when no verified price | `DATA.insights.value[range].{best.name, best.bill, worst.name, spreadX}` (old `overviewInsights().bills` = `view.bills`) | `dollars` (cents below $100), spread 1 dp | follows range | `/v1/dataset → measuredMarket.views[r].bills.{best, worst, spreadX}`; derivable from `/v1/rankings?range= → networks[].costPer1kSuccess × 1000` (residential rows) | exists-but-different-shape | bill = cost per 1M successes (`costPer1kSuccess × 1000`). No per-page answer has the bills. VALUE-ALL-2: the 8-target bill per 1,000 successes is `/v1/home → insights.valueAll.{best, worst, spreadX}` (VALUE-ALL-1), also `market.spreadAll` and `/v1/rankings → networks[].costPer1kAllTargets`, `cost.allTargets`. |
| 48 | Card 1 rows: network name, bill, meter `best ÷ bill`, tone band (> 0.6 pos, > 0.3 warn, else neg) | `insights.value[range].rows[].{name, slug, bill}` (every residential network, cheapest first, missing last) | dollars | follows range | `/v1/dataset → measuredMarket.views[r].bills.all[].{provider, bill}`; or rankings `costPer1kSuccess` | exists-but-different-shape | Meter/tone are client-side. Footnote "Retries and failures included…" is copy. VALUE-ALL-2: the 8-target rows are `insights.valueAll.rows[]` (`costPer1k`, `byTarget`), or rankings `costPer1kAllTargets`; `costPer1kSuccess` is unrounded (GB = 10⁹ bytes). |
| 49 | Card 2 "Most authentic pools" lead "{share} of all IPs sold as residential resolve to hosting ASNs… led by {leader} at {value}" | `DATA.market.misSold`, `insights.authenticity[0].{name,value}` | pct | not range-dependent | misSold: `/v1/dataset → measuredTotals.misclassifiedShare`; leader: per network `profile.trust.residentialAuthenticity` | exists-but-different-shape | Needs a fleet value from the dataset plus one value per network (18 requests) and a client sort. |
| 50 | Card 2 rows (top 5 by authenticity, text "These five"): name, pct, meter `(v − 0.75) ÷ 0.25`, tone > 0.95 pos, > 0.9 warn, else neg | `insights.authenticity[].{name, slug, value}` (old `overviewInsights().authenticity`, `trust.residentialAuthenticity`) | pct | not range-dependent | `/v1/networks/{slug} → profile.trust.residentialAuthenticity` (per network) | exists-but-different-shape | Propose `authenticity` on `/v1/rankings` rows (or a ranked list in a home answer). "Bar scaled from 75%" and "five" are hard-coded in pi-insights.tsx. |
| 51 | Card 3 "Toughest target leaders" lead naming the hardest target; foot "Difficulty {level} of 5"; Method hint = pass condition | `insights.hardest.{label, difficulty, passCondition}` (old: the dataset target with the highest difficulty) | difficulty 1–5 | not range-dependent | `/v1/dataset → targets[].{label, difficulty, passCondition}` (difficulty from catalog `site.targets`) | exists-but-different-shape | Dataset file only. Mock picks "Ticketing queue" (difficulty 5), a catalog target no network measures, so the rows are empty. Propose: choose the hardest among measured targets (`engine` not null) and serve it on a home answer. "of 5" is hard-coded. |
| 52 | Card 3 rows: per network success rate on the hardest target, meter = rate, tone > 0.8 pos, > 0.5 warn | `insights.hardestRates[].{name, slug, value}` (old: each network's `targets[hardest].successRate`) | pct | not range-dependent (30d) | `/v1/networks/{slug} → profile.targets[target=<id>].successRate` | exists-but-different-shape | One request per network (18), then a client sort. Mock: `[]`. Propose a per-target ranked list across networks. |
| 53 | Card 3 rows, new: browser score next to the percentage (start migration) | none yet | score 1–10, rank of M | — | `/v1/networks/{slug} → profile.targets[target=<id>].scores.success.{score, rank, rankedOf, status}` (browser targets only; per engine `engines.<engine>.scores.success`; engine used in `engine`) | exists-but-different-shape | Only when the hardest target is a browser target (network targets have no `scores`). Show `status` (`not_enough_data`, `too_few_networks`, `all_similar`) as a text instead of a score. Per network only; same 18-request problem as row 52. For "hardest", the API's difficulty weights are in `profile.browserOverall.<engine>.success.weights`. |
| 54 | Card 4 "Most exclusive pools" lead "{duplication} of the market's combined IP count is double-counted and {resellers} networks look like resold inventory…" | `DATA.market.duplication` (old `measuredTotals.fleetDuplicationShare`), `DATA.market.likelyResellers` (old `measuredTotals.likelyResellers`) | pct, count | not range-dependent | `/v1/dataset → measuredTotals.{fleetDuplicationShare, likelyResellers}` | exists-but-different-shape | likelyResellers counts providers with `overlap.likelyUpstream`, which the API never computes (FIELDS: "always" N/A), so it is always 0, while /overlap counts resellers from the overlap pairs (`overlap-data.ts RESELLERS`: top pair `shareOfOurs` ≥ 0.5 into a larger pool), so the two pages disagree today. Source: the overlap pairs with that rule (OVL-3, W5). |
| 55 | Card 4 rows (top 5 by exclusive share): name, pct, meter, tone > 0.75 pos, > 0.4 warn; sub "{share} inside {upstream}" | `insights.exclusivity[].{name, slug, value (overlap.exclusiveShare), upstream.{name, share}}` | pct | not range-dependent (30d) | `/v1/networks/{slug} → profile.overlap.exclusiveShare`; upstream `profile.overlap.likelyUpstream` | exists-but-different-shape | exclusiveShare per network only (18 requests). `likelyUpstream` is always null in the API; source: the top `overlap.overlaps[]` pair under the /overlap page's rule (`shareOfOurs` ≥ 0.5, larger other pool; OVL-3, W5). |
| 55a | Card 2 fallback lead "Authenticity is the share of exit IPs really located in the country we asked for. No pool has a measured country match yet." | shown when `insights.authenticity[0].value` is missing | text | — | none (empty state over `profile.trust.residentialAuthenticity`) | editorial | Empty state of row 49. |
| 55b | Card 3 fallback lead "…No target in the suite has a published difficulty yet, so there is no hardest target to rank on."; footer falls back to "Method" | shown when `insights.hardest` is null | text | — | none (empty state over `targets[].difficulty`) | editorial | Empty state of row 51. |
| 55c | Card 3 lead with no rows: "{target} is the hardest target in the suite, and these networks handle it best." printed above an empty list | `ins.hardest` set, `ins.hardestRates` = `[]` (today: "Ticketing queue", no network measured) | text | — | none | editorial | Copy claims rows that are not there; needs an empty-rows sentence (HOME-53), or HOME-29's measured-only pick. |
| 55d | Card 3 rows, proposed: challenge and block rates with their scores next to the success rate | none yet | fraction `pct`; score 1–10, "#rank of M" | — (30d) | `/v1/networks/{slug} → profile.targets[target=<id>].{captchaRate, blockRate, outcomes.challenge, outcomes.block, scores.challenge, scores.block}`; per engine `engines.<engine>.{outcomes.*, scores.challenge, scores.block}` | exists-but-different-shape | Browser targets only; status text instead of a score unless `status` = `ranked`. Per network only (18 requests), as row 53; HOME-31. Never add Chrome and Camoufox together. |
| 55e | Card 4 with no data: no fallback lead; with an empty `exclusivity[]` it still says "these five are mostly their own", and a null `likelyResellers` prints "N/A" inside the sentence | `insights.exclusivity`, `DATA.market.likelyResellers` (`missing() ? "N/A"`) | text | — | as rows 54, 55 (HOME-32 to HOME-35) | editorial | Needs an empty-rows lead like cards 2 and 3 (HOME-53). |

### F. Leaderboard (pi-leaderboard.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 56 | Section header "Every network, ranked in the open" + copy | hard-coded | text | — | — | editorial | — |
| 57 | Card header "Network leaderboard" + "{count} networks · ranked by a published formula…" | `MEASURED.length` | count | — | `/v1/rankings → networks[]` residential length | exists | — |
| 58 | Profile tabs (Balanced, Hard targets, Cost first, Latency first, Compliance), title tooltip = description; footer shows the picked profile's description | `DATA.profiles[].{id, label, description, weights}` (export: `dataset.profiles`, catalog `site.ranking_profiles`) | weights 0–100, 8 keys | Picking a profile re-ranks with `reweight()`; before any pick the published order stands (first tab shows as active) | `/v1/dataset → profiles[].{id, label, description, weights}` | exists-but-different-shape | Only in the dataset file. Propose `profiles` on the `/v1/rankings` answer (5 small rows). |
| 59 | Re-ranked order, score, rank "{rank} of {peers}" / "Unranked" under a profile | `reweight(MEASURED, weights)`: score = Σ breakdown[k]·w ÷ Σ w over keys with w > 0; null when composite or any weighted component is missing; rank = 1 + peers of the same kind with a strictly higher score; peers = scored networks of that kind | 0–100, 1 dp | client re-sort, rows animate (`motion layout`) | inputs: `/v1/rankings?range= → networks[].{breakdown, score, kind}` | exists | Client computation by design (instant toggle). Note rankings' breakdown is per range while the website uses the 30d breakdown. |
| 60 | Grade pill under a profile | `gradeFor(score)`: ≥ 88 A+, ≥ 80 A, ≥ 72 B+, ≥ 64 B, ≥ 56 C+, ≥ 48 C, else D; published profile uses `n.score.grade` | grade | — | published: `/v1/rankings → grade` (framework grade) | exists-but-different-shape | The re-weighted grade uses website thresholds; unverified that they match the framework's grade bands. Propose serving the band table (e.g. with `profiles`) so both agree. |
| 61 | Column "#" | rank/peers as row 46 (published) or row 59 (profile) | "1 of 4" | — | `/v1/rankings → rank, peers` | exists-but-different-shape | See row 46. |
| 62 | Column "Network": monogram, name (link `/proxy-index/networks/{slug}`), "Residential · Measured/Demo" | `n.monogram`, `n.name`, `n.slug`, `n.source` | — | — | `/v1/rankings → slug, name, source, kind` | exists | Monogram derived from name. "Demo" never occurs with the API. |
| 63 | Column "Index Score": score + grade pill | `n.score.composite` / reweighted | 1 dp | — | `/v1/rankings?range= → score, grade` | exists | Per range in the API; 30d in the website. |
| 64 | Column "Success": rate + `Delta` "+1.47 pts" (green > 0, red < 0, grey 0/N/A) | `w.successRate`, `w.successDeltaPts` | pct 1 dp, pts 2 dp | follows range | `/v1/rankings?range= → successRate, successDeltaPts` | exists | Aggregate success over all targets (not browser). |
| 65 | Column "Latency p50" + `Delta` inverted ("−2.0%" green); cell tooltip = latency target note | `w.latencyP50`, `w.latencyDeltaPct`, `w.latencyTarget` | ms, pct 1 dp | follows range | `/v1/rankings?range= → latencyP50, latencyDeltaPct`; tooltip: `/v1/networks/{slug} → stats.<r>.latencyTarget` (id only) | exists-but-different-shape | Values exist; the tooltip text is not in rankings (row 42). |
| 66 | Column "Unique IPs" + "{n} countries" | `w.uniqueIps`, `w.countries` | compact, count | follows range (countries 30d) | `/v1/rankings?range= → uniqueIps, countries` | exists | Rankings `countries` is `pool.countries` (30d), as the website. |
| 67 | Column "DC ASNs" coloured > 0.1 rose, > 0.05 amber, else emerald | `n.dcAsnShare` | pct | — | `/v1/rankings → datacenterAsnShare` | exists | Thresholds website-side. |
| 68 | Column "$ / GB" + "#{n} on value" or "Not collected" | `w.listRate`, `w.leaderboardValueRank` (old cost board over the leaderboard networks) | money 2 dp | follows range (rank) | valueRank: `/v1/rankings → valueRank`; listRate: not in rankings | exists-but-different-shape | Propose `listRate` on rankings rows (today 18 extra profile requests). API `valueRank` ranks every provider of every kind (`costBoard` over all entries); the website's `leaderboardValueRank` ranks the residential leaderboard only (W2: `valueRank` within the kind). |
| 69 | Column "Trend": success sparkline (28 px, tone pos if delta ≥ 0 else neg), no hover | `w.successSpark` (up to 22 points of the merged measured history; 7 for 7d, 6 for the measured network) | fraction | follows range | `/v1/rankings?range= → trend[]` (≤ 26 values, the window `success` series downsampled) | exists-but-different-shape | Point count 26 vs up to 22 and basis (window series vs merged `measuredHistory`) differ; acceptable visually. No labels needed (no hover). |
| 70 | Under the trend: tracking label "<1 day tracked" | `n.trackingLabel` (old `measuredTrackingLabel(measurement)`) | text | — | none | missing | Source: snapshot window `period` + first history point. Propose `trackedDays` (number) on rankings rows; the website writes the copy. |
| 71 | Proposed column (not shown yet): browser overall score next to Success | none | score 1–10, "#rank of M" | — | `/v1/networks/{slug} → profile.browserOverall.<engine>.success.{score, rank, rankedOf, status}` | exists-but-different-shape | Per network only; not in rankings. Propose `browserOverall` on rankings rows. Never add Chrome and Camoufox together (one column per engine or the network's `engine`). |
| 72 | Sorting | fixed: published order (score desc) or profile order; no clickable column sorts; mobile pins # and Network columns (`pinColumns`) | — | — | `/v1/rankings → position` | exists | — |
| 73 | Footer "Success is the request-weighted mean across all {targets} targets, over {phrase}." | `DATA.targets` (8) | count | follows range | `/v1/dataset → targets[]` length | exists-but-different-shape | Dataset file only. API `successRate` is the time-weighted mean of slot rates (FIELDS: "every-request snapshots: the mean of the time-slot success rates"), so "request-weighted" is wrong copy; browser targets never enter a network's window success, so "across all {targets} targets" is wrong too; and it counts catalog targets that are not measured. |

### G. Live tape panel (pi-tape.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 74 | Header "Live tape", link "Open the full tape", live dot | — | — | — | — | editorial | — |
| 75 | Sub "Live test feed" (any row `source == measured`) or "Simulated request feed" · "{age} ago" (newest = min `agoSeconds`) | `DATA.tape[].source`, `.agoSeconds` | `ago()`: s / m / h / d | — | `/v1/dataset → tape[].{source, agoSeconds}`; per network `/v1/networks/{slug} → tape[]` | exists-but-different-shape | Only in the dataset file (200 rows) or per network (20 rows). `agoSeconds` is frozen at build time (relative to `generatedAt`); with live data the website should compute age from `at`. No polling on the home tape. |
| 76 | Filter chips All / Census / Pulse / Incidents / Audit | `FILTERS` (hard-coded), filters `e.kind` | enum | client filter | `/v1/events?category=census\|pulse\|incident\|audit` (raw rows); `tape[].kind` uses the same four values | exists-but-different-shape | `/v1/events` returns raw framework `TestEvent` (`kind`, `category`, `data`), not the mapped title/detail/value; the mapping (Go `TapeFromEvents`) is only applied to `tape[]` in the dataset and network answers. Icon map also has `pricing` (no such API category). |
| 77 | Row: icon chip, title, detail, value (tone by `signal` > 0 pos, < 0 neg, else flat), age | `DATA.tape[].{id, kind, provider, title, detail, signal, value, agoSeconds}` (export: old `tapePage(dataset.tape, {}).rows`, first page = 25 rows) | text | newest first; scroll inside 560 px | `/v1/dataset → tape[].{id, kind, providerName, title, detail, signal, value, agoSeconds, at}` | exists-but-different-shape | Shape matches (`provider` ← `providerName`) but only in the 9 MB file. Propose a mapped tape page: `/v1/tape?category=&since=&limit=25` (or `tape` in a home answer) with `title/detail/value/signal/at`. Mock: 25 rows, all measured Shifter. |
| 77a | Empty filter: a chip with no matching rows shows an empty list, no message | `DATA.tape.filter(e => filter === "all" \|\| e.kind === filter)` | — | client filter | `/v1/events?category=` (empty `events[]`) | editorial | Empty state; needs copy (HOME-53). With the 25 preloaded rows all from one network, Incidents and Audit are often empty. |
| 77b | Zero tape rows: the header age reads "Infinityd ago" (`Math.min(...[])` is `Infinity`); the chips filter only the 25 bundled rows, never request a category | `newest = Math.min(...DATA.tape.map(e => e.agoSeconds))`, `DATA.tape.filter(...)` | text | client filter | `/v1/events?category=` (per-category rows, often empty) | editorial | Website must guard the empty list ("No events yet") and, once HOME-43 serves per-category rows, show the empty-filter copy of 77a. |

### H. Shared helpers that shape every figure (pi-data.ts, pi-charts.tsx, pi-ui.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 78 | N/A rule | `missing()`: null, undefined or non-finite → "N/A" | — | — | API: every missing value is JSON `null` (+ `na[]` paths, each with a reason code: `naReason`, `naReasons`) | exists | Matches. An empty score, grade, rank or breakdown value says why (SCORE-REASONS-1): `no_pulse_tests`, `no_load_test`, `required_target_missing`, `no_price_evidence`, `few_requests`, `low_time_coverage` or `expired` (api/answers/README.md, "Missing data"). A page's N/A state can name it ("no load test yet") instead of "insufficient data". |
| 79 | Number formats | `pct` (×100, 1 dp), `ms` (< 1000 "N ms", else "N.NN s"), `compact` (B/M/k), `count`, `money` (4/3/2 dp by size), `fixed`, `signed` (U+2212 minus), `ago` | — | — | API serves raw numbers (fractions, ms, dollars) | exists | Units match the API: rates are fractions, latency ms, prices dollars, deltas pts / pct. |
| 80 | Sparkline | `{values, labels}`; < 2 non-null points → dashed baseline; hover tooltip `format(v) + labels[i]` when `format` given; interior nulls skipped | — | hover | API spark shape `SparkSeries {values, labels}` exists only in `measuredMarket.views`; chart answers give `{t, v}` (minutes ago) | exists-but-different-shape | If the website moves to chart answers it must build labels from `t` (Go `bucketLabel` rules: "now", "N min ago", "Hh Mm ago", "N days ago"). |
| 81 | Gauge (270° dial, 0–1 clamp), Meter (min 2% width), CountUp (rolls from 0 / previous value), Delta (green when good, `invert` for latency), Pill, IconChip | — | — | animation only | — | editorial | — |
| 82 | Tone palette `TONE` and thresholds | `successTone` (0.8 / 0.6), `gradeTone`, market gauge (0.9), DC ASN (0.1 / 0.05), claim share (0.5 / 0.1), insight bands (value 0.6/0.3, authenticity 0.95/0.9 with 0.75 floor, hardest 0.8/0.5, exclusivity 0.75/0.4) | — | — | none | editorial | Hard-coded display thresholds; not data. Listed so the API team does not need to serve them. |

### I. Modules in the task list that the island imports indirectly but does not render on /proxy-index

`pi-network-cards.tsx` is imported by pi-profile-preview.tsx and pi-networks.tsx (the /proxy-index/networks page); `pi-profile-preview.tsx` is rendered by NetworksContent.astro. Neither is on /proxy-index. Their data needs, for the page that renders them:

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 83 | NetworkCard: rank numeral (podium gold top 3), monogram, name, tagline, score + grade, score meter | `n.standing.rank`, `.monogram`, `.name`, `.tagline`, `.score.{composite,grade}` | — | follows range (figures) | `/v1/rankings → rank, name, score, grade`; tagline `/v1/networks/{slug} → profile.tagline` | exists-but-different-shape | Tagline is not in rankings (18 requests or the dataset file). |
| 84 | NetworkCard six rated figures: Success, Latency p50, $ / GB, Unique IPs, Countries, DC ASNs, coloured Good/Fair/Weak by fixed `SCALES` (success 0.75/0.9, latency 500/1000 ms, price $2/$5, pool 1e5/1e6, countries 15/50, DC 0.03/0.06) | `w.successRate`, `w.latencyP50`, `w.listRate`, `w.uniqueIps`, `w.countries`, `n.dcAsnShare` | — | range | `/v1/rankings?range= → successRate, latencyP50, uniqueIps, countries, datacenterAsnShare`; listRate missing there | exists-but-different-shape | listRate as row 68. SCALES are hard-coded rating bands (editorial). |
| 85 | ProfilePreview: top 3 cycle (4.2 s), rank, score, grade, "Success rate · 30 days" value + 30d curve (64 px area) | `NETWORKS.slice(0,3)`, `windowOf(n.windows, "30d")`: `.successRate`, `.curve` | pct | fixed 30d (hard-coded) | `/v1/rankings?range=30d`; curve `/v1/networks/{slug}/chart?range=30d → success` | exists-but-different-shape | Curve as row 37. |
| 86 | ProfilePreview bars: Latency p50, Unique IPs, "Cost per 1,000 successes"; fill = share of the field's best (lower-is-better inverted, min 4%) | `w.latencyP50`, `w.uniqueIps`, `w.costPer1kSuccess` over every network's 30d window | ms, compact, money | 30d | `/v1/rankings?range=30d → latencyP50, uniqueIps, costPer1kSuccess` | exists | One rankings call covers the whole field. |

### J. home.json: every key, its provenance (export-home.mts) and its API source

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 87 | (not shown by the island) JSON-LD `dateModified` in index.astro | `generatedAt` (old `dataset.generatedAt`) | ISO time | — | every answer's `generatedAt`; `/v1/version → builtAt` | exists | — |
| 88 | not shown | `source` ("proxy-reviews siteapi /v1/dataset"), `ranges[] {id, short, days}` | — | — | `stats.<r>.range.{id, short, days}` | exists | Unused by the island (it uses its own `RANGES`). |
| 89 | Hero requests | `run[] {range, requests}` (old `window.ts fleetWindows`) | — | per range | `/v1/networks/all → ranges.<r>.requests` | exists | Row 8. |
| 90 | Market All scope | `market.{authenticity, countries, incidents, misSold, duplication, likelyResellers}` (old `measuredMarket.authenticity`, `measuredTotals.{countries, incidents24h, misclassifiedShare, fleetDuplicationShare, likelyResellers}`) | — | — | `/v1/dataset → measuredMarket.authenticity`, `measuredTotals.*` | exists-but-different-shape | Rows 25–28, 49, 54. |
| 91 | Market All scope | `market.windows[] {range, observedDays, requests, networks, targets, curve, medianSuccess, uniqueIps, bandwidthGb, medianLatency, latencySpark, successSpark, fastest, mostReliable, cheapest, dearest, spread}` (old `measuredMarketViews()`) | — | per range | `/v1/dataset → measuredMarket.views[]` (same port, `FleetView`), except `observedDays` | exists-but-different-shape | Rows 18–32. `views` is raw JSON with full fleet series too (`fleet.success`, `fleet.latency`). |
| 92 | Strip, scope, leaderboard, cards | `networks[] {slug, name, monogram, kind (primaryKind), source, tagline, score {composite, grade, rank}, standing {rank, peers}, breakdown (8 keys, 2 dp), authenticity, dcAsnShare, incidents, pricingModel, trackingLabel}` | — | — | rankings (`slug, name, kind, source, score, grade, rank, peers, breakdown, datacenterAsnShare`), `/v1/networks/{slug} → profile.{tagline, trust.residentialAuthenticity, incidents[], pricing.model}` | exists-but-different-shape | `monogram` derived; `standing` row 46; `trackingLabel` missing (row 70); `pricingModel` unused by the island. |
| 93 | Network scope, leaderboard | `networks[].windows[] {range, observedDays, requests, schedule, curve, successRate, uniqueIps, countries, cities, latencyP50/P90/P99, lightLatencyP50, latencyTarget, latencyDeltaPct, successDeltaPts, latencySpark, successSpark, listRate, valueRank, cheapestRate, fleetMedianSuccess, claimShare, claimed, completed, leaderboardValueRank, costPer1kSuccess}` (old `windowStats`, `costBoards`, `fleetWindow`, `measuredHistory`, `latencyNote`) | — | per range | `/v1/networks/{slug} → stats.<r>.*` + `profile.pool/pricing`; `/v1/rankings → valueRank, costPer1kSuccess`; `/chart` for series | exists-but-different-shape | Rows 33–45, 64–69. `fleetMedianSuccess` is exported but never read. |
| 94 | (SHOW_DEMO off) leaderboard membership | `measured[]` (slugs of old `measuredLeaderboard`) | — | — | `/v1/rankings → networks[]` (all measured) | exists | — |
| 95 | Leaderboard profiles | `profiles[]` | — | — | `/v1/dataset → profiles[]` | exists-but-different-shape | Row 58. |
| 96 | Breakdown labels | `scoreLabels` (old data.ts `scoreLabels[k].label`) | — | — | none | editorial | Row 11. |
| 97 | Insights | `insights.{value[], authenticity[], hardest, hardestRates[], exclusivity[]}` (old `overview.ts overviewInsights`) | — | value per range | none as such; inputs in the dataset file and per network answers | missing | No insights answer; rows 47–55. |
| 98 | Targets count | `targets` (`dataset.targets.length`) | count | — | `/v1/dataset → targets[]` | exists-but-different-shape | Rows 13, 73. |
| 99 | Tape | `tape[]` (old `tapePage(dataset.tape, {})` first page, 25 rows) | — | — | `/v1/dataset → tape[]` (200 rows) | exists-but-different-shape | Rows 75–77. |

### Further details (website rules, no API gap)

- Card 3 with no hardest target keeps the "Method" label but loses its hint (`footnote: ins.hardest?.passCondition` is undefined); row 55b's footer falls back to the label only.
- `IconChip` ignores `tone`, and `Meter` draws only neg versus blue, so several "tone warn/pos" rules above are not visible.
- `completed` is null for demo networks ("N/A requests completed"); `leaderboardValueRank` covers Shifter only ("Not collected" for the others).
- The "All market" sub-label says "1 networks" while the pills say 4 (two counts, HOME-4 and row 20).
- Tape row 77 lists `provider`, which is not rendered; in the mock, Incidents and Audit hold 7 and 1 rows.
- Profile labels, descriptions and score labels are used as translation keys (as TGT-14).
- `windowOf` falls back to the last window when a range is missing.
- Card 1 reads "a 1.0x saving" with one priced network.
- "the last 30 days" is also hard-coded in card 1 and the leaderboard footer; the tape panel ignores the dock range.

### Gaps summary

1. HOME-1 (dock-freshness): data age / live state in the dock — show `generatedAt`, poll `/v1/version` — exists in the API, website change only.
2. HOME-2 (search-local): palette scores networks locally over bundled data — use `/v1/search?q=` (or `/v1/search-index`) and add `tagline` to search items — site build.
3. HOME-3 (dataset-download): "Download the dataset" serves home.json — point to `/v1/dataset` or a home answer — decision.
4. HOME-4 (targets-count): one measured-targets count (dataset has 8 incl. catalog, market has 2) — `/v1/home → targets` (count of `targets[]` with `engine` set) — `public.targets`.
5. HOME-5 (observed-days): "the last N days / <1 day" for the fleet and each network — `observedDays` on `/v1/networks/all → ranges.<r>` and `/v1/networks/{slug} → stats.<r>` — snapshot `windows[r].period` + first history point (`measuredHistory`).
6. HOME-6 (market-target-count): `measuredMarket.targetCount` only in the dataset file — `/v1/home → market.targets` — `public.targets`.
7. HOME-7 (market-curve): fleet headline spark (110 points for 24h) only in the dataset file — `/v1/home?range= → market.curve {values, labels}` — `measuredMarket.views[r].headlineSpark`.
8. HOME-8 (market-median-success): median of network success only in the dataset file — `/v1/home → market.medianSuccess` — `views[r].fleet.medianSuccessRate`.
9. HOME-9 (market-unique-ips): website sums networks' IPs, API per-page answer is distinct — pick one; serve `market.uniqueIps` (prefer distinct `/v1/networks/all ranges.<r>.uniqueIps`) — fleet `*` entry.
10. HOME-10 (market-bandwidth): GB transferred — `/v1/home → market.bandwidthGb`, and per network `networks[].bandwidthGb` on `/v1/home` and `/v1/rankings`, `stats.<r>.bandwidthMb` and `profile.performance.bandwidthMb24h` on `/v1/networks/{slug}` (decimal: GB = 10⁹ bytes, MB = 10⁶; BANDWIDTH-PER-NETWORK-1) — snapshot `metrics.bytes_up + bytes_down`. The website reads no network's bandwidth on `/v1/targets`, `/v1/overlap` or the dataset file's `providers[]` (whose `performance.bandwidthMb24h` is filled the same way), so those get no `bandwidthGb`.
11. HOME-11 (market-authenticity): fleet country-match gauge only in the dataset file — `/v1/home → market.authenticity` — `measuredMarket.authenticity`.
12. HOME-12 (market-countries): one 30d distinct-countries figure only in the dataset file (per range `exitCountries` exists) — `/v1/home → market.countries` or use `ranges.<r>.exitCountries` — `measuredTotals.countries`.
13. HOME-13 (market-incidents): fleet incidents (24h) only in the dataset file, not per range — `/v1/home?range= → market.incidents` — outages (`incidents[]`).
14. HOME-14 (market-missold): mis-sold share only in the dataset file — `/v1/home → market.misSold` — `measuredTotals.misclassifiedShare`.
15. HOME-15 (market-latency-card): median latency, fastest network, 24-point spark only in the dataset file — `/v1/home → market.{medianLatency, fastest, latencySpark}` — `views[r]`.
16. HOME-16 (market-success-card): most reliable network and 24-point spark only in the dataset file — `/v1/home → market.{mostReliable, successSpark}` — `views[r]`.
17. HOME-17 (market-price-card): cheapest / dearest list rate and names only in the dataset file — `/v1/home → market.{cheapest, dearest}` — `views[r].cheapest/dearest`.
18. HOME-18 (market-spread-card): residential spread and best/worst names only in the dataset file — `/v1/home → market.spread` — `views[r].bills`.
19. HOME-19 (scope-standing): lone scored network shows "#1 of 1" in the website, rankings `rank` is null — `standing {rank, peers}` on `/v1/rankings` rows (Go `KindStanding` rule) — snapshot `scores.balanced` + `inputs.peers`.
20. HOME-20 (net-schedule): schedule text is split from the tagline — `schedule` on `/v1/networks/{slug} → profile` and in the home answer — measured copy rules.
21. HOME-21 (net-curve): per-network curve (84 points 24h / all points) from the merged measured history — `/v1/home?range= → networks[].curve` or `/chart → history` — `measuredHistory`.
22. HOME-22 (net-sparks): latency and success sparks of up to 22 points (fewer when the history is shorter) whose labels carry each point's real time — `/v1/home?range= → networks[].{latencySpark, successSpark}` — `measuredHistory`.
23. HOME-23 (net-latency-note): "HTTP, under load" note; API gives only `latencyTarget: "generic_http"` — add `latencyUnderLoad` (bool) or `latencyNote` to `stats.<r>` and rankings rows — `metrics.latency_under_load_ms`.
24. HOME-24 (list-rate): `$ / GB` (lowest list rate) derived client-side, absent from rankings — `listRate` on `/v1/rankings` rows and `profile.pricing` — snapshot `products[].pricing.lowest_per_gb`/`entry_per_gb`.
25. HOME-25 (cheapest-rate): "cheapest is $X" across all networks only in the dataset file — `/v1/home → market.cheapest.cost` (or min of rankings `listRate`) — as 24.
26. HOME-26 (claim-share): pool vs claim is a client division — `claimShare` on `stats.<r>` — `site.trust_signals.claimed_pool_size` + window unique IPs.
27. HOME-27 (value-bills): bills per 1M successes, best/worst/spread per range only in the dataset file — `/v1/home?range= → insights.value {best, worst, spreadX, rows[]}` — `views[r].bills` (or rankings `costPer1kSuccess × 1000`).
28. HOME-28 (authenticity-list): top networks by country match need 18 network requests — `authenticity` on `/v1/rankings` rows or `/v1/home → insights.authenticity[]` — `trust.residentialAuthenticity` (`w.pool.geo_accuracy`).
29. HOME-29 (hardest-target): hardest target picked from catalog difficulty, can be unmeasured — `/v1/home → insights.hardest {id, label, difficulty, passCondition, engine}` chosen among measured targets — `site.targets.difficulty` + `public.targets`.
30. HOME-30 (hardest-rates): each network's success on the hardest target needs 18 requests — `/v1/home → insights.hardestRates[] {slug, name, successRate}` — `w.targets[id].metrics.success_rate`.
31. HOME-31 (hardest-scores): browser score next to the hardest-target rate — `/v1/home → insights.hardestRates[].{engine, captchaRate, blockRate, scores.{success, challenge, block}, engines.<engine>.scores}` (each `{score, rank, rankedOf, status}`) — `w.targets[id].browser.scores` (served today only per network: `profile.targets[].scores`).
32. HOME-32 (duplication): double-counted share of the fleet's IPs only in the dataset file — `/v1/home → market.duplication` — `measuredTotals.fleetDuplicationShare`.
33. HOME-33 (resellers): likely-reseller count always 0 — `market.likelyResellers` — the overlap pairs with the /overlap page's rule (OVL-3); the home and /overlap disagree until both read W5.
34. HOME-34 (exclusivity-list): exclusive share per network needs 18 requests — `exclusiveShare` on `/v1/rankings` rows or `/v1/home → insights.exclusivity[]` — `w.overlap.exclusive_share`.
35. HOME-35 (likely-upstream): "{share} inside {upstream}" always null — `overlap.likelyUpstream` — the top `overlaps[]` pair under the OVL-3 rule (`shareOfOurs` ≥ 0.5, larger other pool).
36. HOME-36 (profiles): ranking profiles only in the dataset file — `profiles[]` on `/v1/rankings` — `site.ranking_profiles`.
37. HOME-37 (grade-bands): re-weighted grade uses website thresholds (88/80/72/64/56/48) — serve the grade band table with `profiles` — framework methodology settings (unverified).
38. HOME-38 (leaderboard-latency-tooltip): latency target note absent from rankings — as 23 on rankings rows — as 23.
39. HOME-39 (leaderboard-trend): 26-point window-series trend vs 22-point merged history — accept `trend`, or thin to 22 from `measuredHistory` — `measuredHistory`.
40. HOME-40 (tracking-label): "<1 day tracked" missing — `trackedDays` on `/v1/rankings` rows — snapshot window `period` + first history point.
41. HOME-41 (browser-overall): per-network overall browser score not in rankings — `browserOverall.<engine>.success {score, rank, rankedOf, status}` on `/v1/rankings` rows — `w.browser_overall`.
42. HOME-42 (leaderboard-targets): footer target count only in the dataset file, and says "request-weighted … across all {targets} targets" while success is time-weighted and excludes browser targets — as 4, plus copy fix (as CMP-18) — `public.targets`.
43. HOME-43 (tape-mapped): mapped tape rows (title/detail/value/signal) only in the dataset file or per network; `/v1/events` is raw — `/v1/tape?category=&since=&limit=` or `/v1/home → tape[]` (each tab's newest 10 rows, HOME-TAPE-1) — `public.test_events` via Go `TapeFromEvents`. Per-category answers are often empty: the panel needs the empty states of rows 77a and 77b.
44. HOME-44 (tape-age): `agoSeconds` frozen at build time — serve `at` (exists on `tape[]`) and compute age on the client — website change.
45. HOME-45 (spark-labels): chart answers give `t` minutes without labels — add `labels` (Go `bucketLabel`) where sparks are served — build.
46. HOME-46 (card-tagline): network cards (networks page) need `tagline`, not in rankings — `tagline` on `/v1/rankings` rows — `site.providers.tagline` / generated.
47. HOME-47 (insights-answer): no answer for the four insight cards at all — `/v1/home?range= → insights` (rows 47–55 together) — dataset providers + `measuredMarket`.
48. HOME-48 (show-demo): `SHOW_DEMO = true` puts the old site's three residential demo networks into the hero, strip, scope strip, leaderboard, side-by-side and "why" rows; the API has no demo networks — website drops the switch — no API change.
49. HOME-49 (score-range): hero score window, scope tabs, network panel score and leaderboard show the 30d score, grade, rank and breakdown at every range while the figures beside them follow the dock — read `?range=30d` and label it, or follow the range (owner decision) — RK already serves each range.
50. HOME-50 (net-incidents-range): the network panel's "Incidents" counts the whole `incidents[]` list (newest 50, 30d) at every range — read `stats.<r>.incidentCount` — exists, website change.
51. HOME-51 (market-gauge-range): the gauge tile (authenticity, countries, mis-sold) is one figure at every range while the header says "Every figure below follows the time window you pick" — `/v1/home?range= → market.{authenticity, countries, misSold}` per range, or copy that names their window — snapshot `windows[r].pool.geo_accuracy` and `w.trust` per provider, `ranges.<r>.exitCountries`.
52. HOME-52 (hero-vs-market-requests): hero and market request totals differ (24h 187,263 vs 195,275) from two old helpers (row 8) — both read one field (`/v1/networks/all → ranges.<r>.requests`, or `/v1/home → market.requests` once W6 exists) — no API change.
53. HOME-53 (home-copy-states): website copy and null states: "Non-hosting" gauge label over the country-match value (row 40), card 3 lead "these networks handle it best" over an empty list (55c), the tape's empty filter with no message (77a) and "Infinityd ago" with no rows (77b), card 4's lead over an empty list (55e), the hero phrase ignoring `observedDays` (needs HOME-5), rank "–" vs "Unranked" (11a) — no API change.

### Performance notes

- Today: zero API calls. The island is hydrated (`client:load`) from home.json bundled into the JS (3,471 lines; ~25 tape rows, 5 networks × 3 windows with series of up to 84 / 22 / 22 points, fewer where the history is shorter). The only runtime fetch is `/proxy-index/search.json?locale=` once per visit (website route, `max-age=300`). No `/v1/version` polling.
- With today's API, rendering this island needs: `/v1/rankings?range=` (×1 per range picked; 18 rows, ~26 trend points each, a few KB gzip) + `/v1/networks/all` (1) + the dataset file for `measuredMarket`, `measuredTotals`, `profiles`, `targets`, `tape` (about 9 MB, 0.5 MB gzip: far too big for a page view) + `/v1/networks/{slug}` ×18 (tagline, authenticity, exclusivity, hardest-target rate, claimed pool, cities, list rate, incidents) + `/v1/networks/{slug}/chart?range=` ×18 per range for curves and sparks (24h: 288 points × 4 series each). That is ~37 requests and the dataset file per view, so the home cannot be built from per-page answers today. Every "all networks at once" need (scope strip panels, authenticity, exclusivity, hardest-target rates, list rates, tagline) currently means one request per network.
- Proposal: one precomputed `/v1/home?range=24h|7d|30d` answer (3 bodies per build, compressed once like every answer):
  - `market`: requests, observedDays, networks, targets, curve (110 points 24h / 7 / 30), medianSuccess, uniqueIps, bandwidthGb, medianLatency, latencySpark (24), successSpark (24), fastest, mostReliable, cheapest, dearest, spread, authenticity, countries, incidents, misSold, duplication, likelyResellers.
  - `networks[]` (18): slug, name, kind, standing, score, grade, breakdown, tagline, schedule, and the range's panel figures (requests, observedDays, successRate/delta, latency p50/p90/p99/light/delta/note, uniqueIps, countries, cities, listRate, valueRank, claimed, claimShare, authenticity, dcAsnShare, incidents, trackedDays, browserOverall) with curve (84 points 24h, else all) and two 22-point sparks. Share one `labels` array per series length per range instead of repeating labels per network.
  - `insights` (value bills, authenticity top 5, hardest target + rates + scores, exclusivity top 5), `profiles` (5), `tape` (first 25 mapped rows with `at`), `targets` count.
  - Size: 18 × (84 + 22 + 22) ≈ 2,300 numbers + ~60 scalars per network ≈ 40–70 KB raw per range, ~10–15 KB gzip; well under `MaxAnswerBytes` (200 KB). Precompute in the build next to `rankings()` and `measuredMarket()`, from the same `stats` and `measuredHistory` (no request-time work). The leaderboard can keep using `/v1/rankings?range=` if it gains `listRate`, `standing`, `trackedDays`, `authenticity`, `exclusiveShare`, `browserOverall`, `tagline` and the answer carries `profiles`.
  - Range toggles then cost one cached request each (or ship all three ranges in one ~40 KB gzip body to make toggles instant, as the website does today).
- Keep the tape live without the dataset file: a mapped `/v1/tape?since=&limit=25` (or `/v1/events` with the Go mapping applied) polled while visible; and poll `/v1/version` (5 s cache) to refetch `/v1/home` when the data changes.

## 2. /proxy-index (part 2): the Proxy Index home's server-rendered reading content, SEO data, and the shared chrome every Proxy Index page uses

This part covers everything on `src/pages/proxy-index/index.astro` except the React island `ProxyIndexHome`, which part 1 covers. It also covers the shared chrome: search, the dataset download, OG images and the `page/*` components.

Source files read (website-relative):
- `src/pages/proxy-index/index.astro`
- `src/components/proxy-index/ProxyIndexContent.astro`
- `src/components/proxy-index/TestRig.astro`
- `src/components/proxy-index/buyers-guide-art.ts`
- `src/components/proxy-index/pi-faq.ts`
- `src/components/proxy-index/pi-data.ts`: types, `DATA`, `MEASURED`, formatters, `gradeFor`, `SHOW_DEMO`
- `src/components/proxy-index/pi-palette.tsx`
- `src/components/proxy-index/pi-network-strip.tsx`
- `src/components/proxy-index/pi-network-cards.tsx` (`NETWORKS`)
- `src/components/proxy-index/pin-columns.ts` (behaviour only)
- `src/pages/proxy-index/search.json.ts`
- `src/pages/proxy-index/dataset.json.ts`
- `src/pages/og/proxy-index/[key].astro`
- `src/components/proxy-index/review/review-data.ts` (`reviewFor`, `NETWORKS`, `ALL`)
- `src/components/proxy-index/targets/targets-data.ts` (`TARGETS`, `copyFor`)
- `src/components/proxy-index/guides/guides.ts` (`guideItems`, `guideBySlug`)
- `src/components/proxy-index/page/`: `IndexHero.astro`, `IndexSection.astro`, `IndexDetails.astro`, `IndexStepBar.astro`, `IndexNetworkPicker.astro`, `IndexRule.astro`, `IndexIcon.astro`, `IndexRangeStyles.astro`, `index-dock.tsx`, `index-countup-client.ts`, `index-spark-client.ts`, `index-steps-client.ts`, `index-tabs-client.ts`, `index-network-picker-client.ts`, `index-reveal-client.ts`, `copy-client.ts`, `ui.ts`
- `src/data/proxy-index/home.json`
- `scripts/proxy-index/export-home.mts`
- `src/lib/` imports of the Proxy Index pages: `jsonld`, `blog-slugs`, `authors`, `hero-height`. None of them carries Index data.
- `astro.config.*`: `output: 'server'`, so every page view is rendered on the server.

API files checked:
- `api/README.md` (Endpoints) and `api/FIELDS.md` (Dataset, Provider, Browser targets, Browser scores, Records, Deviations 9, Per-page answers, dataset file)
- `api/internal/build/types.go`, `rankings.go`, `pages.go` (`NetworkRow`), `search.go`, `profile.go`, `browser.go`, `build_steps.go`
- `api/internal/model/dataset.go`, `model_snapshot.go`, `model_provider.go`, `target.go`
- `api/internal/snap/snap.go` (`Methodology.Settings`)
- `api/internal/server/server.go` (routes)
- framework `src/proxy_index/calculated/policy.py` (`grade`, `[grades]`) and `configs/methodology.toml`

Shorthands used in the tables:
- `DATA` is `home.json`, exported by `export-home.mts` from the old site's libraries.
- `MEASURED` is `DATA.networks` with `kind == "residential"`, sorted by `score.composite` (highest first), with `standing.rank` recomputed as i+1. `SHOW_DEMO = true` keeps the demo networks in.
- `w(n)` is `windowOf(n.windows, '30d')`.

### 1. Head: SEO, JSON-LD and OG (`index.astro`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1.1 | `<title>`, meta description, keywords, canonical `https://shifter.io/proxy-index`, `noindex=true`, breadcrumbs | `index.astro` literals through `t()` | text | per locale | none | editorial | The description names the metrics (success rate, latency, unique IPs, ASNs, cost per success). No numbers. |
| 1.2 | JSON-LD `Dataset.dateModified` | `DATA.generatedAt` (`home.json → generatedAt`, from old `dataset.generatedAt`) | ISO time | none | `/v1/rankings?range=30d → generatedAt` (every answer carries it) | exists | It is the snapshot `as_of` (FIELDS.md, Dataset). Any one answer the page already fetches can supply it. |
| 1.3 | JSON-LD `variableMeasured` (Success rate, Latency p50, Unique IPs, Countries, DC ASN share, Price per GB, Index Score), `keywords`, `name`, `isAccessibleForFree`, creator/publisher `ORG_ID` | literals in `index.astro`, `src/lib/jsonld` | text | none | none | editorial | These are labels, not values. |
| 1.4 | JSON-LD `distribution.contentUrl` | literal `/proxy-index/dataset.json` (the website route, see section 13) | URL | none | `/v1/dataset` (302 to `/v1/files/dataset-<sha256>.json`) | exists-but-different-shape | The website download serves the old `home.json`, not the API dataset file. See 13.1. |
| 1.5 | JSON-LD `FAQPage` (Layout `faq={faqItems}`) | `proxyIndexFaq(locale, DATA.targets)` | text | none | see section 8 | editorial | The same list as the visible FAQ. The only data value is the target count (8.1). |
| 1.6 | OG image URL `/og/proxy-index/overview.png` | literal key `overview` | URL | uses the request host | none | editorial | The card copy is fixed (see 10.1). |

### 2. How we test (`ProxyIndexContent.astro #how-we-test`, `TestRig.astro`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 2.1 | Link text "the same {targets} targets", which links to /proxy-index/targets | `DATA.targets` (`home.json → targets` = old `dataset.targets.length`; 8 today) | integer | none | dataset file `targets[]` (length), or `measuredMarket.targetCount` (counts framework targets); no small answer carries a count | exists-but-different-shape | Today the count lives only in the 9.2 MB dataset file. `/v1/search-index` lists targets too, but in the old site's shape. The meaning also needs a decision. The API's `targets[]` includes the browser targets and the AI source targets (`group` set: `ai_overview`, `ai_chatgpt`, `ai_gemini`) and catalog-only rows. Options are the cards (`group == null`), every measured target, or `targetCount`. Proposal: `/v1/meta → targetCount` plus `cardTargetCount` (HC-3). |
| 2.2 | Lead sentence ("same requests ... nothing is self-reported") | `t()` literal | text | none | none | editorial | |
| 2.3 | TestRig: 4 stage cards (Census / Unique IPs, Pulse / Success and latency, Load / Under pressure, Audit / Authenticity), with bodies | `TestRig.astro` `stages` literals | text | 12 s CSS cycle, 3 s per stage | none | editorial | |
| 2.4 | TestRig illustration: 3 networks drawn as "A", "B", "C"; 4 target labels Search, Shops, Social, Tickets; audit verdicts (green or red); "SHIFTER RIG" | hard-coded in `TestRig.astro` (`nets`, `targets`, `verdicts`) | SVG | animation only | none | editorial | Decorative. The 3 networks and 4 categories are not real counts or results. |
| 2.5 | "View full methodology" link | literal | link | none | none | editorial | |

### 3. Index Score (`#index-score`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 3.1 | Donut: 8 arcs, one per category, sized by weight | `DATA.profiles[0].weights` (old `dataset.profiles[0]`, the `balanced` catalog profile): reliability 22, speed 14, uptime 10, coverage 12, diversity 8, dcAsns 10, authenticity 10, value 14 | % of 100, `Math.round(w / Σw × 100)` | hovering a card dims the other arcs and puts the weight and name in the centre | dataset file → `weights` (the snapshot's `methodology.settings.weights`, the weights the scores used) or `profiles[id=balanced].weights` (`site.ranking_profiles`) | exists-but-different-shape | Only in the dataset file; no small answer carries it (HC-1). The website reads `profiles[0]`, which depends on order and is the catalog's copy. The correct source is `Dataset.weights`, because the published scores used those weights. The two can differ. |
| 3.2 | Category cards: name and weight % (8 cards) | name: `DATA.scoreLabels[key]` (old site constant: Reliability, Speed, Availability, Coverage, Diversity, DC ASNs, Authenticity, Value); weight: as 3.1 | text, integer % | hover, as 3.1 | names: none; weight: as 3.1 | editorial | The labels are UI copy. The website should own them in i18n, keyed by the API's `breakdown` and `weights` keys (`reliability` ... `value`; all 8 keys always present). |
| 3.3 | Category card bodies (8) | `categories[].body` literals | text | none | none | editorial | |
| 3.4 | Donut centre "100" and "points"; caption "8 categories, scored 0 to 100, then weighted"; "One score from 0 to 100, built from eight categories" | hard-coded in `ProxyIndexContent.astro` | number, text | hover swaps the centre text (3.1) | `weights` has 8 keys (dataset file) | editorial | The 8 is hard-coded. It holds as long as `weights` keeps 8 keys. |
| 3.5 | Donut header "Default weights" and profile name "Balanced" | hard-coded "Balanced" | text | none | dataset file → `profiles[id=balanced].label`; `/v1/rankings → profile` ("balanced", the id) | exists-but-different-shape | The label is in the dataset file only. `/v1/rankings → profile` gives the id. Part of HC-1. |
| 3.6 | Grade ladder: A+ 88+, A 80+, B+ 72+, B 64+, C+ 56+, C 48+, D below 48, coloured by letter | hard-coded `grades` in `ProxyIndexContent.astro`; the same thresholds in `pi-data.ts gradeFor` | score thresholds 0-100 | none | none | missing | The framework owns the grade bands: methodology `[grades]` (`configs/methodology.toml`, `policy.py grade`), published in the snapshot head `methodology.settings.grades`. The API reads only `settings.weights` and `target_groups` (`snap.Methodology`). Proposal: `/v1/meta → grades` (HC-2). |

### 4. What we measure (`#what-we-measure`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 4.1 | Heading copy "measures twelve figures for every network" and 4 group cards (IP pool, Performance, Authenticity, Coverage and cost) with 3 items each | `measureGroups` literals | text | hover only | none | editorial | |
| 4.2 | Specimens: 64-cell IP grid (lit or duplicate pattern), 20-bar latency histogram with p50 and p99 tags, authenticity ring at 93% blue and 4% red, dot map with "$ / ok" | hard-coded in `ProxyIndexContent.astro` (deterministic arithmetic, fixed arrays, `stroke-dasharray="93 100"` and `"4 100"`) | SVG | CSS animation | none | editorial | Decorative. The 93% and 4% are not data. They could show `market.authenticity` and `misSold` (part 1's market panel) if the owner wants them live. |

### 5. Compare proxy providers side by side (`#compare`)

The table shows `field = MEASURED.slice(0, 3)` (the top 3 residential networks) for the 30d window only. The window is hard-coded: `windowOf(n.windows, '30d')` and the copy says "over the last 30 days".

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 5.1 | Choice of the 3 columns: top 3 residential by Index Score | `MEASURED` (filter `kind == "residential"`, sort `score.composite` desc) | list of 3 | fixed 30d | `/v1/rankings?range=30d → networks[]` filtered on `kind == "residential"`, first 3 in `position` order | exists | `position` already sorts by the 30d `balanced` score, highest first. Filter on `kind` client-side, or add `?kind=`. |
| 5.2 | Column chip monogram | `n.monogram` (export: `name.slice(0,2).toUpperCase()`) | 2 letters | none | `/v1/rankings → networks[].name` (derive) | exists | A presentation rule on `name`. No API field is needed. |
| 5.3 | Column badge `#1` to `#3` | the column index i+1 (equals `standing.rank` among residential) | rank | none | `/v1/rankings?range=30d → networks[].rank` (within kind) | exists | |
| 5.4 | Network name (title attribute, 2-line clamp) | `n.name` | text | none | `/v1/rankings → networks[].name` | exists | |
| 5.5 | Index Score, e.g. "91.4" | `fixed(n.score.composite)` (old `p.score.composite` = 30d `scores.balanced.score`) | 0-100, 1 dp; N/A when null | none | `/v1/rankings?range=30d → networks[].score` | exists | |
| 5.6 | Grade letter next to the score | `gradeFor(n.score.composite)`, computed with the hard-coded thresholds | A+ to D, N/A | none | `/v1/rankings → networks[].grade` | exists | Use the API's framework grade instead of computing one. The website's thresholds equal the framework's today (A+ = 88), but only by copy (see 3.6). |
| 5.7 | Score bar width | `n.score.composite` clamped 0-100 | % width | none | `/v1/rankings → networks[].score` | exists | |
| 5.8 | Row "Success rate" | `w(n).successRate` | fraction, `pct` 1 dp | best = highest; bar = v / max | `/v1/rankings?range=30d → networks[].successRate` | exists | Network window success (`metrics.success_rate`). This is not a per-target figure, so no target score applies here. |
| 5.9 | Row "Latency p50" | `w(n).latencyP50` | ms (`ms()`: "812 ms" or "1.23 s") | best = lowest; bar = min / v | `/v1/rankings?range=30d → networks[].latencyP50` | exists | Headline basis: generic_http, under load in every-request snapshots. |
| 5.10 | Row "Unique IPs (30 days)" | `w(n).uniqueIps` | count, `compact` (k / M / B) | best = highest | `/v1/rankings?range=30d → networks[].uniqueIps` | exists | The headline group's distinct IPs. Shows the hour-formula estimate where released (K8). |
| 5.11 | Row "Countries" | `w(n).countries` (= `p.pool.countries`, not per range) | integer | best = highest | `/v1/rankings → networks[].countries` (`pool.countries`) | exists | The value is the same in all three ranges, both on the website and in the API. |
| 5.12 | Row "Cities" | `w(n).cities` (= `p.pool.cities`) | integer | best = highest | `/v1/networks/{slug} → profile.pool.cities`, or `ranges.30d.lists.city` (the distinct city count of the 30d list) | exists-but-different-shape | Not in `/v1/rankings` or `/v1/networks` rows, so the table needs 3 extra network answers (up to 200 KB each). Proposal: `cities` on `RankingRow` (HC-4). |
| 5.13 | Row "DC ASN share" | `n.dcAsnShare` (`p.trust.datacenterAsnShare`) | fraction, `pct` 1 dp | best = lowest | `/v1/rankings → networks[].datacenterAsnShare` | exists | |
| 5.14 | Row "Price per GB" | `w(n).listRate` (old `listRate(p.pricing)` = `pricePerGbAtScale` ?? `pricePerGb`) | USD per GB, `money(v, 2)` | best = lowest | `/v1/networks/{slug} → profile.pricing.pricePerGbAtScale` / `.pricePerGb` | exists-but-different-shape | Not in the rankings row; the client derives `listRate`. Proposal: `listRate` on `RankingRow`, using the same rule the API's cost board uses (HC-5). |
| 5.15 | "Best" badge per row and bar fill (best cell full, others relative, minimum 4%) | computed in `metric()` over the 3 shown values | badge, % | depends on the higher-or-lower rule per row | from 5.8 to 5.14 | exists | Computed client-side over the 3 values shown. No API field is needed. |
| 5.16 | N/A cells | `missing(v)` gives "N/A" (Countries and Cities use `String(v ?? 'N/A')`) | text | none | API `null` | exists | |
| 5.17 | Sticky metric column on phones | `pinColumns()` | behaviour | horizontal scroll | none | editorial | |
| 5.18 | (Proposed, not shown today) "Browser score" row next to the success-rate row | none | 1-10 score with "rank of M" | best = highest | `/v1/networks/{slug} → profile.browserOverall.<engine>.success.{score, rank, rankedOf, status}` (30d) | exists-but-different-shape | Applies only if the owner adds the row when the website moves to scores. It is per network answer only and not in `/v1/rankings`. Proposal: `browserOverall` on `RankingRow` (HC-7). Show it per engine, never Chrome and Camoufox added together. |
| 5.19 | Copy "over the last 30 days" and "Every value comes from our own measurements" | `t()` literals | text | fixed 30d | `/v1/rankings → range` | editorial | |

### 6. Buyer's guide (`#how-to-choose`, `buyers-guide-art.ts`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 6.1 | 6 numbered cards (title, body with one inline link, "more" link to an Index page, guide or blog post) | `guide` literals in `ProxyIndexContent.astro` | text | none | none | editorial | Body numbers ("clears 99 percent ... fail one request in four on a search engine") are prose. |
| 6.2 | Card art "targets": rows easy site 99%, shop 94%, search 76%, tickets 73%, "average 86%" | hard-coded in `buyers-guide-art.ts` (`rows`, `'86%'`) | % | none | (would be) per-target field means: `/v1/networks/{slug} → profile.targets[].successRate` across networks | editorial | An illustration. If the owner wants live numbers, there is no single answer with the field mean per target. That would need the targets-page answer (out of this part). |
| 6.3 | Other card art: advertised vs seen pool, consumer ISP vs hosting ASN, retry bars ("A looks cheaper"), p50/p99, "opted in" vs "source unknown" | hard-coded SVG in `buyers-guide-art.ts` | SVG | none | none | editorial | Decorative. |
| 6.4 | Footer line "Contribute to the Proxy Index" | literal | link | none | none | editorial | |

### 7. Why the Proxy Index exists (`#why`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 7.1 | Prose paragraphs, "Shifter included" link, blog link | `t()` literals | text | none | none | editorial | "since 2012" is company history. |
| 7.2 | Scan panel: 4 rows with monogram (Shifter logo when `slug == 'shifter'`), name, score bar and score `toFixed(1)` | `MEASURED.slice(0, 4)` → `name`, `monogram`, `score.composite ?? 0`, `slug == 'shifter'` | 0-100, 1 dp | CSS scan animation | `/v1/rankings?range=30d → networks[]` (residential, first 4): `name`, `slug`, `score` | exists | A null score renders as "0.0" and a 0-width bar (no N/A here). The website should show N/A instead. |
| 7.3 | Scan panel "Live" badge (pulsing) | hard-coded | badge | none | `/v1/version` or `snapshot.asOf` could gate it | editorial | Not tied to data freshness. |
| 7.4 | "Since 2012" | hard-coded | year | none | none | editorial | |
| 7.5 | Stat row: "24/7" Live testing, "1 method" for every network, "0" paid placements, "100%" open data | `why` literals | text | scroll-reveal | none | editorial | These are claims, not measurements. "24/7" could be checked against the pulse schedule (methodology), but no number is computed. |

### 8. FAQ (`pi-faq.ts` through `FAQSection`, also the JSON-LD FAQPage)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 8.1 | Q10 answer "... across all {targets} targets" | `proxyIndexFaq(locale, DATA.targets)` | integer | none | as 2.1 | exists-but-different-shape | The same count and the same gap as 2.1 (HC-3). |
| 8.2 | Q1 to Q12 questions and answers, title and description | `pi-faq.ts` literals | text | accordion | none | editorial | They contain hard-coded figure claims: Q3 "0 to 100", "eight categories" and the 8 category names; Q6 "well over 95 percent" on easy targets and "below 80 percent" on hard ones; Q10 "24 hours, 7 days and 30 days". Q6 could be checked against data (the per-target means, out of this part). Q11 promises "every network's scores, windows and test events", which the API dataset file meets (13.1). |

### 9. CTA and footer

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 9.1 | CTA "Check the leaderboard against your own traffic", footer | `t()` literals | text | none | none | editorial | |

### 10. OG images (`src/pages/og/proxy-index/[key].astro`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 10.1 | Fixed cards for overview, networks, compare, overlap, true-cost, alternatives, live-tape, methodology, targets, guides, changelog, contribute, disclosure (eyebrow, title, tagline, pills, icon) and the badge "Residential networks, measured" | `PAGES` literals | text | English only | none | editorial | No numbers on any fixed card. |
| 10.2 | `network-{slug}` card: network name, monogram, Shifter mark when `slug == 'shifter'` | `reviewFor(slug)`: the network is in `NETWORKS` (= `MEASURED`) and in `networks.json → networks[slug]` | text | 404 rewrite when there is no review | `/v1/networks → networks[].{slug, name}`; a page exists when `/v1/networks/{slug}` answers 200 (`/healthz → unserved` lists exceptions) | exists | Monogram: derive from `name` (5.2). The OG render needs the network list per image request; cache it per `/v1/version`. |
| 10.3 | `guide-{slug}` card: title, category, reading minutes, drawn cover | `guideBySlug` (Markdown in `guides/content/*.md`), `coverMarkup` | text, SVG | none | none | editorial | The cover's "hosting IPs" label is hard-coded. |

### 11. Shared chrome: search palette (`pi-palette.tsx`, `search.json.ts`)

The palette opens with ⌘K, Ctrl K or `/`. It is mounted by part 1's island on the home (`page` home), by `pi-networks.tsx` (networks), `review-measurements.tsx` (network page, `review`), `tape-board.tsx` (live tape, `tape`, follows a network) and by `IndexDock` (`index`) on targets, compare (adds networks), overlap and true-cost.

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 11.1 | Runtime fetch `GET /proxy-index/search.json?locale=<l>` | `loadIndex()`: once per visit and language, shared by every palette; fired on open, otherwise after 2.5 s idle; retried on the next open after a failure | JSON | `Cache-Control: public, max-age=300` | website route (see 11.4 to 11.6); the API's nearest are `/v1/search-index` and `/v1/search?q=` | exists-but-different-shape | The API index is the old site's (HC-8): hrefs without the `/proxy-index` prefix (`/networks/<slug>`, `/targets`, `/pricing`); old nav pages; a page blurb that still says "ranked by MILF Score"; subtitle "kind · product · HQ"; meta "rank · score"; no Guides group; an extra Countries group. The website keeps its own client-side search. The API should either serve the website's index or retire these two routes. |
| 11.2 | Networks group: title `name`; subtitle `tagline · Measured/Demo`; meta `fixed(score.composite) · #rank` or "Unranked"; monogram chip; keywords `slug name` | `MEASURED[]` → `name`, `slug`, `tagline`, `source`, `score.composite`, `standing.rank`, `monogram` | text; score 1 dp | top 5 before typing ("Top networks"); on pages that navigate (networks, network page, targets, overlap, true-cost) only networks with a review page, once `search.json` arrived; home, compare and live tape offer every network | `/v1/rankings?range=30d → networks[].{slug, name, source, score, rank, kind}` | exists-but-different-shape | `tagline` is in no list answer, only in `/v1/networks/{slug} → profile.tagline` (18 requests) and the dataset file. Proposal: `tagline` on `/v1/rankings` and `/v1/networks` rows (HC-6). `source` is always "measured" in the API (no demo rows), so the "Demo" label goes away with `SHOW_DEMO`. |
| 11.3 | Network pick action | `openNetwork(slug)` (home: event `pi:scope` and scroll to `#market`), else go to `/proxy-index/networks/<slug>` | behaviour | `onNetwork` override on tape and compare | none | editorial | |
| 11.4 | `reviews`: which networks have a review page | `search.json.ts`: `MEASURED.filter(reviewFor).map(slug)` (needs `networks.json`) | slug list | used to filter 11.2 | `/v1/networks → networks[].slug` (every listed network has a `/v1/networks/{slug}` page) | exists | |
| 11.5 | Targets group: title `label`, subtitle the editorial line, link `/proxy-index/targets#suite` | `search.json.ts`: `TARGETS` (`targets.json → targets[].{id, label, category}`) plus `copyFor(target).{short, line}` (editorial) | text | the server sends English keywords | dataset file → `targets[]` (`id`, `label`, `category`, `difficulty`, `passCondition`, `engine`, `group`); `/v1/search-index` items of group Targets (old shape) | exists-but-different-shape | No small answer has the target list. Proposal: `/v1/meta → targets[]` (HC-1). Decide whether the AI source targets (`group` set) are listed; the website lists 8 today. |
| 11.6 | Guides group (Index guides and blog guides): title, question or description, link | `guideItems(locale)` (content collection, `guides/content/*.md`, blog posts) | text | none | none | editorial | |
| 11.7 | Sections group ("Jump to"): home list market, insights, leaderboard, tape, directory; the `IndexDock` pages (targets, compare, overlap, true-cost) use their `IndexStepBar` steps; the live tape uses a hard-coded list (`tape` "Live tape", `kinds` "Reading the tape", `feed` "Take the feed"); the network page's list always includes "Review", even when the page has no article and its tab is hidden; the `page === "compare"` list is never used (`IndexDock` always passes `page="index"`); shown only when the anchor exists on the page | literals in `pi-palette.tsx`; `IndexDock` reads `[data-index-steps] a[data-step]` | text | filtered by `document.getElementById` | none | editorial | |
| 11.8 | Pages group: 13 Index pages with subtitles and keywords | `PAGES` literal | text | none | none | editorial | Subtitle "The price of 1,000 successful requests" is copy. |
| 11.9 | Action "Download the dataset" | link to `/proxy-index/dataset.json` | download | none | `/v1/dataset` | exists-but-different-shape | See 13.1. |
| 11.10 | Action "Copy link"; flash "Link copied" | clipboard | behaviour | none | none | editorial | |
| 11.11 | Range actions "Show the last 24 hours / 7 days / 30 days" | `RANGES = ['24h', '7d', '30d']`; calls `onRange` (only when the page has windowed figures) | range id | scrolls to the page's first section | `range=` of `/v1/rankings` and `/chart`; the range metadata is served in `/v1/networks/{slug} → stats.<r>.range` | exists | |
| 11.12 | Footer "{networks} networks · {targets} targets" | `MEASURED.length` and `DATA.targets` | integers | none | networks: count of `/v1/rankings → networks[]` with `kind == "residential"`; targets: as 2.1 | exists-but-different-shape | The network count exists. The target count is HC-3. |
| 11.13 | Ranking of results: every token must match, title weighted most, loose match for tokens of 4+ characters, group bonus, 5 per group | `score()` client-side | ranking | typing; arrow keys, Tab, Enter, Esc | `/v1/search?q=` (port of the old palette's scoring with different constants, see section 1 A row 4: Pages bonus 3, keywords 30 after subtitle, loose span 3×; 16 results, 5 per group, old items) | exists-but-different-shape | The website does not call `/v1/search`. Keep it client-side, because all items are small. |
| 11.14 | Empty state "Nothing matches “{query}”"; Recent (localStorage `pi-palette-recent`, 4 entries) | `pi-palette.tsx` | text | none | none | editorial | |
| 11.15 | Countries group (API only): title country name, subtitle "N networks with exits here · X exit IPs", meta country code, link `/networks?country=CC` | none (the website has no country search) | text | none | `/v1/search-index` and `/v1/search?q=` → items with `group == "Countries"`, `id` `country:<CC>`, `href` `/networks?country=<CC>` | exists | No website page serves `/networks?country=` (old-site path; `/proxy-index/networks` has no country filter). Drop the group or add a country filter page (HC-11). |
| 11.16 | API-only actions "List your proxy network" (`action:apply`) and "Toggle dark / light theme" (`action:theme`); the API has no "Download the dataset" and no range actions | none | text | none | `/v1/search-index → items[]` `action:apply`, `action:theme`, `action:copy` (`command`) | exists-but-different-shape | The website has no apply flow behind `href: "#"` and no theme switch; its actions are `act:dataset`, `act:copy`, `act:range:<r>`. The API dropped the dataset action on purpose (`startingPointIDs` comment). HC-11. |
| 11.17 | Item ids (Recent list and de-duplication) | `net:<slug>`, `page:<path>`, `guide:` / `blog:` / `target:<id>`, `act:*`; Recent stores 4 ids in `localStorage["pi-palette-recent"]` | id strings | none | `/v1/search-index → items[].id`: `network:<slug>`, `page:<old path>`, `target:<id>`, `country:<CC>`, `action:*` | exists-but-different-shape | Network and action ids differ, so stored Recent ids would not resolve after a switch to the API index (target ids match). HC-11. |
| 11.18 | Empty query: "Jump to" = the current page's sections; "Top networks" = first 5 residential networks | `here.filter(group == "Sections")`; `MEASURED.slice(0, 5)` | list | only anchors present in the DOM | `/v1/search-index → startingPointIds` (`page:/networks`, `page:/compare`, `page:/overlap`, `page:/pricing`), `topNetworkIds` (first 5 of the leaderboard, every kind); `/v1/search?q=` (empty) → the same two sections | exists-but-different-shape | The API's starting points are old-site pages, the website's are sections of this page; top networks are not filtered to residential. HC-11. |
| 11.19 | Target item meta (not shown) | none | "difficulty N/5" | none | `/v1/search-index → items[].meta` for Targets (`difficulty`, "N/A/5" when null) | exists | Not shown yet; the website's target items have no meta. |
| 11.20 | Locale, loading and error states: items in the visitor's language with localised links; no loading state (Guides and Targets appear when `search.json` arrives); a failed fetch is silent and retried on the next open; first query token bolded in titles | `search.json?locale=`, `t()`, `localizePath()`; `loadIndex()`; client highlight | text | per locale | `/v1/search?q=` answers 400 for any parameter but `q` (`unknown query parameter`) and for `q` over 200 bytes, 503 while no build is loaded; English only; `tokens[]` for the highlight | missing | No `locale=` in the API; translation and `/proxy-index` link prefixes stay in the website (key copy by item id). A per-keystroke `/v1/search` would also need a loading state. HC-11. |

### 12. Shared page components (`page/*`, `pi-network-strip.tsx`, `src/lib`)

None of these files reads data. Every figure they show comes from the calling page's props or DOM, so the data rows belong to the page that passes them.

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 12.1 | `IndexHero`: badge, title, lead, `meta` live line, actions, `highlights`, window title and legend, up to 2 floating `pills` ({value, label}), `strip` slot | props from each page | strings | `countUp()` animates the numbers in the pills and meta | per calling page | editorial | Not used by `/proxy-index` (`pi-hero.tsx`), `/proxy-index/networks` (`NetworksHero`) or the network page (`ReviewHero`); used by targets, compare, overlap, true-cost and live tape. Pill values on the other pages are data; map them per page. |
| 12.2 | `NetworkStrip`: line "Ranked by Index Score across {n} networks" (`MEASURED.length`), then monogram, name, `fixed(score.composite)`, `#standing.rank` or "Unranked" | `MEASURED` | 0-100, 1 dp; rank | track repeated to at least 12 items | `/v1/rankings?range=30d → networks[].{name, score, rank}` (residential) | exists | Rendered on every in-scope page except the network page (in `pi-hero.tsx` on `/proxy-index`, `pi-networks.tsx` on `/networks`). Always the 30d score: it does not follow the dock range. With 0 networks it does not render: `Array.from({ length: Math.max(2, Math.ceil(12 / ranked.length)) })` gets `Infinity` and throws `RangeError: Invalid array length`, and the strip is server-rendered on every in-scope page but the network page, so an empty or failed rankings answer breaks those pages (HC-16). Items are not links. |
| 12.3 | `IndexDock` → `TimeDock` (range 24h, 7d or 30d, default 30d) and the palette | `DEFAULT_RANGE = '30d'`; a pick sets `data-range` on `[data-pi-range-root]` and fires `pi:range` | range id | `IndexRangeStyles` shows the server-rendered figures of the picked range (all 3 ranges are rendered into the HTML). Only compare and true-cost have a `[data-pi-range-root]` (and `IndexRangeStyles`); on targets and overlap `IndexDock` shows search only (no range switch, 30d figures) | the pages need each range's answer at render: `/v1/rankings?range=24h\|7d\|30d` (3 requests) | exists | Because all 3 ranges are pre-rendered, a windowed static page fetches every range per view (or per cache). |
| 12.4 | `IndexStepBar` and `index-steps-client`: numbered sticky section bar, scroll-spy, fragment fallbacks | props (`steps`, `fallbacks`) | text | scroll, hash | none | editorial | |
| 12.5 | `IndexNetworkPicker` and its client: "All networks" with a count, then options {slug, name, monogram, count} with a search box | props from changelog `RecordSection` and overlap `PairsSection` | text, integer | type to filter, arrow keys, Enter, Esc; `onPick(slug \| "all")` | network list: `/v1/networks → networks[].{slug, name}`; counts per calling page | exists | Counts are page data (changelog entries or overlap pairs per network). |
| 12.6 | `index-spark-client`: sparkline hover, points `[{x, y, v, l}]` in `data-spark` | points from the caller (compare page) | % positions, value label, date label | pointer and touch hover | per calling page (`/v1/networks/{slug}/chart` or `/v1/rankings → trend`) | exists | The number of points and the resolution are set by the caller. |
| 12.7 | `IndexSection`, `IndexDetails`, `IndexRule`, `IndexIcon`, `index-tabs-client`, `index-reveal-client`, `copy-client`, `ui.ts` | presentational | none | tabs, reveal, copy | none | editorial | |
| 12.8 | `src/lib/jsonld` (`absUrl`, `ref`, `ORG_ID`), `blog-slugs`, `authors`, `hero-height` | helpers | none | none | none | editorial | No Proxy Index figures in these helpers; `src/lib/legacy-network-pages.ts` holds the hard-coded Index network slug table (12.11, HC-15). |
| 12.9 | `IndexHero` window bar "Live" label (pulsing dot) on targets, compare, overlap, true-cost and live tape | hard-coded: prop `live` defaults to `true`; no page passes `live={false}` | text | none | `/v1/version → {revision, builtAt}`; answer `generatedAt`; dataset file `snapshot.asOf`, `snapshot.refreshSeconds` | missing | Never reflects the data's age. The targets page's own row (section 3, 2.4) marks it editorial; it is the same shared label. HC-12. |
| 12.10 | `IndexNetworkPicker` empty state "No network matches" | `[data-np-none]` | text | client filter on the lower-case name | none | editorial | Overlap `PairsSection` only (in scope). |
| 12.11 | `src/lib/legacy-network-pages.ts` `INDEX_NETWORK_SLUGS` (19 old slug → Index slug pairs: 18 networks plus the alias `smartproxy` → `decodo`), `indexNetworkPath()`, `legacyNetworkTarget()` | hard-coded table | slugs, URLs | none | `/v1/networks → networks[].slug` | exists-but-different-shape | Drives the 301 redirects `/review/<slug>` and `/benchmark/<slug>` (`src/middleware.ts`), the footer (`footer-section.tsx`, whose "Competitor Benchmarks" and "Proxy Reviews" lists of provider names on every page come from `FOOTER_BENCHMARKS` and `FOOTER_REVIEWS` in `src/lib/footer-providers.ts`, generated by `scripts/generate-footer-providers.mjs`), `html-sitemap.ts`, `sitemap.ts`, `provider-links.ts`, `llms.txt.ts`, `llms-full.txt.ts`, `benchmarks.astro`, `reviews.astro`, `alternative/[slug].astro`. All point to `/proxy-index/networks/<slug>`, which 404s today for every slug but `shifter` (`networks.json` has 4 networks, 3 of them demo). Build the table from `/v1/networks` at build time and link only listed slugs; keep `smartproxy` as an editorial alias. HC-15. |
| 12.12 | `SHOW_DEMO = true` ("turn off before launch") in `pi-data.ts`: puts the 3 demo networks (`nimbus-residential`, `orbitnode`, `kestrel-net`) into the strip, palette, compare, overlap, true-cost and targets, re-numbers `standing.rank` 1..n by composite, and gates the demo filters of `changelog-data.ts` and `tape-data.ts` | constant | boolean | none | none: every API network and tape row is `source: "measured"` (`model.SourceMeasured`); targets are `measured` or `catalog` | missing | Nothing to map: remove the switch and every demo copy branch ("Demo" subtitles, "Fictional demo provider", "Simulated request feed", "seeded demo network" in `ReviewRuns.astro`) when the pages read the API; ranks come from RK `rank`, `peers`. HC-14. |

### 13. Public download (`src/pages/proxy-index/dataset.json.ts`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 13.1 | `GET /proxy-index/dataset.json` (`Content-Disposition: attachment; filename="proxy-index.json"`, `max-age=300`), linked from the JSON-LD (1.4), the palette (11.9) and FAQ Q11 | the whole `home.json` (65 KB, the old site's homepage export, 5 networks including 4 demo ones) | JSON | none | `/v1/dataset` → 302 to `/v1/files/dataset-<sha256>.json` (`Dataset` plus `pages`, `lists`, `snapshot`, `na`; about 9.2 MB, gzip 0.5 MB at 18 networks × 249 countries; immutable; `.gz` and `.br` on disk) | exists-but-different-shape | The shape is entirely different (`Dataset` vs `ProxyIndexHome`) and about 140 times larger. Proposal: the website route answers with a 302 to the API's `/v1/dataset`, so the file is served precompressed and the website never buffers it. Keep the filename through `Content-Disposition` on the API file, or keep a website-side redirect. Owner decision: which file is "the dataset" (HC-9). |

### Other Proxy Index pages not in the owner's list

The owner's list is `/proxy-index`, `/targets`, `/networks`, `/networks/<slug>`, `/compare`, `/overlap`, `/true-cost` and `/live-tape`. These pages under `src/pages/proxy-index/` are not in it:
- `alternatives.astro`: `alternatives/alternatives-data.ts` (`MEASURED` from `home.json`; `reviewFor` from `networks.json` and `targets.json`), `review-data GENERATED_AT`, `alternatives-faq.ts`, `alternatives-sections.ts`, `NetworkStrip`; sections ProvidersSection, SignsSection, HowSection.
- `alternatives/[slug].astro`: the same `alternatives-data` (`alternativesTo`, `providerFor`), `pi-data fixed`, `review-data GENERATED_AT`, `NetworkStrip`; sections RankedSection, VersusSection, SwitchSection, GuideSection; `alternatives-faq.ts`.
- `changelog.astro`: `changelog/changelog-data.ts` (`changelog.json`, `SHOW_DEMO`): `ENTRIES`, `GENERATED_AT`, `NETWORKS`, `weekCounts`; sections WeekSection, MoversSection, RecordSection (which uses `IndexNetworkPicker`), DiffSection; `NetworkStrip`. **Served** (CHANGELOG-API-1): `GET /v1/changelog?range=24h|7d|30d` answers the whole `ChangeRecord` (`generatedAt`, `runId`, `weekSeconds`, `entries[]`, `movers[]`) field for field, for every network, newest first; `changelog.json` is replaced by it with no change to the page's shape (api/answers/changelog.md: sources, thresholds, every template key).
- `changelog.json.ts`: `changelog-data` (`ENTRIES`, `GENERATED_AT`, `MOVERS`), from `changelog.json`; served by the same `/v1/changelog`.
- `compare/[pair].astro`: `pair/pair-data.ts` (`pairFor`, `tally`), built on `compare/compare-data.ts` (`NETWORKS` from `home.json`, `reviewFor` from `networks.json` and `targets.json`); `pi-data`; `review-data GENERATED_AT`; `pair-faq.ts`; sections VerdictSection, SideBySideSection, TargetsSection, PriceSection, PoolSection, PairGuideSection; `NetworkStrip`.
- `contribute.astro`: copy only, plus `review-data ALL_REVIEWS` and `pi-data` (in ResultsSection, ReviewSection, SubmissionsSection, `results-wizard.tsx`); `contribute/submit.ts` has no endpoint yet (`TODO(api)`); `NetworkStrip`.
- `disclosure.astro`: `DisclosureStatement.astro`, `methodology/framework.ts` (`FRAMEWORK_REPO`), `NetworkStrip`; editorial.
- `guides/index.astro`: `guides/guides.ts guideItems` (Markdown and blog collection), `lib/authors`, `NetworkStrip`; editorial.
- `guides/[slug].astro`: `guides.ts` (`guideBySlug`, `guideItems`), `guides/content/*.md`, `lib/authors`; editorial.
- `live-tape.json.ts`: `live-tape/tape-data.ts` (`TAPE_UPDATED_AT`, `tapeSince`) from `home.json → tape` and `networks.json`.
- `methodology.astro`: `methodology.json` (`methodologyId`, `scopes`, `png`) and `pi-data DATA` (`generatedAt`, `profiles`, `run`, `scoreLabels`, `targets`); sections ScopeSection (reads `methodology.json`), PipelineSection, MetricsSection, ScoreSection, ReproduceSection, LimitsSection; `score-curves.ts` and `score-example.ts` (hard-coded curves and example).
- `search.json.ts` and `dataset.json.ts` are shared chrome. They are covered in sections 11 and 13 above.

### Further details (website rules, no API gap)

- `IndexHero` counts up only the pills (desktop only), not the meta line.
- `IndexDock` shows once 70% of the viewport has scrolled and hides at the last footer.
- The server-rendered compare table and the "why" scan panel have no state for fewer networks than columns or for 0 networks; ties are not handled; bar widths are not clamped.
- The "twelve figures" copy and FAQ Q10 ("every figure" switches range) are not checked against fields; the server-rendered tables are fixed to 30d.

### Gaps summary

1. HC-1 weights-profiles-targets: the default category weights, the profile label "Balanced" and the target list (3.1, 3.5, 11.5) exist only in the 9.2 MB dataset file — proposal: a small per-build `/v1/meta` answer with `generatedAt`, `weights`, `profiles[]` (`id`, `label`, `description`, `weights`), `targets[]` (`TargetMeta`), `ranges` and `grades` — source: snapshot `methodology.settings.weights`, `site.ranking_profiles`, `site.targets` and `public.targets`.
2. HC-2 grade-bands: the grade thresholds A+ 88 down to D 0 are hard-coded in `ProxyIndexContent.astro` and `pi-data.ts gradeFor` (3.6) — proposal: `/v1/meta → grades {"A+": 88, ...}` — source: snapshot `methodology.settings.grades` (framework `[grades]`, `configs/methodology.toml`); the API's `snap.Methodology` does not read it yet.
3. HC-3 target-count: "the same {targets} targets", FAQ Q10 and the palette footer (2.1, 8.1, 11.12) have no count field, and "targets" means different things in different places — proposal: `/v1/meta → targetCount` and `cardTargetCount` (targets with `group == null`) — source: `Dataset.targets`, `measuredMarket.targetCount`.
4. HC-4 cities-in-rankings: Cities in the compare table (5.12) is only in `/v1/networks/{slug}` — proposal: `/v1/rankings → networks[].cities` — source: provider `pool.cities`.
5. HC-5 list-rate-in-rankings: Price per GB in the compare table (5.14) is only in `profile.pricing`, and the client derives it — proposal: `/v1/rankings → networks[].listRate` (`pricePerGbAtScale` ?? `pricePerGb`) — source: `site.pricing`.
6. HC-6 tagline-in-lists: palette network subtitles (11.2) — proposal: `tagline` on `/v1/rankings` and `/v1/networks` rows — source: `site.providers.tagline`, else the generated tagline.
7. HC-7 browser-overall-in-rankings: an optional browser-score row for the compare table (5.18) is per network only — proposal: `/v1/rankings → networks[].browserOverall.<engine>.success.{score, rank, rankedOf, status}` — source: snapshot `w.browser_overall.<engine>` (already mapped in `build/browser.go`).
8. HC-8 search-index-shape: `/v1/search-index` and `/v1/search` still serve the old site's index (11.1, 11.13): wrong hrefs, old nav, a "MILF Score" blurb, no guides — proposal: either serve the website's palette index (networks with `/proxy-index/...` hrefs, targets, review slugs) or retire both routes and document that the website searches client-side — source: `build/search.go`.
9. HC-9 dataset-download: `/proxy-index/dataset.json` serves the old `home.json`, not the API dataset (1.4, 11.9, 13.1) — proposal: the website route answers with a 302 to `/v1/dataset`, and the API file gets `Content-Disposition: attachment; filename="proxy-index.json"` — source: the dataset file (already built per version).
10. HC-10 scan-null-score: the "why" scan panel prints a null score as "0.0" with a 0-width bar (7.2) — proposal: print "N/A" — website change only; RK `score` is null.
11. HC-11 search-differences: beyond HC-8's hrefs and blurbs, the API index differs from the palette in scoring constants (Pages bonus 3 vs 5; keyword word-start 30 after the subtitle vs 55 before it; loose span 3× vs 2×; 16-result cap; raw vs `terms()`-normalised keywords), item ids (`network:` / `action:` vs `net:` / `act:`, so Recent ids break), an API-only Countries group linking to `/networks?country=CC` (no such page), API-only apply and theme actions, no dataset or range actions, empty-query starting points that are old-site pages and top networks of every kind, and English only (`/v1/search` answers 400 for any parameter but `q`) (section 1 A rows 4-5, 11.15 to 11.20) — proposal: in W10's website-shaped index use the palette's constants and ids, drop Countries, apply and theme, drop the old-site starting points (the website's are page sections) and filter top networks by kind, and accept `locale` or document that titles are translated by item id on the website — source: `build/search.go`.
12. HC-12 freshness-labels-and-polling: "Live" is hard-coded in the dock (`TimeDock`, every page), the `IndexHero` window (`live = true`, five pages) and the Overview hero (`pi-hero.tsx`); no page polls `/v1/version`, so open pages never update and never show data age (section 1 A row 2, B row 14a, 12.9, section 10 freshness row) — proposal: show "updated X ago" from the answers' `generatedAt` (amber when older than `snapshot.refreshSeconds`), poll `/v1/version` and refetch on a new `revision` — source: `/v1/version`, `generatedAt` (all exist).
13. HC-13 export-dates: JSON-LD `dateModified` reads three exports (`/proxy-index` home.json 2026-10-03; `/targets` targets.json 2026-09-26; compare, overlap, true-cost networks.json 2026-10-05; live tape the newer of home and networks), and `/networks` and `/networks/<network>` emit no `dateModified` — proposal: every page takes `dateModified` from the `generatedAt` of an answer it already fetches (one value per build) and the two network pages add it — source: answer `generatedAt` (exists).
14. HC-14 demo-switch: `SHOW_DEMO = true` mixes 3 demo networks into strip, palette, compare, overlap, true-cost and targets and re-numbers ranks (12.12) — proposal: remove the switch and the demo copy branches when the pages read the API; ranks from RK — source: none needed (API rows are all `measured`).
15. HC-15 legacy-slugs: `INDEX_NETWORK_SLUGS` (19 hard-coded entries) drives 301 redirects, footer (with the generated `FOOTER_BENCHMARKS` / `FOOTER_REVIEWS` name lists of `src/lib/footer-providers.ts`), sitemaps, provider links and llms.txt, all to network pages that 404 for every slug but `shifter` (12.11) — proposal: build the table from `/v1/networks` at build time, link only listed slugs, keep `smartproxy` → `decodo` as an editorial alias — source: `/v1/networks` (exists).
16. HC-16 strip-empty-state: `NetworkStrip` throws on an empty network list (12.2); the website needs a real empty state (hide the strip, or show "No ranked networks yet") before it reads RK, whose residential rows can be empty or the request can fail — website only.

### Performance notes

Calls per page view. The site is SSR (`output: 'server'`), so the server renders every view.
- `/v1/rankings?range=30d`: one answer of about 18 rows, around 10 to 20 KB. It covers the compare table (5.x except 5.12, 5.14 and 5.18), the scan panel (7.2), the palette networks (11.2 except tagline), `NetworkStrip` (12.2) and `dateModified` (1.2). It is already precomputed per build.
- Today, without HC-1 to HC-5, the page would also need 3 × `/v1/networks/{slug}` (up to 200 KB each) for cities and price per GB. It would need the dataset file (9.2 MB, 0.5 MB gzip) for weights, profiles and the target count, which is unacceptable per view. The proposed `/v1/meta` would be under 10 KB, change only per build, and be precomputable with the rest of the answers.
- Palette: `search.json` is fetched at most once per visit and language (max-age 300). Its data is build-time content plus the target list and the network slugs (`/v1/networks`, 18 rows). The website can build it from `/v1/meta` and `/v1/networks`, cached per `/v1/version` revision. Its taglines need HC-6, or 18 network answers.
- The dataset download should never pass through the website: one 302 to the immutable, precompressed API file.
- OG `network-{slug}` renders need the network list. Cache `/v1/networks` per revision.
- No polling in this part. `/v1/version` (5 s cache) is the right key for a server-side answer cache across the whole Index section.
- Compare and true-cost pre-render all 3 ranges (`IndexRangeStyles`), so they fetch `/v1/rankings` for 24h, 7d and 30d per render; targets and overlap show 30d only and need one range. All three are precomputed. Cache them by revision.

## 3. /proxy-index/targets: the test-target suite and every network's success on each target

Source files read (website root): `src/pages/proxy-index/targets.astro`,
`src/components/proxy-index/targets/{targets-data.ts, target-sections.ts, TargetsVisual.astro, LadderSection.astro, MatrixSection.astro, WhySection.astro, WishlistSection.astro}`,
`src/data/proxy-index/targets.json`, `src/data/proxy-index/home.json` (networks, measured, targets),
`scripts/proxy-index/export-targets.mts`, `scripts/proxy-index/export-home.mts` (monogram, kind, score, standing),
`src/components/proxy-index/pi-data.ts` (MEASURED, SHOW_DEMO, missing, pct, ms, fixed),
`src/components/proxy-index/pi-network-strip.tsx`, `src/components/proxy-index/pi-palette.tsx` (data parts),
`src/components/proxy-index/page/{IndexHero.astro, IndexSection.astro, IndexStepBar.astro, index-dock.tsx, index-steps-client.ts}`,
`src/pages/proxy-index/search.json.ts`, `src/pages/og/proxy-index/[key].astro` (targets entry), `astro.config.*` (output: server).
API side (repo-relative): `api/README.md`, `api/FIELDS.md` ("Browser targets", "Browser scores", "Per-page answers", "The dataset file of a v4 build"),
`api/internal/model/target.go`, `api/internal/model/model_provider.go`, `api/internal/build/{measured.go, measured_sections.go, browser.go, build_steps.go, profile.go, rankings.go, search.go, measured_market.go}`,
`configs/targets.toml`, `configs/methodology.toml` (`[target_groups]`, `[browser_difficulty]`, `[http_difficulty]`: the owner's fixed difficulties), `src/proxy_index/calculated/target_scores.py`.

Shorthands used below:
- **NP** = `/v1/networks/{slug}?kind=residential → profile` (one request per network; each answer up to 200 KB).
- **T(id)** = `NP.targets[]` entry with `target == id` (a `TargetBenchmark`).
- **DSF** = `/v1/dataset` (302 to the dataset file, ~9.2 MB, gzip ~0.5 MB at 18 networks) → `providers[].productMeasurements.residential` (same blocks as NP; `providers[].targets` / `.browserOverall` are the headline category's).
- **RK** = `/v1/rankings?range=<r>` → `networks[]`.
- Page "24h" vs API: the page says every per-target figure is "over the last 24 hours". The API's `targets[]` and `browserOverall` are built from the **30d window only** (`measuredBlock`: `w := p.Window("30d")`, `targetBenchmarks(w, …)`, `browserOverall(w)`). The snapshot has `windows["24h"|"7d"|"30d"].targets[id]` and the calculator scores every window label (`target_scores.score`), but no endpoint serves the 24h or 7d per-target figures or scores. Every per-target row below is therefore at best `exists-but-different-shape` (wrong window) unless the page copy changes to 30d.
- Engines: a browser target's `successRate`/`captchaRate`/`blockRate`/`requests`/`scores` are those of `T(id).engine` (the network's engine with most tries on that target). Two networks can have different `engine` on the same target, so a matrix column can mix Chrome and Camoufox figures; scores are only comparable within one engine (`engines.<engine>.scores`). HTTP targets (`generic_http`, `generic_https`) have no `engine` or `engines`, and only `scores.success` (#881).
- Scores are ranked across all product groups of a window label (all proxy kinds together, `target_scores.score` groups by label only), not within the residential kind the page shows; `rankedOf` can count non-residential groups. Unverified whether this is intended; flag for the architect.

### 0. Page meta, SEO, JSON-LD

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 0.1 | `<title>`, meta description, keywords | `targets.astro` t() strings; description says "the eight test targets" | text | none | none | editorial | "eight" is hard-coded in the description, and the description names "a ticketing queue" (keywords: "best proxy for ticketing"), a target the API does not measure; the count should follow the card-target count (TGT-9) and the copy drop the ticketing queue (TGT-15). |
| 0.2 | canonical, OG image `/og/proxy-index/targets.png`, breadcrumbs, `noindex` | `targets.astro`; OG card copy in `src/pages/og/proxy-index/[key].astro` `targets` | text / PNG | none | none | editorial | OG pills "Per-site success, Difficulty, Pass rules" are static. |
| 0.3 | JSON-LD `ItemList.numberOfItems` | `TARGETS.length` (targets.json `targets[]`, 8) | integer | none | `/v1/dataset → targets[]` filtered `group == null` and `source == "measured"` | exists-but-different-shape | API `targets[]` also lists group sources (`ai_overview`, `ai_chatgpt`, `ai_gemini`, `group` set) and catalog-only rows; client must filter. Only the 9 MB dataset file serves `targets[]`. |
| 0.4 | JSON-LD `itemListElement[].position`, `.name` | `TARGETS[i]`, `target.label` | ordinal, text | order = difficulty asc (export sort), ties dataset order | `/v1/dataset → targets[].label` | exists-but-different-shape | API orders by `sort_order` (framework `public.targets.sort_order`), not difficulty; difficulty is null for framework targets without a `site.targets` row. |
| 0.5 | JSON-LD `dateModified` | `TARGETS_GENERATED_AT` = targets.json `generatedAt` | ISO time | none | `/v1/dataset → generatedAt` (also every page answer's `generatedAt`) | exists | |
| 0.6 | JSON-LD `name` "Proxy Index test targets", `description`, `url`, `creator`, `about` (`/proxy-index#dataset`) | `targets.astro` `jsonLd`; `description` = the meta description | text, ids | none | none | editorial | `description` repeats the meta description, so it carries the same hard-coded "eight" and "ticketing queue" (TGT-15). |

### 1. Floating dock and search palette (shared `IndexDock`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1.1 | Range toggle (24h/7d/30d) in the dock | `IndexDock` shows it only with a `[data-pi-range-root]` | n/a | not present on this page | none | editorial | This page has no range root, so no toggle: per-target figures are fixed to one window. If 24h/7d/30d per-target answers are added (gap TGT-1), the page could get a toggle. |
| 1.2 | Palette network items (name, "score · #rank") | `pi-palette.tsx` `MEASURED[].name`, `fixed(score.composite)`, `standing.rank` | text, 1 dp, ordinal | search by text; Enter opens network page | `/v1/search?q=` / `/v1/search-index` network items; or RK `score`, `rank` (the residential `standing.rank`; `position` counts every kind) | exists-but-different-shape | Shared across pages; mapped in the Overview section. The tagline is in no list answer (HC-6), and the `/v1/search-index` network item has another subtitle and meta and old `/networks/<slug>` links (HC-8); see section 2 row 11.2. |
| 1.3 | Palette target items | `/proxy-index/search.json` → `entries[kind=target]` (`TARGETS` label, `copyFor().line`) | text | client search | `/v1/search-index → items[group=Targets]` (`title` = label, `subtitle` = "category · passCondition", `meta` = "difficulty n/5") | exists-but-different-shape | API subtitle is category + pass rule, the site uses its editorial one-liner; API lists every `targets[]` row (incl. group sources). |
| 1.4 | Palette footer "{networks} networks · {targets} targets" | `MEASURED.length`, home.json `targets` (8) | integers | none | `/v1/networks` row count; `/v1/dataset → measuredMarket.targetCount` | exists-but-different-shape | `targetCount` counts every target with `source == "measured"` (11 in `configs/targets.toml`: the 8 cards plus the 3 AI sources), not the 8 cards. |
| 1.5 | Palette fetch | `GET /proxy-index/search.json?locale=` once per visit (idle 2.5 s or on open) | JSON | cache 300 s | (website route) | editorial | Not an API call. |

### 2. Hero (`IndexHero`, `TargetsVisual`, `NetworkStrip`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 2.1 | Badge, title, highlight, highlights, buttons ("See every network" → #matrix, methodology link) | `targets.astro` | text | anchors | none | editorial | |
| 2.2 | Lead "…the same {n} targets…" | `TARGETS.length` | integer | none | `/v1/dataset → targets[]` count with `group == null`, measured | exists-but-different-shape | As 0.3. |
| 2.3 | Live line "{targets} targets · {networks} networks · scored over the last 24 hours" | `TARGETS.length`, `SHOWN.length` (MEASURED: residential, demo switch on) | integers + text | none | targets as 0.3; networks: RK `networks[]` with `kind == "residential"` count | exists-but-different-shape | "last 24 hours" is false against the API (30d). Pulsing dot implies live; nothing polls. |
| 2.4 | Window title "Eight targets, easiest first", "Live" label | hard-coded in `targets.astro` | text | none | targets count as 0.3 | editorial | "Eight" hard-coded; should be the count. |
| 2.5 | Pill "8 Targets" | `String(TARGETS.length)` | integer | none | as 0.3 | exists-but-different-shape | |
| 2.6 | Visual row label (short name) | `copyFor(target).short` (`TARGET_COPY` in targets-data.ts) | text | rows in `TARGETS` order | none | editorial | `TARGET_COPY` has `ticketing_queue` but no `ai_answers`; with API targets `ai_answers` falls back to its label. |
| 2.7 | Visual difficulty pips (5) | `target.difficulty` (targets.json `targets[].difficulty`) | 1-5 | none | `/v1/dataset → targets[].difficulty` | exists-but-different-shape | `TargetMeta.difficulty` is `site.targets` (display only, null without a catalog row). The scoring difficulty is `configs/methodology.toml [browser_difficulty]`, served only as `NP.browserOverall.<engine>.success.weights[id]` per network. The two can disagree. |
| 2.8 | Visual "Mean success" bar + % | `fieldMean(id)`: mean of `results[slug][id].successRate` over SHOWN with a value | fraction → `pct` 1 dp, "N/A" when null; bar width max(4, mean×100)%, 0 when null | SHOWN = residential networks | N × NP → `T(id).successRate`, client mean | exists-but-different-shape | Needs all networks' figures (N requests, or the 9 MB file); 30d not 24h; mixes engines on browser targets. Proposed: per-target fleet `meanSuccess` per engine in a targets answer (TGT-2). |
| 2.8s | Score row for 2.8 | none | 1-10 | per engine | N × NP → `T(id).engines.<engine>.scores.success.{score,rank,rankedOf,status}` | missing | A mean of rank scores over all ranked networks is always 5.5 by construction, so there is no meaningful "mean score". Show instead the target-level `rankedOf` and status (`ranked` / `all_similar` / `too_few_networks`) per engine; no per-target answer serves them (TGT-3). |
| 2.9 | Network strip: monogram, name, Index Score, "#rank" / "Unranked", "Ranked by Index Score across {n} networks" | `MEASURED[]` (home.json `networks[]`): `monogram` (= name.slice(0,2).toUpperCase()), `name`, `fixed(score.composite)`, `standing.rank` (index in residential list sorted by composite) | text, 1 dp, ordinal | residential only, score desc; CSS marquee | RK `name`, `score`, `rank`, `kind`; filter `kind == "residential"`, order by `position` | exists | Monogram derived client-side from `name`. `standing.rank` is the position within the residential subset: RK `rank` is the rank within the kind (same meaning). Range: home default 30d. |
| 2.9s | Score row for the strip (browser overall next to Index Score) | none | 1-10 | per engine | N × NP → `browserOverall.<engine>.success.score`, `.rank`, `.rankedOf` | exists-but-different-shape | Not in RK; needs one request per network. Proposed: add `browserOverall` to RK rows (TGT-4). |

### 3. Step bar (`IndexStepBar`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 3.1 | Steps "The targets / By network / Why it matters / Coming next" with hover questions | `TARGET_SECTIONS` (target-sections.ts) | text | sticky, scroll-spy, smooth scroll (`initIndexSteps`) | none | editorial | |

### 4. Section 01 "The targets" (`LadderSection`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 4.1 | Section label, title, lead "The same {n} targets…", Next card | `TARGET_SECTIONS[0]`, `TARGETS.length` | text, integer | none | count as 0.3 | editorial | Count part: as 0.3. |
| 4.2 | Card order and number "01".."08" | `TARGETS` order (difficulty asc) | ordinal | none | `/v1/dataset → targets[]` order | exists-but-different-shape | API order is `sort_order`; cards must drop rows with `group != null` (AI sources) and catalog-only rows. |
| 4.3 | Card icon by category | `ICONS[target.category]` (Baseline, E-commerce, Search, Social, Anti-bot, High-contention) | icon | none | `/v1/dataset → targets[].category` | exists-but-different-shape | `category` comes only from `site.targets`; null for a framework target without a catalog row (then `flag` icon). No category for `ai_answers` unless the catalog adds it. |
| 4.4 | Category chip | `target.category` | text | hidden when null | `/v1/dataset → targets[].category` | exists-but-different-shape | Same as 4.3. |
| 4.5 | Card title | `target.label` | text | none | `/v1/dataset → targets[].label` | exists-but-different-shape | Value exists; only in the 9 MB dataset file (no small targets answer, TGT-5). |
| 4.6 | Card one-liner | `copyFor(target).line` | text | none | none | editorial | Per-id copy; needs an `ai_answers` entry. |
| 4.7 | Card "Mean success" (1 dp % or N/A) | `fieldMean(id)` | fraction | SHOWN networks | N × NP → `T(id).successRate`, client mean | exists-but-different-shape | As 2.8. |
| 4.7s | Score row for 4.7 | none | 1-10 / status | per engine | N × NP → `T(id).engines.<engine>.scores.success` | missing | As 2.8s: show `rankedOf` and target-level status per engine (TGT-3), not a mean. |
| 4.8 | Difficulty steps (5 bars) + aria "Difficulty n of 5" | `target.difficulty` (0 when null) | 1-5 | none | `/v1/dataset → targets[].difficulty` | exists-but-different-shape | As 2.7. |
| 4.9 | Difficulty tone colour | `difficultyTone(difficulty)`: 1 green … 5 red | colour | clamped 1-5, null → 1 | (client) from `targets[].difficulty` | exists-but-different-shape | Client threshold; depends on 2.7. |
| 4.10 | Card mean bar width | `mean × 100` % | % | animated on scroll | from 4.7 | exists-but-different-shape | |
| 4.11 | Details "Read the exact rules": intro paragraph | `LadderSection.astro` | text | `<details>` open | none | editorial | Says "Each target is scored over the last 24 hours": API figures are 30d. |
| 4.12 | Rule per target: label, tag "Difficulty n of 5" | `target.label`, `target.difficulty` | text, 1-5 | none | `/v1/dataset → targets[].label`, `.difficulty` | exists-but-different-shape | As 4.5 / 2.7. |
| 4.13 | Rule "Passes when:" | `target.passCondition` | text | hidden when null | `/v1/dataset → targets[].passCondition` | exists-but-different-shape | API pass condition is `site.targets` else `public.targets.pass_condition` (configs/targets.toml); the mock's texts are the old site's and differ from the framework's. Only in the dataset file. |
| 4.14 | Rule "why it is in the suite" | `copyFor(target).why` | text | hidden when empty | none | editorial | |
| 4.15 | Engine per target (not shown today) | none | `http` / `browser` | none | `/v1/dataset → targets[].engine` | exists-but-different-shape | Proposed for the score rows: browser cards get scores, HTTP cards do not. `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 4.16 | Group / sources (not shown today) | none | ids | none | `/v1/dataset → targets[].group`; group's breakdown in `T(ai_answers).sources[]` | exists-but-different-shape | A card list must hide `group != null`. `TargetMeta.Sources` is `json:"-"` (not served on the meta). `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 4.17 | Data strings passed through `t()` as translation keys: card title, category chip, "Passes when:" text, rule labels, matrix column headers, cell tooltips, latency rule `{target}`, why sentence `{easyTarget}`/`{hardTarget}`, JSON-LD `itemListElement[].name` | `t(locale, target.label)`, `t(locale, target.category)`, `t(locale, target.passCondition)` | text | none | `/v1/dataset → targets[].label`, `.category`, `.passCondition` (English, from `site.targets` / `public.targets`) | editorial | Untranslated today (the mock's labels have no catalog entries), and every catalog wording change silently falls back to English. Key translations by `targets[].id` (TGT-14). |

### 5. Section 02 "By network" (`MatrixSection`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 5.1 | Section label, title, lead with link "every network in the Index", Next card | `TARGET_SECTIONS[1]` | text | link to /proxy-index | none | editorial | Lead says "last 24 hours". |
| 5.2 | Column header: target label | `TARGETS[c].label` | text | columns in `TARGETS` order | `/v1/dataset → targets[].label` | exists-but-different-shape | As 4.2/4.5. |
| 5.3 | Column header: difficulty steps + tone | `target.difficulty`, `difficultyTone` | 1-5 | none | `/v1/dataset → targets[].difficulty` | exists-but-different-shape | As 2.7. |
| 5.4 | Rows: networks in leaderboard order | `SHOWN` (MEASURED) | ordinal | residential, composite desc; sticky first column | RK (30d) `position` filtered `kind == "residential"` | exists | RK orders scored networks by score, then unscored ones by name; `position` counts every kind, so the residential rows' positions can have gaps. |
| 5.5 | Row link to `/proxy-index/networks/{slug}`, title "Read the {name} review" | `network.slug`, `network.name` | URL, text | click | RK `slug`, `name` | exists | |
| 5.6 | Row monogram | `network.monogram` | 2 letters | none | RK `name` (client slice) | exists | |
| 5.7 | Row "{n} of {total} tested" | count of targets with non-null `successRate` | integers | none | N × NP → count of `T(*)` with `successRate != null` (or `requests > 0`) | exists-but-different-shape | Needs one request per network. |
| 5.7s | Score row for 5.7: targets ranked | none | integer | per engine | N × NP → `browserOverall.<engine>.success.targets.length`, or count of `T(*).engines.<engine>.scores.success.status == "ranked"` | exists-but-different-shape | One request per network. |
| 5.8 | Cell: success % (1 dp) | `results[slug][id].successRate` via `cell()` | fraction → `pct` | none; hover title "{network} · {target}" | N × NP → `T(id).successRate` (also DSF `.targets[]`) | exists-but-different-shape | 30d not 24h; N requests for the matrix; browser cells are `T(id).engine`'s figures (engine can differ per cell). |
| 5.8s | Score row for 5.8: success score | none | 1-10, rank of M, status | engine pick | N × NP → `T(id).scores.success.{score,rank,rankedOf,status}` (engine `T(id).engine`); per engine `T(id).engines.<engine>.scores.success.*` | exists-but-different-shape | Field exists per network; matrix needs N requests (TGT-6). Show status text when not `ranked` (`not_enough_data`, `too_few_networks`, `all_similar`). Null on HTTP targets and on snapshots before target scores. |
| 5.8e | Engine of the cell (proposed badge) | none | `camoufox` / `chrome` | none | N × NP → `T(id).engine`; `T(id).engines` keys | exists-but-different-shape | Needed so a column does not compare Chrome against Camoufox; N requests. |
| 5.9 | Cell: N/A when not tested | `missing(successRate)` | "N/A" | never counted as 0 | `T(id).successRate == null` (`requests` 0) | exists | Same N/A rule; API also lists the path in `profile.na`. |
| 5.10 | Cell heat bar (fill from 70%, colour bands ≥97, ≥93, ≥88, ≥80, below) | `heatSolid(rate)`, `fill = (rate−0.7)/0.3` | colour, % | hard-coded thresholds in targets-data.ts `heat()` | (client) from 5.8 | exists-but-different-shape | Client presentation. With scores a tone could come from `scores.success.score` (1-10) instead. |
| 5.11 | Best-in-column highlight | `BEST[c] = max(successRate)` over SHOWN; `rate === BEST[c] && SHOWN.length > 1` | colour | per column | client max over N × NP `T(id).successRate` | exists-but-different-shape | Mixes engines. Score analogue: `T(id).engines.<engine>.scores.success.rank == 1` (ties share an average rank, e.g. 1.5: use the minimum rank). |
| 5.12 | Count-up animation of figures (85% → 100% of value) | `data-v = rate×100` | % | IntersectionObserver, 800 ms | from 5.8 | editorial | Presentation only. |
| 5.13 | "Average" column | mean of the row's non-null `successRate` (all targets, HTTP and browser, unweighted) | fraction → `pct`; "N/A" when the row has no tested target | none | N × NP → client mean of `T(*).successRate` | exists-but-different-shape | No API field; the snapshot's network `successRate` (`NP.performance.successRate`, RK `successRate`) is HTTP-only (browser runs never enter window metrics), so it is not this number. |
| 5.13s | Score row for 5.13: overall browser score | none | 1-10, rank of M, status, targets, weights | per engine | N × NP → `browserOverall.<engine>.success.{score,rank,rankedOf,status,targets,weights}` (DSF `productMeasurements.residential.browserOverall`) | exists-but-different-shape | Difficulty-weighted over ranked browser targets only (`ai_answers` once; HTTP targets excluded); not written when the snapshot has none. N requests (TGT-4/TGT-6). No overall for challenge/block. |
| 5.14 | `data-live-key={slug}` on rows | slug | attribute | no consumer found | none | editorial | No live update or polling on this page. |
| 5.15 | Legend bands "97% and up … below 80%", "N/A: not tested on this target yet" | `heatSolid()` samples | text | none | none | editorial | |
| 5.16 | Legend "97.2% Best on this target" | `pct(0.972)` hard-coded in MatrixSection.astro | % | none | none | editorial | Sample value, not data. |
| 5.17 | Details "Read the exact rules": 4 rules (challenges count as failures, why only 24 hours, latency on one target, N/A) | MatrixSection.astro | text | `<details>` | none | editorial | "Why only 24 hours" contradicts the API's 30d per-target window; either the API serves 24h (TGT-1) or the copy changes. Latency rule names `httpTarget.label`: `/v1/dataset → targets[id=generic_http].label`. |
| 5.18 | "Plain HTTP latency, p50" list: network name + p50 | `cell(slug,'generic_http').latencyP50` | ms → `ms()` (≥1000 as s, 2 dp; "N/A" when null) | SHOWN order | RK `?range=24h` → `latencyP50` (headline = `generic_http` under load); or N × NP → `T(generic_http).latencyP50` (30d) | exists | RK 24h is one request and matches the 24h copy; the target entry is 30d. |
| 5.19 | (in JSON, not rendered) `captchaRate` per cell | `results[slug][id].captchaRate` | fraction | none | N × NP → `T(id).captchaRate` (browser: `outcomes.challenge` of `engine`) | exists-but-different-shape | N requests; 30d. |
| 5.19s | Score row for 5.19 | none | 1-10 | per engine | N × NP → `T(id).scores.challenge.*`, `T(id).engines.<engine>.scores.challenge.*` | exists-but-different-shape | Lower challenge share scores higher. N requests. |
| 5.20 | (in JSON, not rendered) `blockRate` per cell | `results[slug][id].blockRate` | fraction | none | N × NP → `T(id).blockRate` (browser: `outcomes.block`) | exists-but-different-shape | As 5.19. |
| 5.20s | Score row for 5.20 | none | 1-10 | per engine | N × NP → `T(id).scores.block.*`, `T(id).engines.<engine>.scores.block.*` | exists-but-different-shape | As 5.19s. |
| 5.21 | (in JSON, not rendered) `requests` per cell | `results[slug][id].requests` | tries | none | N × NP → `T(id).requests`; per engine `T(id).engines.<engine>.requests`, `.successes` | exists-but-different-shape | N requests; score needs ≥100 tries per engine (`not_enough_data`). |
| 5.22 | (available, not used) outcome shares per cell | none | fractions summing to 1 | none | N × NP → `T(id).outcomes.{ok,challenge,block,loginWall,timeout,proxyError,noContent,noAnswer,otherError}`; per engine `T(id).engines.<engine>.outcomes.*` | exists-but-different-shape | Browser targets only. Scores rank `outcomes.ok` (success), `.challenge`, `.block`; the other outcomes have no score. |
| 5.22s | Score row for 5.22 | none | 1-10 | per engine | `outcomes.ok` → `scores.success`; `outcomes.challenge` → `scores.challenge`; `outcomes.block` → `scores.block` | exists-but-different-shape | Scores rank the outcome share, not `successRate` (`target_scores.METRICS`); they may differ slightly (unverified). |
| 5.23 | (available, not used) page load p50/p90 per cell | none | ms | none | N × NP → `T(id).pageLoadP50`, `.pageLoadP90`; per engine `T(id).engines.<engine>.pageLoadP50/P90` | exists-but-different-shape | Browser targets only; no score for page load. `latencyP50` stays null on browser targets. |
| 5.24 | (available, not used) AI group breakdown | none | fraction, tries | none | N × NP → `T(ai_answers).sources[].{target,label,successRate,requests,outcomes}` | exists-but-different-shape | Sources have no per-source score in `sources[]`; their own `T(ai_overview…)` entries carry `scores`. |
| 5.25 | Table caption (screen readers) "Success rate per network and target, last 24 hours" | `MatrixSection.astro` `<caption class="sr-only">` | text | none | none | editorial | Sixth "24 hours" claim (with 2.3, 4.11, 5.1, 5.17 and the details intro below); the API's per-target figures are 30d (TGT-1). |
| 5.26 | Details intro "Each cell is the share of requests to that target that met its written pass rule, over the last 24 hours. Every request counts, and every time slot weighs the same…" | `MatrixSection.astro` (`IndexDetails` first paragraph) | text | `<details>` | none | editorial | "24 hours" is wrong against the API (TGT-1). "Every time slot weighs the same" matches the API: per-target `successRate` is the snapshot's time-weighted `success_rate` (calculator `timed()` per target). |
| 5.27 | Latency rule fallback "Speed is measured on the plain HTTP target only…" and an empty latency list when no `generic_http` target exists | `httpTarget = TARGETS.find(id === 'generic_http')`; list rendered only when `httpTarget` | text | none | `/v1/dataset → targets[id=generic_http]` | editorial | Empty state. `generic_http` is a framework target, so with the API the fallback does not occur. |

### 6. Section 03 "Why it matters" (`WhySection`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 6.1 | Label, title, lead with link to /proxy-index/compare#by-target, Next card | `TARGET_SECTIONS[2]` | text | links | none | editorial | |
| 6.2 | "mean success falls from {easy} on {easyTarget} to {hard} on {hardTarget}" | `fieldMean(TARGETS[0].id)`, `fieldMean(TARGETS.at(-1).id)`, their labels; hidden when either is null or when easiest and hardest are the same target | fraction → `pct`, text | first and last target in difficulty order | N × NP → client means of `T(id).successRate`; labels `/v1/dataset → targets[].label` | exists-but-different-shape | Needs all networks; "hardest" depends on difficulty order (API orders by `sort_order`; with the API as it is, `difficulty` is null for every card without a `site.targets` row (the repo defines none), so the order falls back to `sort_order` and the "hardest" card is `ai_answers` (`sort_order` 80); once W4 defaults difficulty to the scoring weight, the hardest cards are the difficulty-4 ones (`instagram_web`, `cloudflare_challenge`, `ai_answers`), the last of them by `sort_order`. `ticketing_queue` is not measured). Proposed: TGT-2 fleet means. |
| 6.2s | Score row for 6.2 | none | 1-10 | per engine | none | missing | A score drop is meaningless (mean score is constant). Suggest the copy use rates only, or "N of M networks ranked" per target (TGT-3). |
| 6.3 | Three reason cards (icon, title, body, links to guide and /proxy-index/overlap) | WhySection.astro `reasons` | text | links | none | editorial | |
| 6.4 | Link "How one request gets a verdict" | methodology#pipeline | link | none | none | editorial | |

### 7. Section 04 "Coming next" (`WishlistSection`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 7.1 | Label, title, lead with link "open to proposals" | `TARGET_SECTIONS[3]` | text | link /proxy-index/contribute | none | editorial | |
| 7.2 | Three admission rules | WishlistSection.astro `rules` | text | none | none | editorial | |
| 7.3 | Shortlist of 6 proposed targets (name, why) | WishlistSection.astro `shortlist` | text | none | none | editorial | Could later come from catalog rows not yet measured (`targets[]` with `source == "catalog"`); none today. |
| 7.4 | "Suggest a target" button (mailto, or opens live chat) | `openChat(...)` | link | click | none | editorial | |

### 8. CTA and footer

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 8.1 | CTA "Test Shifter on your own target", footer | `targets.astro`, shared sections | text | none | none | editorial | |

### 9. Mock file `src/data/proxy-index/targets.json` (every key)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 9.1 | (meta) | `generatedAt` | ISO time | none | `/v1/dataset → generatedAt` | exists | Export: `dataset.generatedAt`. |
| 9.2 | (not rendered) | `source` ("proxy-reviews siteapi /v1/dataset") | text | none | none | editorial | |
| 9.3 | Suite | `targets[].{id,label,category,difficulty,passCondition}` | as above | export sorts by difficulty (null → 99) | `/v1/dataset → targets[]` (`TargetMeta`) | exists-but-different-shape | See section 10. |
| 9.4 | Matrix and means | `results[slug][targetId].{successRate,captchaRate,blockRate,latencyP50,requests}` (5 slugs × 8 targets; export from old-site `providers[].targets[]`, 4 dp, latency 0 dp) | fractions, ms, tries | keyed by slug for every network; page filters residential | `/v1/dataset → providers[].targets[]` (headline kind) / `providers[].productMeasurements.residential.targets[]`; or N × NP → `targets[]` | exists-but-different-shape | The only one-request source is the 9 MB dataset file. Array keyed by `target`, not an object by id. 30d. |

### 10. Target metadata: page `Target` vs `Dataset.targets[]` (`TargetMeta`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 10.1 | id | `targets[].id` | text | none | `targets[].id` | exists-but-different-shape | Ids differ in content: mock has `ticketing_queue` (only a demo catalog row in tests; `is_demo` rows are not read); the framework has `ai_answers` + sources `ai_overview`, `ai_chatgpt`, `ai_gemini` (configs/targets.toml). `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.2 | label | `targets[].label` | text | none | `targets[].label` (`site.targets`, else `public.targets`) | exists-but-different-shape | `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.3 | category | `targets[].category` | text | none | `targets[].category` (`site.targets` only) | exists-but-different-shape | Null for framework targets without a catalog row; `ai_answers` needs one. |
| 10.4 | difficulty | `targets[].difficulty` | 1-5 | sort key | `targets[].difficulty` (`site.targets`, the same values as the methodology) | exists | The owner's fixed difficulties (2026-11-12, TARGET-DIFFICULTY-FIXED-1, never derived from results, changed only on the owner's word; `api/FIELDS.md` "Target difficulty"): `generic_http` and `generic_https` 1 (`[http_difficulty]`), amazon 2, instagram 3, google 4, cloudflare 4, linkedin 5, `ai_answers` and `ai_overview`/`ai_chatgpt`/`ai_gemini` 5 (`[browser_difficulty]`). The catalog, `/v1/meta` and `/v1/targets` `targets[].scoreWeight` (browser targets; null on the HTTP ones) and `browserOverall`/`successScore` `weights` all carry them (`tests/test_target_difficulty.py`). |
| 10.5 | passCondition | `targets[].passCondition` | text | none | `targets[].passCondition` | exists-but-different-shape | Values differ from the mock (framework wording). `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.6 | engine | not in the page type | `http` / `browser` | none | `targets[].engine` | exists-but-different-shape | Page should read it to decide which cards get scores. `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.7 | group | not in the page type | id or null | hide `group != null` | `targets[].group` | exists-but-different-shape | `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.8 | sources | not in the page type | ids | none | not on `TargetMeta` (`json:"-"`); per network `T(ai_answers).sources[]` | exists-but-different-shape | Fine for the page; the group card's breakdown is per network. |
| 10.9 | source, na | not in the page type | text, paths | none | `targets[].source` (`measured`/`catalog`), `targets[].na` | exists-but-different-shape | Page should show only `source == "measured"`. `targets[]` is served only in the dataset file (`Dataset.Targets`; no route returns `TargetMeta`), as rows 0.4, 4.5, 5.2 and 10.11 (TGT-5; W4, W7). |
| 10.10 | order | difficulty asc (export) | ordinal | none | `targets[]` order = `sort_order` | exists-but-different-shape | Page sorts itself or the catalog's `sort_order` must follow difficulty. |
| 10.11 | where served | (static import) | | | `targets[]` only in `/v1/dataset` (9 MB file); `/v1/search-index` items carry label, category, pass rule, difficulty as display strings | exists-but-different-shape | No small endpoint returns `TargetMeta[]` (TGT-5). |

### 11. Can the API serve the whole network × target matrix in one request?

No, not as a page answer. Today the per-network per-target figures are only in:
- `/v1/networks/{slug}?kind=residential → profile.targets[]` and `profile.browserOverall` (one request per network: 18 requests per view, each answer up to 200 KB because it also carries days, ranges, top-50 countries, stats and records), or
- the dataset file via `/v1/dataset` (`providers[].productMeasurements.<kind>.targets[]`, `.browserOverall`, plus `targets[]` meta): technically one request, but it is the whole ~9.2 MB dataset (gzip ~0.5 MB), not meant as a page answer.
`/v1/rankings`, `/v1/networks` and `/v1/networks/all` carry no per-target figures. The dataset `pages` map holds every `/v1/networks/<slug>?kind=` answer, each with `profile.targets[]` (scores included) and `profile.browserOverall`, so it is the N-network option inside the one file. Both sources hold the 30d window only. Proposal in TGT-6.

### Further details (website rules, no API gap)

- The section-01 lead also mentions a "ticketing queue" (`target-sections.ts`; TGT-15).
- Matrix rows link to network pages that rewrite to /404 without a `reviewFor` entry.
- A network missing from `targets.json` renders an all-N/A row (`?? EMPTY`).
- Row order and `standing` differ when `SHOW_DEMO` is false (HC-14).
- Bars are clamped with `Math.max(4, …)`; ties are all marked best.
- JSON-LD ItemList `@id` / `mainEntity`; the page also uses `IndexDetails`, `IndexRule`, `IndexIcon`, `ui.ts` and `index-countup-client.ts` (presentational, section 2 row 12.7).
- Row 5.18 (HTTP latency) has no rank-based score: latency is not scored.

### Gaps summary

1. TGT-1 window: per-target figures and scores for 24h (page copy "last 24 hours") and 7d — serve `targets[]`/`browserOverall` per range (`?range=` on the targets answer, TGT-6) — snapshot `windows["24h"|"7d"].targets[id]` (metrics, browser, scores) and `windows[*].browser_overall`.
2. TGT-2 fleet-mean: mean success per target across the shown networks (visual, ladder, why line), per engine — `targets[].fleet.<engine>.meanSuccess` (+ `networks` tested) in the targets answer — computed from the snapshot's per-network `targets[id]` (kind filter residential).
3. TGT-3 target-level score status: per target and engine, `rankedOf` and the shared status (`ranked`/`all_similar`/`too_few_networks`) as a target-level fact instead of a meaningless mean score — `targets[].scoreStatus.<engine>.{status,rankedOf}` — snapshot `w.targets[id].browser.engines.<engine>.scores` (same for every network).
4. TGT-4 overall in lists: `browserOverall.<engine>.success.{score,rank,rankedOf,status}` on `/v1/rankings` rows (strip, matrix average) — RK `networks[].browserOverall` — snapshot `w.browser_overall`.
5. TGT-5 target meta: no small endpoint for `TargetMeta[]` (label, category, difficulty, passCondition, engine, group, source) — `/v1/targets` → `targets[]` (cards only or with `group`) — `site.targets` + `public.targets` (already built as `Dataset.targets`).
6. TGT-6 matrix in one request: network × target cells for all networks — `GET /v1/targets?kind=residential&range=24h|7d|30d` → `{generatedAt, range, kind, targets: TargetMeta[] (+ TGT-2, TGT-3), networks: [{slug, name, position, rank, browserOverall, cells: {<targetId>: {successRate, captchaRate, blockRate, requests, latencyP50, engine, scores, engines.<engine>.{requests, successes, outcomes, pageLoadP50, pageLoadP90, scores}, outcomes, pageLoadP50, pageLoadP90, sources[] {target, label, successRate, requests, outcomes} (group cells only, so the `ai_answers` breakdown, row 5.24, needs no request per network)}}}]}`, rows in RK order, precomputed per build and added to the dataset file `pages` — snapshot windows' `targets` per product group.
7. TGT-7 difficulty mismatch: `TargetMeta.difficulty` (catalog, display) vs scoring weight (`[browser_difficulty]`) — add `targets[].scoreWeight` or default `difficulty` to it when the catalog is empty — methodology settings / `browserOverall.weights`.
8. TGT-8 category for framework-only targets (`ai_answers`, null category/difficulty without a `site.targets` row) — fill `site.targets` rows for the browser targets — catalog.
9. TGT-9 card count: count of card targets (`group == null`, measured) for hero/JSON-LD/pill; `measuredMarket.targetCount` counts 11 (the 8 cards plus the 3 AI sources) — `targets` answer `cardCount` or `measuredMarket.cardTargetCount` — `Dataset.targets`.
10. TGT-10 engine mixing: matrix column, best cell and fleet means mix Chrome and Camoufox when networks' main `engine` differs — the targets answer should expose per-engine cells (`engines.<engine>`) and a fleet `engine` choice per target — snapshot `browser.engines`.
11. TGT-11 score peer group: scores rank all product groups of a window (all kinds), the page shows residential only — confirm with the architect; if per kind is wanted, the calculator must rank per kind — `target_scores.py`.
12. TGT-12 row "{n} of {total} tested" and "Average": client counts/means over N network answers — served by TGT-6 (`cells`), plus `networks[].testedTargets` and `networks[].meanTargetSuccess` (unweighted) if the page keeps that average — snapshot targets.
13. TGT-13 search palette target subtitle: API `/v1/search-index` target item subtitle is "category · passCondition", site uses editorial one-liner — keep site copy (no API change) or add `TargetMeta.summary` — catalog.
14. TGT-14 translation keys: target `label`, `category` and `passCondition` from the data are used as `t()` keys (cards, rules, matrix headers, tooltips, why sentence, JSON-LD names, row 4.17); API wording has no entries and any catalog edit falls back to English — key the translations by `targets[].id` (website, `TARGET_COPY`-style per-id copy).
15. TGT-15 hard-coded copy: hero `windowTitle` "Eight targets, easiest first", the meta and JSON-LD description "the eight test targets, from an open page to a ticketing queue", and the keyword "best proxy for ticketing" hard-code a count and a target the API does not measure (rows 0.1, 0.6, 2.4) — build the count from the card count (TGT-9), drop the ticketing queue (website).
16. TGT-16 suite switch on the website: the page must take cards as `targets[]` with `group == null` and `source == "measured"`, sort by `difficulty` itself (nulls last), add `TARGET_COPY.ai_answers` and an icon for its category, remove `ticketing_queue`, and give the `ai_answers` cell a place for `sources[]` (tooltip or expandable cell) — website — `/v1/dataset → targets[]`, NP `T(ai_answers).sources[]` (later the TGT-6 answer).

### Performance notes

- Today (static mock) the page makes no runtime data request; the palette fetches the website's own `/proxy-index/search.json` once per visit. Nothing polls (the "Live" dot and `data-live-key` are decorative).
- With the current API the page needs, per view: `/v1/rankings?range=30d` (row order, names, scores; ~18 rows), `/v1/rankings?range=24h` (HTTP latency list), `/v1/dataset` for `targets[]` meta (the 9.2 MB file), and either the same dataset file for all cells or 18 × `/v1/networks/{slug}?kind=residential` (up to 200 KB each, ~3.6 MB, of which the targets block is a few KB). That is 3 to 21 requests and 4-10 MB for about 18 × 8 = 144 cells (×2 engines for scores), all 30d.
- Payload actually needed: 18 networks × 8 card targets (+3 AI sources) × (5 rates + 3 scores × 4 fields + engine + up to 2 engines × ~15 fields) ≈ 18 × 11 × 45 ≈ 9k numbers, about 40-60 KB raw (5-8 KB gzip) per range; × 3 ranges if served per range.
- Precompute per build (TGT-6): one `/v1/targets?kind=&range=` answer per kind × range (e.g. 4 kinds × 3 ranges = 12 answers, each well under `MaxAnswerBytes` 200 KB), with fleet means and target-level score status (TGT-2, TGT-3) included, stored in the dataset file's `pages` like the other page answers. The scores are already computed by the calculator for every window; the API only has to map `windows[range]` instead of `windows["30d"]`.
- With TGT-4, the strip and the matrix "Average" score need no per-network request.

## 4. /proxy-index/networks — directory of every residential network as ranked, sortable cards

Source files read (website root):
- src/pages/proxy-index/networks/index.astro
- src/components/proxy-index/NetworksContent.astro
- src/components/proxy-index/pi-networks.tsx (NetworksHero, NetworkDirectory, SortMenu)
- src/components/proxy-index/pi-network-cards.tsx (NETWORKS, NetworkCard, SCALES, rate, RatingLegend)
- src/components/proxy-index/pi-network-strip.tsx (NetworkStrip)
- src/components/proxy-index/pi-palette.tsx (CommandPalette, page="networks")
- src/components/proxy-index/proxy-index-home.tsx (TimeDock, SearchTrigger)
- src/components/proxy-index/pi-profile-preview.tsx (ProfilePreview)
- src/components/proxy-index/pi-charts.tsx (CountUp, Sparkline, Meter)
- src/components/proxy-index/pi-data.ts (DATA = home.json, MEASURED, SHOW_DEMO, windowOf, formatters, TONE)
- src/components/proxy-index/pi-ui.tsx (SURFACE, Swap, Sweep, DashStyles)
- src/components/proxy-index/pi-faq.ts (networksFaq)
- src/components/proxy-index/review/review-data.ts (reviewFor, used by search.json)
- src/pages/proxy-index/search.json.ts, src/pages/proxy-index/dataset.json.ts
- src/pages/og/proxy-index/[key].astro (key `networks`)
- src/hooks/use-live-while-visible.ts
- src/data/proxy-index/home.json (the only data file the list page reads; networks.json is read only indirectly, through search.json's `reviews`; editorial.json is not used)
- scripts/proxy-index/export-home.mts (origin of every home.json field)

API files checked: api/README.md (Endpoints), api/FIELDS.md (Provider, Window statistics, Browser scores, Per-page answers, `/v1/rankings`, dataset file v4), api/internal/build/{types.go, rankings.go, pages.go, profile.go, measured.go, allnetworks.go, search.go, browser.go}, api/internal/model/{model.go, target.go}, api/internal/server/server.go.

Key facts used below:
- The page reads no API at run time today. Everything comes from `home.json` (`DATA`) at build time. The only runtime fetch is the palette's `GET /proxy-index/search.json?locale=` (a website route).
- `NETWORKS = MEASURED` = `DATA.networks` filtered `kind === "residential"`. With `SHOW_DEMO = true` (preview) they are sorted by `score.composite` and re-numbered `standing.rank = i+1`. With `SHOW_DEMO = false` (launch) the order is `DATA.measured` (old `measuredLeaderboard`) and `standing` is old `kindStanding(p)`. Mock data: 5 networks, 4 residential, so the page shows "4 residential networks".
- Per network window `windows[range]` (24h/7d/30d) comes from old `windowStats(p, range)`. `countries` is `p.pool.countries` (the same in every range). `listRate` is old `format.listRate(p.pricing)` (the same in every range). `score` is `p.score` (the 30d balanced score, the same in every range).
- The best API match is `/v1/rankings?range=` (one row per network, precomputed per build, `Pages.Rankings`). It has no `listRate`, `tagline`, `kind` filter, target count, fleet requests or weights.

---

### 1. Page head, SEO and structured data (index.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `<title>`, meta description, keywords, canonical, breadcrumbs, `noindex` | literals through `t()` in index.astro | text | locale | — | editorial | The description mentions success rate, latency, unique IPs and price per GB, but contains no numbers. |
| 2 | OG image `/og/proxy-index/networks.png` | og/proxy-index/[key].astro `PAGES.networks` (fixed copy, pills "Measured scores", "Pricing", "User reviews") | PNG | request host | — | editorial | No data. |
| 3 | JSON-LD `ItemList.numberOfItems` | `NETWORKS.length` | integer | — | `/v1/rankings?range=30d` → count of `networks[]` with `kind == "residential"` | exists | The client filters by kind. See NETS-4 for networks whose headline kind is not residential. |
| 4 | JSON-LD `itemListElement[]` (position, name, url) | `NETWORKS[i].name`, `.slug`, order = MEASURED order (score desc) | list | order = score desc | `/v1/rankings?range=30d` → `networks[].name`, `.slug`, order by `position` (filtered to residential) | exists | URL is built as `/proxy-index/networks/{slug}`. Rendered on the server, so it needs the API at SSR time or at build time. |
| 5 | FAQPage JSON-LD + visible FAQ | `networksFaq(locale)` (pi-faq.ts), 5 Q&A | text | — | — | editorial | The answers contain no figures. |
| 5a | JSON-LD constants: `@id` (`{pageUrl}#networks`), `name` "Residential proxy networks in the Proxy Index", `itemListOrder` `ItemListOrderDescending`, `publisher` (`ref(ORG_ID)`) | literals in index.astro; `absUrl('/proxy-index/networks', locale)` | text / URL | locale | — | editorial | `itemListElement[].position` is `i + 1` after the residential filter, not the rankings `position` (which counts every kind): renumber client-side. |

### 2. NetworksHero (pi-networks.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 6 | Badge "Proxy Index · Networks", h1, intro sentence with a link to /proxy-index | literals | text | — | — | editorial | |
| 7 | Stat "Residential networks" | `NETWORKS.length` | integer; CountUp below 10,000 | none | `/v1/rankings?range=30d` → count of residential `networks[]` | exists | |
| 8 | Stat "Targets" | `DATA.targets` = old `dataset.targets.length` | integer, CountUp | none | `/v1/dataset` → `targets[]` length (whole 9 MB file), or the count of Targets-group items in `/v1/search-index` | exists-but-different-shape | No small answer carries the count. Proposed: `targets` (count) on `/v1/rankings`. Note that `measuredMarket.targetCount` counts framework targets only, which may not equal `dataset.targets.length`. |
| 9 | Stat "Requests (30 days)" | `windowOf(DATA.market.windows, "30d").requests` = old `measuredMarketViews()[30d].fleet.requests` | count; `compact()` at 10,000 or more (e.g. "195k"); "N/A" when null | always 30d (not tied to the dock) | `/v1/networks/all` → `ranges["30d"].requests` | exists-but-different-shape | That answer is large (days, top 50 countries x 3 ranges, lists) for one number. It also counts the groups of providers kept off the site (`listed=false`), while the old fleet counted providers. Proposed: `fleet.requests` per range on `/v1/rankings`. The "30 days" label is hard-coded in pi-networks.tsx. |
| 10 | N/A state of a stat | `missing(value)` → "N/A" | text | — | null fields | exists | |

### 3. NetworkStrip (pi-network-strip.tsx), scrolling at the foot of the hero

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 11 | "Ranked by Index Score across {n} networks" | `MEASURED.length` | integer | — | `/v1/rankings?range=30d` → residential row count | exists | |
| 12 | Per network monogram | `n.monogram` = `name.slice(0,2).toUpperCase()` (export script) | 2 letters | — | `/v1/rankings` → `networks[].name` (client derives the monogram) | exists | Display rule, not data. |
| 13 | Per network name | `n.name` | text | order = MEASURED (score desc); the track repeats until 12 or more items, CSS loop 60 s | `/v1/rankings?range=30d` → `networks[].name` in `position` order | exists | `NetworkDirectory` sorts the same array in place (`NETWORKS.sort`), so the strip's order follows the chosen sort after a re-render. This is a website bug, not an API matter. |
| 14 | Per network Index Score | `fixed(n.score.composite)` (1 dp) = old `p.score.composite` (30d balanced) | 0-100, 1 dp, "N/A" | not range-dependent | `/v1/rankings?range=30d` → `networks[].score` | exists | `rankingRow` uses the headline group's 30d `scores.balanced.score`, the same source as `Provider.score.composite` (measured.go `balancedScore(30d)`). |
| 15 | Per network rank badge `#r` / "Unranked" | `n.standing.rank` (preview: 1..N among residential by score; launch: old `kindStanding(p).rank`) | `#n` | — | `/v1/rankings?range=30d` → `networks[].rank` (within kind) | exists-but-different-shape | `rankingRow` gives a null rank to a scored network that is alone in its kind, but `kindStanding` (Go `ScoreView.KindStanding`) shows "#1 of 1". So the strip would show "Unranked" where the old site showed #1. See NETS-3. |

### 4. NetworkDirectory: header, toolbar, dock, sort (pi-networks.tsx, proxy-index-home.tsx TimeDock)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 16 | Section header "Directory", h2, line "Green is good, amber is fair, red is weak…" | literals | text | — | — | editorial | |
| 17 | Toolbar "{n} residential networks" | `NETWORKS.length` | integer | — | `/v1/rankings?range=` → residential row count | exists | |
| 18 | Rating legend Good / Fair / Weak dots | `RATING_LABEL`, `TONE[*].stroke` | colour key | — | — | editorial | |
| 19 | Sort menu: Index Score (default), Success, Latency, Price, Pool size | `SORTS`; key = `n.score.composite`, `w.successRate`, `w.latencyP50`, `w.listRate`, `w.uniqueIps` of the selected range | listbox; Esc/outside click closes it | Score, Success and Pool sort descending; Latency and Price ascending; missing values last (+Infinity); a stable `Array.sort` | `/v1/rankings?range=` → `score`, `successRate`, `latencyP50`, `uniqueIps` (client sorts); Price → no field | exists-but-different-shape | The Price sort needs `listRate` (NETS-1). Score sort: the website uses the 30d score in every range, while `/v1/rankings?range=24h` carries the 24h score. To keep the current behaviour, read the score from the 30d answer, or let the API keep the 30d score in every range. The API's own order (`position`) is score desc, unscored last by name, which equals the default sort. |
| 20 | Range toggle 24h / 7d / 30d (floating dock, default 30d) | `RANGES`, `DEFAULT_RANGE`; `windowOf(n.windows, range)` | segmented control; labels "Last 24h/7d/30d" | changes every card figure and sort key; the dock shows only while the list is in view (IntersectionObserver) | `/v1/rankings?range=24h\|7d\|30d` (3 precomputed answers) | exists | One request per range, or prefetch all 3. |
| 21 | Dock "Live" dot | static | badge | none (no polling) | `/v1/version` (if it should become live) | editorial | Hard-coded. Nothing on this page polls. Making it live would mean polling `/v1/version` and refetching on a revision change. |
| 22 | Dock search trigger and ⌘K / Ctrl K / `/` shortcuts | — | button | opens the palette (section 6) | — | editorial | |
| 23 | Empty state "No networks match these filters" + "Clear filters" | `list.length === 0` | text | "Clear filters" resets sort to score | `/v1/rankings` → empty `networks[]` | exists | There are no filters; the state shows only when no residential network exists. No loading or error state exists: the data is static at build time. Once the page fetches the API, it needs a loading and an error state. |

### 5. NetworkCard, one per network (pi-network-cards.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 24 | Card link to `/proxy-index/networks/{slug}` | `n.slug` | URL | click | `/v1/rankings` → `networks[].slug` | exists | Every card links, but `[slug].astro` rewrites to /404 unless `reviewFor(slug)` finds the slug in networks.json (today all have one, since export-networks writes every residential provider). With the API, link only networks whose page renders (`/v1/networks/{slug}` answers 200). The JSON-LD `url` (row 4) has the same rule. |
| 25 | Kind filter (only residential cards) | `n.kind === "residential"` (= old `p.primaryKind`) | — | applied to the whole list | `/v1/rankings` → `networks[].kind` (headline kind), `kinds[]` | exists-but-different-shape | The rankings row's figures are the headline kind's. A network whose residential product is not its headline kind is dropped, as on the website, and its residential figures would need `/v1/networks/{slug}?kind=residential`. Proposed: `/v1/rankings?range=&kind=residential` (NETS-4). |
| 26 | Podium styling (gold border/glow, "Top" crown badge) for rank 1-3; else "Rank" label | `n.standing.rank <= 3` | colour tiers #FFD36A / #EDB64F / #D69A3C | — | `/v1/rankings` → `networks[].rank` | exists-but-different-shape | Same "#1 of 1" difference as row 15 (NETS-3). |
| 27 | Rank numeral "01"… | `String(n.standing.rank ?? 0).padStart(2,"0")` | 2-digit; a null rank shows "00" | independent of the sort | `/v1/rankings` → `networks[].rank` | exists-but-different-shape | NETS-3. A null rank (unscored, or alone in its kind) renders "00" and the non-podium "Rank" caption; the website should print "N/A" (NETS-13). |
| 28 | Monogram chip | `n.monogram` | 2 letters | — | client from `name` | exists | |
| 29 | Name | `n.name` | text | — | `/v1/rankings` → `networks[].name` | exists | |
| 30 | Tagline (one line, truncated) | `n.tagline` = old `p.tagline` | text | — | `/v1/networks/{slug}` → `profile.tagline` only | exists-but-different-shape | Not in `/v1/rankings`: 18 network requests (each up to 200 KB) for one line per card. Proposed: `tagline` on rankings rows (NETS-2). For measured networks without a catalog tagline the API generates one of three by schedule: "<Kind> proxies · measured on a census, pulse and load schedule" (every-request), "… measured on a census and pulse schedule", else "… measured HTTP pool sample" (`api/internal/build/measured.go`); networks on the same schedule get the same line. A network with no measurement and no catalog row has a null tagline (blank line). |
| 31 | Index Score (big figure) | `fixed(n.score.composite)` | 0-100, 1 dp, "N/A" | not range-dependent | `/v1/rankings?range=30d` → `networks[].score` | exists | See row 19 for the per-range score. |
| 32 | Grade | `n.score.grade` | "A+"…"D"; printed raw, so a null grade renders blank (no "N/A" today) | — | `/v1/rankings?range=30d` → `networks[].grade` | exists | Null in the API (unscored network) renders blank on the card and in the preview (row 68); the website must print "N/A" (NETS-13). |
| 33 | Score meter bar | `score / 100` | bar, 0-100%, min 2%; empty (0 width) when the score is null | animates on view | `/v1/rankings` → `networks[].score` | exists | |
| 34 | Figure "Success" | `pct(w.successRate)` = old `windowStats(p,r).successRate` | %, 1 dp | per range | `/v1/rankings?range=` → `networks[].successRate` | exists | `metrics.success_rate` of the headline group's window. |
| 35 | Success tone (green/amber/red, tooltip Good/Fair/Weak/Not rated) | `rate(v, SCALES.success)`: ≥0.90 pos, ≥0.75 warn, else neg | colour | — | — | editorial | The thresholds are hard-coded in pi-network-cards.tsx `SCALES`. This is a design rule, not data. |
| 36 | (proposed) Browser overall score next to Success | not shown today | 1-10 + `rank`/`rankedOf`, `status` | per engine (chrome/camoufox), 30d only | `/v1/networks/{slug}` → `profile.browserOverall.<engine>.success.{score,rank,rankedOf,status}` | exists-but-different-shape | Served only per network (18 requests) and only for the 30d window (measured.go `browserOverall(p.Window("30d"))`). Proposed: `browserOverall` on rankings rows (NETS-10). Keep the percentage. |
| 37 | Figure "Latency p50" | `ms(w.latencyP50)` | "918 ms" / "1.23 s" | per range | `/v1/rankings?range=` → `networks[].latencyP50` | exists | Generic HTTP target, under load in every-request snapshots. |
| 38 | Latency tone | `SCALES.latency`: ≤500 pos, ≤1000 warn | colour | — | — | editorial | Hard-coded thresholds. |
| 39 | Figure "$ / GB" | `money(w.listRate, 2)` = old `listRate(p.pricing)` (`pricePerGbAtScale` else `pricePerGb`) | $, 2 dp, "N/A" | the same in every range | `/v1/networks/{slug}` → `profile.pricing.pricePerGbAtScale` / `pricePerGb` (client picks) | exists-but-different-shape | Not in `/v1/rankings` (it has `costPer1kSuccess`, not the list rate). Proposed: `listRate` on rankings rows (NETS-1). |
| 40 | Price tone | `SCALES.price`: ≤2 pos, ≤5 warn | colour | — | — | editorial | Hard-coded. |
| 41 | Figure "Unique IPs" | `compact(w.uniqueIps)` | "134k", "1.20M" | per range | `/v1/rankings?range=` → `networks[].uniqueIps` | exists | The headline group's distinct IPs. |
| 42 | Unique IPs tone | `SCALES.pool` (log): ≥1e6 pos, ≥1e5 warn | colour | — | — | editorial | Hard-coded. |
| 43 | Figure "Countries" | `w.countries` = old `p.pool.countries` | integer, "N/A" | the same in every range | `/v1/rankings?range=` → `networks[].countries` (= `pool.countries`) | exists | The per-range exit countries exist as `/v1/networks/{slug}` → `ranges[r].exitCountries`, if the website ever wants them per range. |
| 44 | Countries tone | `SCALES.countries`: ≥50 pos, ≥15 warn | colour | — | — | editorial | Hard-coded. |
| 45 | Figure "DC ASNs" | `pct(n.dcAsnShare)` = old `p.trust.datacenterAsnShare` | %, 1 dp | not range-dependent | `/v1/rankings?range=` → `networks[].datacenterAsnShare` | exists | |
| 46 | DC ASN tone | `SCALES.dc`: ≤3% pos, ≤6% warn | colour | — | — | editorial | Hard-coded. |
| 47 | "View network" + sweep/swap animation on range change | literal | — | `key={range}` re-plays the animation | — | editorial | |

### 6. CommandPalette, page="networks" (pi-palette.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 48 | Network items: monogram, title | `MEASURED[].monogram`, `.name` | text | client-side token scoring (title 120/90, keywords 55, subtitle 45, loose 18, group bonus); at most 5 per group | `/v1/rankings` → `name` (client search), or `/v1/search?q=` → `sections[Networks].items[].title` | exists-but-different-shape | The API search ports the old site's palette: `href` is `/networks/{slug}` (old path), the subtitle is "Kind · Product · HQ", and its meta is the `KindStanding` label. The website's scoring and subtitle differ, so it is simplest to keep the website's client search over the rankings rows (NETS-9). |
| 49 | Network item subtitle "{tagline} · Measured/Demo" | `n.tagline`, `n.source` | text | — | `tagline`: NETS-2; `source`: `/v1/rankings` → `networks[].source` | exists-but-different-shape | `source` exists; `tagline` is missing from the list answer (NETS-2). |
| 50 | Network item meta "{score} · #{rank}" / "Unranked" | `fixed(n.score.composite)`, `standing.rank` | text | — | `/v1/rankings?range=30d` → `score`, `rank` | exists-but-different-shape | NETS-3 ("#1 of 1"). The API's `/v1/search` meta = `KindStanding() + " · " + score` already has the fallback. |
| 51 | Network items offered only if the network has a review page | `remote.reviews` from `GET /proxy-index/search.json?locale=` (website route: `MEASURED.filter(reviewFor)`, reading networks.json) | slug list | fetched once per visit and locale, on open or after 2.5 s idle; `Cache-Control: max-age=300` | — | editorial | The review pages are website content. The API has no "has review page" flag, and none is needed. |
| 52 | Guides and Targets groups (title, subtitle, href) | `search.json` → `entries[]` (guides Markdown; `TARGETS` from targets-data) | text | same fetch | `/v1/search-index` → Targets items (no guides) | exists-but-different-shape | Targets exist in the API search index, labelled from the site catalog. The website uses its own target copy. Guides are website content (editorial). |
| 53 | "Jump to" sections (All networks, Residential proxies, Network profiles, How to choose) and Pages group | literals | text | listed only if the anchor is on the page | — | editorial | |
| 54 | "Top networks" (empty query) | `MEASURED` filtered by reviews, first 5 | list | MEASURED order (score desc; mutated by the directory sort, see row 13) | `/v1/rankings?range=30d` → first 5 residential rows | exists | |
| 55 | "Recent" (empty query) | localStorage `pi-palette-recent` (4 ids) | list | — | — | editorial | Client state. |
| 56 | Action "Download the dataset" | `/proxy-index/dataset.json` (website route, returns home.json) | file | click | `/v1/dataset` → 302 to `files/dataset-<sha>.json` | exists-but-different-shape | The API file is the full Dataset (about 9 MB) in another shape than home.json. Decide whether the link points at `/v1/dataset` (NETS-12). |
| 57 | Actions "Copy link", "Show the last 24 hours / 7 days / 30 days" | literals; `onRange(r)` scrolls to `#networks` | — | sets the directory range | `/v1/rankings?range=` | exists | |
| 58 | Footer "{networks} networks · {targets} targets" | `MEASURED.length`, `DATA.targets` | text | — | count of rankings rows; targets → NETS-5 | exists-but-different-shape | Same as rows 7 and 8. |
| 59 | Empty result "Nothing matches “{query}”" | `visible.length === 0` | text | — | — | editorial | |

### 7. "What makes a good residential proxy network" (NetworksContent.astro, server-rendered)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 60 | Eyebrow, h2, intro, 4 check titles and bodies, "Measured as" labels, links to /targets and /true-cost | literals | text | — | — | editorial | |
| 61 | "Good" thresholds: Hosting ASN share "Under 3%", Unique IPs "Over 1M", Success "Over 90%", Price "Under $2 / GB" | hard-coded in NetworksContent.astro | text | — | — | editorial | They duplicate the `SCALES` cuts in pi-network-cards.tsx (dc 0.03, pool 1e6, success 0.9, price 2). Keep them in one place on the website. |
| 62 | "Score weight" for each check (dcAsns, coverage, reliability, value) | `DATA.profiles[0].weights[key] / Σ weights` → "10%", "12%", "22%", "14%" | %, integer | — | `/v1/dataset` → `profiles[0].weights` (site.ranking_profiles), or `weights` (the snapshot methodology weights) | exists-but-different-shape | Only in the whole dataset file. `/v1/rankings` names `profile: "balanced"` but has no weights. Proposed: `weights` (balanced) on `/v1/rankings` (NETS-7). The website assumes `profiles[0]` is balanced, and should pick by id. |
| 63 | Decorative figures: bar heights 62/78/70/88/94/100, "PASS", fake prices $1.80 / $2.60 / $4.10 | hard-coded in NetworksContent.astro | SVG/text | animation only | — | editorial | Illustrations that look like data. They should stay static and are not to be served by the API. |

### 8. "A full proxy review for every network" + ProfilePreview (NetworksContent.astro, pi-profile-preview.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 64 | Eyebrow, h2, paragraph with a link to /compare, 6 bullet points | literals | text | — | — | editorial | |
| 65 | Cycle through the top 3 networks (progress bars) | `NETWORKS.slice(0,3)` at module load | 3 tabs | auto-advance every 4.2 s while visible (`useLiveWhileVisible`, 1.2 s start delay); click selects | `/v1/rankings?range=30d` → first 3 residential rows | exists | No network fetch per cycle; all 3 come from one answer. |
| 66 | Monogram, name | `n.monogram`, `n.name` | text | — | `/v1/rankings` → `name` | exists | |
| 67 | "Rank #r · Network profile" | `n.standing.rank` ("N/A" if null) | `#n` | — | `/v1/rankings?range=30d` → `rank` | exists-but-different-shape | NETS-3. |
| 68 | Index Score and grade | `fixed(n.score.composite)`, `n.score.grade` | 1 dp; grade | — | `/v1/rankings?range=30d` → `score`, `grade` | exists | |
| 69 | "Success rate · 30 days" value | `pct(windowOf(n.windows,"30d").successRate)` | %, 1 dp | fixed 30d | `/v1/rankings?range=30d` → `successRate` | exists | |
| 70 | (proposed) Browser overall score beside the success rate | not shown today | 1-10, rank of M | 30d, per engine | `/v1/networks/{slug}` → `profile.browserOverall.<engine>.success` | exists-but-different-shape | NETS-10. |
| 71 | Success-rate sparkline (area) + hover tooltip "{pct} {label}" | `w.curve` (30d) = old `sparkSeries(measuredHistory(p, 30d), length)`: for a measured network the 30d daily points merged with the finer 7d and 24h points (mixed spacing, drawn at equal x steps; the mock's Shifter curve has 6 points labelled "30 min ago" … "5 min ago"); 30 daily points with labels "29 days ago"… only for demo networks | y: success 0-1 (min-max scaled); x: day index; tooltip `pct(v)` + `labels[i]` | hover | `/v1/rankings?range=30d` → `trend` (at most 26 values, no times/labels) or `/v1/networks/{slug}/chart?range=30d` → `success[]` (`{t, v}`, daily) | exists-but-different-shape | Neither API source is that merged series: `trend` is the 30d window's own series thinned to 26 values without times, and `/chart?range=30d → success[]` is the 30d window's series only (row 37 describes the same merge for the home curve). NETS-8: either `trend` follows the merged history (with `trendT`), or the page accepts the 30d window series (website change). |
| 72 | "Latency p50" value + bar (share of the field's best: lowest ÷ yours) | `ms(w.latencyP50)`; `share()` over every network's 30d latency | ms; bar 4-100% | — | `/v1/rankings?range=30d` → `latencyP50` of all rows | exists | The client computes the share. |
| 73 | "Unique IPs" value + bar (yours ÷ max) | `compact(w.uniqueIps)` | compact; bar | — | `/v1/rankings?range=30d` → `uniqueIps` | exists | |
| 74 | "Cost per 1,000 successes" value + bar (min ÷ yours) | `money(w.costPer1kSuccess)` = old `windowStats(p,30d).costPer1kSuccess` | $, 2-4 dp | — | `/v1/rankings?range=30d` → `costPer1kSuccess` | exists | |
| 74a | Empty and N/A states of the preview | `if (!n) return null`; grade printed raw (`{n.score.grade}`) | nothing rendered; grade blank when null | — | `/v1/rankings?range=30d` → no residential row; `grade` null | exists | With no residential network the whole preview card disappears (no placeholder). A null grade renders blank, not "N/A" (NETS-13). The whole card is `aria-hidden`, so none of its figures reach screen readers. |

### 9. "How to choose", "Missing a proxy network?", FAQ, CTA, footer

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 75 | 4 numbered steps (titles, bodies, link to /overlap), "Suggest a network" (mailto / live chat handoff), FAQ (5 items), CTA, footer | literals; `networksFaq` | text | the chat opens on click if available | — | editorial | "Cost per 1,000 successful requests" is mentioned in prose only. |

---

### Further details (website rules, no API gap)

- "Requests (30 days)" and "Success rate · 30 days" are hard-coded labels while `observedDays` is 0 (HOME-5).
- The ProfilePreview `TOP` list is taken at hydration, after the directory's in-place sort.
- The palette's review filter applies only after `search.json` loads (section 2 row 11.2).

### Gaps summary

1. NETS-1 listRate: card "$ / GB" figure and the Price sort (rows 19, 39). Proposed `/v1/rankings?range=` → `networks[].listRate` (`pricePerGbAtScale`, else `pricePerGb`). Source: snapshot `products[].pricing.lowest_per_gb` / `entry_per_gb`, else `site.pricing.price_per_gb_at_scale` / `price_per_gb`.
2. NETS-2 tagline: card subline and palette subtitle (rows 30, 49). Proposed `/v1/rankings` → `networks[].tagline`. Source: `Provider.tagline` (catalog `site.providers`, else generated).
3. NETS-3 standing: rank numeral, podium, strip badge, palette meta, preview rank (rows 15, 26, 27, 50, 67). Rankings `rank` is null for a scored network alone in its kind, where the old `kindStanding` shows #1 of 1. Proposed `/v1/rankings` → `networks[].standing {rank, peers}` with the `ScoreView.KindStanding` fallback. Source: snapshot `scores.balanced` rank and `components.*.inputs.peers`.
4. NETS-4 kind filter: the directory shows residential networks only, and rankings rows carry the headline kind's figures (row 25). Proposed `/v1/rankings?range=&kind=residential` (rows and rank of that kind's product group). Source: snapshot product groups per kind (as `/v1/networks/{slug}?kind=`).
5. NETS-5 target count: hero "Targets" and palette footer (rows 8, 58). Proposed `/v1/rankings` → `targets` (count). Source: `Dataset.targets` (site.targets + public.targets).
6. NETS-6 fleet requests: hero "Requests (30 days)" (row 9). Proposed `/v1/rankings?range=` → `fleet.requests` (or a small `/v1/summary`). Source: `measuredMarket` fleet window requests (snapshot fleet / summed product groups of listed networks).
7. NETS-7 score weights: "Score weight" percentages (row 62). Proposed `/v1/rankings` → `weights` of profile `balanced`. Source: `site.ranking_profiles` / snapshot `methodology.settings.weights`.
8. NETS-8 trend times: the ProfilePreview sparkline needs per-point day labels (row 71). Proposed `/v1/rankings` → `networks[].trendT` (minutes before the build, parallel to `trend`). Source: `WindowStats.success[].t`. Decide whether `trend` follows the old merged `measuredHistory` (30d days + 7d and 24h finer points) or the page accepts the 30d window's own series (row 71).
9. NETS-9 search shape: palette network items (row 48). `/v1/search` uses old-site hrefs `/networks/{slug}` and a different subtitle. Proposed: the website searches the rankings rows client-side, or `/v1/search-index` adopts `/proxy-index/networks/{slug}` and a tagline subtitle. Source: build/search.go.
10. NETS-10 browser overall score in list rows: next to Success on the card and in the preview (rows 36, 70). Proposed `/v1/rankings` → `networks[].browserOverall.<engine>.success {score, rank, rankedOf, status}`. Source: snapshot `windows[r].browser_overall.<engine>`, which the calculator already writes for every window with browser targets; the API maps only the 30d window today (`measuredBlock` → `browserOverall(p.Window("30d"))`), so each `range` answer can carry its own window's.
11. NETS-11 palette targets: the palette's Targets group uses website copy, while the API search index has the catalog labels. Proposed: none needed if the website keeps its own target copy. Source: `site.targets`.
12. NETS-12 dataset download: the palette action serves home.json (row 56). Proposed: point the action at `/v1/dataset` (full Dataset, about 9 MB) or keep a website export. Source: `/v1/dataset`.
13. NETS-13 unranked and ungraded states: a null rank renders "00" on the card (row 27) and a null grade renders blank on the card and preview (rows 32, 68, 74a); the API sends null `rank`/`grade` for networks without a ready score of that range. Website change: print "N/A". Source: `/v1/rankings` → `rank`, `grade` (exist).
14. NETS-14 FAQ promises cost per success on the cards: FAQ answers 3 and 4 describe a card figure ("a fair cost per successful request", "each network shows its cost per 1,000 successful requests") the cards do not have; only the profile preview (row 74) shows it, for the top three. Website change: add a seventh card figure from `/v1/rankings?range=` → `costPer1kSuccess` (with `valueRank`), or reword the FAQ (it feeds the FAQPage JSON-LD). Source: `/v1/rankings` → `costPer1kSuccess` (exists).
15. NETS-15 score, grade and rank per range: the cards keep the 30d score, grade and rank (and the "Index Score" sort) while Success, Latency and Unique IPs follow the dock; `/v1/rankings?range=24h|7d` carries that range's score, grade and rank (rows 19, 27, 31, 32). Decide which the page shows. Source: `/v1/rankings` (`rankingRow` reads `Window(rangeID).Scores["balanced"]`).
16. NETS-16 Countries and DC ASNs look per range: both sit among range-following figures and re-animate on a range switch, but are 30d in every range today and in `/v1/rankings` (`countries` = `p.Pool.Countries`, `datacenterAsnShare` = `p.Trust.DatacenterAsnShare`) (rows 43, 45). A per-range count exists only as `/v1/networks/{slug}` → `ranges[r].exitCountries` (one network per request). Proposed: the W1 per-range `countries` and `datacenterAsnShare` on RK rows. Source: snapshot `windows[r].pool`, `windows[r].metrics`.

### Performance notes

- Today the page makes 0 API calls. With the API, one view needs:
  - `/v1/rankings?range=30d`, at SSR for JSON-LD, the hero, the strip, the cards and the preview.
  - `/v1/rankings?range=24h` and `?range=7d`, on the toggle (or prefetched).
  - The website's own `/proxy-index/search.json` for the palette.
- Without NETS-1/NETS-2/NETS-10, every card would also need `/v1/networks/{slug}` (18 requests, each up to 200 KB, together about 1-3 MB) just for `tagline`, the list rate and `browserOverall`. Those fields must move into the rankings rows.
- Without NETS-5/NETS-6/NETS-7 the hero and the weight percentages need `/v1/networks/all` (a large answer for one number) and the whole dataset file (about 9.2 MB, gzip 0.5 MB). Both are unacceptable per view; add small fields to `/v1/rankings`.
- Rankings size at production volume: 18 rows x (about 30 scalars + 8-key breakdown + 26-point trend), about 0.7-1 KB per row, about 15-20 KB per range and about 50-60 KB for all 3 (gzip about 10 KB). Adding the tagline, list rate, standing, trendT (26 numbers) and browserOverall adds about 0.5 KB per row. That is still far under the 200 KB cap.
- All three rankings answers are already precomputed per build (`Pages.Rankings`, compressed once) and served with an ETag/304. The added fields must be computed in the same `rankingRow` pass, never per request.
- No polling on this page. If the dock's "Live" badge is made real, poll `/v1/version` (5 s cache) and refetch the rankings only when the `revision` changes.
- Client work is trivial: sorting and filtering at most 18 rows, and computing the min/max shares for the preview bars.

## 5. /proxy-index/networks/{slug} — one network's review page: measured performance per range, coverage, targets, ownership, pricing, editorial review, benchmark ranks, alternatives, weekly analysis, customer reviews and independent runs

Source files read (website root): `src/pages/proxy-index/networks/[slug].astro`; `src/components/proxy-index/review/review-data.ts`, `review-hero.tsx`, `review-score-panel.tsx`, `review-exit-strip.tsx`, `review-mark.tsx`, `review-tabs.tsx`, `review-measurements.tsx`, `review-chart.tsx`, `review-editorial.ts`, `review-voices.tsx`, `review-write.ts`, `ReviewOverview.astro`, `ReviewArticle.astro`, `ReviewBenchmark.astro`, `ReviewAlternatives.astro`, `ReviewAnalysis.astro`, `ReviewRuns.astro`, `ReviewRunChecks.astro`; `src/components/proxy-index/pi-data.ts`, `pi-network-cards.tsx` (NETWORKS, SCALES, rate), `pi-palette.tsx` (CommandPalette, search.json fetch), `proxy-index-home.tsx` (TimeDock, useDockShown, usePaletteShortcut), `pi-charts.tsx` (Sparkline, Meter, CountUp; exports only); `src/pages/og/proxy-index/[key].astro` (network-{slug} card); `src/pages/proxy-index/search.json.ts`; `src/data/proxy-index/networks.json` (4 networks: 3 demo + shifter; every key inspected), `home.json` (`networks[]`, `scoreLabels`), `targets.json` (`targets[]`), `editorial.json` (empty `networks: {}`); `scripts/proxy-index/export-networks.mts`, `scripts/proxy-index/export-home.mts` (network/window provenance).
API files read (repo): `api/README.md` (Endpoints), `api/FIELDS.md` (Provider, Measurement blocks, Window statistics, Browser targets, Browser scores, Records, Per-page answers, Payload v4 list answers, dataset file of a v4 build), `api/internal/build/types.go`, `pages.go`, `profile.go`, `window.go`, `measured.go`, `rankings.go`, `tape.go`, `search.go`, `coverage.go`; `api/internal/model/model.go`, `model_provider.go`, `model_snapshot.go`, `target.go`; `api/internal/server/server.go` (routes).

Conventions used below. `NJ` = `src/data/proxy-index/networks.json → networks[slug]`; `W[r]` = `NJ.windows[r]` (r = 24h, 7d, 30d); `HN` = `home.json → networks[]` entry (the `Network` type, via `NETWORKS = MEASURED`, residential only, re-ranked by composite); `HW` = `windowOf(HN.windows, "30d")`. API: `NET` = `/v1/networks/{slug}?kind=residential`; `P` = `NET → profile`; `S[r]` = `NET → stats[r]`; `R[r]` = `NET → ranges[r]`; `CH[r]` = `/v1/networks/{slug}/chart?kind=residential&range=r`; `COV[r]` = `/v1/networks/{slug}/coverage?kind=residential&range=r&q=&page=`; `RK[r]` = `/v1/rankings?range=r → networks[]`. "Export:" names the old-site (milf-website) function the mock value came from. `P` measured blocks (pool, performance, pricing, trust, targets, overlap, score) are the 30d window only (`build/measured.go measuredBlock` reads `p.Window("30d")`); only `stats`, `ranges`, `tileRanks`, chart, coverage and places are per range.

### 0. Document head: title, meta, canonical, OG image, breadcrumbs, JSON-LD (`[slug].astro`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1 | 404 when the slug is unknown | `reviewFor(slug)`: slug must be in `HN` (residential, MEASURED) and in `NJ` | — | — | `NET` answers 404 for unknown network or category | exists | Website also hides non-residential networks; API serves every kind (`?kind=`). |
| 2 | `<title>` "{name} Review: Residential Proxy Test Results \| Shifter" (≤60 chars variant) | `HN.name` | text | length fallback chain | `P.name` (or `NET → network.name`) | exists | Copy is editorial; only the name is data. |
| 3 | meta description, keywords | `HN.name` | text | 140–160 char pick | `P.name` | exists | Deliberately no figures. |
| 4 | canonical, breadcrumbs (Proxy Index › Networks › {name}) | `HN.slug`, `HN.name` | text | — | `P.slug`, `P.name` | exists | |
| 5 | OG image `/og/proxy-index/network-{slug}.png` (name, "review", monogram, Shifter mark) | `og/[key].astro`: `n.name`, `n.monogram` (= `name.slice(0,2).toUpperCase()`, export-home), `slug==='shifter'` | image | — | `P.name`, `P.slug`; monogram derived client-side | exists | Tagline/pills hard-coded copy (editorial). |
| 6 | JSON-LD `Review.reviewBody` | `NJ.verdict` (export: `p.verdict`) | text | — | `P.verdict` | exists | API generates verdict from snapshot when no catalog value (FIELDS "Provider"). |
| 7 | JSON-LD `reviewRating.ratingValue` (0–100) | `HN.score.composite` | number 0–100, 1 dp | omitted when null | `P.score.composite` | exists | 30d balanced score. |
| 8 | JSON-LD `itemReviewed.url`, `name` (article title or page title) | `NJ.website`; `reviewArticleFor(r)?.title` | URL / text | — | `P.website`; article title: none | exists (url) / editorial (title) | Article is editorial content (see section 6). |
| 9 | `noindex` | constant `true` | — | — | — | editorial | Launch flag, not data. |
| 9a | JSON-LD constants: `@type` Review, `@id` `{pageUrl}#review`, `url`, `itemReviewed.@type` Organization, `reviewRating.bestRating` 100 / `worstRating` 0, `author` and `publisher` (`ref(ORG_ID)`) | literals in `[slug].astro` `jsonLd`; `absUrl(path, locale)` | text / URL | locale | — | editorial | Only the slug inside `url` and `@id` is data (`P.slug`). `reviewRating` is left out when the composite is null (#7). |

### 1. Hero (`review-hero.tsx`, `review-score-panel.tsx`, `review-mark.tsx`, `review-exit-strip.tsx`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 10 | Breadcrumb trail, H1 name (first word plain, rest gradient) | `HN.name` | text | — | `P.name` | exists | |
| 11 | Monogram / Shifter logo tile | `HN.monogram`, `HN.slug` | 2 letters | — | derived from `P.name` | exists | No logo field in API (none on site either). |
| 12 | Tagline | `NJ.tagline` (export: `p.tagline`) | text | — | `P.tagline` | exists | Generated "… measured on a census, pulse and load schedule" when no catalog tagline. |
| 13 | Chip "Residential" | hard-coded | — | — | `P.primaryKind` / `NET.kind` | editorial | Should follow `?kind=`. |
| 14 | Chip HQ | `NJ.hq` | text | hidden when null | `P.hq` | exists | |
| 15 | Chip "Fictional demo provider" / "Real measurements" | `HN.source` (`demo`/`measured`) | enum | tone warn/pos | `P.source` | exists | API serves only `measured` now. |
| 16 | Chip KYC ("KYC policy not reviewed" for measured; published/not found for demo) | `NJ.kycVerified` (export: `p.trust.kycVerified`) | bool | measured networks always show "not reviewed" | `P.trust.kycVerified` (catalog `site.trust_signals`) | exists | Null for measured unless catalog fills it. |
| 17 | Chip peer consent | `NJ.consentDisclosed` | bool | as above | `P.trust.consentDisclosed` | exists | |
| 18 | "Try {short}" button href | `NJ.productUrl ?? NJ.website`; Shifter falls back to hard-coded `https://shifter.io/order` (`review-data.ts`) | URL | target _blank | `P.products[]` with `measured: true` → `.url`, else `P.products[0].url`, else `P.website` (the export's order) | exists | Shifter fallback is hard-coded in review-data.ts; should be a catalog `site.provider_products.url`. The button is hidden when all three are null. |
| 18a | Short name `{short}` used in "Try {short}", the closing CTA "Measure {short} against Shifter", "What {short} customers actually say", "View all {short} alternatives", benchmark and runs headings | `shortName(HN.name)` = first word of the name (`review-data.ts`) | text | — | derived from `P.name` | exists | Client derivation; a two-word brand keeps only its first word ("Bright Data" → "Bright"). No API field (not needed). |
| 19 | "View benchmark results" → `#performance` | — | — | anchor | — | editorial | |
| 20 | Score panel: Rank "#N" (podium colour for 1–3) and "of {peers}" | `HN.standing.rank`, `HN.standing.peers` (re-ranked by composite among residential in `pi-data.ts MEASURED`) | integer | podium colours #FFD36A/#EDB64F/#D69A3C for ranks 1–3 | `RK["30d"] → rank`, `peers` (rank within kind); `P.score.rank` has the rank but no `peers` (`ScoreView.Peers` is `json:"-"`) | exists-but-different-shape | Peers only via `/v1/rankings`, a second request. Add `peers` to `P.score`. Website rank = position by composite; API rank = framework rank in kind (ties share). A missing rank prints "N/A" and hides "of {peers}". |
| 21 | Score panel: Index Score (count-up, 1 dp) | `HN.score.composite` | 0–100 | N/A when null | `P.score.composite` | exists | |
| 22 | Score panel: grade badge, tone (A green, B blue, C amber, else red) | `HN.score.grade`; `gradeTone()` | A+…D | tone thresholds in `pi-data.ts` | `P.score.grade` | exists | `grade` is null in the API for an unscored network (`P.score.grade`, RK `grade`); the page prints it raw: blank badge in the score panel (row 22) and on alternative cards (row 148), "Grade null" in the overview (row 36). Website must print "N/A" (NET-NA-GRADE). |
| 23 | Score panel: 8 breakdown rows, label + 20 LED segments + value 0 dp | `HN.breakdown.{reliability,speed,uptime,coverage,diversity,dcAsns,authenticity,value}`; labels `home.json scoreLabels[key]` | 0–100 | segments lit = round(v/100×20); colours by segment index | `P.score.breakdown.*` (all 8 keys) | exists | Labels are UI constants (editorial); API has no `scoreLabels`. A null component (for example `value` without a price) lights 0 segments and prints "N/A"; the overview summary (#38) skips null components. |
| 24 | Floating badge "{requests} Requests (30 days)" | `HW.requests` (export-home: `windowStats(p,'30d').requests`) | integer, en-US grouping | — | `S["30d"].requests` (or `R["30d"].requests`) | exists | |
| 25 | Headline figure: Success | `HW.successRate` | % 1 dp | 30d fixed | `S["30d"].successRate` | exists | Time-weighted mean of slot rates (FIELDS). |
| 26 | Headline figure: Latency | `HW.latencyP50` | ms / s | 30d | `S["30d"].latencyP50` (under load) | exists | |
| 27 | Headline figure: Unique IPs | `HW.uniqueIps` | compact (k/M) | 30d | `S["30d"].uniqueIps` | exists | Hour-formula estimate where released (API-ESTIMATES-1). |
| 28 | Headline figure: $ / GB | `HW.listRate` (export-home `listRate(p.pricing)`) | $ 2 dp | 30d | `P.pricing.pricePerGbAtScale` ?? `P.pricing.pricePerGb` (Go `listRate()`) | exists-but-different-shape | No `listRate` field; client must apply the at-scale-else-entry rule. Add `P.pricing.listRate`. |
| 29 | Exit strip sentence "Exits observed for {name} across {n} countries" | `W["30d"].coverage.countries` (export: `pool.countries`) | integer | 30d | `R["30d"].exitCountries` (or `R["30d"].lists.country`, `P.pool.countries`) | exists | |
| 30 | Exit strip marquee: per country flag, name, IP-weighted latency, top 24 by IPs | derived from `W["30d"].geo[]` (only the 60 exported City×ASN rows): sum `ips`, IP-weighted mean `latency` per `countryCode` | ms | sorted by IPs desc, top 24; hidden if < 3 countries | `R["30d"].countries[]` (top 50: `countryCode`, `country`, `ipsSum`, `requests`, `successRate`, `latencyP50`; MEASURED-FIELDS-1: requests and latency of the tests that requested the country and of the pulses that exited there, else null with `not_measured_per_place`) | exists | Use the API country row directly; the website's derivation from 60 City×ASN rows is wrong at production volume (thousands of rows). Value becomes country p50, not an IP-weighted row mean. |

### 2. Section tabs strip (`review-tabs.tsx`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 31 | Tabs Overview, Performance, Pricing, Review (only with an article), Benchmark, Alternatives, Analysis (grouped menu), Reviews | `article` presence: `reviewArticleFor(r)` (`editorial.json` or `NJ.review`) | — | sticky after Overview; scroll-spy line 260 px; group menu on hover/click | — | editorial | Review tab depends on editorial content (section 6). |
| 32 | Reviews tab count | `NJ.userReviews.items.length` | integer | — | `NET → records.reviews` (full count) or `P.reviews.count`; `NET.reviews[]` is capped at 20 | exists-but-different-shape | `records.reviews` and `P.reviews.count` count every product's non-pending reviews of the network; the page counts residential reviews only (export filter `product` starts with `residential`). Use `records.reviews` once the record lists follow `?kind=` (NET-RECORDS-KIND). The count badge is hidden at 0. |
| 33 | "Try {short}" in strip | as #18 | URL | — | as #18 | exists | |

### 3. Overview (`ReviewOverview.astro`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 34 | H2 "{name} review, at a glance" | `NJ.name` | text | — | `P.name` | exists | |
| 35 | Verdict rank "#N" (podium colours) and "of {peers} residential networks" / "Not ranked yet" | `HN.standing.rank`, `.peers` | integer | — | `RK["30d"] → rank`, `peers` | exists-but-different-shape | Same as #20. |
| 36 | "Grade {grade}" badge, tone | `HN.score.grade` | text | `gradeTone` | `P.score.grade` | exists | `grade` is null in the API for an unscored network (`P.score.grade`, RK `grade`); the page prints it raw: blank badge in the score panel (row 22) and on alternative cards (row 148), "Grade null" in the overview (row 36). Website must print "N/A" (NET-NA-GRADE). |
| 37 | Verdict label (Recommended / Best for specific uses / Not recommended) | `NJ.review.verdict` (milf `src/content/reviews/<slug>.md` frontmatter) | enum | shown only with an article | none | editorial | Editorial review content; no API source (see Gaps). |
| 38 | Summary sentence "Strongest on {first} and {second}" / "Top marks on every part" | derived: `HN.breakdown` sorted desc, labels `scoreLabels` | text | ties handled | `P.score.breakdown.*` | exists | Derivation stays client-side (copy rule). |
| 39 | "Best used for" tiles (top 3 breakdown keys): figure per key — success %, latency p50, availability %, unique IPs, countries, hosting-IP share, authenticity %, $/GB | `HW.successRate`, `HW.latencyP50`, `W["30d"].uptimePct` (2 dp + %), `HW.uniqueIps`, `HW.countries`, `HN.dcAsnShare`, `HN.authenticity` (export-home `p.trust.residentialAuthenticity`), `HW.listRate` | mixed | sorted by breakdown score, ties by "lead" | `S["30d"].successRate`, `.latencyP50`, `.uptimePct`, `.uniqueIps`; `P.pool.countries`; `P.trust.datacenterAsnShare`; `P.trust.residentialAuthenticity`; list rate as #28 | exists | All on `NET`. |
| 40 | "Best in Index" badge per tile (network holds the field max/min) | `leadsOn()` over all `NETWORKS` 30d: successRate, latencyP50, uniqueIps, countries, dcAsnShare, authenticity, listRate | bool | needs all networks | `RK["30d"]` has `successRate`, `latencyP50`, `uniqueIps`, `countries`, `datacenterAsnShare`; no `authenticity`, no list rate | exists-but-different-shape | All-networks figure. Authenticity and list rate need 18 `NET` requests today. Add `authenticity` and `listRate` to rankings rows, or a precomputed `leads` set per network. The availability tile never gets the badge (`lead: false` hard-coded), so W2's `uptimePct` on RK serves this page only if the website adds that lead. |
| 41 | "How we tested {name}" fact: product label ("Rotating residential") | `NJ.protocol.label` (export: `productLabel(p.measuredProduct)`) | text | — | `P.measuredProduct` (`residential_rotating`) → label client-side | exists-but-different-shape | API gives the id, not the label; label map is UI copy. |
| 42 | Fact "{n} test targets" | `NJ.protocol.targets` (export: `measuredProtocol(p).targetIds.length` or `dataset.targets.length`) | integer | — | count of `P.targets[]` with `requests > 0`; scope list only in `/v1/dataset → scopes[].targets` | exists-but-different-shape | `P.targets` lists every dataset target, measured or not (requests 0). Add `P.protocol.targets` (count) or a scopes answer. |
| 43 | Fact "{n} countries" | `W["30d"].coverage.countries` | integer | — | `R["30d"].exitCountries` / `P.pool.countries` | exists | |
| 44 | Link "Full methodology" | — | — | — | — | editorial | |

### 4. Performance (`review-measurements.tsx`; every figure follows the dock range)

##### 4a. Time dock and command palette

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 45 | Floating dock: "Live" dot, range toggle 24h / 7d / 30d (default 30d) | `RANGES`, `DEFAULT_RANGE` (`pi-data.ts`); switches `r.windows[range]` | enum | shown from Performance until footer | `S[r]`, `CH[r]`, `COV[r]`, `RK[r]`, `R[r]`, `P.tileRanks[r]` | exists | Range ids match API ranges. "Live" is decorative; page never polls. `/v1/version` exists for polling if wanted. |
| 46 | Command palette (⌘K, "/"): Networks group (name, tagline · Demo/Measured, meta "score · #rank"), Sections group (page="review" anchors) | `MEASURED` (`HN`); runtime `fetch('/proxy-index/search.json?locale=')` at open or after 2.5 s idle (website route `search.json.ts`, `reviews` = slugs with a review page) | text | client fuzzy search | `/v1/search-index` (Networks, Pages, Targets, Countries, Actions groups) or `/v1/search?q=` | exists-but-different-shape | API has no `reviews` slug list (every API network has a page) and no Guides; sections are UI constants. The Sections list always includes "Review", even when the page has no article and its tab is hidden (section 2 row 11.7). |

##### 4b. Metric cards (`MetricCards`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 47 | Success rate value, tone (>0.9 pos, >0.7 warn, else neg) | `W[r].successRate` (export `windowStats(p,r).successRate`) | % 1 dp | per range | `S[r].successRate` | exists | Tone thresholds are UI rules (differ from `successTone` 0.8/0.6 elsewhere). |
| 48 | Success delta "+x.x pts" (hidden if \|d\| < 0.05) | `W[r].successDeltaPts` | pts 1 dp | per range | `S[r].successDeltaPts` | exists | Closing fifth vs opening fifth (FIELDS "Deviations"). |
| 49 | Success rank "#N/{NETWORKS.length}" | `W[r].successRank` (export: `rankHigher` over fleet `windowStats`) | integer | per range; podium colours | none on `NET`; derive from `RK[r] → successRate` of all networks | exists-but-different-shape | All-networks figure; needs `/v1/rankings` per range. Add `S[r].ranks.successRate {rank, peers}` precomputed. |
| 50 | Caption "{n} requests" | `W[r].requests` | integer | per range | `S[r].requests` | exists | |
| 51 | Success sparkline (32 px) with hover label per bucket (day, or "day · HH:MM–HH:MM UTC") | `W[r].success[]` values, `W[r].successAgo[]` minutes before `generatedAt` | fraction; 24h = 96 pts in mock, 7d = 7, 30d = 30 | per range; hover | `CH[r].success[] {t, v}`; `t` minutes before as-of; `generatedAt` on the answer | exists | API 24h points are the snapshot's 5-minute series buckets: 288 points, one per bucket (empty buckets included); 30 minutes is the every-request scoring time slot, not the series step. Mock has 96 (`viewWindow.success`, thinned): downsample client-side or draw 288. |
| 52 | $ / GB value, tone by price rank (≤1 pos, ≤3 warn, else neg), caption "Lowest list rate" | `NJ.pricing.listRate`, `NJ.pricing.priceRank` (export `rankLower(listRate, fleet)`) | $ 2 dp; rank | not per range | list rate as #28; rank: `P.pricing.headlinePriceRank` (MEASURED-FIELDS-1: the catalog value, else computed: the list rate's rank among the networks of its kind, cheapest first, ties share the better rank) | exists | Price rank is a per-kind figure; no list rate in `/v1/rankings`. The page's list rate is `networks.json pricing.listRate ?? home.json windows[30d].listRate` (`review-data.ts`), so a network whose review export has no price (Shifter: `pricing` null) still shows the home file's rate ("$0.75 / GB") with a null `priceRank`. |
| 53 | Availability value "99.96%" (2 dp), tone (>99.8 pos, >99 warn) | `W[r].uptimePct` | % | per range | `S[r].uptimePct` | exists | |
| 54 | Availability rank "#N/N" | `W[r].availabilityRank` (export `rankHigher` over fleet uptime) | integer | per range | none (no `uptimePct` in `/v1/rankings`) | exists-but-different-shape | All-networks figure: today 18 `NET` requests. Source: `windowStats.uptimePct` of every network, rank at build. Served one network per request (`S[r].uptimePct`; `P.pricing.pricePerGbAtScale` / `pricePerGb`), so the field rank, leader or peer $/GB needs one `NET` per network or the dataset file's `pages` (as section 4 row 39). |
| 55 | Caption "{duration} weighted degradation" | `W[r].downtimeMinutes` | "x min" / "h h m min" | per range | `S[r].downtimeMinutes` | exists | Null for v1 snapshots. |
| 56 | Availability sparkline | `W[r].availability[]`, `W[r].availabilityAgo[]` | fraction; 24h 288 pts, 7d 7, 30d 30 | per range | `CH[r].availability[] {t, v}` | exists | |
| 57 | DC ASNs share, tone (>0.1 neg, >0.05 warn) | `W[r].dcAsnShare` (export: `p.measurement.windows[r].pool.datacenter_asn_share`) | % 1 dp | per range | `P.trust.datacenterAsnShare` (30d only); `RK[r].datacenterAsnShare` (provider-level 30d value in every range) | exists-but-different-shape | Per-range value missing. Add `S[r].datacenterAsnShare` from `w.pool.datacenter_asn_share`. Export: a demo network (no measurement) gets its 30d `trust.datacenterAsnShare` in every range; the measured network's is per window. |
| 58 | DC rank "#N/N" | `W[r].dcRank` (export `rankLower`) | integer | per range | derive from `RK[r].datacenterAsnShare` | exists-but-different-shape | All-networks; add precomputed rank. Same basis as row 57. |
| 59 | Rank tooltip "Rank among {n} residential networks" | `NETWORKS.length` | integer | — | `RK[r] → peers` / count of residential rows | exists-but-different-shape | Needs `/v1/rankings`. |

##### 4c. Pool size chart (`PoolChart` + `TimeChart`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 60 | Header total "Unique IPs" | `W[r].uniqueIps` | integer, grouped | per range | `S[r].uniqueIps` | exists | |
| 61 | Area series: distinct exit IPs per bucket; x = bucket time, y from zero | `W[r].pool[]`, `W[r].poolAgo[]` | IPs; 24h 289/288 pts (5 min), 7d 7, 30d 30 daily | per range; hover readout with bucket time | `CH[r].pool[] {t, v}` | exists | 24h is one point per released hour (24 pts) for estimated windows (API-ESTIMATES-1). |
| 62 | X-axis labels ("24h ago, 18h, 12h, 6h, now" / "7d ago, 5d, 3d, now" / "30d ago, 20d, 10d, now") | `W[r].axis` (export `stats.range.axis`) | text | per range | `S[r].range.axis` | exists | |
| 63 | Y-axis ticks (5), N/A when no points | computed in `TimeChart` | compact | — | — | exists | Client rendering. |

##### 4d. Latency chart (`LatencyChart`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 64 | Toggle "Under load" (default) / "Normal requests" | `W[r].latency.load` / `W[r].latency.normal` (export `latencyHistory(p, stats, mode)`) | — | per range and mode | — | — | — |
| 65 | Under-load series p50 (area), p90, p99 (dashed) | `W[r].latency.load.points[][p50,p90,p99]`, `.ago[]` | ms; 24h 289 pts, 7d 7, 30d 30 | per range | none: `CH[r].latency[]` is the series rows' `latency_ms` (`seriesLatency`), the light-load latency, and only when the window latency is HTTP-only | missing | The snapshot already publishes it per bucket: `windows[r].series[].latency_under_load_ms {p50, p90, p99}` (written when the window has a latency target). The API parses it (`snap.SeriesRow.LatencyUnderLoadMs`) and uses its p50 for the latency trend (`measuredLatency`), but the chart's `latency[]` takes `latency_ms`. Add `CH[r].latencyUnderLoad[] {t, p50, p90, p99}` in the build; no calculator change (NET-LAT-LOAD). |
| 66 | Normal-requests series p50/p90/p99 | `W[r].latency.normal.points[]` | ms | per range | `CH[r].latency[] {t, p50, p90, p99}` | exists | Series `latency_ms` is the light-load (pulse) latency of the latency target's rows, null where the window's latency is not `generic_http`'s alone (`seriesLatency`). Empty for demo networks. |
| 67 | `aggregate` flag per mode | `W[r].latency.*.aggregate` | bool | not rendered | — | editorial | Exported, unused. |
| 68 | Shaded incident bands (red, fraction of window) | `W[r].bands[][from,to]` (export `stats.bands`) | fraction 0–1 of window | per range | `CH[r].bands[] {from, to, label}` | exists | Object instead of tuple; `label` "major · <country>" not shown (could be a tooltip). |
| 69 | Empty state "N/A: under-load latency is not published for this window." / normal variant | `!points.some(p50)` | text | — | empty `CH[r].latency` | exists | |
| 70 | Hover tooltip: bucket time, p50/p90/p99 | `bucketTimes(ago)` from `NJ.generatedAt` | UTC | hover | `CH[r].generatedAt`, `latency[].t` | exists | |

##### 4e. Coverage evidence (`CoverageEvidence`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 71 | Total tile Unique IPs + rank | `W[r].coverage.uniqueIps` (export `pool.unique_ips`), `.ranks.uniqueIps` (export `rankHigher` over fleet `ipSamples`/pool) | compact; "#N" (tone: 1 pos, ≤3 info) | per range | `S[r].uniqueIps`; `P.tileRanks[r].uniqueIps {rank, peers}` | exists | Export: the tile ranks are computed over every network's 30d pool and IP samples (`cov()` reads `pool.uniqueIps30d`, `ipSamples`) in every range; the totals use the window's pool for a measured network but fall back to the 30d pool for demo networks, so on 24h and 7d the Coverage "Unique IPs" tile can disagree with the Pool size header. The API's `tileRanks[r]` and `R[r].lists` are per range. |
| 72 | Total tile Countries + rank | `W[r].coverage.countries`, `.ranks.countries` | integer | per range | `R[r].lists.country` (or `R[r].exitCountries`); `P.tileRanks[r].countries` | exists | Rank basis as row 71 (30d in every range in the export). |
| 73 | Total tile States / regions + rank | `W[r].coverage.regions`, `.ranks.regions` | integer | per range | `R[r].lists.region`; `P.tileRanks[r].regions` | exists | Rank basis as row 71 (30d in every range in the export). |
| 74 | Total tile Cities + rank | `W[r].coverage.cities`, `.ranks.cities` | integer | per range | `R[r].lists.city`; `P.tileRanks[r].cities` | exists | Rank basis as row 71 (30d in every range in the export). |
| 75 | Total tile ASNs + rank | `W[r].coverage.asns`, `.ranks.asns` | integer | per range | `R[r].lists.asn`; `P.tileRanks[r].asns` | exists | `P.pool.*` holds the same counts for 30d. Rank basis as row 71 (30d in every range in the export). |
| 76 | Table rows (City × ASN): flag + country name | `W[r].geo[].countryCode`, `.country` (export `geoRows(p, fleet, r).slice(0, 60)`) | text | per range | `COV[r] → rows[][0]` countryCode, `[1]` country | exists | Export truncated to 60 rows (Shifter has 32,761 cities, 9,236 ASNs): the page must page through `COV`. First page also in the dataset file `lists[slug][kind][r].city_asn`. Export rows are the 30d-ranked `geoRows(p, fleet, r)` cut to 60. |
| 77 | Column State / region | `geo[].region` | text | — | `rows[][2]` | exists | |
| 78 | Column City | `geo[].city` | text | — | `rows[][3]` | exists | |
| 79 | Column ASN / ISP: name + "AS{n}" | `geo[].asnName`, `geo[].asn` | text / int | — | `rows[][5]` asnName, `[4]` asn | exists | |
| 80 | Column Type badge ("Residential") | `geo[].type` (export `r.typeLabel \|\| r.usageType`) | text | — | `rows[][6]` usageType | exists-but-different-shape | API passes the usage type (DB-IP); the display label map (Residential/Hosting/…) is not served. Unverified which raw values appear. |
| 81 | Column IPs | `geo[].ips` | integer | — | `rows[][7]` ips (× `breakdown_scale` for estimated windows) | exists | |
| 82 | Column Latency + rank under it | `geo[].latency` (0 dp), `geo[].latencyRank` | ms; "#N" | — | `rows[][8]` latencyP50; `rows[][9]` countryLatencyRank, `[10]` peers | exists-but-different-shape | API rank is the row's country rank among networks (FIELDS "Capped snapshots"), not a City×ASN rank; tooltip should say so. |
| 83 | Row order | `geoRows` order (old site) | — | not sortable on page | `COV`: most IPs first, ties in list order | exists | No column sorting on page; none in API. |
| 84 | Search box "Search country, state, city, ASN or ISP…" (client filter over the 60 rows: country, code, region, city, asnName, "as{asn}") | `rows.filter(...)` | text | case-insensitive substring | `COV[r]?q=` (every word in code, country, region, city, `AS<n>`/number, ASN name) | exists | Server-side search over every row; debounce on the client. |
| 85 | "{n} results" | `rows.length` | integer | updates with search | `COV → total` (with `q`: also `totals {ips, countries, cities, asns}`) | exists | |
| 86 | Paging | none (scroll box, h 640 px, sticky header, pinned first column on phones) | — | — | `COV → page`, `pageSize` 50, `pages` | exists | Page must add paging or infinite scroll. |
| 87 | Footnote "Presence means at least one exit was independently observed." | hard-coded | — | — | — | editorial | |
| 88 | Country / region / city / ASN place lists (not shown on this page) | — | — | — | `/v1/networks/{slug}/places?kind=&dimension=country\|region\|city\|asn&range=&q=&page=`; `/v1/places/region/{key}`, `/v1/places/city/{key}` | exists | Available, unused by the current design (one combined City×ASN table only). Region, city and ASN rows have requests and latency only from pulses; daily tests record them per requested country, so those rows' nulls carry `not_measured_per_place` in the page's `na` (MEASURED-FIELDS-1). |

##### 4f. Live tape (`ReviewTape`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 89 | Subtitle "Simulated request feed" (demo) / "Measured test events · newest first" | `NJ.source` | text | — | `P.source` | exists | |
| 90 | Filter tabs All / Census / Pulse (+request) / Incidents / Audit | `NJ.tape[].kind` (export: `dataset.tape` filtered by provider and product, first 30) | enum | client filter; empty state "No events in this window" | `NET → tape[]` (newest 20, `records.tape` full count); kinds `census\|pulse\|incident\|audit` (`tapeCategory`) | exists-but-different-shape | Tape does not follow the range (mock neither). For more than 20 rows or a per-filter list: `/v1/events?product=<framework product>&category=` (up to 500). Mock kinds `rotation`, `overlap` are demo-only. `NET` tape, reviews and community runs (and `records.*`) are every product of the network: `?kind=residential` does not filter them; the page shows residential rows only (export filters). Filtering the newest 20 client-side can leave fewer rows than exist (NET-RECORDS-KIND). |
| 91 | Row icon tone (signal >0 pos, <0 neg, else info), title, detail, value, age ("18h") | `tape[].signal`, `.title`, `.detail`, `.value`, `.agoSeconds` | text; seconds | — | `tape[].signal`, `.title`, `.detail`, `.value`, `.agoSeconds` (+ `at`) | exists | |

##### 4g. Per-target results (`TargetResults`) — add browser scores next to the percentages

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 92 | Row set and order (by difficulty asc) | `W[r].targets[]` (export `targetBenchmarksFor(p, r)`), meta `targets.json → targets[]` (8 ids incl. `ticketing_queue`) | — | per range (mock values identical across ranges) | `P.targets[]` (30d only); meta only in `/v1/dataset → targets[]` (`label`, `category`, `difficulty`, `passCondition`, `engine`, `group`) | exists-but-different-shape | 24h and 7d target rows missing. Target meta has no small endpoint (dataset file ~9 MB). API cards: targets with `group` null (generic_http, generic_https, amazon_pdp, google_search, linkedin_public, instagram_web, cloudflare_challenge, ai_answers); `ticketing_queue` is not an API target; `ai_answers` sources should not be rows. |
| 93 | Target label, category; row tooltip pass condition | `TARGETS[id].label`, `.category`, `.passCondition` | text | — | `/v1/dataset → targets[]` | exists-but-different-shape | Add target meta to `NET` (or `/v1/targets`). Without a meta row the label falls back to the raw target id and the category to blank; label and category go through `t()` as translation keys (TGT-14). |
| 94 | Difficulty 5-bar meter, "Difficulty {n} of 5" | `TARGETS[id].difficulty` | 1–5 | sort key | `/v1/dataset → targets[].difficulty`; also `P.browserOverall.<engine>.success.weights[id]` for browser targets | exists-but-different-shape | As #93. A target without a difficulty sorts last (`?? 9`), lights 0 bars and reads "Difficulty N/A of 5". |
| 95 | Success % + meter, tone (≥0.9 pos, ≥0.7 warn) | `targets[].successRate` | % 1 dp | per range | `P.targets[].successRate` (browser: = `outcomes.ok` of `engine`) | exists | 30d only (#92). |
| 96 | NEW (score next to success %): Success score 1–10 | — | 1–10 | per engine | browser targets: `P.targets[].scores.success.score` (status `ranked` else null; show `status` text for `not_enough_data`/`too_few_networks`/`all_similar`) | exists | Website does not render yet. HTTP targets have no score. |
| 97 | Success rank "#N" under the meter | `targets[].successRank` (export `rankHigher` over fleet per target) | integer | per range | browser targets: `P.targets[].scores.success.rank` / `.rankedOf` (fractional on ties); HTTP targets: none | exists-but-different-shape | HTTP targets (generic_http/https) need a computed per-target rank; browser rank is rank-based and only when `ranked`. |
| 98 | Challenge % | `targets[].captchaRate` | % 1 dp | per range | `P.targets[].captchaRate` (browser: `outcomes.challenge`) | exists | 30d only. |
| 99 | NEW: Challenge score 1–10 (next to Challenge %) | — | 1–10 | — | `P.targets[].scores.challenge {score, rank, rankedOf, status}` | exists | Browser targets only. |
| 100 | Hard block % | `targets[].blockRate` | % 1 dp | per range | `P.targets[].blockRate` (browser: `outcomes.block`) | exists | |
| 101 | NEW: Block score 1–10 (next to Hard block %) | — | 1–10 | — | `P.targets[].scores.block {score, rank, rankedOf, status}` | exists | Browser targets only. |
| 102 | NEW: engine used for the row's figures (Camoufox / Chrome) and per-engine figures/scores | — | text | engine switch if the site shows both | `P.targets[].engine`; `P.targets[].engines.<engine>.{requests, successes, outcomes, pageLoadP50, pageLoadP90, scores}` | exists | Never add engines together (FIELDS "Browser targets"). |
| 103 | P50 latency + latency rank | `targets[].latencyP50`, `targets[].latencyRank` (export `rankLower`) | ms; "#N" | per range | `P.targets[].latencyP50`: `generic_http`'s headline, `generic_https`'s display-only light-load latency of its own successes (HTTPS-LATENCY-1; null below 30 successes in every time slot); browser targets: `pageLoadP50`/`pageLoadP90` instead; rank: none (generic_http rank = `P.tileRanks[r].latency`) | exists-but-different-shape | Browser targets have no latency by design; the column should show page load for browser rows. `generic_https` latency is display only (no score reads it); `/v1/targets` cells carry it too. Per-target latency rank missing. |
| 104 | Requests | `targets[].requests` | integer | per range | `P.targets[].requests` (0 when unmeasured) | exists | |
| 105 | NEW per-network browser figure: overall browser success score per engine (in the table header or the Benchmark "Rank by dimension" list) | — | 1–10, rank of rankedOf | per engine | `P.browserOverall.<engine>.success {score, rank, rankedOf, status, targets, weights}` | exists | Difficulty-weighted; not written when the snapshot has none. |
| 105a | NEW: outcome shares beyond ok / challenge / block (login wall, timeout, proxy error, no content, no answer, other error), e.g. a row tooltip or a stacked bar | — | fractions | per engine | browser targets: `P.targets[].outcomes.{loginWall, timeout, proxyError, noContent, noAnswer, otherError}`; per engine `P.targets[].engines.<engine>.outcomes.*` | exists | Not shown today. Browser targets only, 30d only (NET-TARGETS-RANGE). Never add engines together. |
| 105b | NEW: group target breakdown: one sub-row per source of `ai_answers` (label, success rate, requests, outcomes) | — | %; count | — | `P.targets[].sources[] {target, label, successRate, requests, outcomes}` (written on group targets only); beside each sub-row's rates the source's own `P.targets[target=<source>].scores.success\|challenge\|block` and `.engines.<engine>.scores` (`{score, rank, rankedOf, status}`), as section 3 row 5.24 | exists | Not shown today. The sources (`ai_overview`, `ai_chatgpt`, `ai_gemini`) are also listed as their own `P.targets[]` entries with `group` set: draw them only as sub-rows of `ai_answers`, never as rows (#92). |
| 106 | Footnote and link "See what each request asserts." | hard-coded | — | — | — | editorial | |

##### 4h. Ownership split (`OwnershipSplit`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 107 | Exclusive share (big), tone (≥0.75 pos, ≥0.4 warn) | `W[r].ownership.exclusive` (export `ownershipFor(p, r)`) | % 1 dp | per range | `P.overlap.exclusiveShare` (30d) | exists-but-different-shape | Per-range values missing (mock identical across ranges). |
| 108 | Shared share; two-part bar | `W[r].ownership.shared` | % 1 dp | per range | `P.overlap.sharedShare` (30d) | exists-but-different-shape | As #107. |
| 109 | Label badge ("Mostly exclusive", "Fully exclusive") | `W[r].ownership.label` (old-site `ownershipFor`) | text | — | none | exists-but-different-shape | Derive from `exclusiveShare` client-side, or serve `overlap.label`; thresholds live in the old lib. |
| 110 | Overlap peer cards (max 4): monogram, name, "{n} shared IPs", share of ours (0 dp), link if peer is listed | `W[r].ownership.overlaps[] {slug, name, sharedIps, shareOfOurs}` (export: `measurement.windows[r].overlap.pairs` first 4) | integer; % | per range | `P.overlap.overlaps[] {slug, name, sharedIps, shareOfOurs, shareOfTheirs}` (30d, all peers) | exists | Per range only 30d. Link check needs the network list (`/v1/networks`). |
| 111 | "Full overlap matrix" link, footnote | hard-coded | — | — | — | editorial | |

### 5. Pricing (`Pricing`, `PlanCarousel` in `review-measurements.tsx`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 112 | Stat $ / GB + sub "Lowest list rate · $at-scale–$entry/GB" | `NJ.pricing.listRate`, `.pricePerGbAtScale`, `.pricePerGb` | $ 2 dp | — | `P.pricing.pricePerGbAtScale`, `P.pricing.pricePerGb` (list rate as #28) | exists | Snapshot `lowest_per_gb`/`entry_per_gb` win over catalog. The page's list rate is `networks.json pricing.listRate ?? home.json windows[30d].listRate` (`review-data.ts`), so a network whose review export has no price (Shifter: `pricing` null) still shows the home file's rate ("$0.75 / GB") with a null `priceRank`. |
| 113 | Stat Free trial Available / Not available / N/A + terms | `NJ.pricing.hasTrial` (derived: trial set and not "none"), `NJ.pricing.trial` | text | — | `P.pricing.trial` | exists-but-different-shape | `hasTrial` is derived client-side from free text; add a boolean. Sub-line fallback "Trial terms confirmed by {name}" when there is no trial text. |
| 114 | Stat Value rank "#N of {total}" | `W[r].valueRank` (export `providerView(...).valueRank`) | integer | per range | `RK[r].valueRank` (cost-board position); `P.pricing.valueRank` is a catalog value | exists-but-different-shape | Needs `/v1/rankings?range=r`; add to `S[r]` as `{rank, peers}` (NET-VALUE-RANK). The page's "of {total}" is `NETWORKS.length` (every residential network, priced or not); the API rank is the position among networks with a `costPer1kSuccess` in that range (all kinds until W2's within-kind rank), so the total is the count of RK rows with a non-null `valueRank`. |
| 115 | Stat Entry tier "Smallest plan's $ / GB" | `NJ.pricing.pricePerGb` | $ 2 dp | — | `P.pricing.pricePerGb` | exists | |
| 116 | Plan cards (4 per page, arrows wrap): name, "Popular" (recommended), "Entry" (first), "{n}% off", struck-through entry rate, unit price $/GB or $/IP, quantity (GB or IPs), total price, struck full price | `NJ.pricing.plans[] {name, price, gb, ips, recommended, discountPct}`; unit = price/qty, full = entryRate×qty | $ 2 dp | client paging (PER_PAGE 4) | `P.pricing.plans[] {name, price, gb, ips, note, recommended, discountPct, catalog}` | exists | `catalog` JSONB (order URL, billing period, features) is unused by the page. Empty state "N/A: individual plan details have not been published in the dataset." when no plans (`na: pricing.plans`). |
| 117 | Plan CTA "Start free trial" / "Order on provider website" / "Order link unavailable" | `tryHref`, `hasTrial` | URL | — | `P.products[].url`, `P.website`; per plan `plans[].catalog` order URL | exists | |
| 118 | 90-day price history sparkline, "Changed {n} days ago" / "No published change", last change % ("+x.x%"), "Recorded range $lo–$hi" | `NJ.pricing.history[]` (90 values, export `view.price90d`), `.lastChangePct`, `.daysSinceChange` | $; days; % | — | `P` has no series (`series.pricePerGb90d` "none, always"; series not in `NET`) | missing | Source: snapshot `products[].pricing` (`effective_from`, `valid_until`) across published snapshots, or a `site.pricing` history table; no source yet for a 90-day series. With no change the sub-line reads "The published rate stayed stable throughout the window." and the change prints "0%" (`(lastChangePct ?? 0)`; the export writes `lastChangePct: 0`). |
| 119 | "Recommended alternative": peer name, monogram, "{success} success · {latency} median", "{n} cheaper" badge; else "Best-priced option" box | `W[r].alternative {slug, name, successRate, latencyP50, cheaperBy}` (export `viewWindow.cheaperAndBetter[0]`; `cheaperBy = 1 − alt.costPer1k / own.costPer1k`) | %; ms; % 0 dp | per range | none; derivable from `RK[r]` (`costPer1kSuccess`, `successRate`, `latencyP50`) | exists-but-different-shape | All-networks derivation. Add `S[r].alternative` precomputed (cheaper and better rule from old `providerView`). In the mock, `alternative` is null in every range of all four networks except kestrel-net 7d, so the "Best-priced option" box is what shows today. Fallback box: "Lowest-cost comparable product" with a "Best value" badge, and the empty text "No cheaper measured provider delivers a comparable success rate." |
| 120 | "Good fit for" chips (tone cycles by index) | `NJ.bestFor[]` | text | — | `P.bestFor` | exists | |
| 120a | "Good fit for" empty state: "N/A" chip and "No product-specific recommendations have been published." when `bestFor` is empty | `r.bestFor.length` | text | — | `P.bestFor` (`[]`, listed in `na` until `site.providers.best_for` is filled) | exists | With the API this is the usual state for measured networks (catalog field). |
| 121 | Section subtitle link "what a delivered result actually costs" | hard-coded | — | — | — | editorial | |

### 6. Review article (`ReviewArticle.astro`; only when an article exists)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 122 | Verdict pill, "{n} min read", "updated {date}", title, "The bottom line", article HTML, "In this review" contents (h2 ids) | `editorial.json networks[slug].review` ?? `NJ.review {verdict, readingMinutes, updated, title, bottomLine, html, headings}` (export: milf `src/content/reviews/<slug>.md`, markdown → HTML in the export) | text / HTML | anchors | none | editorial | Editorial content with no API source. If the API should serve it: a `site.review_articles` table (or keep in the website). `description`, `suitedTo`, `notSuitedTo` are exported but not rendered. |
| 123 | Company facts: Founded | `NJ.founded` | year | hidden when null | `P.founded` | exists | |
| 124 | Company facts: Headquarters | `NJ.hq` | text | — | `P.hq` | exists | |
| 125 | Company facts: Index rank "#N of {NETWORKS.length}" | `HN.standing.rank`, `NETWORKS.length` | integer | — | `RK["30d"] → rank`, `peers` | exists-but-different-shape | As #20. |
| 126 | Company facts: Index Score "81.1 (A)" | `HN.score.composite`, `.grade` | 1 dp | — | `P.score.composite`, `.grade` | exists | |
| 127 | Company facts: Price per GB "from $x" | `listRateOf(n)` = `HW.listRate` | $ 2 dp | — | as #28 | exists-but-different-shape | |
| 128 | Company facts: Free trial | `NJ.pricing.hasTrial`, `.trial` | text | — | `P.pricing.trial` | exists-but-different-shape | As #113. |
| 129 | Company facts: Website host link | `NJ.website` | URL | — | `P.website` | exists | |
| 130 | CTA "Put {name} to work where it wins", Try / Compare alternatives | `tryHref` | — | — | as #18 | editorial | |

### 7. Benchmark (`ReviewBenchmark.astro`, `review-editorial.ts benchmarkFor`, `standingsFor`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 131 | Step 1 "How we tested {short}" text: product label, `protocol.summary`, requests, targets, distinct IPs, countries | `NJ.protocol.label`, `NJ.protocol.summary` (export `measuredProtocol(p).summary` ?? `scope.summary`), `W["30d"].requests`, `NJ.protocol.targets`, `W["30d"].coverage.uniqueIps`, `.countries` | text + numbers | 30d fixed | `P.measuredProduct`; summary only in `/v1/dataset → scopes[]` (site.scopes); `S["30d"].requests`, `S["30d"].uniqueIps`, `R["30d"].exitCountries` | exists-but-different-shape | Scope copy needs the dataset file. "Sampled continuously in 5-minute buckets" (hard-coded in `review-editorial.ts`) matches the stored facts (the calculator's `coverage_basis` "5-minute buckets"; the 24h series step); 30 minutes is the scoring time slot, a different thing. Copy can stay. |
| 132 | Setup chips: Requests sent, Target sites, Exit IPs seen, Countries | `setup` from #131 | integers | 30d | as #131 | exists-but-different-shape | Target count as #42. |
| 133 | Rank by dimension: Success rate value + rank /total + leader | `W["30d"].successRate`, `.successRank`; leader = max over `ALL_REVIEWS` 30d | % ; rank | 30d | `S["30d"].successRate`; rank and leader from `RK["30d"].successRate` | exists-but-different-shape | All-networks (rankings). Bar = (total − rank + 1)/total, tone thirds (`rankTone`). |
| 134 | Rank by dimension: Availability + rank + leader | `W["30d"].uptimePct`, `.availabilityRank` | % 2 dp | 30d | `S["30d"].uptimePct`; rank/leader: none | exists-but-different-shape | As #54 (no uptime in rankings). Served one network per request (`S[r].uptimePct`; `P.pricing.pricePerGbAtScale` / `pricePerGb`), so the field rank, leader or peer $/GB needs one `NET` per network or the dataset file's `pages` (as section 4 row 39). |
| 135 | Rank by dimension: Median latency + rank (computed in page) + leader | `W["30d"].latencyP50`; rank `1 + count(lower)` over `ALL_REVIEWS` | ms | 30d | `S["30d"].latencyP50`; `P.tileRanks["30d"].latency {rank, peers}`; leader from `RK["30d"].latencyP50` | exists | Leader name/value needs `/v1/rankings`. |
| 136 | Rank by dimension: Unique exit IPs + rank + leader | `W["30d"].coverage.uniqueIps`, `.ranks.uniqueIps` | compact | 30d | `S["30d"].uniqueIps`, `P.tileRanks["30d"].uniqueIps`; leader `RK["30d"].uniqueIps` | exists | |
| 137 | Rank by dimension: Countries + rank + leader | `W["30d"].coverage.countries`, `.ranks.countries` | integer | 30d | `R["30d"].exitCountries`, `P.tileRanks["30d"].countries`; leader `RK["30d"].countries` | exists | |
| 138 | Rank by dimension: ASN spread + rank + leader | `W["30d"].coverage.asns`, `.ranks.asns` | integer | 30d | `R["30d"].lists.asn`, `P.tileRanks["30d"].asns`; leader: none (no ASN count in rankings) | exists-but-different-shape | Leader needs 18 `NET` requests; add `asns` to rankings rows. |
| 139 | Rank by dimension: Hosting ASN share + rank + leader | `W["30d"].dcAsnShare`, `.dcRank` | % | 30d | `P.trust.datacenterAsnShare`; rank/leader from `RK["30d"].datacenterAsnShare` | exists-but-different-shape | All-networks derivation. |
| 140 | Rank by dimension: Cost per 1k successes + rank + leader | `W["30d"].costPer1kSuccess`, `.valueRank` | $ 2 dp | 30d | `S["30d"].costPer1kSuccess`; `RK["30d"].valueRank`, leader `RK["30d"].costPer1kSuccess` | exists-but-different-shape | Needs rankings. |
| 141 | Rank by dimension: List price per GB + rank + leader | `NJ.pricing.listRate`, `.priceRank` | $ 2 dp | — | list rate as #28; rank/leader: none | exists-but-different-shape | All-networks list rate is not served in one answer; add `listRate` + `priceRank` to rankings rows. Served one network per request (`S[r].uptimePct`; `P.pricing.pricePerGbAtScale` / `pricePerGb`), so the field rank, leader or peer $/GB needs one `NET` per network or the dataset file's `pages` (as section 4 row 39). The page's list rate is `networks.json pricing.listRate ?? home.json windows[30d].listRate` (`review-data.ts`), so a network whose review export has no price (Shifter: `pricing` null) still shows the home file's rate ("$0.75 / GB") with a null `priceRank`. The benchmark leader is computed over `ALL_REVIEWS` without that fallback, so such a network can never be the price leader. |
| 141a | Benchmark rank empty state: a dimension whose value is N/A gets no rank ("N/A"), whatever the export says; steps with no text are dropped from the list | `review-editorial.ts` `standingsFor` (`x.value === NA ? {rank: null}`); `ReviewBenchmark.astro` `.filter((s) => s.body.length)` | text | — | inputs of rows 133-141 | exists | Client rules over null values. |
| 142 | NEW row: Browser overall (per engine) score + rank of rankedOf | — | 1–10 | 30d | `P.browserOverall.<engine>.success.{score, rank, rankedOf, status}` | exists | Suggested placement next to the dimension ranks. |
| 143 | Header "30 days", total `/N` | `NETWORKS.length` | integer | — | `RK["30d"]` peers | exists-but-different-shape | |
| 144 | Steps "Where {short} leads" / "trails" / "What would move it up" text (leaders, "#rank of total", IMPROVE templates) | generated from #133–#141 unless `editorial.json networks[slug].benchmark` | text | — | as #133–#141; editorial override: none | editorial | Generated copy; its inputs are the rank rows. |
| 145 | Step "Target by target": strongest/weakest target with success %, "highest success rate in the Index on {list}" (successRank === 1) | `W["30d"].targets[]` sorted by successRate; labels `TARGETS` | % | 30d | `P.targets[].successRate`; rank 1: browser `scores.success.rank === 1`, HTTP none; labels `/v1/dataset targets[]` | exists-but-different-shape | Add "best score" wording from `scores.success.score` (10 = best) next to the rate. |
| 145a | The "Target by target" step appears only with 2 or more measured targets (top and lowest named) | `targets.length >= 2` (`review-editorial.ts`) | text | — | `P.targets[]` with a non-null `successRate` | exists | With the API's 8 card targets it shows whenever the network is measured; target labels as row 93. |

### 8. Alternatives (`ReviewAlternatives.astro`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 146 | Three cards: the best-ranked other residential networks (by composite) | `NETWORKS.filter(slug≠).slice(0,3)` | — | sort by score desc | `RK["30d"]` rows in `position` order, filter `kind === 'residential'` and slug ≠ | exists | All-networks, one request. |
| 147 | Card: monogram, name, rank numeral (podium 1–3), link | `x.monogram`, `x.name`, `x.standing.rank`, `x.slug` | — | — | `RK["30d"].name`, `.rank`, `.slug` | exists | |
| 148 | Card: Index Score (1 dp), grade badge, score bar | `x.score.composite`, `x.score.grade` | 0–100 | — | `RK["30d"].score`, `.grade` | exists | Rankings score is the range's own score. `grade` is null in the API for an unscored network (`P.score.grade`, RK `grade`); the page prints it raw: blank badge in the score panel (row 22) and on alternative cards (row 148), "Grade null" in the overview (row 36). Website must print "N/A" (NET-NA-GRADE). |
| 149 | Card figures Success, Latency p50 (tones on `SCALES` success 0.75/0.9, latency 500/1000) | `windowOf(x.windows,'30d').successRate`, `.latencyP50` | %; ms | — | `RK["30d"].successRate`, `.latencyP50` | exists | Tone scales are UI constants. |
| 150 | Card figure $ / GB (tone price 2/5) | `windowOf(x.windows,'30d').listRate` | $ 2 dp | — | none in rankings | exists-but-different-shape | Needs each peer's `NET` (3 extra requests). Add `listRate` to `/v1/rankings` rows. Source: `listRate(pricing)` per provider. Served one network per request (`S[r].uptimePct`; `P.pricing.pricePerGbAtScale` / `pricePerGb`), so the field rank, leader or peer $/GB needs one `NET` per network or the dataset file's `pages` (as section 4 row 39). |
| 151 | Subtitle "Fictional demo alternatives…" when any demo | `x.source` | — | — | `RK.source` | exists | |
| 152 | "View all {short} alternatives" link | slug | — | — | — | editorial | |

### 9. Analysis (`ReviewAnalysis.astro`, `review-editorial.ts analysisFor/weeksOf/generatedEntry`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 153 | Source of the weekly entries | `editorial.json networks[slug].analysis[]` (agent; empty) else generated from `W["30d"].success[]`, `.successAgo[]`, `.availability[]`, `.pool[]` cut into 7-day weeks, newest first | — | — | daily inputs: `CH["30d"].success[]`, `.availability[]`, `.pool[]` (or `NET → days[]` `successRate`, `uniqueIps`); agent text: none | editorial | Agent analysis has no API source. Generated entries are a client derivation over 30 daily points (4 weeks). |
| 154 | Week numeral (ISO week of the period's middle day), "Published", "Next analysis" (+7 d) | `periodStart/End`, `generatedAt` (= min(week end + 1 ms, `GENERATED_AT`)) | dates | — | `CH["30d"].generatedAt`, point `t` | exists | Derived. |
| 155 | Headline ("held steady at", "gained/lost {d} points") | week mean success, delta vs previous week | % / pts | thresholds \|d\| < 1 steady | from `CH["30d"].success[]` | exists | Client derivation. |
| 156 | Change tiles: Success rate (Δ pts), Availability (Δ pts, 2 dp), Exit IPs a day (Δ %) with tones | week means of success, availability, pool | %; compact | tone eps 0.5 / 0.05 / 1 | `CH["30d"].success`, `.availability`, `.pool` | exists | |
| 157 | Paragraphs: date range, weakest day vs other six (if gap > 5 pts), gateway availability, distinct exit IPs a day, "{name} stands #{rank} of {total}" | as #155–#156; `HN.standing.rank`, `NETWORKS.length` | text | — | as above; rank `RK["30d"]` | exists-but-different-shape | Rank sentence needs rankings. |
| 158 | Byline "Written by the Shifter editorial agent" / "Built from this week's Index runs." | `entry.author` | enum | — | none | editorial | |
| 159 | Past analyses list: "{n} weeks", 5 shown, "Show {n} older weeks", each with date range, first delta badge, headline, 3 change values, paragraphs | `past[]` | — | expand in place | as above | exists | With only 30 daily points the archive is at most 3 weeks; a real archive needs stored weekly entries (missing source). |
| 159a | Empty states: whole section hidden when there is no week; "The first analysis for this network. Earlier weeks will appear here." when there is no past week | `current` / `past.length === 0` from `analysisFor(r, n, locale)` | text | — | `CH["30d"]` with fewer than 7 daily points | exists | Copy editorial. Weeks are cut from the end in 7-point slices (`weeksOf`, `end >= 7`). The API's 30d series has one point per day of the window, empty days included, so four weeks always exist; days before a network was measured average to N/A instead. The Analysis tab (#31) is listed even when the section is hidden. |

### 10. Customer reviews (`[slug].astro` section + `review-voices.tsx`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 160 | "{count} published · {verified} backed by a submitted run" | `NJ.userReviews.items.length`, count of `verification === 'run_verified'` | integers | — | `NET → records.reviews`; `P.reviews.count`, `P.reviews.verifiedCount` | exists-but-different-shape | Website counts its residential list. `verifiedCount` counts `invoice_verified` and `run_verified` (verified in code), so it is not the page's "backed by a submitted run" figure (NET-REVIEW-COUNTS). The page also mixes scopes: the count is residential items, while the mean (#161) and distribution (#162) are every product's (`p.reviews`); Nimbus shows "7 published" beside a distribution of 8. |
| 161 | Mean rating "4.3 / 5" + stars | `NJ.userReviews.mean` | 1 dp | — | `P.reviews.mean` | exists-but-different-shape | Every product's rows, not the residential ones the page shows (NET-RECORDS-KIND). |
| 162 | Distribution bars 5→1 with counts | `NJ.userReviews.distribution[5]` | counts | — | `P.reviews.distribution[5]` | exists-but-different-shape | Every product's rows, not the residential ones the page shows (NET-RECORDS-KIND). |
| 163 | "Verified only" toggle; filters All / Critical (≤3) / Positive (≥4) | `items[].verification`, `.rating` | — | client filter over the list | `NET → reviews[]` (newest 20) | exists-but-different-shape | Filters over at most 20 rows; with more reviews a filtered/paged reviews endpoint is missing (e.g. `/v1/networks/{slug}/reviews?filter=&verified=&page=`). `NET` tape, reviews and community runs (and `records.*`) are every product of the network: `?kind=residential` does not filter them; the page shows residential rows only (export filters). Filtering the newest 20 client-side can leave fewer rows than exist (NET-RECORDS-KIND). |
| 164 | Review item: stars, title, "by {author}", verification label badge, age, body, provider reply | `items[].rating`, `.title`, `.author`, `.verificationLabel` (export `verificationLabels[verification]`), `.agoSeconds`, `.body`, `.providerReply` | text | — | `reviews[].rating`, `.title`, `.author`, `.verification`, `.agoSeconds`, `.body`, `.providerReply` | exists-but-different-shape | Label text not served (UI map). `role`, `useCase`, `monthlySpend`, `monthsUsed`, `helpfulCount`, `invoiceMonth`, `linkedRunId` exist in API, unused by the page. The verification badge renders only for `run_verified`, never for `invoice_verified`. |
| 165 | Empty state "No published reviews yet." | — | — | — | empty `reviews[]` | exists-but-different-shape | Every product's rows, not the residential ones the page shows (NET-RECORDS-KIND). |
| 166 | "Write a review" composer (rating, title, body ≥ 20 chars, name, email) → Intercom or mailto via `openHandoff` | — | form | opened by `data-write-review` anywhere, `#write-review` hash | none | editorial | No API write path (by design; site handoff). |

### 11. Independent runs (`ReviewRuns.astro`, `ReviewRunChecks.astro`)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 167 | "Do they agree?" checks: Success rate (±3 pts), Median latency (±20%), Shared with other networks (±10 pts): Index value, runs median, Agrees/Differs, dot track | Index: `W["30d"].successRate`, `.latencyP50`, `.ownership.shared`; runs: accepted `NJ.runs[].successRate`, `.latencyP50`, `.overlapShare` | %; ms | paged 3 per page with arrows when > 3 | `S["30d"].successRate`, `S["30d"].latencyP50`, `P.overlap.sharedShare`; `NET → communityRuns[]` (`status`, `successRate`, `latencyP50`, `overlapShare`) | exists-but-different-shape | Tolerances are UI rules. Runs capped at 20 (`records.communityRuns` full count). `NET` tape, reviews and community runs (and `records.*`) are every product of the network: `?kind=residential` does not filter them; the page shows residential rows only (export filters). Filtering the newest 20 client-side can leave fewer rows than exist (NET-RECORDS-KIND). |
| 168 | `?demo-checks` design-review checks: Latency p90 (index ×1.06/0.97), Captcha rate 0.031 vs 0.034/0.029, Block rate 0.012 vs 0.031/0.026 | hard-coded in `ReviewRuns.astro` | % / ms | query flag | should be `S["30d"].latencyP90`, `P.performance.captchaRate`, `P.performance.blockRate`; runs: `communityRuns[].targets[]` (`captchaRate`, `blockRate`), no per-run p90 | editorial | Hard-coded numbers pretending to be data; remove before launch or wire to the fields named. |
| 168a | Summary "{n} of {total} checks agree with the Index"; no Agrees/Differs badge when the Index value or the runs median is missing | `checks.filter(c => c.agrees).length`, `checks.length`; `agrees = null` when `missing(index)` or no accepted run value | integers | `?demo-checks` adds three checks to the total | as #167 | exists | Client derivation over #167. The runs median is taken over at most the newest 20 runs of every product today (NET-RECORDS-KIND, NET-RUNS-PAGED). |
| 168b | "Runs, median" per check: "N/A" when no accepted run has the value | `c.median ?? 'N/A'` (`ReviewRunChecks.astro`) | text | — | `communityRuns[]` accepted rows (newest 20, every product: NET-RECORDS-KIND, NET-RUNS-PAGED) | exists-but-different-shape | |
| 169 | Empty state "No independent runs for {short} yet" | `accepted.length === 0` | — | — | empty `communityRuns` | exists-but-different-shape | Every product's rows, not the residential ones the page shows (NET-RECORDS-KIND). |
| 170 | Submitted runs: "{n} accepted of {total}" | `runs.filter(accepted).length`, `runs.length` | integers | — | `communityRuns[]`, `records.communityRuns` | exists-but-different-shape | `NET` tape, reviews and community runs (and `records.*`) are every product of the network: `?kind=residential` does not filter them; the page shows residential rows only (export filters). Filtering the newest 20 client-side can leave fewer rows than exist (NET-RECORDS-KIND). The run list is hidden when there are no runs. |
| 171 | Run row: initials, region flag (last comma part), contributor, status badge, region, age; accepted: Success, Latency, Unique IPs, Shared; else rejection reason / "Awaiting schema and measurement validation." | `runs[].submittedBy`, `.region`, `.status`, `.agoSeconds`, `.successRate`, `.latencyP50`, `.uniqueIps`, `.overlapShare`, `.rejectionReason` | %, ms, compact | — | `communityRuns[].submittedBy`, `.region`, `.status`, `.agoSeconds`, `.successRate`, `.latencyP50`, `.uniqueIps`, `.overlapShare`, `.rejectionReason` | exists-but-different-shape | Flag code parsed from region text; API has no region country code. Every product's rows, not the residential ones the page shows (NET-RECORDS-KIND). Status labels Accepted / Rejected / Pending; ages read "today", "{n}d ago" (under 45 days), "{n}mo ago". |
| 172 | Reproduce terminal: "GET /proxy-index/dataset.json", "network = {slug}, window = 30d", copy button | hard-coded path; `HN.slug`, `NJ.source` | text | copy to clipboard | `/v1/dataset` (302 to the dataset file) | editorial | Hard-coded path should become the public dataset URL (`/v1/dataset` through the website passthrough). |
| 173 | Three steps, "Submit a run", "Full methodology" | hard-coded | — | — | — | editorial | |

### 12. Closing CTA and footer

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 174 | CTA "Check these figures" (Shifter) / "Measure {short} against Shifter" | `n.slug === 'shifter'`, `n.name` | text | — | `P.slug`, `P.name` | editorial | |
| 175 | Footer, navbar | site-wide | — | — | — | editorial | |

### 13. networks.json keys exported but not rendered (provenance only)

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 176 | (not rendered) claim, strengths, weaknesses | `NJ.claim`, `NJ.strengths`, `NJ.weaknesses` (export `p.*`) | text | — | `P.claim`, `P.strengths`, `P.weaknesses` | exists | Candidates for the Overview "case for and against" the comment mentions. |
| 177 | (not rendered) score.rank, score.note | `NJ.score.{composite, grade, rank, note}` (note: `kindStanding(p).note`) | — | — | `P.score.rank`; note: none (`ScoreView.KindStanding()` text is not serialized) | exists-but-different-shape | Page uses `HN.score` instead, and the two files disagree today: Shifter 100 / "A+" (home.json) vs null / "N/A" (networks.json), breakdown `value` 100 vs null; Orbitnode 80.9 vs 80.7, Kestrel 74.1 vs 73.8. With the API both read `P.score` (NET-TWO-SOURCES). |
| 178 | (not rendered) trust: stickinessHoldRate, stickinessAdvertised, blacklistHitRate, leakChecks[] | `NJ.trust.*` | %, text | — | `P.trust.stickinessHoldRate`, `.stickinessAdvertised`, `.blacklistHitRate`, `.leakChecks[]` (catalog only) | exists | No trust panel in the current design. |
| 179 | (not rendered) pricing.costBasis | `NJ.pricing.costBasis` | text | — | `P.pricing.costBasis` | exists | |
| 180 | (not rendered) protocol.schedule, geography, rationale | `NJ.protocol.*` (export `measuredProtocol` ?? scope) | text | — | `/v1/dataset → scopes[]` | exists-but-different-shape | |
| 181 | (not rendered) window latencyP90/P99 (only in demo checks), costPer1kSuccess per range | `W[r].latencyP90`, `.latencyP99`, `.costPer1kSuccess` | ms, $ | per range | `S[r].latencyP90`, `.latencyP99`, `.costPer1kSuccess` | exists | |
| 182 | (not rendered) changelog | not exported | — | — | `NET → changelog[]` (≤20, the network's `/v1/changelog` entries), `records.changelog` | exists | The page has no changelog section; the brief lists one. |
| 183 | Data timestamp for bucket ages | `networks.json generatedAt` | ISO | — | every answer's `generatedAt` | exists | |
| 184 | (not rendered) score breakdown in the review export | `NJ.breakdown.*` (8 keys; export `p.score.breakdown`, 1 dp) | 0-100 | — | `P.score.breakdown.*` | exists | The page draws `home.json`'s breakdown instead (rows 23, 177; NET-TWO-SOURCES). |
| 185 | (not rendered) review counts in the export | `NJ.userReviews.count`, `.verifiedCount` (export `p.reviews`) | integer | — | `P.reviews.count`, `.verifiedCount` | exists | The page counts `items` instead; for Nimbus the file says 8 / 6 against 7 residential items (row 160, NET-RECORDS-KIND, NET-REVIEW-COUNTS). |
| 186 | (not rendered) plan note | `NJ.pricing.plans[].note` | text | — | `P.pricing.plans[].note` | exists | |
| 187 | (not rendered) review item product and run link | `NJ.userReviews.items[].product` (product label), `.linkedRun` (boolean `Boolean(linkedRunId)`) | text; boolean | — | `reviews[].product`, `reviews[].linkedRunId` (an id, not a boolean) | exists-but-different-shape | Website derives the boolean from the id (row 164). |
| 188 | (not rendered) file `source` string | `networks.json → source` ("proxy-reviews siteapi /v1/dataset") | text | — | none | editorial | Provenance of the export. |

### Further details (website rules, no API gap)

- Rows 103 and 105a: page load and the non-scored outcome shares (login wall, timeout, proxy error, no content, no answer, other error) have no rank-based score; only `success`, `challenge` and `block` are scored.
- The Benchmark board shows no leader column; leaders appear only in sentences. The "mid-table" sentence and the trails rule are client copy over rows 133-141.
- The tape subtitle reads "Simulated request feed · just now" for demo rows.
- Analysis entries carry `id`, `findings[]` and `daily[]`, which an archive would store (NET-ANALYSIS-ARCHIVE).

### Gaps summary

1. NET-PEERS: rank "of N" peers for hero, overview, article facts, rank tooltips (#20, #35, #59, #125, #143) — add `peers` to `P.score` (serialize `ScoreView.Peers`) — snapshot `scores.balanced.components.*.inputs.peers`.
2. NET-LISTRATE: list rate $/GB (#28, #127) — add `P.pricing.listRate` (at-scale else entry) — Go `listRate(pricing)`.
3. NET-LEADS: "Best in Index" flags need authenticity and list rate of every network (#40) — add `authenticity`, `listRate` to `/v1/rankings` rows — `trust.residentialAuthenticity`, `listRate(pricing)`.
4. NET-PROTOCOL: product label and test-target count (#41, #42, #131, #132) — add `P.protocol {label, targets, summary, schedule}` — `site.scopes`, measured product, measured target ids.
5. NET-SEARCH-REVIEWS: palette's list of networks with a review page (#46) — derive from `/v1/networks` or add `reviews` to `/v1/search-index` — provider list.
6. NET-RANK-SUCCESS: per-range success rank (#49, #133) — `S[r].ranks.successRate {rank, peers}` precomputed at build — `windowStats.successRate` of every network.
7. NET-RANK-PRICE: price rank (#52, #141) — served as `P.pricing.headlinePriceRank`, computed from the `listRate` of every network of the kind when the catalog has none (MEASURED-FIELDS-1).
8. NET-RANK-AVAIL: availability rank and leader (#54, #134) — `S[r].ranks.uptimePct` and `uptimePct` in `/v1/rankings` rows — `metrics.uptime` per network and range.
9. NET-DC-RANGE: per-range hosting-ASN share (#57) — `S[r].datacenterAsnShare` — `windows[r].pool.datacenter_asn_share`.
10. NET-RANK-DC: per-range DC rank (#58, #139) — `S[r].ranks.datacenterAsnShare` — same source as NET-DC-RANGE over every network.
11. NET-LAT-LOAD: under-load latency series for the chart (#65) — `CH[r].latencyUnderLoad[] {t, p50, p90, p99}` — snapshot `windows[r].series[].latency_under_load_ms` (published per 5-minute / daily bucket, already parsed by the API; build change only).
12. NET-USAGE-LABEL: City×ASN usage-type display label (#80) — map raw usage type to Residential/Hosting/Mobile/Business in `COV` rows or document the values — DB-IP usage type.
13. NET-COUNTRY-RANK-NOTE: latency rank in coverage rows is the country's, not the row's (#82) — document or add a per-row rank — `countryLatencyRanks`.
14. NET-TARGETS-RANGE: per-target rows for 24h and 7d (#92, #95, #98, #100, #104) — `S[r].targets[]` (or `?range=` on targets) — `windows[r].targets[id].metrics` (browser scores are per window in the calculator).
15. NET-TARGET-META: target label, category, difficulty, pass condition without the dataset file (#92–#94, #145) — embed `targets[]` meta in `NET` or add `/v1/targets` — `site.targets` / `public.targets`.
16. NET-TARGET-RANK: per-target success rank for HTTP targets (#97, #145) — `P.targets[].successRank {rank, peers}` — `w.targets[id].metrics.success_rate` across networks.
17. NET-TARGET-LATRANK: per-target latency rank and page-load column (#103) — use `P.tileRanks[r].latency` for generic_http, `pageLoadP50` for browser rows; add per-target latency rank if wanted — `w.targets[id]`.
18. NET-OVERLAP-RANGE: exclusive/shared share and peers per range (#107, #108, #110) — `S[r].overlap {exclusiveShare, sharedShare, overlaps[]}` — `windows[r].overlap` and `overlap.peers` per window.
19. NET-OWNERSHIP-LABEL: ownership label (#109) — `P.overlap.label` from fixed thresholds — `exclusiveShare`.
20. NET-TRIAL-BOOL: has-trial flag (#113, #128) — `P.pricing.hasTrial` — `site.pricing.trial`.
21. NET-VALUE-RANK: value rank on the network answer (#114, #140) — `S[r].valueRank` (cost-board position) — rankings cost board.
22. NET-PRICE-HISTORY: 90-day price series, last change %, days since change (#118) — `P.pricing.history[] {t, v}`, `lastChangePct`, `daysSinceChange` — no source yet (snapshot `products[].pricing.effective_from/valid_until` over successive snapshots, or a `site.pricing` history table).
23. NET-ALTERNATIVE: recommended cheaper-and-better alternative (#119) — `S[r].alternative {slug, name, successRate, latencyP50, cheaperBy}` precomputed — rankings rows (`costPer1kSuccess`, `successRate`).
24. NET-ARTICLE: editorial review article (verdict, title, bottom line, HTML, headings, reading time, updated) (#37, #122) — `site.review_articles` table served on `NET`, or keep in the website — milf `src/content/reviews/*.md` (no API source).
25. NET-SCOPE-COPY: protocol summary text for the method step (#131) — part of NET-PROTOCOL — `site.scopes.summary`.
26. NET-BUCKET-TEXT: withdrawn on verification: the "5-minute buckets" text (#131) matches the stored facts and the 24h series step; 30 minutes is the scoring time slot (section 13).
27. NET-ASN-LEADER: ASN-spread leader (#138) — add `asns` to `/v1/rankings` rows — `w.pool.asns`.
28. NET-ALT-PRICE: peer $/GB on alternative cards (#150) — `listRate` in `/v1/rankings` (same as NET-LEADS) — `listRate(pricing)`.
29. NET-ANALYSIS-ARCHIVE: stored weekly analyses (agent text and an archive beyond 30 days) (#153, #159) — `/v1/networks/{slug}/analysis` or a site table — no source yet (editorial.json is empty).
30. NET-RANK-SENTENCE: Index rank in the analysis and article facts (#125, #157) — covered by NET-PEERS plus `P.score.rank` — rankings.
31. NET-REVIEW-COUNTS: published and run-verified counts (#160) — `P.reviews.runVerifiedCount` or document `verifiedCount` — `site.reviews.verification`.
32. NET-REVIEWS-PAGED: filtered/paged reviews beyond 20 (#163) — `/v1/networks/{slug}/reviews?rating=&verified=&page=` — `site.reviews`.
33. NET-VERIFY-LABEL: verification label text (#164) — UI map, or `reviews[].verificationLabel` — old `verificationLabels`.
34. NET-SCORE-NOTE: kind-standing note ("#1 of 1 in its kind") (#177) — serialize `P.score.standing` text — `ScoreView.KindStanding()`.
35. NET-SCOPE-UNUSED: protocol schedule, geography, rationale (#180) — part of NET-PROTOCOL — `site.scopes`.
36. NET-RECORDS-KIND: tape, reviews and community runs on `NET` (and `records.*`, `P.reviews` count, mean and distribution) cover every product of the network; `?kind=` does not filter them, while the page shows residential rows only (#32, #90, #160–#163, #167, #170) — filter the record lists, their `records` counts and `P.reviews` by the requested kind — `site.reviews` product kind, `community_runs.product`, tape `product`.
37. NET-RUNS-PAGED: community runs beyond the newest 20; the "Do they agree?" medians and "{n} accepted of {total}" need every run of the kind (#167, #168a, #170) — `/v1/networks/{slug}/runs?kind=&status=&page=`, or the accepted runs' three figures in `NET` — `site.community_runs`.
38. NET-TWO-SOURCES: hero, score panel, overview, alternatives, article facts and JSON-LD rating read `home.json`, the rest `networks.json`, and they disagree (Shifter 100 / A+ vs null / N/A) (#7, #20–#23, #35–#39, #126, #177) — website reads `P.score` and `S[r]` for both — no API change.
39. NET-NA-GRADE: a null grade prints blank (score panel, alternative cards) or "Grade null" (overview) (#22, #36, #148) — website prints "N/A" — `P.score.grade`, RK `grade` (exist).
40. NET-MOCK-DATA: the `ticketing_queue` row (not an API target), three fictional networks (`SHOW_DEMO = true`) in ranks, totals and alternatives, and the `?demo-checks` hard-coded figures (#92, #151, #168) — website removes them when it reads the API — no API source.

### Performance notes

- Requests per page view if the page is fed from the API (per network, kind `residential`):
  - `NET` once (profile, stats ×3, ranges ×3 with top 50 countries each, tile ranks ×3, 30d targets with browser scores, overlap, pricing with plans, ≤20 reviews / community runs / changelog / tape rows). Bounded by `build.MaxAnswerBytes` (200 KB).
  - `CH[r]` once per range: 30d at load, 24h and 7d when the dock switches (or all three up front). 24h: one point per series bucket; at 5-minute buckets that is 288 points × (success, availability, latency p50/p90/p99, pool), about 40–50 KB JSON. 24h is always 288 five-minute points (empty buckets included); estimated IP windows give 24 hourly pool points. 7d: 7 daily points, 30d: 30. Bands: a few per range.
  - `COV[r]` once per range for the City×ASN table (50 rows/page), and once per search keystroke (debounced) with `q`. At production volume one network has tens of thousands of City×ASN rows (the Shifter mock alone has 32,761 cities and 9,236 ASNs), so 1,000+ pages per network and range. `q` must be answered from the build's compact lists, not a database scan; the server already renders pages on demand and caches 1,024 pages per version. 18 networks × 3 ranges = 54 lists.
  - `RK[r]` for every figure compared across networks (ranks, leaders, "Best in Index", alternatives, value rank): one call for 30d (benchmark, overview, alternatives) plus one per range the dock shows (metric-card ranks, value rank, alternative). 18 rows each, small (about 10 KB).
  - Target meta: today only in `/v1/dataset` (about 9.2 MB, gzip 0.5 MB). Embed it in `NET` or add a small `/v1/targets` (NET-TARGET-META).
  - Palette: `/v1/search-index` once when opened or after 2.5 s idle (today the website's `/proxy-index/search.json`).
  - Total: about 1 + 3 + 3 + 3 + 1 = 11 requests for a full view with every range, or 4–5 for the default 30d view. No polling today; `/v1/version` (5 s cache) can be polled if the page should refresh.
- All-networks data that today needs one request per network (18): availability rank and leader, ASN-spread leader, price rank and leader, peer $/GB on alternative cards, authenticity "Best in Index", per-target ranks. Each is fixed by adding the value (or a precomputed rank) to `/v1/rankings` rows or to `S[r]` / `P.targets[]`.
- What must be precomputed at build for instant answers:
  - Per network, kind and range: ranks `{rank, peers}` for success, availability, hosting-ASN share, value and price (`tileRanks` already covers IPs, countries, regions, cities, ASNs and latency).
  - The per-range alternative pick, the ownership label, per-range target rows with HTTP target ranks, and the under-load latency series.
  - Coverage first pages are kept per list (already in the build and in the dataset file `lists[slug][kind][range].city_asn`). Search pages are rendered on demand.
- Static build option: the dataset file's `pages` already holds `NET` and `CH[r]` for every network, kind and range, plus `/v1/rankings` ×3, `targets[]`, `scopes[]` and the first coverage page per range. An Astro build can render the whole page from one dataset download. Only coverage pages after the first and coverage search need runtime API calls.

## 6. /proxy-index/compare — up to four residential networks side by side: score, every measured metric, per-target success, price and trial

Source files read: src/pages/proxy-index/compare.astro, src/components/proxy-index/compare/compare-data.ts, compare/compare-client.ts, compare/compare-sections.ts, compare/CompareVisual.astro, compare/PickSection.astro, compare/TableSection.astro, compare/TargetsSection.astro, compare/TrySection.astro, src/components/proxy-index/review/review-data.ts, pi-data.ts, pi-network-cards.tsx (NETWORKS, SCALES, rate), pi-network-strip.tsx, page/index-dock.tsx, pi-palette.tsx (search fetch), targets/targets-data.ts, src/data/proxy-index/{home,networks,targets}.json, scripts/proxy-index/export-{home,networks,targets}.mts.

How the page gets data today: everything is imported at build time from the JSON files. `ENTRIES` = residential networks of home.json (`MEASURED`, re-ranked by `score.composite` while `SHOW_DEMO` is on) joined with networks.json (`reviewFor`). Every network and every window (24h, 7d, 30d) is rendered into the HTML; the picker and the dock's range switch only toggle `hidden` / `data-w` (no runtime request). So the page needs data for ALL networks at once, not only the N picked.

API side (verified in api/internal/build/pages.go, profile.go, rankings.go, measured.go): `/v1/networks/{slug}` carries `profile` (the 30d measured blocks: `pool`, `performance`, `pricing`, `trust`, `targets[]`, `overlap`, `browserOverall`, `score`, `products`, `website`) and `stats[24h|7d|30d]` (WindowStats without histories); `ranges[r]` (per-range totals and list counts). `/v1/rankings?range=` is the only all-network per-range answer (no pricing, trust, overlap, p99, uptime or targets). Per-target figures and overlap are 30d only (`measuredBlock` reads `p.Window("30d")`; `overlapPeers` keeps `r.Window == "30d"`).

### Page head, SEO, JSON-LD

| # | Shown on page (element) | Website data field (file → JSON path or TS expression) | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C1 | Title, description, keywords, breadcrumbs, badge, highlights, CTA copy | compare.astro literals via t() | text | none | none | editorial | `noindex` until launch |
| C2 | JSON-LD ItemList: numberOfItems, item names, item URLs | `ENTRIES.length`, `ENTRIES[i].network.name/slug` | count, text | leaderboard order | `/v1/rankings?range=30d → networks[]` (filter `kind == "residential"`, `position` order; `name`, `slug`) | exists | website order is composite-desc re-rank; API `position` is balanced score of the range, same idea |
| C3 | JSON-LD dateModified | `GENERATED_AT` (networks.json `generatedAt`) | ISO date | none | any answer → `generatedAt` (e.g. `/v1/rankings → generatedAt`) | exists | |
| C3a | JSON-LD `name`, `description`, `creator` (`ref(ORG_ID)`), `about` (`/proxy-index#dataset`), `itemListElement[].position` (`i + 1`) | `compare.astro` `jsonLd` literals, `ENTRIES` index | text, int | none | none (position: renumber RK rows after the `kind == "residential"` filter) | editorial | No data beyond C2. |
| C4 | Hero lead "{rows} measured metrics", pill "Metrics" | `ROWS.length` (24, compare-data.ts) | count | none | none | editorial | row count of the page's own table definition |
| C5 | Hero meta "{networks} networks", pill "Networks" | `ENTRIES.length` | count | none | `/v1/rankings?range=30d → networks[]` count where `kind == "residential"` | exists | |
| C6 | Hero meta "{targets} targets" | `TARGETS.length` (targets.json) | count | none | `/v1/dataset` file → count of `targets[]` with `group == null` (not `measuredMarket.targetCount`: it counts every framework target, including the `ai_answers` sources whose `group` is set) | exists-but-different-shape | target list is only in the 9 MB dataset file, no page answer carries it (see CMP-8) |
| C7 | Window title "Head to head, last 30 days" | `DEFAULT_RANGE === '30d'` | text | none | none | editorial | |

### Hero visual (CompareVisual.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C8 | Lead network (#1) vs rotating rival (#2-#4), names (first word), marks | `ENTRIES.slice(0, MAX_PICK)`, `network.name`, `NetworkMark` | text, logo | rival changes every 4 s (CSS, hard-coded `TURN = 4`) | `/v1/rankings?range=30d → networks[0..3].name/slug` | exists | logos are static assets |
| C9 | Index Score per side | `network.score.composite` | 0-100, 1 dp | 30d only | `/v1/rankings?range=30d → networks[].score` | exists | |
| C10 | Success per side | `win(e,'30d').successRate` | %, 1 dp | 30d | `/v1/rankings?range=30d → networks[].successRate` | exists | |
| C11 | Latency p50 per side | `win(e,'30d').latencyP50` | ms / s | 30d | `/v1/rankings?range=30d → networks[].latencyP50` | exists | under-load basis in every-request snapshots |
| C12 | Unique IPs per side | `win(e,'30d').coverage.uniqueIps` | compact | 30d | `/v1/rankings?range=30d → networks[].uniqueIps` | exists | K8 hour-formula estimate for daily groups |
| C13 | DC ASNs per side | `win(e,'30d').dcAsnShare` | % | 30d | `/v1/rankings?range=30d → networks[].datacenterAsnShare` | exists | 30d value; see C33 for other windows |
| C14 | $ / GB per side | `review.pricing.listRate` | $, 2 dp | none | `/v1/networks/{slug} → profile.pricing.pricePerGbAtScale ?? pricePerGb` | exists-but-different-shape | no `listRate` field and not in rankings rows (CMP-6) |
| C15 | Bar split, winner lit, "ahead on {n} of {total}" | derived `row()` share = va/(va+vb), lower-better inverted, clamp 8-92 | %, count | none | derived from C9-C14 | exists | client arithmetic |
| C15a | Duel row with a missing value: "N/A" on that side, 50/50 bar, no winner (also when both values are 0); reduced motion keeps only the first rival; no duel without networks | `row()`: `missing(va) \|\| missing(vb) \|\| va + vb === 0`; `prefers-reduced-motion` CSS; `lead && ...` | text | reduced motion | inputs C9-C14 | exists | Wired to RK without prices (CMP-6), the $ / GB row is 50/50 in every duel. |

### Network strip and dock (shared)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C16 | Strip: "Ranked by Index Score across {n} networks", name, monogram, score, "#rank"/"Unranked" | `MEASURED[]`: `name`, `monogram` (name.slice(0,2)), `score.composite`, `standing.rank` | text, 1 dp | marquee loop (CSS) | `/v1/rankings?range=30d → networks[].name/score/rank` | exists | monogram derived from name |
| C17 | Dock range picker 24h / 7d / 30d | `IndexDock` sets `data-range` on `[data-pi-range-root]` | enum | toggles pre-rendered `data-w` blocks, no fetch | each windowed row below → `?range=` or `stats[r]` | exists | default 30d (`DEFAULT_RANGE`) |
| C18 | Dock search palette (on open) | `fetch('/proxy-index/search.json?locale=')` (pi-palette.tsx) | JSON | picking a network fires `cmp:add` (adds it to the compare picker) | `/v1/search-index` (or `/v1/search?q=`) | exists | website passthrough route |
| C18a | Palette pick of a network with no chip (other kind, not in the field) does nothing, no message | `add()` returns when no `[data-pick]` matches the slug | none | `cmp:add` | `/v1/search-index` rows vs `/v1/rankings` residential rows | editorial | Client rule; the palette lists networks the picker cannot add. |

### 01 Pick (PickSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C19 | Chip per network: mark, "#rank", name, score (0 dp) | `ENTRIES[]`: `standing.rank ?? i+1`, `name`, `score.composite` | text, int | click toggles (max 4, a 5th replaces the last picked, last one cannot be removed); `cmp:add` from search | `/v1/rankings?range=30d → networks[].rank/name/score` | exists | |
| C20 | Default picks, maximum | `DEFAULT_PICK` = first 4 of ENTRIES, `MAX_PICK = 4` | slugs, count | none | `/v1/rankings?range=30d → networks[0..3]` | exists | 4 is hard-coded UI rule (editorial) |
| C21 | Picked counter "{n} of {max} picked" | DOM count (compare-client.ts) | count | live | none | editorial | |
| C21a | Pick state: DOM only (`aria-pressed` chips, `hidden` columns and cards); no URL parameter, no localStorage; a reload or a shared link shows the default top four | `initCompare()` `order` | slugs | none | none | editorial | A shareable comparison is a website change (CMP-13); no API change, every network is pre-rendered. |
| C22 | Card: name link, grade badge (tone from letter), "#rank of peers" / "Unranked" | `score.grade`, `gradeTone()`, `standing.rank`, `standing.peers ?? ENTRIES.length` | text | none | `/v1/rankings?range=30d → networks[].grade/rank/peers` | exists | API `grade` is null for a network with no score in the range (`rankingRow` sets `Grade = nil`); `gradeTone(grade: string)` calls `startsWith` and throws on null: map null to "N/A" first (CMP-14). |
| C23 | Card: Index Score | `network.score.composite` | 1 dp | none | `/v1/rankings?range=30d → networks[].score` (or `/v1/networks/{slug} → profile.score.composite`) | exists | |
| C24 | Card: 8 sub-score labels | `DATA.scoreLabels` (home.json) | text | fixed key order | none | editorial | keys match `breakdown` keys; labels are copy |
| C25 | Card: 8 sub-score values and bars, tone >=75 green, >=50 blue, >=30 amber, else red | `network.breakdown[key]` | 0-100, 0 dp | none | `/v1/rankings?range=30d → networks[].breakdown.<key>` | exists | null → N/A, grey bar |
| C26 | Card: success rate per window | `win(e,r).successRate` | %, 1 dp | per window (`data-w`) | `/v1/rankings?range=r → networks[].successRate` | exists | |
| C27 | Card: success sparkline per window, hover point value + label, stroke green/red by delta sign | `windowOf(n.windows,r).successSpark.values` (up to 22 points; 7 for 7d, 6 for the measured network) + `.labels`; `win(e,r).successDeltaPts` | % per bucket, label text | per window; hover tooltip (index-spark-client) | `/v1/rankings?range=r → networks[].trend` (≤26 values, no times) / `/v1/networks/{slug}/chart?range=r → success[] {t,v}`; `networks[].successDeltaPts` | exists-but-different-shape | trend has no per-point time/label; chart answer needs 1 request per network per range (CMP-7) |
| C28 | Empty state "Not enough points yet" | fewer than 2 non-null points | text | none | derived from C27 | exists | |

### 02 Side by side (TableSection.astro, ROWS in compare-data.ts)

Columns are every network (hidden unless picked); each windowed cell is rendered three times (`data-w`). Best value per row is re-marked client-side among picked networks (`rescore`, ties all marked, needs ≥2 values, `better` = high/low/none).

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C29 | Header: network name link + mark | `network.name/slug` | text | column hidden unless picked | `/v1/rankings → networks[].name/slug` | exists | |
| C30 | Group headers, row labels, row hints, "Read the exact rules" text | `ROWS[].group/label/hint`, TableSection copy | text | none | none | editorial | |
| C31 | Row Index Score (best = high) | `network.score.composite` | 1 dp | fixed (not windowed) | `/v1/rankings?range=30d → networks[].score` | exists | |
| C32 | Row Rank (best = low), "Unranked" | `network.standing.rank` | `#n` | fixed | `/v1/rankings?range=30d → networks[].rank` | exists | |
| C33 | Row Success rate | `win(e,r).successRate` | %, 1 dp | per window | `/v1/rankings?range=r → networks[].successRate` | exists | The row hint "Request-weighted across all targets" does not describe the API value: `successRate` is the mean of the time-slot success rates over network rows, browser targets excluded (copy fix, CMP-18; as HOME-42). |
| C34 | Row Latency p50 | `win(e,r).latencyP50` | ms / s | per window | `/v1/rankings?range=r → networks[].latencyP50` | exists | |
| C35 | Row Latency p99 | `win(e,r).latencyP99` | ms / s | per window | `/v1/networks/{slug} → stats[r].latencyP99` | exists | not in rankings rows: 1 request per network (CMP-11) |
| C36 | Row Challenge rate (request-weighted over the window's targets) | `challengeRate(win(e,r))` = Σ captchaRate×requests / Σ requests over `targets[]` | %, 2 dp | per window | `/v1/networks/{slug} → profile.performance.captchaRate` (30d `metrics.challenge_rate`) | exists-but-different-shape | 30d only; no per-range challenge rate (CMP-1). Window metric excludes browser targets, the website mean includes every target row |
| C37 | Row Requests | `win(e,r).requests` | compact | per window | `/v1/networks/{slug} → stats[r].requests` | exists | not in rankings |
| C38 | Row Availability | `win(e,r).uptimePct` | %, 2 dp | per window | `/v1/networks/{slug} → stats[r].uptimePct` | exists | not in rankings |
| C39 | Row Weighted degradation | `win(e,r).downtimeMinutes` → `duration()` "16 min" / "2h 14m" | minutes | per window | `/v1/networks/{slug} → stats[r].downtimeMinutes` | exists | not in rankings |
| C40 | Row Unique IPs | `win(e,r).coverage.uniqueIps` | compact | per window | `/v1/rankings?range=r → networks[].uniqueIps` (= `stats[r].uniqueIps`) | exists | |
| C41 | Row Countries | `win(e,r).coverage.countries` (window `pool.countries`) | count | per window | `/v1/networks/{slug} → ranges[r].exitCountries` / `ranges[r].lists.country`; `profile.pool.countries` is 30d | exists-but-different-shape | rankings `countries` is the 30d `pool.countries` repeated in every range (code: `Countries: p.Pool.Countries`) (CMP-2) |
| C42 | Row Regions | `win(e,r).coverage.regions` | count | per window | `/v1/networks/{slug} → ranges[r].lists.region` (list row count); `profile.pool.regions` 30d | exists-but-different-shape | list row count, not `pool.regions` of the window (CMP-2) |
| C43 | Row Cities | `win(e,r).coverage.cities` | count | per window | `/v1/networks/{slug} → ranges[r].lists.city`; `profile.pool.cities` 30d | exists-but-different-shape | as C42 |
| C44 | Row ASNs | `win(e,r).coverage.asns` | count | per window | `/v1/networks/{slug} → ranges[r].lists.asn`; `profile.pool.asns` 30d | exists-but-different-shape | as C42 |
| C45 | Row DC ASN share | `win(e,r).dcAsnShare` (window `pool.datacenter_asn_share`) | %, 2 dp | per window | `/v1/networks/{slug} → profile.trust.datacenterAsnShare`; `/v1/rankings → networks[].datacenterAsnShare` | exists-but-different-shape | both are the 30d value, also in the 24h/7d rankings (CMP-3) |
| C46 | Row On a public blocklist | `review.trust.blacklistHitRate` | % | fixed | `/v1/networks/{slug} → profile.trust.blacklistHitRate` | exists | catalog `site.trust_signals`; N/A for measured until filled |
| C47 | Row Pool overlap (shared share) | `win(e,r).ownership.shared` | % | per window | `/v1/networks/{slug} → profile.overlap.sharedShare` | exists-but-different-shape | 30d only; snapshot has `windows[r].overlap.shared_share` (CMP-4) |
| C48 | Row Likely upstream (name or "None found") | `upstreamOf()`: first `ownership.overlaps[]` among shown networks with `shareOfOurs >= 0.5` | text | per window | `/v1/networks/{slug} → profile.overlap.likelyUpstream` | missing | always null (declared N/A, `build/measured_sections.go declaredNA`); derivable from `overlap.overlaps[].shareOfOurs` (CMP-5) |
| C49 | Row Sticky sessions held | `review.trust.stickinessHoldRate` | % | fixed | `/v1/networks/{slug} → profile.trust.stickinessHoldRate` | exists | catalog only; framework does not measure it |
| C50 | Row Leak checks passed "p / n" | `review.trust.leakChecks[].passed` | count | fixed | `/v1/networks/{slug} → profile.trust.leakChecks[]` | exists | catalog only (always N/A for measured today) |
| C51 | Row Peer consent disclosed Yes/No | `review.consentDisclosed` | bool | fixed | `/v1/networks/{slug} → profile.trust.consentDisclosed` | exists | catalog. The website type is `boolean` (export `Boolean(p.trust.consentDisclosed)`), so unknown already reads "No" and its raw 0 enters the best mark (`better: high`); with the API's null show "N/A" and leave it out of the mark (CMP-14). |
| C52 | Row $ / GB (lowest list rate) | `review.pricing.listRate` (fallback home.json `windows[].listRate`) | $, 2 dp | fixed | `/v1/networks/{slug} → profile.pricing.pricePerGbAtScale ?? pricePerGb` | exists-but-different-shape | API applies `listRate()` internally but serves no `listRate` field; not in rankings (CMP-6) |
| C53 | Row Cost per 1,000 successes | `win(e,r).costPer1kSuccess` | $, 2 dp | per window | `/v1/rankings?range=r → networks[].costPer1kSuccess` (= `stats[r].costPer1kSuccess`) | exists | wire bytes × list rate ÷ request success rate |
| C54 | Row Trial text | `review.pricing.trial ?? (slug === 'shifter' ? 'Free trial' : 'N/A')` | text | fixed | `/v1/networks/{slug} → profile.pricing.trial` | exists | catalog; Shifter fallback "Free trial" hard-coded in compare-data.ts |
| C55 | Row Trial CTA link, label "Start trial" / "Contact sales" / "Visit site" | `trialCta()`: `review.productUrl ?? review.website`; label from trial text | URL, text | opens external | `/v1/networks/{slug} → profile.products[measured].url ?? profile.products[0].url ?? profile.website` | exists | catalog. The label comes from the trial text (`none` → "Visit site", "sales" → "Contact sales", else "Start trial"), so with the API's usual null trial every measured network shows "Start trial" beside a "Not published" chip (C74a): tie the label to `P.pricing.hasTrial` (NET-TRIAL-BOOL, W3) or fall back to "Visit site" (CMP-12). Shifter URL `https://shifter.io/order` hard-coded in review-data.ts |
| C56 | Best-value highlight (blue) | `bestOf()` / `rescore()` over `data-raw` | none | re-marked on every pick, per window | derived | exists | client |
| C57 | NEW row Browser overall score (next to Success rate) | not used yet | 1-10 + "#rank of rankedOf", status | per network; engine picker or headline engine | `/v1/networks/{slug} → profile.browserOverall.<engine>.success.{score,rank,rankedOf,status,targets,weights}` | exists-but-different-shape | 30d only, 1 request per network; not in rankings (CMP-10). Show `status` text when not `ranked` |

### 03 By target (TargetsSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C58 | Target rows, easiest first: label | targets.json `targets[].label`, sorted by `difficulty` | text | sort by difficulty, ties in dataset order | `/v1/dataset` file → `targets[].label/difficulty` (cards: `group == null`) | exists-but-different-shape | only in the dataset file (CMP-8); mock list is old (has `ticketing_queue`, lacks `ai_answers`) |
| C59 | Short line per target ("HTTP", "Marketplace"...) | `TARGET_COPY[id].short` | text | none | none | editorial | |
| C60 | Difficulty 5-step bar, tone, "Difficulty {n} of 5" | `target.difficulty`, `difficultyTone()` | 1-5 | none | `/v1/dataset` file → `targets[].difficulty` | exists-but-different-shape | as C58 (catalog `site.targets`) |
| C61 | Cell success rate per network per target | `win(e,r).targets[target].successRate ?? cell(slug,id).successRate` | %, 1 dp | per window | `/v1/networks/{slug} → profile.targets[id].successRate` | exists-but-different-shape | 30d only, no 24h/7d per-target (CMP-9); 1 request per network; on a browser target the rate is `engine`'s (most completed tries, ties to Camoufox), never engines added together. |
| C62 | Cell heat bar (fill from 70%), band colour 97/93/88/80 | `heatSolid(rate)`, fill `(rate-0.7)/0.3` | colour | none | derived from C61 | editorial | fixed thresholds |
| C63 | Best on target (blue) | max of picked networks' rates | none | re-marked on pick | derived | exists | |
| C64 | Cell N/A | `missing(rate)` | text | none | target row with `requests == 0` / null rate | exists | |
| C65 | NEW row under each browser-target cell: success score | not used yet | 1-10, "#rank of rankedOf", status label | per network; per window wanted | `/v1/networks/{slug} → profile.targets[id].scores.success.{score,rank,rankedOf,status}` (engine `profile.targets[id].engine`; per engine `engines.<e>.scores.success`) | exists-but-different-shape | 30d only (CMP-10); HTTP targets have no `scores`. Scores never compare Chrome with Camoufox |
| C66 | NEW (if challenge/block rates are added per target) challenge and block scores | not used | 1-10 | as C65 | `profile.targets[id].scores.challenge` / `.block` next to `captchaRate` / `blockRate`; per engine `engines.<e>.scores.challenge` / `.block` next to `engines.<e>.outcomes.challenge` / `.block` | exists-but-different-shape | as C65 |
| C67 | Legend bands, sample "97.2%" "Best on this target" | literals | text | none | none | editorial | "97.2%" is a hard-coded example, not data |
| C67a | Engine of a browser-target cell and the per-engine rates (not shown yet) | not used | `chrome` / `camoufox`; fraction | per network; per window wanted | `/v1/networks/{slug} → profile.targets[id].engine`; `profile.targets[id].engines.<engine>.successes` ÷ `.requests`, `.outcomes.*`, `.pageLoadP50` / `P90` | exists-but-different-shape | 30d only (CMP-9). Two networks can have a different `engine` on one target, so C63's best mark can set Chrome against Camoufox (CMP-17). |
| C67b | Unknown difficulty and copy: null `difficulty` draws 0 pips and "Difficulty 0 of 5"; a target without `TARGET_COPY` (the API's `ai_answers`) shows its label as the short line | `target.difficulty ?? 0`, `copyFor()` fallback | text | none | `/v1/dataset` file → `targets[].difficulty` (null for a browser target without a `site.targets` row, TGT-8) | exists-but-different-shape | Show "N/A" for a null difficulty (CMP-14); add copy for `ai_answers`. |
| C68 | Footer note and link to targets page | copy | text | none | none | editorial | |

### 04 Try them (TrySection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| C69 | Card name link, "#rank of peers" | `network.name`, `standing.rank/peers` | text | card hidden unless picked | `/v1/rankings?range=30d → networks[].name/rank/peers` | exists | |
| C70 | Score ring (composite, 0 dp) + grade, ring colour by grade | `score.composite`, `score.grade`, `gradeTone` | 0-100 | ring animates on reveal | `/v1/rankings?range=30d → networks[].score/grade` | exists | Null grade: as C22 (CMP-14); a null score draws an empty ring (`composite ?? 0`). |
| C71 | Price "From $x / GB" or "Price", "up to $y at entry volume" / "One rate at every volume" | `priceOf()`: lo = `pricePerGbAtScale ?? listRate ?? pricePerGb`, hi = `pricePerGb` | $, 2 dp | none | `/v1/networks/{slug} → profile.pricing.pricePerGbAtScale` / `pricePerGb` (or `profile.products[].priceLabel` "$0.75–$1.00/GB") | exists | |
| C72 | Price range bar on one log scale for the whole field, "Cheaper/Dearer" | min/max over every ENTRY's lo/hi, ×0.8 / ×1.15 | position % | none | as C71 for all networks | exists-but-different-shape | needs every network's pricing: 18 requests today (CMP-6/CMP-11) |
| C73 | Trial chip: label and tone (free green, card/$ amber, sales blue, none grey; Shifter "Free trial") | `trialOf()` on `pricing.trial` text | text, tone | none | `/v1/networks/{slug} → profile.pricing.trial` | exists | tone is a text heuristic in the page; Shifter fallback hard-coded |
| C74 | CTA button / "No public link" | `trialCta(e)` | URL | external | as C55 | exists | |
| C74a | Trial chip fallbacks: no trial text → "Not published" (grey, info icon), Shifter → "Free trial" (green); `none` → "No trial" (grey); icon star / tag / chat / info by tone | `trialOf()` | text, icon | none | `/v1/networks/{slug} → profile.pricing.trial` (null → "Not published") | exists | Catalog; null for measured networks until `site.pricing.trial` is filled (CMP-15). |
| C74b | Price N/A: no price → "N/A", no "/ GB", no scale bar; the sub-line still reads "One rate at every volume" | `priceOf()`, `ranged` | text | none | as C71 | exists | Website copy fix: hide the sub-line when the price is N/A (CMP-14). |
| C75 | Section lead, methodology note | copy | text | none | none | editorial | |

### Further details (website rules, no API gap)

- Pick counter variants (`data-label` / `data-label-max`); cards centre when fewer than 4 are picked; no best value with one pick.
- Without JavaScript the best values come from `DEFAULT_PICK`.
- Section copy and CTA (as O7c): `COMPARE_SECTIONS` labels, titles, leads and Next cards (editorial).
- The target order is sorted by the export (`export-targets.mts`, null difficulty last), not by the page (C58).
- C61's expression is `targets.find(row => row.target === id)` with a fallback to `targets.json`, which has no window.
- `compact()` on the counts rows; clamps `Math.max(4, …)` and a 2% minimum for sub-score bars; `hi < lo` price ranges; `difficultyTone(null)`; a palette pick scrolls to `#pick`.

### Gaps summary

1. CMP-1: per-window challenge (and block) rate — `/v1/networks/{slug} → stats[r].captchaRate`, `stats[r].blockRate` and the same in `/v1/rankings` rows — snapshot `windows[r].metrics.challenge_rate` / `block_rate`.
2. CMP-2: per-window coverage counts — `stats[r].countries/regions/cities/asns` (and rankings `countries` per range, not the 30d value) — snapshot `windows[r].pool.countries/regions/cities/asns`.
3. CMP-3: per-window DC ASN share — `stats[r].datacenterAsnShare`, rankings `datacenterAsnShare` per range — snapshot `windows[r].pool.datacenter_asn_share`.
4. CMP-4: per-window overlap shares — `stats[r].overlap.{exclusiveShare,sharedShare}` — snapshot `windows[r].overlap` (the store already builds pair rows for every window, the build keeps only 30d).
5. CMP-5: likely upstream — compute `profile.overlap.likelyUpstream` {slug, name, shareOfOurs} (top pair by `shareOfOurs` ≥ 0.5 whose pool is larger) — snapshot `overlap.peers` (`shared_ips`, `ips_ours`, `ips_theirs`).
6. CMP-6: list rate in one place — `profile.pricing.listRate` and rankings rows `listRate`, `pricePerGb`, `pricePerGbAtScale`, `priceLabel` — snapshot `products[].pricing.lowest_per_gb/entry_per_gb`, `site.pricing`.
7. CMP-7: sparkline times — rankings `trend` as `{t, v}` (or a parallel `trendT`) so labels can be drawn without the chart answer — `WindowStats.success`.
8. CMP-8: target metadata in a page answer — `targets[]` {id, label, category, difficulty, engine, group} in `/v1/rankings` or a new `/v1/targets` — `site.targets` / `public.targets` (today dataset file only).
9. CMP-9: per-window per-target figures — `stats[r].targets[]` (successRate, captchaRate, blockRate, requests) or `/v1/targets?range=` matrix (network × target) — snapshot `windows[r].targets[id].metrics`.
10. CMP-10: per-window browser scores and overall — `stats[r].targets[id].scores`, `stats[r].browserOverall` (and the matrix of CMP-9) — snapshot `windows[r].targets[id].browser.scores`, `windows[r].browser_overall`.
11. CMP-11: one all-network compare answer — `/v1/compare?range=` (or extended `/v1/rankings` rows) with every side-by-side row per network: p99, requests, uptimePct, downtimeMinutes, coverage counts, dcAsnShare, overlap shares, likelyUpstream, trust catalog fields, trial, product URL, pricing — the blocks above (precomputed per build).
12. CMP-12: structured trial kind — `profile.pricing.trialKind` (`free`, `card`, `paid`, `sales`, `none`) and the same on rankings rows, replacing the text matching of C55 and C73 — `site.pricing.trial`.
13. CMP-13: picks live only in the DOM; a reload or shared link resets to the top four (C21a) — website change.
14. CMP-14: API nulls the page does not handle: `grade` null crashes `gradeTone`, `consentDisclosed` null reads "No" and counts in the best mark, `difficulty` null reads "Difficulty 0 of 5", no price still says "One rate at every volume" (C22, C51, C67b, C70, C74b) — website change.
15. CMP-15: hard-coded Shifter trial "Free trial" (`compare-data.ts`, `TrySection.astro trialOf`) and order URL (`review-data.ts SHIFTER_TRIAL`) — catalog data.
16. CMP-16: Index Score, grade, rank and sub-scores are drawn fixed (30d) while `/v1/rankings?range=` serves them per range — website or owner decision.
17. CMP-17: best-on-target mark across engines — a browser cell's rate is its network's main `engine`, which can differ between networks (C61, C63, C67a) — website rule on W4 data.
18. CMP-18: Success rate row hint says "Request-weighted across all targets"; the API value is time-weighted over network rows (C33) — copy fix.

### Performance notes

- Per page view mapped to today's API: `/v1/rankings?range=24h|7d|30d` (3 requests, 18 rows each, a few KB) plus `/v1/networks/{slug}?kind=residential` for every network (18 requests, each up to 200 KB because it also carries `days[]`, the top-50 `countries` per range and records the page does not use), plus the dataset file (about 9.2 MB) for target metadata. That is about 22 requests and several MB for one page. Comparing N networks would need N network requests only if the page fetched on pick. Today it pre-renders all of them, so it needs all 18.
- Data actually drawn at production volume: 18 networks × 24 rows × 3 windows (about 1,300 values), 18 × 3 sparklines × up to 22 points, and the target matrix 18 networks × 8 card targets × 3 windows × (rate + score) (about 860 cells). That is 60 to 100 KB of JSON if it is precomputed as one answer (CMP-11 + CMP-9/10).
- Must be precomputed per build: per-window coverage, DC share, challenge rate, overlap shares, likelyUpstream, per-window per-target rates and scores, and list rates. Everything else is already in WindowStats and rankings.
- Website note (not an API issue): with every window and every network rendered into the HTML, the page is about 1.1 MB with 4 networks today. With 18 networks it would grow about 4.5 times.

## 7. /proxy-index/overlap — how much of each residential pool is shared with other networks, exclusivity, resale signatures and market duplication

Source files read: src/pages/proxy-index/overlap.astro, src/components/proxy-index/overlap/overlap-data.ts, overlap/overlap-client.ts, overlap/overlap-sections.ts, overlap/OverlapVisual.astro, overlap/MarketSection.astro, overlap/ExclusivitySection.astro, overlap/PairsSection.astro, overlap/SegmentBar.astro, overlap/MethodSection.astro, compare/compare-data.ts (ENTRIES, win), review/review-data.ts, pi-data.ts, pi-network-strip.tsx, page/index-dock.tsx, src/data/proxy-index/networks.json (`windows.30d.ownership`, `coverage.uniqueIps`), scripts/proxy-index/export-networks.mts (ownership from old-site `ownershipFor(p, range)` and `measurement.windows[range].overlap.pairs` or `p.overlap.overlaps`, capped at 4 pairs). `overlap-data.ts` also exports `overlapTone` (red ≥ 0.5, amber ≥ 0.25, blue ≥ 0.05), used by no component; `export-networks.mts` does not export `shareOfTheirs` (`pairsOf` takes the other network's own `shareOfOurs` for the pair, else `sharedIps / poolOf(other)`); scripts/proxy-index/export-home.mts (network list, order, `monogram`).

The page has no range switch (no `data-pi-range-root`). Every figure is the 30d window (`DEFAULT_RANGE`), which matches the API: overlap is mapped from 30d only. Every section needs the overlap rows of ALL networks at once. Today every overlap figure on the page is demo data: 3 of the 4 networks shown (`nimbus-residential`, `orbitnode`, `kestrel-net`) are `source: "demo"`, kept by `SHOW_DEMO = true`; the only measured network (`shifter`) has `exclusive: 1`, `shared: 0` and no pairs, and `comparable()` never pairs it with a demo one. The demo pairs are inconsistent with their pools (Orbitnode ↔ Kestrel: 52,895 / 305,450 = 17.3%, stored `shareOfOurs` 0.71) (OVL-8).

API side (verified in api/internal/build/measured.go `overlapPeers`, measured_sections.go `overlapSummary`/`declaredNA`, build.go `fleetTotals`/`scanFleet`, store/network.go `overlapRows`): per network `profile.overlap` = `exclusiveShare`, `sharedShare` (30d `windows.30d.overlap`), `overlaps[]` {slug, name, sharedIps, shareOfOurs, shareOfTheirs, requestsBothSides: null}, sorted by `sharedIps` descending (every pair, not capped). `likelyUpstream` is always null (declared N/A). Dataset `totals`: `distinctIpsAcrossFleet = max(1, Σ pool.uniqueIps24h − Σ pair sharedIps (30d))` (NaN with no fleet), `fleetDuplicationShare = 1 − distinct / Σ uniqueIps24h`, `likelyResellers = count(likelyUpstream != null)` (so always 0), over every provider of every kind, served only in the dataset file. No answer serves the 18×18 pair matrix. It is spread over 18 `/v1/networks/{slug}` answers, or sits whole in the 9.2 MB dataset file (`providers[].overlap.overlaps[]`).

### Page head, SEO, hero

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| O1 | Title, description, keywords, breadcrumbs, badge, highlights, window legend, CTA copy | overlap.astro literals | text | none | none | editorial | |
| O2 | JSON-LD Dataset dateModified | `GENERATED_AT` | ISO date | none | any answer → `generatedAt` | exists | |
| O3 | JSON-LD variableMeasured names | literals | text | none | none | editorial | |
| O4 | Hero meta "{n} networks, each matched against every other" | `ENTRIES.length` | count | none | `/v1/rankings?range=30d → networks[]` where `kind == "residential"` | exists | |
| O5 | Hero pill "Duplicated" | `FLEET.duplicated` = (sum − distinct) / sum | %, 1 dp | 30d, residential shown networks | `/v1/dataset` file → `totals.fleetDuplicationShare` | exists-but-different-shape | API basis differs: sum of 24h pools of every kind minus 30d shared IPs (OVL-2, OVL-7) |
| O6 | Hero visual (two pools, amber shared cells) | OverlapVisual.astro constants | animation | none | none | editorial | illustration, not data |
| O7 | Network strip and dock search | as C16, C18 (no range on this page) | | | as C16, C18 | exists | |
| O7a | Canonical `https://shifter.io/proxy-index/overlap`, OG image `/og/proxy-index/overlap.png` (fixed card, see section 2 item 10), `noindex` | `overlap.astro` constants | URL, PNG | none | none | editorial | `noindex` until launch. |
| O7b | Hero lead link "inventory you can already reach through the other" → `/proxy-index/alternatives`; CTA anchors "See every pool" → `#pairs`, "How it's matched" → `#method` | `overlap.astro` `leadParts`, `primary`, `secondary` | links | none | none | editorial | |
| O7c | Step bar: 4 steps (The market, Exclusivity, By network, How it works) with their questions; each section's label, title, highlight, lead and "Next" card | `OVERLAP_SECTIONS` (`overlap-sections.ts`) through `IndexStepBar` and `IndexSection` | text, anchors `#market`, `#exclusivity`, `#pairs`, `#method` | scroll spy | none | editorial | |

### 01 The market (MarketSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| O8 | "Sum of pools" | `FLEET.sum` = Σ `poolOf(e)` = Σ `windows.30d.coverage.uniqueIps` | compact, counts up on reveal | residential shown networks | `/v1/rankings?range=30d → Σ networks[kind=residential].uniqueIps` | exists-but-different-shape | no served total; `totals.uniqueIps24h` is 24h and every kind (OVL-2). Pool sizes can be K8 estimates while overlap is measured (OVL-6) |
| O9 | "Counted twice" | `FLEET.sum − FLEET.distinct` = Σ pair `sharedIps`, each pair once | compact | as O8 | derived from `/v1/networks/{slug} → profile.overlap.overlaps[].sharedIps` for all 18 | exists-but-different-shape | 18 requests or the dataset file; `totals` has only the distinct figure (OVL-2). Today's export keeps 4 pairs per network (`.slice(0, 4)`), so pairs can be missed and the sum undercounted; the API serves every pair. |
| O10 | "Distinct IPs" | `FLEET.distinct` = max(0, sum − shared) | compact | as O8 | `/v1/dataset` file → `totals.distinctIpsAcrossFleet`; true distinct (every kind) `/v1/networks/all → ranges.30d.uniqueIps` | exists-but-different-shape | page formula under-counts distinct IPs held by 3 or more pools: such an IP is subtracted once per pair (3 times for 3 pools, though it was counted only 2 extra times), so "Counted twice" over-counts it; the result is floored at 0. API total mixes windows and kinds (OVL-2, OVL-7) |
| O11 | Plain sentence with the same figures and share, link to networks | `plainValues` {sum, twice, share, distinct} | text | none | as O8-O10, O5 | exists-but-different-shape | same as O8-O10 |
| O11a | Equation with no pool figures: the three terms show "0", the hero pill "Duplicated" and the sentence's `{share}` show "N/A"; a network with a null pool adds 0 to the sum while its pairs still count | `FLEET.sum` (`poolOf(e) ?? 0`), `FLEET.duplicated = sum ? … : null`, `twice = max(0, sum − distinct)` | compact; % | none | as O8-O10, O5 | editorial | No separate empty state. With the API a null `pool.uniqueIps30d` should be skipped, not added as 0. |
| O12 | Heading "Resale signatures", "{n} of {total} networks have most of their pool inside one larger network" | `FLEET.resellers` = `RESELLERS.length`, `ENTRIES.length` | count | none | `/v1/dataset` file → `totals.likelyResellers` | missing | always 0: `overlap.likelyUpstream` is never computed (OVL-3) |
| O13 | Empty state "No network's pool sits mostly inside a larger one in this run." | `RESELLERS.length === 0` | text | none | as O12 | missing | depends on O12 |
| O14 | Reseller card list, order by top `shareOfOurs` desc | `RESELLERS`: top pair (`pairsOf(e)[0]`) with `shareOfOurs >= 0.5` and `poolOf(other) > poolOf(e)` | list | sorted | `/v1/networks/{slug} → profile.overlap.likelyUpstream` | missing | rule lives in overlap-data.ts (OVL-3) |
| O15 | Card: network name, badge "Resale signature", sentence with both names | `e.network.name`, `top.other.network.name` | text | none | `profile.name`, `profile.overlap.overlaps[0].name` | exists | once O14 selects the card |
| O16 | Card picture: two circles sized by sqrt(pool ratio), offset by share; legend names + pool sizes | `lens(poolOf(e), poolOf(top.other), top.shareOfOurs)`, `compact(poolOf())` | geometry, compact | animates on reveal | `/v1/rankings?range=30d → networks[].uniqueIps`; `profile.overlap.overlaps[0].shareOfOurs` | exists | colours by ENTRIES index (palette editorial) |
| O17 | Card stats "{a} inside {b}" (ours) and reverse (theirs), bars | `top.shareOfOurs`, `top.shareOfTheirs` | %, 1 dp, counts up | none | `/v1/networks/{slug} → profile.overlap.overlaps[].shareOfOurs` / `.shareOfTheirs` | exists | the website derives theirs when absent; the API serves it |
| O18 | Card note "A shared block of {n} addresses", link to true-cost | `top.sharedIps` | compact | none | `profile.overlap.overlaps[].sharedIps` | exists | |
| O18a | Card: reseller name and mark link → `/proxy-index/networks/{slug}`; stat labels "{a} inside {b}" use the first word of each name; a stat bar is empty when its share is missing | `href(e.network.slug)`, `name.split(' ')[0]`, `missing(stat.v) ? []` | link, text | none | `/v1/rankings?range=30d → networks[].slug/name`; `profile.overlap.overlaps[].shareOfOurs/shareOfTheirs` | exists | |

### 02 Exclusivity (ExclusivitySection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| O19 | Row order, most exclusive first, missing last | `BY_EXCLUSIVITY` sort by `ownership.exclusive` | none | sort | derived from `profile.overlap.exclusiveShare` of all networks | exists-but-different-shape | needs all 18 answers (OVL-1) |
| O20 | Network name + mark + sourcing label ("Fully exclusive", "Mostly exclusive", "Mixed sourcing", "Resale signal") | `e.network.name`, `ownership.label` (old-site `ownershipFor`) | text | none | none | missing | no label field; rule from the exclusive share and resale flag (OVL-4) |
| O21 | Pool size | `poolOf(e)` = `coverage.uniqueIps ?? uniqueIps` (30d) | compact | none | `/v1/rankings?range=30d → networks[].uniqueIps` / `profile.pool.uniqueIps30d` | exists | pool may be a K8 estimate, overlap shares are measured (OVL-6) |
| O22 | Exclusive share + bar, tone ≥75% green, ≥50% amber, else red | `ownership.exclusive`, `exclusiveTone()` | %, 1 dp | none | `/v1/networks/{slug} → profile.overlap.exclusiveShare` | exists | zero peers → 100% exclusive (API rule) |
| O23 | Shared share, red when > 40% | `ownership.shared` | %, 1 dp | none | `/v1/networks/{slug} → profile.overlap.sharedShare` | exists | |
| O24 | Largest overlap: other network name link | `pairsOf(e)[0].other.network.name/slug` (sorted by shareOfOurs) | text | none | `profile.overlap.overlaps[0].slug/name` (sorted by sharedIps, which is the same order for one network) | exists | API lists every pair; website export capped 4 |
| O25 | Largest overlap: "{a} of {name}'s pool · {b} of {other}'s" | `top.shareOfOurs`, `top.shareOfTheirs` | % | none | `profile.overlap.overlaps[0].shareOfOurs` / `.shareOfTheirs` | exists | |
| O26 | "None detected" | `pairsOf(e)` empty | text | none | `profile.overlap.overlaps == []` (N/A path `overlap.overlaps`) | exists | |
| O27 | Distinct exits you get | `round(poolOf(e) × ownership.exclusive)` | compact | none | derived `profile.pool.uniqueIps30d × profile.overlap.exclusiveShare` | exists-but-different-shape | multiplies an estimated pool by a measured share; serve `exclusiveIps` from `ips_ours × exclusive_share` (OVL-5) |
| O28 | "Read the exact rules" text | copy | text | none | none | editorial | |
| O28a | Exclusive bar aria label "{share} of {name}'s pool is its own"; table caption "Pool exclusivity per network", 6 column heads; table scrolls horizontally below 900 px | `pct(exclusive)`, `e.network.name`; `t()` | text | none | `profile.overlap.exclusiveShare`, `/v1/rankings → networks[].name` | exists | Caption and heads editorial. |
| O28b | N/A states: Pool size, Exclusive, Shared show "N/A" (`pct`/`compact`); "Distinct exits you get" prints a hard-coded, untranslated "N/A" when pool or exclusive is missing; a missing exclusive gives an empty bar and grey tone | `missing(pool) \|\| missing(exclusive) ? 'N/A'`; `exclusiveTone(null)` = `#6b7590` | text | none | null `profile.pool.uniqueIps30d` / `profile.overlap.exclusiveShare` | editorial | The untranslated "N/A" is a website fix (OVL-10). |

### 03 By network (PairsSection.astro, SegmentBar.astro, overlap-client.ts)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| O29 | Summary "{n} of {total} networks share part of their pool"; with a pick: "{name} shares addresses with {n} networks" / "1 network" / "no addresses" | `SHARING` (rows with ≥1 segment), `ENTRIES.length`; `data-count` = segments | count | changes with the picker | derived from all `profile.overlap.overlaps[]` | exists-but-different-shape | full matrix needed (OVL-1) |
| O30 | Network picker "Show one network", "All networks" with counts | `appearsIn(slug)` = own row + bars containing it | count | filters rows to the network's own bar and every bar it appears in; dims other segments | derived from the matrix | exists-but-different-shape | OVL-1 |
| O31 | Row order, most shared first | `POOLS` sort by `shared` desc | none | sort; picked network's own bar first | `profile.overlap.sharedShare` of all networks | exists-but-different-shape | OVL-1 |
| O32 | Row name link + mark | `row.e.network.name/slug` | text | none | `/v1/rankings → networks[].name/slug` | exists | |
| O33 | Green own segment width + tooltip "Only in {name}", "{share} of {name}'s pool", "{n} addresses" | `row.exclusive`, `round(pool × exclusive)` | %, compact | hover/focus/tap tooltip | `profile.overlap.exclusiveShare`, `profile.pool.uniqueIps30d` | exists-but-different-shape | address count as O27 (OVL-5) |
| O34 | One segment per other network, colour per network, width = shareOfOurs scaled to fit the shared part | `segments[]`: `p.shareOfOurs × scale` (scale = room / Σ shares when Σ > room) | % width | hover lights a run, picker dims | `profile.overlap.overlaps[].shareOfOurs` | exists | scaling is client-side (IPs in 3+ pools) |
| O35 | Segment tooltip: other name, "{share} of {name}'s pool is also in it", "{n} shared addresses" | `seg.share`, `seg.sharedIps` | %, compact | tooltip | `profile.overlap.overlaps[].name/shareOfOurs/sharedIps` | exists | |
| O36 | Exclusive % at row end, tone | `row.exclusive`, `exclusiveTone` | % | none | `profile.overlap.exclusiveShare` | exists | |
| O37 | "Show {n} more networks" (page size 8), empty state | `PAGE = 8`, `data-ov-empty` | count | client paging | none | editorial | client-only paging of a pre-rendered list. The `[data-ov-empty]` paragraph has no text, so an empty picker result would show a blank card (cannot happen today: the picked network's own row always matches) (OVL-9). |
| O38 | Measured vs demo never matched | `comparable()` (`network.source`) | none | filter | none needed: every API provider is `source = "measured"` | exists | |
| O38a | Peers that are not shown networks are dropped from every bar, sum and card (today Kestrel ↔ `helix-mobile`, a mobile network) | `pairsOf`: `bySlug.get(o.slug)` over `ENTRIES` (residential only) | none | filter | none needed: API peers are the other networks of the same kind (`overlap.peers`) | exists | |
| O38b | Bar interactions: picker shown only with more than 2 networks; paging resets to 8 rows on a pick; one shared tooltip follows the pointer per colour run, shows on keyboard focus, pins on tap (second tap clears), hides on scroll unless pinned; own segment drawn only when exclusive > 0; tooltip address line left out when the count is missing | `ENTRIES.length > 2`, `limit = page` on pick, `pinned`, `own: exclusive <= 0 ? []`, `tip.ips = ''` | behaviour | picker, hover, focus, tap, scroll | none | editorial | Client-side, no request. |
| O38c | Bar aria labels: bar "How {name}'s pool splits"; segments "{share} only in {name}" / "{share} also in {other}" | `partsOf(row).tip.label`, `SegmentBar label` | text | none | `profile.overlap.exclusiveShare`, `overlaps[].shareOfOurs`, `name` | exists | |
| O39 | Full pair matrix for all networks (needed by O8-O10, O12-O14, O19, O29-O31) | `ownership(e).overlaps` of every ENTRY | 18 × 17 pairs | none | `/v1/networks/{slug} → profile.overlap.overlaps[]` × 18, or `/v1/dataset` file → `providers[].overlap.overlaps[]` | exists-but-different-shape | no single answer (OVL-1) |

### 04 How it works (MethodSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| O40 | Steps, limits, illustration IP addresses, "Overlap over time" mini chart, guide links | MethodSection.astro literals | text, SVG | animations | none | editorial | IPs and chart points are illustration, not data |

### Further details (website rules, no API gap)

- The pool bar's unfilled remainder is drawn; segments at or below 0 are dropped but still counted in `FLEET`; segments have a 0.8% minimum width.
- The sort uses `shared ?? 1 − exclusive` while the table shows N/A.
- The resale card's `lens()` fallbacks and its fixed sentence "The relationship runs one way".
- The copy "read the same in every window" contradicts per-window pairs (W5 serves them per range).

### Gaps summary

1. OVL-1: full overlap matrix in one answer — `/v1/overlap?kind=residential` → `networks[]` {slug, name, poolIps, exclusiveShare, sharedShare, exclusiveIps, label, likelyUpstream, peers[] {slug, sharedIps, shareOfOurs, shareOfTheirs}} — snapshot `windows.30d.overlap` + `overlap.peers`.
2. OVL-2: market totals per kind — `/v1/overlap → market` {sumOfPools, sharedIps, distinctIps, duplicatedShare, networks} — the same peers. A true distinct count comes from the kind's fleet entry, if the calculator publishes one per kind (unverified; `/v1/networks/all` has only the every-kind fleet `*`).
3. OVL-3: resale detection — `overlap.likelyUpstream` per network, `market.likelyResellers` and `totals.likelyResellers` (now always 0) — overlap peers with the rule shareOfOurs ≥ 0.5 and a larger other pool.
4. OVL-4: sourcing label — `overlap.label` (enum: fully_exclusive, mostly_exclusive, mixed, resale) — exclusive share thresholds + OVL-3. The thresholds must be defined; the old site's `ownershipFor` is not in this repo. Today's exported pairs: exclusive 1 → "Fully exclusive", 0.9101 → "Mostly exclusive", 0.7292 → "Mixed sourcing", 0.11 → "Resale signal" (bounds between them unknown).
5. OVL-5: exclusive address count — `overlap.exclusiveIps` — `ips_ours × exclusive_share` from the measured overlap basis.
6. OVL-6: pool basis for overlap — `overlap.poolIps` (the measured `ips_ours` the shares were computed on) — `overlap.peers[].ips_ours`. Today pool sizes may be K8 estimates while shares are measured.
7. OVL-7: `totals.distinctIpsAcrossFleet` / `fleetDuplicationShare` mix 24h pool sums with 30d shared IPs, across every kind — compute per kind and per window on one basis — `windows[r].pool.unique_ips` + `overlap.peers`.
8. OVL-8: demo overlap — every overlap figure shown today (sum, counted twice, duplicated, the one resale card, the pool bars) comes from 3 demo networks; the measured network has no pairs; demo `shareOfOurs` values do not equal `sharedIps / pool` — no API change: read only measured networks from the API (`shareOfOurs = sharedIps / ips_ours` by construction) and turn `SHOW_DEMO` off — `networks.json` `source`, `pi-data.ts` `SHOW_DEMO`.
9. OVL-9: blank empty state — `[data-ov-empty]` in `PairsSection.astro` has no text (O37) — no API change: add the copy — website only.
10. OVL-10: untranslated "N/A" — "Distinct exits you get" prints a literal "N/A" instead of the shared formatter (O28b) — no API change — website only.

### Performance notes

- Per page view mapped to today's API: 18 × `/v1/networks/{slug}` (profile.overlap + pool) or the whole dataset file, plus `/v1/rankings?range=30d` for order and pool sizes. One page needs the whole matrix.
- Volume: 18 networks × up to 17 peers = at most 306 pair rows (153 unique pairs), about 20-30 KB as one precomputed `/v1/overlap` answer. With per-window overlap (the source exists for 24h/7d/30d) it is 3 times that. The page does not switch windows today.
- Must be precomputed per build: the market totals, the reseller flags, labels and exclusive IP counts. They are cheap (one pass over the peers), so a request should only look them up.

## 8. /proxy-index/true-cost — every residential network priced per 1,000 successful requests, the retry tax and the bill at volume

Source files read: src/pages/proxy-index/true-cost.astro, src/components/proxy-index/cost/cost-data.ts, cost/cost-sections.ts, cost/cost-tone.ts, cost/CostVisual.astro, cost/CostSection.astro, cost/RetrySection.astro, cost/VolumeSection.astro, compare/compare-data.ts (ENTRIES, win, gbRange), pi-network-cards.tsx (SCALES.price/success, rate), targets/targets-data.ts (TARGETS, copyFor), review/review-data.ts, pi-data.ts, pi-network-strip.tsx, page/index-dock.tsx, src/data/proxy-index/{networks,targets,home}.json, scripts/proxy-index/export-networks.mts (`windowStats(p, range).costPer1kSuccess`, `pricing.*` from the provider).

The page is one analysis per window. Every table is rendered for 24h, 7d and 30d (`data-w`). The cost table is also rendered once per target tab (`data-tgt-body`), so it covers 3 × (1 + targets) bodies × all networks. No runtime fetch; the tabs and the range toggle are client-side.

API side (verified in api/internal/build/measured.go `wireCostPer1k`/`listRate`/`costPer1k`, window.go `costBoard`, fleet.go `residentialBills`, measured_market.go): `stats[r].costPer1kSuccess` / rankings `costPer1kSuccess` = (bytes_up + bytes_down) / completed × 1000 / 1024³ × lowest list rate ÷ `request_success_rate`, 4 dp. Rankings `valueRank` = position on the range's cost board over every network of every kind. `measuredMarket.views[r].bills` {all[] {provider, bill = cost × 1000}, best, worst, spreadX} covers residential networks only and is served only in the dataset file. `measuredMarket.views[r].cheapest/dearest` are picked by LIST RATE, not by cost. Pricing is in `profile.pricing` per network: `pricePerGb`, `pricePerGbAtScale`, `minCommit`, `trial`, `plans[]` (+ `catalog`), `costPer1kSuccess` (30d), `avgPayloadKb`, `costBasis`, `headlinePriceRank`, `valueRank`.

### Page head, SEO, hero

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T1 | Title, description, keywords, breadcrumbs, badge, lead, highlights, legend, window title "1,000 billed requests", CTA copy | true-cost.astro literals | text | none | none | editorial | "fails a third of the time" is copy |
| T2 | JSON-LD Dataset dateModified, variableMeasured | `GENERATED_AT`, literals | ISO date, text | none | any answer → `generatedAt` | exists | |
| T3 | Hero meta "{n} networks" | `ENTRIES.length` | count | none | `/v1/rankings?range=30d → networks[]` where `kind == "residential"` | exists | |
| T4 | Hero pill "Price gap" | `VIEWS[30d].spreadX` = dearest / cheapest cost per 1k | `x`, 1 dp | 30d | `/v1/dataset` file → `measuredMarket.views[30d].bills.spreadX` (or `totals.residentialSpreadX`, the 30d profile cost) | exists-but-different-shape | dataset file only; derivable from `/v1/rankings?range=30d` costs (TC-1). Fixed to `DEFAULT_RANGE` (30d), does not follow the dock; the pill is dropped (`pills = []`) when `spreadX` is null. `totals.residentialSpreadX` uses `pricing.costPer1kSuccess`, where a catalog value wins over the measured one. |
| T5 | Hero visual: grid of billed requests, one third lost | CostVisual.astro `LOST_SHARE = 1/3` | animation | none | none | editorial | fixed illustration, not a network |
| T6 | Dock range switch and search | as C17, C18 | | toggles `data-w` | as C17, C18 | exists | |
| T6a | Step bar (Cost per result, Retry tax, At volume: labels, questions, anchors `#cost`, `#retry-tax`, `#volume`) and each section's label, title, lead and "Next" card | `COST_SECTIONS` (cost-sections.ts) | text | scroll spy (shared, section 2 item 12) | none | editorial | One list feeds the step bar, the three section headings and the Next cards. |
| T6b | Network strip under the hero: "Ranked by Index Score across {n} networks"; per network monogram, name, score and "#rank" / "Unranked" | `NetworkStrip` (`pi-network-strip.tsx`) over `MEASURED` | 1 dp; rank | none | `/v1/rankings?range=30d → networks[].{name, score, rank}` (residential) | exists | As C16 and section 2 row 12.2 (30d score, rank caveat HOME-19). |

### 01 Cost per result: tiles (CostSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T7 | Subheading "Rotating residential, per 1,000 successful requests" | literal | text | none | none | editorial | |
| T8 | Tile Cheapest: cost + network name ("Not collected" when none) | `view.cheapestCost`, `view.cheapest.network.name` (min `costPer1kSuccess`) | $, 2 dp, counts up | per window | `/v1/dataset` file → `measuredMarket.views[r].bills.best.{provider.name, bill/1000}`; or `/v1/rankings?range=r → networks[valueRank == 1]` | exists-but-different-shape | bills are in the dataset file only; rankings `valueRank` mixes every kind (TC-1, TC-6). Do not use `totals.cheapestCostPer1kSuccess` / `dearestCostPer1kSuccess`: every kind, 30d only, catalog cost wins. |
| T9 | Tile Median + "Middle of {n} priced networks" | `view.medianCost`, `view.priced.length` | $, 2 dp; count | per window | none (derive from `/v1/rankings?range=r → networks[].costPer1kSuccess`) | missing | no median and no priced count served (TC-1) |
| T10 | Tile Most expensive: cost + name | `view.dearestCost`, `view.dearest` | $, 2 dp | per window | `/v1/dataset` file → `measuredMarket.views[r].bills.worst` | exists-but-different-shape | TC-1 |
| T11 | Tile Price gap | `view.spreadX` | `x`, 1 dp | per window | `/v1/dataset` file → `measuredMarket.views[r].bills.spreadX` | exists-but-different-shape | TC-1 |

### 01 Cost per result: table with target tabs (CostSection.astro, cost-data.ts `targetRows`)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T12 | Tabs "All targets" + one per target (short label) | `TARGETS[]`, `copyFor(x).short` | text | tab switches `data-tgt-body` and the footer note | `/v1/dataset` file → `targets[]` (ids, cards `group == null`); short label editorial | exists-but-different-shape | CMP-8. Order: targets.json order (the export sorts by `difficulty`, easiest first); today 8 tabs + All. The API's card targets are also 8, but `ai_answers` replaces the mock's `ticketing_queue`; `copyFor` then falls back to the target `label` (no short label), see section 3 row 2.6. |
| T13 | Rank "#n" (gold/silver/bronze podium for 1-3), "·" when unpriced | row index after sort by cost, missing last | `#n` | per window and per tab | All targets: `/v1/rankings?range=r → networks[].valueRank` | exists-but-different-shape | `valueRank` ranks every kind together; per target tab not served (TC-6, TC-8). Ties keep `ENTRIES` order (leaderboard, `score.composite` desc; stable sort). A target tab's rank is over the T21 estimate. |
| T14 | Network name link + mark | `e.network.name/slug` | text | none | `/v1/rankings → networks[].name/slug` | exists | |
| T15 | Rate card "$lo to $hi / GB" (tooltip "From the rate at volume to the entry tier"); tone on fixed price scale (≤$2 green, ≤$5 amber, else red) | `gbRange(e)`: `pricePerGbAtScale`, `pricePerGb`, fallback `listRate`; `rate(pricePerGbAtScale ?? listRate, SCALES.price)` | $, 2 dp | none (not windowed) | `/v1/networks/{slug} → profile.pricing.pricePerGbAtScale` / `.pricePerGb`; or `profile.products[].priceLabel` | exists-but-different-shape | 18 requests (or `/v1/dataset → providers[].pricing`); not in rankings rows (CMP-6, TC-5). `review.pricing.listRate` falls back to `home.json windows[30d].listRate` (`review-data.ts`), so Shifter, whose `networks.json` pricing is null, shows "$0.75 / GB" in green (as C52). |
| T16 | Success, All targets; tone ≥90% green, ≥75% amber | `successOf(e,r)` = `win(e,r).successRate`; `SCALES.success` | %, 1 dp | per window | `/v1/rankings?range=r → networks[].successRate` | exists | time-weighted success; cost uses the request success rate (TC-7) |
| T17 | Success, one target tab | `win(e,r).targets[id].successRate` | %, 1 dp | per window × target | `/v1/networks/{slug} → profile.targets[id].successRate` | exists-but-different-shape | 30d only (CMP-9) |
| T18 | NEW next to T17 for browser targets: success score | not used yet | 1-10, "#rank of rankedOf", status | per window wanted | `/v1/networks/{slug} → profile.targets[id].scores.success.{score,rank,rankedOf,status}` (engine `targets[id].engine`) | exists-but-different-shape | 30d only (CMP-10) |
| T19 | NEW next to T16: browser overall score | not used yet | 1-10 | per network | `/v1/networks/{slug} → profile.browserOverall.<engine>.success.score` | exists-but-different-shape | 30d only, not in rankings (CMP-10). Window success excludes browser targets, so the two figures measure different traffic |
| T20 | $ / 1k successes, All targets; tone by thirds of the window's field ("Not collected" when null) | `costOf(e,r)` = `win(e,r).costPer1kSuccess`; `costTone()` | $, 2 dp | per window | `/v1/rankings?range=r → networks[].costPer1kSuccess` | exists | Tone: position between the tab's own cheapest and dearest known cost (thirds). Mock: the only measured network (`shifter`) has null pricing and cost in networks.json, so today it reads "Not collected", rank "·" and 1M "N/A" and sorts last, while its rate card shows the home file's list rate (T15) and its retry row a real rate of about $0.78 (T28); the API prices it from the snapshot list rates (`lowest_per_gb` / `entry_per_gb`). |
| T21 | $ / 1k successes, one target tab | `row.costPer1kSuccess` if present, else `cost × overall success / target success` (estimate) | $, 2 dp | per window × target | none | missing | API has no per-target cost; the estimate assumes equal bytes per request on every target, which does not hold for browser page loads (TC-3). It also mixes success bases: the window cost divides by `request_success_rate`, the estimate multiplies by the time-slot `successRate` and divides by the target's rate. Tone is by thirds of that tab's estimates. |
| T22 | 1M successes bill | `cost × 1000` → `bill()` | $, 0 or 2 dp | per window × tab | `/v1/dataset` file → `measuredMarket.views[r].bills.all[].bill`; or derived from T20 | exists | Derived; "N/A" when the cost is null. On a target tab it is the T21 estimate × 1000, so `missing` there (TC-3). |
| T23 | Footer note: "All targets" vs "Estimated ... until the test run publishes a measured cost per target" | literals | text | per tab | none | editorial | the second note goes away once TC-3 exists |
| T24 | Legend thirds, "Read the exact rules" (4 rules), guide link | literals | text | none | none | editorial | |
| T24a | (optional, not drawn) for browser-target tabs: challenge and block scores, per-engine scores | not used yet (the page shows no challenge or block rate) | 1-10, "#rank of rankedOf", status | per window wanted | `/v1/networks/{slug} → profile.targets[id].scores.challenge` / `.block`, `profile.targets[id].engines.<engine>.scores.*` | exists-but-different-shape | 30d only, one network per request (CMP-10); null on `generic_http` / `generic_https`. Only needed if the tab gains challenge or block figures; T18 covers the success score. |

### 02 Retry tax (RetrySection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T25 | Row order, worst retry tax first, missing last | `view.retry` sorted by `tax` desc | none | per window | derived from `/v1/rankings?range=r → networks[].successRate` | exists | One request per range. Ties keep `ENTRIES` order (stable sort). |
| T26 | Delivered/failed bar (success vs 1 − success), tooltip "{failed} of every 20 requests fail" | `row.success`, `round((1 − success) × 20)` | %, count | per window | `/v1/rankings?range=r → networks[].successRate` | exists | derived |
| T27 | Retry tax "+x%", colour >50% red, >20% amber, else green | `tax = 1/success − 1` | %, 1 dp | per window | derived from `successRate` | exists-but-different-shape | page uses the time-weighted `successRate`, the API cost uses `request_success_rate`, so the tax and the cost table disagree; serve `stats[r].requestSuccessRate` (TC-7) |
| T28 | Real rate "$x" and bar listed vs added by retries | `effective = listRate / success`, `listShare = listRate / effective` | $, 2 dp | per window | `profile.pricing.pricePerGbAtScale ?? pricePerGb` ÷ `successRate` | exists-but-different-shape | list rate needs 18 network requests (CMP-6); success basis as T27. With the home-file list-rate fallback (T15), Shifter's real rate reads about $0.78 although its cost is "Not collected". |
| T29 | Column headers, legend, "Attempts is 1 divided by the success rate" note | literals | text | none | none | editorial | |
| T29a | Empty states: no success (or 0) → both bar parts 0 wide, tax "N/A" in grey, row sorted last; no list rate → real rate "N/A", listed share 100% | `row.success`, `row.tax`, `row.effective`, `listRateOf(e)` (`ok = success > 0`) | N/A | per window | inputs as T26 to T28 | exists-but-different-shape | As T27, T28. |
| T29b | (not drawn) attempts per result `1 / success` | `RetryRow.attempts` (computed, never rendered; described in the T29 note) | ratio | per window | derived from `/v1/rankings?range=r → networks[].successRate` | exists-but-different-shape | Success basis as T27 (TC-7). |
| T30 | Six "Read the rate card twice" cards (e.g. "At 80% success ... $1.00 → $1.25", per-IP, expiry) | `cards[]` literals | text, animation | none | none | editorial | example numbers are hard-coded illustration |

### 03 At volume (VolumeSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T31 | Column volumes 10k, 100k, 1M, 10M successes | `VOLUMES` (cost-data.ts) | count | none | none | editorial | hard-coded buyer volumes |
| T32 | Row order, cheapest cost first, missing last | `view.byValue` | none | per window | `/v1/rankings?range=r → networks[].costPer1kSuccess` sorted ascending, residential only, null last (or `valueRank`, which mixes every kind) | exists | `valueRank` mixes every kind (TC-6). Ties keep `ENTRIES` order. |
| T33 | Network name link + mark | `e.network.name/slug` | text | none | `/v1/rankings → networks[].name/slug` | exists | |
| T34 | Bill per volume, tone by thirds | `bill(cost × v / 1000)`, `costTone(cost, view)` | $ | per window | derived from `/v1/rankings?range=r → networks[].costPer1kSuccess` | exists | "N/A" in grey when the cost is null. |
| T35 | Footer "Usage only. Below a network's minimum commitment..." | literal | text | none | `/v1/networks/{slug} → profile.pricing.minCommit` could be shown | editorial | `minCommit` exists (catalog) but is not drawn |
| T36 | "Pay less for the same results" card | literals | text, animation | none | none | editorial | |

### Pricing fields the API has that this page does not draw

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| T37 | (not drawn) plans and published offers | `review.pricing.plans[]` (network page only) | $, GB | none | `/v1/networks/{slug} → profile.pricing.plans[]` {name, price, gb, ips, note, recommended, discountPct, catalog} | exists | catalog `site.pricing_plans`; `catalog` holds tier, unit rate, billing period, order URL, check date |
| T38 | (not drawn) cost inputs and catalog ranks | none | | none | `profile.pricing.avgPayloadKb`, `costBasis`, `headlinePriceRank`, `valueRank`, `minCommit`, `costPer1kSuccess` (30d) | exists | catalog `site.pricing.*`; without a catalog value `avgPayloadKb` is the 30d wire KB per request (the bytes `costPer1kSuccess` reads) and `headlinePriceRank` the list rate's rank in the kind, cheapest first (MEASURED-FIELDS-1); `pricing.valueRank` is catalog, the per-window value rank is rankings `valueRank`. `pricing.costPer1kSuccess` is the catalog `site.pricing.cost_per_1k_success` when set, else the 30d wire cost, else `avgPayloadKb` × price ÷ success, so it can differ from `stats.30d.costPer1kSuccess`; use `stats` / rankings on this page. |
| T39 | (not drawn) pricing model; the page assumes per-GB for every network | none | text (`per_gb`) | none | `/v1/networks/{slug} → profile.pricing.model` | exists | The snapshot model wins when the snapshot has a price, else the catalog's. |
| T40 | (not drawn) wire traffic per 1,000 requests (bytes up + down ÷ completed), the main input of every cost on the page | none (inside the old site's `windowStats`) | GB per 1k requests | per window | none as a number: only inside the text `profile.pricing.costBasis` ("x.xxx GB of wire traffic per 1,000 requests…", 30d, 3 dp); `stats[r].bandwidthMb` is the window's wire bytes ÷ 10⁶ (BANDWIDTH-PER-NETWORK-1), not per 1k requests | missing | TC-9 |

### Further details (website rules, no API gap)

- The "All targets" footer says "request-weighted" over a time-weighted success rate (as CMP-18).
- Retry tax: tooltip text when success is missing; columns hidden below `md`; legend colours.
- Canonical URL, OG image `/og/proxy-index/true-cost.png`, `noindex`, hero links and meta line, and the "Price gap" caption with its N/A state (T4) are page constants as O7a and O7b (editorial).

### Gaps summary

1. TC-1: per-window cost summary of the residential field — `/v1/rankings?range=` → `cost` {cheapest {slug, name, costPer1kSuccess}, median, dearest, spreadX, priced}, or serve `measuredMarket.views[r].bills` in a page answer — `WindowStats.costPer1kSuccess` (today dataset file only, and no median).
2. TC-2: hero spread uses TC-1 for 30d — same field — same source.
3. TC-3: measured cost per target — `stats[r].targets[id].costPer1kSuccess` (or in the `/v1/targets?range=` matrix) — `wireCostPer1k(listRate, windows[r].targets[id].metrics)` (bytes_up/bytes_down per target; whether browser targets publish bytes is unverified).
4. TC-4: per-window per-target success rate — see CMP-9 — `windows[r].targets[id].metrics.success_rate`.
5. TC-5: pricing in the all-network list — rankings rows `listRate`, `pricePerGb`, `pricePerGbAtScale`, `priceLabel` (CMP-6) — snapshot `products[].pricing`, `site.pricing`.
6. TC-6: value rank within the kind — rankings `valueRank` among networks of the same kind (the page lists residential only) — `costBoard` per kind.
7. TC-7: one success basis for cost and retry tax — `stats[r].requestSuccessRate` (and in rankings rows) — `windows[r].metrics.request_success_rate`. Closest served today: `/v1/networks/{slug}?kind= → ranges.<r>.successRate` (successes ÷ requests, summed over the network's product groups of the kind, one request per network), which can differ from the headline group's `request_success_rate` the cost uses.
8. TC-8: per-target value rank for the target tabs — `stats[r].targets[id].valueRank` (or matrix rank) — computed from TC-3.
9. TC-9: wire traffic per 1,000 requests per window, the input of every cost on the page (row T40) — `stats[r].wireGbPer1k` (and on rankings rows) — snapshot `windows[r].metrics.bytes_up + bytes_down` ÷ `completed` (today only inside `pricing.costBasis` text, 30d; `stats[r].bandwidthMb` is null for measured windows).

### Performance notes

- Per page view mapped to today's API: `/v1/rankings?range=24h|7d|30d` (3 requests: success, cost, value rank for every network). Pricing needs 18 × `/v1/networks/{slug}`, target metadata needs the dataset file, and the tiles' bills need the dataset file (`measuredMarket`). The per-target tabs cannot be served for 24h/7d at all.
- Volume: 18 networks × 3 windows × (1 + 8 card targets) table bodies (27 tbodies), so about 490 rows (success, cost, rank each), plus 18 × 3 retry rows and 18 × 3 × 4 bills. That is under 100 KB of JSON if it is precomputed as rankings rows with pricing (TC-5) plus a per-window target matrix with cost (TC-3, CMP-9).
- Must be precomputed per build: per-target wire cost and its rank, the residential cost summary (min, median, max, spread, priced count) per window, and the list rate. A request then serves stored answers only.

## 9. /proxy-index/live-tape — one feed of every test event across all networks: metrics, activity, per-network rail, filterable live feed polled every 15 s, plus the public JSON feed

Source files read (website root): `src/pages/proxy-index/live-tape.astro`, `src/pages/proxy-index/live-tape.json.ts`,
`src/components/proxy-index/live-tape/{tape-data.ts, use-tape-stream.ts, tape-board.tsx, tape-controls.tsx, tape-rail.tsx, tape-metrics.tsx, tape-stats.ts, tape-row.tsx, tape-number.tsx, tape-activity.tsx, tape-heatmap.tsx, TapeVisual.astro, KindsSection.astro, KindVisual.astro, FeedSection.astro}`,
`src/components/proxy-index/pi-tape.tsx`, `src/components/proxy-index/pi-network-strip.tsx`, `src/components/proxy-index/pi-palette.tsx` (tape parts),
`src/components/proxy-index/pi-data.ts` (`ago`, `successTone`, `DEFAULT_RANGE`, `RANGES`, `SHOW_DEMO`), `src/components/proxy-index/review/review-chart.tsx` (`bucketTime`),
`src/data/proxy-index/home.json` (`generatedAt`, `networks[].monogram`, `tape[]`), `src/data/proxy-index/networks.json` (`generatedAt`, `networks.<slug>.tape[]`),
`scripts/proxy-index/export-home.mts` (tape, monogram), `scripts/proxy-index/export-networks.mts` (per-network tape).
API read: `api/README.md`, `api/FIELDS.md` (tape, TapeEvent, dataset file), `api/internal/store/events.go`, `api/internal/store/load.go` (EventWindow/NotableWindow),
`api/internal/build/events.go`, `api/internal/build/tape.go`, `api/internal/build/build_steps.go` (tape step, details), `api/internal/build/profile.go` (RecordCap),
`api/internal/model/model.go` (TapeEvent), `api/internal/model/dataset.go`, `api/internal/model/model_provider.go` (FrameworkLink),
`api/internal/server/server.go` (ParseEventFilter, serveEvents), `api/internal/server/reuse.go`, `api/internal/server/render.go`;
event producers `src/proxy_index/detect.py`, `src/proxy_index/orchestrator/{events.py, batch_events.py, admin/writers.py}`, `configs/methodology.toml [events]`.

Shapes used below:
- **TestEvent** (`/v1/events → events[]`, `store.EventJSON`): `id` (int), `at` (ISO, µs, `+00:00`), `kind` (framework kind: `pulse_finished`, `pulse_down`, `pulse_recovered`, `success_drop`, `latency_spike`, `block_spike`, `latency_rise`, `down`, `recovered`, `node_offline`, `no_node`, `census_started|finished|retry|skipped`, `daily_test_started|finished`, `estimate_hour_released`, `new_hosting_asn`, `ip_overlap`), `category` (census|pulse|incident|audit), `product` (`"<frameworkProvider>/<product>"`), `provider` (framework provider id, not the site slug), `country`, `region`, `target`, `signal` (-1/0/1), `severity`, `data` (raw JSON: `requests`, `successes`, `success_rate`, `p50`, `baseline`, `value`, `unique_ips`, …). No title, detail, value text, site slug or display name. Response is `{events}` only.
- **TapeEvent** (`Dataset.tape[]`, in the dataset file via `/v1/dataset`; per network `/v1/networks/{slug} → tape[]`, max 20): `id` (`"event-<n>"` for measured rows, the catalog `tape_id` for editorial rows), `kind` (= category: census|pulse|incident|audit, editorial rows any), `providerSlug`, `providerName`, `product` (proxy kind), `title`, `detail`, `signal`, `value`, `agoSeconds` (vs snapshot `generatedAt`), `at` (measured rows; null for editorial although `site.tape_events.occurred_at` exists), `source` (always `measured`, editorial rows too; `is_demo` rows are never served), `na`. 200 measured rows (Prioritize: up to 100 non-routine first) then editorial `site.tape_events`. Built only on a full build: an events-only refresh does not update it (README "Cache lifetime").
- Website **TapeEvent** (`tape-data.ts`): `id` (`"t<N>"`, rank by time, re-numbered every export), `kind` (pulse|request|census|incident|audit|rotation|overlap|pricing), `network{slug,name,monogram}`, `title`, `detail`, `value`, `signal`, `at`, `source`.

### 1. Head, SEO, JSON-LD

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `<title>`, description, keywords, canonical, OG image URL, breadcrumbs, `noindex` | `live-tape.astro` literals via `t()` | text | — | — | editorial | Description claims "pulses, censuses, incidents and exit audits"; production replaces census with daily tests (`detect.py` CATEGORIES). |
| 2 | JSON-LD `Dataset.dateModified` | `TAPE_UPDATED_AT` = max(`home.generatedAt`, `networks.generatedAt`) | ISO time | — | `/v1/events?limit=2 → events[0].at` (newest event; `limit=1` is prioritized and returns the newest non-routine event, `NotableQuota(1)` = 1), or `/v1/dataset → generatedAt` | exists | For a live feed the newest event's `at` is the right meaning; the dataset's `generatedAt` lags up to one data period. |
| 3 | JSON-LD `distribution.contentUrl` | literal `/proxy-index/live-tape.json` | URL | — | website route (proxy of `/v1/events`) | editorial | `/v1/events` answers with `Access-Control-Allow-Origin: *` like every API answer (`server.go` `write`), so the public feed URL could point at the API directly; the website route reshapes rows (EV-TEXT, EV-CURSOR). |
| 3a | JSON-LD constants: `@id` `{pageUrl}#tape`, `name` "Proxy Index live tape", `description` (= meta description), `url`, `isAccessibleForFree` true, `creator`/`publisher` `ref(ORG_ID)`, `isPartOf` `{/proxy-index}#dataset`, `distribution.encodingFormat` `application/json`; WebPage `mainEntity` → `#tape` | `live-tape.astro` `jsonLd`, `webPage` | text, ids | — | — | editorial | `contentUrl` is always built with locale `en`; `description` repeats "pulses, censuses, incidents and exit audits" (#1). |

### 2. Hero (IndexHero + TapeVisual + NetworkStrip)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 4 | Badge, title, highlight, lead, highlights chips, buttons ("Watch the tape", "Open the JSON feed"), window title | literals | text | anchors `#tape`, `/proxy-index/live-tape.json` | — | editorial | — |
| 5 | Meta "{events} events from {networks} networks on the tape" — events | `TAPE.length` (every row on record after merge; 74 in mock) | integer | none (all time) | none | missing | No count of events. Proposed `/v1/events/stats?range= → totals.events` from `public.test_events` (COUNT). "All on record" is meaningless at production volume (millions); use the 24h count. |
| 6 | Meta — networks | `TAPE_NETWORKS.length` (networks with ≥1 row) | integer | — | `/v1/networks → networks[]` filtered to `kind == "residential"`, length (the list covers every kind; the page counts residential `MEASURED`) (tracked); reporting count none | exists-but-different-shape | Tracked networks exist (18); "networks with events" needs `stats.totals.reporting`. |
| 7 | Pill "Events" | `TAPE.length` | integer | — | as #5 | missing | Same value as #5. |
| 8 | Pill "Networks" | `TAPE_NETWORKS.length` | integer | — | as #6 | exists-but-different-shape | Same as #6. |
| 9 | TapeVisual rows (9 newest, looped) | `TAPE.slice(0, 9)` | list | newest first | `Dataset.tape[0..8]`; first 9 of `/v1/events?limit=18` (or more) → `events[]` | exists-but-different-shape | `limit=9` is prioritized: up to 5 notable rows, then the 4 newest, so it is not the 9 newest; the k newest rows are all in the answer only when `limit` ≥ 2k. `Dataset.tape` has the text but is the last full build's copy; `/v1/events` is current but has no title/value text (gap EV-TEXT). |
| 10 | TapeVisual monogram tile | `e.network.monogram` ← `home.networks[].monogram` (export: `name.slice(0,2).toUpperCase()`) | 2 letters | — | none; derive from `Dataset.tape[].providerName` | exists-but-different-shape | API has no `monogram`; client derivation from name is what the export did. |
| 11 | TapeVisual kind chip label + colour | `kindLabel(e.kind)`, `KINDS[kind].chip` | label | 8 website kinds | `Dataset.tape[].kind` (4 categories); `/v1/events → category` | exists-but-different-shape | API never emits `request`, `rotation`, `overlap`, `pricing` (demo / editorial only). Website must map on `category` (or `kind` for sub-labels). |
| 12 | TapeVisual title | `e.title` | text | — | `Dataset.tape[].title` | exists | Not on `/v1/events` (EV-TEXT). |
| 13 | TapeVisual value + colour (green >0, rose <0, grey 0) | `e.value`, `e.signal` | text, sign | — | `Dataset.tape[].value`, `.signal`; `/v1/events → signal` | exists | Signal meaning differs per kind, see #47. |
| 14 | Empty state "No events recorded yet." | `TAPE.length === 0` | — | — | `Dataset.tape` = `[]` / `events` = `[]` | exists | — |
| 15 | NetworkStrip "Ranked by Index Score across {n} networks" | `MEASURED.length` (pi-data) | integer | — | `/v1/networks → networks[]` filtered to `kind == "residential"`, length (the list covers every kind; the page counts residential `MEASURED`) | exists | Shared component (mapped on the Overview too). |
| 16 | NetworkStrip per network: monogram, name, `fixed(score.composite)`, `#rank` / "Unranked" | `MEASURED[]` | 0-100, rank | rank order | `/v1/networks → name, score, rank` (rank order) | exists | Monogram as #10. Score is the Index Score, not a browser target score. |

### 3. Step bar and section 01 header

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 17 | Step bar (3 stops + questions), section label/title/lead, Next cards | `stops` in `live-tape.astro` | text | scroll spy | — | editorial | — |
| 17a | CTA "See the results on your own traffic" + description "The tape shows how every network answers our test runs…" (page-specific `CTASection` props); footer shared | `live-tape.astro` `ctaData` via `t()` | text | — | — | editorial | Shared chrome in section 2, item 9. |

### 4. TimeDock and command palette (TapeBoard)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 18 | Range toggle 24h / 7d / 30d (default 30d) | `range` → `inWindow(stream.rows, range)`: rows with `at` > newest `at` − range | window | applies to metrics, activity, rail, feed | `/v1/events?since=` only | missing | Window ends at the newest event, not now. `/v1/events` caps at 500 rows (~6-10 min at production volume), so no client can hold 24h/7d/30d of rows. Needs server aggregates (EV-STATS) plus, for the feed, `since`+`limit` paging. |
| 19 | Palette: pick a network → follow it in the feed | `CommandPalette onNetwork(slug)` over `MEASURED` | slug | — | `/v1/networks → slug, name` | exists | Following then filters by slug (#45). |
| 20 | Palette: pick a range | `onRange` | window | — | as #18 | missing | Same as #18. |
| 21 | Palette footprint "{networks} networks · {targets} targets" | `MEASURED.length`, `DATA.targets` | integers | — | `/v1/networks` length; `Dataset.targets.length` (dataset file) | exists-but-different-shape | Target count only in the 9.2 MB dataset file; shared palette, mapped on other pages. |
| 21a | Palette "Jump to" list: Live tape (`#tape`), Reading the tape (`#kinds`), Take the feed (`#feed`) | hard-coded in `pi-palette.tsx` (`page === "tape"`) | text | shown only when the anchor exists | none | editorial | Section 2 row 11.7. |

### 5. Metric cards (tape-metrics.tsx, tape-stats.ts `totals`, `bucketize`)

Sparklines use `WINDOWS[range].bars / 2` buckets: 24h → 24 × 1 h, 7d → 28 × 6 h, 30d → 30 × 24 h; the window ends at the newest event + 1 ms; labels `HH:MM` (24h) or `d MMM` (7d/30d), UTC until mounted then visitor zone.

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 22 | "Events" figure | `totals(rows).events` = rows in window | integer, rolling digits | range | none | missing | `stats.totals.events` (COUNT of `test_events` in window, all networks). |
| 23 | "Events" caption "In the window, every network" | literal | text | — | — | editorial | — |
| 24 | "Events" sparkline + tooltip "{n} events" | `bucketize(...).total` | count per bucket | range → 24/28/30 points | none | missing | `stats.buckets[].total` at sparkline resolution. |
| 25 | "Pass rate" figure (N/A when no tests) | `passed / tests`; tests = rows of group `requests` (kind pulse/request); passed = `signal > 0` | %, 1 decimal | range | `/v1/networks/all → ranges.<range>.successRate` (`requests`, `successes`; `stats` is `{}` on the all-networks answer); per network `/v1/rankings?range= → networks[].successRate` | exists-but-different-shape | Wrong source on the API side: in `/v1/events` a `pulse_finished` exists only when a pulse moved (≥5 pp, ≥1.5× p50, or blocks appeared/vanished) and its `signal` is the direction of the move, not pass/fail (mock JSON shows "Pulse in UM" 100% with signal −1). Use fleet `successes/requests` per range, or `stats.totals.pulseRequests/pulseSuccesses` summed from `data.requests`/`data.successes`. |
| 26 | "Pass rate" tone | `successTone`: >0.8 pos, >0.6 warn, else neg; null flat | tone | — | derived from #25 | exists-but-different-shape | Follows #25. |
| 27 | "Pass rate" caption "{passed} of {tests} test requests passed" | `sum.passed`, `sum.tests` (row counts) | integers | range | `/v1/networks/all → ranges.<range>.requests`, `.successes` | exists-but-different-shape | API counts requests (≈1.8M/h fleet-wide), the page counts tape rows. Copy must change to requests. |
| 28 | "Pass rate" sparkline (null bucket when no tests), tooltip % | `b.passed / b.tests` per bucket | ratio per bucket | 24/28/30 points | `/v1/networks/all → days[]` (30d, per UTC day: requests, successes, successRate) | exists-but-different-shape | 30d × 1 day matches the 30-point sparkline exactly; 24h hourly and 7d 6-hourly series missing (`stats.buckets[].pulseSuccessRate`, or a fleet chart answer `/v1/networks/all/chart?range=`). |
| 29 | "Incidents" figure | rows with `kind === "incident"` | integer | range | none | missing | `stats.totals.byCategory.incident`. API `incident` category includes `recovered` (signal +1), `node_offline`, `no_node`, `latency_rise`; caption says "latency spikes and success drops" → count `signal < 0` incidents (`stats.totals.incidentsBad`). |
| 30 | "Incidents" tone (neg if >0, else pos) | `sum.incidents` | tone | — | derived from #29 | missing | Follows #29. |
| 31 | "Incidents" caption | literal | text | — | — | editorial | — |
| 32 | "Incidents" sparkline | `b.incidents` | count per bucket | 24/28/30 points | none | missing | `stats.buckets[].byCategory.incident`. |
| 33 | "Reporting" figure | distinct `network.slug` in window | integer | range | none | missing | `stats.totals.reporting` (COUNT DISTINCT provider). |
| 34 | "Reporting" "/{networks}" | `networks.length` = TAPE_NETWORKS (networks with events), so = reporting at 30d | integer | — | `/v1/networks → networks[]` filtered to `kind == "residential"`, length (the list covers every kind; the page counts residential `MEASURED`) | exists-but-different-shape | Denominator should be tracked networks (18), not networks that already reported. |
| 35 | "Reporting" sparkline + tooltip "{n} networks" | `b.networks` (distinct per bucket) | count per bucket | 24/28/30 points | none | missing | `stats.buckets[].networks`; distinct counts cannot be summed, must be computed per bucket. |
| 36 | Card captions/labels/icons | literals | text | — | — | editorial | — |

### 6. Activity heatmap (tape-activity.tsx, tape-heatmap.tsx)

Buckets: `WINDOWS[range].bars`: 24h → 48 × 30 min, 7d → 56 × 3 h, 30d → 60 × 12 h. Rows (groups): requests, census, audit, pool, incident.

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 37 | "Events in the window" figure | sum of `buckets[].total` | integer | range | none | missing | = `stats.totals.events` (#22). |
| 38 | Heatmap cells: count of each group per bucket; colour alpha 0.2 + 0.8·n/peak (peak over all cells), red row for incidents, empty cell dim | `bucketize(rows, ms, bars).byGroup[g]` | count per bucket × 5 rows | range → 48/56/60 columns | none | missing | `stats.heatmap[]` = per bucket `byCategory{pulse,census,audit,incident}`. Group `pool` (rotation, overlap) has no API category: `ip_overlap` is filed under `audit`, rotation has no event. 4 rows from the API, or the API adds a `pool` sub-group for `ip_overlap`. |
| 39 | Hover tooltip: bucket time (`bucketTime`, UTC, step-aware) + count per group | `buckets[i].start`, `byGroup` | time + counts | hover column | none | missing | Same aggregate; bucket `start` needed. |
| 40 | Axis labels "{h}h ago", ⅔, ⅓, "newest" | `axisLabels(range)` | text | range | — | editorial | Derived from the range only. |
| 41 | Card title/subtitle | literals | text | — | — | editorial | — |

### 7. Network rail (tape-rail.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 42 | List of networks, busiest first | `TAPE_NETWORKS` (networks with ≥1 event, sorted by all-time row count) | list | sort by count; scrolls at 384 px | `/v1/networks → slug, name` (all tracked, rank order) | exists-but-different-shape | No per-network event count to sort by; networks with no events are missing from the website list. `stats.networks[]` {slug, events} sorted. |
| 43 | Mark (`NetworkMark` by slug, monogram fallback) + name | `n.slug`, `n.monogram`, `n.name` | — | — | `/v1/networks → slug, name` | exists | Monogram as #10; marks are website assets. |
| 44 | "Last event {age} ago" / "No events yet" | `own[0].at` | age (s/m/h/d) | visitor clock | `/v1/events?product=<frameworkProvider>&limit=2 → events[0].at`, one request per network | exists-but-different-shape | 18 requests per view, and `product` takes the framework provider id (#45). `stats.networks[].lastAt`. `product` alone is still prioritized, so `limit=1` gives the newest notable event, not the last event. |
| 45 | Click → follow / unfollow network | `onSelect(slug)` → feed filter `e.network.slug === network` | slug | toggle | `/v1/events?product=<frameworkProvider>[/<product>]` | exists-but-different-shape | The API filter is by framework provider id; slug→id only in the dataset file (`providers[].framework.provider`). Add `network=<slug>` (or accept the slug in `product`) and `providerSlug` on rows. |
| 46 | Count in window | `own.length` | integer | range | none | missing | `stats.networks[].events`. |
| 47 | "{n} passed" | `own.filter(signal > 0).length` | integer | range | none | missing | `stats.networks[].good`; same signal caveat as #25 (for pulses use success rate, not signal). |
| 48 | Beat strip: last 24 events as cells (blue +, red −, dim 0), newest right, empty cells dim | `own.slice(0, 24).reverse()[].signal` | 24 cells | — | `/v1/events?product=<frameworkProvider>&limit=24 → events[].signal`, one request per network | exists-but-different-shape | 18 requests per view, or `stats.networks[].recent[]` (24 signals). `limit=24` without `kind`/`category` is prioritized: up to 12 notable rows, then the 12 newest, so it is not the network's last 24 events (use `kind=<all kinds>` or W9 `stats.networks[].recent[]`). The true last 24 are almost all `pulse_finished` (routine). |
| 49 | Card title/subtitle | literals | text | — | — | editorial | — |

### 8. Live tape card: status, controls, filters (tape-controls.tsx, tape-board.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 50 | Stream status "Replaying the latest run" / "Live" / "Paused" + dot | `stream.mode` | state | — | — | editorial | Client state. |
| 51 | "{n} events to go" (during replay) | `stream.queued` | integer | — | — | editorial | Client state (replay queue of 10). |
| 52 | "Last event {age} ago · checks every {s}s" (short form on phones) | `stream.rows[0].at`, `POLL_MS/1000` | age; 15 s | visitor clock | `/v1/events → events[0].at` | exists | 15 s is hard-coded in `use-tape-stream.ts`; matches `/v1/events` `max-age=15, s-maxage=15`. |
| 53 | "Waiting for the first event" / "Checks every {s}s" | no rows / not mounted | text | — | `events = []` | exists | — |
| 54 | Pause / Resume, Replay buttons; "{n} new events" pill (pending while paused) | `stream.pending`, `pause`, `resume`, `replay` | integer | click | — | editorial | Client state over polled rows. |
| 55 | Kind tabs All / Requests / Census / Incidents / Audits / Pool | `GROUPS`; `kindMeta(e.kind).group === group` over in-memory rows | tab | client filter | `/v1/events?category=pulse\|census\|incident\|audit` | exists-but-different-shape | Requests ↔ `pulse`; Pool has no category (`ip_overlap` is `audit`, no rotation event). Client-side filtering over ≤300 rows that are mostly `pulse_finished` leaves Incidents/Audits nearly empty at production volume: the tab must query the API with `category=` (and page with `since`). |
| 56 | "Following {network}" chip with clear | `networks.find(slug).name` | name | clears filter | as #45 | exists-but-different-shape | Same mapping gap. |
| 57 | Search box "Search country, target or network…" | substring of `title + detail + value + network.name`, lower-case | text | client filter, live | none | missing | `/v1/events` rejects unknown params (`q` → 400). TestEvent carries `country`, `region`, `target`: add `country=`, `target=`, `network=` filters and optionally `q=` over the rendered text. Source `public.test_events` (+ rendered text). Countries appear in titles only as 2-letter codes (`describe` uses `country`, else `region`), so searching a country name ("Romania") matches nothing today or with the API text; the Pulse picture's `Intl.DisplayNames` name is not searchable. |
| 58 | "{n} results" in the search box | `visible.length` | integer | — | none | missing | Count of matches in window would need `stats` with the same filters; today only the loaded rows. |

### 9. Feed rows (tape-row.tsx)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 59 | Row icon by kind (Radar/Activity/Check/Zap/ShieldCheck/RefreshCw/Layers/Tag) | `e.kind` | icon | — | `Dataset.tape[].kind`; `/v1/events → category` (+ `kind` for finer icons) | exists-but-different-shape | 4 categories vs 8 website kinds (#11). |
| 60 | Row icon tone (pos / neg / info) | `e.signal` | sign | — | `Dataset.tape[].signal`; `/v1/events → signal` | exists | Meaning per kind: `pulse_finished` = direction of change; `recovered`/`pulse_recovered` = +1; `new_hosting_asn` = −1; `ip_overlap` = 0. |
| 61 | Row title | `e.title` | text | — | `Dataset.tape[].title` | exists-but-different-shape | Not on `/v1/events` rows (raw `kind` + `data`); the polled rows would need the website to port `tape.go describe`. Add `title`, `detail`, `value` (rendered once per row in `EventWindow`) to `/v1/events` rows. |
| 62 | Row detail ("Provider · 100 requests · p50 1.42 s") | `e.detail` | text | — | `Dataset.tape[].detail` | exists-but-different-shape | As #61. |
| 63 | Row network mark + name (lg screens) | `e.network.slug`, `.name`, `.monogram` | — | — | `Dataset.tape[].providerSlug`, `.providerName` | exists-but-different-shape | `/v1/events` has only `provider` (framework id). Add `providerSlug`, `providerName`. |
| 64 | Row kind label under the name | `kindLabel(e.kind)` | label | — | as #59 | exists-but-different-shape | — |
| 65 | Row clock time `HH:MM:SS` (visitor zone, after mount) | `e.at` | time | — | `/v1/events → at`; `Dataset.tape[].at` | exists | Editorial `Dataset.tape` rows have `at` null → `generatedAt − agoSeconds`. All events of one `detect()` call share the detection time (one `at` per minute burst). |
| 66 | Row value ("98%", "6,275 IPs", "3.63 s", "Down", "New") | `e.value` | text | — | `Dataset.tape[].value` | exists-but-different-shape | Not on `/v1/events` (EV-TEXT); raw figures are in `data` (`success_rate`, `unique_ips`, `p50`, …). |
| 67 | Row age (`ago(now − at)`) | `e.at` | s/m/h/d | ticks every 1 s | `at` | exists | — |
| 68 | Score next to a pulse's success % when the pulse is on a browser target (owner rule) | not shown yet | 1-10 | — | `/v1/networks/{slug}?kind= → profile.targets[<target>].scores.success.score` (+ `rank`, `rankedOf`, `status`; per engine `engines.<engine>.scores`) | exists-but-different-shape | The row's per-pulse rate is not a per-target aggregate; showing the target score beside it needs one network request per network on screen and a map from `TestEvent.target` (framework target id) to the `profile.targets` entry; the event does not carry the engine. Optional, low value. |
| 69 | Empty state "No events match these filters" | `visible.length === 0` | — | — | — | editorial | — |
| 70 | Footer "{visible} of {total} events shown" | `visible.length`, `rows.length` (in window, in memory) | integers | — | none | missing | `stats.totals.events` with the active filters. |
| 71 | Footer "JSON feed" link | `endpoint` | URL | — | website route | editorial | — |
| 71a | Fallback chip "Event" (grey) for any kind the page does not know (`pricing` has its own chip but group `pricing`) | `kindMeta()` → `FALLBACK_KIND` (group `other`), `kindLabel()` default | label | shown only under "All": no tab for groups `other`/`pricing`, no heatmap row (`HEAT_ROWS`), yet counted in "Events" (#22) and "Events in the window" (#37) | `Dataset.tape[].kind` of editorial rows (`site.tape_events.kind` is free text) | exists | With API data only editorial rows can reach it; "Events in the window" can then exceed the sum of the visible heatmap cells. |

### 10. Runtime: initial rows and polling (tape-data.ts, use-tape-stream.ts)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 72 | Initial rows (SSR, hydrated) | `TAPE` = `home.tape[]` (25 rows, `provider` name) + every `networks.<slug>.tape[]` (≤30, residential), merged on (slug, kind, title, detail, value, minute), `SHOW_DEMO` filter, sorted by `at`, ids `t<N>` | list | newest first | `Dataset.tape[]` (200 measured + editorial) | exists-but-different-shape | One API source makes the merge/dedupe obsolete. But `Dataset.tape` is only rebuilt on a full build (≤1 data period stale) and the dataset file is 9.2 MB (0.5 MB gzip) for 200 rows. Better: SSR from `/v1/events?limit=200` once it carries rendered text (EV-TEXT). |
| 73 | Row time from `agoSeconds` | `dated(generatedAt, agoSeconds)` | ISO | — | `Dataset.tape[].at` | exists | `at` is already absolute for measured rows. |
| 74 | Replay: 10 newest held back, played every 1.4 s unless newest < 10 min old or reduced motion | `REPLAY_COUNT`, `REPLAY_GAP_MS`, `FRESH_MS` | client | — | — | editorial | Hard-coded client constants. |
| 75 | Poll `${endpoint}?since=<cursor>` every 15 s; skipped while tab hidden; immediate poll on visible | `POLL_MS`, `cursor = events[0].id` (`t<N>`) | GET | — | `/v1/events?since=<ISO>&limit=` | exists-but-different-shape | API `since` is an exclusive ISO time only; no id cursor; response has no `cursor`. Because a detect() burst shares one `at`, a page cut by `limit` inside a burst loses the rest of it on the next `since`. Add `after=<id>` (or `(at,id)` cursor) and return `cursor`. |
| 76 | Merge polled rows: prepend, dedupe by `id`, keep 300 | `push()`, `MAX_ROWS` | list | — | `/v1/events → events[]` newest first, `id` | exists | Order matches (`at` desc, id desc). |
| 77 | Catch-up after a hidden tab / long pause | one poll with an old cursor | — | — | `/v1/events` prioritized pick | exists-but-different-shape | Without `kind`/`category` the API returns up to `limit` rows with non-routine first (Prioritize), so routine rows in the middle are silently dropped and the cursor jumps to the newest. Add `more: true` / `next` so a client can page, or skip prioritizing when `since` is set. |
| 78 | Failed poll: silent retry next tick | `catch {}` | — | — | 503 "events unavailable" when the DB fallback fails | exists | — |

### 11. The JSON feed route (live-tape.json.ts) and section 03 (FeedSection.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 79 | `GET /proxy-index/live-tape.json?since=&limit=` → `{updatedAt, cursor, events[]}` | `tapeSince(since, limit)`; `since` = `t<N>` or ISO; `limit` 1-500, default 200 | JSON | since, limit | `/v1/events?since=&limit=` → `{events}` | exists-but-different-shape | API default `limit` 100, max 500; `since` ISO only; no `updatedAt`/`cursor`; rows are TestEvent (no network name/title/value). The route can stay as a thin adapter, or the API adds the fields. The route clamps `limit` to 1-500; `/v1/events` answers 400 outside 1-500, so a public client copying today's habit can fail. The route returns build-time rows only (#85a). |
| 80 | Route headers `Cache-Control: public, max-age=10`, `Access-Control-Allow-Origin: *` | literal | — | — | `/v1/events` `public, max-age=15, s-maxage=15` and `Access-Control-Allow-Origin: *` | exists | The cache lifetime differs (10 s on the route, 15 s on the API); 15 s matches the page's poll. |
| 81 | curl command `…/live-tape.json?since=<TAPE[1].id>` with copy button | `TAPE[1].id` | text | copy | `/v1/events?since=<ISO>` | exists-but-different-shape | Cursor value changes from id to time unless the API adds `after=<id>`. |
| 82 | Sample reply `updatedAt`, `cursor` | `TAPE_UPDATED_AT`, `TAPE[0].id` | strings | — | none | missing | `/v1/events` returns `{events}` only. Add `cursor` (newest id) and `updatedAt` (newest `at` or build time). |
| 83 | Sample event `id`, `kind`, `network`, `title`, `value`, `at` | `TAPE[0]` | strings | — | `/v1/events → id, kind, at`; network/title/value none | exists-but-different-shape | See EV-TEXT and #63. |
| 84 | Steps "newest 200 events", "every 15 seconds" | literals | text | — | API default 100 / max 500; cache 15 s | editorial | Hard-coded in `FeedSection.astro`; "200" is wrong for `/v1/events` default (100). |
| 85 | Pipeline card (Test runs → Scored → Public feed → This page), links, note | literals | text | — | — | editorial | "Every event lands in one JSON feed" holds for `/v1/events`. |
| 85a | What a poll returns today between deploys, and after one | `tapeSince()` over the build-time `TAPE` (imported `home.json` + `networks.json`); ids `t<N>` re-ranked by time on every build | JSON | since | `/v1/events → events[].id` (stable integer `test_events` id) | exists-but-different-shape | Today a poll never returns a new row: the route re-serves the snapshot the page was built with. After a deploy the same event carries another `t<N>`, so a client's old cursor re-delivers seen rows under new ids (dedupe by `id` fails) or skips rows. API ids are stable, so this ends once the route proxies `/v1/events` (EV-FEED-STATIC). |

### 12. Section 02 "Reading the tape" (KindsSection.astro, KindVisual.astro)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 86 | Section texts, lead "Five kinds of event… green passed, red failed, grey neutral", card titles/bodies/links, kind chips | literals, `KINDS[k].chip` | text | — | — | editorial | API has 4 categories; "green passed" is not what `signal` means for `pulse_finished` (#60). |
| 87 | Pulse card points "100 requests per pulse", body "a burst of 100 test requests" | literals | text | — | `/v1/events → data.requests` of `pulse_finished` | editorial | Hard-coded pulse size; real size is per event `data.requests`. |
| 88 | Pulse visual: 100-dot grid with 2 failures at fixed places (98%) | `FAILED = {37, 82}` | grid | CSS loop | `data.requests`, `data.successes` of the newest pulse | editorial | Hard-coded in `KindVisual.astro`; could be drawn from a real pulse. |
| 89 | "Latest pulses" examples: up to 4, one per country, tier-1 first (`TIER_ONE` list), passing first; flag + "Pulse in {country}" (from `/in ([A-Z]{2})/` on title), value (rose if signal<0), "{network} · {detail after name}" | `of('pulse','request')` filtered `kind === 'pulse'` | text | rotate every 4 s | `Dataset.tape[]` category pulse: `title`, `value`, `detail`, `providerName`; `/v1/events → country` | exists-but-different-shape | API rows carry `country` directly (no regex needed) but not the text (EV-TEXT). `pulse` category also holds `pulse_down`/`pulse_recovered` ("Down"/"Recovered" values). TIER_ONE is editorial. |
| 90 | "Latest requests" examples (single target requests) | `kind === 'request'` rows | text | rotate | none | missing | No per-request event exists (pulse runs write no per-run event); only demo rows. No source yet; drop the panel or use per-target pulse rows (`target` set). |
| 91 | Proxy → Target → "200 OK" animation | literal | text | CSS | — | editorial | Hard-coded. |
| 92 | Census visual: "Distinct exit IPs" value | `of('census')` with `/IPs/` in value, `[0].value` | "N IPs" | — | `Dataset.tape[]` census rows `value` (`census_finished`, `daily_test_finished`, `estimate_hour_released` → "N IPs") | exists-but-different-shape | `Dataset.tape[].kind` is only the category, so the page's `/IPs/` filter takes the first census-category row: `census_finished`, `daily_test_finished` and `estimate_hour_released` ("Hourly update for HH:00 UTC", value "N IPs") all pass, and the hourly row is the most frequent. Picking a finished census or daily test needs the raw kind (EV-RAWKIND). Production writes daily tests, not censuses. |
| 93 | Incident visual: "baseline {value}" (regex `baseline ([\d.,]+\s?m?s)` on detail) + spike value | `of('incident')` with `/baseline/` in detail, `[0]` | ms/s | — | `Dataset.tape[]` incident rows: `detail` "baseline 1.23 s" (latency_spike/latency_rise), "baseline 95%" (success_drop, block_spike), `value` | exists-but-different-shape | Regex matches latency baselines only; a success-drop first sample leaves "baseline" blank. Raw `data.baseline` on `/v1/events` is cleaner. Line/spike SVG is hard-coded. |
| 94 | Audit visual: up to 3 rows, ASN name (`detail.split(' · ')[1]`) + verdict "Data centre" (signal<0) / "Residential" | failed audit first, then others | text | scan loop | `Dataset.tape[]` audit rows; `/v1/events → data.name` (new_hosting_asn) | exists-but-different-shape | API audits: `new_hosting_asn` (signal −1, detail "Provider · ASN name") and `ip_overlap` (signal 0, detail "Provider" only → shows network name, labelled "Residential", wrong). No "ASN audit clean" event exists (demo). |
| 95 | Pool visual "Latest overlap": title, detail, network name, legend "Other pool", "Shared IP" | `[...of('overlap'), ...of('rotation')][0]` | text | — | `/v1/events` `kind=ip_overlap → data.value, shared_ips, unique_ips, region` (category audit) | exists-but-different-shape | No rotation event; `ip_overlap` does not name the other network (demo title "Exit IP also seen in Helix Mobile"). Other-network overlap is in the snapshot overlap blocks (`profile.overlap`), not on the event. |
| 95a | Empty state: a card's picture is not rendered when it has no sample rows; its "{n} on the tape now" count still shows | `card.samples.length > 0 &&` | — | — | sample rows per card (#89-#95) | exists-but-different-shape | With `Dataset.tape` the Pool picture is always hidden and its count is 0 (`overlap` folded into `audit`, no `rotation`); the "Latest requests" panel is hidden too (`list.length > 0`). |
| 96 | "{n} on the tape now" per card | `of(...card.kinds).length` over all rows on record | integer | — | none | missing | `stats.totals.byCategory` for the selected range (or 24h). |

### 13. Shared Overview tape (pi-tape.tsx, `home.json` tape)

| # | Shown on page (element) | Website data field | Unit / format | Filters, sorts, ranges, interactions | API endpoint → response field | Status | Notes |
|---|---|---|---|---|---|---|---|
| 97 | Rows: icon, title, detail, value, tone by signal | `DATA.tape[]` ← `home.json tape[]` ← export-home `tapePage(dataset.tape).rows` (`id, kind, providerName→provider, title, detail, signal, value, agoSeconds, source`) | text | newest first | `Dataset.tape[]` same fields | exists | Overview component; links "Open the full tape" here. |
| 98 | Filters All / Census / Pulse / Incidents / Audit | `e.kind === filter` | tab | client filter | `/v1/events?category=` (same 4 values); `Dataset.tape[].kind` | exists | Exactly the API categories. |
| 99 | "Live test feed" vs "Simulated request feed" | any `source === 'measured'` | label | — | `Dataset.tape[].source` | exists | — |
| 100 | Header "{age} ago" (min `agoSeconds`) and row ages | `agoSeconds` | s/m/h/d | — | `Dataset.tape[].agoSeconds` | exists-but-different-shape | `agoSeconds` is relative to the snapshot's `generatedAt`, not now; use `at` against the visitor clock as the live tape does. |
| 101 | Network page tape (source of the merged rows) | `networks.json networks.<slug>.tape[]` (≤30, residential only) | list | — | `/v1/networks/{slug}?kind= → tape[]` (≤20, `records.tape` full count) | exists-but-different-shape | Cap 20 vs 30; `kind=` selects the category. Not needed by this page once it reads one feed. |

### Further details (website rules, no API gap)

- The merge drops a separate identical "Down in NR" event (75 rows → 74).
- Board figures exclude the 10 held-back and the paused rows.
- The rail's network list is fixed at build time: "Reporting" can exceed its denominator, and following a network with no rows shows no chip.
- "No events yet" means none in the window; the empty feed has a second line, "New ones appear here as they are recorded."
- Heatmap axis labels are in days for 7d and 30d; the heatmap is 338 px tall.
- The spark tooltip uses `pct(v, 0)`; the spark turns red on neg; the following chip shows only the network name.
- JSON route edge cases: `limit=0` gives 200 (clamped), `cursor ?? since` fallback, an invalid `since` returns `[]`.
- The FeedSection sample prints `network` as a string and leaves out `detail`, `signal` and `source`; `cursor` is not the last key.
- The stable `event-<n>` ids of `home.json` are replaced by `t<N>` (EV-FEED-STATIC).

### Gaps summary

1. EV-COUNT: total events on record / in window (hero meta, pills, "Events" card, "Events in the window", footer total, "on the tape now") — `/v1/events/stats?range=24h|7d|30d[&network=&category=] → totals.events`, `totals.byCategory` — `public.test_events` COUNT, precomputed per range.
2. EV-REPORTING: networks with events in window and tracked denominator (#6, #8, #33, #34) — `stats.totals.reporting` + `/v1/networks` length — `test_events` COUNT DISTINCT provider.
3. EV-RANGE: 24h/7d/30d window over events (#18, #20) — `stats?range=` plus paged `/v1/events` (`since` + cursor) — `test_events`.
4. EV-SPARK: per-bucket totals, incidents, distinct networks (24 × 1 h / 28 × 6 h / 30 × 1 d) (#24, #32, #35) — `stats.buckets[] {start,total,byCategory,networks}` — `test_events` GROUP BY bucket.
5. EV-PASSRATE: pass rate from signals is the wrong measure (#25-#27) — `/v1/networks/all → ranges.<range>.requests/successes/successRate` or `stats.totals.pulseRequests/pulseSuccesses` — snapshot fleet totals or `test_events.data`.
6. EV-PASS-SPARK: pass rate series for 24h (hourly) and 7d (6-hourly) (#28) — `/v1/networks/all/chart?range=24h|7d|30d → success[]` already serves the fleet success series for every range (`profile.go` `allCharts`): resample it to 24 / 28 / 30 points (website), or `stats.buckets[].successRate` — snapshot fleet history.
7. EV-INCIDENTS: incident count and its meaning (recovered counts as incident) (#29, #30) — `stats.totals.byCategory.incident` + `incidentsBad` (signal<0) — `test_events`.
8. EV-HEATMAP: 5-row heatmap at 48 / 56 / 60 buckets with tooltips (#37-#39) — `stats.heatmap[] {start, byCategory}` — `test_events`; `pool` group has no category.
9. EV-RAIL: per-network events, passed, last event, 24 recent signals, sort (#42, #44, #46-#48) — `stats.networks[] {slug, events, good, bad, lastAt, recent[24]}` — `test_events` per provider; today 18 × `/v1/events?product=` requests.
10. EV-SLUG: `/v1/events` filters and rows use the framework provider id, page uses site slug (#45, #56, #63) — `network=<slug>` param, `providerSlug`, `providerName` on rows — `slugOfFramework` map already in the build.
11. EV-TEXT: polled rows have no title/detail/value (#9, #61, #62, #66, #83, #89) — add `title`, `detail`, `value` (port of `tape.go describe`, rendered once in `EventWindow`) to `/v1/events` rows — `test_events.data`.
12. EV-KINDS: website kinds request/rotation/overlap/pricing and group Pool have no API source; `ip_overlap` sits under audit (#11, #55, #59, #64, #90, #95) — map on `category`, optional `pool` sub-group for `ip_overlap`; no source for request/rotation — framework.
13. EV-SEARCH: free-text search and result count (#57, #58) — `/v1/events?country=&target=&q=` (+ `stats` count) — `test_events.country/target` + rendered text.
14. EV-CATEGORY-TAB: kind tabs filter only loaded rows (#55) — call `/v1/events?category=` per tab — exists, website change.
15. EV-CURSOR: id cursor and `cursor`/`updatedAt` in the response; `since` time cursor loses rows inside a same-`at` burst (#75, #79, #81, #82) — `after=<id>` param, response `{cursor, updatedAt, events}` — `test_events.event_id`.
16. EV-CATCHUP: prioritized pick silently drops routine rows when more than `limit` arrived since the cursor (#77) — `more: true` / `next` cursor, or no prioritizing when `since` is set — `/v1/events`.
17. EV-INITIAL: initial SSR rows come from the stale, 9.2 MB dataset file (#9, #72) — SSR from `/v1/events?limit=200` with EV-TEXT — `test_events`.
18. EV-CORS: withdrawn on verification: `/v1/events` already sends `Access-Control-Allow-Origin: *` (#80).
19. EV-MONOGRAM: no monogram field (#10, #43) — `monogram` on `/v1/networks` rows or client `name.slice(0,2)` — catalog.
20. EV-TARGETS-COUNT: target count only in the dataset file (#21) — `targets` count on `/v1/networks` or `/v1/version` — `Dataset.targets`.
21. EV-SCORE: browser target score beside a pulse % (#68) — `/v1/networks/{slug} → profile.targets[].scores.success` keyed by `TestEvent.target`; event lacks engine — snapshot browser scores.
22. EV-AUDIT-VERDICT: audit visual labels `ip_overlap` (signal 0) "Residential"; no clean-audit event (#94) — website maps by `kind`; no source for "clean" — framework.
23. EV-OVERLAP-PEER: `ip_overlap` does not name the other network (#95) — `data.peer` on the event or link to `profile.overlap` — framework detect / snapshot overlap.
24. EV-BASELINE: incident baseline parsed from text, only latency matches (#93) — use `data.baseline` + kind from `/v1/events` — `test_events.data`.
25. EV-AGO: Overview tape ages relative to snapshot time (#100) — use `at` — `Dataset.tape[].at`.
26. EV-NETTAPE: per-network tape cap 20 vs 30 (#101) — `/v1/networks/{slug} → tape` + `records.tape` — fine for this page.
27. EV-RAWKIND: `Dataset.tape[]` and `/v1/networks/{slug} → tape[]` carry only the category, so the page cannot tell `estimate_hour_released`/`daily_test_finished` from `census_finished` (#92), `ip_overlap` from `new_hosting_asn` (#94, #95) or `pulse_down`/`pulse_recovered` from a pulse (#89) — add the raw kind (`event`) to `TapeEvent`, or read rows from W8's `/v1/events` (which has `kind`) — `test_events.kind`.
28. EV-NEWEST: a `/v1/events` answer without `kind`/`category` is prioritized even for `limit=1`, so `limit=1` is the newest notable event and `limit=k` is not the k newest (#2, #9, #44, #48) — read the newest with `limit` ≥ 2 (k newest with `limit` ≥ 2k); the rail's beats from W9 `stats.networks[].recent[]` — `/v1/events` as is.
29. EV-FEED-STATIC: the website's `/proxy-index/live-tape.json` re-serves the build-time snapshot, so polls never get new rows, and `t<N>` ids are re-ranked on every build, so a cursor held across a deploy re-delivers or skips rows (#85a, #75, #79) — the route becomes a thin adapter over `/v1/events` (W8 text and cursor) with the stable integer ids, or the page and JSON-LD point at `/v1/events` — `test_events.event_id`.
30. EV-EDITORIAL: editorial `site.tape_events` rows are served with `at: null` although `occurred_at` is read, with `source: "measured"` although they are catalog rows, and appended after the measured rows instead of in time order (#65, #97, #99) — serve `at` = `occurred_at` and a distinct `source` (e.g. `editorial`), sort the merged tape by time — `site.tape_events.occurred_at`.
31. EV-PROXYKIND: `Dataset.tape[]` and `/v1/events` mix every proxy kind; the networks export kept residential rows only, and `/v1/events` has no proxy-kind filter (`product` is `<frameworkProvider>[/<product>]`, one provider) (#72, #101) — add a `proxyKind=` filter (rows already carry `product`), low priority while every measured network is residential (TGT-11) — `test_events.product`.

### Performance notes

**Production event volume (estimate).** Pulse runs write no per-run event (`admin/writers.py`); `detect()` runs every minute, evaluates each pulse batch × product once and writes `pulse_finished` only when success moved ≥5 pp from the previous pulse, p50 moved ≥1.5×, or challenges/blocks appeared or vanished (`methodology.toml [events]`). It also writes incidents (`success_drop`, `latency_spike`, `block_spike`, `down`, `recovered`).

| Source | Plan | Events | Per hour |
|---|---|---|---|
| Pulses | ~18,000 runs/h | `pulse_finished` on change. With 100 requests near 95% success, sampling noise alone moves two pulses ≥5 pp apart ~10% of the time; more on browser targets where blocks flip. Est. 10-25% | 1,800-4,500 (cap 18,000) |
| Pulse incidents | same | spikes, drops, down/recovered, est. 1-3% | 180-540 |
| Daily tests | ~3,848/day | `daily_test_started` + `_finished` per network × region run (not per country), ≤2 per test | ≤320, in peak-hour bursts |
| Estimate releases | hourly | one `estimate_hour_released` per network hour | ~18 in one burst at :00 |
| Audits | per hour of daily tests (`daily/audits.py`), per batch (`detect`) | `new_hosting_asn` (rare), `ip_overlap` (at most one per network and hour) | ≤18, plus a rare new ASN |
| **Total** | | | **~2,000-5,500 (central ~3,000; worst ~19,000)** |

So ~50 events/min. About 12 events land per 15 s poll (worst ~80), all stamped with the minute's detection time. 24h is ~50k-130k rows, 30d ~1.5M-4M. More than 90% are routine (`pulse_finished` + census tab).

The event volume above assumes HTTP pulses; production has none, see section 14 (about 5,000 to 8,000 events a day). Production's audits come from the daily ingest's audit step, not from `detect` (no batches there): without it the Audit tab stays empty (#815).

**What the API holds.** `EventWindow` = 2,000 newest rows (~20-60 min at production volume) + 500 notable. `Dataset.tape` = 200 rows (~2-6 min; 100 notable + 100 newest). The page's 24h/7d/30d figures can never be computed from rows on the client. All metric cards, the heatmap, the rail and the counts need server aggregates (EV-STATS, gaps 1-9).

**Calls per view today (if wired to the API as is).** One SSR read of `Dataset.tape` (the 9.2 MB / 0.5 MB gzip dataset file, or its cached copy). The rail would take 18 × `/v1/events?product=` (last event + 24 beats). The search box has no server call possible. Then 1 poll / 15 s / viewer (4 req/min/viewer). Target: one SSR `/v1/events?limit=200` (rendered rows), one `/v1/events/stats?range=` per range change (and every ~60 s to refresh figures), and the 15 s poll.

**Polling load.** Polls are URL-keyed (`since`). All viewers converge on the same newest `at`, so the CDN (`s-maxage=15`) collapses them to about one origin request per distinct cursor per 15 s, whatever the audience. At the origin, each events refresh (change detection every 10 s; a new event at least every minute in production) reads the same 2,000 + 500 rows in one read-only transaction and resets the events LRU (256 entries). It pre-renders only the default `{limit:100}` query without `since`, so the first poll per cursor scans 2,500 rows in memory (microseconds). A poll whose `since` is older than the window (a long-hidden tab) goes to the database (10 s timeout). One poll is ~12 TestEvents × ~0.4 KB ≈ 5 KB (worst ~32 KB). The initial 200 rendered rows are ~60-100 KB raw. Polling every 15 s is finer than the 1-minute detect cadence; 30-60 s would lose nothing, but 15 s matches the cache lifetime.

**Precompute for instant answers.** At each events refresh, keep rolling per-bucket counters from `test_events` (index on `at`). Per range: 24h 48 × 30 min, 7d 56 × 3 h, 30d 60 × 12 h; sparklines take half that resolution by summing bucket pairs. Each bucket holds per provider × category: counts of events, good (signal>0), bad (<0), and pulse requests/successes (`data`). That is ≤18 × 60 × 4 ≈ 4.3k cells per range, so distinct networks per bucket and per-network totals fall out without re-scanning. Also keep per provider the `lastAt` and the 24 newest signals. Render the stats answer (≈10-20 KB) once per refresh per range (3 bodies; per network × range adds 54 bodies if the followed network should rescope the cards). Render `title/detail/value/providerSlug/providerName` once per event when the window is loaded, so polled rows and `Dataset.tape` share one shape. Choose one window anchor: the website ends windows at the newest event; the API should do the same, or say it ends at now.

## 10. Requests per page view

What one view of each page would need from the API as it is today, and after the work items in
section 11. Compare and true-cost pre-render all three ranges into the HTML (`IndexRangeStyles`); the
Overview, networks, network and live tape pages switch ranges in React state over data for all
three; targets and overlap have no range switch (30d). So on a windowed page a range-dependent
answer is requested once per range.

| Page | Today, wired to the API as it is | After section 11 |
|---|---|---|
| `/proxy-index` | about 37: RK, ALL, 18 × NP (tagline, authenticity, exclusivity, hardest-target rate, list rate, cities, incidents), 18 × CH per range (curves, sparks), plus the 9.2 MB dataset file for `measuredMarket`, `measuredTotals`, profiles, targets and tape | 3 to 5: `/v1/home?range=` per range, RK per range (leaderboard), `/v1/events` (tape poll) |
| `/proxy-index/targets` | 3 to 21: RK 24h and 30d, the dataset file for `targets[]`, and the dataset file or 18 × NP for the 144 network × target cells, 30d only | 2 to 4: `/v1/targets?range=` per range, `/v1/meta` |
| `/proxy-index/networks` | about 24: RK per range, ALL (one number), the dataset file (target count, weights), 18 × NP (tagline, list rate) and 3 × CH (preview sparkline labels; or RK `trend` without labels) | 1 to 3: RK per range (with `/v1/meta` folded in or fetched once) |
| `/proxy-index/networks/<network>` | about 11: NP, CH × 3, COV × 3, RK × 3, the dataset file (target meta); per-range targets, overlap and ranks cannot be served | 8 to 12 for every range (NP, CH, COV, RK, `/v1/targets` and `/v1/overlap` per range); 4 to 5 for the default 30d view |
| `/proxy-index/compare` | about 22: RK × 3, 18 × NP, the dataset file | 4 to 7: RK × 3, `/v1/targets` × 3, `/v1/meta` |
| `/proxy-index/overlap` | 19: 18 × NP (or the dataset file), RK | 2: `/v1/overlap`, RK |
| `/proxy-index/true-cost` | 22 or more: RK × 3, 18 × NP (prices), the dataset file (bills, targets); per-target tabs for 24h and 7d cannot be served | 6: RK × 3 (with the `cost` summary), `/v1/targets` × 3 (with cost per target) |
| `/proxy-index/live-tape` | the dataset file's 200 tape rows, 18 × EV for the rail, then one EV poll every 15 s; 24h / 7d / 30d figures cannot be served | 3: EV `limit=200` at render, `/v1/events/stats?range=` per range, the EV poll every 15 s |
| Search palette (every page) | the website's own `search.json` | `/v1/search-index` in the website's shape, once per visit |
| Freshness (every page) | none: no page polls; data changes only with a rebuild and deploy; the live tape polls a build-time `live-tape.json` | `GET /v1/version` while the page is open (`public, max-age=5`, send `If-None-Match`, 304 when unchanged); on a new `revision`, refetch the answers the page shows (HC-12) |

Answer sizes stay far under the 200 KB cap: `/v1/home` about 40 to 70 KB per range, `/v1/targets`
40 to 60 KB, `/v1/overlap` 20 to 30 KB, `/v1/events/stats` 10 to 20 KB, RK with the new fields
about 30 KB (identity; gzip 5 to 8 times smaller).

Data cadence: section 14.

## 11. Gap index by work item

Every `missing` and `exists-but-different-shape` row of sections 1 to 9, grouped by the API change
that closes it. Gap IDs are the ones in each page's "Gaps summary". `docs/api-web-map-gaps.json`
lists every gap ID with the class this section, section 12 or section 13 gives it, and for an API
gap the requests and fields that show it closed (`siteapi bench --gaps`, `api/README.md`). Each work item is computed
once per build, from data the build already reads, and served from memory like every other
answer; only W9 reads the database per refresh, from a summary table kept on insert.

| Item | Change | Closes | Source |
|---|---|---|---|
| W0 | **Page-answer registry.** One file per new answer: route, sample requests, golden hashes and doc; so W1 to W12 do not edit the same files. | (enables the rest) | API code layout |
| W1 | **Per-range window figures** in `WindowStats` (NP `stats[r]`, RK rows): `captchaRate`, `blockRate`, `requestSuccessRate`, `countries`, `regions`, `cities`, `asns`, `datacenterAsnShare`, `exclusiveShare`, `sharedShare`, `observedDays`, `latencyUnderLoad` (flag and note), `wireGbPer1k` (wire traffic per 1,000 requests), the chart's `latencyUnderLoad[]` series (from `series[].latency_under_load_ms`), point times for `trend` (`trendT`) and spark labels. | CMP-1, CMP-2, CMP-3, CMP-4, CMP-7, TC-7, TC-9, HOME-5, HOME-23, HOME-38, HOME-40, HOME-45, NETS-8, NETS-16, NET-DC-RANGE, NET-LAT-LOAD | snapshot `windows[r].metrics`, `.pool`, `.overlap`, `.period`, `.series` |
| W2 | **Rankings rows and list facts.** Rows gain `listRate`, `pricePerGb`, `pricePerGbAtScale`, `priceLabel`, `tagline`, `standing {rank, peers}` (the "#1 of 1" rule), `cities`, `asns`, `authenticity`, `uptimePct`, `requests`, `latencyP90`/`P99`, `downtimeMinutes`, trust and trial fields, `browserOverall.<engine>.success`, the W1 figures; `?kind=`; `valueRank` within the kind. The answer gains `targets` (counts), `weights`, `profiles`, `fleet.requests` and a `cost` summary (cheapest, median, dearest, spread, priced). | HOME-19, HOME-24, HOME-28, HOME-34, HOME-36, HOME-41, HOME-46, NETS-1 to NETS-7, NETS-10, HC-4 to HC-7, CMP-6, CMP-11, CMP-12, TC-1, TC-2, TC-5, TC-6, TGT-4, NET-LEADS, NET-ALT-PRICE, NET-ASN-LEADER, NET-RANK-AVAIL (list) | provider blocks already built (`pricing`, `trust`, `pool`), `w.browser_overall`, `costBoard` |
| W3 | **Network answer extras** (NP): `score.peers` and standing text, `pricing.listRate`, `pricing.hasTrial`, `protocol {label, targets, summary, schedule}`, per-range ranks `{rank, peers}` for success, availability, hosting share, value and price, the per-range alternative pick, `schedule`, `claimShare`, review counts and verification labels, the City × ASN usage label, the record lists (tape, reviews, community runs) and their counts filtered by kind; tape rows with their raw event kind (`event`), and editorial rows with `at`, `source` `editorial` and in time order. | CMP-12, NET-PEERS, NET-LISTRATE, NET-PROTOCOL, NET-RANK-SUCCESS, NET-RANK-PRICE, NET-RANK-AVAIL, NET-RANK-DC, NET-VALUE-RANK, NET-TRIAL-BOOL, NET-ALTERNATIVE, NET-SCOPE-COPY, NET-SCOPE-UNUSED, NET-RECORDS-KIND, NET-RANK-SENTENCE, NET-REVIEW-COUNTS, NET-VERIFY-LABEL, NET-USAGE-LABEL, NET-SCORE-NOTE, NET-COUNTRY-RANK-NOTE, HOME-20, HOME-26, EV-RAWKIND, EV-EDITORIAL | the build's `stats`, `tileRanks`, `site.scopes`, `site.pricing`, `site.reviews`, `site.tape_events`, `test_events` |
| W4 | **`/v1/targets?kind=&range=`**: target meta (label, category, difficulty, scoring weight, pass condition, engine, group, sources), card count, and per network × target the main engine's rates, outcomes, page load and scores with its `engine` and `otherEngines` (`?engine=<e>` gives each engine's figures), the network's `browserOverall`, cost per 1k successes and success and cost ranks, plus per target and engine the fleet mean, `rankedOf` and shared score status; every range (not only 30d). The network page reads its own row from it. | TGT-1 to TGT-7, TGT-9, TGT-10, TGT-12, CMP-8, CMP-9, CMP-10, TC-3, TC-4, TC-8, NET-TARGETS-RANGE, NET-TARGET-META, NET-TARGET-RANK, NET-TARGET-LATRANK, EV-SCORE | snapshot `windows[r].targets`, `windows[r].browser_overall`, `site.targets`, `public.targets`, methodology `[browser_difficulty]` |
| W5 | **`/v1/overlap?kind=&range=`** (24h, 7d and 30d, each computed; 24h every build): every network's pool basis, exclusive and shared share and IPs, ownership label, likely upstream, and every pair (shared IPs, share of ours and theirs); market totals per kind on one basis; likely resellers. The network page reads its own row. | OVL-1 to OVL-7, CMP-4 (pairs), CMP-5, HOME-33, HOME-35, NET-OVERLAP-RANGE, NET-OWNERSHIP-LABEL | snapshot `windows[r].overlap`, `overlap.peers`; the old site's ownership rule |
| W6 | **`/v1/home?range=`**: the market panel (requests, curve, median success, distinct IPs, bandwidth, median latency, sparks, fastest, most reliable, cheapest, dearest, spread, authenticity, countries, incidents, mis-sold share, duplication), the four insight cards, every network's panel figures with curve and sparks, and each tape tab's newest 10 mapped rows (HOME-TAPE-1). | HOME-4, HOME-6 to HOME-18, HOME-21, HOME-22, HOME-25, HOME-27, HOME-29 to HOME-33, HOME-35, HOME-43, HOME-47, HOME-51 (resellers and upstream through W5) | `measuredMarket` views, `measuredTotals`, `windowStats`, the tape mapping (all built today, only in the dataset file) |
| W7 | **`/v1/meta`**: weights, ranking profiles, grade bands, target meta and counts (all, measured, cards), ranges, scopes. | HC-1, HC-2, HC-3, HOME-37, HOME-42, TGT-5 (small form), EV-TARGETS-COUNT | snapshot `methodology.settings` (`weights`, `grades`), `site.ranking_profiles`, `site.targets`, `public.targets`, `site.scopes` |
| W8 | **Tape rows**: `/v1/events` rows carry `title`, `detail`, `value`, `providerSlug`, `providerName`; `network=<slug>`, `country=`, `target=` and `q=` filters; an id cursor (`after=`) with `cursor` and `more` in the answer; render rows from it. | EV-TEXT, EV-SLUG, EV-CURSOR, EV-CATCHUP, EV-SEARCH, EV-INITIAL, EV-BASELINE, EV-AGO (with `at`), EV-PROXYKIND | `test_events`, the build's `TapeFromEvents` |
| W9 | **`/v1/events/stats?range=`**: totals by category, reporting networks, buckets (24 × 1 h, 28 × 6 h, 30 × 1 d), the heatmap, per-network rail (events, good, bad, last event, 24 recent signals); fleet pass rate from the network figures. Backed by a summary table kept on insert. | EV-COUNT, EV-REPORTING, EV-RANGE, EV-SPARK, EV-PASSRATE, EV-PASS-SPARK, EV-INCIDENTS, EV-HEATMAP, EV-RAIL | `test_events`, a new per-bucket counts table maintained by an insert trigger; ALL ranges |
| W10 | **Search index in the website's shape**: `/proxy-index/...` hrefs, taglines, review pages, target copy, no old-site pages or actions. | HC-8, HC-11, HOME-2, NETS-9, NETS-11, TGT-13, NET-SEARCH-REVIEWS | `build/search.go` |
| W11 | **Calculator additions** where the snapshot lacks the input: per-target wire bytes (for W4's cost per target), per-kind fleet distinct IPs (for W5's market totals); each only if the snapshot does not publish it already. | TC-3 (input), OVL-2 (input) | `src/proxy_index/calculated/` |
| W12 | **Paged reviews and price history**: `/v1/networks/{slug}/reviews?rating=&verified=&page=`; paged community runs `/v1/networks/{slug}/runs?kind=&status=&page=`; a price history (90 days, last change, days since change) from a new catalog history table. | NET-REVIEWS-PAGED, NET-RUNS-PAGED, NET-PRICE-HISTORY | `site.reviews`, `site.community_runs`; a new `site.pricing_history` |

## 12. No API change: website or owner decisions

| Gap | What | Recommendation |
|---|---|---|
| HOME-1, HOME-44, EV-CATEGORY-TAB, EV-MONOGRAM, NETS-12 (part), HOME-39 | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend` | Website changes; the API already serves the inputs. |
| HOME-3, HC-9, NETS-12 | "Download the dataset" serves the website's `home.json` | The website route redirects to `/v1/dataset` (already immutable and precompressed). |
| NET-ARTICLE | The editorial review article (verdict, bottom line, body) | Stays website content (it is editorial, written per network); no API source. |
| NET-ANALYSIS-ARCHIVE | Stored weekly analyses beyond 30 days | No source yet; out of scope until analyses are written. |
| EV-KINDS, EV-AUDIT-VERDICT, EV-OVERLAP-PEER | Event kinds `request`, `rotation`, `pricing`, a "clean audit" event, the other network of an `ip_overlap` event | No framework source; the website maps the kinds it gets. |
| EV-NETTAPE | Per-network tape cap 20 (page shows 30) | Website shows 20, or W8's `network=` filter. |
| HOME-33, OVL-3 | Likely resellers | Computed by W5 from the overlap pairs (the old site's rule); no reseller detection beyond it. |
| TGT-8 | `category` (and catalog `difficulty`) of the browser targets without a `site.targets` row | Catalog rows (owner content), written with SQL as `api/scripts/*.sql`; W4 defaults the difficulty to the scoring weight meanwhile. |
| TGT-11 | Target scores rank every product group of a window together, not per proxy kind | All 18 networks are residential today, so the ranks are the residential ones; per-kind ranking waits until a second kind is measured. |
| TGT-14 | Data strings (`label`, `category`, `passCondition`) used as translation keys on the targets page | Website keys target copy by `targets[].id`; the API keeps serving English catalog wording. |
| TGT-15 | Hard-coded "Eight"/"eight" and "ticketing queue" in the targets page's window title, meta and JSON-LD description and keywords | Website copy built from the card count (TGT-9 field once W4/W7 serve it), ticketing mention removed. |
| TGT-16 | Targets page still built on the mock suite (`ticketing_queue`, no `ai_answers`, no group filter, no sources breakdown) | Website change when it switches to the API; reads `targets[].group`, `.source`, `.difficulty` and `sources[]` (W4 adds `sources` to its cells). |
| OVL-8, OVL-9, OVL-10 | Demo overlap data (3 of 4 networks, inconsistent shares), the blank `[data-ov-empty]` state and the untranslated "N/A" on the overlap page | Website changes: read measured networks from the API with `SHOW_DEMO` off, add the empty-state copy, use the shared "N/A". |
| CMP-13 | Compare picks are not in the URL or stored | Website reads and writes `?networks=a,b,c,d`; no API change (every network is pre-rendered). |
| CMP-14 | Null grade, consent, difficulty and price states (C22, C51, C67b, C70, C74b) | Website maps each null to "N/A" before formatting (also every other `gradeTone` call on the home and network pages); no API change. |
| CMP-15 | Shifter's trial text and order URL are hard-coded in the website | Owner fills `site.pricing.trial` and the measured product's `url` for Shifter (`api/scripts/*.sql`); the website then drops both fallbacks. |
| CMP-16, HOME-49 | Headline score, grade, rank and sub-scores do not follow the dock (C19, C22-C25, C31, C32) | Decide whether they follow the range; RK already serves each range, no API change. |
| CMP-17 | By-target best mark can compare a Chrome rate with a Camoufox rate (C61, C63) | Website marks the best only among cells of the same `engine`, or reads one engine's body (`/v1/targets?engine=<e>`); W4 carries `engine`, `otherEngines` and the per-engine bodies (#735). |
| CMP-18 | Success rate hint misdescribes the figure (C33) | Website copy: "Mean of time-slot success rates, browser targets excluded" (as HOME-42). |
| EV-NEWEST | Newest event(s) from a prioritized `/v1/events` answer (dateModified, hero rows, rail last event and beats) | Website requests `limit` ≥ 2 (≥ 2k for the k newest); the rail uses W9. |
| EV-FEED-STATIC | The website feed route serves a build-time snapshot with per-build ids | Website route proxies `/v1/events` once W8 lands (EV-TEXT, EV-CURSOR); no API change beyond W8. |
| HOME-48 | Demo networks in every home list (`SHOW_DEMO = true`) | Website sets `SHOW_DEMO = false` when it reads the API (as OVL-8). |
| HOME-50, HOME-52 | Network incidents counted over the whole list; hero and market request totals from two helpers | Website reads `stats.<r>.incidentCount` and one requests field for both tiles. |
| HOME-53, HC-10 | Gauge label "Non-hosting", card 3 lead over no rows, empty tape filter, hero range phrase, "0.0" for a null score, rank "–" | Website copy and null states ("N/A", empty-state sentences, "Exit IPs in the requested country" label). |
| HC-12 | "Live" hard-coded in the dock, `IndexHero` window and Overview hero; no page polls `/v1/version` | Website change: show data age from `generatedAt` and poll `/v1/version` (304 when unchanged), refetching on a new `revision`; extends HOME-1 to every page. |
| HC-13 | JSON-LD `dateModified` from three export dates; none on `/networks` and the network page | Website change: one `generatedAt` per build from any answer the page fetches; add it to the two network pages. |
| HC-14 | `SHOW_DEMO` demo networks and re-numbered ranks on every page | Website change: delete the switch and demo branches with the API switch-over; ranks from RK `rank`, `peers`. |
| HC-15 | Hard-coded `INDEX_NETWORK_SLUGS` for redirects, footer, sitemaps, llms.txt | Website change: build the slug table and the footer's provider lists from `/v1/networks` at build time; link only listed slugs; keep the `smartproxy` alias. |
| HC-16 | `NetworkStrip` crashes on 0 networks (server render) | Website guards the empty list (hide the strip or an empty-state line); no API change. |
| NET-TWO-SOURCES, NET-NA-GRADE, NET-MOCK-DATA | Network page reads two mock files that disagree on the score; a null grade prints blank or "Grade null"; the mock `ticketing_queue` row, three demo networks and the `?demo-checks` numbers | Website changes when the page reads the API: one source (`P.score`, `S[r]`), print "N/A" for a null grade (as NETS-13), drop `ticketing_queue` (as TGT-16), set `SHOW_DEMO = false` (as HOME-48) and remove `?demo-checks` or wire it to `S["30d"].latencyP90`, `P.performance.captchaRate` / `.blockRate` and `communityRuns[].targets[]`. |
| NETS-13, NETS-14, NETS-15 | "00" rank and blank grade for unscored networks; FAQ promises cost per 1,000 successes on every card; 30d score, grade and rank beside per-range figures | Website changes over fields RK already serves: print "N/A"; add `costPer1kSuccess` as a card figure (recommended, the FAQ's argument is the page's) or reword FAQ answers 3 and 4; read score, grade and rank from `?range=30d` and label them "30 days", or follow the range (owner decision). |

## 13. Corrections made while checking this map

- EV-CORS (live tape rows 3 and 80) was withdrawn: `/v1/events`, like every API answer, sends
  `Access-Control-Allow-Origin: *` (`api/internal/server/server.go`, `write`).
- NET-BUCKET-TEXT (network page row 131) was withdrawn: the stored facts and the 24h series are 5-minute buckets (proxy-index `calculated/aggregate.py` `coverage_basis`, `series`); the 30-minute value is the every-request scoring slot.
- An independent omission pass mapped every page again from the website source, then compared it
  with this map, verifying each finding in the website and API code. It added rows (suffixed `a`,
  `b`, … so existing row numbers stay), corrected statuses and wrong fields, and added the gaps
  NETS-13 to NETS-16, TGT-14 to TGT-16, CMP-12 to CMP-18, OVL-8 to OVL-10, TC-9, EV-RAWKIND,
  EV-NEWEST, EV-FEED-STATIC, EV-EDITORIAL, EV-PROXYKIND, HOME-48 to HOME-53, HC-10 to HC-15 and
  NET-RECORDS-KIND to NET-MOCK-DATA, each indexed in section 11 or 12. NET-LAT-LOAD moved from W11
  to W1: the snapshot already publishes the under-load series per bucket.
- Every API field cited as `exists` was checked against the JSON tags and keys of
  `api/internal/model` and `api/internal/build`.

## 14. Test cadence in production, and how the 24h and 7d windows are built

The data behind every page has a cadence. Each kind of test below is generated at its real
production cadence (SIM-GEN-1 and SIM-LOAD-1), never more often. The API's 24h and 7d figures then
come from those tests, through the calculator's existing windows and the hour formula: the same
window logic the API and the scores use today. Nothing is interpolated to fill an hourly grid.

| Test | Production cadence | Size of one run | Source in the code |
|---|---|---|---|
| Daily test (HTTP, `generic_http`) | Once per 24 h per network × country, at the country's test hour from the network's formula. 3,848 tests a day for the 18 networks; the busiest hour (13:00 UTC) has 442. | About 4,300 unique IPs on average (REPLAY-RUN-1's real hour 0: 190 tests, 825k IPs, 1.2M observation rows), with 2 to 4 five-minute buckets | `formulas/<network>.json` `schedule`; `daily/planner.py` (one droplet per network and region with countries due); `daily/hourly.py` (the manager's hourly timer) |
| Estimated hours | 24 per daily test, released hourly as each hour ends | 24 `estimated_hours` rows per test | `estimates/compute.py` `hours`; the release step |
| Browser targets | Once per 24 h per network × target, 100 tries, in each host's interleaved queue: 18 networks × 8 targets = 144 runs, 14,400 tries a day. The three AI sources are targets of their own; `ai_answers` is their group. | 100 tries; bundle bytes as measured by BP-VERIFY-3 (BP-VERIFY-4 once it reports) | #691 (merged): `orchestrator/browser_queue.py`, `[schedule] plan = "queue"`, `queue_period_minutes = 1440` |
| HTTP pulses (`generic_http`, `generic_https`), census and load waves | **None in production.** The targets are configured with 30-minute pulses (`configs/targets.toml`), but pulses run only on pulse nodes, and no pulse node exists: DigitalOcean has no `pi-pulse-node` droplet (checked 2026-11-08). Census and load waves are off in production (`scripts/scale/README.md`). | (not generated) | `orchestrator/batches.py` `pulse_plan` (plans only networks with a node in the region) |
| Test events (live tape) | From the tests above:<br>- `daily_test_started` and `daily_test_finished` per network × region cell, at most about 6,900 a day;<br>- one `estimate_hour_released` per network and hour, 432 a day;<br>- the audits (`new_hosting_asn`, `ip_overlap`): `detect` writes them for finished batches (pulse and census), and the daily ingest's audit step for daily tests, which have no batch. After each hour's daily tests are stored it runs `detect`'s checks once on the hour's runs: a network's first hosting ASN (once per network and ASN, ever) and, once per network and hour, the network's IPs that another network's daily tests of the same hour also used. The shared addresses are counted with hash aggregates and joins, so an hour's audits grow with its rows (AUDIT-DAILY-2); `proxy-index daily audits` runs them over runs already stored.<br>- `proxy-index daily tape` runs the detector over pulse batches already stored in a time range (the score inputs of a load, `scripts/scale/SIMULATE.md`): it sets the watermark to the range's start, calls `detect` in time order to its end and prints the events per kind and network; `--dry-run` writes nothing and a second run adds nothing. The order: store, tape, audits, calculate.<br>The detector evaluates pulse batches only (`detect.py`), so without pulses there are no `pulse_finished`, down, recovered or latency events. That is about 5,000 to 8,000 events a day, not the 50k to 130k section 9 estimated from HTTP pulses. | | `daily/ingest.py` `emit_cell` and `store_cells`; `daily/audits.py`; `orchestrator/events.py`; `detect.py` (`b.kind='pulse'`) |

**Assumption.** The owner could not be asked, so this is decided from the code and configs: production
tests each country with one daily test per network per 24 hours, and each network × browser target
once per 24 hours (100 tries). It has no HTTP pulse fleet. The generator therefore produces no
pulses, no census and no load waves. It keeps a switch, off by default, that adds them at
`configs/targets.toml`'s cadence (30 minutes) for the day a pulse fleet starts.

How the windows come out of one test a day:

- **IP counts in 24h and 7d** (unique IPs, place and ASN rows, the 24h IP chart) come from the hour
  formula's released estimates (API-ESTIMATES-1, `api/FIELDS.md` "IP numbers from the hour
  formula"). The 24h chart has one point per released hour. A 7d point is the measured day's IPs ×
  `breakdown_scale`.
- **Success, latency and errors in 24h** come from the daily tests whose runs fall in the window.
  That is one test per network × country, so a network's 24h figure mixes its countries' tests at
  their own hours. Its 24h success series has points only in the 5-minute buckets where a test
  ran. With 2 to 4 buckets per test, spread over about 214 countries a network, most of the 288
  buckets have a point.
- **Uptime and availability** come from the buckets with a started test (the calculator's rule:
  no evidence, no value).
- **Browser figures and scores in 24h** cover one run per network × target. That is 100 tries,
  exactly the score's minimum (`MIN_REQUESTS = 100` completed tries; `target_scores.py`).
  - A run cut short by its deadline, or a failed upload, leaves that network `not_enough_data` in
    the 24h window.
  - Because the queue rotates each day, a 24h window ending at a given hour may hold 0, 1 or 2
    runs of one network × target. So a 24h browser figure can be empty, or cover 200 tries.
  - 7d holds about 7 runs (700 tries) per network × target, and 30d about 30.
  - PERF-REPORT-1 counts how often each case occurs in the generated week.
- **7d windows** hold 7 daily tests per network × country, and 7 browser runs per network × target.

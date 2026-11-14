# Page answers added from Phase 3

Each page answer added from Phase 3 (the work items W1 to W12 of `docs/api-web-map.md`,
section 11) lives in files of its own, so answers can be added in parallel without editing
the same files. The registry is `api/internal/build/answers_registry.go`.

## What one answer adds

| File | What |
|---|---|
| `api/internal/build/<name>.go` | The answer: an `init()` calling `build.RegisterAnswer(build.PageAnswer{...})`, and its `Build`. |
| `api/internal/build/<name>_test.go` | Its tests. |
| `api/internal/server/testdata/answers-v4-scale/<name>.json` | Its golden hashes (below). |
| `api/internal/store/<name>.go` | Only when it reads the database on request: its SQL, registered with `store.RegisterStatement(store.Statement{...})` from an `init()`, so `siteapi explain` times it. |
| `api/answers/<name>.md` | Its documentation: route, query parameters, body fields and their sources. |

No other file changes: the server installs the route, renders the bodies and checks the
samples by itself.

## `build.PageAnswer`

| Field | What |
|---|---|
| `Name` | The family, such as `targets`. Every stored path is `/v1/<Name>` or under `/v1/<Name>/`; a path outside it leaves the answer out of the build. The families served before the registry are reserved (`rankings`, `networks`, `charts`, `countries`, `places`, `coverage`, `search`, `events`, `dataset`, `version`, `files`). |
| `Build func(*build.Context) (map[string]any, error)` | Every body of one build by its stored path, such as `/v1/targets/residential/7d`. It runs once per build and reads only the `Context`. |
| `Route` | The mux pattern, such as `GET /v1/targets` or `GET /v1/networks/{slug}/reviews`. |
| `Key func(*http.Request) (string, error)` | Validates the request and gives the stored path it serves. An error, a `build.APIError` (`build.UnknownParameter`, `build.InvalidValue`, `build.InvalidFormat`, `build.IntParam`, ...), answers 400 with its code and parameters (`api/README.md`, "Endpoints"). The helpers `build.Query` (`unknown_parameter`), `build.QueryRange` (`24h`, `7d` or `30d`, default `30d`), `build.QueryKind`, `build.QueryPage` (`out_of_range` beyond 1 to 100,000) and `build.PathSlug` (`invalid_parameter`, format `slug`) give the usual checks. |
| `Samples []string` | Request URLs (path and query) that must answer 200: the tests, `/healthz` and the bench ask for them. |
| `InDataset bool` | Adds the bodies to the dataset file's `pages`, each under `Request(path)`; with `Cut`, the cut of every held path (for an answer with `Events`, those of the build's events). `api/FIELDS.md` "Page answers in the dataset file" lists them. |
| `Request func(path string) string` | Needed with `InDataset`: the request URL that serves the body stored under `path`, every parameter spelled out (`/v1/home/7d` is `/v1/home?range=7d`); `""` leaves the body out of the file. |
| `Cut func(held map[string]any, path string) (any, bool)` | Optional, for answers with many filtered pages: `Build`'s values are held instead of rendered, and a request is answered with `Cut(held, Key(r))` (404 when not found), rendered at the fast level and kept in the version's page LRU, as the place pages are. The reviews, runs and pricing-history answers use it. |
| `Root string` | Optional: the stored paths' root in place of `/v1/<Name>`, for an answer under a reserved family's path (`/v1/events/stats`). |
| `Events bool` | Optional, with `Cut`: `Build` also runs on every events refresh (`build.WithEvents`), with the new events (`Context.Events`, its `Stats` the event counts) and the last build's `GeneratedAt` and `Pages`, all of the `Context` it may read. Its values are held in `Result.Events.Held`, and each body cut is kept in the version's events LRU (which every refresh replaces) with the events' `Cache-Control`. The events-stats answer uses it. |

`build.Context` is what the build already holds: `GeneratedAt`, the published `Snapshot`, the
networks (`Entries`), the window `Stats`, the provider `Details`, the `Dataset` (its targets,
providers and measured market), the `Events` window, the `Inputs` the build read (the site
catalog rows and the framework's) and the `Pages` built before the registered answers (the
rankings and the network answers, the all-networks one included). Building an answer reads no
database.

## What the server does with it

- Every body is rendered and compressed with the other answers, so it gets the same headers,
  ETags, 304s and `CacheData` lifetime: until the next data update, or, with a CDN configured
  (`siteapi serve --cdn-ttl`), the CDN's TTL with a 60 s browser lifetime and `CDN-Tag`
  (`api/README.md`, Caching). An answer with `Events` keeps the events' lifetime in both modes.
- A body over `build.MaxAnswerBytes` (200 KB) fails the build, as for every answer;
  `TestEveryAnswerFitsTheCap` also asks for every sample at the QA-1 scale.
- A `Build` error leaves only that answer out: its samples answer 404 and `/healthz`
  `unserved` lists them with the error.
- The route serves the body stored under `Key(r)`; a path the build has none for answers 404.
  With `Cut`, the body is cut from what the build holds on the first request and kept in the
  page LRU; its golden hashes are then its samples only.

## Golden hashes

`TestEveryV4Answer` keeps the SHA-256 of every answer of its two v4 builds in
`api/internal/server/testdata/answers-v4-scale/<family>.json`, one file per family. The families
of the answers served before the registry are `rankings`, `networks`, `charts`, `countries`,
`places` (the long-list pages), `coverage`, `search`, `events` and `dataset` (the dataset file,
the encoded pages and details, and `/v1/version`). A registered answer's family is its `Name`:
its stored bodies and its samples, rendered on request. `GOLDEN_WRITE=1` rewrites each file
(a case skipped with `-short` is left out, so write without it). The goldens of the answers from before the
registry (`testdata/answers-v4.json` and the build package's `network-one-group` and
`network-long-lists`) leave registered answers out, so a new answer changes only its own file;
an answer with `InDataset` also changes `dataset.json`, whose file holds its bodies (the
`/v1/events/stats` ones at the case's fixed time).

## Missing data

Every page answer follows the API's rule (`api/README.md`): a field is always present, a
value the API does not have is `null`, and the entity it belongs to lists its path in `na`.
The answers added from Phase 3 (`/v1/rankings`, `/v1/networks/{slug}`'s `profile`, `stats`
and own fields, its `.../places` pages, `/v1/home`, `/v1/targets`, `/v1/overlap`, `/v1/meta`) also say why, with a
reason code from the fixed set below (`build/answer_na.go`, `build/score_na.go`):

| Code | Why the field is null |
|---|---|
| `catalog` | It comes from a `site.*` catalog row that does not exist: list rates (`pricePerGb`, `pricePerGbAtScale`), the cost basis (`listRate`, `priceBasis`, `basisPlanGb`: no `price_per_gb_basis` evidence, COST-BASIS-200GB-1), trial (`trial`, `trialKind`, `hasTrial`, and the trial numbers when the catalog has none), a plan's `audience`, anything priced or ranked by price (a target's `costPer1kSuccess`, `costRank`, `valueRankAllTargets`, `headlinePriceRank`, `costPer1kAllTargets`, the cost summary's and the home's cheapest and dearest, `spreadAll`, an `alternative`) when the network has no list rate or, for a cost, no cost basis, `claimShare` without a claimed pool size, `kycVerified`, `consentDisclosed`, `stickinessAdvertised`, `protocol.summary` and `.schedule`, a target's `difficulty`, `passCondition` and `category`, editorial text, empty `profiles`, a network's empty `reviews` and `communityRuns`, and a `/v1/meta` scope's `geoCoverage.code` (a `site.scopes.geo_coverage` that is not a code) and empty booleans. |
| `not_measured` | The network, target or figure has no test in production's cadence (docs/api-web-map.md section 14): a `generic_https` cell's latency in a snapshot written before HTTPS-LATENCY-1, a target or engine with no try in the network's 30-day window, a catalog network, latency without a latency test, TTFB, `avgPayloadKb` without wire bytes in the 30-day window, and `trust.blacklistHitRate`, `trust.stickinessHoldRate` and `trust.leakChecks` without a `site.trust_signals` value (the framework runs no blocklist, stickiness or leak check, so these are `not_measured` whether or not the window has a run), and a `/v1/networks/{slug}` target's `bandwidthMb` (`ranges.<range>.targets[<id>]`, `profile.targets[<id>]`) when the target has requests but no recorded bytes (TARGET-TRAFFIC-1), and a `/v1/meta` scope's `countries` and `geoCoverage.countries` when no published network measures the scope's product (#994). |
| `not_applicable` | The field has no meaning for this row: a plan's `ips` when it is sold by GB, a per-IP plan's `gb`, an untiered pool's `catalog.tier` (plans are named by their index in the path: `pricing.plans[3].ips`), the trial numbers (`pricing.trialPrice`, `trialCredit`, `trialGb`, `trialDays`, `trialNeedsCard`) without a trial, a credit's `trialGb` and the `trialDays` of a trial without a time limit, and a network answer's `changelog[]` fields that the entry's kind leaves empty (an incident's `title`, `detail` and `delta`, another kind's `incident`, an ongoing incident's end figures), and the all-networks answer's `network.name` (all of them have no name of their own). A plan sold by GB without a GB amount has `catalog`. |
| `history_covers_range` | `observedDays` (a network's `stats.<range>`, a rankings row) is null because the history covers the whole range; without a run in the window it is `no_run_in_window`. |
| `not_published` | `ipSamples` is always empty: exit addresses are not published (the database holds real exit addresses and the API is public), and no made-up sample stands in for them. |
| `not_measured_per_place` | A place row's own requests, success rate or latency: `/v1/networks/{slug}`'s `ranges.<range>.countries[]` and the rows of `.../places` pages (`rows[]`). Daily tests record requests per requested country, not per exit city, region or ASN, so a city, region or ASN row has requests and latency only where a test records them per place (pulses), and a country row only when a test requested it. |
| `no_run_in_window` | The network or target is measured but has no run in this window (a 24h browser cell, a network window without requests, an overlap row of a network with no run in the range, the home's hardest target with no network's success, a network page target's `bandwidthMb` with no request in the range: `ranges.<range>.targets[<id>]`, `profile.targets[<id>]`). It comes before `not_measured`, `no_evidence` and `insufficient_data`: every null of a window without a run has it (latency, success, uptime, the IP figures, a 24h figure of the profile), and the hour formula's estimate never fills such a window (api/FIELDS.md, "IP numbers from the hour formula"). |
| `few_successes` | A target's latency is measured but no time slot of the window had `[slots] min_latency_successes` (30) light-load successes (pulses outside a census, runs outside a batch): `/v1/targets`' `networks[].cells.<target>.latencyP50`/`latencyP90` (HTTPS-LATENCY-1). |
| `no_successes` | A target has tries in the window but no success, so it has no cost per success: `/v1/home`'s `insights.valueAll.rows[].byTarget.<target>.costPer1k` (VALUE-ALL-1). |
| `incomplete_targets` | A bill over the card targets is null because a card target has no cost (no run or no bytes; no success too in a build from before VALUE-ALL-RANK-1, since which a target without a success is counted at a lower bound, `costIsLowerBound`): `/v1/home`'s `insights.valueAll.rows[].costPer1k`, and its `best`, `worst` and `spreadX` when no priced row is complete (VALUE-ALL-1); and a priced network's `/v1/networks/{slug}` `stats.<range>.alternative` when its own all-targets bill is null (ALT-REASON-1). |
| `no_cheaper_peer` | A priced network's `/v1/networks/{slug}` `stats.<range>.alternative` is null because no other network of the kind has a lower all-targets bill (`costPer1kAllTargets`, not a lower bound) with a success rate at most 2 points below: the network is the cheapest of its kind in the range, or every cheaper one is worse (ALT-REASON-1). |
| `no_pulse_fleet` | A `/v1/meta` scope's `cadence.pulseIntervalMinutes` is null because the published snapshot has no pulse batch: production's daily tests run no pulses (#841, #994). |
| `no_evidence` | Availability without monitoring evidence in the window: `uptimePct`, `downtimeMinutes`, incident counts. |
| `no_score_to_compare` | A success score delta (`successScoreDelta`, `profile.performance.successScoreDelta24h`/`30d`, a cell's or target's `scores.success.delta`) is null because this build or the previous published build has no ranked success score for the network, range (and target), or the snapshot predates previous scores (`api/FIELDS.md`, "Success score deltas"). |
| `no_pulse_tests` | The score (with its grade and rank) or `breakdown.uptime` is empty because the network has no, or too few, finished pulse tests in the window, so its uptime is unknown. |
| `no_load_test` | The score or `breakdown.speed` is empty because no load test in the window gives a latency under load (none ran, none ran before the light-load slots, or every one ran during a census). |
| `required_target_missing` | The score or `breakdown.reliability` is empty because a target the score requires (`generic_https`, say) has no ready result in the window. |
| `no_price_evidence` | The score or `breakdown.value` is empty because the network has no verified, dated price per GB. A network without a list rate keeps `catalog` for `breakdown.value`. |
| `few_requests` | The score is empty because the network made fewer requests than the methodology requires. |
| `low_time_coverage` | The score is empty because its requests cover too little of the window's time. |
| `expired` | The score is empty because its measurements are older than the score stays valid. |
| `insufficient_data` | The calculator published null for another stated reason (a score whose reason matches none of the codes above, a rank of a network alone in its kind, a delta without an earlier window, an outcome not classified). |

The N/A report (`/v1/coverage`, `/v1/coverage/{slug}`) gives each gap the same codes as
`reason`, with `source`: the `na_source` code of what would fill it (a `site.*` table or
column, `public.targets`, `snapshot` for the framework's published calculation) or null
(`build/na_reasons.go`, COVERAGE-REASONS-1). A scope's gaps take the scope's own codes
(`/v1/meta` `scopes[].naReason`, `naReasons`), with `site.scopes` for a catalog one.

The seven score codes come from the calculator's own reasons: `score`, `grade`, `rank` and
`standing.rank` (also the network page's `network.score`, `network.rank` and its profile's
`score.*`) take the code of the score's `reasons`, `breakdown.<component>` the code of that
component's `reason`. With several reasons the first code in the order above wins (no pulse
tests, then no load test, a required target, no price, few requests, low time coverage,
expired). `catalog` and `no_run_in_window` keep their meaning and come first; a score that has
a value never gives its rank a score code.

An entity carries the codes beside its `na`: `naReason` is the most common one, and
`naReasons` maps each path whose code differs to it (both left out when `na` is empty), so a
path's reason is `naReasons[path]`, else `naReason`. `/v1/targets` lists its targets',
networks' and cells' nulls once at its root, as a list's elements are listed
(`targets[].difficulty`, `networks[].rank`, `networks[].cells.<target>.costPer1kSuccess`),
each with the reason most of those entities have: per-cell lists would add about a kilobyte
per network to a body sized for 23 networks.

`TestEveryPhase3AnswerListsItsNulls` (build package) checks every answer of a
production-sized build without catalog rows: every null is listed in an `na` around it or
null by contract (api/FIELDS.md), and every listed path is null or empty.

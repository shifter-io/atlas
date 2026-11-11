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
| `Name` | The family, such as `targets`. Every stored path is `/v1/<Name>` or under `/v1/<Name>/`; a path outside it leaves the answer out of the build. The families served before the registry are reserved (`rankings`, `networks`, `charts`, `countries`, `places`, `coverage`, `search`, `search-index`, `events`, `dataset`, `version`, `files`). |
| `Build func(*build.Context) (map[string]any, error)` | Every body of one build by its stored path, such as `/v1/targets/residential/7d`. It runs once per build and reads only the `Context`. |
| `Route` | The mux pattern, such as `GET /v1/targets` or `GET /v1/networks/{slug}/reviews`. |
| `Key func(*http.Request) (string, error)` | Validates the request and gives the stored path it serves. An error answers 400 with its text. The helpers `build.Query` (refuses unknown parameters), `build.QueryRange` (`24h`, `7d` or `30d`, default `30d`), `build.QueryKind` and `build.PathSlug` give the usual checks. |
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
  ETags, 304s and `CacheData` lifetime.
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
and own fields, `/v1/home`, `/v1/targets`, `/v1/overlap`, `/v1/meta`) also say why, with a
reason code from the fixed set below (`build/answer_na.go`):

| Code | Why the field is null |
|---|---|
| `catalog` | It comes from a `site.*` catalog row that does not exist: list rates (`listRate`, `pricePerGb`, `pricePerGbAtScale`, `priceLabel`), trial (`trial`, `trialKind`, `hasTrial`), anything priced (`costPer1kSuccess`, `costRank`, `valueRank`, the cost summary's and the home's cheapest, dearest and spread, an `alternative`) when the network has no list rate, `claimShare` without a claimed pool size, `kycVerified`, `consentDisclosed`, `stickinessAdvertised`, `protocol.summary` and `.schedule`, a target's `difficulty`, `passCondition` and `category`, editorial text, empty `profiles`, and a network's empty `reviews` and `communityRuns`. |
| `not_measured` | The network, target or figure has no test in production's cadence (docs/api-web-map.md section 14): a `generic_https` cell, a target or engine with no try in the network's 30-day window, a catalog network, latency without a latency test, TTFB, blocklist, stickiness and leak checks. |
| `no_run_in_window` | The network or target is measured but has no run in this window (a 24h browser cell, a network window without requests, an overlap row of a network with no run in the range, the home's hardest target with no network's success). It comes before `not_measured`, `no_evidence` and `insufficient_data`: every null of a window without a run has it (latency, success, uptime, the IP figures, a 24h figure of the profile), and the hour formula's estimate never fills such a window (api/FIELDS.md, "IP numbers from the hour formula"). |
| `no_evidence` | Availability without monitoring evidence in the window: `uptimePct`, `downtimeMinutes`, incident counts. |
| `insufficient_data` | The calculator published null for another stated reason (a score not ready, a delta without an earlier window, an outcome not classified). |

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

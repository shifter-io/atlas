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
| `InDataset bool` | Adds the bodies to the dataset file's `pages` under their stored paths. False until DATASET-PAGES-2. |
| `Cut func(held map[string]any, path string) (any, bool)` | Optional, for answers with many filtered pages: `Build`'s values are held instead of rendered, and a request is answered with `Cut(held, Key(r))` (404 when not found), rendered at the fast level and kept in the version's page LRU, as the place pages are. Not with `InDataset`. The reviews, runs and pricing-history answers use it. |

`build.Context` is what the build already holds: `GeneratedAt`, the published `Snapshot`, the
networks (`Entries`), the window `Stats`, the provider `Details`, the `Dataset` (its targets,
providers and measured market), the `Events` window and the `Inputs` the build read (the site
catalog rows and the framework's). Building an answer reads no database.

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
`network-long-lists`) leave registered answers out, so a new answer changes only its own file.

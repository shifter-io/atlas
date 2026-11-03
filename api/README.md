# siteapi

A Go service that returns every piece of data the website needs from the
`proxy_reviews` Postgres database. The site reads nothing else: `src/lib/site-api.ts`
fetches `/v1/dataset` once per static build (a dev server or the on-demand
`/api/live` route revalidates it every few seconds with `If-None-Match`), and the
browser reaches `/v1/version`, `/v1/events`, `/v1/snapshot`, `/v1/search-index` and the per-page answers (`/v1/networks…`, `/v1/countries…`)
through the same-origin `/api/site/*` route. Open pages poll `/v1/version` to
update in place when the data changes (see "Live updates" in AGENTS.md). The
former `src/data/*.json` imports are gone; the files now live in `fixtures/` as
seed and test input only.

- Its main output is the site's `Dataset` type (`src/lib/types.ts`); `src/lib/data.ts`
  exports it unchanged (numeric nulls become the site's NaN sentinel, see `site-api.ts`).
- Missing data is explicit. Every field is always present. A value the API does not
  have is JSON `null`, and every entity carries an `na` array listing the paths of its
  missing fields. `/v1/coverage` reports every gap with the reason and the table
  or column that would fill it. `api/FIELDS.md` maps every field to its source.
- The four design networks (Nimbus, Kestrel, Orbitnode, Helix) live in the
  database as `is_demo = true` rows. `siteapi unseed-demo` removes them.
- Every response is precomputed and precompressed in memory. Requests never touch
  the database (except an uncommon `/v1/events` filter, see below).

Module: `github.com/Zeus-Development/proxy-index-website/api`, Go 1.27 (`/opt/homebrew/bin/go`).

## Commands

All commands find the database the same way: `DATABASE_URL`, else
`--database-file <toml>` (a `[postgres]` section like `proxy-index/.secrets/database.toml`),
else `proxy-index/.secrets/database.toml` in or above the working directory.

```sh
cd api && go build -o /tmp/siteapi ./cmd/siteapi && cd ..   # or: npm run api:build (→ api/bin/siteapi)

/tmp/siteapi migrate                        # create/upgrade the "site" schema
/tmp/siteapi seed-demo                      # pg_dump backup, then load fixtures/demo/design-demo.json as is_demo rows
/tmp/siteapi seed-demo --force              # reload even if the same file is already loaded
/tmp/siteapi unseed-demo                    # pg_dump backup, then delete every is_demo row
/tmp/siteapi serve --listen 127.0.0.1:8090  # the cached API (read-only DB sessions)
/tmp/siteapi dump --out dataset.json        # write /v1/dataset for a static build
/tmp/siteapi dump --endpoint /v1/coverage --out coverage.json --pretty
```

| Flag / env | Default | Used by |
|---|---|---|
| `--listen`, `SITEAPI_LISTEN` | `127.0.0.1:8090` | serve |
| `--database-file`, `SITEAPI_DATABASE_FILE`; `DATABASE_URL` wins | `proxy-index/.secrets/database.toml` | all |
| `--interval` | `10s` | serve: change-detection poll |
| `--content-dir`, `SITEAPI_CONTENT_DIR` (`-` disables) | `src/content` above cwd | serve, dump: guide/review search entries |
| `--file` | `fixtures/demo/design-demo.json` above cwd | seed-demo |
| `--force` | false | seed-demo |
| `--no-backup`, `--backup-dir`, `--runtime-file` | backups in `proxy-index/.state/postgres/backups/`, `pg_dump` from `bin_dir` in `proxy-index/.state/postgres/runtime.json` | seed-demo, unseed-demo |
| `--endpoint`, `--pretty` | `/v1/dataset` | dump |

`seed-demo` is idempotent. It records the file's sha256 in `site.datasets`. The same
file a second time is a no-op that takes no backup. A different file (or `--force`)
replaces every demo row in one transaction. Catalog rows (scopes, weights, ranking
profiles, framework targets) are inserted only where missing, so editorial changes
are never overwritten.

## Endpoints

All `GET` (and `HEAD`), JSON, `Access-Control-Allow-Origin: *`.

| Path | Body |
|---|---|
| `/v1/dataset` | The full `Dataset` (types.ts) plus `snapshot` and `na` (see FIELDS.md, "Additions"). |
| `/v1/overview` | What the Overview page computes: `totals`, `weights`, `profiles`, `ranges`, `fleetViews` (port of `overview.ts` and `window.ts fleetWindow`), `costBoards`, a `leaderboard` of provider summaries with `windows` (`windowStats` for 24h/7d/30d), 60 `tape` rows (the first 60, or when measured rows fill them, chosen among those like `/v1/events` chooses), `snapshot`, `na`. |
| `/v1/providers` | `{generatedAt, providers: ProviderSummary[]}` in leaderboard order (rank, unranked last). |
| `/v1/providers/{slug}` | `{provider, windows: {category: {range: WindowStats}}, reviews (published), communityRuns, changelog, tape, generatedAt}`. Gives 404 for an unknown slug. |
| `/v1/events?product=&kind=&category=&since=&limit=` | `{events: TestEvent[]}`, newest first, the exact shape of the framework's `/v1/events` (`src/lib/measured-types.ts` `TestEvent`). Uses the same validation: `limit` 1-500 (default 100), `category` census, pulse, incident or audit, `since` an exclusive ISO time. Without `kind` or `category` (the live tape's polls) routine rows (the census tab and `pulse_finished`) never push other rows off the page: the newest other rows (pulse down and recovered, incidents, audits) take up to half of `limit`, the newest rows of any kind fill the rest, newest first. A `kind` or `category` filter gets exactly the newest matching rows. |
| `/v1/search?q=` | `{query, tokens, total, sections: [{group, items: SearchItem[]}]}`, ranked like `CommandPalette.astro` (same scoring, 16 results, 5 per group). An empty `q` gives the palette's default sections. |
| `/v1/search-index` | The precomputed index (`search.ts buildSearchIndex`: networks, guides, network reviews, pages, targets, actions, plus a new `Countries` group) and the palette's constants. |
| `/v1/coverage` | The N/A report index: summary counts, dataset-level gaps, per-target/scope/profile gaps, record counts, a per-field index of which providers lack it, and one entry (`id`, `name`, `source`, `naCount`) per provider. |
| `/v1/coverage/{slug}` | One provider's gaps grouped by section with a reason for each. |
| `/v1/snapshot` | A summary of the published framework snapshot, never the payload itself (a daily build publishes one product group per network and requested country: 179 MB for 18 networks x 40 countries): `snapshotId`, `schemaVersion`, `implementationId`, `asOf`, `generatedAt`, `readiness`, `refreshSeconds`, `contentHash`, the `methodology` and `periods` as published, `productGroups`, and `scores` (every network's headline group scores per range and profile). Gives 404 `{"error":"no published snapshot","available":false}` when nothing is published. |
| `/v1/networks` | The overview: one row per network (`slug`, `name`, `kind`, `kinds`, `rank`, `score`, `uniqueIps` (the headline group's distinct IPs), `countries`, `latencyP50`, `successRate`), in rank order. |
| `/v1/networks/{slug}?kind=` | One network and category (default its headline one), summed over all its product groups: `productGroups`, `requestedCountries`, `days` (the 30d window per UTC day: requests, successes, success rate, `ipsSum`, `uniqueIps`) and per range the totals (`requests`, `successes`, `successRate`, `ipsSum`, `uniqueIps`, `latencyP50/P90/P99`), the top 50 `countries` and the full length of each place list (`lists`). Distinct IPs and latency cannot be added up: they are the group's own when the network has one product group in that category, else null. |
| `/v1/networks/all` | Every network together, in the same shape (`slug` and `kind` `all`, plus `networks`): requests, successes and place lists over every product group; `uniqueIps` (each exit IP once, however many networks or requested countries saw it, per range and per day) and the light-load latency percentiles from the snapshot's every-group `fleet` entry (cohort `*`, proxy-index `fleet_totals`); null when the snapshot has none, never a sum. Its `ipsSum` totals are null. `/v1/networks/all/places` pages its place lists like a network's. The slug `all` is reserved: a network named `all` would hide it. |
| `/v1/rankings?range=` | Every network in its final list order for one range (default `30d`): `position`, `rank` and `peers` (within its kind), `score`, `grade`, `previousRank`, the sub-score `breakdown` (profile `balanced`), and the list's numbers (`successRate`, `successDeltaPts`, `latencyP50`, `latencyDeltaPct`, `uniqueIps`, `countries`, `datacenterAsnShare`, `costPer1kSuccess`, `valueRank`, a 26-point `trend`). Measured networks by their score for that range, highest first; unscored ones after, by name. |
| `/v1/networks/{slug}/places?kind=&dimension=&range=&q=&page=` | One page (50 rows) of that network's `country`, `region`, `city` or `asn` list (default `country`, `30d`), most IPs first, with `total` and `pages`. `q` keeps rows whose code, names or `AS<number>` contain every word. |
| `/v1/countries` | Every country any network was seen in (30d): networks and `ipsSum`. |
| `/v1/countries/{cc}` | That country's row of every network and category per range, with `ipsSumRank` and `latencyRank` among the networks of the same category, and the city count per range. |
| `/v1/countries/{cc}/cities?range=&q=&page=` | One page of that country's cities across networks (each row names its network and category). |
| `/v1/version` | `{revision, builtAt}`: what open pages poll (through `/api/site/v1/version`) to learn that the data changed. `revision` is the hex SHA-256 of the `/v1/dataset` body, i.e. its ETag without quotes, so it changes exactly when the published data does (a rebuild with identical bytes keeps it) and a build from a `siteapi dump` file can compute it from the bytes. `public, max-age=5`; answer `If-None-Match` with `304`. |
| `/healthz` | `no-store`. `status` (`ok`, `degraded` after a failed rebuild, `starting`), version, build timings, fingerprint, `lastError`, `lastErrorAt`, cache sizes. |

Every answer is at most 200 KB (`build.MaxAnswerBytes`): `TestEveryAnswerFitsTheCap` renders a QA-1-sized
build (`internal/testscale`: 21 framework providers, 18 with 40 per-country product groups, the framework
targets, 559 test events and the site's own content) and fails when any answer or page is larger. Until
SNAP-SIZE-2 a temporary list in the test exempts `/v1/dataset` (the static build's input and what the
download dialog fetches), `/v1/overview`, `/v1/providers` and `/v1/providers/{slug}` (API views no page
fetches). A place row adds the requests of every product group that saw the place and keeps the latency
p50 of the row with the most successes; `ipsSum` adds each group's distinct IPs, so an IP seen by two
groups counts twice (an upper bound, not a distinct count). Capped lists are read whole from the build's
typed rows. Network, country, coverage and snapshot answers are computed and compressed once per build;
a long-list page is cut from a precomputed list on first request and kept in a 1,024-entry LRU per version.

`source` is `"demo"` or `"measured"` on providers, tape rows, changelog, reviews and
community runs. On targets it is `"measured"` for framework targets, `"demo"` for
fixture-only targets, and `"catalog"` for editorial ones.

## Caching

- **Versions.** A build reads everything in one `REPEATABLE READ, READ ONLY`
  transaction (`store.Load`), assembles every response (`build.Build`), then renders
  each one to immutable bytes. Each body is stored three ways: identity, gzip
  (level 9) and brotli (quality 11, or quality 10 with a 16 MB window above 512 KB).
  The new version is swapped in through an atomic pointer. Requests only look up
  bytes.
- **Headers.** `ETag` is a strong sha256 of the identity body. Each coding has its
  own tag (`"<sha>"`, `"<sha>-gz"`, `"<sha>-br"`), because the bytes differ. The
  headers also carry `Vary: Accept-Encoding`, `Content-Length`,
  `X-Content-Type-Options: nosniff`, and `X-Site-Version`. `If-None-Match` uses the
  weak comparison and matches any coding's tag (or `*`), returning `304` with no
  body. Every `/v1/*` response is
  `Cache-Control: public, max-age=60, stale-while-revalidate=600`. The exceptions
  are `/v1/events`, which is `public, max-age=15, stale-while-revalidate=60`, and
  `/v1/version`, which is `public, max-age=5`.
- **Change detection.** Every 10 s one cheap query reads a fingerprint:
  - `current_calculation.build_id`
  - `max(test_events.event_id)`
  - `site.revision`, which statement triggers on every site table bump, so deletes
    count too
  - the max `updated_at` across site tables
  - the demo provider count
  - the framework product count
  - a digest of `public.targets`

  A rebuild only happens when the fingerprint changed. It is single-flight: one build
  at a time, and waiters re-check rather than rebuild. The previous version keeps
  serving during a rebuild. A failed rebuild keeps it serving, is logged, and shows
  in `/healthz` (`status: degraded`, `lastError`).
- **Search.** `/v1/search` normalises the query (lowercase, collapsed whitespace),
  scores it against the in-memory index, and keeps the rendered response in a
  bounded LRU of 1,024 entries. The LRU is per version, so a rebuild cannot serve
  stale results.
- **Events.** The newest 2,000 events are held in memory, and past them the newest
  500 non-routine events (so a burst of hourly estimate releases never evicts a
  pulse down or an incident). The default query is pre-rendered. Other filters are
  answered from memory when the window provably contains the whole answer:
  - `limit` matches were found,
  - or the table has no more rows,
  - or `since` is newer than the window's oldest event;
  - and for a filter without `kind` or `category`, also the newest half-`limit`
    non-routine matches were found (or all of them are held).

  Otherwise a single read-only query runs. Either way the rendered response is kept
  in a 256-entry LRU for that version.
- **Measured (Apple Silicon, localhost).** Serving a cached `/v1/dataset` through the
  handler takes about 1.8 µs (`BenchmarkCachedDataset`). Over loopback HTTP with
  keep-alive and brotli bodies, p50 is about 0.15-0.2 ms and p99 about 0.5-0.7 ms
  for dataset, overview, a provider, search and events (`TestServeFromDatabase`
  asserts p99 < 2 ms). A full rebuild with the measured snapshot published takes
  about 1 s, mostly brotli on the 1.9 MB dataset.

## Database: the `site` schema

`siteapi migrate` applies the embedded `internal/store/migrations/NNNN_*.sql` in
one transaction under an advisory lock and records `site.schema_versions(version,
name, checksum)`. An applied migration whose checksum no longer matches the embedded
file is refused. Write a new migration instead of editing one, as the framework does.
`serve` and `dump` check the same checksums read-only and refuse to start on a
mismatch.

| Table | Holds |
|---|---|
| `site.datasets` | Fixture metadata (demo `generatedAt`, seed, runId, collectorVersion, windowHours, source sha256). |
| `site.targets` | Target presentation (label, category, pass condition, difficulty, order). Framework targets appear even without a row. |
| `site.scopes` | What is tested per product kind (methodology copy). |
| `site.score_weights`, `site.ranking_profiles` | Default composite weights and the alternative weightings. |
| `site.providers` | Provider presentation (slug PK, name, tagline, kinds, primary kind, measured product, hq, founded, claim, verdict, strengths, weaknesses, best_for, website, listed, sort order) plus `framework_provider` (= `public.providers.id`). The score columns and `headline_overlap` are used only by fixtures. |
| `site.provider_products` | Everything a provider sells, `measured`, url, price label, note, `framework_product` (= `public.products.name`). |
| `site.pricing`, `site.pricing_plans` | List pricing per category: billing model, $/GB entry and at scale, minimum commit, trial, cost per 1k success, average payload, cost basis, ranks, and the plans. |
| `site.trust_signals` | Trust per category: authenticity, DC ASN share, claim gap, blocklist, stickiness, leak checks, KYC, consent, `claimed_pool_size`. |
| `site.measurements` | Measurement documents (JSONB pool, performance, targets, series, IP samples, incidents, geo, ASNs, overlap) for providers without a framework measurement. Only the fixtures use it. |
| `site.reviews`, `site.community_runs`, `site.changelog`, `site.tape_events` | Records, with absolute timestamps (`posted_at`, `submitted_at`, `occurred_at`). The API turns them back into `agoSeconds`. |
| `site.revision` | Change counter bumped by triggers. |

Every row has `is_demo`. Columns the site sorts, filters or joins on are typed.
Nested demo-only documents are JSONB. There are no foreign keys into the framework
schema, so neither side can block the other's migrations. The runtime pool sets
`default_transaction_read_only=on`. Only `migrate`, `seed-demo` and `unseed-demo`
write, and only to `site.*`. They never drop or truncate a framework table.

### Making a real provider complete

A framework provider appears automatically when it has products or a published
snapshot product. Catalog rows fill what the framework does not measure:

```sql
-- Placeholders: replace every <...> with a verified value.
INSERT INTO site.providers(slug, name, framework_provider, hq, founded, claim, website, best_for)
VALUES ('<slug>', '<Display name>', '<public.providers.id>', '<City, Country>', <year>, '<claim being checked>', '<https://...>', '{"<use case>"}');
INSERT INTO site.provider_products(provider_slug, product_kind, name, measured, url, price_label, framework_product)
VALUES ('<slug>', 'residential_rotating', 'Rotating residential', true, '<https://...>', '<$X / GB>', '<public.products.name>');
INSERT INTO site.pricing(provider_slug, category, model, price_per_gb, avg_payload_kb, trial)
VALUES ('<slug>', 'residential', 'per_gb', <entry $/GB>, <KB per request>, '<trial terms>');
INSERT INTO site.trust_signals(provider_slug, category, kyc_verified, consent_disclosed, stickiness_advertised, claimed_pool_size)
VALUES ('<slug>', 'residential', <bool>, <bool>, '<TTL>', <claimed IPs>);
```

The next refresh (10 s at most) picks this up. `/v1/coverage` then lists only what
is still missing.

## Tests

```sh
cd api
go vet ./... && gofmt -l .
go test ./...                                                   # database tests skip with a message
PG_TEST_ADMIN_FILE=$PWD/../proxy-index/.secrets/postgres-admin.toml go test ./...   # full suite
PG_TEST_ADMIN_FILE=... go test -race ./...
go test -bench . -run XXX ./internal/server/
```

CI's `api` job runs `gofmt`, `go vet` and `go test ./...` on every ready pull request (and `-race` on `main`); its database tests skip until CI gets PostgreSQL and the framework schema.

Database tests create a throwaway `siteapi_test_<random>` database with the admin
credentials. They load the framework schema from `proxy-index/src/proxy_index/` (or
`PG_TEST_FRAMEWORK_DIR`) and drop the database afterwards. `proxy_reviews` is never
touched. The suite covers:

- `store`:
  - migrations are idempotent and checksummed
  - triggers bump the revision on insert and delete
  - seed and unseed are idempotent and leave the catalog intact
  - `pg_dump` backups are restorable
- `build`:
  - **demo round trip**: the API's dataset for the seeded fixtures equals
    `fixtures/demo/design-demo.json` field for field. Only documented additions are allowed, and
    the fixtures' only N/A is `website`.
  - **N/A manifest** for a real provider with partial data: a minimal v2 snapshot
    is published, with catalog rows and events. Every listed path is really
    null/empty, every null is listed, and the expected gaps and values are present.
  - unseed leaves an honest empty dataset
  - **measured only**: a database with the published snapshot and nothing else
    (staging before demo seeding) gives a measured fleet basis, empty cost boards
    and N/A price picks. `SITEAPI_TEST_DUMP=<file>` writes that `/v1/dataset`
    body, for an offline `SITE_API_DATASET_FILE` build of every page over it.
  - **parity with the site's TypeScript**: `scripts/reference.mjs` bundles
    `src/lib/{data,window,overview,tape}.ts` with the site's esbuild, with the
    site's API loader swapped for `src/lib/site-api.fixture.ts`
    (`presentationDataset` over `fixtures/demo/design-demo.json` and
    `fixtures/measured/snapshot.json`), so TypeScript is still compared with Go. The test
    compares `presentationDataset`, `windowStats` (every provider and range),
    `fleetWindows`, `costBoards`, `fleetViews` and `tapeFromEvents` with the Go
    port. It only allows the deviations listed in FIELDS.md. It runs three times:
    over the committed v1 fixture, over that fixture as v2 with outages and
    time-based uptime, and over `fixtures/measured/snapshot-every-request.json`
    (the framework's every-request shape: latency under load, list prices,
    per-IP shares). `REFERENCE_SNAPSHOT` points the TypeScript at the same file.
    It also checks how a 2-hour outage lands in incidents, bands and downtime,
    and that a priced measured network enters the cost boards. Skipped without node and `node_modules`.
- `server`:
  - headers, ETag per coding, 304 (strong, weak, other coding, `*`), HEAD, 405, 404
  - brotli and gzip bodies decode to the identity bytes
  - rebuild only on change
  - the background loop picks up changes
  - a failed rebuild keeps serving, and health reports it
  - single-flight
  - the search LRU (normalisation, hits, bound, reset per version)
  - events filters and validation, and the database fallback that runs only when
    needed
  - the DB-backed integration test: read-only pool, change pickup, and p99 latency
- `jsnum`: `toFixed`, `toLocaleString` and `JSON.stringify` number formatting
  checked against node's output.

The parity test reads the live `src/lib`, which is still changing. If it fails after
a site change, the Go port needs the same change, or FIELDS.md needs a new documented
deviation.

## Rollback

From least to most drastic:

1. **Remove the demo networks:** `siteapi unseed-demo`. This takes a backup first,
   then deletes every `is_demo` row.
2. **Restore the database as it was before seeding.** The backup from the first real
   seed is
   `proxy-index/.state/postgres/backups/20261001_163955_before_seed_demo/database.dump`.
   Its `backup.json` has the sha256 and the exact command. It was taken after
   `migrate`, so it holds an empty `site` schema. To restore it, stop anything using
   the database, then run from the website folder, with `<bin_dir>` taken from
   `proxy-index/.state/postgres/runtime.json`:

   ```sh
   '<bin_dir>/pg_restore' --clean --if-exists --no-owner \
     -h 127.0.0.1 -p 55432 -U proxy_reviews -d proxy_reviews \
     proxy-index/.state/postgres/backups/20261001_163955_before_seed_demo/database.dump
   ```

3. **Remove the site schema entirely.** It is owned by the API, and nothing in the
   framework references it: `DROP SCHEMA site CASCADE;`. Alternatively, restore
   `20261001_155607_before_site_api/database.dump`, the backup taken before any of
   this work, the same way.
4. **Code:** `git tag pre-site-api` (commit `0084db6`) marks the website before
   the API, when pages imported `src/data/design-demo.json` and `src/data/dataset.json`
   (both still in that commit, and now in `fixtures/`). The site needs the API to
   build since it switched over; a static build without one uses
   `SITE_API_DATASET_FILE=<siteapi dump output>`.

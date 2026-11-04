# siteapi

A Go service that returns every piece of data the Proxy Index website needs from the
`proxy_reviews` Postgres database. The website lives in its own repository and reads
nothing else: it fetches the dataset once per static build (`/v1/dataset` redirects
to the build's dataset file, a static file named by its hash; a long-lived site
process revalidates it every few seconds with `If-None-Match`), and its pages reach
`/v1/version`, `/v1/events`, `/v1/search-index` and the per-page
answers (`/v1/rankings`, `/v1/networks…`, `/v1/countries…`) through a same-origin
passthrough. Open pages poll `/v1/version` to update in place when the data changes.
`fixtures/` holds test input only.

- Its main output is the website's `Dataset` type (`src/lib/types.ts` in the website
  repository); `internal/model` is that contract in Go.
- Missing data is explicit. Every field is always present. A value the API does not
  have is JSON `null`, and every entity carries an `na` array listing the paths of its
  missing fields. `/v1/coverage` reports every gap with the reason and the table
  or column that would fill it. `api/FIELDS.md` maps every field to its source.
- There are no demo networks. Every network comes from the framework (its published
  snapshot and provider list) or from a catalog row in `site.providers`. Rows an earlier
  build loaded as `is_demo = true` (the four design networks) are never read; `seed-demo`
  and `unseed-demo` are gone.
- Every response is precomputed and precompressed in memory. Requests never touch
  the database (except an uncommon `/v1/events` filter, see below).

Module: `github.com/Zeus-Development/proxy-index-website/api`, Go 1.27.

## Commands

All commands find the database the same way: `DATABASE_URL`, else
`--database-file <toml>` (a `[postgres]` section like `proxy-index/.secrets/database.toml`),
else `proxy-index/.secrets/database.toml` in or above the working directory.

```sh
cd api && go build -o /tmp/siteapi ./cmd/siteapi && cd ..   # always build from the current checkout

/tmp/siteapi migrate                        # create/upgrade the "site" schema
/tmp/siteapi serve --listen 127.0.0.1:8090  # the cached API (read-only DB sessions)
/tmp/siteapi dump --out dataset.json        # write /v1/dataset to a file (an offline website build)
/tmp/siteapi dump --endpoint /v1/coverage --out coverage.json --pretty
```

Never run a prebuilt or older binary (`api/bin/` is ignored by git and nothing
rebuilds it): build from the current checkout first. An old binary serves only the routes it was built
with (other paths answer 404) and writes its own migration checksums. `/healthz`
`binary.revision` and the `siteapi starting` log line name the commit it was built from.

| Flag / env | Default | Used by |
|---|---|---|
| `--listen`, `SITEAPI_LISTEN` | `127.0.0.1:8090` | serve |
| `--database-file`, `SITEAPI_DATABASE_FILE`; `DATABASE_URL` wins | `proxy-index/.secrets/database.toml` | all |
| `--interval` | `10s` | serve: change-detection poll |
| `--data-period` | `1h` | serve: data update schedule (must divide 24h); data answers are cached until the published snapshot's next period (see Caching) |
| `--cache-floor` | `30s` | serve: data answer TTL while the next update is unknown or overdue |
| `--endpoint`, `--pretty` | `/v1/dataset` | dump |

### Where the site schema comes from

The `site` schema is created and upgraded only by `siteapi migrate`, from the
migrations embedded in the binary (`internal/store/migrations`). In production run it
with the new binary, as a role that may create tables in the database (the framework's
owner role), before starting `serve`; it is idempotent and takes an advisory lock.
Catalog rows are then written with SQL (`api/scripts/*.sql`, see "Site schema").

`serve` and `dump` need only the framework's schema. The site schema is read through the
catalog (`pg_class`/`pg_attribute`): a site table is read only when it and every column
the API selects exist. A missing schema, table or column is served as no rows, logged
once (`site tables missing; serving them empty`, again only when the set changes) and
listed in `/healthz` `siteGaps`; the status stays `ok`. Creating the schema under a
running server is picked up by the next fingerprint poll. Applied migration checksums
are checked by `migrate`, not by `serve`.

## Endpoints

All `GET` (and `HEAD`), JSON, `Access-Control-Allow-Origin: *`.

| Path | Body |
|---|---|
| `/v1/dataset` | `302 Found` to the current dataset file, `Location: files/dataset-<sha256>.json` (relative, so it also resolves under a proxy's path prefix), `public, max-age=5, s-maxage=5`. Browsers and `fetch` follow it; `If-None-Match` travels with the redirect and the file answers `304`. |
| `/v1/files/dataset-<sha256>.json` | The full `Dataset` (types.ts) plus `snapshot` and `na` (see FIELDS.md, "Additions"): a static file rendered (and compressed) once per data update, named by the SHA-256 of its bytes and cached for good (`public, max-age=31536000, immutable`). The current build's file and, until the next rebuild, the previous one are served as they are (a client may hold the old redirect for 5 s or race a rebuild); any older `dataset-<sha256>.json` name answers an uncached `302` to the current file, so a download or a build never gets a 404 because the data changed. Other names are 404. Not an answer: a download (the whole dataset) read in one piece. The website's old `/api/site/*` passthrough did not forward the redirect. |
| `/v1/events?product=&kind=&category=&since=&limit=` | `{events: TestEvent[]}`, newest first, the exact shape of the framework's `/v1/events` (the website's `TestEvent`). Uses the same validation: `limit` 1-500 (default 100), `category` census, pulse, incident or audit, `since` an exclusive ISO time. Without `kind` or `category` (the live tape's polls) routine rows (the census tab and `pulse_finished`) never push other rows off the page: the newest other rows (pulse down and recovered, incidents, audits) take up to half of `limit`, the newest rows of any kind fill the rest, newest first. A `kind` or `category` filter gets exactly the newest matching rows. |
| `/v1/search?q=` | `{query, tokens, total, sections: [{group, items: SearchItem[]}]}`, ranked like the website's command palette (same scoring, 16 results, 5 per group). An empty `q` gives the palette's default sections. |
| `/v1/search-index` | The precomputed index (`search.ts buildSearchIndex` without its articles: networks, pages, targets, actions, plus a new `Countries` group) and the palette's constants (`groupBonus` has no `Guides` group). Guides and network reviews are not searched, and the pages only the old website had (`/guides`, `/contribute`, `/disclosure`) are not listed, nor its "Download the raw dataset" action (`/data/design-preview.json`, the old site's demo file). |
| `/v1/coverage` | The N/A report index: summary counts, dataset-level gaps, per-target/scope/profile gaps, record counts, a per-field index of which providers lack it, and one entry (`id`, `name`, `source`, `naCount`) per provider. |
| `/v1/coverage/{slug}` | One provider's gaps grouped by section with a reason for each. |
| `/v1/networks` | The overview: one row per network (`slug`, `name`, `kind`, `kinds`, `rank`, `score`, `uniqueIps` (the headline group's distinct IPs), `countries`, `latencyP50`, `successRate`), in rank order. |
| `/v1/networks/{slug}?kind=` | One network and category (default its headline one), summed over all its product groups: `productGroups`, `requestedCountries`, `days` (the 30d window per UTC day: requests, successes, success rate, `ipsSum`, `uniqueIps`) and per range the totals (`requests`, `successes`, `successRate`, `ipsSum`, `uniqueIps`, `latencyP50/P90/P99`), the top 50 `countries` and the full length of each place list (`lists`). Distinct IPs and latency cannot be added up: they are the group's own when the network has one product group in that category, else null. It is also the network page's answer (it replaces `/v1/providers/{slug}`): `profile` (the provider's presentation, score and that category's measured blocks, without the geo and ASN lists, the place ranks, the histories and the raw snapshot product), `stats` (`windowStats` per range without its histories) and the network's newest published `reviews`, `communityRuns`, `changelog` and `tape` rows (at most 20 each, with their full counts in `records`). 404 for an unknown network or category. |
| `/v1/networks/{slug}/chart?kind=&range=` | That category's histories for one range (default `30d`): `success`, `availability`, `latency` (p50/p90/p99), `pool` (distinct IPs) and outage `bands`, as `windowStats` draws them (5-minute points for 24h, days otherwise; `t` in minutes before the build). For `all`, the every-group fleet series (success, light-load latency, distinct IPs per bucket; no availability or bands, which are per product). |
| `/v1/networks/all` | Every network together, in the same shape (`slug` and `kind` `all`, plus `networks`): requests, successes and place lists over every product group; `uniqueIps` (each exit IP once, however many networks or requested countries saw it, per range and per day) and the light-load latency percentiles from the snapshot's every-group `fleet` entry (cohort `*`, proxy-index `fleet_totals`); null when the snapshot has none, never a sum. Its `ipsSum` totals are null. `/v1/networks/all/places` pages its place lists like a network's. The slug `all` is reserved: a network named `all` would hide it. |
| `/v1/rankings?range=` | Every network in its final list order for one range (default `30d`): `position`, `rank` and `peers` (within its kind), `score`, `grade`, `previousRank`, the sub-score `breakdown` (profile `balanced`), and the list's numbers (`successRate`, `successDeltaPts`, `latencyP50`, `latencyDeltaPct`, `uniqueIps`, `countries`, `datacenterAsnShare`, `costPer1kSuccess`, `valueRank`, a 26-point `trend`). Measured networks by their score for that range, highest first; unscored ones after, by name. |
| `/v1/networks/{slug}/places?kind=&dimension=&range=&q=&page=` | One page (50 rows) of that network's `country`, `region`, `city` or `asn` list (default `country`, `30d`), most IPs first, with `total` and `pages`. `q` keeps rows whose code, names or `AS<number>` contain every word. |
| `/v1/countries` | Every country any network was seen in (30d): networks and `ipsSum`. |
| `/v1/countries/{cc}` | That country's row of every network and category per range, with `ipsSumRank` and `latencyRank` among the networks of the same category, and the city count per range. |
| `/v1/countries/{cc}/cities?range=&q=&page=` | One page of that country's cities across networks (each row names its network and category). |
| `/v1/version` | `{revision, builtAt}`: what open pages poll (through the website's passthrough) to learn that the data changed. `revision` is the hex SHA-256 of the dataset file (its ETag without quotes, and the hash in its name), so it changes exactly when the published data does (a rebuild with identical bytes keeps it) and a build from a `siteapi dump` file can compute it from the bytes. `public, max-age=5, s-maxage=5`; answer `If-None-Match` with `304`. |
| `/healthz` | `no-store`. `status` (`ok`, `degraded` after a failed rebuild or while `unserved` is not empty, `starting`), version, build timings, fingerprint, `lastError`, `lastErrorAt`, cache sizes, `unserved`, `siteGaps` and `binary`. `unserved` lists, after every build, each page that does not answer 200 among `/v1/networks`, `/v1/countries`, `/v1/rankings`, `/v1/networks/all` and, for every product group of the published snapshot, its network's `/v1/networks/{slug}`, `/chart` and `/places` (with and without `?kind=`); a product whose provider has no network is a line too, unless a catalog row hides it (`listed = false`). At most 50 lines, then `and N more`; the same list is logged as a warning (`published products without their network pages`). `siteGaps` lists the site tables (or columns) the database lacks and that are served empty (see "Where the site schema comes from"); informational, it does not degrade the status. `binary` is the git `revision`, `time` and `modified` flag the running siteapi was built from (null when built outside a checkout); `serve` logs it at start. |

Every answer is at most 200 KB (`build.MaxAnswerBytes`): `TestEveryAnswerFitsTheCap` renders a QA-1-sized
build (`internal/testscale`: 21 framework providers, 18 with 40 per-country product groups, the framework
targets and 559 test events) and fails when any answer or page is larger. It has no
exemptions: the dataset is not an answer but the file `/v1/dataset` redirects to (11.5 MB at the QA-1
scale; the test logs its identity, gzip and brotli sizes). `/v1/overview`, `/v1/providers`, `/v1/providers/{slug}` and `/v1/snapshot` are gone:
`/v1/rankings`, `/v1/networks/{slug}` and `/v1/networks/{slug}/chart` answer what a page needs. A place row adds the requests of every product group that saw the place and keeps the latency
p50 of the row with the most successes; `ipsSum` adds each group's distinct IPs, so an IP seen by two
groups counts twice (an upper bound, not a distinct count). Capped lists are read whole from the build's
typed rows. Network, country and coverage answers are computed and compressed once per build;
a long-list page is cut from a precomputed list on first request and kept in a 1,024-entry LRU per version.

The published snapshot is read in either of the framework's two stored forms
(`internal/store/parts.go`). A build stored before calculated schema v8 is one
`calculated_snapshots.payload` row. A build stored since (proxy-index
`calculated/parts.py`, contract K1) keeps a head there (every top-level key except
`products` and `runs`, plus `"parts": {"products": n, "runs": m}`) and its product
groups in `calculated_snapshot_parts`, one group per `product` row; its daily groups'
24-hour series hold only the 5-minute buckets with requests or availability. The API
reads the head, then the `product` rows in order, one at a time: each 24-hour series
gets its empty buckets back exactly as the framework writes them, and the groups go
where the single row has them, so the payload decodes as the same text and every answer
is byte for byte what it was. A build whose stored product rows differ from the head's
count, or whose stored point is off its window's 5-minute grid, fails the rebuild (the
old answers stay served). The `runs` rows are not read: no answer uses them.

`source` is `"measured"` on providers, tape rows, changelog, reviews and community
runs. On targets it is `"measured"` for framework targets and `"catalog"` for
editorial ones.

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
  body.
- **Cache lifetime.** Data answers (search, coverage and every page answer, `304`s
  included) are cached by browsers and the CDN until the next data update:
  `Cache-Control: public, max-age=N, s-maxage=N` and `Expires`, where N is the whole
  seconds until the update after the published snapshot is due. That is the end of the
  snapshot cutoff's (`as_of`) period on the UTC grid of `--data-period` (default `1h`,
  which must divide 24 h: a cutoff of 13:00 or 13:05 means the next update is due at
  14:00). While no snapshot is published, or once that time has passed and the new
  snapshot has not been loaded yet (the update is late), N is `--cache-floor` (default
  30 s), so the new snapshot shows within about 30 s plus the 10 s change detection.
  The CDN's copy never outlives the update; a browser that got its copy from the CDN
  late in that copy's life can keep it up to N more unless the CDN sends `Age`.
  Not everything in a data answer comes from the snapshot: the network answers' tape
  and changelog are built from test events and site rows, and catalog edits change
  names and texts. A catalog edit rebuilds the cache at once, but cached copies of the
  data answers show it only after they expire, at most at the next update. A new test
  event refreshes only `/v1/events` (see Change detection): the dataset's and the
  network answers' tapes keep the events of the last full build until the next one.
  `/v1/version` and the `/v1/dataset` redirect stay `public, max-age=5, s-maxage=5`, so
  open pages notice every data change within seconds and always get the current
  dataset file; the live tape follows events through `/v1/events`.
  `/v1/events` (the live tape) is `public, max-age=15, s-maxage=15`; the dataset file
  is `public, max-age=31536000, immutable` (its name changes with its bytes);
  `/healthz` and errors are `no-store`.
- **Change detection.** Every 10 s one cheap query reads a fingerprint:
  - `current_calculation.build_id`
  - `max(test_events.event_id)`
  - `site.revision`, which statement triggers on every site table bump, so deletes
    count too
  - the max `updated_at` across the site tables the API reads
  - which of those site tables exist (so a `siteapi migrate` is noticed)
  - the framework product count
  - a digest of `public.targets`

  A full rebuild only happens when the fingerprint's data part (everything but
  `max(test_events.event_id)`) changed. When only the newest event id changed, the
  events alone are read (one read-only transaction, the same 2,000 + 500 rows as a
  full read) and a new version is swapped in that shares every other rendered answer
  with the serving one: the dataset file, the network and page answers, search and
  coverage are not read, built, rendered or compressed again, and `/v1/events` (its
  default query pre-rendered) answers from the new events. A full rebuild re-renders
  every answer but keeps the serving version's precompressed body wherever the new
  bytes are identical (same SHA-256 and bytes, same `Cache-Control` and status), so
  only changed bodies are compressed again. Refreshes are single-flight: one at a
  time, and waiters re-check rather than rebuild. The previous version keeps serving
  during a refresh. A failed one keeps it serving, is logged, and shows in `/healthz`
  (`status: degraded`, `lastError`).
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
  in a 256-entry LRU for that version (a new event starts a new one).
- **Measured (Apple Silicon, localhost).** Serving the cached dataset file through the
  handler takes about 1.8 µs (`BenchmarkCachedDataset`). Over loopback HTTP with
  keep-alive and brotli bodies, p50 is about 0.15-0.2 ms and p99 about 0.5-0.7 ms
  for the dataset, rankings, a network, search and events (`TestServeFromDatabase`
  asserts p99 < 2 ms). A full rebuild with the measured snapshot published takes
  about 1 s, mostly brotli on the 1.9 MB dataset.

## Database: the `site` schema

`siteapi migrate` applies the embedded `internal/store/migrations/NNNN_*.sql` in
one transaction under an advisory lock and records `site.schema_versions(version,
name, checksum)`. An applied migration whose checksum no longer matches the embedded
file is refused. Write a new migration instead of editing one, as the framework does;
`TestAppliedMigrationsAreNeverEdited` fails when an embedded file's checksum changes.
`serve` and `dump` do not check checksums: they read whatever site tables and
columns exist (see "Where the site schema comes from"). One exception: `0001_site_schema.sql` changed twice in its header comment
only (the Go module path, 2026-10-24 and 2026-10-27), so its two earlier checksums
(`commentOnlyEdits` in `migrate.go`) are accepted as the same schema, and `migrate`
rewrites them to the current one (`rechecked` in its output).

| Table | Holds |
|---|---|
| `site.datasets` | Unused: metadata of the removed demo fixtures. Not read. |
| `site.targets` | Target presentation (label, category, pass condition, difficulty, order). Framework targets appear even without a row. |
| `site.scopes` | What is tested per product kind (methodology copy). |
| `site.score_weights`, `site.ranking_profiles` | Default composite weights and the alternative weightings. |
| `site.providers` | Provider presentation (slug PK, name, tagline, kinds, primary kind, measured product, hq, founded, claim, verdict, strengths, weaknesses, best_for, website, listed, sort order) plus `framework_provider` (= `public.providers.id`). The score columns and `headline_overlap` were used only by the removed demo fixtures and are not read. |
| `site.provider_products` | Everything a provider sells, `measured`, url, price label, note, `framework_product` (= `public.products.name`). |
| `site.pricing`, `site.pricing_plans` | List pricing per category: billing model, $/GB entry and at scale, minimum commit, trial, cost per 1k success, average payload, cost basis, ranks, and the plans. |
| `site.trust_signals` | Trust per category: authenticity, DC ASN share, claim gap, blocklist, stickiness, leak checks, KYC, consent, `claimed_pool_size`. |
| `site.measurements` | Unused: the removed demo fixtures' measurement documents. Not read. |
| `site.reviews`, `site.community_runs`, `site.changelog`, `site.tape_events` | Records, with absolute timestamps (`posted_at`, `submitted_at`, `occurred_at`). The API turns them back into `agoSeconds`. |
| `site.revision` | Change counter bumped by triggers. |

The API reads `site.targets`, `scopes`, `score_weights`, `ranking_profiles` (the
dataset), and `providers`, `provider_products`, `pricing`, `pricing_plans`,
`trust_signals`, `reviews`, `community_runs`, `changelog` and `tape_events` (the network
pages and the dataset). Every row has `is_demo`; rows with `is_demo = true` (left by the
removed `seed-demo`) are never read. Columns the site sorts, filters or joins on are
typed. There are no foreign keys into the framework schema, so neither side can block
the other's migrations. The runtime pool sets `default_transaction_read_only=on`. Only
`migrate` writes, and only to `site.*`; it never drops or truncates a framework table.
No migration drops the unused `site.datasets` and `site.measurements` or the leftover
`is_demo` rows yet.

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
  - a build stored in parts (head, product rows, sparse daily 24-hour series)
    reads as the same payload text stored whole, without its runs; a head whose
    part count does not match, or a point off the 5-minute grid, fails the read
  - triggers bump the revision on insert and delete
  - a database without the site schema loads with every site table empty and one
    gap; `migrate` changes the fingerprint
  - a missing table or column empties only that table and is named; `is_demo` rows
    are never read
- `build`:
  - a framework-only database (no site schema, nothing published) builds an honest
    empty dataset
  - **N/A manifest** for a real provider with partial data: a minimal v2 snapshot
    is published, with catalog rows and events. Every listed path is really
    null/empty, every null is listed, and the expected gaps and values are present.
  - leftover `is_demo` rows (a network, its pricing, a target, a tape row) are not
    served
  - **measured only**: a database with the published snapshot and nothing else
    (staging before any catalog rows) gives a measured fleet basis, no value ranks
    and N/A price picks. `SITEAPI_TEST_DUMP=<file>` writes that `/v1/dataset`
    body, for an offline `SITE_API_DATASET_FILE` build of every page over it.
  - **snapshot mapping**: the committed v1 fixture as v2 with outages and
    time-based uptime lands in incidents, bands and downtime as expected, and
    `fixtures/measured/snapshot-every-request.json` (the framework's every-request
    shape: latency under load, list prices, per-IP shares) maps its latency
    target, pricing, copy and value ranks. Until the website moved out, these
    also compared the Go port with the site's TypeScript; that parity test went
    with it.
- `server`:
  - every answer is byte for byte the same from a build stored whole and from the
    same build stored in parts with sparse daily 24-hour series: the every-request
    fixture (a census/pulse group and a daily copy) and the QA-1 scale (18 x 40;
    more than one jsonb row holds, so its whole form is the parts with every point)
  - headers, ETag per coding, 304 (strong, weak, other coding, `*`), HEAD, 405, 404
  - brotli and gzip bodies decode to the identity bytes
  - rebuild only on change
  - the background loop picks up changes
  - a failed rebuild keeps serving, and health reports it
  - single-flight
  - the search LRU (normalisation, hits, bound, reset per version)
  - events filters and validation, and the database fallback that runs only when
    needed
  - the DB-backed integration test: read-only pool, catalog rows in the network
    answer, change pickup, and p99 latency
  - a fresh database with one published product (site schema migrated by an older
    build, missing, or missing tables and a column) answers 200 on every page, logs
    the missing site tables once and lists them in `/healthz` `siteGaps`
- `jsnum`: `toFixed`, `toLocaleString` and `JSON.stringify` number formatting
  checked against node's output.

## Rollback

From least to most drastic:

1. **Restore the database as it was before the demo networks were first seeded.** The backup from the first real
   seed is
   `proxy-index/.state/postgres/backups/20261001_163955_before_seed_demo/database.dump`.
   Its `backup.json` has the sha256 and the exact command. It was taken after
   `migrate`, so it holds an empty `site` schema. To restore it, stop anything using
   the database, then run from this repository's folder, with `<bin_dir>` taken from
   `proxy-index/.state/postgres/runtime.json`:

   ```sh
   '<bin_dir>/pg_restore' --clean --if-exists --no-owner \
     -h 127.0.0.1 -p 55432 -U proxy_reviews -d proxy_reviews \
     proxy-index/.state/postgres/backups/20261001_163955_before_seed_demo/database.dump
   ```

2. **Remove the site schema entirely.** `serve` keeps serving the framework's networks
   without it. It is owned by the API, and nothing in the
   framework references it: `DROP SCHEMA site CASCADE;`. Alternatively, restore
   `20261001_155607_before_site_api/database.dump`, the backup taken before any of
   this work, the same way.
3. **Code:** `git tag pre-site-api` (commit `0084db6`) marks the website before
   the API, when pages imported `src/data/design-demo.json` and `src/data/dataset.json`.
   The Astro website was removed from this repository afterwards; its last
   version here is the parent of the commit that removed it.

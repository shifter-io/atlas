# siteapi

A Go service that returns every piece of data the Proxy Index website needs from the
`proxy_reviews` Postgres database. The website lives in its own repository and reads
nothing else: it fetches the dataset once per static build (`/v1/dataset` redirects
to the build's dataset file, a static file named by its hash; a long-lived site
process revalidates it every few seconds with `If-None-Match`), and its pages reach
`/v1/version`, `/v1/events`, `/v1/search-index` and the per-page
answers (`/v1/rankings`, `/v1/networks…`, `/v1/countries…`) through a same-origin
passthrough. Open pages poll `/v1/version` to update in place when the data changes.
`api/fixtures/` holds test input only.

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
- Every response is precomputed and precompressed in memory, except the dataset,
  which is a precompressed file on disk (`--files-dir`). Requests never touch the
  database (except an uncommon `/v1/events` filter, see below).

Module: `github.com/Zeus-Development/proxy-index/api`, Go 1.27.3.

## Commands

All commands find the database the same way: `DATABASE_URL`, else
`--database-file <toml>` (a `[postgres]` section like `.secrets/database.toml`),
else `.secrets/database.toml` in the repository root: the working directory or its
nearest parent that holds the framework tree `src/proxy_index/`.

```sh
cd api && go build -o /tmp/siteapi ./cmd/siteapi && cd ..   # always build from the current checkout

/tmp/siteapi migrate                        # create/upgrade the "site" schema
/tmp/siteapi serve --listen 127.0.0.1:8090  # the cached API (read-only DB sessions)
/tmp/siteapi dump --out dataset.json        # write /v1/dataset to a file (an offline website build)
/tmp/siteapi dump --endpoint /v1/coverage --out coverage.json --pretty
/tmp/siteapi explain --database-file .secrets/database.toml --out /tmp/explain   # time every API statement
/tmp/siteapi bench --base-url http://127.0.0.1:8090 --out /tmp/bench             # time every page's requests
/tmp/siteapi bench --base-url http://127.0.0.1:8090 --out /tmp/bench --gaps ../docs/api-web-map-gaps.json  # and check every map gap
```

Never run a prebuilt or older binary (`api/bin/` is ignored by git and nothing
rebuilds it): build from the current checkout first. An old binary serves only the routes it was built
with (other paths answer 404) and writes its own migration checksums. `/healthz`
`binary.revision` and the `siteapi starting` log line name the commit it was built from.

| Flag / env | Default | Used by |
|---|---|---|
| `--listen`, `SITEAPI_LISTEN` | `127.0.0.1:8090` | serve |
| `--database-file`, `SITEAPI_DATABASE_FILE`; `DATABASE_URL` wins | `.secrets/database.toml` in the repository root | all |
| `--interval` | `10s` | serve: change-detection poll |
| `--data-period` | `1h` | serve: data update schedule (must divide 24h); data answers are cached until the published snapshot's next period (see Caching) |
| `--cache-floor` | `30s` | serve: data answer TTL while the next update is unknown or overdue |
| `--files-dir`, `SITEAPI_FILES_DIR` | `/var/lib/proxy-index-api/files` | serve: where the dataset file is written and served from (created if missing; in production inside the unit's only writable path) |
| `--endpoint`, `--pretty` | `/v1/dataset` | dump |
| `--out` | (required) | dump: the output file; explain, bench: the output directory |
| `--base-url` | (required) | bench: the running siteapi |
| `--warm`, `--concurrency`, `--budgets` | `200`, `8`, none | bench: warm requests per request, workers, a budgets file |

### Timing the API's statements: `siteapi explain`

`siteapi explain --out <dir>` runs every SQL statement shape the API runs against the
database with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, in one REPEATABLE READ read-only
transaction. Run it as the API's read-only role: a role that can write (a superuser, or
one with `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` on a `public` or `site` table) is
refused. The statements are the registry `store.Statements()`, built from the same SQL the
call sites run: the poll (site layout, fingerprint), the full rebuild in `LoadLists`
(site tables, framework tables, snapshot head, network list counts), the v4 network-list
row and label reads of a row-cache miss, and the events reads (the load's window and
notable reads, `LoadEvents`, and the `/v1/events` database fallback for the default,
product, kinds, category and since filters), then each page answer's own statements
(`store.RegisterStatement`, `api/answers/`). The API reads no build before v4, so
there is no statement shape for one.

The arguments come from the current build: the network list with the most rows (and its
network's provider for the product filter), its largest dimension's rows, the label ids
of those rows and of its city and ASN rows, and the oldest of the 2,000 newest events
(the notable read's cursor and the since filter). The largest place, country and a
city in it are reported with them.

- `explain.json`: the parameters, then per statement its registry name, call site, SQL,
  planning and execution time, rows, shared buffers hit and read, and the top plan nodes
  (the root and its children); `skipped` or `error` when it did not run.
- `explain.md`: the timed statements slowest first; every one over 100 ms is flagged, and
  every one over 3 s (10% of the API's 30 s `statement_timeout`) is flagged as such; then
  the skipped and failed ones.

A statement that fails runs in a savepoint and is reported; the others still run.

### Timing every page's requests: `siteapi bench`

`siteapi bench --base-url <url> --out <dir> [--warm 200] [--concurrency 8] [--budgets <file>] [--gaps <file>]`
measures a running siteapi from the outside, the way the website calls it. It reads
`/healthz` (the last build's load, assemble, render and total times, and the version's age),
`/v1/version` (the revision) and `/v1/networks` and `/v1/countries` (the real slugs, kinds
and a country code for the list search), then expands every page's request list.

The pages are `bench.Specs` (`internal/bench/pages.go`), the "After section 11" column of
`docs/api-web-map.md` section 10: each page's existing routes, plus the `Samples` of every
registered answer (`api/answers/`) whose path the page draws (`/v1/home`, `/v1/targets`,
`/v1/meta`, `/v1/overlap`, `/v1/events/stats`, `/v1/networks/{slug}/...`). The network
page is one page per network and kind. A sample's `{slug}` segment (by its `Route`) is set
to the page's network, its `kind=` to the page's kind, and a sample with `range=` is
requested once per range of the page; a sample no page draws goes to the page "other
registered answers". The same specs are what PAGE-LATENCY-1's CI test expands, so the two
never disagree. The dataset file is not requested: no page needs it after section 11.

Every distinct request is sent once cold (in order, on one keep-alive connection, after
`/v1/version`: the first request since the current version appeared, unless someone else
asked first, hence the version's age in the report), once with `Accept-Encoding: identity`
for its size, then `--warm` times over `--concurrency` workers, each with its own
keep-alive transport, with `Accept-Encoding: br`. It records the status, the identity and
encoded bytes, the cold time and the warm p50, p95, p99 (nearest rank) and max, each time
including reading the whole body. A page's figures are the sums over its requests.

Budgets (ms) are PAGE-LATENCY-1's CI budgets plus 20 ms of network time per request:

| Class | Requests | Cold | p99 |
|---|---|---|---|
| `answer` | a precomputed answer (every other route) | 22 | 22 |
| `events` | `/v1/events` | 22 | 22 |
| `search` | `/v1/search?q=` | 25 | 25 |
| `list` | a long-list page (`/v1/networks/{slug}/places` and `/coverage`, `/v1/countries/{cc}/cities` and `/regions`, `/v1/places/...`): cold is its first render | 70 | 22 |
| `list-search` | the same with `q=` | 70 | 70 |

A page's p99 sum may be 10 ms plus 20 ms per request. A `--budgets` JSON file is laid over
the defaults; a class or request it names replaces that budget whole, and a 0 (or missing)
limit is not checked:

```json
{"classes": {"answer": {"coldMs": 30, "p99Ms": 25}},
 "requests": {"/v1/events?limit=200": {"coldMs": 40, "p99Ms": 30}},
 "pageMs": 10, "pageRequestMs": 20}
```

- `bench.json`: the run (base URL, revision, whether the version changed during the run),
  the build timings, every page and every request with its budget and why it failed.
- `bench.md`: the build timings, every request or page over its budget, the pages slowest
  first, then every request.

Both are written to `<dir>` (created mode 700), mode 600. The command exits 1 when a
request or page is over its budget or a request does not answer 200 (a 404 or 500 fails),
0 otherwise.

#### Every map gap, pass or fail: `--gaps`

`siteapi bench ... --gaps docs/api-web-map-gaps.json` first checks every gap of
`docs/api-web-map.md`. The manifest has one entry per gap ID of the map's "Gaps summary"
lists (`tests/test_api_web_map_gaps.py` keeps it in step with the map): its `page` (section),
its `class` and how it is closed:

- `api`: the work item (`work`, `W1` to `W12`) and its `issue`, and `checks`, each a `request`
  template over `{slug}`, `{kind}`, `{range}` and `{cc}` and the `paths` its answer must have;
- `website`, `owner`: the section 12 row and its recommendation, as the `reason`;
- `withdrawn`: the section 13 line that withdrew it, as the `reason`.

A check's request is expanded as the bench expands page requests: `{slug}` for every network
(with `{kind}`, every kind of it), `{kind}` alone for every kind once, `{range}` for each range,
`{cc}` for the first country of `/v1/countries`. Every request is sent once. A path uses dots
for keys, `[]` for every element of a list and `*` for every key of an object (but `na`), such
as `networks[].cells.*.engines.*.scores.success.status`. It passes in an answer when it reaches
a value at least once (a list element or key without it is skipped, an empty list reaches
nothing) and every null it reaches is explained: listed in the `na` of an object around it
(by its path from that object, list positions and `[<id>]` keys compared as `[]`), or null by
contract, which the path marks with a trailing `?`. `match` gives a regexp every value at a
path must match (a number as its JSON text), such as `^residential$` for a `kind=` filter.

A gap passes when every path passes in every answer of its checks; it fails with the first
failing request of each path and why (`missing`, `null at <path>, not listed in na`, a value
that does not match, or a request that does not answer 200). `gaps.json` and `gaps.md` are
written beside `bench.json` and `bench.md`: the counts, then every gap with its verdict and
evidence; `website`, `owner` and `withdrawn` gaps are listed with their reason and no verdict.
The command also exits 1 when a gap fails. An issue whose PR names a field differently from
the manifest updates its entries in the same PR.

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
| `/v1/files/dataset-<sha256>.json` | The data the site's pages use, so a download regenerates them: the `Dataset` (types.ts) plus `snapshot` and `na` (see FIELDS.md, "Additions"), `pages`, the body of every page answer the site draws by its request, the country answers included (FIELDS.md, "Page answers in the dataset file"), and `lists`: of every network list (country, region, city, ASN and City × ASN) its first page as compact arrays, its exact `total` and its page count; every further page, search and cross-network list is read from the API (DATASET-SIZE-2; FIELDS.md, "The dataset file of a v4 build"). At 18 networks × 249 countries it is about 9.2 MB, gzip 0.5 MB (at most 15 MB, `TestTheRunADatasetFileIsSmallAndQuickToWrite`). The raw snapshot product (`measurement`) and the place ranks (`placeRanks`), which no page uses, are not in it (DATASET-SIZE-1). It is a static file on disk with its `.gz` and `.br` (see "The dataset file"), named by the SHA-256 of its bytes and cached for good (`public, max-age=31536000, immutable`). The current file and, for 10 minutes after it was replaced, the one before are served as they are (a client may hold the old redirect for 5 s or race a replacement); any other `dataset-<sha256>.json` name answers an uncached `302` to the current file, so a download or a build never gets a 404 because the data changed. Other names are 404. Not an answer: a download (the whole dataset) read in one piece. The website's old `/api/site/*` passthrough did not forward the redirect. |
| `/v1/events?product=&kind=&category=&since=&limit=` | `{events: TestEvent[]}`, newest first, the exact shape of the framework's `/v1/events` (the website's `TestEvent`). Uses the same validation: `limit` 1-500 (default 100), `category` census, pulse, incident or audit, `since` an exclusive ISO time. Without `kind` or `category` (the live tape's polls) routine rows (the census tab and `pulse_finished`) never push other rows off the page: the newest other rows (pulse down and recovered, incidents, audits) take up to half of `limit`, the newest rows of any kind fill the rest, newest first. A `kind` or `category` filter gets exactly the newest matching rows. |
| `/v1/search?q=` | `{query, tokens, total, sections: [{group, items: SearchItem[]}]}`, ranked like the website's command palette (same scoring, 16 results, 5 per group). An empty `q` gives the palette's default sections. |
| `/v1/search-index` | The precomputed index (`search.ts buildSearchIndex` without its articles: networks, pages, targets, actions, plus a new `Countries` group) and the palette's constants (`groupBonus` has no `Guides` group). Guides and network reviews are not searched, and the pages only the old website had (`/guides`, `/contribute`, `/disclosure`) are not listed, nor its "Download the raw dataset" action (`/data/design-preview.json`, the old site's demo file). |
| `/v1/coverage` | The N/A report index: summary counts, dataset-level gaps, per-target/scope/profile gaps, record counts, a per-field index of which providers lack it, and one entry (`id`, `name`, `source`, `naCount`) per provider. |
| `/v1/coverage/{slug}` | One provider's gaps grouped by section with a reason for each. |
| `/v1/networks` | The overview: one row per network (`slug`, `name`, `kind`, `kinds`, `rank`, `score`, `uniqueIps` (the headline group's distinct IPs), `countries`, `latencyP50`, `successRate`), in rank order. |
| `/v1/networks/{slug}?kind=` | One network and category (default its headline one), summed over all its product groups: `productGroups`, `requestedCountries`, `days` (the 30d window per UTC day: requests, successes, success rate, `ipsSum`, `uniqueIps`) and per range the totals (`requests`, `successes`, `successRate`, `ipsSum`, `uniqueIps`, `latencyP50/P90/P99`), the top 50 `countries` and the full length of each place list (`lists`). Distinct IPs and latency cannot be added up: they are the group's own when the network has one product group in that category, else null. It is also the network page's answer (it replaces `/v1/providers/{slug}`): `profile` (the provider's presentation, score and that category's measured blocks, without the geo and ASN lists, the place ranks, the histories and the raw snapshot product), `stats` (`windowStats` per range without its histories); on a browser target's entry of `profile.targets`, `scores` (`success`, `challenge`, `block`, each `{score, rank, rankedOf, status}`: a 1-10 score, the rank among the `rankedOf` ranked networks (M), and `status` `ranked`, `not_enough_data`, `too_few_networks` or `all_similar`; null for a snapshot without them) and `engines.<engine>.scores`, and on `profile` the network's `browserOverall.<engine>.success` (`{score, rank, rankedOf, status, targets, weights}`, not written when the snapshot has none; FIELDS.md, "Browser scores") and the network's newest published `reviews`, `communityRuns`, `changelog` and `tape` rows (at most 20 each, with their full counts in `records`). 404 for an unknown network or category. |
| `/v1/networks/{slug}/chart?kind=&range=` | That category's histories for one range (default `30d`): `success`, `availability`, `latency` (p50/p90/p99), `pool` (distinct IPs) and outage `bands`, as `windowStats` draws them (5-minute points for 24h, days otherwise; `t` in minutes before the build). For `all`, the every-group fleet series (success, light-load latency, distinct IPs per bucket; no availability or bands, which are per product). |
| `/v1/networks/all` | Every network together, in the same shape (`slug` and `kind` `all`, plus `networks`): requests, successes and place lists over every product group; `uniqueIps` (each exit IP once, however many networks or requested countries saw it, per range and per day) and the light-load latency percentiles from the snapshot's every-group `fleet` entry (cohort `*`, proxy-index `fleet_totals`); null when the snapshot has none, never a sum. Its `ipsSum` totals are null. `/v1/networks/all/places` pages its place lists like a network's. The slug `all` is reserved: a network named `all` would hide it. |
| `/v1/rankings?range=` | Every network in its final list order for one range (default `30d`): `position`, `rank` and `peers` (within its kind), `score`, `grade`, `previousRank`, the sub-score `breakdown` (profile `balanced`), and the list's numbers (`successRate`, `successDeltaPts`, `latencyP50`, `latencyDeltaPct`, `uniqueIps`, `countries`, `datacenterAsnShare`, `costPer1kSuccess`, `valueRank`, a 26-point `trend`). Measured networks by their score for that range, highest first; unscored ones after, by name. |
| `/v1/networks/{slug}/places?kind=&dimension=&range=&q=&page=` | One page (50 rows) of that network's `country`, `region`, `city` or `asn` list (default `country`, `30d`), most IPs first, with `total` and `pages`. `q` keeps rows whose code, names or `AS<number>` contain every word. |
| `/v1/countries` | Every country any network was seen in (30d): networks and `ipsSum`. |
| `/v1/countries/{cc}` | That country's row of every network and category per range, with `ipsSumRank` and `latencyRank` among the networks of the same category, and the city count per range. |
| `/v1/countries/{cc}/cities?range=&q=&page=` | One page of that country's cities across networks (each row names its network and category). |
| `/v1/countries/{cc}/regions?range=&q=&page=` | A v4 build only: one page of that country's regions across networks, as `/cities`. |
| `/v1/networks/{slug}/coverage?kind=&range=&q=&page=` | A v4 build only: one page (50 rows) of the network's City × ASN rows with IPs, most IPs first, with `total`, `pages` and, with `q`, `totals` (FIELDS.md, "Payload v4 list answers"). |
| `/v1/places/region/{key}?range=` and `/v1/places/city/{key}?range=` | A v4 build only: every network's row in that region or city, ranked among the networks of the same kind; 404 for an unknown key. |
| `/v1/version` | `{revision, builtAt}`: what open pages poll (through the website's passthrough) to learn that the data changed. `revision` is the hex SHA-256 of the dataset file (its ETag without quotes, and the hash in its name), so it changes exactly when a new dataset file is written (see "The dataset file"; a rebuild with identical bytes keeps it) and a build from a `siteapi dump` file can compute it from the bytes. `public, max-age=5, s-maxage=5`; answer `If-None-Match` with `304`. |
| Page answers added from Phase 3 | See `api/answers/`: each one's route, query and body in `api/answers/<name>.md`. |
| `/healthz` | `no-store`. `status` (`ok`, `degraded` after a failed rebuild or while `unserved` is not empty, `starting`, `unavailable` while the current build is older than payload v4), version, build timings, fingerprint, `lastError`, `lastErrorAt`, `reason`, cache sizes, `unserved`, `siteGaps` and `binary`. `reason` is null unless nothing serves because the current build is older than payload v4: then it is `calculation older than payload v4: recalculate`, `/healthz` answers 503 like every data route (each with that reason as its `error`), and the answers of an earlier v4 build are no longer served; `serve` starts anyway, and the next v4 build serves again. `unserved` lists, after every build, each page that does not answer 200 among `/v1/networks`, `/v1/countries`, `/v1/rankings`, `/v1/networks/all` and, for every product group of the published snapshot, its network's `/v1/networks/{slug}`, `/chart` and `/places` (with and without `?kind=`); a product whose provider has no network is a line too, unless a catalog row hides it (`listed = false`). At most 50 lines, then `and N more`; the same list is logged as a warning (`published products without their network pages`). `siteGaps` lists the site tables (or columns) the database lacks and that are served empty (see "Where the site schema comes from"); informational, it does not degrade the status. `binary` is the git `revision`, `time` and `modified` flag the running siteapi was built from (null when built outside a checkout); `serve` logs it at start. |

Every answer is at most 200 KB (`build.MaxAnswerBytes`): `TestEveryAnswerFitsTheCap` renders the QA-1
build as payload v4 (`internal/testscale`: 21 framework providers, 18 networks each one summary over 40
requested countries, the framework targets and 559 test events) and fails when any answer or page is larger. It has no
exemptions: the dataset is not an answer but the file `/v1/dataset` redirects to. That file must stay at most
12 MB, gzip at most 2 MB, at the QA-1 scale (about 9.0 MB, gzip 0.5 MB, with 2,000 City × ASN rows per network
and range; the test logs its identity, gzip and brotli sizes). It holds each list's first page only, so real
networks' longer lists do not make it larger. `/v1/overview`, `/v1/providers`, `/v1/providers/{slug}` and `/v1/snapshot` are gone:
`/v1/rankings`, `/v1/networks/{slug}` and `/v1/networks/{slug}/chart` answer what a page needs. A place row adds the requests of every network list that saw the place and keeps the latency
p50 of the row with the most successes; `ipsSum` adds each list's distinct IPs, so an IP seen by two
networks counts twice (an upper bound, not a distinct count). Lists are read whole from the network
lists. Network, country and coverage answers are computed and compressed once per build;
a long-list page is cut from a precomputed list on first request and kept in a 1,024-entry LRU per version.

The API reads only payload v4 builds (`internal/store/parts.go`, below). The
framework (proxy-index `calculated/parts.py`, contract K1) keeps a build's head in
`calculated_snapshots.payload` and its product groups in `calculated_snapshot_parts`;
the API reads the head and never the product rows. A current build before v4 (one
`calculated_snapshots.payload` row, or a head with `schema_version` below 4 or without
`networks`) fails the load with `store.ErrBeforeV4` before its payload, product parts or
lists are read. The API then serves nothing: every data route answers 503 and
`/healthz` gives the reason `calculation older than payload v4: recalculate` (see
`/healthz` above) until a v4 build is current. The website reads v4 dataset files only.

The long lists are typed rows (`internal/store/lists.go`): from calculated schema v9
(contract K2), the shared `calculated_list_rows` (by `list_id`). One query per set of
ids filters `list_id = ANY($1) AND dimension = ANY($2)` on the stored dimension code, by
the primary key, and the rows' JSON is written in Go. Lists are deleted, never dropped,
so a read's snapshot keeps seeing the rows of the build it read.

A payload v4 build (contract K9: `schema_version` 4 and the head's `networks`, one
summary per provider and proxy kind over all its product groups) is read from two
things only (`internal/store/network.go`): its head, through a SQL projection that
leaves out the `parts` count, every `overlap` row and the per-cohort `fleet` rows (it
keeps the every-group `*` entries), and the network lists its windows name (contract
K10, `store.NetLists`: each list's row counts, held between rebuilds like the group
lists, which a v4 read drops; the rows stay in the database). No product part and no group list is read. Each network is then given to the
build as one product group (`cohort` `network`, `id` `<provider>:<kind>:network`),
so every answer is made by the build's product-group code. Each window gets from its list its `breakdowns.country` and
`pool.countries_breakdown` (every country), `breakdowns.city_asn` (every City × ASN
row), `pool.asns_breakdown` (the 50 ASNs with most IPs) and
`breakdown_entries`; the snapshot's overlap rows are made from its `overlap.peers`; a network with one
requested country gets it as `criteria.country`. The build serves its place,
country and Coverage lists from the compact rows (`build/placelists.go`).

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
  event refreshes only `/v1/events` (see Change detection): the network answers'
  tapes keep the events of the last full build until the next one, the dataset
  file's those of the build that last wrote it.
  `/v1/version` and the `/v1/dataset` redirect stay `public, max-age=5, s-maxage=5`, so
  open pages notice every data change within seconds and always get the current
  dataset file; the live tape follows events through `/v1/events`.
  `/v1/events` (the live tape) is `public, max-age=15, s-maxage=15`; the dataset file
  is `public, max-age=31536000, immutable` (its name changes with its bytes);
  `/healthz` and errors are `no-store`.
- **Change detection.** Every 10 s one cheap query reads a fingerprint:
  - `current_calculation.build_id` and that build's `created_at`
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
  only changed bodies are compressed again; the dataset file is written only when its
  marker changed (see "The dataset file"). Refreshes are single-flight: one at a
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
- **Network lists** (contracts K10, K11; read for a payload v4 build; API-MEM-1).
  `store.NetLists` holds only each network window's list id and row count per
  dimension, reading the counts only of list ids it does not hold. A dimension's rows
  are read from `calculated_list_rows` (by list id, in position order, in a read-only
  transaction of their own) when the build, a page or a search needs them, as 24-byte rows (IPs, completed, successes, p50 and indexes into place
  and ASN names shared by every list; at most 40 MB per million rows,
  `TestNetListsMemory`), and the rows read last stay in a 64 MB cache (least recently
  used first out). Most-IPs-first order, every-word search and totals run over those
  rows, so answers are as before. A list keeps its rows while its build is retained
  (contract K2 deletes lists, never changes them), so a page of the build being served
  during a rebuild reads its own rows; a read that finds fewer rows than the list had
  at its load fails, and the page answers 500.
- **The dataset file** (contract K4). `/v1/dataset` redirects to a file in
  `--files-dir`, written with its `.gz` and `.br` only when its marker changed: the
  published build's `created_at`, the site tables' newest `updated_at`,
  `site.revision` (which also counts deletes), and the framework product count and
  targets digest (the dataset's targets and providers' products come from them). A
  rebuild for anything else (a new event) keeps the file, and so the `/v1/version`
  revision. The marker is part of the fingerprint, so it is checked every poll. The
  file is never held whole in memory: its JSON is written one provider at a time
  (`build.WriteDataset`, byte for byte what encoding the whole `Dataset` gives; a v4
  build's `lists` one list's first page at a time, from the pages the build keeps,
  so no list row is read), then compressed from disk (gzip level 6, brotli quality 5
  with a 4 MB window, in parallel; at 18 networks × 249 countries the whole write
  takes about 0.3 s, at most 2 s). Each file is written as a `.tmp` (mode 0644), fsynced and renamed. A
  replaced file is still served for 10 minutes after its replacement, then deleted
  (checked every poll). `dataset.meta.json` records the marker, the code that wrote
  the file (the binary's git revision) and the files, so a restart reuses a file whose
  marker and code match (after checking its three sizes); a new binary, or one built
  from a modified tree or without git information, writes the file again, so a deploy
  that changes the dataset's encoding takes effect at once. A restart also removes
  what an interrupted write left, so run one `serve` per files directory. A failed
  write fails the whole refresh: every answer stays at the serving version (so
  `/v1/version` always names the served file) until a write succeeds, and `/healthz`
  shows it like a failed rebuild. `TestWritingTheDatasetFileHasBoundedMemory` builds
  a small synthetic shape with QA-1's per-network content (240 exit countries per
  window) and repeats its providers to at least 90 MB, a stand-in for 18 networks x
  249 countries, which the synthetic generator cannot build within a test machine's
  memory: encoding its JSON adds at most 32 MB of heap (holding the file would take
  all of it), and the whole write adds at most 160 MB (most of it brotli's state).
  `TestWritingAV4DatasetFileHasBoundedMemory` does the same for a v4 file: QA-1's v4
  build with its lists repeated (as more networks) to at least 90 MB of rows, within
  the same two bounds; it logs the seconds per MB of the JSON write and of each coding.
- **Measured (Apple Silicon, localhost).** `BenchmarkCachedDataset` measures serving
  the dataset file through the handler (not measured since the file moved to disk).
  Over loopback HTTP with
  keep-alive and brotli bodies, p50 is about 0.15-0.2 ms and p99 about 0.5-0.7 ms
  for the dataset, rankings, a network, search and events (`TestServeFromDatabase`
  asserts p99 < 2 ms; CI runs it alone, see `AGENTS.md`). A full rebuild with the measured snapshot published took
  about 1 s, mostly brotli on the 1.9 MB dataset, before the dataset became a file
  written only on a new marker.

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
PG_TEST_ADMIN_FILE=$PWD/../.secrets/postgres-admin.toml go test ./...   # full suite
PG_TEST_ADMIN_FILE=... go test -race ./...
go test -bench . -run XXX ./internal/server/
```

CI's `api` workflow (`.github/workflows/api.yml`) runs `gofmt` and `go vet`, and `go test` with the database tests against a PostgreSQL 17 container and the framework schema of the same checkout, on every ready pull request that changes `api/`, the framework's SQL (`src/proxy_index/**/*.sql`) or the workflow, and again on every merge-queue test (the `merge_group` event; it skips the build and tests when the entry changes none of those files). It is one job on its own fresh 32-CPU CI server with all its work in parallel; nothing runs on pushes to `main`, which only ever gets what the merge queue tested ("How a change reaches main" in `AGENTS.md`). `go test -race` without the database runs nightly on `main` and on dispatches, in its own step after the normal tests (so their time and limits count only them); it reports its time and gates no merge. `PG_TEST_REQUIRED=1` makes a missing `PG_TEST_ADMIN_FILE` fail the database tests instead of skipping them. Its tests run with `go test -json`; the job summary lists the 20 slowest tests and subtests (QA-1's included) with the test steps' time, the `api-timing` artifact keeps them 90 days (`python3 scripts/ci/timing_history.py --workflow api`), a test over 60 s or test steps over 90 s warn, and a test of 90 s or more or test steps over 240 s fail the job (`scripts/ci/timing.py`).

Database tests create a throwaway `siteapi_test_<random>` database with the admin
credentials. They load the framework schema (`schema.sql`, `calculated/schema.sql`) from
`src/proxy_index/` in the repository root, so they always test the framework's current
schema, and drop the database afterwards. `proxy_reviews` is never
touched. The framework schema is calculated v10 (v9 and its memo tables). The suite covers:

- `store`:
  - migrations are idempotent and checksummed
  - a build before payload v4 (a v3 head in parts, a head of `schema_version` 4
    without `networks`, a whole v2 payload) is `store.ErrBeforeV4`, and its load
    reads neither its product parts nor any list
  - triggers bump the revision on insert and delete
  - a database without the site schema loads with every site table empty and one
    gap; `migrate` changes the fingerprint
  - a missing table or column empties only that table and is named; `is_demo` rows
    are never read
  - `siteapi explain`, through a read-only role on a v4 build: every registered
    statement runs and appears in `explain.json` and `explain.md`, and the parameters
    are the build's; a role that can write is refused
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
    `api/fixtures/measured/snapshot-every-request.json` (the framework's every-request
    shape: latency under load, list prices, per-IP shares) maps its latency
    target, pricing, copy and value ranks. Until the website moved out, these
    also compared the Go port with the site's TypeScript; that parity test went
    with it.
- `server`:
  - every answer of a v4 build (`testdb.PublishV4`): the every-request fixture and
    QA-1 as payload v4 (18 networks with every exit country, the tails and 2,000
    City × ASN rows per window) match a per-route SHA-256 golden, one file per
    answer family (`testdata/answers-v4-scale/<family>.json`, `GOLDEN_WRITE=1`
    rewrites them; `api/answers/README.md`)
  - the committed fixtures as v4 builds answer as main did before browser targets
    (`testdata/answers-v4.json`)
  - headers, ETag per coding, 304 (strong, weak, other coding, `*`), HEAD, 405, 404
  - brotli and gzip bodies decode to the identity bytes
  - rebuild only on change
  - the background loop picks up changes
  - a failed rebuild keeps serving, and health reports it
  - a current build older than payload v4 (a v3 head in parts), at start or after
    a v4 build served, answers 503 on every data route and on `/healthz`, with the
    reason `calculation older than payload v4: recalculate`; a v4 build serves again
  - single-flight
  - the search LRU (normalisation, hits, bound, reset per version)
  - events filters and validation, and the database fallback that runs only when
    needed
  - the DB-backed integration test (the v1 fixture as a v4 build): read-only pool,
    catalog rows in the network answer, change pickup, and p99 latency
  - a fresh database with one published product as a v4 build (site schema migrated by an older
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
   The tag and the Astro website are in the old `proxy-index-api` repository, which
   removed the website afterwards; its last version there is the parent of the
   commit that removed it.

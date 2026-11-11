# `/v1/events/stats`

The live tape's figures of a range: event counts by tab, the reporting networks, the sparkline
buckets, the heatmap, the per-network rail and the pass rate, for 24h, 7d and 30d. At production
cadence that is up to about 240,000 events in 30 days, so no request and no refresh counts raw
rows: the answer reads `public.test_event_buckets` (framework schema v13), which a statement
trigger on `test_events` keeps up to date on every insert. Map: `docs/api-web-map.md`, section 11,
W9 (EV-COUNT, EV-REPORTING, EV-RANGE, EV-SPARK, EV-PASSRATE, EV-INCIDENTS, EV-HEATMAP, EV-RAIL).

- **Route:** `GET /v1/events/stats?range=24h|7d|30d[&network=<slug>]`. `range` defaults to `30d`;
  any other parameter answers 400, a `network` that is not a slug 400, and a slug of no tracked
  network 404.
- **Bodies:** every range for all networks and for each tracked network (a catalog network with
  a framework provider), stored as `/v1/events/stats/<range>[/<slug>]`. They are built on every
  build and again on every events refresh (the 10 s change detection, `build.WithEvents`): a
  registered answer with `Events` (`api/answers/README.md`). A request is answered from memory and
  kept in the version's events LRU, which each refresh replaces; `Cache-Control` is
  `public, max-age=15, s-maxage=15`, as `/v1/events`.
- **Window:** every figure covers `[start, end)`. `end` is the end of the 30-minute bucket holding
  the newest stored event, or holding the read's `now()` when that is earlier or there is no event
  (HOME-TAPE-1): live data ends about now, and frozen data (test days that ended) at its own end, so
  its ranges count its events. `start` is `end` minus the range, so a quiet network's range can be
  empty.
- **Size:** about 10 to 20 KB for 30d with 18 networks (24 signals each); far under the 200 KB cap.
- **Code:** `api/internal/build/events_stats_answer.go` (the answer), `api/internal/store/events_stats.go`
  (the read).

## Fields

| Field | Type | Source |
|---|---|---|
| `range`, `network` | `24h`, `7d` or `30d`; the `network=` slug or null | The request. |
| `start`, `end` | ISO time (`+00:00`, µs, as `/v1/events` `at`) | Above. |
| `totals.events` | integer | Every event of the range, events without a product or a network included. |
| `totals.byCategory` | `{census, pulse, incident, audit}`, integers | The same events by tab (`store.Category`, `/v1/events` `category`). |
| `totals.good`, `totals.bad` | integers | Events with `signal > 0`, and `signal < 0`. A signal's meaning depends on the kind (a `pulse_finished` signal is the direction of a change); for pass or fail use `pass`. |
| `totals.reporting` | integer | Tracked networks with an event in the range. |
| `totals.tracked` | integer | Tracked networks (1 with `network=`): the rail's rows. |
| `pass.requests`, `pass.successes`, `pass.rate` | numbers, null when unknown | The range's test requests and successes from the network figures, not event signals: `/v1/networks/all` `ranges.<range>` (`requests`, `successes`, `successRate`), or with `network=` the sum over that network's kinds of `/v1/networks/{slug}?kind=` `ranges.<range>` and their ratio. They change with a build, not with events. |
| `buckets[]` | `{start, total, byCategory, networks}`: 24 × 1 h for 24h, 28 × 6 h for 7d, 30 × 1 d for 30d, oldest first | The sparklines: events per bucket, by tab, and the tracked networks with an event in it. |
| `heatmap[]` | `{start, byCategory}`: 48 × 30 min, 56 × 3 h, 60 × 12 h, oldest first | The heatmap's columns. |
| `networks[]` | `{slug, name, events, good, bad, lastAt, recent}`, busiest first, then in `/v1/networks` order | Every tracked network (only the `network=` one with it), networks without events included: their counts in the range, the newest event's `at` (null without one) and `recent`, the signals of its newest 24 events in the range, oldest first (the beat strip draws the newest on the right; fewer when it has fewer). |

## Storage (framework schema v13)

- `test_event_buckets (bucket_start, product_id, category, events, good, bad, last_at)`, primary
  key `(bucket_start, product_id, category)`: one row per 30-minute bucket of `at`
  (`date_bin('30 minutes', at, '2000-01-01')`), product (`0` for events without one) and tab
  (`store.Category` in SQL). The view `test_event_bucket_counts` counts the same rows from
  `test_events` (the migration's fill, and the replay wipe's recount of the buckets it empties).
- The `AFTER INSERT ... FOR EACH STATEMENT` trigger `test_event_buckets_add` on the partitioned
  `test_events` (every partition fires it) groups the statement's transition table and upserts
  one row per key, adding the counts and keeping the newest `last_at`: one statement per insert
  statement or `COPY`, no per-row work. It calls no function, so a role that inserts events
  needs no EXECUTE right (production revokes EXECUTE from PUBLIC). A daily campaign hour's event inserts (144, one
  transaction per cell, as `daily/ingest.emit_cell` writes them, through a 78 ms link) take
  0.06 % longer with it (`tests/event_buckets_measure.py`).
- Concurrency: the upsert locks each row it writes until the writing transaction ends, so a
  statement that writes the same bucket row (30-minute bucket, product and tab) as another open
  transaction waits for it. The framework's event writers keep their transactions short (the
  longest is `detect`'s, once a minute), and each statement writes its rows in key order.
- `migrate_v12_v13.sql` creates the table, the view, the trigger function and the trigger and
  fills the table from the view in the same transaction.
- Rows are never deleted: the retention job drops old `test_events` partitions, and their buckets
  stay (about 3,500 rows a day at 18 networks); the answer reads only the last 30 days.

## Speed

- On every refresh, in the events read's REPEATABLE READ transaction (`store.loadEventStats`):
  `load.event_stats_head` (`end` from the newest event's `at`, the first entry of `test_events_at`,
  and whether the table exists; a schema before v13 reads no
  counts, every count 0), `load.event_buckets` (the 30 days' rows on the primary key: at most
  1,440 buckets × products × 4 tabs) and `load.event_recent` (each product's newest 24 events of
  the 30 days on `test_events_product`, a `LATERAL` index read). All three are in
  `siteapi explain`.
- Building every body is one pass over the rows per range and per network's rows; nothing is
  read on request.

## Tests

`build/events_stats_answer_test.go` (every field on events in 3 tabs over 3 days, `network=`, an
empty range and a database before v13, the key, the rebuild on `WithEvents`),
`store/events_stats_test.go` (the trigger's rows read back against the events inserted, the recent
signals, a database without the table), `server/events_stats_db_test.go` (served, then updated by an
events-only refresh without a full load; 404 and 400), the framework's
`tests/test_storage_migrate_v13.py` (v12 → v13 with the fill compared with a count of
`test_events`, the trigger on a multi-row insert, a `COPY` and an insert in a later bucket), and the
golden family `server/testdata/answers-v4-scale/events-stats.json`.

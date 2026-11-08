# `/v1/events`: the live tape's rows

The test events (`public.test_events`) as the live tape draws them: the framework's `TestEvent`
with the tape's text, the network's site slug and name, filters for the tape's controls and an id
cursor that never loses a row. Map: `docs/api-web-map.md` section 11, W8 (TAPE-ROWS-1).

## Query parameters

Every parameter is optional; any other parameter answers 400.

| Parameter | What |
|---|---|
| `product=<provider>[/<product>]` | The framework provider id, and optionally its product name. |
| `network=<slug>` | The site slug of a network, mapped to its framework provider. A slug that names no network answers 400 (`unknown network <slug>`). With `product=`, both apply. |
| `kind=<kind>[,<kind>...]` | Exact event kinds. |
| `category=census\|pulse\|incident\|audit` | The tape tab. |
| `country=<CC>` | The event's country, two letters (case-insensitive). |
| `target=<id>` | The event's target id. |
| `proxyKind=residential\|datacenter\|mobile\|isp` | Rows of products of that proxy kind (the product name). |
| `q=<words>` | Every word (split on spaces, at most 200 bytes) must occur, case-insensitive, in the row's `title`, `detail`, network name (`providerName`), `country` or `target`. |
| `since=<ISO time>` | Rows with a later `at` (exclusive), newest first. |
| `after=<event_id>` | Rows with a larger `event_id`, oldest first (by id). |
| `limit=1..500` | At most this many rows (default 100). |

## Order and choice

- With `after`: the rows past it by `event_id`, oldest first, up to `limit`. A poll that passes the
  last answer's `cursor` walks forward through a burst that shares one `at` without losing a row.
- With `since`, `kind` or `category`: exactly the newest matching rows, newest first.
- Otherwise (the tape's first read): prioritized, newest first. Routine rows (the census tab and
  `pulse_finished`) never push other rows off the page: the newest other rows take up to half of
  `limit`, the newest rows of any kind fill the rest.

## Body

`{events, cursor, updatedAt, more}`.

| Field | What | Source |
|---|---|---|
| `events[]` | The rows: every `TestEvent` field (`id`, `at`, `kind`, `category`, `product`, `provider`, `country`, `region`, `target`, `signal`, `severity`, `data`) and the five below. | `test_events` |
| `events[].title`, `.detail`, `.value` | The tape's text, the mapping `Dataset.tape` uses (`build/tape.go` `TapeFromEvents`): `detail` starts with the network name. Rendered once per event when the event window loads. | `test_events.kind`, `.data`, `.country`, `.region` |
| `events[].providerSlug`, `.providerName` | The network's site slug and display name; null for a provider without a network. | the build's framework-provider-to-slug map and catalog names |
| `cursor` | The largest `event_id` the answer covers: its largest row id, else the request's `after`, else null. When a `q` read of the database fallback past `after` stops at its bound (below), the last row it read. | `test_events.event_id` |
| `updatedAt` | The newest event's `at` (of every event, not only the matching ones), null without events. | `test_events.at` |
| `more` | True when more matching rows follow `cursor`: pass it as the next `after`. | |

## Speed

- **Memory.** The build holds the newest 2,000 events and past them the newest 500 non-routine
  ones, each rendered once (text, slug, name and `q`'s lower-case text). A filter is answered from
  them when they provably hold its whole answer (README "Events"); with `after`, when `after` is at
  least the smallest id of the 2,000 (rows are stamped when written, so a newer id is not older
  than the window).
- **Database fallback.** Otherwise one read-only query (`store.QueryEvents`) runs, with `network`,
  `country`, `target` and `proxyKind` as predicates (`p.provider`, `e.country_code`, `e.target_id`,
  `p.name`) and `after` as `e.event_id > $n ORDER BY e.event_id`, reading `limit + 1` rows to tell
  `more`. `q` matches the rendered text, so it is not SQL: the fallback reads up to 2,000 rows
  passing the other filters (`store.TextScan`) and matches `q` among them. Every shape is in
  `siteapi explain` (`events.query.network`, `.network_notable`, `.country`, `.target`,
  `.proxy_kind`, `.after`, `.q`).
- The rendered answer is kept in the version's 256-entry LRU; `Cache-Control` is
  `public, max-age=15, s-maxage=15`.

## Tests

`build/events_tape_test.go` (a 300-event burst read with `limit=100` and `after` loses none, from
memory and from the fallback; `network=` against `product=`; `q=`; `more`, `cursor`, `updatedAt`;
a bounded `q` read), `store/events_tape_test.go` (each new filter as a database predicate),
`server/events_tape_test.go` and `server/events_tape_db_test.go` (the every-request build), and the
golden family `server/testdata/answers-v4-scale/events.json`.

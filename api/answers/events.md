# `/v1/events`: the live tape's rows

The test events (`public.test_events`) as the live tape draws them, in the tape row's shape
(`kind` the group, `event` the code, `values` the numbers; #993): the network's site slug and
name, filters for the tape's controls, an id cursor that never loses a row, and a `before`
cursor that pages back (EVENTS-BEFORE-1). Map: `docs/api-web-map.md` section 11, W8 (TAPE-ROWS-1).

## Query parameters

Every parameter is optional; any other parameter answers 400 `unknown_parameter`. Every error is
`{"error": {code, params}}` (`api/README.md`, "Endpoints"); the 400s below name each one.

| Parameter | What |
|---|---|
| `product=<provider>[/<product>]` | The framework provider id, and optionally its product name. |
| `network=<slug>` | The site slug of a network, mapped to its framework provider. A slug that names no network answers 400 `invalid_parameter` (`name` `network`, its `value`). With `product=`, both apply. |
| `kind=<group>[,<group>...]` | Groups, the tape's tabs (`census`, `pulse`, `incident`, `audit`; `api/CODES.md`, `event_group`), as `kind` serves them: a row passes when its group is listed. Any other value (an event code too, since #993: use `event=`) answers 400 `invalid_parameter` with `allowed`; more than 32 values 400 `too_many_values`. |
| `event=<code>[,<code>...]` | Event codes (`api/CODES.md`, `event`), as `event` serves them: a row passes when its code is listed. With `kind=`, both apply. Another value answers 400 `invalid_parameter` with `allowed`; more than 32 values 400 `too_many_values`. |
| `country=<CC>` | The event's country, two letters (case-insensitive); else 400 `invalid_parameter`, `format` `country`. |
| `target=<id>` | The event's target id. With it, that target's own rows, unmerged (below). |
| `proxyKind=residential\|datacenter\|mobile\|isp` | Rows of products of that proxy kind (the product name); another value answers 400 `invalid_parameter` with `allowed`. |
| `q=<words>` | Every word (split on spaces, at most 200 bytes) must occur, case-insensitive, in the row's network name (`providerName`) or slug (`providerSlug`), event code (`event`), `country` or `target`; never in English text (#984). Longer: 400 `too_long`. |
| `since=<ISO time>` | Rows with a later `at` (exclusive), newest first; not a time: 400 `invalid_parameter`, `format` `iso_time`. |
| `after=<event_id>` | Rows with a larger `event_id`, oldest first (by id); not an id: 400 `invalid_parameter`, `format` `event_id`. |
| `before=<event_id>` | The rows below that event, newest first (below). Not with `after` (400 `conflicting_parameters`, `names` `["before", "after"]`); an id that names no event answers 400 `unknown_cursor` (`name` `before`, its `value`). |
| `limit=1..500` | At most this many rows (default 100); outside: 400 `out_of_range` (`min` 1, `max` 500), not a number: 400 `invalid_parameter`, `format` `positive_integer`. |

## Order and choice

- With `after`: the rows past it by `event_id`, oldest first, up to `limit`, stopping before the
  cursor bound (below). A poll that passes the last answer's `cursor` walks
  forward through a burst that shares one `at` without losing a row.
- With `since`, `kind`, `event` or `before`: exactly the newest matching rows, newest first.
- Otherwise (the tape's first read): prioritized, newest first. Routine rows (the census tab and
  `pulse_finished`) never push other rows off the page: the newest other rows take up to half of
  `limit`, the newest rows of any kind fill the rest.

## Paging back (EVENTS-BEFORE-1)

Every newest-first list is ordered by `at`, then `id` (`store.Newer`). `before=<id>` gives the next
`limit` rows below the place of the row with that id, with every other filter (with `since`: newer
than `since` and below `before`). A client pages back by passing each page's last row id while
`more` is true.

- **The place** is a keyset on (`at`, `id`), so rows sharing one `at` are paged by id and a
  boundary never repeats or skips one. A merged row (below) carries its group's largest id, which
  may not be its newest row: its place is its group's newest matching row's, where it stood.
- **Groups:** a group with a row at or above that place stood on that page or an earlier one, so
  the page leaves it out; every other group is below it whole.
- **Memory or database:** from memory when the window holds the `before` row and the page's
  `limit` matching rows and one more below it (or every row, or `since` reaching past it); otherwise
  the database fallback (`store.QueryEvents`, `queryBefore`): the anchor row
  (`events.query.before_anchor`), for a merged answer its group's rows above it, the `limit + 1`
  rows below the place (4 times `limit`, plus one, for a merged answer; `events.query.before`) and
  every row of the groups of the merging rows read, above the place or below the read
  (`events.query.before_groups`: only those groups, by kind, network, product, country, region and slot, at most 4 rows each, so
  no other group of a slot takes their place under the limit; the `after` read of a group's earlier
  rows, `events.query.after_groups`, reads the same way). While a full read leaves `limit` rows or
  fewer once the groups standing at or above the place (shown on an earlier page) are left out, it
  reads on below the last row read, so a wide outage never ends a page early.
- With `q`, a database read stops at 2,000 rows below the place (`store.TextScan`); when it
  does, `more` is true even if fewer rows matched. A page that matched no row then has no id to
  continue from: a client narrows the filter (a tab, a network) instead.
- A `before` page's `cursor` follows the same rule as the default poll's; it is not a polling
  position: a client keeps polling `after` with the newest page's.

## One row per incident (EVENTS-MERGE-1)

The detector judges each pulse series per target, so an outage seen by the `generic_http` and the
`generic_https` pulses of one slot is two events. Every answer without `target=` shows it as one
row; the stored events are untouched.

- **A group** is the rows of one non-routine kind (pulse down and recovered, the incident tab, the
  audit tab) with a target and the same product, country, region and pulse slot (their batch's
  `scheduled_for`; without a batch, the same `at`). Routine rows (the census tab and
  `pulse_finished`) and rows without a target are never merged: each target's pulse is its own
  measurement.
- **The merged row** is the group's `generic_http` row if it has one, else its first written
  (smallest id), with `id` the group's largest id (an `after` cursor passes the whole group) and
  `targets` the group's targets, sorted. `target` stays that row's, `kind` and `event` are
  unchanged: a client that shows which targets saw it reads
  `targets`. It stands where the group's newest row stands.
- **Pages never split a group:** `limit` counts merged rows, and with `after` a group whose largest
  id is past it also takes its rows at or below it. The database fallback reads 4 times `limit`
  rows (`store.GroupRows`) so a group at the limit is read whole; with `after` it also reads the
  rows at or below `after` of the slots it read (`events.query.after_groups`, on
  `test_events_notable`), as the detector writes a slot's batches one target at a time.
- **Polling:** when a slot's two batches are detected in different runs, a client polling with
  `after` gets the incident twice: first with one target, then with both and a larger `id`. A
  client replaces a row by the later one of its group (same `event`, `providerSlug`, `product`,
  `country`, `region` and, with a batch, slot; the second row's `targets` holds the first's).

## Settled rows (the cursor bound)

`event_id` is taken when a row is inserted, not when its transaction commits, and a writer's
transaction id often comes earlier still (the framework's writers insert events after other rows).
So neither id order nor transaction order tells whether a row with a smaller id can still commit,
and a cursor past it would skip it. The API therefore moves a cursor only over ids that can no
longer appear behind it (`build.cursorMarks`):

- Each events read (`store.LoadEvents`, and `Load` for a full build) records, in the same
  REPEATABLE READ transaction as its rows, its snapshot's `xmin`, its **mark** (the first
  transaction id not yet handed out: `xmin + age(xmin)`, `xid8`, so no 32-bit wraparound) and the
  largest event id it sees (`store.EventsRead`, `load.events_read`). The snapshot's `xmax` is not
  the mark: it is the newest finished id + 1, and a running transaction can hold it or a later one.
- Once a later read's `xmin` reaches a read's mark, every transaction that had an id at that read
  has ended, so no id up to its largest can still appear. The largest such id is the **bound**. The
  window keeps the last reads still waiting (at most 16) and carries them across full builds.
- An `after` answer returns no row above the bound, and `more` stays true while such rows wait.
  They come in a later answer, in id order. The database fallback takes the bound from the window.
- Every `cursor` stops at the bound, the default poll's too (it still returns the newest rows). A
  poll with that cursor may return some of those rows again: **clients deduplicate by `id`**.
- Until a first bound is known (after a start, one refresh, or longer while a transaction open at
  the start stays open), the default poll's `cursor` is its smallest row id minus one instead, so a
  client does not walk from the oldest event. A row of a transaction open across the start with an
  id below that can then be missed; every cursor after that follows the bound.
- While the window holds ids above the bound, every refresh (10 s) reads the events again, even
  without a new event. So a just-written event shows up through `after` one refresh (10 s) later
  than in the default poll, or longer while a transaction of the database server that was open at
  the read stays open (transaction ids are the whole server's). When no transaction with an id is
  running at a read, the bound is that read's largest id at once.
- No time lag is used: no fixed delay can bound a slow transaction, the snapshots can.

## Body

`{events, cursor, updatedAt, more}`.

| Field | What | Source |
|---|---|---|
| `events[]` | The rows, one per incident (above): `id`, `at`, `product` (`"<frameworkProvider>/<product>"`), `country`, `region`, `target`, `signal` (-1, 0, 1), `severity` (`event_severity` code), and the fields below. No stored data, `category` or framework `provider` (removed in #993). | `test_events` |
| `events[].kind` | The group, the tape's tab (`census`, `pulse`, `incident`, `audit`; `event_group` code), as a tape row's `kind`. Was the raw event kind until #993. | `test_events.kind` |
| `events[].event` | The event code (`ip_overlap`; `api/CODES.md`, `event`), as a tape row's `event`. | `test_events.kind` |
| `events[].targets` | The targets of the rows merged into it, sorted; a row standing for itself has its own target alone (`[]` without one). | `test_events.target_id` |
| `events[].title`, `.detail`, `.value` | Not served (removed on 2026-11-13, #962: the website writes the line from `event`, `country` and `values`). | |
| `events[].values` | The event's numbers (made from the event's registered data keys only, `api/CODES.md`, "Event data"): on every row the same 8 keys `requests`, `successes`, `p50`, `baseline`, `value` (the numeric headline), `uniqueIps`, `sharedIps`, `hour` (the start of the event's hour, an ISO time: an `ip_overlap`'s `data.hour`, a release's `data.day` and `data.hour`), plus the kind's own (`baselineRate`, `share`, a `pulse_finished`'s `previousSuccesses` and `previousRequests`, ...); null where the event has none (the previous pulse's counts on events stored before 2026-11-12), made by the same function as a tape row's `values` (`build/event_values.go`), so the two agree: `FIELDS.md`, "Tape and event values" (TAPE-VALUES-1). | `test_events.kind`, `.data` |
| `events[].providerSlug`, `.providerName` | The network's site slug and display name; null for a provider without a network. | the build's framework-provider-to-slug map and catalog names |
| `cursor` | The largest `event_id` the answer covers: its largest row id, else the request's `after`, else null. Never above the bound (above), unless `after` is. When a `q` read of the database fallback past `after` stops at its bound (below), the last row it read. | `test_events.event_id` |
| `updatedAt` | The newest event's `at` (of every event, not only the matching ones), null without events. | `test_events.at` |
| `more` | With `after`: true when more matching rows follow `cursor`, above the bound or not: pass it as the next `after`. Otherwise (newest first): true when matching rows exist below the page: pass the last row's `id` as `before`. On the prioritized pick (no `kind`, `event`, `since` or cursor): true when a matching row is left out. | |

## Removed in #993

After the website's W6 switch (it reads `event`, `values` and `providerSlug` and filters with
`kind=<group>` and `event=`), #993 removed `category` (the group: read `kind`), `data` (every
number the website read from it is in `values`: `hour`, `requests`, `successes`, `p50`, `value`,
`unique_ips` as `uniqueIps`, `shared_ips` as `sharedIps`; a raw `baseline` is `values.baseline`
for a latency kind, `baselineRate` for `down`, `success_drop` and `recovered`,
`baselineBlockShare` for `block_spike`), `provider` (read `providerSlug`) and the `category=`
filter (400 `unknown_parameter`; use `kind=<group>`); `kind` turned into the group, as on tape
rows, and `kind=` takes groups only (an event code there: use `event=`).

## Speed

- **Memory.** The build holds the newest 2,000 events, past them the newest 500 non-routine ones,
  and up to 500 late ones: rows with an id above the 2,000's smallest but an older `at` (the daily
  ingest stamps a row with its test's own times, hours before it writes it). Each is rendered once
  (values, slug, name and `q`'s lower-case text). A filter is answered from them when they provably
  hold its whole answer (README "Events"); with `after`, when `after` is at least the 2,000's
  smallest id minus one and every late row is held, since every row past it is then among the
  2,000 or the late ones.
- **Database fallback.** Otherwise one read-only query (`store.QueryEvents`) runs, with `network`,
  `country`, `target` and `proxyKind` as predicates (`p.provider`, `e.country_code`, `e.target_id`,
  `p.name`) and `after` as `e.event_id > $n ORDER BY e.event_id`, reading `limit + 1` rows to tell
  `more` (4 times `limit`, plus one, for a merged answer); every other read reads one row more than
  it returns too, for the same reason. `q` matches the rendered names, so it is not SQL: the fallback reads up to 2,000 rows
  passing the other filters (`store.TextScan`) and matches `q` among them. Every shape is in
  `siteapi explain` (`load.events_read`, `load.events_late`, `events.query.groups`, `.events`, `events.query.network`, `.network_notable`, `.country`, `.target`,
  `.proxy_kind`, `.after`, `.after_groups`, `.before_anchor`, `.before`, `.before_groups`, `.q`).
- The rendered answer is kept in the version's 256-entry LRU; `Cache-Control` is
  `public, max-age=15, s-maxage=15`.

## Tests

`build/events_cursor_test.go` (the bound over a series of reads; an `after` answer and the default
poll's cursor stop at it, from memory and from the fallback; the hand-off from the default poll),
`server/events_settle_db_test.go` (a writer whose transaction id came first commits a larger id
while another transaction holds a smaller one: no poll passes it, and both arrive once it commits,
through the default poll's cursor too; a 300-row burst in one transaction read whole),
`build/events_tape_test.go` (a 300-event burst read with `limit=100` and `after` loses none, from
memory and from the fallback; a late row an `after` poll returns; `network=` against `product=`; `q=`; `more`, `cursor`, `updatedAt`;
a bounded `q` read), `store/events_tape_test.go` (each new filter as a database predicate, the late read),
`build/events_before_test.go` (paging back with `before` through more than 3 pages of equal-time
rows and merged groups with every filter, from memory, a partial window and the fallback, every row
once; `more` on a full first page), `server/events_before_test.go` and
`server/events_before_db_test.go` (the route's 400s and `unknown_cursor`; the fallback's before read
against the database with every filter), `build/event_values_test.go` (the values of every kind; a tape row and its `/v1/events` row agree), `build/events_merge_test.go` (a two-target outage: one row per event with both targets, `target=`'s
own rows, a page boundary inside a group, the dataset's tape), `store/events_merge_test.go` (the
counts and the slot read), `server/events_tape_test.go` and `server/events_tape_db_test.go` (the every-request build), and the
golden family `server/testdata/answers-v4-scale/events.json`.

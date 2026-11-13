# `/v1/networks/{slug}/pricing-history`

A network's list rate over the last 90 days and its last change (NET-PRICE-HISTORY), from
`site.pricing_history` (migration 0003; `api/scripts/pricing-history.sql` is the template
for its rows). Built in `api/internal/build/pricing_history_answer.go`, once per build for
every network and kind, and rendered on first request (kept in the page cache).

## Request

`GET /v1/networks/{slug}/pricing-history?kind=`

`kind` is `residential`, `datacenter`, `mobile` or `isp`, by default the network's primary
kind. Another value or parameter answers 400; an unknown network answers 404. `all` answers
an empty history (no single list rate).

## Body

| Field | What |
|---|---|
| `generatedAt`, `network`, `kind` | The build's time, the slug and the kind used. |
| `kindDefault` | True when no `kind` was given (`kind` is then the primary one). |
| `points` | `{t, v}` oldest first: `v` is the list rate in USD per GB in force `t` days before the build's day (89 to 0), on the days a rate was in force. |
| `lastChangePct` | The newest change, in percent of the rate before it (2 dp). |
| `daysSinceChange` | Days from that change to the build's day. |
| `na` | The fields above that are null. |

## Sources

- A row's rate is `price_per_gb_at_scale` (the lowest list rate), else `price_per_gb` (the
  entry rate), as the network answer's `$ / GB`. A row is in force from its
  `effective_from` until the next row; rows dated after the build's day are left out.
- The snapshot's `products[].pricing` (`lowest_per_gb`, else `entry_per_gb`) is the newest
  step when its `effective_from` is later than the newest row's (and not after the build's
  day). The old `basis` text was removed on 2026-11-13 (#962).
- Changes are taken over every row, not only the 90 days: a change 120 days ago gives
  `daysSinceChange` 120 and a flat series.
- Without rows (or without the table, which `/healthz` lists in `siteGaps`), `points` is
  empty and `lastChangePct` and `daysSinceChange` are null, listed in `na`. With
  rows but no change, only `lastChangePct` and `daysSinceChange` are null.

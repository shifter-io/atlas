# `/v1/targets`: the network × target matrix

`GET /v1/targets?kind=&range=&engine=` serves, in one request, every target's meta and
every network's figures on every measured target in one range: the targets page, the
compare table's by-target section, true cost's target tabs and the network page's target
rows (work item W4 of `docs/api-web-map.md`). The build precomputes one body per proxy
kind, range and engine view: the default body (stored as `/v1/targets/<kind>/<range>`) and
one per engine (`/v1/targets/<kind>/<range>/camoufox` and `.../chrome`); the code is
`api/internal/build/targets_answer.go`.

## Query

| Parameter | Values | Default |
|---|---|---|
| `kind` | `residential`, `datacenter`, `mobile` or `isp` | `residential` |
| `range` | `24h`, `7d` or `30d` | `30d` |
| `engine` | `camoufox` or `chrome` | none: each network's main engine |

Any other value, or any other parameter, answers 400. A kind with no measured network
answers 200 with no `networks`.

## Sources

`w` is the range's window of the network's snapshot product group for the kind
(`products[].windows[<range>]`; every range, not only 30d). The dataset is the
`/v1/dataset` dataset of the same build. Rates are fractions from 0 to 1.

**No engine mixing.** A browser target's cell is the figures of one engine (`camoufox` or
`chrome`); means, statuses and ranks are per engine and never add Chrome and Camoufox
tries together. A network target (`generic_http`, `generic_https`) has the engine `http`
and the same cell in every body.

**Engine views.** In the default body each network's browser cell is its main engine's
(the engine with the most tries on the target), so two networks' cells of one target can
be of different engines. Scores and best marks only compare within one engine
(`docs/api-web-map.md` CMP-17): a page that compares networks reads the `engine=` body,
where every browser cell is that engine's. Every body has the same `targets[]` (with
`fleet` and `scoreStatus` per engine) and the same networks in the same order. The other
engines' figures of one network are in its network answer (`/v1/networks/{slug}` →
`profile.targets[].engines`).

## Body

| Field | Unit | Source |
|---|---|---|
| `generatedAt` | ISO time | the build's `generatedAt` |
| `range`, `kind` | | the request's (after defaults) |
| `engine` | | the request's `engine`; null in the default body |
| `cardCount` | targets | how many `targets[]` have `card` true |
| `targets[]` | | every dataset target (`/v1/dataset → targets[]`), in dataset order (`sort_order`) |
| `networks[]` | | every network with a snapshot product group of the kind, in the rankings' order (below) |
| `na`, `naReason`, `naReasons` | paths, codes | every null of `targets[]`, `networks[]` and their cells, once for all of them (`targets[].difficulty`, `networks[].rank`, `networks[].cells.<targetId>.costPer1kSuccess`), each with the reason most of them have (`answers/README.md`, "Missing data"). A score's `score`, `rank` and `rankedOf` (explained by its `status`) and `engine` in the default body are null by contract |

### `targets[]`

| Field | Unit | Source |
|---|---|---|
| `id`, `label`, `category`, `passCondition`, `engine`, `group` | | the dataset target (`site.targets`, `public.targets`; `group` from the snapshot's `[target_groups]`); `engine` is `http`, `browser`, or null for a catalog-only target |
| `sources` | target ids | a group target's sources in order (the snapshot's `[target_groups]`); `[]` on every other target |
| `scoreWeight` | 1 to 5 | the snapshot methodology's `[browser_difficulty]`: the target's weight in the overall browser score; a group without one takes its hardest source's (`policy.browser_difficulty`). Null for a target without one (network targets) |
| `difficulty` | 1 to 5 | the catalog's display difficulty (`site.targets.difficulty`); `scoreWeight` when the catalog has none |
| `card` | boolean | true when the target is measured (`source` `measured`) and is no group's source (`group` null) |
| `fleet.<engine>.meanSuccess` | fraction | the unweighted mean, over the networks of the answer with tries of the engine, of each network's success on the target: a browser target's `w.targets[id].browser.engines.<engine>.successes / completed`, a network target's `successRate` (engine `http`) |
| `fleet.<engine>.networks` | networks | how many networks the mean is over |
| `scoreStatus.<engine>.status` | | the success score status the networks of the answer share for the engine (`w.targets[id].browser.engines.<engine>.scores.success.status`): `ranked`, `all_similar` or `too_few_networks` from any network with enough tries (all such networks share it); `not_enough_data` when no network of the answer has enough, or none has a score for the engine in the range (a 24h window without a run). See "Browser scores" in `api/FIELDS.md`. Every engine (`camoufox`, `chrome`) on a browser target; `{}` on a network target |
| `scoreStatus.<engine>.rankedOf` | networks | M, the networks ranked; null unless `ranked`. The calculator ranks every product group of the window, all kinds together, so M can count networks of other kinds |

### `networks[]`

| Field | Unit | Source |
|---|---|---|
| `slug`, `name` | | the network (`/v1/networks`) |
| `position` | ordinal | its place in the list (1 = first): by the range's balanced score (`w.scores.balanced.score`), highest first, then unscored networks by name, as `/v1/rankings` |
| `rank` | ordinal | the published rank among the ready networks of the kind (`w.scores.balanced.rank`); null without a score in the range |
| `browserOverall.<engine>.success` | | the overall browser score of the range, `{score, rank, rankedOf, status, targets, weights}` (`w.browser_overall`; see `browserOverall` in `api/FIELDS.md`); `browserOverall` is null when the window has none |
| `cells.<targetId>` | | one cell per measured target (`targets[]` with `source` `measured`); a target the window does not measure has a cell of nulls with `requests` 0 |

### `cells.<targetId>`

A cell carries the figures of a network page's `targets[]` entry (`api/FIELDS.md`,
"Browser targets") from the range's window, with the same field names, for one engine. It
has no `engines` object (one engine per body, above) and no group `sources[]` breakdown:
each source (`targets[].sources`) has its own cell.

| Field | Unit | Source |
|---|---|---|
| `successRate`, `captchaRate`, `blockRate` | fraction | default body: `w.targets[id].metrics.success_rate`, `.challenge_rate`, `.block_rate` (a browser target's: its main `engine`'s). `engine=` body, browser targets: the engine's `w.targets[id].browser.engines.<engine>.outcome_rates.ok`, `.challenge`, `.block` |
| `requests` | tries | default body: `w.targets[id].metrics.completed`; `engine=` body, browser targets: the engine's `.completed`. 0 when unmeasured or without tries of the engine |
| `latencyP50`, `latencyP90` | milliseconds | network targets only: `generic_http`'s headline latency (`w.targets.generic_http.metrics`: latency under load in an every-request window with a load test, else its light-load `latency_ms`, the daily tests' own); on `generic_https` (every other HTTP target) its display-only light-load `latency_ms` from its own successes (HTTPS-LATENCY-1, `api/FIELDS.md` `targets[<id>]`), null with `few_successes` when no time slot had 30 of them, `not_measured` in a snapshot written before it. Not written on browser targets |
| `engine` | `camoufox` or `chrome` | browser targets only: the engine whose figures the cell is. Default body: the engine with the most completed tries (`w.targets[id].browser.engine`), null when unmeasured; `engine=` body: the requested one, also on a cell without its tries |
| `otherEngines` | engine names | browser targets only: the target's other engines with tries in the window, by name (`[]` when none), so a page knows a second engine exists |
| `outcomes.*` | fraction | browser targets only: `ok`, `challenge`, `block`, `loginWall`, `timeout`, `proxyError`, `noContent`, `noAnswer`, `otherError`. Default body: `w.targets[id].browser.outcome_rates`; `engine=` body: the engine's `.outcome_rates` |
| `pageLoadP50`, `pageLoadP90` | milliseconds | browser targets only: `.page_load_ms.p50`, `.p90` (successful tries), of the cell's engine as above |
| `scores` | | browser targets only: the cell's engine's rank-based `success`, `challenge` and `block` scores `{score, rank, rankedOf, status}` (`w.targets[id].browser.scores`, else its main engine's `.engines.<engine>.scores`, the calculator's copy; in an `engine=` body the engine's `.scores`); null in a snapshot without scores or without tries of the engine |
| `costPer1kSuccess` | dollars per 1,000 successful requests | the window cost rule (`/v1/rankings` `costPer1kSuccess`) on the target's own wire bytes, all engines' (the window does not split them): `(w.targets[id].metrics.bytes_up + bytes_down) / completed × 1,000 / 2³⁰` GB at the network's list rate for the kind (`productMeasurements.<kind>.pricing`: `pricePerGbAtScale`, else `pricePerGb`), over the request success rate (`request_success_rate`, else `success_rate`), 4 dp. Null when the target has no wire bytes (W11), no list rate or no success, and in an `engine=` body on a cell without the engine's tries |
| `successRank {rank, peers}` | ordinal, networks | the cell's competition rank by `successRate` (higher first; ties share a rank) among the networks of the body whose cell has the same engine and a rate (in an `engine=` body: the networks with tries of that engine); `peers` is how many those are; `rank` null without a rate |
| `costRank {rank, peers}` | ordinal, networks | the same by `costPer1kSuccess`, lower first |

A cell without tries of the requested engine has null rates, outcomes, page loads, scores
and cost, `requests` 0 and `engine` the requested one.

## Size

Every answer is at most 200 KB (`build.MaxAnswerBytes`, `api/README.md`). The matrix grows
with networks × browser targets × engines, so a body carries one engine per cell, never
all of them. `TestEveryTargetsBodyFitsWithRoom` (`targets_size_test.go`) builds 18
networks at the page latency test's week shape (`testscale.Week`: 8 browser targets and
the AI group's 3 sources, each on two engines) and at QA-1's (9 browser targets, one
engine), and fails when a body is over 150 KB (153,600 bytes) or the largest body's
bytes per network are over 8,600, the share that still lets 23 networks fit under the
cap.

| Shape | Default body | `engine=camoufox` | `engine=chrome` | Per network (largest) |
|---|---|---|---|---|
| Week, residential (any range) | 152,100 | 152,106 | 152,104 | 8,450 |
| QA-1, residential (any range) | 122,755 | 122,761 | 97,382 | 6,820 |
| Either, another kind (no network) | about 2,950 | | | |

With both engines in every cell the week's body was 254,816 bytes. One browser cell is
about 620 bytes (`scores` about 215, `outcomes` about 150), so beyond about 22 networks
the matrix needs paging or a split by target category.

## Tests

`api/internal/build/targets_answer_test.go` (meta, order, engines, the engine bodies,
cost and ranks, 24h against 30d, the JSON shape, the query),
`api/internal/build/targets_size_test.go` (the size rule, and the engine bodies' cells of
one network against its network answer's `engines.<engine>`) and the golden hashes
`api/internal/server/testdata/answers-v4-scale/targets.json`.

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

Any other value answers 400 `invalid_parameter`, any other parameter 400 `unknown_parameter`
(`api/README.md`, "Endpoints"). A kind with no measured network
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
| `id`, `category`, `engine`, `group` | | the dataset target (`site.targets`, `public.targets`; `group` from the snapshot's `[target_groups]`); `category` is a `target_category` code (`baseline`, `ecommerce`, `search`, `social`, `anti_bot`, `ai_answers`; null, `catalog`, without one); `engine` is `http`, `browser`, or null for a catalog-only target. A target's name and pass condition are not served (TARGET-TEXT-REMOVE-1, #989): a client words a target by `id`; `/v1/codes` set `target` holds the catalog's English reference |
| `sources` | target ids | a group target's sources in order (the snapshot's `[target_groups]`); `[]` on every other target |
| `scoreWeight` | 1 to 5 | the snapshot methodology's `[browser_difficulty]`: the target's weight in the overall browser score; a group without one takes its hardest source's (`policy.browser_difficulty`). Null for a target without one (`generic_http` and `generic_https`, which the browser overall does not count). `successScore` reads no difficulty: its 8 card targets weigh the same |
| `difficulty` | 1 to 5 | the catalog's display difficulty (`site.targets.difficulty`); `scoreWeight` when the catalog has none. Every difficulty is the owner's fixed value (2026-11-12), never derived from results: HTTP 1, `amazon_pdp` 2, `social_web` and its sources `instagram_web` and `x_web` 3, `google_search` and `cloudflare_challenge` 4, `linkedin_public`, `ai_answers` and its sources 5 ("Target difficulty" in `api/FIELDS.md`) |
| `card` | boolean | true when the target is measured (`source` `measured`) and is no group's source (`group` null) |
| `fleet.<engine>.networks` | networks | how many networks the mean is over |
| `fleet.<engine>.successScore` | `{score, status, basis}` | The mean success rate (internal; `meanSuccess` was removed on 2026-11-14, #900) on the 1-10 scale, absolute as `/v1/home` `market.successScore`: `score` = `1 + 9 ×` the mean, 1 decimal; `status` `ranked`, or null with `score` without a mean; `basis` `"mean_success_rate"`. A mean of the cells' scores would always be about 5.5 (they are ranks). `FIELDS.md`, "Success scores instead of success rates". |
| `scoreStatus.<engine>.status` | | the success score status the networks of the answer share for the engine (`w.targets[id].browser.engines.<engine>.scores.success.status`): `ranked` (a tie is a shared first place, SUCCESS-TIE-1) or `too_few_networks` from any network with enough tries (all such networks share it); `not_enough_data` when no network of the answer has enough, or none has a score for the engine in the range (a 24h window without a run). See "Browser scores" in `api/FIELDS.md`. Every engine (`camoufox`, `chrome`) on a browser target; on a measured network target (`generic_http`, `generic_https`) `http`, from the cells' `scores.success.status` (`w.targets[id].scores`, SUCCESS-SCORE-CALC-1); `{}` on a catalog-only target |
| `scoreStatus.<engine>.rankedOf` | networks | M, the networks ranked; null unless `ranked`. The calculator ranks every product group of the window, all kinds together, so M can count networks of other kinds |

### `networks[]`

| Field | Unit | Source |
|---|---|---|
| `slug`, `name` | | the network (`/v1/networks`) |
| `position` | ordinal | its place in the list (1 = first): by the range's balanced score (`w.scores.balanced.score`), highest first, then unscored networks by name, as `/v1/rankings` |
| `rank` | ordinal | the published rank among the ready networks of the kind (`w.scores.balanced.rank`); null without a score in the range |
| `browserOverall.<engine>.success` | | the overall browser score of the range, `{score, rank, rankedOf, status, targets, weights}` (`w.browser_overall`; see `browserOverall` in `api/FIELDS.md`); `browserOverall` is null when the window has none |
| `successScore` | | the network's overall 1-10 success score of the range over the 8 card targets (`targets[].card`), each scored by its distance from the best network, their plain mean (12.5% each, SUCCESS-ALL-8-1), `{score, rank, rankedOf, status}` (`w.success_overall`; "Success scores" in `api/FIELDS.md`); `status` `partial_targets` when a card target had no try in the range (left out); null when the window has none. The best network of the range reads 10 here and in each cell's `scores.success`, `.challenge` and `.block` (per target, metric and engine), the others keeping their measured distance ("The best network reads 10" in `api/FIELDS.md`, #948). Its `targets` and `weights` (served on the other answers) are left out here: the body has no room to repeat them ("Size"); every card target weighs 1 |
| `cells.<targetId>` | | one cell per measured target (`targets[]` with `source` `measured`); a target the window does not measure has a cell of nulls with `requests` 0 |

### `cells.<targetId>`

A cell carries the figures of a network page's `targets[]` entry (`api/FIELDS.md`,
"Browser targets") from the range's window, with the same field names, for one engine. It
has no `engines` object (one engine per body, above) and no group `sources[]` breakdown:
each source (`targets[].sources`) has its own cell.

| Field | Unit | Source |
|---|---|---|
| `requests` | tries | default body: `w.targets[id].metrics.completed`; `engine=` body, browser targets: the engine's `.completed`. 0 when unmeasured or without tries of the engine |
| `latencyP50`, `latencyP90` | milliseconds | network targets only: `generic_http`'s headline latency (`w.targets.generic_http.metrics`: latency under load in an every-request window with a load test, else its light-load `latency_ms`, the daily tests' own); on `generic_https` (every other HTTP target) its display-only light-load `latency_ms` from its own successes (HTTPS-LATENCY-1, `api/FIELDS.md` `targets[<id>]`), null with `few_successes` when no time slot had 30 of them, `not_measured` in a snapshot written before it. Not written on browser targets |
| `engine` | `camoufox` or `chrome` | browser targets only: the engine whose figures the cell is. Default body: the engine with the most completed tries (`w.targets[id].browser.engine`), null when unmeasured; `engine=` body: the requested one, also on a cell without its tries |
| `otherEngines` | engine names | browser targets only: the target's other engines with tries in the window, by name (`[]` when none), so a page knows a second engine exists |
| `outcomes.*` | fraction | browser targets only: `challenge`, `block`, `loginWall`, `timeout`, `proxyError`, `noContent`, `noAnswer`, `otherError`. Default body: `w.targets[id].browser.outcome_rates`; `engine=` body: the engine's `.outcome_rates` |
| `pageLoadP50`, `pageLoadP90` | milliseconds | browser targets only: `.page_load_ms.p50`, `.p90` (successful tries), of the cell's engine as above |
| `scores` | | network targets (`generic_http`, `generic_https`): `{success: {score, rank, rankedOf, status, delta}, challenge, block}`, the target's success score and its challenge and block scores `{score, rank, rankedOf, status}` (`w.targets[id].scores`, ranked on the internal challenge and block rates; challenge and block since CHALLENGE-BLOCK-TIE-1, absent in an older snapshot). A tie on challenge or block is a shared first place: 10, rank 1 of M, `ranked` ("Browser scores" in `api/FIELDS.md`). Browser targets: the cell's engine's rank-based `success`, `challenge` and `block` scores `{score, rank, rankedOf, status}`, `success` with its `delta` too (`w.targets[id].browser.scores`, else its main engine's `.engines.<engine>.scores`, the calculator's copy; in an `engine=` body the engine's `.scores`); null in a snapshot without scores or without tries of the engine. `success.delta` is `score` minus the previous published build's (`previous_score`), unrounded; null with `no_score_to_compare` when either has no ranked score, and in an `engine=` body for an engine other than the target's main one (`api/FIELDS.md`, "Success score deltas") |
| `networkBlocked` | `true`, `false` or null | whether the network's own gateway refuses the target for our account in the range, as opposed to the target site challenging or blocking (`w.targets[id].metrics.completed` and `.error_categories`; the rule and its thresholds: "Network-blocked targets" in `api/FIELDS.md`). Null under 100 tries, when every target of the network is refused, and in an `engine=` body on a browser cell of an engine other than the target's main one (the error counts are the main engine's) |
| `costPer1kSuccess` | dollars per 1,000 successful requests | the window cost rule (`/v1/rankings` `costPer1kSuccess`) on the target's own wire bytes, all engines' (the window does not split them): `(w.targets[id].metrics.bytes_up + bytes_down) / completed × 1,000 / 10⁹` GB (GB = 1,000,000,000 bytes; 2³⁰ and 4 dp before VALUE-ALL-2; it equals `/v1/home` `insights.valueAll.rows[].byTarget.<id>.costPer1k`) at the network's cost basis for the kind (`productMeasurements.<kind>.pricing.listRate`: the price of its first plan of 200 GB or more, else of its largest plan, COST-BASIS-200GB-1, `api/FIELDS.md`, "The cost basis"), over the request success rate (`request_success_rate`, else `success_rate`), unrounded. Null when the target has no wire bytes (W11), no list rate or no success, and in an `engine=` body on a cell without the engine's tries |
| `successRank {rank, peers}` | ordinal, networks | the cell's competition rank by the internal success rate (higher first; ties share a rank) among the networks of the body whose cell has the same engine and a rate (in an `engine=` body: the networks with tries of that engine); `peers` is how many those are; `rank` null without a rate |
| `costRank {rank, peers}` | ordinal, networks | the same by `costPer1kSuccess`, lower first |

A cell without tries of the requested engine has null outcomes, page loads, scores
and cost, `requests` 0 and `engine` the requested one.

## Size

Every answer is at most 200 KB (`build.MaxAnswerBytes`, `api/README.md`). The matrix grows
with networks × browser targets × engines, so a body carries one engine per cell, never
all of them. `TestEveryTargetsBodyFitsWithRoom` (`targets_size_test.go`) builds 18
networks at the page latency test's week shape (`testscale.Week`: 8 browser targets and
the AI group's 3 sources, each on two engines) and at QA-1's (9 browser targets, one
engine), both with the success scores at full width (`testscale.Shape.SuccessScores`:
17 significant digits, as the calculator writes them, deltas included), and fails when a
body is over 170 KB (174,080 bytes) or the largest body's bytes per network are over
9,700, the share at which 21 networks still just fit under the cap (18 exist).

The line was 150 KB and 8,600 until the success scores (SUCCESS-SCORE-API-1, #881):
each network gained `successScore` and its two HTTP cells `scores.success`, about 265
bytes, so the week's body went from 152,100 to 156,884 bytes. The architect raised
the line rather than drop the row's score or round the scores: the browser scores are
served unrounded, and rounding only the new ones would make two kinds of score
disagree (rounding every score is a separate decision). It was 160 KB and 8,900 until
the success score deltas (SUCCESS-SCORE-DELTA-1, #899): each cell's `scores.success`
gained `delta`, so the week's body went to 164,100 bytes; the line was raised the same
way. It was 168 KB and 9,300 until the HTTP challenge and block scores
(CHALLENGE-BLOCK-TIE-1, #925): each network's two HTTP cells gained `scores.challenge` and
`scores.block`, about 410 bytes at full width, so the week's body went to 171,514 bytes
(9,528 per network); the issue set the 170 KB line (30 KB under the cap), and the room is
now 21 networks. Each cell's `networkBlocked` (NETWORK-BLOCKED-1) fits under the same
line: the sizes below are measured with it.

| Shape | Default body | `engine=camoufox` | `engine=chrome` | Per network (largest) |
|---|---|---|---|---|
| Week, residential (any range) | 170,145 | 170,151 | 171,301 | 9,516 |
| QA-1, residential (any range) | 141,196 | 141,202 | 107,231 | 7,844 |
| Either, another kind (no network) | about 3,800 to 4,400 | | | |

With both engines in every cell the week's body was 254,816 bytes. One browser cell is
about 640 bytes (`scores` about 235, `outcomes` about 150), so beyond about 22 networks
the matrix needs paging or a split by target category.

## Tests

`api/internal/build/targets_answer_test.go` (meta, order, engines, the engine bodies,
cost and ranks, 24h against 30d, the JSON shape, the query),
`api/internal/build/targets_size_test.go` (the size rule, and the engine bodies' cells of
one network against its network answer's `engines.<engine>`) and the golden hashes
`api/internal/server/testdata/answers-v4-scale/targets.json`.

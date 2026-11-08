# `/v1/targets`: the network × target matrix

`GET /v1/targets?kind=&range=` serves, in one request, every target's meta and every
network's figures on every measured target in one range: the targets page, the compare
table's by-target section, true cost's target tabs and the network page's target rows
(work item W4 of `docs/api-web-map.md`). The build precomputes one body per proxy kind and
range (stored as `/v1/targets/<kind>/<range>`); the code is
`api/internal/build/targets_answer.go`.

## Query

| Parameter | Values | Default |
|---|---|---|
| `kind` | `residential`, `datacenter`, `mobile` or `isp` | `residential` |
| `range` | `24h`, `7d` or `30d` | `30d` |

Any other value, or any other parameter, answers 400. A kind with no measured network
answers 200 with no `networks`.

## Sources

`w` is the range's window of the network's snapshot product group for the kind
(`products[].windows[<range>]`; every range, not only 30d). The dataset is the
`/v1/dataset` dataset of the same build. Rates are fractions from 0 to 1.

**No engine mixing.** A browser target's figures are those of one engine at a time
(`camoufox` or `chrome`); means, statuses and ranks are per engine and never add Chrome
and Camoufox tries together. A network target (`generic_http`, `generic_https`) has the
engine `http`.

## Body

| Field | Unit | Source |
|---|---|---|
| `generatedAt` | ISO time | the build's `generatedAt` |
| `range`, `kind` | | the request's (after defaults) |
| `cardCount` | targets | how many `targets[]` have `card` true |
| `targets[]` | | every dataset target (`/v1/dataset → targets[]`), in dataset order (`sort_order`) |
| `networks[]` | | every network with a snapshot product group of the kind, in the rankings' order (below) |

### `targets[]`

| Field | Unit | Source |
|---|---|---|
| `id`, `label`, `category`, `passCondition`, `engine`, `group` | | the dataset target (`site.targets`, `public.targets`; `group` from the snapshot's `[target_groups]`); `engine` is `http`, `browser`, or null for a catalog-only target |
| `sources` | target ids | a group target's sources in order (the snapshot's `[target_groups]`); `[]` on every other target |
| `scoreWeight` | 1 to 5 | the snapshot methodology's `[browser_difficulty]`: the target's weight in the overall browser score; a group without one takes its hardest source's (`policy.browser_difficulty`). Null for a target without one (network targets) |
| `difficulty` | 1 to 5 | the catalog's display difficulty (`site.targets.difficulty`); `scoreWeight` when the catalog has none |
| `card` | boolean | true when the target is measured (`source` `measured`) and is no group's source (`group` null) |
| `fleet.<engine>.meanSuccess` | fraction | the unweighted mean, over the networks of the answer with tries of the engine, of each network's success on the target: a browser target's `engines.<engine>.successes / requests`, a network target's `successRate` (engine `http`) |
| `fleet.<engine>.networks` | networks | how many networks the mean is over |
| `scoreStatus.<engine>.status` | | the success score status the networks of the answer share for the engine (`w.targets[id].browser.engines.<engine>.scores.success.status`): `ranked`, `all_similar` or `too_few_networks` from any network with enough tries (all such networks share it); `not_enough_data` when no network of the answer has enough. See "Browser scores" in `api/FIELDS.md`. Browser targets only (`{}` on the others) |
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
"Browser targets") from the range's window. It does not repeat the main engine's
`outcomes`, `pageLoadP50`/`P90` and `scores`: read them at `engines[engine]`. It also has
no group `sources[]` breakdown: each source (`targets[].sources`) has its own cell. Without
these copies a body fits the size cap with room for more networks (below).

| Field | Unit | Source |
|---|---|---|
| `successRate`, `captchaRate`, `blockRate` | fraction | `w.targets[id].metrics.success_rate`, `.challenge_rate`, `.block_rate` (a browser target's: its `engine`'s) |
| `requests` | tries | `w.targets[id].metrics.completed`; 0 when unmeasured |
| `latencyP50`, `latencyP90` | milliseconds | `generic_http` only (the headline latency of `w.targets.generic_http.metrics`: latency under load in every-request snapshots); null on every other target |
| `engine` | `camoufox` or `chrome` | browser targets only: the engine with the most completed tries, whose figures the rates are (`w.targets[id].browser.engine`); null when unmeasured |
| `engines.<engine>.requests`, `successes` | tries | browser targets only: `w.targets[id].browser.engines.<engine>.completed`, `.successes` |
| `engines.<engine>.outcomes.*` | fraction | `.outcome_rates` (`ok`, `challenge`, `block`, `loginWall`, `timeout`, `proxyError`, `noContent`, `noAnswer`, `otherError`) |
| `engines.<engine>.pageLoadP50`, `pageLoadP90` | milliseconds | `.page_load_ms.p50`, `.p90` (successful tries) |
| `engines.<engine>.scores` | | `.scores`: the engine's rank-based `success`, `challenge` and `block` scores `{score, rank, rankedOf, status}`; null in a snapshot without scores |
| `costPer1kSuccess` | dollars per 1,000 successful requests | the window cost rule (`/v1/rankings` `costPer1kSuccess`) on the target's own wire bytes: `(w.targets[id].metrics.bytes_up + bytes_down) / completed × 1,000 / 2³⁰` GB at the network's list rate for the kind (`productMeasurements.<kind>.pricing`: `pricePerGbAtScale`, else `pricePerGb`), over the request success rate (`request_success_rate`, else `success_rate`), 4 dp. Null when the target has no wire bytes (W11), no list rate or no success |
| `successRank {rank, peers}` | ordinal, networks | the cell's competition rank by `successRate` (higher first; ties share a rank) among the networks of the answer whose cell has the same engine and a rate; `peers` is how many those are; `rank` null without a rate |
| `costRank {rank, peers}` | ordinal, networks | the same by `costPer1kSuccess`, lower first |

## Size

At the QA-1 scale (18 networks, nine browser targets and two network targets in every
window) the largest body is 132,604 bytes, under the 200 KB cap
(`TestEveryAnswerFitsTheCap`). The cell keeps no copy of the main engine's figures and no
group breakdown: with them it was 214 KB.

## Tests

`api/internal/build/targets_answer_test.go` (meta, order, engines, cost and ranks, 24h
against 30d, the JSON shape, the query) and the golden hashes
`api/internal/server/testdata/answers-v4-scale/targets.json`.

# `/v1/overlap`: every pair, exclusivity, ownership labels and resellers

One answer serves the whole overlap picture of a kind for one range: every network's pool basis,
exclusivity, ownership label and likely upstream, every pair of networks, and the market's
duplication on one basis. It serves the overlap page, the home's exclusivity card and the network
page's ownership card (`docs/api-web-map.md` section 7, OVL-1 to OVL-7; section 11, W5).

Code: `api/internal/build/overlap_answer.go` (the answer) and `overlap_facts.go` (the label, the
upstream and the market totals, shared with the home answer, W6).

## Request

`GET /v1/overlap?kind=&range=`

| Parameter | Values | Default |
|---|---|---|
| `kind` | a proxy kind (`residential`, `mobile`, `isp`, `datacenter`, ...) | `residential` |
| `range` | `24h`, `7d`, `30d` | `30d` |

Another parameter answers 400 `unknown_parameter`, a bad value 400 `invalid_parameter` (`api/README.md`, "Endpoints"). A kind no network is measured in answers 404 `not_found`,
except `residential`, which is always built (with no snapshot, its `networks` is empty). One body
is stored per kind and range (`/v1/overlap/<kind>/<range>`), precomputed with the build.

## Body

| Field | What | Source |
|---|---|---|
| `generatedAt`, `kind`, `range` | The build time and the request. | |
| `networks[]` | Every network measured in the kind, in catalog order. | the kind's snapshot product per network |
| `networks[].slug`, `name` | The network. | catalog |
| `networks[].poolIps` | The range's `uniqueIps` (`stats.<r>.uniqueIps`, the K8 hour formula), the pool every other answer shows; the observed pool for a window without the formula's figure (EXCLUSIVE-ESTIMATE-1, #1283). | `poolBasis` |
| `networks[].poolBasis` | `unique_ips`: the range's `uniqueIps`. Without its formula figure: `overlap_ips_ours`, the pairs' `ips_ours`, or `window_unique_ips`, the window's measured `pool.unique_ips` for a network with no pair. Null when none is known. | |
| `networks[].observedPoolIps` | The measured pool the shares were computed on: the pairs' `ips_ours`, or the window's measured `pool.unique_ips` for a network with no pair. Null when neither is known. | `windows[r].overlap.peers[].ips_ours`, `pool.unique_ips` |
| `networks[].exclusiveShare`, `sharedShare` | Share of the pool seen in no other network of the kind, and in at least one. A network with no peers is 1 and 0. | `windows[r].overlap.exclusive_share`, `shared_share` |
| `networks[].exclusiveIps` | `poolIps × exclusiveShare`, rounded: the network's exit IPs no other network of the kind saw in the range, on the range's `uniqueIps` (the measured share × the hour formula's pool; the overlap is counted per network, so the network's share scales the network's total). Shifter 30d at the 2026-11-16 19:10 build: 29.8% of 6.91M = 2.06M. Null when either is unknown. | |
| `networks[].observedExclusiveIps` | `observedPoolIps × exclusiveShare`, rounded: the measured count (the `exclusiveIps` served before #1283). Null when either is unknown. | |
| `networks[].exclusiveScore` | `{score, rank, rankedOf, status}`: `exclusiveIps` on the 1-10 scale, `max(1, 10 × exclusiveIps ÷ the highest exclusiveIps of the kind)`; `rank` by `exclusiveIps` among the networks with one (a tie shares the average place), `rankedOf` how many, `status` `ranked`. Null with `exclusiveIps` (same reason). Display only (`FIELDS.md`, "Exclusive score"; EXCLUSIVE-SCORE-1). | |
| `networks[].label` | `fully_exclusive`, `mostly_exclusive`, `mixed` or `resale` (rule below). Null when a share is unknown. | |
| `networks[].likelyUpstream` | `{slug, name, shareOfOurs}` of the network its pool sits mostly inside (rule below), or null. | |
| `networks[].peers[]` | Every network of the kind it shares exit IPs with in the range, most shared first (ties by slug): `slug`, `sharedIps`, `shareOfOurs` (measured shared ÷ `ips_ours`), `shareOfTheirs` (measured shared ÷ `ips_theirs`). `sharedIps` is on this network's pool: `shareOfOurs × poolIps`, rounded (#1283), so one pair's two rows can differ; the shares and the order are the measured pair's. | `windows[r].overlap.peers`, as the snapshot's overlap rows of the range |
| `market.networks` | How many networks are in `networks[]`. | |
| `market.sumOfPools` | Σ `poolIps`. | |
| `market.observedSumOfPools` | Σ `observedPoolIps`. The market's other counts are the measured ones × `sumOfPools ÷ observedSumOfPools` (#1283). | |
| `market.sharedIps` | The measured shared IPs of every pair, each pair once, × `sumOfPools ÷ observedSumOfPools`, rounded. | |
| `market.distinctIps` | The distinct IPs across the kind's networks in the range, each IP once however many networks saw it: the unique IPs of the kind's fleet entry (cohort `*:<kind>`, CALC-PAGE-INPUTS-1), which the calculator counts as `count(DISTINCT ip_id)` over the kind's runs in the window, for 24h, 7d and 30d, × `sumOfPools ÷ observedSumOfPools`, rounded (#1283). Null with `insufficient_data` when the snapshot publishes no such entry; never `sumOfPools − sharedIps`, which counts an IP of k networks k(k−1)/2 times (OVERLAP-DISTINCT-2). | `fleet[]` row `*:<kind>`, `unique_ips` |
| `market.basis` | `kind_fleet`: `distinctIps` is the kind's fleet entry. Null with `distinctIps`. | |
| `market.duplicatedShare` | `1 − distinctIps / sumOfPools` (at least 0), on the measured counts (the kind fleet's unique IPs ÷ `observedSumOfPools`), unchanged by #1283; null with `insufficient_data` with no pool or no `distinctIps`. | |
| `market.likelyResellers` | The slugs of the networks with a `likelyUpstream`, the largest `shareOfOurs` first. | |
| `na`, `naReason`, `naReasons` | The market's nulls with their reason; each `networks[]` row carries its own, with `peers` when empty. Every range's pairs are computed (24h every build, 7d and 30d once a UTC day). A network with no run in the range has `no_run_in_window`, every other null `insufficient_data`; `likelyUpstream` is null by contract (`answers/README.md`, "Missing data"). | |

Every figure of a body is on one basis: this kind's and this range's pools and pairs. The shares are
the measured ones; the IP counts are on the range's `uniqueIps` (#1283). It
never mixes 24h pools with 30d pairs, the defect of the dataset's `totals.distinctIpsAcrossFleet`
(OVL-7).

## The rules (the old site's)

### Ownership label

Ported from the old site's `ownershipFor` (`src/lib/ownership.ts` of the frozen `proxy-index-api`
repository, at the parent of `85b74f2`):

```ts
const fullyExclusive = known && exclusive === 1 && shared === 0;
const label = notCompared ? 'Not compared yet'
  : !known ? 'N/A'
  : fullyExclusive ? 'Fully exclusive'
  : exclusive! >= 0.75 ? 'Mostly exclusive'
  : exclusive! >= 0.4 ? 'Mixed sourcing'
  : 'Resale signal';
```

| `label` | Rule |
|---|---|
| `fully_exclusive` | `exclusiveShare` is exactly 1 and `sharedShare` exactly 0 (0.9999 is not rounded up) |
| `mostly_exclusive` | `exclusiveShare ≥ 0.75` |
| `mixed` | `exclusiveShare ≥ 0.4` |
| `resale` | below 0.4 |
| null | either share unknown (the old site's "N/A" and "Not compared yet") |

### Likely upstream and resellers

The old site's resale rule (its methodology page: "flagged as resale when shareOfOurs > 45% and
shareOfOurs > 3 × shareOfTheirs"; its generator applies it to the largest pair):

```ts
const top = overlaps[0]; // most of our pool first
const likelyUpstream =
  top && top.shareOfOurs > 0.45 && top.shareOfOurs > top.shareOfTheirs * 3
    ? { slug: top.slug, name: top.name, shareOfOurs: top.shareOfOurs }
    : null;
```

So a network's `likelyUpstream` is its first peer when more than 45% of its pool is in that
network and that share is more than 3 times the other's share inside it, that is, the other pool
is more than 3 times larger. Peers are most shared first, which for one network is also most of
its pool first. `market.likelyResellers` are the networks with one.

The `label` and the upstream are separate rules, as on the old site: a network can be
`resale` with no `likelyUpstream` (its shared part spread over several networks), and the other
way round.

## Size

About 1 KB per network with every pair: 18 networks all paired with each other are under 30 KB
(`TestEveryPairOfEighteenNetworksFits`), far under the 200 KB cap.

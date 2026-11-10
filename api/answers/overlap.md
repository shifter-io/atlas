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

Another parameter, or a bad value, answers 400. A kind no network is measured in answers 404,
except `residential`, which is always built (with no snapshot, its `networks` is empty). One body
is stored per kind and range (`/v1/overlap/<kind>/<range>`), precomputed with the build.

## Body

| Field | What | Source |
|---|---|---|
| `generatedAt`, `kind`, `range` | The build time and the request. | |
| `networks[]` | Every network measured in the kind, in catalog order. | the kind's snapshot product per network |
| `networks[].slug`, `name` | The network. | catalog |
| `networks[].poolIps` | The measured pool the shares were computed on, never the K8 estimate. | `poolBasis` |
| `networks[].poolBasis` | `overlap_ips_ours`: the pairs' `ips_ours`. `window_unique_ips`: the window's measured `pool.unique_ips`, for a network with no pair. Null when neither is known. | |
| `networks[].exclusiveShare`, `sharedShare` | Share of the pool seen in no other network of the kind, and in at least one. A network with no peers is 1 and 0. | `windows[r].overlap.exclusive_share`, `shared_share` |
| `networks[].exclusiveIps` | `poolIps × exclusiveShare`, rounded. Null when either is unknown. | |
| `networks[].label` | `fully_exclusive`, `mostly_exclusive`, `mixed` or `resale` (rule below). Null when a share is unknown. | |
| `networks[].likelyUpstream` | `{slug, name, shareOfOurs}` of the network its pool sits mostly inside (rule below), or null. | |
| `networks[].peers[]` | Every network of the kind it shares exit IPs with in the range, most shared first (ties by slug): `slug`, `sharedIps`, `shareOfOurs` (`sharedIps / ips_ours`), `shareOfTheirs` (`sharedIps / ips_theirs`). | `windows[r].overlap.peers`, as the snapshot's overlap rows of the range |
| `market.networks` | How many networks are in `networks[]`. | |
| `market.sumOfPools` | Σ `poolIps`. | |
| `market.sharedIps` | Σ `sharedIps` of every pair, each pair once. | |
| `market.distinctIps` | The kind's fleet entry's unique IPs (cohort `*:<kind>`, CALC-PAGE-INPUTS-1) when the snapshot publishes one; otherwise `max(0, sumOfPools − sharedIps)`. | |
| `market.basis` | `kind_fleet` or `sum_of_pools_minus_shared_ips`: which `distinctIps` is. The second counts an IP held by three or more pools more than once. | |
| `market.duplicatedShare` | `1 − distinctIps / sumOfPools` (at least 0); null with no pool. | |
| `market.likelyResellers` | The slugs of the networks with a `likelyUpstream`, the largest `shareOfOurs` first. | |
| `na`, `naReason`, `naReasons` | The market's nulls with their reason; each `networks[]` row carries its own, with `peers` when empty. Every range's pairs are computed (24h every build, 7d and 30d once a UTC day). A network with no run in the range has `no_run_in_window`, every other null `insufficient_data`; `likelyUpstream` is null by contract (`answers/README.md`, "Missing data"). | |

Every figure of a body is on one basis: this kind's and this range's measured pools and pairs. It
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

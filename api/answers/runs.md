# `/v1/networks/{slug}/runs`

Every community run of a network, filtered and paged, with the medians the review page's
"Do they agree?" block compares with the Index (NET-RUNS-PAGED). The network answer holds
only the newest 20 runs. Built in `api/internal/build/runs_answer.go`: each build holds
every network's runs from `site.community_runs`, and a page is cut from that list on its
first request and kept in the page cache, as the place pages are.

## Request

`GET /v1/networks/{slug}/runs?kind=&status=&page=`

| Parameter | Values | Default |
|---|---|---|
| `kind` | `residential`, `datacenter`, `mobile`, `isp`, or `any` (every product) | the network's primary kind, as `/v1/networks/{slug}`; `any` for `all` |
| `status` | `accepted`, `pending`, `rejected` or `any` | `any` |
| `page` | 1 or more | 1 |

A run is of a kind when its product kind is the kind or starts with it and `_`
(`residential_rotating` is `residential`). `{slug}` is a network, or `all` for every
network's runs. A bad value or another parameter answers 400; an unknown network or a page
past the last answers 404.

## Body

| Field | What |
|---|---|
| `generatedAt`, `network` | The build's time and the slug. |
| `kind`, `status` | The filter (`kind` is the kind used, the primary one by default). |
| `kindDefault` | True when no `kind` was given (`kind` is then the primary one). |
| `page`, `pages`, `total` | This page, the number of pages (at least 1) and the runs of the kind and status. Pages hold 20 runs, newest first. |
| `counts` | The runs of the kind, whatever the status: `accepted`, `total`. |
| `agreement` | The medians of the kind's accepted runs: `successRate`, `latencyP50`, `uniqueIps`, `overlapShare` (each null when no accepted run has it), and `runs`, the accepted runs. |
| `rows` | The page's runs, in the `CommunityRun` shape (`FIELDS.md`). |

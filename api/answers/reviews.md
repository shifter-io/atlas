# `/v1/networks/{slug}/reviews`

Every published review of a network, filtered and paged (NET-REVIEWS-PAGED). The network
answer (`/v1/networks/{slug}`) holds only the newest 20; this answer pages all of them.
Built in `api/internal/build/reviews_answer.go`: each build holds every network's
published reviews from `site.reviews`, and a page is cut from that list on its first
request and kept in the page cache, as the place pages are (same headers and ETags).

## Request

`GET /v1/networks/{slug}/reviews?rating=&verified=&page=`

| Parameter | Values | Default |
|---|---|---|
| `rating` | `1` to `5`: only reviews with that rating | every rating |
| `verified` | `invoice` (`invoice_verified`), `run` (`run_verified`) or `any` (either of the two) | every review |
| `page` | 1 or more | 1 |

`{slug}` is a network, or `all` for every network's reviews. A bad value answers 400
`invalid_parameter` (a `page` outside 1 to 100,000 `out_of_range`), another parameter 400
`unknown_parameter`. An unknown network, or a page past the last, answers 404 `not_found`
(`api/README.md`, "Endpoints"); a filter
that keeps nothing answers page 1 with no rows.

## Body

| Field | What |
|---|---|
| `generatedAt` | The build's time. |
| `network` | The slug. |
| `rating`, `verified` | The filter (null when not given). |
| `page`, `pages`, `total` | This page, the number of pages (at least 1) and the reviews the filter keeps. Pages hold 20 reviews, newest first. |
| `counts` | Every published review of the network, whatever the filter: `published`, `verified` (invoice or run verified), `runVerified`, `byRating` (5 counts, ratings 1 to 5). |
| `rows` | The page's reviews: the `UserReview` shape (`FIELDS.md`) plus `verificationLabel`, the old site's label: `Invoice verified`, `Run verified`, `Unverified`. Each row has `lang`, the BCP 47 language of what the user wrote (`title`, `body` and `providerReply` stay as written), and `monthlySpendMin`, `monthlySpendMax`, the monthly spend's bounds in US dollars, each null when unknown (PROVIDER-CODES-1). The text `monthlySpend` stays until #991, and `useCase` then holds a code of the `use_case` set (`/v1/codes`). |

A published review is one whose verification is not `pending`. Reviews of every product of
the network are listed (no `kind` filter).

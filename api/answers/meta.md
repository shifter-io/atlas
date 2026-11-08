# `/v1/meta`

The build's reference data every Proxy Index page shares: the score weights, the ranking
profiles, the grade bands, the target list and its counts, the ranges and the test scopes.
Until this answer they were only in the dataset file (`/v1/dataset`, about 9 MB) or written
into the website. Map: `docs/api-web-map.md`, section 11, W7 (HC-1, HC-2, HC-3).

- **Route:** `GET /v1/meta`. It takes no query parameter; any parameter answers 400.
- **Bodies:** one per build, stored as `/v1/meta`. It changes only with a new build, so it
  gets the same ETag, 304s and cache lifetime as every other answer.
- **Size:** about 2 KB on the test fixtures (every target, no profile or scope rows); the tests require under 20 KB.
- **Code:** `api/internal/build/meta_answer.go`. It reads only what the build already holds.

## Fields

| Field | Type | Source |
|---|---|---|
| `generatedAt` | ISO time | The build's time, as `Dataset.generatedAt`. |
| `snapshot.id`, `snapshot.asOf`, `snapshot.methodologyId` | text, ISO time, text; null when nothing is published | The published calculation, as `Dataset.snapshot.{snapshotId, asOf, methodologyId}`. |
| `weights` | the 8 score categories (`reliability` ... `value`), each a weight in percent | `Dataset.weights`: the snapshot's `methodology.settings.weights`, else `site.score_weights`. |
| `profiles[]` | `id`, `label`, `description`, `weights` (the 8 categories), `na` | `Dataset.profiles` (`site.ranking_profiles`). |
| `grades[]` | `{grade, min}` from the highest band down, such as `{"grade": "A+", "min": 88}`; `min` is the lowest 0-100 score that earns the grade | The snapshot's `methodology.settings.grades` (framework `configs/methodology.toml` `[grades]`). Null, and listed in `na`, when the snapshot publishes none or nothing is published. |
| `targets[]` | `TargetMeta` (`id`, `label`, `category`, `passCondition`, `difficulty`, `source`, `engine`, `group`, `na`), plus `scoreWeight` and `card`, in dataset order | `Dataset.targets` (`site.targets`, `public.targets`, the snapshot's `[target_groups]`). |
| `targets[].scoreWeight` | integer 1 (easy) to 5 (hard), or null | The target's weight in the browser overall score: the snapshot's `methodology.settings.browser_difficulty`; a group target without one takes its hardest source's, as the framework does (`policy.py browser_difficulty`); the same value as `/v1/targets` `targets[].scoreWeight` (`build.scoreWeights`). Null for a target the score does not weigh (an HTTP or catalog-only target, which never lists it in `na`), and for a browser target the snapshot gives none (listed in its `na`). |
| `targets[].card` | boolean | True for a measured target (`source` `measured`) outside any group (`group` null): the targets a page shows as cards. The AI source targets (`group` `ai_answers`) and catalog-only rows are not cards. |
| `targetCounts.all` | integer | Every target of `targets[]`. |
| `targetCounts.measured` | integer | The targets with `source` `measured` (as `Dataset.measuredMarket.targetCount`). |
| `targetCounts.cards` | integer | The targets with `card` true. |
| `ranges[]` | `id` (`24h`, `7d`, `30d`), `label`, `short`, `days`, `bucket`, `resolution`, `axis`, `phrase` | The API's range list (`build.Ranges`, the website's `ranges.ts`). |
| `scopes[]` | `TestScope`: `id`, `label`, `summary`, `cadence`, `targets` (ids or `"all"`), `liveIpSampling`, `poolTracking`, `overlapAudit`, `stickinessTest`, `geoCoverage`, `rationale`, `na` | `Dataset.scopes` (`site.scopes`). |
| `na` | paths | The answer's own null fields (`grades`, `snapshot.*`, `weights.*`); `targets[]`, `profiles[]` and `scopes[]` carry their own `na`. |

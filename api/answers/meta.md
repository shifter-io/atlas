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
| `profiles[]` | `id`, `label`, `weights` (the 8 categories), `na` | `Dataset.profiles` (`site.ranking_profiles`, written by `api/scripts/catalog-profiles.sql`: `balanced`, `hard-targets`, `cost-first`, `latency-first`, `compliance`). A client re-ranks with a profile from each network's `breakdown`; no profile score is published. The profile's `description` is not served (#962). |
| `grades[]` | `{grade, min}` from the highest band down, such as `{"grade": "A+", "min": 88}`; `min` is the lowest 0-100 score that earns the grade | The snapshot's `methodology.settings.grades` (framework `configs/methodology.toml` `[grades]`). Null, and listed in `na`, when the snapshot publishes none or nothing is published. |
| `targets[]` | `TargetMeta` (`id`, `label`, `category`, `passCondition`, `difficulty`, `source`, `engine`, `group`, `na`), plus `scoreWeight` and `card`, in dataset order | `Dataset.targets` (`site.targets`, `public.targets`, the snapshot's `[target_groups]`). `category` and `difficulty` are `site.targets`' (`api/scripts/catalog-targets.sql`); a browser target's `difficulty` equals its `scoreWeight` (`tests/test_catalog_targets.py`); every difficulty is the owner's fixed value, never derived from results (`tests/test_target_difficulty.py`, "Target difficulty" in `api/FIELDS.md`). |
| `targets[].scoreWeight` | integer 1 (easy) to 5 (hard), or null | The target's weight in the browser overall score: the snapshot's `methodology.settings.browser_difficulty`; a group target without one takes its hardest source's, as the framework does (`policy.py browser_difficulty`); the same value as `/v1/targets` `targets[].scoreWeight` (`build.scoreWeights`). Null for a target the score does not weigh (an HTTP target, which weighs its `[http_difficulty]`, 1, only in `successScore`, or a catalog-only target, which never lists it in `na`), and for a browser target the snapshot gives none (listed in its `na`). |
| `targets[].card` | boolean | True for a measured target (`source` `measured`) outside any group (`group` null): the targets a page shows as cards. The AI source targets (`group` `ai_answers`) and catalog-only rows are not cards. |
| `targetCounts.all` | integer | Every target of `targets[]`. |
| `targetCounts.measured` | integer | The targets with `source` `measured` (as `Dataset.measuredMarket.targetCount`). |
| `targetCounts.cards` | integer | The targets with `card` true. |
| `ranges[]` | `id` (`24h`, `7d`, `30d`), `short`, `days`, `axis`, `hours` | The API's range list (`build.Ranges`, the website's `ranges.ts`). The range's `label`, `bucket`, `resolution` and `phrase` are not served (#962). |
| `ranges[].hours` | integer (24, 168, 720), or null | The range's methodology window in hours: its `days` × 24 when the snapshot's `methodology.settings.windows` (`configs/methodology.toml` `windows`) has that window. Null, and listed in `na` as `ranges[].hours`, when it has not or nothing is published. |
| `windowHours` | integer (24), or null | The shortest of the snapshot's `methodology.settings.windows`. Null, and listed in `na`, when nothing is published. |
| `scopes[]` | `TestScope`: `id`, `targets`, `countries`, `cadence`, `geoCoverage`, `liveIpSampling`, `poolTracking`, `overlapAudit`, `stickinessTest`, `na`: codes and numbers, the website writes the words (#966; example below) | `Dataset.scopes` (`site.scopes`, written by `api/scripts/catalog-scopes.sql`: the five scopes of the website's methodology page, in its order). The scope's `label`, `summary`, `rationale` and its `cadence` and `geoCoverage` sentences are not served. |
| `scopes[].id` | code | The product kind (`residential_rotating`, `residential_sticky`, `isp_static`, `datacenter`, `mobile`). |
| `scopes[].targets` | target ids | A scope over every target (`all_targets`): the ids of `targets[]` with `source` `measured`, in their order. Else its own `target_ids`. |
| `scopes[].countries` | integer, or null | The distinct requested countries (a network answer's `requestedCountries`) of the published product groups of every network whose measured product of a kind is this scope's. A scope is measured when a published network's measured product of a kind (its `productMeasurements`, else its `measuredProduct`) is the scope's id: today only `residential_rotating`. Null, and listed in `na` with the reason `not_measured`, when no published network measures the product. |
| `scopes[].cadence` | `{dailyTestsPerNetworkCountryPerDay, browserTriesPerTargetPerDay, pulseIntervalMinutes}` | Production's cadence (`docs/api-web-map.md` section 14): 1 daily test per network and country a day (`formulas/<network>.json` `schedule`), 100 browser tries per network and target a day (the browser queue's `[[target]]` `tries = 100` every `every_minutes = 1440`). Neither the snapshot nor the database carries them, so they are constants in `build/meta_scopes.go`, held to the configuration by `TestTheScopeCadenceIsTheConfigurations`: every target's `tries` × 1440 ÷ its `every_minutes` in the production browser queue (`configs/browser-pulse.production.example.toml`), and one scheduled UTC hour per country in every `formulas/*.json` `schedule`. `pulseIntervalMinutes` is the published snapshot's pulse interval: the scoring `slots.pulse_interval_minutes` of its first product group with a pulse batch in a window (`uptime.batches` above 0; 30 on the test days). Without a pulse batch it is null, and listed in `na` with the reason `no_pulse_fleet`: production's daily tests run no pulses (#841), and the calculator writes the targets' configured interval even then, so the interval alone does not show a pulse. |
| `scopes[].geoCoverage` | `{code, countries}` | `code` is `site.scopes.geo_coverage`: `all_usable_countries`, `provider_inventory` or `restricted` (null, and in `na` with the reason `catalog`, for any other value). `countries` is the scope's tested countries, the same count as `scopes[].countries`; null, and in `na` with the reason `not_measured`, for a scope no published network measures (#994; it was 6 for `restricted`, a number the configuration does not carry). |
| `scopes[].liveIpSampling`, `poolTracking`, `overlapAudit`, `stickinessTest` | boolean, or null | `site.scopes`; null, and in `na` with the reason `catalog`, when the row leaves it empty. |
| `scopes[].na`, `naReason`, `naReasons` | paths, codes | The scope's null fields, each with its reason (#994): `naReason` the most common, `naReasons` each path whose reason differs, as every answer's (`answers/README.md`, "Missing data"). |
| `na`, `naReason`, `naReasons` | paths, codes | The answer's own null fields (`grades`, `snapshot.*`, `weights.*`, `ranges[].hours`, `windowHours`) and `profiles` when empty (`catalog`: no `site.ranking_profiles` rows), each with its reason (`answers/README.md`, "Missing data"); `targets[]`, `profiles[]` and `scopes[]` carry their own `na`. |

## Scope example

Two of the five scopes, with every network's residential product measured as
`residential_rotating` (so no published network measures `mobile`), in production's
daily-test cadence (no pulse batch):

```json
[
  {"id": "residential_rotating",
   "targets": ["generic_http", "generic_https", "amazon_pdp", "google_search", "linkedin_public", "instagram_web",
               "cloudflare_challenge", "ai_answers", "ai_overview", "ai_chatgpt", "ai_gemini"],
   "countries": 233,
   "cadence": {"dailyTestsPerNetworkCountryPerDay": 1, "browserTriesPerTargetPerDay": 100, "pulseIntervalMinutes": null},
   "geoCoverage": {"code": "all_usable_countries", "countries": 233},
   "liveIpSampling": true, "poolTracking": true, "overlapAudit": true, "stickinessTest": false,
   "na": ["cadence.pulseIntervalMinutes"], "naReason": "no_pulse_fleet"},
  {"id": "mobile",
   "targets": ["generic_http", "generic_https", "amazon_pdp", "google_search", "linkedin_public", "instagram_web",
               "cloudflare_challenge", "ai_answers", "ai_overview", "ai_chatgpt", "ai_gemini"],
   "countries": null,
   "cadence": {"dailyTestsPerNetworkCountryPerDay": 1, "browserTriesPerTargetPerDay": 100, "pulseIntervalMinutes": null},
   "geoCoverage": {"code": "restricted", "countries": null},
   "liveIpSampling": true, "poolTracking": true, "overlapAudit": true, "stickinessTest": true,
   "na": ["cadence.pulseIntervalMinutes", "countries", "geoCoverage.countries"],
   "naReason": "not_measured", "naReasons": {"cadence.pulseIntervalMinutes": "no_pulse_fleet"}}
]
```

The scope's `label` and `summary` stay in `site.scopes` for `/v1/networks/{slug}`
`profile.protocol` (`label` is also the coverage report's scope name); they are not
in this answer or the dataset file.

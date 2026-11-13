# `/v1/codes`

Every code the API sends, in one catalog (CODES-CATALOG-1, #975): each code set, the fields
that hold it, each code's parameters with their type and unit, and one English sentence for
documentation only. A site in any language translates the codes from it; no other answer
carries English words for them. The same list, with the conventions every answer follows, is
`api/CODES.md`, written from the same registry.

- **Route:** `GET /v1/codes`. It takes no query parameter; any parameter answers 400 `unknown_parameter` (`api/README.md`, "Endpoints").
- **Bodies:** one per build, stored as `/v1/codes`: the catalog sets' codes are the build's
  catalog rows, so it is a page answer (not in the dataset file). Same ETag, 304s and cache
  lifetime as every other answer.
- **Size:** a few tens of KB (about 40 sets); far under the 200 KB cap (`TestEveryAnswerFitsTheCap`).
- **Code:** `api/internal/build/codes.go` (the registry and the answer) and one
  `api/internal/build/codes_<area>.go` per area, each registering its sets from an `init()`
  with `build.RegisterCodeSet(build.CodeSet{...})`, as the answers register themselves. A
  repeated set or code, a parameter of an unknown type, or a set without codes panics.

## Fields

| Field | Type | Source |
|---|---|---|
| `contract` | date (`YYYY-MM-DD`) | `build.Contract`: the date of the API's last breaking change, the same as `/v1/version` `contract`. `api/CHANGES.md` lists what changed on each date. |
| `generatedAt` | ISO time | The build's time. |
| `currency` | code | `USD`: every price in the API is a plain number in US dollars. |
| `sets[]` | `{name, usedIn, source, values}`, by `name` | Every registered set. |
| `sets[].name` | code | The set, such as `event` or `na_reason`. |
| `sets[].usedIn` | paths | The fields that hold the set's codes: a route (`*` for every answer and the dataset file), a space, then a path in the body (`[]` every list element, `<id>` any key, `<key>` the keys themselves, `**` any depth, never inside `naReasons`). |
| `sets[].source` | `api` or `catalog` | `api`: the API's own codes, fixed in the code. `catalog`: the build's catalog rows (`target` from `site.targets`, label and pass condition; `product_kind` from `site.scopes`, label and summary; `ranking_profile` from `site.ranking_profiles`, label and description). |
| `sets[].values[]` | `{code, en, params}` | Each code; `en` is its English reference sentence (for documentation only, the only English the API serves), with `{name}` where a parameter goes. |
| `sets[].values[].params[]` | `{name, type, unit}` | The code's parameters (`[]` for a plain code): `type` is `number`, `integer`, `ratio`, `points`, `ms`, `seconds`, `minutes`, `usd`, `usd_per_gb`, `gb`, `mb`, `count`, `country`, `country_list`, `code:<set>`, `timestamp`, `date` or `name`; `unit` is a code of the `unit` set when the type does not say it, else null. |

## Tests

- `TestEveryServedCodeIsRegistered` (server package) walks every answer, a page of every long
  list and the dataset file of the scan build: every value at a set's `usedIn` path is null or
  a code of that set, and every path holds a value somewhere (or is listed, with why, as one
  the scan build cannot reach).
- `TestCodesDocIsCurrent` (build package) fails when `api/CODES.md` differs from the registry;
  `GOLDEN_WRITE=1` rewrites it.

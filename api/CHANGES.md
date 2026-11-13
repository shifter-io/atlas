# API changes by contract date

`/v1/version` and `/v1/codes` serve `contract`, the date of the API's last breaking change
(`build.Contract`). Each section below is one contract date, newest first: one line per field,
`added`, `changed` or `removed`, with its endpoint and, for a change, the old and the new form.
A task that changes a served field adds its lines here, and moves the contract date when the
change is breaking (`AGENTS.md`, "Changing an API field").

## 2026-11-13

- added: `GET /v1/codes` (every code set the API sends; `api/answers/codes.md`, `api/CODES.md`).
- added: `/v1/version` `contract`.
- removed: `/v1/networks/{slug}/chart` and `/v1/networks/all/chart` `labels` (`success`, `availability`, `latency`, `latencyUnderLoad`, `pool`); each point's `t` stays (UNREAD-TEXT-1, #977).
- changed: outage bands, `/v1/networks/{slug}/chart` `bands[]` and dataset file `measuredMarket.views[].fastest.stats.bands[]`, `.mostReliable.stats.bands[]`: `label` (`"major · AD"`) removed; `severity` (an `incident_severity` code) and `country` (ISO code) added (#977).
- removed: `/v1/networks/{slug}` `profile.targets[].sources[].label`; dataset file `providers[].targets[].sources[].label`, `providers[].productMeasurements.<kind>.targets[].sources[].label`; `sources[].target` stays (#977).
- removed: `/v1/networks/{slug}` `profile.products[].name`; dataset file `providers[].products[].name`; `products[].kind` stays (#977).
- changed: `/v1/networks/all` `network.name`: `"All networks"` → null, `not_applicable` (#977).
- changed: every 4xx and 5xx body of every route: `{"error": "<sentence>"}` → `{"error": {"code", "params"}}`, a code of the `error` set (`api/CODES.md`); statuses and headers unchanged (TYPED-ERRORS-1, #980).
- added: code sets `error`, `error_format`, `unavailable_reason`, `unserved` (`/v1/codes`, `api/CODES.md`) (#980).
- changed: `/healthz` `reason`: `"calculation older than payload v4: recalculate"` → `payload_before_v4` (#980).
- changed: `/healthz` `unserved[]`: English lines → `{path, code, status, product}`; `"and N more"` → `unservedMore` (added, integer) (#980).
- changed: `/healthz` `fingerprint`: a string holding JSON → the JSON object (#980).

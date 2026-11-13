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
- added: `/v1/meta` and dataset file `scopes[].naReason`, `scopes[].naReasons` (each scope null's reason; #994).
- added: `na_reason` code `no_pulse_fleet` (a scope's `cadence.pulseIntervalMinutes` without a pulse batch; #994).
- changed: `/v1/meta` `scopes[].geoCoverage.countries`: the scope's tested-country count (null, `not_measured`, without a measuring network), was 6 for `restricted` and null otherwise (#994).
- changed: `/v1/meta` `scopes[].cadence.pulseIntervalMinutes`: the snapshot's pulse interval when it has a pulse batch, was always null (#994).
- removed: `/v1/networks/{slug}/places` and `/v1/networks/all/places` `rows[].country` (PLACE-CODES-1, #978; read `countryCode`).
- removed: `/v1/networks/{slug}` and `/v1/networks/all` `ranges.<range>.countries[].country` (#978; `countryCode`).
- removed: `/v1/countries` `countries[].country` (#978; `countryCode`).
- removed: `/v1/countries/{cc}` `country` and `ranges.<range>[].country` (#978; `countryCode`).
- removed: `/v1/countries/{cc}/cities` and `/regions` `rows[].country` (#978; `countryCode`).
- removed: `/v1/places/{region|city}/{key}` `rows[].country` (#978; `countryCode`).
- removed: dataset `providers[].geo[].country` and `providers[].productMeasurements.<kind>.geo[].country` (#978; `countryCode`).
- removed: dataset `lists.<slug>.<kind>.<range>.<dimension>.rows[][]` the country-name position (#978; read the rows by `columns`).
- added: `/v1/networks/{slug}/coverage` `columns` (#978; the names of the row positions, `place_column` codes).
- added: dataset `lists.<slug>.<kind>.<range>.<dimension>.columns` (#978).
- added: `country=` on `/v1/networks/{slug}/coverage` and `/places` (#978; a two-letter code, any case; 400 otherwise).
- changed: `q` on `/v1/networks/{slug}/coverage` and `/places` (#978): matches codes, region, city, ASN names and `AS<number>`; old: also English country names.
- changed: `/v1/networks/{slug}/coverage` `totals` (#978): also with `country=` alone; old: only with `q`.

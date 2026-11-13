# API changes by contract date

`/v1/version` and `/v1/codes` serve `contract`, the date of the API's last breaking change
(`build.Contract`). Each section below is one contract date, newest first: one line per field,
`added`, `changed` or `removed`, with its endpoint and, for a change, the old and the new form.
A task that changes a served field adds its lines here, and moves the contract date when the
change is breaking (`AGENTS.md`, "Changing an API field").

## 2026-11-13

- added: `GET /v1/codes` (every code set the API sends; `api/answers/codes.md`, `api/CODES.md`).
- added: `/v1/version` `contract`.
- changed: `/v1/coverage` `dataset.sections.fields[].reason`, `targets[].sections.fields[].reason`,
  `scopes[].sections.fields[].reason`, `profiles[].sections.fields[].reason`, `fields.<path>.reason` and
  `/v1/coverage/{slug}` `sections.<section>[].reason`: an English sentence (`"Editorial field: no
  site.providers.hq"`) → a code of the `na_reason` set (`"catalog"`).
- added: `/v1/coverage` `dataset.sections.fields[].source`, `targets[].sections.fields[].source`,
  `scopes[].sections.fields[].source`, `profiles[].sections.fields[].source`, `fields.<path>.source` and
  `/v1/coverage/{slug}` `sections.<section>[].source`: a code of the `na_source` set
  (`site.providers.hq`, `snapshot`) or null.
- removed: `/v1/coverage` `targets[].name`, `scopes[].name`, `profiles[].name` (the `id` stays).

# API changes by contract date

`/v1/version` and `/v1/codes` serve `contract`, the date of the API's last breaking change
(`build.Contract`). Each section below is one contract date, newest first: one line per field,
`added`, `changed` or `removed`, with its endpoint and, for a change, the old and the new form.
A task that changes a served field adds its lines here, and moves the contract date when the
change is breaking (`AGENTS.md`, "Changing an API field").

## 2026-11-13

- added: `GET /v1/codes` (every code set the API sends; `api/answers/codes.md`, `api/CODES.md`).
- added: `/v1/version` `contract`.

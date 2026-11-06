## What this repository is

One repository, two parts:

- The framework: the Python library and CLI in `src/proxy_index/` (tests in `tests/`,
  scripts in `scripts/`, configs in `configs/`, guides in `docs/`). See `README.md`.
- The API: the Go service `siteapi` in `api/` behind the Proxy Index website, with its
  tests, migrations and documentation (`api/README.md`, `api/FIELDS.md`). It moved here
  from the `proxy-index-api` repository with its history; that repository is frozen and
  takes no new changes. `api/fixtures/` is test input only; `api/` is not part of the
  Python package.

The website is built and deployed from its own repository and reads nothing but the
API; do not add website code here. New numbers the website needs go into the API
(`api/internal/build`, documented in `api/FIELDS.md`).

## Building and testing

Build and test on remote machines (CI or a disposable test server), never on a
workstation.

One CI job, `tier1` in `.github/workflows/ci.yml`, gates every ready pull request for
both parts, on one CI server: privacy, commit-message, commit-identity and secret checks
for every change, then

- Framework: `uv sync --locked`, ruff, the source limits, pytest with a PostgreSQL
  (database tests must not skip) and changed-line coverage of at least 85%.
- API, when `api/` (Markdown outside `api/fixtures/` aside), the framework's SQL
  (`src/proxy_index/**/*.sql`) or `ci.yml` changes: `gofmt`, `go vet` and `go test`
  with the database tests, each in its own scratch database in the job's PostgreSQL 17
  with the framework schema read from `src/proxy_index/` of the same checkout, beside
  pytest. A framework schema change that breaks the API's database tests fixes them in
  the same pull request. The latency test `TestServeFromDatabase` (p99 < 2 ms) runs
  alone in its own process after every other test of the job. See "Tests" in
  `api/README.md`.
- Always build `siteapi` from the current checkout (`cd api && go build
  ./cmd/siteapi`); `api/bin/` is ignored and never committed. An old binary serves
  only the routes it was built with and writes its own migration checksums.
- API migrations are checksummed: never edit an applied migration, even a comment;
  add a new one.

## Commits

No local paths, emails, private keys, tokens or real IP addresses in a change (the
privacy step), no AI attribution in commit messages, and GitHub noreply addresses only
for authors, committers and co-authors. `tier1` checks all of them on every change,
`api/` included.

## Private measurement results

- Never commit, push, or publish real IP-bearing measurement results from any provider.
  This includes raw observations, exports, sampled-IP fixtures, logs, and reports;
  GitHub issues, pull requests, and CI artifacts are also prohibited destinations.
- Keep these files in the ignored private local folders (`results/`, `artifacts/`,
  `reports/`, `onboarding-networks/`, `.secrets/`, `.state/`) and on the authorized test
  servers. Never use `git add -f` for results. Permission to push source code does not
  grant permission to publish measurement data.
- Never copy real exit IPs into test fixtures; use reserved documentation addresses
  when substituting for private measurements.

## Local layout

The local `proxy-index/` checkout is the only working checkout of this repository;
commit and push every change, framework or API, from it. The folder around it is the
old API clone: commit nothing there. Never move `proxy-index/` (or its local-only
`.secrets/`, `.state/`, `db-ip/` and Postgres data) anywhere else unless the owner
asks for it.

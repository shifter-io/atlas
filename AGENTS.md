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

- Framework: `.github/workflows/ci.yml` (`tier1`) runs on every ready pull request:
  privacy, commit-message, commit-identity and secret checks for every change, then
  for framework code `uv sync --locked`, ruff, the source limits, pytest with a
  PostgreSQL (database tests must not skip) and changed-line coverage of at least 85%.
- API: `.github/workflows/api.yml` runs when `api/`, the framework's SQL
  (`src/proxy_index/**/*.sql`) or that file changes: `gofmt`, `go vet` and `go test`
  with the database tests against a throwaway PostgreSQL 17 and the framework schema
  read from `src/proxy_index/` of the same checkout. A framework schema change that
  breaks the API's database tests fixes them in the same pull request. It is one job on
  a 32-CPU server: `gofmt` and `go vet` in the background while every package's tests
  run in parallel, then the latency test `TestServeFromDatabase` (p99 < 2 ms) alone in
  its own process, after everything else. See "Tests" in `api/README.md`.
- Always build `siteapi` from the current checkout (`cd api && go build
  ./cmd/siteapi`); `api/bin/` is ignored and never committed. An old binary serves
  only the routes it was built with and writes its own migration checksums.
- API migrations are checksummed: never edit an applied migration, even a comment;
  add a new one.

## Live tests without touching production

A live test runs on temporary servers made for it and removed after it; production
keeps running untouched. Details: "Deploy and operating guide" in
`docs/campaign.html` (the staging manager is its `#staging` paragraph, teardown its
section 6).

- Test servers: Maestro's `maestro-run "<cmd>"` creates a disposable server, syncs the
  checkout, runs the command and destroys the server. For more than one command, use
  `do_test_server.py` (Maestro's tools) directly: `create`, `run`, `destroy`, `extend`;
  `list --project proxy-index` shows what is still up.
- A staging manager: `proxy-index campaign manager deploy --environment staging
  --inventory .secrets/campaign-manager-staging.json --mac-key
  ~/.ssh/proxy-index-campaign-staging …`. Staging has its own inventory, its own Mac
  key, and only `pi-camp-stg-` resources: the tags `pi-camp-stg-manager` and
  `pi-camp-stg-worker`, the firewalls `pi-camp-stg-manager-fw` and
  `pi-camp-stg-worker-fw`, and its dated keys. The worker image is production's and
  only read: plan with production's `--images` inventory. Pass the staging
  `--inventory` to every later `campaign` and `campaign manager` step.
- A throwaway PostgreSQL on the test server, never the local one:
  `python scripts/postgres_local.py init --bin-dir <pg bin> --port <free port>
  --ephemeral` (as `ci.yml` does), then point `PG_TEST_ADMIN_FILE` at its
  `.secrets/postgres-admin.toml`. It goes with the server.
- Teardown: `proxy-index campaign manager teardown --inventory
  .secrets/campaign-manager-staging.json [--after-collect <report> …]`. Add
  `--keep-worker-assets` only to replace the staging manager between runs: it removes
  the manager and its firewall and keeps `pi-camp-stg-worker-fw` and the staging keys,
  which only the next full teardown removes. End with a full teardown, then check that
  nothing is left: its proof counts all 0 for every `pi-camp-stg-` tag and the
  campaigns' tags, `unassigned_managers` is empty, and `do_test_server.py list
  --project proxy-index` shows none of your servers. A failed removal is rerun, never
  done by hand.
- Never touch production: its inventory (`.secrets/campaign-manager.json`) and its
  manager (`pi-camp-manager-…`), the worker images and `.secrets/campaign-images.json`
  beyond reading them, its database (the local cluster under `.state/postgres` and
  `.secrets/database.toml`), and its firewalls (`pi-camp-manager-fw`,
  `pi-camp-worker-fw`). Every `campaign manager` step of a test names the staging
  `--inventory`, and its `deploy` also `--environment staging`.

## How a change reaches main

Nobody pushes to `main`. Every change is a pull request that GitHub's merge queue
merges (squashed), so `main` is only ever what the queue tested; pushes to `main` run
no CI. What the checks run and how long they may take: the CI paragraph in
`README.md`.

1. Open a ready (not draft) pull request. On its head these must pass: `tier1`
   (`ci.yml`), `maestro-policy` (size, privacy, attribution; `maestro-policy.yml`),
   `api` (`api.yml`) when it touches `api/` or the framework's SQL, and the
   `maestro/review` and `maestro/qa` statuses the reviewer and QA set on that same
   commit. A new push, a merge of `main` into the branch included, needs review and
   QA again for the new head.
2. Join the queue: `gh pr merge <pr> --auto`. The queue's merge method is squash, so
   pass no method flag. It enters the queue once every check is green.
3. The queue tests the change on top of `main` and the entries ahead of it: `tier1`
   and `api` run again for the merge commit (`merge_group` event), and the queue gate
   (`maestro-queue.yml`) reports `maestro/review`, `maestro/qa` and `maestro-policy`
   for it after checking that they passed on the pull request's head. Nothing is
   re-reviewed. If the combined test fails, the entry leaves the queue and `main` is
   unchanged.
4. Wait without polling GitHub:
   `maestro-wait pr --repo Zeus-Development/proxy-index --pr <pr> --head <sha>`
   reports `merged`, `checks-failed`, `conflict` or `left-queue`.

`tier1` and `api` each run as one job on their own fresh 32-CPU CI server
(`ci-32cpu`), all their work in parallel there. Both write their slowest tests and
their test step's time to the job summary and the `ci-timing` / `api-timing` artifact;
they warn when a test takes over 60 s or the step over 90 s, and fail when a test takes
90 s or more or the step over 240 s. Every test runs on every event, pull requests
included. Trends across runs:
`python3 scripts/ci/timing_history.py [--workflow api] --event merge_group`.

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

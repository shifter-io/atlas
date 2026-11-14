# Renaming Proxy Index to Atlas (RENAME-ATLAS-1, #1008)

The product is called **Atlas** (owner decision, 2026-11-13). This repository says Atlas
everywhere except where the old name belongs to something outside the repository that is
running today: renaming those would break it. This file lists what was renamed, what was kept
and why, and how an open branch catches up.

The rename is done by `scripts/rename_to_atlas.sh`. What keeps the old name is listed in
`scripts/rename_to_atlas.keep`, one group per reason below; everything else is renamed. The test
`tests/test_atlas_rename.py` fails when the old name appears anywhere outside that list, in a
file's contents or its path, so it cannot come back unnoticed.

## Bringing a branch up to date

On a branch that was started before the rename landed:

```sh
git fetch origin
git checkout origin/main -- scripts/rename_to_atlas.sh scripts/rename_to_atlas.py scripts/rename_to_atlas.keep
scripts/rename_to_atlas.sh            # renames the branch's own files the same way (a few seconds)
git add -A && git commit -m "<task>: rename to Atlas with scripts/rename_to_atlas.sh"
git merge origin/main                 # the renamed lines now match main's
scripts/rename_to_atlas.sh --check    # nothing left; run the script again if it lists anything
```

Then let `ruff check --fix` and `ruff format` run (on a test server or CI), as for any change:
shorter names can let a wrapped line fit again, and `atlas` sorts before other first-party
imports. Goldens and hashes that the branch itself changed are regenerated as usual. The
script is idempotent: on an up-to-date branch it changes nothing. It moves paths with `git mv`
(a plain move for files git does not track yet) and leaves content changes unstaged.

## What is renamed

Every spelling becomes Atlas by its case: `proxy_index` → `atlas`, `proxy-index` → `atlas`,
`Proxy Index` → `Atlas`, `PROXY_INDEX` → `ATLAS`, `ProxyIndex` → `Atlas`; "a Proxy Index" →
"an Atlas".

| What | Old | New |
| --- | --- | --- |
| Python package | `src/proxy_index/` | `src/atlas/` |
| Project (pyproject, uv.lock, wheel) | `proxy-index`, `proxy_index-<v>-py3-none-any.whl` | `atlas`, `atlas-<v>-py3-none-any.whl` |
| CLI and console script | `proxy-index …`, `.venv/bin/proxy-index` | `atlas …`, `.venv/bin/atlas` |
| Go module | `github.com/Zeus-Development/proxy-index/api` | `github.com/Zeus-Development/atlas/api` |
| Environment variables | `PROXY_INDEX_CALCULATE_MEMORY_MAX`, `PROXY_INDEX_DATABASE_FILE`, `PROXY_INDEX_ALLOW_PRODUCTION_TARGET`, `PROXY_INDEX_BROWSER_DIR`, `PROXY_INDEX_TOOLS`, `PROXY_INDEX_SCALE_DEMO` | `ATLAS_…` (same suffix) |
| CI cache folder on the CI servers | `$CI_CACHE_DIR/proxy-index/` | `$CI_CACHE_DIR/atlas/` (filled again on the first run) |
| Linux browser-host unit example | `configs/systemd/proxy-index-browser-node.service.example` | `configs/systemd/atlas-browser-node.service.example` |
| Unit descriptions, notification titles, the admin login realm, the DB-IP download User-Agent, temporary folder prefixes, certificate names of newly made certificates | "Proxy Index …" | "Atlas …" |
| Docs, README, AGENTS.md, `api/*.md`, CI workflows, configs, tests, goldens, comments | | Atlas |

No variable is read under its old name as well, because no running server needs it: the only
one a deployed server sets is `PROXY_INDEX_CALCULATE_MEMORY_MAX`, in the orchestrator unit on the
London and staging managers, and that unit runs the old package (the old console script stays
installed beside the new one) until `campaign manager orchestrator-enable` rewrites it, with the
new console script and the new variable together. The other variables are set by people or by
scripts in this repository, which now use the new names; a shell or script that still exports an
old name must change it (`ATLAS_ALLOW_PRODUCTION_TARGET=1` for the production guard, which
otherwise refuses, as it should).

### Public API text that changed

The website thread is told these, field by field:

| Answer | Field | Old value | New value |
| --- | --- | --- | --- |
| `GET /v1/codes` | code set `source`, code `measured`: its text | "Measured by the Proxy Index tests." | "Measured by the Atlas tests." |
| `GET /v1/files/dataset-<sha256>.json` (the dataset download) | `Content-Disposition` file name | `shifter-proxy-index-dataset.json` | `shifter-atlas-dataset.json` |

No other answer, and nothing in the dataset file's content, carried the old name. The website's
page paths (`/proxy-index/…`) appear only in the API's tests and its page benchmark, which time
the website's pages; they stay until the website moves them (see "website" below).

## What keeps the old name, and why

| Group in the keep-list | What | Why it stays |
| --- | --- | --- |
| (none needed) | Database names and roles: `proxy_reviews`, `proxy_reviews_staging`, `proxy_api`, `proxy_admin` | They exist on the database servers; the new production database uses the same names. They do not contain the old product name, so nothing in the script touches them. |
| `digitalocean` | The DigitalOcean team account key `proxy-index` (`--account proxy-index`, inventories' `"account"`), its project "Proxy Index", the keychain entries "Proxy Index DigitalOcean", "team: Proxy Index" and "Proxy Index Spaces archive" | They exist at DigitalOcean and in the owner's keychain; the code finds them by these exact names, and the safety checks refuse anything outside the project named "Proxy Index". |
| (none needed) | DigitalOcean resource names, tags, images and firewalls (`pi-camp-*`, `pi-account`, `pic-*`, `pi-pulse-*`) | They use the `pi` prefix, not the product name. |
| `buckets` | The Spaces bucket and key `proxy-index-archive` (and the owner's local copy of the archive) | The bucket holds the archive; bucket names cannot be renamed. |
| `owner-machines` | SSH key files `~/.ssh/proxy-index-campaign[-staging]`, the Mac's launchd agent `com.proxy-index.campaign-watch` and its log folder, the Mac minis' agent `com.proxyindex.browser-node`, the local checkout folder `proxy-index/` and its `.state` | They exist on the owner's Mac and the minis. The `.secrets` folder's file names do not contain the old name. |
| `old-servers` | Paths and unit names on the servers that run today: `/opt/proxy-index-campaign` (the managers), `/opt/proxy-index-worker` and `/run/proxy-index-worker` (inside the worker image), `/etc/proxy-index/database.toml`, `/var/lib/proxy-index-api` (the API server's default files folder), the units `proxy-index-campaign-*`, `proxy-index-orchestrator`, `proxy-index-daily`, the on-demand pulses' `proxy-index-pulse` (PULSE-ONDEMAND-1: installed beside the daily timer on the same managers, so the droplet scripts and the host layout's translations keep one pattern), `proxy-index-db-analyze`, the onboarding servers' `proxy-index-onboarding-*` and the scope `proxy-index-calculate` | Code updates on the London and staging managers rewrite these units in place; new names would leave the old units running beside the new ones (and a check for a running onboarding unit would look for a name that does not exist). The worker image holds its tools under the old path. The new Canada server's setup uses the new names (`atlas-manager`, `atlas-orchestrator`, `atlas-api@blue/green`, `~/atlas`). When the old servers are deleted, remove this group and run the script again. |
| `website` | The Shifter website's page paths (`/proxy-index`, `/proxy-index/networks/…`, `https://shifter.io/proxy-index…`), its source files (`src/pages/proxy-index/…`, `src/components/proxy-index/…`, `src/data/proxy-index/…`, `scripts/proxy-index/…`, `og/proxy-index/…`), component names (`ProxyIndexContent`, `ProxyIndexHome`, `proxyIndexFaq`, `proxy-index-home.tsx`) and its branch `proxy-index` | They live in the website's repository; renaming them here would point at pages and files that do not exist. When the website moves them, remove this group and run the script again. |
| `external-repos` | The old, frozen `proxy-index-api` repository and the website's `proxy-index-website` repositories | Other repositories' names. |
| `format-ids` | `proxy-index-hour-formula`, `proxy-index-best-hours` (formula files), `proxy-index-raw-day` (the Spaces archive), `proxy-index-browser-raw` (browser raw manifests), `.proxy-index-release.json` (the browser hosts' release marker) | These identifiers are written inside files that already exist outside the repository (in the archive bucket, on the managers and on the browser hosts). The code checks them when it reads those files, so new names would make every existing file unreadable. |
| `checksummed` | `src/atlas/schema.sql`, the framework's `migrate_v*.sql`, `tests/fixtures/schema_v*.sql`, `api/internal/store/migrations/*.sql` | The databases record each schema file's SHA-256 (comments included) and applied migrations are never edited; a changed comment would make every existing database refuse the new code. The files move with the package; their bytes stay. |
| `history` | `docs/reports/*` | Dated reports record what ran at the time, under the names of the time. Git history is not rewritten either. |
| `rename-tooling` | `scripts/rename_to_atlas.*`, `tests/test_atlas_rename.py`, the rename cases in `tests/test_ci_gates.py`, this file | They name what they rename. |

Domain names never contained the old name, so none needed keeping.

## Outside this repository (for the orchestrator)

These are not changed by the pull request; they follow when the GitHub repository is renamed:

- **The repository itself:** `Zeus-Development/proxy-index` → `Zeus-Development/atlas`. GitHub
  redirects the old address, so existing clones keep working; links in the docs already use the
  new one.
- **Maestro:** `maestro-run` names its server image and project after the origin URL's last part,
  so after the rename it looks for an image named `atlas` (today's is `proxy-index`, in
  `tools/images/proxy-index/`), and `do_test_server.py list --project atlas` shows the servers. The
  CI server pool's repository list (`tools/ciod/config.json`, `"repos"`) names `proxy-index`: add
  `atlas` there, or tier1 and api jobs of the renamed repository may find no CI server. The shared
  watcher's repository list follows the rename too.
- **Local checkouts:** the folder name of a clone does not change by itself; `git remote set-url
  origin` to the new address is optional (the redirect works).
- **Browser minis and the Mac's campaign watcher:** they run from a checkout's `.venv`. After that
  checkout is updated (`uv sync`), the console script is `.venv/bin/atlas`, so the installed
  launchd agent must be rendered again from `configs/launchd/com.proxyindex.browser-node.plist.example`
  (the label stays) or `atlas campaign guard install`.
- **Managers:** the next code update of a manager installs `bin/atlas` beside the old
  `bin/proxy-index`; `campaign manager deploy` and `orchestrator-enable` rewrite every unit to call
  `bin/atlas`.

## Repository description and README (prepared for after the merge)

The README's first paragraph is part of this pull request. The GitHub settings are changed by the
orchestrator after the repository rename:

```sh
gh repo edit Zeus-Development/atlas \
  --description "Atlas: census and pulse measurement of proxy networks, IP-database enrichment, scoring, and the snapshot API behind the Atlas website."
```

The same sentence is the "About" text. The website field stays empty until `https://atlas.shifter.io`
is live; then: `gh repo edit Zeus-Development/atlas --homepage https://atlas.shifter.io`.

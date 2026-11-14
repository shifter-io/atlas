# Deploying the siteapi without a gap (blue/green)

Every deploy of a new siteapi binary to the production API server follows this procedure.
Never restart the serving siteapi in place: a fresh process answers 503 (`starting`) on every
data route and on `/healthz` until its first build is done, about 100 to 140 s at today's volume.

Data changes need nothing here: a new calculation is built beside the serving version and
swapped in at once (`api/internal/server/server.go`, `current`). This page is only for a new binary.

**A refused build does not loop.** When a new calculation cannot be served the same way every
time (an answer above the size cap, a content check that fails, a build older than payload v4),
the serving siteapi builds it once, keeps the previous version serving and does not build it again
until the calculation or the catalog changes, or the siteapi restarts (`api/README.md`, Caching:
change detection). It looks like this in the admin `/healthz` (below): `"status":"degraded"`, `version` still the older build,
`failedFingerprint` the refused build id, `failedError` the reason (for example `answer /v1/home/7d
is 205565 bytes, above the 204800-byte cap`) and `"retryAt":null`; the log has one `build refused;
not retried until the fingerprint changes` line, not one per poll. The fix is a new binary (deployed
as below) or a new calculation. Any other failure (the database unreachable, a timeout) shows a
`retryAt`: it is retried with a doubling wait, at most 30 min apart.

The server's own details (how to log in, the paths, the public URL) are in the owner's private
runbook, which has the same steps. Nothing of them goes on GitHub.

**On the Canada server** `scripts/server-setup.sh <env>` performs these steps itself, as user
services named `atlas-api@<colour>` (staging: `atlas-staging-api@<colour>`, ports 9090/9091) behind
the shared `atlas-caddy.service`: it builds the commit, starts the idle colour, waits for the checks
of step 3 (the full `/healthz` on the colour's loopback admin listener, 8190/8191, staging 9190/9191),
switches that environment's Caddy site and reloads Caddy, then stops
and disables the old colour after 60 s ([canada-server.md](canada-server.md)).

## Layout

- **Two instances of one systemd unit template**, `siteapi@blue` and `siteapi@green`:
  - the same user, hardening and read-only database role (`proxy_api` on `proxy_reviews`);
  - `MemoryMax=7G` and `GOMEMLIMIT=6451MiB` (about 90 %, `api/README.md` "Caching: Memory");
  - the same flags (`--row-cache 512`, the database file);
  - per colour, from that colour's environment file: its own loopback API port (the siteapi
    reads `--listen` / `SITEAPI_LISTEN`; the production unit passes `--listen
    127.0.0.1:${SITEAPI_PORT}`, a value systemd fills in from the environment file: blue 8090,
    green 8091) and its own loopback admin port (`SITEAPI_ADMIN_LISTEN`, which the siteapi reads
    itself: blue 127.0.0.1:8190, green 127.0.0.1:8191; staging 9190, 9191). Without its own
    `--listen` a colour takes the default 127.0.0.1:8090 and collides with blue.
    The admin port refuses any address that is not loopback and serves the full `/healthz`
    report and nothing else (SEC-HEALTH-1, #1011). The public `/healthz` says only `status`,
    `version`, `builtAt`, `revision` and `reason`. An older binary ignores the variable, so a
    rollback to it needs no change to the file;
  - per colour: its own binary link (pointing at a commit's directory) and its own
    `--files-dir`, so neither writes the other's dataset file.
- **The database file** both colours read (SEC-DBTLS-1, #1033): a regular file owned by the
  API's own user with mode 600, and `sslmode = "require"` for the database host, which is not
  local. The binary refuses any other owner or mode, and any weaker mode for a remote host. The
  binaries kept for rollback run as the same user, so they read it too.
- **Exactly one colour serves:** the reverse proxy (Caddy) forwards to that colour's port.
  The other colour is stopped and disabled; the serving one is enabled at boot.
- Each binary lives in its own directory named by its full commit. Keep the last two or three
  for rollback; never delete one a colour's link points at.

## When to deploy

- **Each API item is deployed as soon as it merges** (the owner's rule), unless the
  orchestrator has put deploys on hold, for example while the website confirms a new shape.
  Several merges in a row go out together in one deploy of the newest `main`.
- **Never during a calculation round's publishing window.** Before starting, read the round
  runner's status: a `PUBLISHING DAY n` line without a later served or `NOT PUBLISHING` line
  means wait. A deploy may run while the round calculates; write the deploy's `START` and
  `DONE` lines (with the commit) where the runner reads them, so it holds its next publish
  until `DONE`.
- **New fields that need the next calculation** may be served null until that build is
  published, each with its reason in `na`. Check that every such null has one (the null-reason
  check, below); if one has none, do not switch and report the field.

## Before a deploy

1. **Build** `siteapi` from the merged `main` commit, never on the owner's machine. The old build
   server is gone: build on a disposable test server with the Maestro test-server tool's `exec`
   mode and its `--fetch` option, which brings the built binary back as a file and destroys the
   server afterwards. Send it a folder with a git bundle of the commit (so the binary records it)
   and a small build script that:
   - downloads the Go version pinned in `.github/workflows/api.yml` (`GO_VERSION`, today 1.27.4,
     as `api/go.mod` requires since SEC-GO-1, #1010) and checks it against `GO_SHA256`;
   - clones the bundle and runs `GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 GOOS=linux
     GOARCH=amd64 go build -trimpath ./cmd/siteapi` in `api/`;
   - prints `go version -m` of the binary, which must show `vcs.revision` = the commit and
     `vcs.modified=false`, and its SHA-256. Record both.
2. **Copy** it into a new directory beside the old binaries and check the SHA-256 on the server.
3. **The database file:** check it is still owned by the API's user with mode 600 and asks for
   `sslmode=require` (Layout). A new binary refuses a file with another owner or mode, and for a
   remote host an `sslmode` of `disable`, `allow` or `prefer`; a file that names no mode gets
   `require` added.
4. **The database, when the commit needs it** (a new file in `api/internal/store/migrations/`,
   or a merged PR whose deploy note names catalog scripts), on `proxy_reviews` with the
   production guard (#817), in this order:
   1. **The guard's identity check:** the connection's database is `proxy_reviews`, its user the
      schema owner (`proxy_admin`), the `search_path` does not name the scale scripts' scratch
      schema (`proxy_scale`) and that schema does not exist (`scripts/scale/target.py`, the
      owner's `PRODUCTION-AS-STAGING` go). Stop on any other answer.
   2. **`siteapi migrate`** with the new binary, recording `site.schema_versions` before and
      after. Migrations only add to `site`; an older binary still serves on the migrated database.
   3. **The catalog loads** the PRs name (for example `api/scripts/catalog-network-facts.sql`, then
      the `api/scripts/pricing-<slug>.sql` files), each with `ON_ERROR_STOP` as the catalog writer.
      The admin database file is streamed to a memory-only (tmpfs) file on the API server for
      these steps and shredded at the end of the same session, then checked gone: it is never
      written to disk and never stays on a server.
   4. **The grants check**, read-only as the API role: every `site` table readable, no table
      writable by it, its connections on TLS.

   Role changes such as the API role's limits (SEC-DBROLE-1, #1013) are not part of a deploy:
   they need the owner's go and an operator.
5. **Memory:** both colours run during a switch. Check that
   the idle colour's first-build peak (at most 3 GB, SITEAPI-MEM-1)
   + the serving colour's current memory (`systemctl show <unit> -p MemoryCurrent`)
   + 2 GB for the system fits in the server's total memory (`free -b`).
   If not, **stop and report; do not deploy over the limit.**
6. Start an **outside probe** on the Canada server, through the public URL: `/v1/version` and
   `/v1/rankings?range=24h` every second, logging each status code, until after step 6 of The
   deploy (the old colour stopped). 0 non-200 is required.

## The deploy

1. **Point the idle colour's link** at the new commit's directory.
2. **Start the idle colour** (`systemctl start siteapi@<idle>`). The serving colour keeps serving.
3. **Wait on the idle colour's own ports** (loopback, not through the proxy) until all of these hold:
   - its admin port's `/healthz` has `"status":"ok"`, `"unserved":[]` and `"lastError":null`;
   - that `/healthz` `binary.revision` is the new commit and its `fingerprint` the build you expect;
   - each of these answers 200 on its API port: `/v1/version`, `/v1/rankings?range=24h`, `/v1/networks/shifter`,
     `/v1/overlap?range=30d`, `/v1/search?q=a`, and `/v1/dataset` (a 302 whose target answers 200);
   - `/v1/home` in each range is under the 204,800 B cap, and the null-reason check passes.

   If it does not get there, stop the idle colour and investigate: nothing has changed for clients.
4. **Switch the proxy:** change the `reverse_proxy` upstream to the idle colour's port, then
   `caddy validate`, then `systemctl reload caddy`. The reload is graceful: Caddy keeps its
   listening socket, and requests in flight finish. Keep a copy of the previous config.
   **With a CDN cache time configured, purge it now** (see The CDN), before the old colour stops
   in step 6: the new colour's first build does not purge (only a later rebuild does), so the CDN
   would keep the old colour's answers for up to its cache time. Run `siteapi cdn-purge` with the
   colours' own `--cdn-purge-url`, `--cdn-key-file` and `--cdn-tag` (from the colours' environment
   file, never typed on a command line); exit 0 is a confirmed purge. On exit 1 run it once more; if
   it fails again, carry on and report it (the data answers then refresh within the CDN's TTL).
5. **Check through the public URL:** `/healthz` `revision` is the new commit, and the probe shows only 200.
6. **Drain, then stop the old colour:** wait 60 s after the reload, then
   `systemctl stop siteapi@<old>` and `systemctl disable siteapi@<old>`,
   `systemctl enable siteapi@<new>`. Read both colours' `MemoryPeak` before stopping.
   The stop is clean even while the old colour is rebuilding (for example after a
   catalog change): it cancels the rebuild, discards it and exits within a few seconds
   (`api/README.md`, Shutdown), so the unit's 20 s `TimeoutStopSec` stays. A stop that
   still ends in SIGKILL is a bug: `systemctl reset-failed siteapi@<old>`, check that
   its `--files-dir` holds no `.tmp` file it would serve (none is served; the next
   start removes them), carry on and report it.
7. **Stop the probe** a little after step 6 and record: the requests, the non-200 count (must
   be 0), the time of the switch and both peaks of memory.

Expected after a switch: `/v1/version`'s `revision` changes even when the data did not, because
each build writes a new dataset file. Open pages refetch once. An old dataset file name answers
a 302 to the current file, never a 404.

## The checks after every switch

On the public URL, over every answer the site uses, every network's pages and the dataset file
(and every page the dataset file lists):

- **The scans:** no success rate without its score (#898); none of the plain-HTTP cost fields
  (#950); none of the removed prose and label fields (#962, #977); none of the fields
  `api/CHANGES.md` lists as removed; 0 hits of the IP data vendor's name (#884, any case).
- **`/v1/home`** under the 204,800 B cap in each range, with the three sizes logged.
- **The null-reason check:** every null the new fields bring carries a reason (`na` with
  `naReason` or `naReasons`, or its status).
- **The contract:** `/v1/version` and `/v1/codes` name the contract date `api/CHANGES.md` ends with.
- **The items of the deploy:** the fields each merged PR describes, read on the public answers.

If a check fails after the switch, report it before deciding to roll back.

## The CDN

`SITEAPI_CDN_TTL` (`--cdn-ttl`, CDN-HEADERS-1, #1005) sets how long the CDN keeps a data answer
(browsers then keep it 60 s), and `SITEAPI_CDN_TAG` (`--cdn-tag`) the tag those answers carry so
a purge can name them (default `atlas-data`; it has an effect only with a cache time set). The
cache time stays off (`0`, the default) until the plan's owner turns it on; while it is off, data
answers keep today's cache lifetimes and step 4's purge is not needed.

The CDN fetches from the Canada server over plain HTTP (ORIGIN-HTTP-1, the owner's decision of
2026-11-14): Caddy's plain HTTP listener, 8080 for production and 9080 for staging, next to the
HTTPS one (8445/9445), with the same `X-Atlas-Origin-Key` check and no redirect to HTTPS
([canada-server.md](canada-server.md), step 7, which also has the security note on the key sent in
the clear). The CDN keeps each answer for 1 hour by its own cache setting; no CDN API key and no
purge are used, so `SITEAPI_CDN_TTL` stays `0` and step 4's purge is not needed.

## Rollback

- **Before step 6** (the old colour still runs): set the proxy upstream back to the old colour's
  port and `systemctl reload caddy`. Then stop the new colour.
- **After step 6:** start the old colour (its link, binary, unit and environment file are
  unchanged), wait for its checks as in step 3 (on its admin port, or on its API port for a
  binary from before #1011), switch the proxy back and reload, then stop the new colour after 60 s.
  If a deploy also changed a colour's environment file or the database file, the backup copy made
  before the change restores it; neither change stops an older binary.
- Migrations and catalog loads are never rolled back: they only add to `site`, and an older binary
  checks only the migrations it embeds.

## Never

- Restart the serving colour, or run a deploy that stops it before the other colour serves.
- Start both colours on the same port or with the same `--files-dir`.
- Deploy a commit that is not on `main`, or a binary built from a modified tree.
- Deploy while a calculation round is publishing.
- Write an admin database file to disk, or leave one on a server.

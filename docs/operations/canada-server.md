# Atlas on the Canada server

Atlas runs on one server: production and staging side by side, as one normal user with
lingering user services (`systemctl --user`), without root. The server's address and login
are in the owner's private runbook, never on GitHub.

## Installing or updating an environment

```
bash scripts/server-setup.sh <production|staging> [--ref <commit>] [--dry-run]
```

Run it on the server as the normal user (it refuses root and never uses `sudo`). It takes the
code from a git repository on the server, `~/atlas/repo.git` (or `ATLAS_REPO_URL`): the
repository is private and the server has no GitHub login, so the caller pushes `main` there first:

```
ssh <server> git init -q --bare atlas/repo.git
git push <server>:atlas/repo.git <commit>:refs/heads/main
ssh <server> bash atlas/<env>/src/scripts/server-setup.sh <env> --ref <commit>
```

(On the very first install, run the script from any checkout of the same commit, for example
piped over ssh with `bash -s -- <env> --ref <commit> < scripts/server-setup.sh`; it then reads
its templates from the code it checked out.)

What it does, each step only when something is missing or different:

1. **Tools** in `~/atlas/bin/`, each pinned and its SHA-256 checked: `uv` and a uv-managed
   Python 3.12; Go, the version `api/go.mod` names (the script stops if its pin differs); Caddy 2;
   `psql` 17 or newer, unpacked from the release's own package with `apt-get download` and
   `dpkg -x` (checked against the release's signed index), never installed.
2. **Code:** the commit (default: the latest `main` of the repository) in `~/atlas/<env>/src`,
   which must be on `main` and unmodified; the venv `~/atlas/<env>/venv` from `uv sync --locked
   --reinstall-package atlas` (plain `uv sync` keeps the old build when only code changed). The
   script stops before the build when the installed `atlas` package differs from `src/atlas`.
3. **Build:** `siteapi` into `~/atlas/<env>/api/<commit>/siteapi` (`-trimpath`, no cgo);
   `go version -m` must name the commit with `vcs.modified=false`.
   **Worker wheel** (IMAGE-AUTO-1): the commit's wheel (`uv build --wheel`, reproducible from
   the commit time) into `~/atlas/<env>/wheels/<sha12>/`, named in `~/atlas/<env>/wheels/current.json`
   (`commit`, `wheel`, `sha256`, `requirements_sha256`: the SHA-256 of `uv export --frozen --no-dev
   --no-emit-project` without its comment lines, as `campaign image build` records it). Every daily hour and on-demand pulse hour ships that wheel to its droplets
   at launch and records it (`code` in `daily.json` / `pulse.json`), so a code change reaches the
   droplets with the next hour without a new image. A commit whose wheel is there already is
   not built again.
4. **Private files** in `~/.config/atlas/<env>/` (folder 700, files 600, or the script stops
   with the file's name): `database-api.toml`, the API's read-only database login; `origin-key`,
   the CDN's secret header value, made from 32 random bytes when missing and never printed.
   Optional: `api.env` with `ATLAS_API_FLAGS="..."`, extra `siteapi serve` flags (the CDN settings).
   **Database TLS** (SEC-DBTLS-2): when the operator has copied the database servers' root
   certificate to `~/.config/atlas/db-root.crt`, the script installs it as
   `~/.config/atlas/<env>/db-root.crt` and gives each `database*.toml` there whose host is not
   local `sslmode = "verify-full"` and `sslrootcert` naming it (other lines are kept; a file is
   rewritten only when it changes, mode 600). It stops when that file holds a private key or is no
   certificate. Without it the database files keep their mode (`require`, SEC-DBTLS-1). See
   [Database TLS](#database-tls-verify-full).
5. **Units** in `~/.config/systemd/user/`: the API template `atlas[-staging]-api@.service`
   (`MemoryMax=7G`, `GOMEMLIMIT=6451MiB`, `--row-cache 512`, a `--files-dir` per colour) and
   the shared `atlas-caddy.service`. Every unit has `Restart=on-failure`, `RestartSec=10`,
   `StartLimitIntervalSec=600`, `StartLimitBurst=5`.
6. **Blue/green**, as in [api-deploy.md](api-deploy.md): the first run starts blue. A new
   commit starts the idle colour, waits until its full `/healthz` (on its loopback admin listener,
   `SITEAPI_ADMIN_LISTEN` in `~/atlas/<env>/api/<colour>.env`; the API port for an older siteapi)
   is ok and names the commit and the listed answers are 200 (at most 15 minutes, `ATLAS_HEALTH_TIMEOUT`),
   points this environment's Caddy site at it and reloads Caddy, then stops and disables the
   old colour 60 s later. A failed wait stops the idle colour and exits 1; nothing is switched.
   `~/atlas/<env>/api/serving` names the serving colour.
7. **Caddy:** `~/atlas/caddy/Caddyfile` imports one file per environment from `sites/`, so
   writing one environment never changes the other's. Each site listens on its port with
   `tls internal` (one certificate, `atlas-origin`, for any name or address) and answers an
   empty 403 unless the request has the header `X-Atlas-Origin-Key` with the environment's
   origin key. Hardening (SEC-ORIGIN-1): server timeouts (read header 5 s, read body 10 s,
   write 60 s, idle 2 min), at most 64 kB of headers and a 1 MB request body; forwarded answers
   get `Strict-Transport-Security: max-age=31536000`; the access log
   `~/atlas/caddy/logs/<env>-access.log` (mode 600, 10 files of 50 MB) and Caddy's own log (the
   service's journal, which also records failed requests) delete the origin key header before
   writing; Caddy's admin endpoint is a unix socket in `~/atlas/caddy/`, never
   a network port. Caddy itself is pinned by version and SHA-256 (step 1).
   **Plain HTTP for the CDN** (ORIGIN-HTTP-1): the same site also listens on plain HTTP, 8080
   for production and 9080 for staging. The CDN fetches from there: no TLS between the CDN and
   the server (for speed; the visitor's TLS ends at the CDN), and nothing to set in the CDN but
   the origin address, no secret header and no edge rule. This listener forwards every request
   to the serving colour, with no header check, no 403 and no redirect to HTTPS; its answers
   carry no `Strict-Transport-Security` (meaningless on plain HTTP). It shares the log and the
   request limits with the HTTPS site, which keeps its key check but is no longer what the CDN
   uses. One file holds both listeners, so a colour switch moves both, and `caddy validate`
   gates every change as before.
   **Security note:** the origin is reachable directly by anyone who knows its address: a visitor
   can skip the CDN and its cache. That is accepted. Every answer is public data, the API takes no
   writes (a POST is refused), and the flood guard on uncached work (SEC-COST-1, #1018:
   `--uncached-limit` concurrent computations, a 503 `busy` past it) keeps cached answers fast and
   the database safe under a flood. A visitor's own share of that work is bounded too (SEC-RATE-1, #1019: "The
   visitor's address" below).
8. **Alerts, production only:** if `scripts/server-alerts.sh` exists, `server-setup.sh production`
   ends with `server-alerts.sh install production`. `server-setup.sh staging` installs no alert
   units and removes none (the owner's decision of 2026-11-14): staging is not watched unless an
   operator installs its alerts by hand.

A second run with the same commit changes no file. `--dry-run` prints every action and
changes nothing. The managers' own units are installed by `campaign manager deploy` (host mode).

## The visitor's address (SEC-RATE-1, #1019)

siteapi gives each visitor a small budget of answers computed on request (`--per-client-rate`,
default 5 a second, burst 20; past it a 429 `rate_limited`; `api/README.md`, Caching: the
per-visitor budget). It learns the visitor from Caddy, in the header `X-Atlas-Client-Ip`, and
reads that header only on requests from the loopback (`--trusted-proxies`).

Caddy names the visitor from the CDN's `X-Forwarded-For`, read right to left
(`trusted_proxies_strict`), and only on requests from the CDN's edge servers. A request from any
other address is named by its own address, whatever headers it sends. The edge servers' addresses
come from the CDN's published lists:

- `~/.config/atlas/cdn-edge-lists` (not secret) holds the lists' URLs, one per line (`#` starts a
  comment); each list is a JSON array of addresses or one address per line.
- `atlas-cdn-edges.timer` (daily, at a random time in the hour after midnight, caught up after a
  stop) runs `~/atlas/bin/atlas-cdn-edges`, which writes `~/atlas/caddy/edges/cdn-edges.caddy`
  (`trusted_proxies static ...`, imported in the Caddyfile's `servers` block) and
  `client-header.caddy` (the `header_up` that sends the visitor, imported in each
  `reverse_proxy`), runs `caddy validate` and reloads Caddy when they changed.
- Until both files exist, Caddy sends `unknown` in place of a visitor (a request's own
  `X-Atlas-Client-Ip` never reaches siteapi), and siteapi keeps no per-visitor budget: the flood
  guard on uncached work still applies. So a deploy may come before the timer in any order.
- A run that cannot fetch or read a list, finds fewer than 10 addresses, a private address or a
  range wider than /16 (IPv4) or /32 (IPv6), or fails `caddy validate` changes nothing and fails
  the unit; the previous addresses stay in force.

`server-setup.sh` writes the unit, the timer and the script (shared by both environments) but
never enables the timer. The operator turns it on once:

```sh
printf '%s\n' '<IPv4 list URL>' '<IPv6 list URL>' > ~/.config/atlas/cdn-edge-lists
~/atlas/bin/atlas-cdn-edges --dry-run | head -c 300   # the lists read; nothing written
systemctl --user start atlas-cdn-edges.service        # writes the files, validates, reloads Caddy
journalctl --user -u atlas-cdn-edges.service -n 5     # "<n> ranges, Caddy reloaded"
systemctl --user enable --now atlas-cdn-edges.timer
# Requests through the CDN now log the visitor, not an edge server (several distinct addresses):
tail -n 200 ~/atlas/caddy/logs/production-access.log | jq -r .request.client_ip | sort | uniq -c | sort -rn | head
```

If that last list shows only a few addresses that are in `edges/cdn-edges.caddy`, the CDN sent no
`X-Forwarded-For`: switch it off (below) and report it.

To switch it off: `--per-client-rate 0` in `ATLAS_API_FLAGS` (`~/.config/atlas/<env>/api.env`)
and a restart of the serving colour turns off the budget; or stop naming visitors with
`systemctl --user disable --now atlas-cdn-edges.timer`, `rm ~/atlas/caddy/edges/*.caddy` and
`systemctl --user reload atlas-caddy`.

## Live security check

`scripts/server-security-check.sh` proves the origin settings on the live server: the Phase 2
security runbook's part A (the server from outside) and part D (through the CDN), the checks that
need no password. Read-only and light (a few requests per check, one slow port scan).

```
bash scripts/server-security-check.sh <production|staging> [--host <server>] [--name <public name>] \
    [--ssh <user>:<port>] [--db <database server>]...
```

Run it as root on a disposable host outside the server's cloud network (the UDP scan needs
root), with `curl`, `nmap`, `openssl`, `ssh` and `psql`. The addresses come from the owner's
private runbook and are given only as arguments. It prints one line per check,
`<id> pass|fail|skip <evidence>`, and exits 1 on any fail, 2 on bad arguments. The evidence
names ports, status codes and header names, never an address or a secret.

| ID | Pass |
|---|---|
| A0 | always `skip`: the cloud panel's firewall against the same provider's machines is not testable from outside |
| A1 | a full TCP scan finds only the origins open: 8080, 8445, 9080 and 9445 |
| A2 | no UDP port open among the top 20 |
| A3 | (`--ssh`) the SSH port does not answer, or it offers only `publickey` |
| A4 | the HTTPS origin port without the key, with a made-up key and with a public host name: a closed connection or an empty 403 for `/healthz`, `/v1/version` and `/v1/networks` |
| A5 | plain HTTP on the HTTPS origin port: no answer, or a 400 without server details |
| A6 | TLS 1.2 and 1.3 only, every cipher graded A |
| A7 | the certificate is valid today and names only `atlas-origin` |
| A8 | the managers' upload port (8444 or 9444) is filtered or closed |
| A9.n | (`--db`, numbered) the database port 5432 is filtered or closed |
| A10.n | only when A9.n connects: a made-up password is refused by the access list, with and without TLS |
| A11 | the plain HTTP origin port (8080 or 9080), without any header: `/v1/version` answers 200 directly, no redirect, no `Strict-Transport-Security` |
| D1 | (`--name`; `skip` while the name does not resolve) the second rankings request is a CDN cache HIT |
| D2 | rankings, version and the dataset file with a random extra parameter: each a HIT or a small 400 |
| D3 | an answer has `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Access-Control-Allow-Origin: *` and no `Access-Control-Allow-Credentials` (with the CDN on the plain HTTP origin, `Strict-Transport-Security` comes only from the CDN's own settings) |
| D4 | the name's certificate is valid; TLS 1.2 and 1.3 only, every cipher graded A |
| D5 | a POST is refused (405 or 403); `/healthz` shows only the status-only fields |

Run it once per environment; part D for staging with staging's public name. The checks on the
server itself (runbook parts B and C) need its login and stay manual.

## Database TLS (verify-full)

Every database client checks that the server is ours (SEC-DBTLS-2): each database server has a
certificate for the name or address the clients connect to, signed by a private CA, and each
database file uses `sslmode = "verify-full"` with that CA's certificate as `sslrootcert`.

```
bash scripts/db-tls-setup.sh <database server name or address> [--ca-dir <folder on the Mac>]
```

prints the exact steps for one database server and runs nothing. The owner, or an operator with
the owner's login, runs them:

1. On the operator's Mac, once: the CA (EC P-256, 10 years) in an owner-only folder (default
   `~/atlas-db-ca`). Its key `ca.key` never leaves that folder and is never on a server, like the
   browser hosts' CA (SEC-MINI-KEYS-1); keep the folder backed up offline.
2. On the database server, in the data directory: its own key and a signing request. The key
   never leaves the server.
3. On the Mac: the certificate for that name (`DNS:`) or address (`IP:`), server authentication
   only, 825 days. Compare the request's SHA-256 on both sides.
4. On the database server: `ssl = on`, `ssl_cert_file`, `ssl_key_file`,
   `ssl_min_protocol_version = 'TLSv1.2'` and `ssl_ciphers` with `ALTER SYSTEM`, then a reload;
   every `pg_hba.conf` line for another machine is `hostssl`.
5. The CA certificate only (never `ca.key`) to `~/.config/atlas/db-root.crt` on this server, then
   `server-setup.sh` for each environment on the commit it already runs. The API and the
   manager read their database file at their next start.
6. Check C2 (below).

Before day 825, repeat steps 2 to 4 with a new key; the clients need nothing new.

**Check C2** (the security runbook's part C, on this server with its login), for each environment:

| Step | Pass |
|---|---|
| `atlas db-server check --database-file ~/.config/atlas/<env>/database-api.toml`, then with `database.toml` | each report's `server` shows `"ssl": true` (the file says `verify-full`, so the certificate and its name were checked) |
| `psql` with `sslmode=verify-full sslrootcert=system` | refused: `certificate verify failed` (a client that trusts other CAs does not accept the server) |
| `psql` with `host=atlas-c2.invalid`, the server's address as `hostaddr` and our `sslrootcert` | refused: `does not match host name` (the name is checked) |

`db-tls-setup.sh` prints these commands with the server's name filled in.

## Ingest and calculation limits

The hourly ingest and the calculation size their processes, connections and memory to this
server (48 CPUs, about 94 GB: 12 ingest processes, 36 connections, about 11 GiB in flight, 12
parallel reads and an 11G calculation scope), each within the database server's and the login's
free connections; both environments' jobs and the APIs share the machine. The settings, their
defaults and how to read each hour's `timing:` lines from the journal: README "Hourly limits and
timings".

## Layout

| | production | staging |
|---|---|---|
| Manager: pulse nodes / browser uploads | 8443 / 8444 | 9443 / 9444 |
| API through Caddy (HTTPS, with the origin key) | 8445 | 9445 |
| API through Caddy (the CDN's origin, plain HTTP, no key) | 8080 | 9080 |
| API blue / green (loopback only) | 8090 / 8091 | 9090 / 9091 |
| API admin `/healthz`, blue / green (loopback only) | 8190 / 8191 | 9190 / 9191 |
| Orchestrator unit | `atlas-orchestrator.service` | `atlas-staging-orchestrator.service` |
| Nightly analyze | `atlas-analyze.timer` | `atlas-staging-analyze.timer` |
| Hourly test (installed, never enabled by a deploy) | `atlas-hourly.timer`, transient `atlas-hourly-YYYYMMDDTHH` | `atlas-staging-hourly.timer`, `atlas-staging-hourly-YYYYMMDDTHH` |
| Manager on/off (orchestrator + analyze) | `atlas-manager.target` | `atlas-staging-manager.target` |
| Calculation scope | `atlas-calculate.scope` | `atlas-staging-calculate.scope` |
| API colours | `atlas-api@blue`, `atlas-api@green` | `atlas-staging-api@blue`, `atlas-staging-api@green` |
| Folders | `~/atlas/production/` | `~/atlas/staging/` |
| Private files (dir 700, files 600) | `~/.config/atlas/production/` | `~/.config/atlas/staging/` |

Shared: tools in `~/atlas/bin/`, one `atlas-caddy.service` with `~/atlas/caddy/Caddyfile`,
the CDN edge refresh `atlas-cdn-edges.timer` (enabled by the operator, "The visitor's address"),
the alerts `atlas-alerts.timer` and `atlas-alert@.service`, and the Slack webhook file
`~/.config/atlas/slack-webhook`.

## Pulse SSH host keys (PULSE-KNOWNHOSTS-1, #1126)

Every droplet the manager connects to (pulse nodes, daily workers) has its own known_hosts file,
`.runtime/known_hosts/<droplet id>` (pruned after 2 days). The temporary workaround from
14 November is no longer needed once this version is deployed: on the manager run
`systemctl disable --now atlas-pulse-knownhosts.timer`, delete its `.timer` and `.service`
files from `/etc/systemd/system/` and run `systemctl daemon-reload`; the old shared file
`<pulse root>/.runtime/known_hosts-00` can be deleted too. A pulse node that does not boot now
shows why in its hour's `nodes.json` (`error_detail.reason`: `host_key_changed`, `auth_failed`,
`unreachable`, `ssh_failed` or `not_active`) and in the journal line `pulse node ... boot failed`.

## Daily worker image (IMAGE-DAILY-1, #1136)

The droplets keep DB-IP in their image, and the manager updates its own release every day (the
orchestrator's `dbip_update` job at 05:23 UTC). `atlas campaign image-daily` brings the worker
image up to the manager's release:

```
~/atlas/<env>/venv/bin/atlas campaign image-daily --root ~/atlas/<env>            # dry run
~/atlas/<env>/venv/bin/atlas campaign image-daily --root ~/atlas/<env> --apply \
    --database-file ~/.config/atlas/<env>/database.toml
```

The dry run reads only the environment's files and prints the manager's release, the image the
daily template names, the action (`none`, `reuse` or `build`), the new image's name and the files it
would switch (`daily_template`, `pulse_fleet`). With `--apply`:

1. Nothing happens when the image's build record (`.secrets/campaign-images.json`) holds the
   manager's release (`db-ip/release.json`, the same SHA-256).
2. Otherwise a recorded build of that release and of the code's locked dependencies is taken, first
   copied to any region it misses; else a new one is built like `campaign image build`: a lon1
   builder for about 6 minutes, the wheel of `wheels/current.json` with the `requirements.txt`
   `server-setup.sh` writes beside it, `db-ip/dbip-full.mmdb`, the copies to the other regions side
   by side. Its name is `atlas-camp-worker-<version>-<wheel sha12>-<release date>`. The DigitalOcean
   token is `.secrets/do-token` (`--token-file`), never the keychain; the builder's SSH key is
   `.secrets/image-builder-key`, made on first use.
3. The image must be in every region; then, only between :15 and :45 past an hour (never around
   the :56 daily and :57 pulse launches), the daily template's `campaign-spec.json`
   (`image_by_region`, `image_name`) and `pulse/fleet.json` (`image_by_region`, snapshot ids) are
   each replaced in one step. A `fleet.json` the pulses refuse as it is stops the switch before any
   write. An hour already running keeps the image it was launched with.

A failed try (the build, a region copy, the check) leaves both files on the old image; the build
removes its builder through the scope guard. The run tries once more after 30 minutes; a second
failure opens one major alert `image_daily_failed` (Slack, with the failed step and the reason,
never a log line) and exits 3. Two runs at once refuse (`.secrets/.image-daily.lock`).

**The old image** goes after the switch, in the same run (also in a run with nothing to build,
so a removal left from an earlier day is finished). Every other recorded image (a build in
`.secrets/campaign-images.json` with its snapshot id) is removed through the scope guard, only
when the new one is in every region, no daily template or pulse fleet on the server names it
(production's or staging's: staging launches from production's image), a fresh read names it
`atlas-camp-worker-*`, and no droplet under a campaign or pulse tag was created from it. The hour
launched before the switch still runs on it: the run checks every 5 minutes for up to 3 hours,
then leaves it for the next day. A removed build keeps its record (`removed_snapshot_id`). The
output's `prune` shows what was `removed`, `held` (and why), `extra` (every worker snapshot
besides the kept one) and `exactly_one`. A worker snapshot no record names (such as one
named before the rename to Atlas), an image another template still names, a refused removal or an error
is one alert; such a snapshot is never removed by the run: remove it by hand once checked.
When the alert says another template or fleet names the old image, it is usually staging's
daily template (staging launches from production's image and the run never rewrites staging's
files): plan staging's template again with the new image (`campaign plan --image <new name>`
with production's `--images` inventory, then `manager upload-spec` and `orchestrator-enable
--daily-template` with staging's inventory). The next run then removes the old image.

**Turning it on** (production only; installed off by `orchestrator-enable`, like the hourly timers):

```
atlas campaign manager image-enable --inventory <production inventory>                  # dry run
atlas campaign manager image-enable --inventory <production inventory> --apply --owner-go <answer> [--at 05:45]
atlas campaign manager image-disable --inventory <production inventory> --apply
```

`image-enable --apply` needs the owner's recorded go, runs the dry run on the server first (it
must not refuse), writes `--at` (UTC, default 05:45, about 20 minutes after the DB-IP update) as a
drop-in when it differs, and adds `atlas-image.timer` to `timers.target`. `image-disable` (and
`orchestrator-disable`) take it out; a run under way finishes. The service never restarts a run
that ended with its own alert (exit 3) or a refusal (exit 2), and stops after 6 hours.

**Cost per day:** a rebuild runs one `s-1vcpu-1gb` builder in lon1 for about 6 minutes (under
$0.01). The image is stored once per region: about 4 GB in each of the 8 regions at $0.06 per GB a
month is about $1.90 a month, about $0.06 a day, for the one image kept (`size_gigabytes` on the
image gives its real size). On a rebuild day the old image adds a few hours of the same until it is
removed. A day without a new DB-IP release costs nothing extra.

## Slack alerts

`scripts/server-alerts.sh install <production|staging>` (run as the normal user; the setup
script calls it at its end for production only) keeps Slack informed about every Atlas service of
that environment:

- It writes the user units `atlas-alerts.service` and `atlas-alerts.timer` (a check every
  2 minutes of every installed environment) and the template `atlas-alert@.service`, copies itself
  to `~/atlas/bin/atlas-alerts`, and adds a drop-in `OnFailure=atlas-alert@%n.service` to every
  enabled or active `atlas*` unit of the environment (a template instance's drop-in goes on its
  template, so both API colours are covered). Unit files are never edited. A second run changes
  nothing. Only the first install of an environment on the machine posts "Atlas <env>: alerts
  installed on <host>" (the marker `~/atlas/<env>/alerts-state/installed-posted` records that it
  went out; a failed post is sent by the next install). Every later install, as each deploy's
  `server-setup.sh production` runs one, posts nothing. `server-alerts.sh test <env>` posts
  "Atlas <env>: alerts test from <host>" on demand. To announce a reinstall anyway, delete the
  marker before reinstalling.
- Watched units are the enabled or active ones: a unit switched off on purpose (the manager
  until GO) is not watched. `atlas-caddy.service` serves both environments and is watched by both.
- Messages: `<!everyone> Atlas <env> on <host>: <service>: <restarted|could not restart|recovered|check failed>: <error>`,
  then the unit's last 15 journal lines (a unit's own alert only; a manager alert row is posted
  as `manager alert <id> (<kind>): check failed: <message>` with no journal lines, since the
  orchestrator's journal is its access log). "restarted" when `NRestarts` grew since the last check,
  "could not restart" when the unit is failed (from the timer or from `OnFailure=`), "recovered"
  once when a reported unit or check is fine again.
- Checks, 5 s each: every active API colour's `/healthz` on loopback is `ok`; the manager's two
  ports accept TLS while the orchestrator is active; a read-only `SELECT 1` with
  `~/.config/atlas/<env>/database-api.toml` succeeds; the home file system is at most 85% full.
- Manager events, read only: new `major` or `critical` rows of the `alerts` table, read with the
  manager's login `~/.config/atlas/<env>/database.toml` (the API's login may not read it, #1013;
  one message each; the first check starts after the newest row), failed transient hourly test and pulse
  hour units (once each, one line naming the unit's journal, never its lines: PULSE-STATE-1; each
  such unit starts `atlas-alert@` itself through its own `OnFailure=`, since a transient unit gets no
  drop-in and is unloaded as soon as it fails) and
  `create_failed` lines in the orchestrator's journal.
- Pulse hours (PULSE-STATE-1): an hour whose results only wait behind the hourly calculation to be
  stored is done and posts nothing. A real loss posts one manager alert line each
  (`pulse_cells_lost`, `pulse_runs_open`, `pulse_hour_failed`, `pulse_teardown_failed`); the
  `atlas-pulse-YYYYMMDDTHH` unit fails, and posts "the pulse hour stopped with an error", only
  when the job crashed (exit 3, a loss already alerted, is its success: `SuccessExitStatus=3`).
  A pulse droplet that stops mid-hour has its pulses moved to a sibling droplet or its one
  replacement; the hour records it as the minor `pulse_nodes_stalled` alert (not posted), and
  only pulses that could not move page, as `pulse_cells_lost`. The reaper's `run_stale` alerts of
  the hour's nodes are acknowledged when the hour ends.
  `pulse_runs_open` is one line per network and region (PULSE-STATE-2): "pulse hour <hour> UTC:
  N country pulses of <network> in <city> (<region>) were not measured this hour (a gap in its
  data, not counted as down). Once: nothing to do. Every hour: tell the operator."
- The same environment, service and kind is posted at most once per 10 minutes; "recovered" is
  always posted. A post that fails is retried by the next check. State: `~/atlas/<env>/alerts-state/`.

### Wider checks (MONITOR-1, #1094)

`scripts/server-alerts/checks.sh` adds these checks to the same 2-minute timer (installed beside
the script as `~/atlas/bin/server-alerts/checks.sh`). Each is a check: **one** message when it
fails (`check failed: <what>`), **one** `recovered` when it is fine again, nothing in between.
All reads are read only (SQL in a read-only transaction, HTTP GET).

| Check (Slack name) | Fails when | When it runs |
| --- | --- | --- |
| `atlas-api serving` | an API colour is enabled but none is running (a failed colour is already "could not restart") | an API colour is enabled |
| `Caddy port 8080 /healthz` (staging 9080) | Caddy's port does not answer `/healthz` with `status` `ok` | an API colour is enabled |
| `public atlas.shifter.io/healthz` (staging `staging.atlas.shifter.io`) | not status 200 through the CDN, or `status` is not `ok` | always |
| `public …/v1/version` | not status 200, or `revision`, `builtAt` or `contract` is missing | always |
| `public … snapshot` | the public answers of `/v1/networks` and of home, rankings, targets and overlap for 24h, 7d and 30d, read through the CDN, carry more than one `generatedAt` (two builds) for 15 minutes; the message names each build's answers (API-SNAPSHOT-CONSISTENCY-1, #1267: a CDN copy outliving its `Cache-Control`) | always |
| `certificate atlas.shifter.io` | the public certificate expires in under 14 days, or cannot be read | always |
| `API connections` | the API's database login uses 80% or more of its connection limit (the role's own, else 20) | the database answers |
| `atlas-hourly.timer on time`, `atlas-pulse.timer on time` | the timer is on but has not fired for 63 minutes | the timer is on |
| `ingest` | the newest stored test run (`test_runs.imported_at`) is over 90 minutes old | the hourly test timer is on |
| `calculation` | the published calculation is over 90 minutes old, or there is none | the manager is on |
| `manager job <job>` | `calculate`, `estimates_release` or `browser_plan` has not run for 10 minutes, `raw_upload` for 20 minutes, `dbip_update` for 26 hours; or its last finished run failed | the manager is on |
| `mini browser-<n>` | a browser mini in the manager's node list (region `browser`) has sent no heartbeat for 30 minutes | the manager is on |
| `memory` | under 10% of the memory is available (`MemAvailable`) | once per server |
| `load` | the 15-minute load is over 2 per CPU | once per server |
| `system units` | a system (not user) unit is failed, such as PostgreSQL | once per server |
| `droplets <tag>` | a test worker or pulse droplet is older than its hour plus 30 minutes (an orphan, named) | once per server, with the environment's `venv/bin/atlas` and `.secrets/do-token` |
| `droplets count` | more than 250 test and pulse droplets are up (near the account's limit) | the same |
| `droplets` | `atlas daily droplets` (#1105, read-only `list_tag` calls) cannot run or read the cloud | the same |
| `image dependencies` | the worker image's locked dependencies (`requirements_sha256` in its build record) differ from the code's (`wheels/current.json`): rebuild the image | per environment, with `venv/bin/atlas` and `daily/template` |
| `image db-ip` | the image's DB-IP release (`mmdb_built_at`, else the build's date) is over 35 days old, or the image has none | the same |
| `image record` / `image` | no built record of the template's image in `.secrets/campaign-images.json`, a file unreadable, or `atlas daily image` cannot run | the same |

The jobs, the minis and the ingest are read with the manager's login (`database.toml`), the
calculation and the connections with the API's (`database-api.toml`). When `SELECT 1` fails, only
`database` is posted: the checks that need the database wait until it answers again. Just after the
manager is switched on, each job and the calculation wait their own limit, and the ingest waits
its limit plus an hour after the hourly test is (no alert for the time it was off). A check that
stops running (the manager or a timer switched off, a mini gone from the node list) posts its
`recovered: no longer checked` once, so nothing stays down for good. "Once per
server" checks run with production (with staging only when production has no alerts installed).
The browser minis post their own checks (`docs/operations/browser-minis.md`). A failed
droplet teardown is the manager's `pulse_teardown_failed` alert row (posted above); the droplet it
leaves behind is a `droplets` orphan here.

**All good:** at 08:00 UTC each environment posts `Atlas <env> on <host>: all good` once, when no
check is failing. An `Environment=ATLAS_ALERTS_HEARTBEAT_HOUR=<hour>` drop-in on `atlas-alerts.service`
changes the hour (`off`: none).

**Silence** (for planned work): `~/atlas/bin/atlas-alerts silence <production|staging> <minutes>`
holds every message of that environment for that long; `silence <env> 0` ends it early. Nothing
is lost: a check still failing after the silence is posted then, and a recovery is posted as usual.

The webhook URL is a secret. It lives only in `~/.config/atlas/slack-webhook` (mode 600; the
script refuses any other mode), copied there over ssh stdin, and reaches `curl` on its stdin
(`curl -K -`), so it never appears in argv, `ps`, logs, Slack text or output.

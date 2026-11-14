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
   which must be on `main` and unmodified; the venv `~/atlas/<env>/venv` from `uv sync --locked`.
3. **Build:** `siteapi` into `~/atlas/<env>/api/<commit>/siteapi` (`-trimpath`, no cgo);
   `go version -m` must name the commit with `vcs.modified=false`.
4. **Private files** in `~/.config/atlas/<env>/` (folder 700, files 600, or the script stops
   with the file's name): `database-api.toml`, the API's read-only database login; `origin-key`,
   the CDN's secret header value, made from 32 random bytes when missing and never printed.
   Optional: `api.env` with `ATLAS_API_FLAGS="..."`, extra `siteapi serve` flags (the CDN settings).
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
   the database safe under a flood.
8. **Alerts, production only:** if `scripts/server-alerts.sh` exists, `server-setup.sh production`
   ends with `server-alerts.sh install production`. `server-setup.sh staging` installs no alert
   units and removes none (the owner's decision of 2026-11-14): staging is not watched unless an
   operator installs its alerts by hand.

A second run with the same commit changes no file. `--dry-run` prints every action and
changes nothing. The managers' own units are installed by `campaign manager deploy` (host mode).

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
the alerts `atlas-alerts.timer` and `atlas-alert@.service`, and the Slack webhook file
`~/.config/atlas/slack-webhook`.

## Slack alerts

`scripts/server-alerts.sh install <production|staging>` (run as the normal user; the setup
script calls it at its end for production only) keeps Slack informed about every Atlas service of
that environment:

- It writes the user units `atlas-alerts.service` and `atlas-alerts.timer` (a check every
  2 minutes of every installed environment) and the template `atlas-alert@.service`, copies itself
  to `~/atlas/bin/atlas-alerts`, and adds a drop-in `OnFailure=atlas-alert@%n.service` to every
  enabled or active `atlas*` unit of the environment (a template instance's drop-in goes on its
  template, so both API colours are covered). Unit files are never edited. A second run changes
  nothing. It ends by posting "Atlas <env>: alerts installed on <host>"; `test <env>` posts the
  same.
- Watched units are the enabled or active ones: a unit switched off on purpose (the manager
  until GO) is not watched. `atlas-caddy.service` serves both environments and is watched by both.
- Messages: `<!everyone> Atlas <env> on <host>: <service>: <restarted|could not restart|recovered|check failed>: <error>`,
  then the unit's last 15 journal lines. "restarted" when `NRestarts` grew since the last check,
  "could not restart" when the unit is failed (from the timer or from `OnFailure=`), "recovered"
  once when a reported unit or check is fine again.
- Checks, 5 s each: every active API colour's `/healthz` on loopback is `ok`; the manager's two
  ports accept TLS while the orchestrator is active; a read-only `SELECT 1` with
  `~/.config/atlas/<env>/database-api.toml` succeeds; the home file system is at most 85% full.
- Manager events, read only: new `major` or `critical` rows of the `alerts` table (one message
  each; the first check starts after the newest row), failed transient hourly test units (once
  each) and `create_failed` lines in the orchestrator's journal.
- The same environment, service and kind is posted at most once per 10 minutes; "recovered" is
  always posted. A post that fails is retried by the next check. State: `~/atlas/<env>/alerts-state/`.

The webhook URL is a secret. It lives only in `~/.config/atlas/slack-webhook` (mode 600; the
script refuses any other mode), copied there over ssh stdin, and reaches `curl` on its stdin
(`curl -K -`), so it never appears in argv, `ps`, logs, Slack text or output.

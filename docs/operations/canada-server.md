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
   `tls internal` (one certificate, `atlas-origin`, for any name or address) and answers 403
   unless the request has the header `X-Atlas-Origin-Key` with the environment's origin key.
8. **Alerts:** if `scripts/server-alerts.sh` exists, it ends with `server-alerts.sh install <env>`.

A second run with the same commit changes no file. `--dry-run` prints every action and
changes nothing. The managers' own units are installed by `campaign manager deploy` (host mode).

## Layout

| | production | staging |
|---|---|---|
| Manager: pulse nodes / browser uploads | 8443 / 8444 | 9443 / 9444 |
| API through Caddy (the CDN's origin, HTTPS) | 8445 | 9445 |
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
script calls it at its end) keeps Slack informed about every Atlas service of that environment:

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

# Moving production's campaign manager from London to Toronto (tor1)

Issue #769 (MANAGER-PROD-MOVE-1). **This is a plan only.** Nothing in it has been run, and
nothing of production was read with credentials to write it: every fact below comes from the
repository's code and runbook (`docs/campaign.html`, `docs/browser.html`). Nothing is executed
until the owner approves the plan, the window and each irreversible step.

Why: the PostgreSQL server is at OVH near Montreal. The London manager is 78 ms from it, a
tor1 droplet about 8 ms (#767, #768). Every database round trip of the ingest, the planners
and the calculation pays that distance.

## The owner's decisions

Each one is asked in plain words; the recommended answer is first.

1. **Approve this plan** as the way to move. Nothing runs before this.
2. **The size of the new manager.** Recommended: `s-8vcpu-16gb-amd`, the same as London today.
   The orchestrator step only accepts this size (`manager_orch.WANTED_SIZE`), and the measured
   load was bound by the database's distance, not by CPU (section 2). Any other size is a code
   change first.
3. **A new production droplet, not the staging one.** Recommended: a new droplet made by
   `campaign manager deploy --region tor1` in the production environment. The staging tor1
   manager (MANAGER-TOR-1, #768) stays staging: the tooling binds each inventory to one
   environment and staging's names and tags (`pi-camp-stg-`) cannot become production's.
4. **The staging tor1 manager after #718.** Recommended: a full staging teardown when #718's
   7-day load has finished (about $1 an hour while it runs). Alternative: keep it for later
   staging work.
5. **The database's allowed list.** Only the owner can change it: add the new manager's
   address to the OVH server's firewall and `pg_hba` for the production database and its
   run-time role, before the window. The address is sent in chat only, never on GitHub.
   After the London teardown, remove London's address.
6. **The window.** Recommended: a weekday hour of the owner's choice, both the operator and
   the owner reachable for 2 hours, the switch itself between `:15` and `:50` past the hour UTC
   (section 4). Not between 03:30 and 05:45 UTC (the nightly analyze at 03:47 and the DB-IP
   update at 05:23 run then).
7. **A go for each irreversible or production-changing step**, recorded as an answer file like
   every earlier go (D9, D13):
   - go A: the switch (London's orchestrator stops, tor1's starts). It can be undone until the
     teardown (section 6);
   - go B: the hourly daily test on tor1 (`hourly-enable`), when it is on in London today;
   - go C: tearing London down after the soak. This cannot be undone. It also lifts, for this
     move only, the standing rule that production's manager is never torn down;
   - go D: removing London's address from the database's allowed list.
8. **The soak.** Recommended: 72 hours, so tor1 runs three full days (three DB-IP updates,
   three nightly analyzes, every browser queue position on each mini three times) while London
   stays ready to take back over. Cost: both managers for 3 days (section 7).
9. **Tooling first or by hand.** Recommended: the two required tooling issues in section 8
   land before the window. Without them, two steps are done by hand and the London teardown is
   refused while tor1's daily workers run.

## 1. Production's manager today (London)

From the code and the runbook. "Manager" paths are on the droplet, under
`/opt/proxy-index-campaign` unless named otherwise.

### Services and timers

| Unit | What it does | Started by |
|---|---|---|
| `proxy-index-orchestrator.service` | The node API on port **8443** (HTTPS, open to the tag `pi-pulse-node`), the browser port **8444** (HTTPS with client certificates, open to every address), the admin API, and the scheduler with every job: census and pulse planning, pulse evaluation, estimate release, the hourly calculation (in its own `proxy-index-calculate.scope`, 8 GB cap), DB-IP daily at 05:23 UTC, `raw_upload` every 5 minutes, retention, reports, the browser plan every minute. `MemoryMax=12G`. | `campaign manager orchestrator-enable --apply` |
| `proxy-index-db-analyze.timer` | `db-server analyze` daily at 03:47 UTC. | the same |
| `proxy-index-daily.timer` | The hourly daily test: at `HH:56` UTC it starts the next hour as its own unit `proxy-index-daily-YYYYMMDDTHH`, which runs the hour's workers and imports the hour into the database from the manager itself. No `[Install]` section: only `hourly-enable --apply` starts it. | `campaign manager hourly-enable --apply` (owner's go) |
| `proxy-index-campaign-*@<cid>` | The older hourly droplet campaign's tick, collector and watchdogs, one set per enabled campaign. | `campaign manager enable` |

The DB-IP update, the raw spool's archive to Spaces, the collector of the daily hours and the
alerts are not separate units: the first two are orchestrator jobs, the daily hour collects and
imports its own workers, and alerts are rows in the shared database (the campaign units also
write `alerts.jsonl` in their campaign root).

### State on the manager's disk

| What | Where | Made by | Moves how |
|---|---|---|---|
| The wheel, venv, worker wheel | `venv/`, `wheels/` | `deploy` | rebuilt by `deploy` on tor1 |
| DigitalOcean token | `.secrets/do-token` | `deploy` (from the keychain) | written again by `deploy` |
| Provider credentials per network | `.secrets/credentials/<net>.toml` | `deploy --networks` | written again by `deploy` with the same networks |
| Spaces settings | `.secrets/spaces.toml` | `deploy --spaces-file` | written again by `deploy` |
| Worker SSH key | `.secrets/worker-key` | `deploy` (never leaves the manager) | **new** on tor1 (new key name) |
| Orchestrator TLS key and certificate (for the manager's address, 825 days) | `.secrets/tls/orchestrator.{key,crt}` | `orchestrator-enable` | **new** on tor1 (the certificate names the address) |
| Browser hosts' client CA | `.secrets/tls/browser-ca.{key,crt}` | `orchestrator-enable` | **new** on tor1 (made there) |
| Admin password | `.secrets/orchestrator.env` | `orchestrator-enable` | **new** on tor1 |
| The database file (run-time role) | `/etc/proxy-index/database.toml`, mode 600 | **by hand**: no command writes it; `orchestrator-enable` only checks it | by hand (section 8, issue 3) |
| Formulas | `daily/formulas/` | `orchestrator-enable` (from the repository) | `orchestrator-enable` on tor1 |
| Daily template campaign | `campaigns/<cid>`, link `daily/template` | `campaign plan` + `upload-spec`, recorded as `daily_template` | **planned again** for tor1: a spec names its manager, wheel and worker key |
| Daily hours' roots | `campaigns/<hour cid>` beside the template | each daily hour | stay in London (imported and spooled already) |
| Raw spool (30 days, every imported run's raw file) | the `raw.spool_dir` setting | the orchestrator's API and each daily hour | **copied** (section 3, step P7 and C9) |
| DB-IP file | `db-ip/dbip-full.mmdb` (setting `dbip.dir`) | the `dbip_update` job | copied, or fetched once on tor1 |
| Browser settings | the `browser.settings_file` setting (a private file) | by hand (BROWSER-HOSTS-SETTINGS-1) | copied |
| Reports | the `reports.dir` setting | the `reports` job | copied (optional) |
| Archive work folder | `.state/archive` | the archive job | not needed (scratch) |

Settings live in the shared database, so tor1 inherits them. Every path setting above points
inside `/opt/proxy-index-campaign`, which `deploy` lays out the same way on tor1, so no
setting changes value. The runbook's cutover step "set the path settings" becomes "read them
and confirm".

On the Mac, beside the inventory: the inventory itself, its `known_hosts` file, the
orchestrator certificate `<inventory>.orchestrator.crt`, the audit log, and the owner's go
files. The tor1 manager gets its own inventory (section 3); London's stays untouched for
rollback.

### External dependencies

| Dependency | Today | After the move |
|---|---|---|
| The database's allowed list (OVH firewall and `pg_hba`) | admits London | admits tor1 too (owner, before the window); London removed after teardown (owner, go D) |
| `pi-camp-manager-fw` (by droplet id) | SSH from the Mac's IPv4; 8443 from the tag `pi-pulse-node`; 8444 from everywhere | a new firewall of the same name for the tor1 droplet, made by `deploy` and `orchestrator-enable` |
| `pi-camp-worker-fw` (by the tag `pi-camp-worker`) | SSH to workers only from London | must also admit tor1 from its first daily hour (section 8, issue 1) |
| Tags `pi-camp-manager`, `pi-camp-worker`, `pi-pulse-node` | London carries `pi-camp-manager` | both managers carry it during the soak |
| Worker images (snapshots in `campaign-images.json`) | shared, read only | unchanged; never removed by this move |
| The browser minis (`browser-1` to `browser-3`) | `--orchestrator https://<London>:8444`, London's certificate as `--ca-file`, a client certificate of London's browser CA | the tor1 address, tor1's certificate, a new client certificate from `browser-cert` on tor1 |
| Pulse nodes (when a fleet is armed) | `fleet.json` `orchestrator_url` and `ca_file` point at London | tor1's; each node armed again (section 8, issue 4) |
| DNS | none: every client uses the manager's address, and the certificate names the address | none (a name is a later option, out of scope) |
| Production siteapi on the tor1 API droplet | reads the database only; never talks to the manager | unchanged; check that the website's numbers move after the first calculation on tor1 |
| Mac watch agent (`campaign guard install --inventory …`) | when installed, watches London's inventory | re-installed with tor1's inventory, if installed |

## 2. The target

**A new production manager in tor1, `s-8vcpu-16gb-amd`, deployed with**
`campaign manager deploy --region tor1` **under its own inventory.**

- **Size.** What the repository records about the load:
  - REPLAY-RUN-1 (#614) on staging at 78 ms: 190 daily tests took 50 minutes at **2% CPU**;
    the time was round trips. INGEST-RTT-1 (#663) then batched the store to a fixed number of
    round trips per batch, and the 8 ms link cuts each round trip about ten times.
  - The browser queue is 144 runs a day across three minis (#691): small for the manager.
  - Memory: the orchestrator is capped at 12 GB and one calculation at 8 GB, together more
    than the droplet's 16 GB. That is true in London today and unchanged by the move (risk R9).
  - The workers, not the manager, do the measuring.

  So the same size is enough, and it is the only one `orchestrator-enable` accepts.
  **Before the window**, the operator reads London's real figures once, read only, with the
  owner's go for that read (step P1): load average, the orchestrator unit's peak memory
  (`systemctl show -p MemoryPeak`), the last calculations' durations from the job health, and
  the daily hours' ingest times. If any is near its limit, the owner decides the size again.
- **Why not the staging droplet.** Its inventory, name, tags and firewalls are staging's and
  the tooling refuses to turn them into production's; it is `c-32`, four times the need; and
  staging and production never share a manager.
- **Why a second inventory.** `deploy --region tor1` on London's inventory is refused while
  London is recorded (MANAGER-REGION-1): the only path in one inventory is to tear London down
  first, which leaves no rollback and makes the database allowed-list change fall inside the
  outage (the address is only known after the deploy). A second production inventory lets
  tor1 be built, checked and admitted by the database while London keeps running.

## 3. Before the window (nothing in production changes)

Every command runs on the Mac from the framework checkout at the SHA London runs (its
inventory records `framework_version` and `wheel_sha256`), so the move changes one thing: the
place. **Always pass `--inventory` explicitly**: `deploy`, `plan`, `upload-spec`, `enable`,
`disable`, `allow-me` and `teardown` default to `.secrets/campaign-manager.json`, so a missing
flag acts on that file.
Below, `<london>` is production's current inventory and `<tor1>` a new, empty one beside it.

| Step | Command and check | Time |
|---|---|---|
| **P0** | The section 8 tooling issues are merged (or the owner accepts the manual steps). | – |
| **P1** | Read only, with the owner's go: London's load figures (section 2), the size of the raw spool (`du -sh` of `raw.spool_dir`) and of the DB-IP file, the path settings from the admin API (`/admin/api`), whether the hourly test is on (`hourly` in `<london>`), which pulse nodes are armed (`pulse-fleet status --root …`), and whether the Mac watch agent is installed (`campaign guard status`). Also check that `s-8vcpu-16gb-amd` is offered in tor1 (DigitalOcean's size list, read only). | 20 min |
| **P2** | `proxy-index campaign manager deploy --account proxy-index --environment production --region tor1 --size s-8vcpu-16gb-amd --inventory <tor1> --wheel dist/proxy_index-<version>-py3-none-any.whl --requirements dist/requirements.txt --networks <the networks in <london>'s credentials> --spaces-file <the [spaces] settings file> --mac-key <production's Mac key>`. Check: it ends with the ids, `hardened` was printed, `<tor1>` records `manager_region: tor1` and `manager_size: s-8vcpu-16gb-amd`; `campaign manager resize --inventory <tor1>` (dry run) reports `action: none`. | 15 min |
| **P3** | The operator sends tor1's address to the orchestrator thread in chat; **the owner adds it to the database's allowed list** (decision 5) and says "done". | owner |
| **P4** | The database file: the run-time role's TOML (the one London uses) written to `/etc/proxy-index/database.toml`, mode 600, over SSH stdin, never in `argv` (or `deploy --database-file`, section 8 issue 3). Check: on tor1, `proxy-index db-server check --database-file /etc/proxy-index/database.toml` answers (read only), and the median TCP connect time of 20 connects to the database port is about 8 to 9 ms. Post the median in the issue, never the address. | 10 min |
| **P5** | The daily template for tor1: `proxy-index campaign plan --account proxy-index --inventory <tor1> --images <production images inventory> --start <a future whole hour> --networks <the London template's networks> --regions <its regions> --concurrency <its value> --size <its worker size> --image <its snapshot> --output <dir>`, then `proxy-index campaign manager upload-spec --inventory <tor1> <dir>/<campaign id>`. Read the London template's values from its planned root on the Mac. Check: `upload-spec` accepts it (it refuses a spec for another manager, wheel or account). | 10 min |
| **P6** | `proxy-index campaign manager orchestrator-enable --inventory <tor1> --daily-template <campaign id>` **without** `--apply`. Check: the printed firewall rules (22 from the Mac, 8443 from `pi-pulse-node`, 8444 from everywhere), the unit hashes, the `seed_targets` command. | 2 min |
| **P7** | Copy what tor1 needs before it starts, each through the Mac as a pipe (managers cannot reach each other; nothing is stored on the Mac): the DB-IP file to `db-ip/dbip-full.mmdb`, the browser settings file to the path its setting names, the reports folder (optional), and the raw spool's **ended, uploaded** hours, each with its `uploaded.json`: `ssh <London> 'tar -C <spool> -cf - <hours>' \| ssh <tor1> 'tar -C <spool> -xf -'`, files 600, folders 700, then compare SHA-256 lists on both sides. (Or `campaign manager copy-state`, section 8 issue 2.) | 10–40 min, by size |
| **P8** | The browser minis: the owner (or whoever runs them) confirms who can change each mini's launchd agent during the window, and that each mini **keeps its London certificate files** for rollback. | owner |

After P8, tor1 is built, admitted and holds its inputs, with no scheduler running. London is
unchanged.

## 4. The switch (one hour, with go A)

Called hour **H−1** below; tor1 runs the daily test from hour **H** on. Start at `H−1:15`,
once that hour's calculation (it starts at `:06`) has finished; if it has not by `H−1:45`,
wait for the next hour.

| Step | Command and check | Time |
|---|---|---|
| **C1** | Read only on London: the hour's calculation finished in the job health; `raw_upload` uploaded hour H−2 (it packs an hour 5 minutes after it ends); the daily hour H−1 runs (`systemctl list-units 'proxy-index-daily-*'`). | 5 min |
| **C2** | `proxy-index campaign manager orchestrator-disable --inventory <london> --apply`. It stops the orchestrator, the analyze timer **and** the hourly timer, and closes 8443 and 8444. Check on London: `systemctl is-active proxy-index-orchestrator` is `inactive`. The running daily hour H−1 is **not** stopped: it finishes on London, imports into the database and spools there. | 1 min |
| **C3** | Read the path settings in the database (admin API on tor1 after C4, or `psql` as the operator): unchanged, as recorded at P1. | 2 min |
| **C4** | `proxy-index campaign manager orchestrator-enable --inventory <tor1> --owner-go <go A> --apply`. Check: it prints `healthy` (it waits for `/health` over HTTPS), `<tor1>.orchestrator.crt` is written, `systemctl status proxy-index-orchestrator` on tor1, the admin job health. **C2 must have passed first**: two orchestrators must never run against one database (two schedulers would plan the same slots; nothing in the code stops it, risk R8). | 3 min |
| **C5** | Only when London's hourly test was on: `proxy-index campaign manager hourly-enable --inventory <tor1> --owner-go <go B> --apply`, **before `H−1:56`**. Check: `systemctl is-active proxy-index-daily.timer` is `active` on tor1; at `H−1:56` the unit `proxy-index-daily-<H>` starts on tor1. | 1 min |
| **C6** | Browser minis, one at a time: `proxy-index campaign manager browser-cert --inventory <tor1> --host browser-<n> --out <dir>`; install `browser-<n>.key`, `.crt` and `<tor1>.orchestrator.crt` in the mini's state folder (owner-only, 600) **beside** the London files, set `--orchestrator https://<tor1>:8444` and the three file flags in its launchd agent, restart it. Check: the host's next heartbeat and its next `/v1/results` upload on tor1. | 10 min per mini |
| **C7** | Pulse nodes, only when a fleet is armed: set `fleet.json` `orchestrator_url` to `<tor1>`'s `orchestrator.orchestrator_url` and `ca_file` to `<tor1>.orchestrator.crt`, then arm every node again (section 8 issue 4; without it, `pulse-fleet retire --name <node>` and `reconcile` for each node, which makes new droplets). Check: each node registers and uploads. | 15–30 min |
| **C8** | Verify: the census, pulse and browser plans run; the first uploads are stored; the next calculation at `H:06` succeeds and the website's numbers move; no `job_failed` alert. | 30 min |
| **C9** | When London's daily hour H−1 has finished (`daily.json` in its root, up to about `H:30`): copy London's spool hours **not yet uploaded** (H−1 and later) into tor1's spool: a folder tor1 lacks is copied whole; in a folder tor1 already has, only the `.tar.gz` files it lacks, **never London's `uploaded.json`** (London never uploaded these hours, so tor1 uploads them, as a further part when it has already sent its own). Check: tor1's next `raw_upload` result lists those hours, no `archive_changed` error. | 10 min |
| **C10** | If the Mac watch agent was installed: `campaign guard install … --inventory <tor1>`. | 2 min |

London then stays up with nothing running and its secrets in place, ready for rollback.

## 5. The cutover window: what pauses, and the straddling hour

- **The orchestrator** is down from C2 to C4, about 3 to 5 minutes. Nothing is planned in that
  gap. Nodes are marked offline after 60 s and in-flight batches are reaped after 15 minutes
  (`batch.stale_seconds`), so keeping the gap short keeps every batch. A calculation is never
  running then (C1).
- **The daily test** loses no hour and runs none twice. London started hour H−1 at `H−2:56`
  and finishes it; C2 turns London's timer off before `H−1:56`, so London never starts hour H;
  tor1's timer, on since C5, starts hour H at `H−1:56`. Hours overlap freely by design, each
  with its own workers. An hour is never started twice on one manager (its unit name), and
  across two managers only this order prevents it, so C2 must precede C5.
- **The browser queue** pauses for each mini from C2 until that mini is switched (C6). The
  queue's catch-up rule (#691) plans a missed entry at the host's next free gap, at most one
  per gap, if it can still finish before its next day's start; otherwise it is skipped with
  the alert `browser_run_missed`. With one entry every 30 minutes per mini, a 30-minute pause
  loses at most one entry per mini, which is caught up. A run already running on a mini
  finishes; its upload to London fails, stays in the mini's outbox and is sent again to tor1
  after C6.
- **Ingest.** A run stored by London whose answer was lost is sent again to tor1 and recognized
  as already stored, not stored twice (the idempotent ingest, #663; the browser outbox accepts
  only the server's confirmation of its SHA-256). Pulse and census uploads that fail stay on
  the node and are sent again (runbook).
- **The raw spool.** London stops uploading at C2, after hour H−2 is in Spaces. tor1 uploads
  from hour H−1 on, and London's later files join through C9. Spaces never overwrites an object
  (`put_verified`), which is why London's `uploaded.json` must not be copied over tor1's for an
  hour tor1 writes.

## 6. Rollback

| Until | Back to London |
|---|---|
| Before C2 | Nothing to undo. Stop. tor1 can be retired later (section 8 issue 1: a **full** teardown of `<tor1>` would also remove the shared worker snapshots, so never run it). |
| After C2, before the London teardown | `orchestrator-disable --inventory <tor1> --apply` (it also stops tor1's hourly timer); `orchestrator-enable --inventory <london> --owner-go … --apply` (London's certificate, browser CA and password are still there, so nothing changes for clients that kept London's files); `hourly-enable --inventory <london>` with the owner's go, under the same hour rule as C2/C5 the other way round; the minis back to London's URL and their kept London files; pulse nodes armed again with London's values; tor1's not-uploaded spool hours copied to London by the C9 rule. About 30 minutes. London's database allowed-list entry and its worker-firewall rule stay until the teardown for this reason. |
| After the London teardown (go C) | No way back to London. A return needs a new manager, a new move. |

## 7. Soak, then London's teardown

**Soak, 72 hours (decision 8).** London stays powered with nothing running. Measure on tor1
and compare with London's figures from P1:

- the ingest time of each daily hour (the `proxy-index-daily-*` journal and `daily.json`),
  which was bound by the 78 ms link: the main reason for the move;
- the median TCP connect time to the database (expect 8 to 9 ms);
- the calculation's duration, the orchestrator's peak memory, the load average;
- `raw_upload` health and the Spaces objects of the straddling hours;
- browser uploads per mini per day (48 expected) and any `browser_run_missed`;
- the DB-IP update at 05:23 UTC and the nightly analyze at 03:47 UTC.

**Teardown, with go C:**

| Step | Command and check | Time |
|---|---|---|
| **T1** | Collect reports for every campaign `<london>` records, as `teardown` requires (open question Q3). | depends |
| **T2** | `proxy-index campaign manager teardown --inventory <london> --keep-worker-assets [--after-collect …]`. It shreds London's secrets, then removes London's droplet and its `pi-camp-manager-fw`, and keeps the worker snapshots, `pi-camp-worker-fw`, the worker key and the Mac key. Today it is **refused while any `pi-camp-worker` droplet exists**, which tor1's daily hours make almost always true (section 8 issue 1). A production teardown also removes the Mac watch agent, which by then watches tor1: install it again after (C10). Never a full teardown of `<london>`: it would remove the worker snapshots that tor1 uses. | 10 min |
| **T3** | London's worker-firewall rule and its old worker key are removed (section 8 issue 1). | 5 min |
| **T4** | The owner removes London's address from the database's allowed list (go D). | owner |
| **T5** | Proof: London's droplet is gone, `pi-camp-manager` lists one droplet (tor1), the minis no longer hold London's files (they may be deleted now). From now on `<tor1>` is production's inventory; the repository's mentions of production's inventory and region are updated in a small docs change. | 10 min |

**Cost.** The second `s-8vcpu-16gb-amd` costs about the same as London's (DigitalOcean's list
price on the day; roughly $0.15 to $0.20 an hour), so a 72-hour soak adds about $10 to $15. The
staging `c-32` costs about $1 an hour until its own teardown (decision 4).

## 8. Missing tooling (for the architect to file)

| # | Issue | Why | Size (production lines) | Needed |
|---|---|---|---|---|
| 1 | **MANAGER-SUCCESSOR-1**: a production manager in a second inventory replaces the first. The successor's `deploy` makes the workers admit its SSH (today it makes a second `pi-camp-worker-fw` on the same tag; whether DigitalOcean accepts a duplicate name is to be proven in the fake-cloud tests, or the successor takes over the existing firewall). `teardown --keep-worker-assets` of the superseded manager does not refuse because of the successor's workers and judges its proof by its own droplet and campaigns, then removes its own worker-firewall rule and worker key, never the snapshots or the shared Mac key, and leaves the Mac watch agent to the successor. A full teardown of an inventory that does not own the snapshots refuses. | Without it the soak, the rollback and T2/T3 are blocked or need hand-work on firewalls. | 150–250 | yes |
| 2 | **MANAGER-STATE-COPY-1**: `campaign manager copy-state --from <inventory> --to <inventory> [--spool-hours …]` pipes the raw spool (with the C9 merge rule), the DB-IP file, the browser settings file and the reports folder through the Mac and compares SHA-256 on both sides. | P7 and C9 by hand are long piped commands under time pressure. | 100–150 | recommended |
| 3 | **MANAGER-DB-FILE-1**: `deploy --database-file <file>` streams the run-time role's TOML into `/etc/proxy-index/database.toml` (0600) over SSH stdin, like `--spaces-file`. | No command writes it today (P4). | 40–60 | recommended |
| 4 | **PULSE-REARM-1**: `pulse-fleet reconcile --rearm` arms already-armed nodes again with the current `fleet.json` (new `orchestrator_url`, `ca_file`). | `reconcile` only arms nodes that are not armed; today each node must be retired and recreated. | 60–100 | only if a fleet is armed |
| 5 | **ORCH-SINGLE-1**: the orchestrator holds a database session lock while it schedules and refuses to start while another holds it. | Only the order C2 → C4 prevents two schedulers on one database (R8). | 40–70 | recommended |
| 6 | **ORCH-PREPARE-1**: `orchestrator-enable --prepare` installs the units and makes the TLS certificate and browser CA without starting anything, so the minis' new files can be issued and staged before the window. | Shortens the browser pause at C6 from minutes per mini to a restart. | 30–50 | optional |

## 9. Risks

| # | Risk | What reduces it |
|---|---|---|
| R1 | **The minis' certificate and endpoint change.** The certificate names the manager's address and the browser CA is made on the manager, so every mini needs a new URL, a new pinned certificate and a new client certificate. A mistake stops that mini's runs. | One mini at a time (C6), checked by its next upload; the London files stay on the mini for rollback; the catch-up rule covers short pauses. |
| R2 | **Firewalls and tags.** Workers admit SSH only from the recorded manager; a wrong worker firewall stops tor1's first daily hour at its SSH step. Both managers carry `pi-camp-manager` during the soak; London's teardown proof counts that tag. | Issue 1 before the window. Check `pi-camp-worker-fw`'s rules after P2. |
| R3 | **The database's allowed list** is outside the repository (owner). If tor1 is not admitted, P4 fails, before anything changes. | P3/P4 before the window. London's entry stays until go D. |
| R4 | **Data in flight**: the straddling daily hour, browser runs, pulse uploads, batches reaped after 15 minutes. | Section 5: London finishes hour H−1; uploads are retried and stored once; the orchestrator gap stays under 5 minutes. |
| R5 | **The spool.** An hour written on both managers can collide in Spaces (never overwritten, `archive_changed`), and an hour left only on London is missing from tor1's 30-day local copy. | C9's merge rule; check `raw_upload` on tor1. |
| R6 | **Cost**: both managers for the soak, plus the staging `c-32` until its teardown. | Section 7. |
| R7 | **The wrong inventory.** Several commands, `deploy` and `teardown` among them, default to `.secrets/campaign-manager.json`, and a full teardown of either production inventory would remove the shared worker snapshots. | `--inventory` on every command; `--keep-worker-assets` only; issue 1's refusal. |
| R8 | **Two orchestrators on one database.** Nothing in the code refuses a second scheduler. | C2 checked `inactive` before C4; issue 5. |
| R9 | **Memory**: the orchestrator's 12 GB cap plus one calculation's 8 GB exceed the droplet's 16 GB. True in London today, unchanged. | P1's peak memory figures; the owner decides the size again if it is close. |
| R10 | **A newer wheel during the move** could add a second change. | P2 uses London's own SHA; an upgrade is a separate step after the soak. |

## 10. Open questions (the runbook is silent)

- **Q1.** Who writes `/etc/proxy-index/database.toml` on a production manager, and from which
  file? London's was placed by hand. (Issue 3 answers it for the future.)
- **Q2.** Who can change each browser mini's launchd agent during the window, and how
  (the minis are the owner's home machines)?
- **Q3.** Which campaigns does production's inventory record, and does each need a collect
  report for `teardown` (the daily template never runs as a campaign itself)?
- **Q4.** Is a pulse fleet armed against London today, and does the hourly daily test run
  (P1 reads both)?
- **Q5.** Is the Mac watch agent installed for production's daily mode?
- **Q6.** Do the daily hours' roots on London (already imported and spooled) need to be kept
  anywhere before the teardown shreds the manager, or is the database plus Spaces enough?

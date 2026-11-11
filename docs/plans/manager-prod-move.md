# Moving production's campaign manager from London to Toronto (tor1)

## In plain words: read this first

**What Proxy Index is.** Proxy Index tests proxy networks (companies that rent out internet
addresses) and shows the results on its website. Behind the website, one rented server, the
**manager**, runs the whole schedule: every hour it starts short-lived test machines, collects
their results, stores them in the database (the one big store of every result) and recalculates
the numbers the website shows. The manager of the real website is called **production**; the
copy we use for trials is called **staging**. We rent the manager from DigitalOcean, a hosting
company that calls a rented server a **droplet** and names its places by short codes: `lon1` is
London, `tor1` is Toronto.

**What moves.** Production's manager moves from its server in London to a new server in Toronto.
The database, the website and what we measure do not change.

**Why.** The database is on a server at OVH (another hosting company) near Montreal, in Canada.
Every question the manager asks the database travels there and back: from London that takes
78 ms (thousandths of a second), from Toronto about 8 ms. The manager asks very many such
questions every hour, to store each hour's results, plan the next tests and recalculate the
numbers. On staging, with the London distance, 190 hourly tests took 50 minutes to process
while the server's processors were busy only 2% of the time: almost all of it was waiting for
the database. From Toronto each wait is about ten times shorter.

**What you will notice.**
- The website stays up the whole time: it reads the database directly and never talks to the
  manager.
- On the day of the switch, for about 3 to 5 minutes no new test is planned.
- The browser tests on the three Mac minis at home (`browser-1` to `browser-3`) pause until
  each mini is pointed at Toronto, about 10 minutes per mini. Each mini needs Toronto's address
  (its **endpoint**: the web address the mini sends its results to) and new **certificates**
  (small digital ID files by which the mini and the manager recognise each other). Someone
  must change these settings on each mini during the switch; the person running the move (the
  operator) will ask you who.
  A browser test that was due during the pause is run in the mini's next free gap; at most
  one per mini is late.
- After the switch we expect each hour's results to be stored much faster, so the website's
  numbers are ready sooner.
- You are asked for: an answer to the nine questions below, one change on the database server
  that only you can make, 2 hours of being reachable on the day of the switch, and a separate
  yes at four moments of the move.

**What it costs.** The Toronto server is the same size and about the same price as London's
(roughly $0.15 to $0.20 an hour). For the 3 days when both run side by side, the move adds about
$10 to $15. Once London is removed, the monthly cost is the same as today. Separately, the
staging server already in Toronto costs about $1 an hour until it is removed (question 4).
Before the move, developers make a few small changes to our tools (question 9).

**Status.** This is a plan only, written for GitHub task #769 ("MANAGER-PROD-MOVE-1", the task to
plan this move). Nothing in it has been run, and nothing of production was read with passwords
to write it: every fact comes from the repository's code and its operating guides
(`docs/campaign.html`, `docs/browser.html`). Nothing is done until you approve the plan, the
time of the switch and each step that cannot be undone. Sections 1 to 10 after the questions
are the operator's step-by-step checklist; you do not need them to answer.

## The owner's decisions

Nine questions. Each starts with a bold title, "Question N: ...", followed by the same five
headings in the same order: what it's about, your options (numbered, the recommended one first),
why we recommend it, what is hard to undo, and what happens if you don't decide. You can answer
all nine in one line (an example is at the end of this part).

Words used in the questions, each in one sentence:
- **Manager:** the rented server that runs the whole schedule behind the website: it starts
  the hourly tests, collects and stores their results and recalculates the website's numbers.
- **Server, droplet:** a computer we rent; DigitalOcean, the company we rent from, calls one
  a droplet.
- **London (`lon1`), Toronto (`tor1`):** where DigitalOcean keeps the server.
- **Production, staging:** production is the real system behind the website; staging is a
  separate copy for trials, which never touches the real data.
- **Operator:** the person or AI agent who types the commands of the move; in the examples,
  Ben.
- **Orchestrator:** the program on the manager that plans and runs every job on a timetable.
- **Test machines:** short-lived servers the manager starts every hour to measure the proxy
  networks; they are removed when the hour is done.
- **The database's allowed list:** the list of computers the database server accepts.
- **Soak:** a trial period after the switch, when the new server does the work and the old one
  stays ready to take over again.
- **Teardown:** removing a server for good: its secrets are erased and the server is deleted.
- **UTC:** the world's reference clock; in winter it equals London time, and Toronto is 5
  hours behind it.

---

**Question 1: Do you approve this plan as the way to move?**

**What it's about:** today the manager in London waits 78 ms for every answer from the
database in Canada; from Toronto it would wait about 8 ms. The plan builds a second manager in
Toronto beside London, switches over within one hour, keeps London ready for 3 days, then
removes London. Example: Ben prepares the Toronto server during the week; on the agreed day he
stops London and starts Toronto within 5 minutes, and Carla, reading the website at that
moment, sees no break.

**Your options:**
1. **Approve the plan** (recommended)
   - Ben prepares the Toronto server.
   - Nothing in production changes during the preparation.
   - Each later step that changes production still needs its own yes (question 7).
2. **Not now**
   - Nothing changes and nothing costs more.
   - Every question to the database keeps taking 78 ms instead of 8, so storing each hour's
     results stays slow.
3. **Ask for changes** (say which)
   - The plan is rewritten as you say and asked again.

**Why we recommend 1:** now, it removes the main cause of slow storing without touching the
website; later, every growth in testing (more networks, more countries) waits ten times less on
the database. Option 2 saves about $10 to $15 and the work, but keeps the slow link. Option 3
delays the move.

**Hard to undo:** approving alone changes nothing; every real change asks you again.

**If you don't decide:** nothing is done; London keeps running as today.

---

**Question 2: Which size should the new Toronto server be?**

**What it's about:** a server's size is how many processors and how much memory it has, and it
sets the price. London is DigitalOcean's size `s-8vcpu-16gb-amd`: 8 processors and 16 GB of
memory. Example: if Ben picked a bigger size "to be safe", our tool that starts the
orchestrator (the program that runs the timetable) would refuse it.

**Your options:**
1. **The same size as London** (recommended)
   - Costs about the same as London today, and our tools accept it as they are.
   - What we measured was slowed by the distance to the database, not by the processors, so
     more processors would not help. The test machines, not the manager, do the measuring.
   - Risk: one known limit stays exactly as in London today. The orchestrator may use up to
     12 GB of memory and one hourly calculation up to 8 GB, together more than the 16 GB.
     Before the switch Ben reads London's real figures once (only reading, with your yes for
     that reading); if any is close to its limit, you are asked this question again.
2. **Another size** (say which)
   - Our tools accept only this one size, so a developer must change them first, which delays
     the move.
   - A bigger size costs more every month; a smaller one may run out of memory.

**Why we recommend 1:** now, it needs no tool change and costs what London costs; later,
Toronto's measurements during the trial show whether a change is ever needed. Option 2 adds a
tool change and, if bigger, a monthly cost, for no measured gain.

**Hard to undo:** little. A size can be raised later with your approval, with the server off
for a few minutes; DigitalOcean does not let a server's disk shrink again after a raise.

**If you don't decide:** Ben uses the same size as London, the only one our tools accept
today.

---

**Question 3: A new production server, or the staging server already in Toronto?**

**What it's about:** there is already a manager in Toronto, built for staging (the separate
copy for trials) to run a 7-day test with a full week of generated work. Example: it might look
quicker for Ben to rename that staging server and make it production's.

**Your options:**
1. **A new production server** (recommended)
   - Ben makes it with our setup command (`campaign manager deploy --region tor1`) in
     production's settings, beside London.
   - Costs about $0.15 to $0.20 an hour.
   - Staging stays separate.
2. **The staging server**
   - Our tools refuse it. Each server's record belongs to one environment, and staging's names
     and labels (they start with `pi-camp-stg-`) cannot become production's.
   - It is four times the size needed (DigitalOcean's `c-32`, about $1 an hour).
   - Trials and real work would share one machine.

**Why we recommend 1:** now, it works with today's tools and keeps trials away from the real
system; later, staging can be rebuilt or removed freely without any risk to the website.
Option 2 needs tool changes, costs about five times more per hour and mixes trials with the
real system.

**Hard to undo:** nothing at this point. Until the switch, the new server can be removed with
nothing lost.

**If you don't decide:** Ben makes a new production server, the only way our tools allow.

---

**Question 4: What happens to the staging server in Toronto after its 7-day test?**

**What it's about:** the staging server in Toronto (built for GitHub task #768, "a staging
manager in Toronto") is running a 7-day test (GitHub task #718, "SIM-RUN-1": seven days of
generated data run through it and checked against the real data). It costs about $1 an hour:
about $24 a day, about $720 a month. Example: the test ends on a Friday; should the server keep
running in case Dana, a developer, needs it next month?

**Your options:**
1. **Remove it when its 7-day test has finished** (recommended)
   - It is removed completely (a full staging teardown), and its cost stops.
   - If staging needs a manager again later, a new one is built (about 15 minutes of setup).
2. **Keep it for later staging work**
   - It is ready at once for later staging work.
   - Costs about $720 a month while it waits.

**Why we recommend 1:** now, it saves about $24 a day; later, a staging manager can be built
again in minutes when one is needed. Option 2 keeps one ready, at about $720 a month.

**Hard to undo:** removal is final. The server and the files on it are deleted; a new one can be
built, but not the same one.

**If you don't decide:** it keeps running, at about $1 an hour, until you do.

---

**Question 5: Will you let the new Toronto server into the database?**

**What it's about:** the database server answers only computers on its allowed list. That list
is kept in two places: OVH's firewall (the gate in front of the database server) and `pg_hba`
(the database's own file of which address may log in, to which database, as which user). Only
you can change them. Example: Ben builds the Toronto server and sends you its address in chat;
you add it for the production database and for the user the manager logs in as (its "run-time
role"), then answer "done".

**Your options:**
1. **Yes, I add Toronto before the switch and remove London after London's removal**
   (recommended)
   - Before the switch you add Toronto's address. Ben then checks that Toronto can read the
     database, while London still runs and nothing has changed.
   - The address is sent in chat only, never on GitHub, where it would be public.
   - London's address stays on the list until London has been removed, so London can take back
     over if needed; then you take it off (closing the database to London, the last step in
     question 7).
2. **No**
   - Toronto cannot reach the database, so the move cannot happen; London keeps running.

**Why we recommend 1:** it is the only way the move can work, and doing it before the switch
means a mistake shows up while nothing in production has changed. Option 2 stops the move.

**Hard to undo:** nothing; an address can be taken off the list again at any time.

**If you don't decide:** the move waits at this step; London keeps running.

---

**Question 6: When should the switch happen?**

**What it's about:** the switch is the hour when London's orchestrator (the program that runs
the timetable) stops and Toronto's starts. Ben and you must both be reachable for 2 hours from
its start. Example: you choose Tuesday 14:00 UTC (UTC is the world's reference clock; in winter
it equals London time). Ben starts at 14:15, once that hour's calculation (it starts at 14:06)
has finished, and the switch itself is done before 14:50.

**Your options:**
1. **A weekday hour of your choice, outside 03:15 to 05:45 UTC** (recommended; name it, for
   example "Tuesday 14:00 UTC")
   - The switch happens between 15 and 50 minutes past that hour, after the hourly calculation
     and before the next hour's tests start at 56 minutes past.
   - It must not be between 03:15 and 05:45 UTC, because three daily jobs run then: deleting
     data older than its keeping period (03:17), the database's nightly tidy-up (03:47) and the
     daily download of DB-IP, the list that tells which country an internet address belongs to
     (05:23).
   - You are reachable for your yes (the switch, the first step in question 7) and for any
     question.
2. **Not yet**
   - Nothing happens; the preparation waits, with London running.

**Why we recommend 1:** now, a weekday means people are around if something needs fixing, the
minutes chosen avoid every hourly job, and 2 hours cover the switch and its checks (the first
calculation on Toronto, and the website's numbers moving). Option 2 keeps the slow link for
longer.

**Hard to undo:** the switch itself can be undone in about 30 minutes, until London is removed.

**If you don't decide:** no switch happens.

---

**Question 7: A separate yes from you before each step that changes production?**

**What it's about:** four steps change the real system. For each, Ben asks you at that moment,
and your yes is saved as a small answer file that our tool checks before it acts, the same way
as every yes you gave before. Example: Ben writes "Go for the switch? London stops and Toronto
starts at 14:15 UTC"; you answer "go for the switch".

The four steps, in this order:
- **The switch:** London's orchestrator (the program that runs the timetable) stops and
  Toronto's starts. It can be undone until London is removed.
- **The hourly test in Toronto:** turning on the timer that starts test machines every hour
  (the command `hourly-enable`), only if that timer is on in London today.
- **Removing London:** the London server's teardown after the trial period: its secrets are
  erased and the server is deleted. This cannot be undone. It also lifts, for this move only,
  the standing rule that production's manager is never removed.
- **Closing the database to London:** you take London's address off the database's allowed
  list (the list of computers the database accepts).

**Your options:**
1. **A separate yes before each of the four steps** (recommended)
   - You decide each step with what is known at that moment; for example, you see the trial's
     results before London is removed.
   - It takes four short messages.
2. **One yes now for all four**
   - Fewer messages.
   - Risk: London is removed without you seeing the trial's results first.

**Why we recommend 1:** now, it keeps you able to stop the move at every point; later, nothing
final happens without you having seen how Toronto ran. Option 2 gives up the chance to stop
before the step that cannot be undone.

**Hard to undo:** removing London. After it there is no way back to London; going back would be
a whole new move.

**If you don't decide:** no step runs without its yes, so the move stops before the switch.

---

**Question 8: How long should London stay ready after the switch (the soak)?**

**What it's about:** after the switch, Toronto does all the work while London stays switched
on with nothing running, ready to take back over in about 30 minutes. This trial period is
called the soak. Example: on day 2, Toronto's 05:23 download of DB-IP (the list of which country
an internet address belongs to) fails; with London still there, Ben can switch back while it is
fixed.

**Your options:**
1. **72 hours** (recommended)
   - Toronto runs three full days, so every daily job runs three times (the DB-IP download, the
     database's nightly tidy-up) and every browser test slot on each mini comes three times.
   - Both servers run for 3 days: about $10 to $15 in total.
2. **24 hours**
   - About $7 to $10 less.
   - Risk: each daily job is seen only once.
3. **Longer** (say how long)
   - Each extra day costs about $3.60 to $4.80 more.

**Why we recommend 1:** now, three repeats show that each daily job works every day, not just
once, for about $10 to $15; during them Ben compares Toronto with London (how long each hour
takes to store, how fast the database answers, how long the calculation takes, how much memory
is used). Option 2 saves a few dollars, but one bad day could pass unseen. Option 3 costs more
for little extra.

**Hard to undo:** nothing during the trial. Its end leads to removing London, the step in
question 7 that cannot be undone, which needs its own yes.

**If you don't decide:** Ben uses 72 hours, then asks for your yes to remove London; London is
not removed without your yes for it.

---

**Question 9: Tool changes first, or some steps by hand?**

**What it's about:** some steps of this move have no command in our tools yet. Section 8 of
this plan ("Missing tooling") lists the small tool changes developers can make before the
switch. Example: without a copy command, Ben must type long copy commands by hand during the
switch hour, while the clock runs.

**Your options:**
1. **Tool changes first** (recommended)
   - The switch waits for them (a few days of developer work).
   - Then each step is one checked command.
2. **By hand**
   - The switch can come sooner.
   - Two steps are done by hand: writing the database login file on Toronto, and copying
     London's files to Toronto.
   - Today's tool refuses to remove London (a step in question 7) while Toronto's test machines
     run, which is almost always.

**Why we recommend 1:** now, fewer hand-typed commands during the switch hour means fewer
chances of a mistake; later, the same commands serve any future move. Option 2 starts sooner
but is riskier, and London still cannot be removed until the tool is changed.

**Hard to undo:** nothing.

**If you don't decide:** the switch waits until you do.

---

**Your answer:** one line, for example
`Q1: 1, Q2: 1, Q3: 1, Q4: 1, Q5: 1, Q6: 1 Tuesday 14:00 UTC, Q7: 1, Q8: 1, Q9: 1`.

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
| **P5** | The daily template for tor1: `proxy-index campaign plan --account proxy-index --inventory <tor1> --images <production images inventory> --start <a future whole hour> --networks <the London template's networks> --regions <its regions> --concurrency <its value> --size <its worker size> --image <its snapshot> --output <dir>`, plus every other value the London template was planned with (`--slot-minutes`, `--slots`, `--grace-hours`, `--credentials-file`) so tor1's hours match London's, then `proxy-index campaign manager upload-spec --inventory <tor1> <dir>/<campaign id>`. Read the London template's values from its planned root on the Mac. Note the campaign id: C4 needs it (`orchestrator-enable` records `daily_template` only with `--apply`). Check: `upload-spec` accepts it (it refuses a spec for another manager, wheel or account). | 10 min |
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
| **C4** | `proxy-index campaign manager orchestrator-enable --inventory <tor1> --daily-template <campaign id from P5> --owner-go <go A> --apply`. Without `--daily-template` it refuses here, after London is already stopped: the P6 dry run records nothing, and C5's `hourly-enable` needs the recorded template too. Check: it prints `healthy` (it waits for `/health` over HTTPS), `<tor1>.orchestrator.crt` is written, `systemctl status proxy-index-orchestrator` on tor1, the admin job health. **C2 must have passed first**: two orchestrators must never run against one database (two schedulers would plan the same slots; nothing in the code stops it, risk R8). | 3 min |
| **C5** | Only when London's hourly test was on: `proxy-index campaign manager hourly-enable --inventory <tor1> --owner-go <go B> --apply`, **before `H−1:56`**. Check: `systemctl is-active proxy-index-daily.timer` is `active` on tor1; at `H−1:56` the unit `proxy-index-daily-<H>` starts on tor1. | 1 min |
| **C6** | Browser minis, one at a time: `proxy-index campaign manager browser-cert --inventory <tor1> --host browser-<n> --out <dir>`; install `browser-<n>.key`, `.crt` and `<tor1>.orchestrator.crt` in the mini's state folder (owner-only, 600) **beside** the London files, set `--orchestrator https://<tor1>:8444` and the three file flags in its launchd agent, restart it. Check: the host's next heartbeat and its next `/v1/results` upload on tor1. | 10 min per mini |
| **C7** | Pulse nodes, only when a fleet is armed: set `fleet.json` `orchestrator_url` to `<tor1>`'s `orchestrator.orchestrator_url` and `ca_file` to `<tor1>.orchestrator.crt`, then arm every node again (section 8 issue 4; without it, `pulse-fleet retire --name <node>` and `reconcile` for each node, which makes new droplets). Check: each node registers and uploads. | 15–30 min |
| **C8** | Verify: the census, pulse and browser plans run; the first uploads are stored; the next calculation at `H:06` succeeds and the website's numbers move; no `job_failed` alert. | 30 min |
| **C9** | When London's daily hour H−1 has finished (`daily.json` in its root, up to about `H:30`): copy London's spool hours **not yet uploaded** (H−1 and later) into tor1's spool: a folder tor1 lacks is copied whole; in a folder tor1 already has, only the `.tar.gz` files it lacks, **never London's `uploaded.json`** (London never uploaded these hours, so tor1 uploads them, as a further part when it has already sent its own). Also copy, by the P7 rule (each with its `uploaded.json`), the ended hours London uploaded after P7, up to H−2, so tor1's 30-day local copy (which `rebuild` reads) has no hole. Check: tor1's next `raw_upload` result lists the not-uploaded hours, no `archive_changed` error, and the SHA-256 lists of every copied hour match London's. | 15 min |
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
| R5 | **The spool.** An hour written on both managers can collide in Spaces (never overwritten, `archive_changed`), and an hour left only on London is missing from tor1's 30-day local copy. | C9: the merge rule for the hours London did not upload, and the P7 rule for the hours it uploaded after P7; check `raw_upload` on tor1. |
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

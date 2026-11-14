# Atlas rename: the operator's infrastructure checklist (RENAME-ATLAS-2)

The code now creates every cloud, server and Mac resource under an Atlas name and, for one
release, still recognises, lists and removes the ones created under the old names (they are
written only in `src/atlas/legacy_names.py`). This checklist is what the operator renames or
removes by hand, outside the repository. Nothing here was changed by the code change itself:
no server or cloud resource was touched.

Work top to bottom. Each step says how to check it. When every step is done, the next release
removes `src/atlas/legacy_names.py` and the code stops reading the old names.

## Names: old to new

| What | Old name | New name |
| --- | --- | --- |
| Spaces bucket (the raw archive) | `proxy-index-archive` | `atlas-archive` |
| Keychain entry of the Spaces secret | service `Proxy Index Spaces archive` | service `Atlas Spaces archive` |
| DigitalOcean project (in each team) | `Proxy Index` | `Atlas` |
| DigitalOcean account key (`--account`, inventories, specs) | `proxy-index` | `atlas` |
| Keychain entry of the DigitalOcean token | service `Proxy Index DigitalOcean`, account `team: Proxy Index` | service `Atlas DigitalOcean`, account `team: Atlas` |
| Campaign tags (worker, manager, builder, campaign, hour) | `pi-camp-worker`, `pi-camp-manager`, `pi-camp-builder`, `pi-camp-<sid>`, `pi-camp-<sid>-hNN` | `atlas-camp-…` the same way |
| Staging tags and names | `pi-camp-stg-…` | `atlas-camp-stg-…` |
| Pulse node tag and names | `pi-pulse-node`, `pi-pulse-<region>-<network>` | `atlas-pulse-node`, `atlas-pulse-<region>-<network>` |
| Worker droplet names | `pic-<sid>-hNN-<region>-<network>` | `atlas-<sid>-hNN-<region>-<network>` |
| Manager and builder droplet names | `pi-camp-manager-<date>-<hex>`, `pi-camp-builder-<date>-<hex>` | `atlas-camp-manager-…`, `atlas-camp-builder-…` |
| Worker snapshots | `pi-camp-worker-<version>-<sha>-<date>` | `atlas-camp-worker-…` |
| Firewalls | `pi-camp-worker-fw`, `pi-camp-manager-fw` (staging `pi-camp-stg-…-fw`) | `atlas-camp-worker-fw`, `atlas-camp-manager-fw` (staging `atlas-camp-stg-…-fw`) |
| SSH keys registered at DigitalOcean | `pi-camp-mac-<date>`, `pi-camp-worker-<date>` | `atlas-camp-mac-<date>`, `atlas-camp-worker-<date>` |
| Droplet manager folders | `/opt/proxy-index-campaign`, `/etc/proxy-index/database.toml` | `/opt/atlas-campaign`, `/etc/atlas/database.toml` |
| Droplet manager units | `proxy-index-orchestrator`, `proxy-index-daily`, `proxy-index-pulse`, `proxy-index-db-analyze`, `proxy-index-campaign-*@<cid>`, scope `proxy-index-calculate` | `atlas-orchestrator`, `atlas-daily`, `atlas-pulse`, `atlas-db-analyze`, `atlas-campaign-*@<cid>`, scope `atlas-calculate` |
| Worker image folders | `/opt/proxy-index-worker`, `/run/proxy-index-worker` | `/opt/atlas-worker`, `/run/atlas-worker` |
| Worker units | `pi-camp-round`, `pi-camp-sampler` | `atlas-camp-round`, `atlas-camp-sampler` |
| Pulse node units | `pi-pulse-node`, `pi-dbip-update` | `atlas-pulse-node`, `atlas-dbip-update` |
| Onboarding units | `proxy-index-onboarding-*` | `atlas-onboarding-*` |
| API files folder (default of `--files-dir`) | `/var/lib/proxy-index-api/files` | `/var/lib/atlas-api/files` |
| Mac SSH key | `~/.ssh/proxy-index-campaign` | `~/.ssh/atlas-campaign` |
| Mac watch agent and its logs | `com.proxy-index.campaign-watch`, `~/Library/Logs/proxy-index-campaign` | `io.atlas.campaign-watch`, `~/Library/Logs/atlas-campaign` |
| Mac minis' browser agent | `com.proxyindex.browser-node` | `io.atlas.browser-node` |
| Format ids in files kept outside the repository | `proxy-index-hour-formula`, `proxy-index-best-hours`, `proxy-index-raw-day`, `proxy-index-browser-raw`, `.proxy-index-release.json` | `atlas-hour-formula`, `atlas-best-hours`, `atlas-raw-day`, `atlas-browser-raw`, `.atlas-release.json` |

Kept on purpose (not renamed): the database and role names `proxy_reviews`,
`proxy_reviews_staging`, `proxy_api`, `proxy_admin`; every published API field; the Shifter
website's page paths (`/proxy-index/…`) and its repositories; applied schema and migration files.

## What the code still reads under the old names (one release)

- A droplet, firewall, SSH key or snapshot with a `pi-camp-`, `pi-pulse-` or `pic-` name or tag
  passes the same ownership checks as its Atlas name, so it can be listed, counted and removed.
  Teardown and `campaign limits` count both tags; the pulse fleet and the on-demand pulses list
  both pulse tags (an old node shows as `unknown`, never orphaned).
- Updating an old firewall renames it: the update writes the Atlas name and tags.
- The project may be named `Proxy Index` or `Atlas`, never both at once (the code finds exactly
  one project by either name and stops if it finds two).
- `--account proxy-index` and an inventory or spec that records `proxy-index` read as `atlas`;
  the next save writes `atlas`.
- The token is read from the `Atlas DigitalOcean` keychain entry, else from the old entry.
- Formula files, archive day files and browser manifests with the old format ids still load;
  a browser installed under the old release marker is still recognised.
- `atlas campaign guard install` and `remove` also unload and delete the old watch agent.
- `--mac-key` defaults to `~/.ssh/atlas-campaign`, or the old key while only that one exists.

Nothing is created under an old name, and no droplet is launched from an old `pi-camp-…`
snapshot (`OldImageError`).

## Checklist

### 1. Spaces bucket (DigitalOcean)

1. Create the bucket `atlas-archive` in the archive's region: private, then
   `atlas archive bucket --spaces-file <settings> --apply` enables versioning.
2. Create a new Spaces access key limited to `atlas-archive` only.
3. Put its secret in the keychain: service `Atlas Spaces archive`, account the new access key id.
4. In the `[spaces]` settings file: `bucket = "atlas-archive"` and the new `access_key_id`.
5. Move nothing: the archive has never run. If a `proxy-index-archive` bucket and its key exist,
   delete them once the check below passes.

Check: `atlas archive bucket --spaces-file <settings>` reports `atlas-archive`, private and
versioned.

### 2. DigitalOcean project, account and token

1. Rename the project `Proxy Index` to `Atlas` in the Atlas team, and in the Leftclick team if it
   has one. The project id stays the same, so recorded inventories stay valid.
2. Add the keychain entry service `Atlas DigitalOcean`, account `team: Atlas`, with the same
   token; then delete the old `Proxy Index DigitalOcean` entry.
3. Use `--account atlas` from now on.

Check: in the DigitalOcean console, each team's project list shows `Atlas` and no `Proxy Index`.
(`atlas campaign limits --account atlas` only shows that the token works: it prints the name it
was given, and its lookup also accepts the old name.)

### 3. Worker image and snapshots: rebuild before the start

The old worker image holds its tools under `/opt/proxy-index-worker`; the new code runs them under
`/opt/atlas-worker`. **Rebuild the worker image with the new names before the production start.**
The code refuses to launch any droplet (worker, probe, shot or pulse node) from a `pi-camp-…`
snapshot: the create stops with `OldImageError` and nothing is created.

1. Build it: `atlas campaign image build --account atlas …` (snapshot `atlas-camp-worker-…`).
2. Plan new campaigns with the new images inventory.
3. Delete the `pi-camp-worker-…` snapshots once no campaign uses them.

### 4. Droplet managers (London, staging)

They run under the old folders and units. They are not upgraded in place: this release refuses
every command (deploy, enable, disable, teardown, any SSH step) on a manager whose recorded name is
`pi-camp-manager-…`, and says to use the release that built it.

1. Build the new manager with the Atlas names (`atlas campaign manager deploy --account atlas …`,
   docs/plans/manager-prod-move.md, steps P1 onwards, or the manager on the Canada server, which
   already uses `atlas-*` user units).
2. Once the old manager is no longer needed, tear it down with the release that created it, then
   delete its `pi-camp-manager-fw` firewall, its `pi-camp-mac-…` and `pi-camp-worker-…` keys.

### 5. Pulse nodes

Retire the old nodes before the first reconcile with the new code: the reconcile creates the
Atlas-named replacements while the old ones still run, so until they are retired every pulse runs
twice.

1. Find the old nodes without creating anything: `atlas campaign pulse-fleet status --root <pulse
   root>` lists the recorded nodes (the old ones have `pi-pulse-…` names), and the DigitalOcean
   console's tag `pi-pulse-node` lists every old node, recorded or not.
2. Retire each recorded old node with
   `atlas campaign pulse-fleet retire --root <pulse root> --name <old name>`; remove an unrecorded
   one in the DigitalOcean console.
3. Only then run `atlas campaign pulse-fleet reconcile --root <pulse root>`: it creates the
   Atlas-named nodes, which run `atlas-pulse-node` and `atlas-dbip-update`.

Check: the console's tag `pi-pulse-node` lists no droplet, and the reconcile reports no `unknown`
node.

### 6. Tags and firewalls

DigitalOcean cannot rename a tag. Once no droplet or firewall carries them (steps 3 to 5), delete
the old tags: `pi-camp-worker`, `pi-camp-manager`, `pi-camp-builder`, every `pi-camp-<sid>` and
`pi-camp-<sid>-hNN`, the `pi-camp-stg-…` ones and `pi-pulse-node`. Delete the old `pi-camp-…-fw`
firewalls that no longer protect a droplet.

### 7. Canada server

Its manager and API already use the Atlas names (`atlas-orchestrator`, `atlas-api@blue/green`,
`~/atlas`). Check that no `proxy-index-*` unit is installed:
`systemctl --user list-unit-files 'proxy-index*'` and `systemctl list-unit-files 'proxy-index*'`
list nothing. The API's default files folder moved from `/var/lib/proxy-index-api/files` to
`/var/lib/atlas-api/files`: a unit that does not pass `--files-dir` (or `SITEAPI_FILES_DIR`) and
allows writing only under the old folder must pass the folder explicitly, or the upgraded API
writes outside its only writable path.

### 8. The owner's Mac

1. Rename the key: `mv ~/.ssh/proxy-index-campaign ~/.ssh/atlas-campaign` and the same for
   `.pub` (and `-staging` if present).
2. Reinstall the watch: `atlas campaign guard install …`; it removes the old agent. The old log
   folder `~/Library/Logs/proxy-index-campaign` can be deleted.

Check: `atlas campaign guard status` shows the agent loaded; `launchctl print
gui/$(id -u)/com.proxy-index.campaign-watch` finds nothing.

### 9. The Mac minis

1. `launchctl bootout gui/$(id -u)/com.proxyindex.browser-node` and delete
   `~/Library/LaunchAgents/com.proxyindex.browser-node.plist`.
2. Install `configs/launchd/io.atlas.browser-node.plist.example` as
   `~/Library/LaunchAgents/io.atlas.browser-node.plist` (replace the placeholders) and
   `launchctl bootstrap gui/$(id -u)` it.

The installed browser is recognised under its old release marker; nothing needs to be fetched.

# The browser minis' watchdog (MONITOR-1, #1094)

Each browser mini (`browser-1` to `browser-3`) runs its own small watchdog,
`scripts/mini-watch.sh`, as a launchd agent every 2 minutes. It posts to the same Slack channel
as the Canada server's alerts (`docs/operations/canada-server.md`, "Slack alerts"), under the
mini's logical name, so a mini whose home address changes still reports. The Canada server
watches the other side: a mini that has sent the manager no heartbeat for 30 minutes is posted as
`mini browser-<n>: check failed: offline` (its node list, "Wider checks").

## What it watches

| Check (Slack name) | Fails when |
| --- | --- |
| `browser service` | the browser node's launchd agent is not loaded, or loaded but not running |
| `queue` | the newest finished run in `<state>/logs/browser-node.jsonl` (or its rotated `.1`) is older than 2 queue gaps (`--gap-minutes`, 27 for 54 runs a day: 54 minutes), or there is none |
| `uploads` | the newest hourly line has more than 3 run bundles waiting to upload (`outbox_pending`) |
| `disk` | the file system of the node's state folder is more than 85% full |
| `load` | the 15-minute load is over 2 per CPU |

Messages: `<!everyone> Atlas mini browser-<n>: <check>: check failed: <what>`, once, then
`recovered: ok again` once when it is fine again; nothing in between. At 08:00 UTC it posts
`Atlas mini browser-<n>: all good` when nothing is failing. It only reads: it never restarts,
changes or deletes anything. Its own state is `~/.local/state/atlas-mini-watch/` (mode 700).

## Install (the operator, on each mini, after merge)

1. The Slack webhook, as a private file (the same URL as the Canada server's; never in the
   repository, never on the command line): `mkdir -p ~/.config/atlas && chmod 700 ~/.config/atlas`,
   then write the URL as the only line of `~/.config/atlas/slack-webhook` from the clipboard,
   private from the start: `(umask 077; pbpaste > ~/.config/atlas/slack-webhook)`. The script
   refuses any mode but 600.
2. `bash <checkout>/scripts/mini-watch.sh test --host browser-<n>` posts
   `Atlas mini browser-<n>: watchdog installed`.
3. Copy `configs/launchd/atlas-mini-watch.plist.example` to
   `~/Library/LaunchAgents/atlas.mini-watch.plist` and replace the placeholders: the checkout
   the browser node runs from, the node's state folder, the host name, the browser node's own
   launchd label (`launchctl list | grep browser-node` shows it) and the queue gap in whole
   minutes, rounded up.
4. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/atlas.mini-watch.plist`. Check:
   `launchctl print gui/$(id -u)/atlas.mini-watch` shows it, and
   `<state>/logs/mini-watch.err.log` stays empty.

To remove it: `launchctl bootout gui/$(id -u)/atlas.mini-watch` and delete the plist.

## Silence

For planned work on a mini: `bash <checkout>/scripts/mini-watch.sh silence <minutes>` holds its
messages for that long (`silence 0` ends it). A check still failing afterwards is posted then.

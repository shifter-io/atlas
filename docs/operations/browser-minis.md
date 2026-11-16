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
| `queue` | in `<state>/logs/browser-node.jsonl` (or its rotated `.1`), no run started or finished for 2 queue gaps (`--gap-minutes`, 27 for 54 runs a day: 54 minutes), or none finished for 90 minutes (at least 2 gaps), or there is no finished run at all. A long run alone is not late: a linkedin run takes 40 to 70 minutes and finishes nothing meanwhile, but the other runs still start on rhythm (QUEUE-LATE-1, #1257) |
| `uploads` | the newest hourly line has more than 3 run bundles waiting to upload (`outbox_pending`) |
| `disk` | the file system of the node's state folder is more than 85% full |
| `load` | the 15-minute load is over 2 per CPU |

Worst-case detection of a stalled queue (the node hung, no start and no finish): 2 gaps plus the
2-minute watchdog interval, 56 minutes at a 27-minute gap; of a run that never ends while others
still start: 90 minutes plus 2. A node that is not running alerts `browser service` at the next
check (2 minutes); the Canada server's `offline` check catches a mini with no heartbeat in 30.

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
   launchd label and the queue gap in whole minutes, rounded up. The label is the `Label` in the
   node's plist: on the minis the node is a system daemon,
   `/Library/LaunchDaemons/io.atlas.browser-node.plist`, so `io.atlas.browser-node`. The watchdog
   reads both domains: `launchctl print gui/<uid>/<label>` (a user agent), then
   `launchctl print system/<label>` (a system daemon; it works without sudo).
4. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/atlas.mini-watch.plist`. Check:
   `launchctl print gui/$(id -u)/atlas.mini-watch` shows it, and
   `<state>/logs/mini-watch.err.log` stays empty.

To remove it: `launchctl bootout gui/$(id -u)/atlas.mini-watch` and delete the plist.

## Silence

For planned work on a mini: `bash <checkout>/scripts/mini-watch.sh silence <minutes>` holds its
messages for that long (`silence 0` ends it). A check still failing afterwards is posted then.

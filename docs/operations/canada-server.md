# Atlas on the Canada server

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

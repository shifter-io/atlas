#!/usr/bin/env bash
# Slack alerts for the Atlas services of one environment on the Canada server (#1007), with the
# wider checks of server-alerts/checks.sh (MONITOR-1, #1094).
#
#   server-alerts.sh install <production|staging>  units, OnFailure drop-ins, then a test post
#   server-alerts.sh test <production|staging>     the install message only
#   server-alerts.sh check-all                     every installed environment (the timer)
#   server-alerts.sh check <env>                   one environment
#   server-alerts.sh failed <unit>                 OnFailure= of a unit (atlas-alert@.service)
#   server-alerts.sh silence <env> <minutes>       no messages for that long (0 ends it)
#
# Runs as the normal user (systemctl --user), never as root. The webhook URL is read from
# ~/.config/atlas/slack-webhook (mode 600) and handed to curl on stdin (curl -K -), so it
# never appears in argv, ps, logs or output. State lives in ~/atlas/<env>/alerts-state/.
set -euo pipefail

ATLAS_HOME=$HOME/atlas
CONFIG=$HOME/.config/atlas
WEBHOOK=$CONFIG/slack-webhook
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
TEMPLATES=$(cd "$(dirname "$0")" && pwd)/server-alerts
LIMIT=600          # the same environment, service and kind at most once per 10 minutes
DISK_MAX=85        # percent of the home file system
TICK=120           # the timer's interval, the first journal window
ENVS=(production staging)
# shellcheck source=scripts/server-alerts/slack.sh
. "$TEMPLATES/slack.sh"
# shellcheck source=scripts/server-alerts/checks.sh
. "$TEMPLATES/checks.sh"

die() { echo "server-alerts: $*" >&2; exit 1; }
now() { echo "${ATLAS_ALERTS_NOW:-$(date +%s)}"; }

# Names, ports and folders of one environment (the shared layout table).
use_env() {
  ENV=$1
  # shellcheck disable=SC2034 # PUBLIC is read by checks.sh
  case $ENV in
    production) PREFIX=atlas OFFSET=0 PUBLIC=atlas.shifter.io ;;
    staging) PREFIX=atlas-staging OFFSET=1000 PUBLIC=staging.atlas.shifter.io ;;
    *) die "unknown environment '$ENV' (production or staging)" ;;
  esac
  STATE=$ATLAS_HOME/$ENV/alerts-state
  HOST=$(hostname -s)
  TITLE="Atlas $ENV on $HOST"
}

# Is this unit one of the environment's services? atlas-caddy serves both environments.
in_env() {
  case $1 in
    atlas-alert@*|atlas-alerts.*) return 1 ;;
    atlas-caddy.service) return 0 ;;
    atlas-staging-*) [ "$ENV" = staging ] ;;
    atlas-*) [ "$ENV" = production ] ;;
    *) return 1 ;;
  esac
}

# The last 15 journal lines of a unit, as a code block after its alert.
alert_details() {
  local lines
  lines=$(journalctl --user -u "$1" -n 15 --no-pager -o cat 2>/dev/null \
    | iconv -c -f utf-8 -t utf-8 | tr -d '`' || true)
  # shellcheck disable=SC2016 # the backticks are Slack's code block
  printf '\n```\n%s\n```' "${lines:-(no journal lines)}"
}

prop() { sed -n "s/^$1=//p" <<< "$2"; }

check_unit() {
  local unit=$1 props active filestate restarts result last
  props=$(systemctl --user show "$unit" -p ActiveState -p UnitFileState -p NRestarts -p Result)
  active=$(prop ActiveState "$props") filestate=$(prop UnitFileState "$props")
  restarts=$(prop NRestarts "$props") result=$(prop Result "$props")
  case "$filestate/$active" in
    enabled*/*|*/active|*/activating|*/reloading|*/deactivating) ;;
    disabled/*|masked*/*) up "$unit" "switched off"; return 0 ;;  # on purpose (the manager until GO)
    */failed) ;;  # a static unit, such as a timer's oneshot
    *) return 0 ;;
  esac
  last=$(cat "$STATE/$unit.nrestarts" 2>/dev/null || echo "${restarts:-0}")
  echo "${restarts:-0}" > "$STATE/$unit.nrestarts"
  if (( ${restarts:-0} > last )); then
    ROUTE_TYPE=once alert "$unit" restarted "restarted $(( restarts - last )) time(s) since the last check" "$unit"
  fi
  if [ "$active" = failed ]; then
    alert "$unit" could-not-restart "the unit is failed (result: $result)" "$unit"
    down "$unit"
  elif [ "$active" = active ] || [ "$result" = success ]; then
    up "$unit" "active again" "$unit"
  fi
}

check_units() {
  local unit
  while read -r unit _; do
    in_env "$unit" || continue
    case $unit in
      "$PREFIX"-hourly-[0-9]*) check_hourly "$unit" ;;
      *) check_unit "$unit" ;;
    esac
  done < <(systemctl --user list-units --all --plain --no-legend --no-pager 'atlas*')
}

# A failed transient hourly test unit, reported once.
check_hourly() {
  [ "$(systemctl --user is-failed "$1" 2>/dev/null || true)" = failed ] || return 0
  [ -f "$STATE/$1.reported" ] && return 0
  event "$1" check-failed "the hourly test failed" "$1"
  if [ "$POSTED" = 1 ]; then touch "$STATE/$1.reported"; fi
}

api_ok() {
  local body status
  body=$(curl -fsS --max-time 5 "http://127.0.0.1:$1/healthz") || { echo "no answer on port $1"; return 1; }
  status=$("${ATLAS_PYTHON:-python3}" -c 'import json, sys; print(json.load(sys.stdin).get("status", ""))' \
    <<< "$body" 2> /dev/null || true)
  [ "$status" = ok ] || { echo "/healthz status is ${status:-unreadable}"; return 1; }
}

tls_ok() {
  local out
  out=$(timeout 5 openssl s_client -brief -connect "127.0.0.1:$1" < /dev/null 2>&1 || true)
  grep -q 'CONNECTION ESTABLISHED' <<< "$out" || { echo "port $1 does not accept TLS"; return 1; }
}

# psql with the environment's database file (the API's read-only login unless SQL_DB names
# another), read only, 5 s; PGPASSWORD never reaches argv.
sql() {
  "${ATLAS_PYTHON:-python3}" - "${SQL_DB:-$CONFIG/$ENV/database-api.toml}" "$1" <<'PY'
import os, sys, tomllib
with open(sys.argv[1], "rb") as f:
    pg = tomllib.load(f)["postgres"]
env = dict(os.environ, PGHOST=str(pg["host"]), PGPORT=str(pg["port"]), PGDATABASE=str(pg["dbname"]),
           PGUSER=str(pg["user"]), PGPASSWORD=str(pg.get("password", "")), PGCONNECT_TIMEOUT="5",
           PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=5000")
os.execvpe("timeout", ["timeout", "5", "psql", "-X", "-A", "-t", "-q", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sys.argv[2]], env)
PY
}

db_ok() { [ "$(sql 'SELECT 1')" = 1 ] || { echo "SELECT 1 failed"; return 1; }; }

disk_ok() {
  local pct
  pct=$(df -P "$HOME" | awk 'NR == 2 { sub("%", "", $5); print $5 }')
  (( ${pct:-100} <= DISK_MAX )) || { echo "the home file system is ${pct}% full"; return 1; }
}

check_health() {
  local colour port=$((8090 + OFFSET)) enabled=0 serving=0
  for colour in blue green; do  # the serving colour is the enabled one; a starting one is not
    systemctl --user is-enabled --quiet "$PREFIX-api@$colour.service" || { port=$((port + 1)); continue; }
    enabled=1
    if systemctl --user is-active --quiet "$PREFIX-api@$colour.service"; then
      serving=1
      health "$PREFIX-api@$colour /healthz" api_ok "$port"
    elif [ "$(systemctl --user is-failed "$PREFIX-api@$colour.service" 2> /dev/null || true)" = failed ]; then
      serving=1  # reported as "could not restart" by check_unit
    fi
    port=$((port + 1))
  done
  if [ "$enabled" = 1 ]; then
    if [ "$serving" = 1 ]; then up "$PREFIX-api serving"; else health "$PREFIX-api serving" say "no API colour is running"; fi
    health "Caddy port $((8080 + OFFSET)) /healthz" api_ok $((8080 + OFFSET))
  fi
  if systemctl --user is-active --quiet "$PREFIX-orchestrator.service"; then
    for port in $((8443 + OFFSET)) $((8444 + OFFSET)); do health "manager port $port" tls_ok "$port"; done
  fi
  DB_OK=1  # the database checks of checks.sh run only when SELECT 1 does
  if [ -f "$CONFIG/$ENV/database-api.toml" ]; then health database db_ok; DB_OK=$OK; fi
  health disk disk_ok
}

# New major or critical rows of the manager's alerts table, one message each (no journal lines).
# The table is the manager's: it is read with the manager's login (database.toml); the API's
# login may not read it.
check_alerts() {
  local seen=$STATE/alert-id last rows id kind message SQL_DB=$CONFIG/$ENV/database.toml
  [ -f "$SQL_DB" ] || return 0
  last=$(cat "$seen" 2>/dev/null || true)
  if ! [[ $last =~ ^[0-9]+$ ]]; then  # the first check starts after the newest row
    if last=$(sql 'SELECT coalesce(max(alert_id), 0) FROM alerts'); then echo "$last" > "$seen"; fi
    return 0
  fi
  if ! rows=$(sql "SELECT alert_id, kind, translate(message, E'\\n\\t', '  ') FROM alerts
      WHERE alert_id > $last AND severity IN ('major', 'critical') ORDER BY alert_id LIMIT 20"); then
    alert "manager alerts" check-failed "the alerts table could not be read"
    return 0
  fi
  route ok "manager alerts"
  while IFS=$'\t' read -r id kind message; do
    [ -n "$id" ] || continue
    # Name, server and the row's short message only: never the orchestrator's journal, whose
    # lines are its access log (PULSE-NODE-RETIRE-1).
    ROUTE_TYPE=skip alert "manager alert $id ($kind)" check-failed "$message"  # route_alerts
    if [ "$POSTED" = 1 ]; then echo "$id" > "$seen"; else break; fi
  done <<< "$rows"
}

# The open major and critical rows of the alerts table, for the router (ALERT-POLICY-1): a row
# is open until it is acknowledged; a failed job's row also until the job ran ok after it.
route_alerts() {
  local rows key kind level since message SQL_DB=$CONFIG/$ENV/database.toml
  if [ ! -f "$SQL_DB" ] || ! manager_on; then return 0; fi  # nothing new; the open ones wait
  rows=$(sql "SELECT CASE WHEN dedupe_key LIKE kind || '%' THEN dedupe_key
      ELSE kind || ':' || coalesce(dedupe_key, '') END, kind,
      CASE severity WHEN 'critical' THEN 3 ELSE 2 END, extract(epoch FROM first_at)::bigint,
      translate(message, E'\\n\\t', '  ') FROM alerts a
      WHERE acknowledged_at IS NULL AND severity IN ('major', 'critical') AND NOT (kind = 'job_failed'
        AND EXISTS (SELECT 1 FROM job_runs r WHERE r.job = a.data->>'job' AND r.status = 'ok'
          AND r.started_at > a.last_at)) ORDER BY alert_id LIMIT 500") || return 0
  while IFS=$'\t' read -r key kind level since message; do
    if [ -n "$key" ]; then route open "manager:$key" "$kind" "$message" "$since" "$level"; fi
  done <<< "$rows"
  route sweep "manager:"
}

check_create_failed() {
  local mark=$STATE/journal-until since end line
  end=$(now)
  since=$(cat "$mark" 2>/dev/null || echo $((end - TICK)))
  line=$(journalctl --user -u "$PREFIX-orchestrator.service" --since "@$since" --until "@$end" \
    --no-pager -o cat 2>/dev/null | grep -m 1 create_failed || true)
  [ -z "$line" ] || event "test machines" check-failed "$line" "$PREFIX-orchestrator.service"
  if [ -z "$line" ] || [ "$POSTED" = 1 ]; then echo "$end" > "$mark"; fi  # else retried next check
}

lock() {
  [ -d "$STATE" ] || die "alerts are not installed for $ENV"
  exec 9> "$STATE/.lock"
  flock -w 60 9 || die "another check of $ENV still runs"
}

check() {
  use_env "$1"
  lock
  FAILED=0
  route_begin
  check_units
  check_health
  check_alerts
  route_alerts
  check_create_failed
  check_more
  route_flush
  return "$FAILED"
}

# Writes a file only when its content differs; CHANGED records a write.
put() {
  [ -f "$1" ] && [ "$(cat "$1")" = "$2" ] && return 0
  mkdir -p "$(dirname "$1")"
  printf '%s\n' "$2" > "$1"
  CHANGED=1
}

# The environment's enabled or active units, template instances as their template.
watched_units() {
  { systemctl --user list-unit-files --state=enabled --no-legend --no-pager 'atlas*'
    systemctl --user list-units --state=active --plain --no-legend --no-pager 'atlas*'
  } | while read -r unit _; do
    in_env "$unit" || continue
    case $unit in "$PREFIX"-hourly-[0-9]*|*.scope) continue ;; esac
    case $unit in *@?*.*) unit=${unit%%@*}@.${unit##*.} ;; esac
    echo "$unit"
  done | sort -u
}

install_alerts() {
  use_env "$1"
  [ "$(id -u)" != 0 ] || die "run as the normal user, never as root"
  webhook_url > /dev/null
  CHANGED=0
  mkdir -p "$STATE"
  chmod 700 "$STATE"
  mkdir -p "$ATLAS_HOME/bin"
  cmp -s "$0" "$ATLAS_HOME/bin/atlas-alerts" || { install -m 755 "$0" "$ATLAS_HOME/bin/atlas-alerts"; CHANGED=1; }
  mkdir -p "$ATLAS_HOME/bin/server-alerts"
  for name in slack.sh checks.sh router.py policy.txt; do
    cmp -s "$TEMPLATES/$name" "$ATLAS_HOME/bin/server-alerts/$name" \
      || install -m 644 "$TEMPLATES/$name" "$ATLAS_HOME/bin/server-alerts/$name"
  done
  local name unit
  for name in atlas-alerts.service atlas-alerts.timer atlas-alert@.service; do
    put "$UNIT_DIR/$name" "$(cat "$TEMPLATES/$name")"
  done
  while read -r unit; do
    put "$UNIT_DIR/$unit.d/atlas-alert.conf" $'[Unit]\nOnFailure=atlas-alert@%n.service'
  done < <(watched_units)
  if [ "$CHANGED" = 1 ]; then systemctl --user daemon-reload; fi
  systemctl --user enable --now --quiet atlas-alerts.timer
  router_live || post "Atlas $ENV: alerts installed on $HOST"  # live: nothing to act on
}

main() {
  case ${1:-} in
    install) install_alerts "${2:-}" ;;
    test) use_env "${2:-}"; post "Atlas $ENV: alerts installed on $HOST" ;;
    silence)
      use_env "${2:-}"
      [[ ${3:-} =~ ^[0-9]+$ ]] || die "usage: server-alerts.sh silence <production|staging> <minutes>"
      lock
      echo $(( $(now) + 10#$3 * 60 )) > "$STATE/silence"
      echo "Atlas $ENV alerts silenced for $3 minute(s)" ;;
    check) check "${2:-}" ;;
    check-all)
      local env status=0
      for env in "${ENVS[@]}"; do
        [ -d "$ATLAS_HOME/$env/alerts-state" ] || continue
        ( check "$env" ) || status=1
      done
      return "$status" ;;
    failed)
      local unit=${2:?unit} env=production
      case $unit in atlas-staging-*) env=staging ;; esac
      use_env "$env"
      [ -d "$STATE" ] || exit 0
      lock
      FAILED=0
      route_begin
      alert "$unit" could-not-restart "the unit failed" "$unit"
      down "$unit"
      route_flush
      return "$FAILED" ;;
    *) die "usage: server-alerts.sh install|test|check <production|staging> | check-all | failed <unit> | silence <env> <minutes>" ;;
  esac
}

main "$@"

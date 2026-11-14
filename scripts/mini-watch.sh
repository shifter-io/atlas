#!/bin/bash
# The browser mini's own watchdog (MONITOR-1, #1094). A launchd agent runs it every 2 minutes
# (configs/launchd/atlas-mini-watch.plist.example) and it posts to Slack itself, under the mini's
# logical name, so a mini whose home address changes still reports. Written for macOS bash 3.2.
#
#   mini-watch.sh check --host browser-<n> --state <dir> --label <label> [--gap-minutes 27]
#   mini-watch.sh test --host browser-<n>          one test post
#   mini-watch.sh silence <minutes>                no messages for that long (0 ends it)
#
# --state is the browser node's state folder (logs/browser-node.jsonl), --label the launchd label
# of the browser node's agent, --gap-minutes the host's queue gap (docs/browser.html). The webhook
# URL is read from ~/.config/atlas/slack-webhook (mode 600, never in the repository); it reaches
# curl on stdin only. The watchdog's own state is ~/.local/state/atlas-mini-watch/.
set -euo pipefail

WEBHOOK=$HOME/.config/atlas/slack-webhook
STATE=${ATLAS_WATCH_STATE:-$HOME/.local/state/atlas-mini-watch}
LIMIT=600        # the same check at most once per 10 minutes (only when it flaps)
DISK_MAX=85      # percent of the state folder's file system
LOAD_MAX=2       # the 15-minute load per CPU
PENDING_MAX=3    # bundles waiting to upload at the last hour line
GAP=27           # minutes between two runs of the host's queue; late after 2 gaps
# shellcheck source=scripts/server-alerts/slack.sh
. "$(cd "$(dirname "$0")" && pwd)/server-alerts/slack.sh"

die() { echo "mini-watch: $*" >&2; exit 1; }
now() { echo "${ATLAS_ALERTS_NOW:-$(date +%s)}"; }

# An ISO 8601 UTC time ("2026-11-14T15:04:05.123+00:00") as a unix time, to the second.
epoch() {
  local t=${1:0:19}
  [[ $t =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$ ]] || return 1
  date -u -d "$t" +%s 2> /dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%S' "$t" +%s
}

# The newest line of a kind ("run", "hour") in the node's log, its last rotated file included.
newest() {
  { tail -c 2000000 "$LOG.1" 2> /dev/null || true; tail -c 2000000 "$LOG" 2> /dev/null || true; } \
    | grep "\"kind\": \"$1\"" | tail -n 1 || true
}

service_ok() {
  local out
  out=$(launchctl print "gui/$(id -u)/$LABEL" 2>&1) || { echo "$LABEL is not loaded"; return 1; }
  grep -q 'state = running' <<< "$out" || { echo "$LABEL is loaded but not running"; return 1; }
}

queue_ok() {
  local line at age
  line=$(newest run)
  [ -n "$line" ] || { echo "no finished run in the node's log"; return 1; }
  at=$(sed -n 's/.*"at": "\([^"]*\)".*/\1/p' <<< "$line")
  at=$(epoch "$at") || { echo "the last run line has no readable time"; return 1; }
  age=$(( $(now) - at ))
  (( age <= 2 * GAP * 60 )) \
    || { echo "late: the last run finished $(( age / 60 )) min ago (2 gaps: $(( 2 * GAP )) min)"; return 1; }
}

outbox_ok() {
  local pending
  pending=$(newest hour | sed -n 's/.*"outbox_pending": \([0-9]*\).*/\1/p')
  (( ${pending:-0} <= PENDING_MAX )) || { echo "$pending run bundles are waiting to upload"; return 1; }
}

disk_ok() {
  local pct
  pct=$(df -Pk "$NODE_STATE" | awk 'NR == 2 { sub("%", "", $5); print $5 }')
  (( ${pct:-100} <= DISK_MAX )) || { echo "the disk is ${pct}% full"; return 1; }
}

load_ok() {
  local load cpus
  load=$(sysctl -n vm.loadavg | awk '{ print $4 }')  # "{ 1m 5m 15m }"
  cpus=$(sysctl -n hw.ncpu)
  awk -v l="$load" -v c="$cpus" -v m="$LOAD_MAX" 'BEGIN { exit !(l <= c * m) }' \
    || { echo "the 15-minute load is $load on $cpus CPUs"; return 1; }
}

check() {
  if [ -z "${LABEL:-}" ] || [ -z "${NODE_STATE:-}" ]; then die "check needs --state and --label"; fi
  LOG=$NODE_STATE/logs/browser-node.jsonl
  FAILED=0
  health "browser service" service_ok
  health "queue" queue_ok
  health "uploads" outbox_ok
  health disk disk_ok
  health load load_ok
  heartbeat
  return "$FAILED"
}

main() {
  local command=${1:-}
  [ $# -eq 0 ] || shift
  while [ $# -gt 0 ]; do
    case $1 in
      --host) MINI=$2 ;;
      --state) NODE_STATE=$2 ;;
      --label) LABEL=$2 ;;
      --gap-minutes) GAP=$2 ;;
      *) break ;;
    esac
    shift 2
  done
  mkdir -p "$STATE"
  chmod 700 "$STATE"
  case $command in
    check|test)
      [[ ${MINI:-} =~ ^browser-[0-9]+$ ]] || die "--host must be browser-<n>"
      [[ $GAP =~ ^[1-9][0-9]*$ ]] || die "--gap-minutes must be a whole number of minutes"
      TITLE="Atlas mini $MINI"
      if [ "$command" = test ]; then post "$TITLE: watchdog installed"; else check; fi ;;
    silence)
      [[ ${1:-} =~ ^[0-9]+$ ]] || die "usage: mini-watch.sh silence <minutes>"
      echo $(( $(now) + $1 * 60 )) > "$STATE/silence"
      echo "mini alerts silenced for $1 minute(s)" ;;
    *) die "usage: mini-watch.sh check --host browser-<n> --state <dir> --label <label> [--gap-minutes N] | test --host browser-<n> | silence <minutes>" ;;
  esac
}

main "$@"

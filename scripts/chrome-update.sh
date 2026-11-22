#!/bin/bash
# Updates a browser mini's Google Chrome to the current Stable, by hand, between runs
# (CHROME-CURRENT-1; the operator's procedure is docs/browser.html, "Keeping Chrome current").
# Written for macOS bash 3.2.
#
#   chrome-update.sh [--state <dir>] [--app <path>] [--wait <s>] [--run-minutes <m>] [--dry-run]
#   chrome-update.sh --restore [--state <dir>] [--app <path>] [--wait <s>] [--run-minutes <m>]
#   chrome-update.sh --status [--state <dir>] [--app <path>]
#
# Update: download Google's current Stable installer, check Google's signature on the app in it,
# and install it only when it is newer than the installed app (or there is none). The app is
# replaced only at a quiet moment: no process of it runs and, with --state (the browser node's
# state folder), no run of the node is open (a "start" line of its log in the last --run-minutes,
# default 180, without its "run" line). It waits up to --wait seconds (default 3600) for one,
# else changes nothing. The new app is copied next to the installed one first; the swap is then
# two renames on the same disk. The replaced app is kept as .atlas-chrome-previous.app beside it.
# Restore: swap that previous app back in, at a quiet moment too (the replaced one is kept in
# its place, so a second --restore undoes the first). Status: the installed and previous
# versions and the open runs, changing nothing.
# Exit: 0 done (or nothing to do), 3 no quiet moment within --wait (nothing changed), 1 an error
# (the installed app unchanged).
set -euo pipefail

APP="/Applications/Google Chrome.app"
STATE=""
MODE=update
WAIT=3600
RUN_MINUTES=180
POLL=2
URL=${ATLAS_CHROME_DMG_URL:-https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg}
TEAM=EQHXZ8M8AV # Google LLC's Apple developer team

say() { echo "chrome-update: $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { say "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case $1 in
    --app) APP=${2:?--app needs a path}; shift 2 ;;
    --state) STATE=${2:?--state needs a folder}; shift 2 ;;
    --wait) WAIT=${2:?--wait needs seconds}; shift 2 ;;
    --run-minutes) RUN_MINUTES=${2:?--run-minutes needs minutes}; shift 2 ;;
    --dry-run) MODE=dry; shift ;;
    --restore) MODE=restore; shift ;;
    --status) MODE=status; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ $WAIT =~ ^[0-9]+$ ]] || die "--wait must be whole seconds"
[[ $RUN_MINUTES =~ ^[0-9]+$ ]] || die "--run-minutes must be whole minutes"
[ -z "$STATE" ] || [ -d "$STATE/logs" ] || die "no browser node log folder in $STATE"

DIR=$(dirname "$APP")
STAGE="$DIR/.atlas-chrome-new.app"
PREV="$DIR/.atlas-chrome-previous.app"

# The version in an app's Info.plist ("158.0.8127.37"); nothing when there is no app.
version_of() {
  [ -f "$1/Contents/Info.plist" ] || return 0
  plutil -extract CFBundleShortVersionString raw -o - "$1/Contents/Info.plist"
}

# Whether dotted version $1 is newer than $2 (no installed version: "").
newer() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); split(b, y, ".")
    for (i = 1; i <= n; i++) { if (x[i] + 0 != y[i] + 0) exit !(x[i] + 0 > y[i] + 0) }
    exit 1 }'
}

# The UTC time $1 minutes ago, as the node log writes times (GNU date, else BSD date).
iso_ago() {
  local t=$(($(date +%s) - $1 * 60))
  date -u -d "@$t" +%Y-%m-%dT%H:%M:%S 2> /dev/null || date -u -r "$t" +%Y-%m-%dT%H:%M:%S
}

# The node's runs (batch:network; a batch is shared by the host's networks) started in the last
# RUN_MINUTES whose "run" line is not written.
open_runs() {
  [ -n "$STATE" ] || return 0
  local log="$STATE/logs/browser-node.jsonl"
  { tail -c 2000000 "$log.1" 2> /dev/null || true; tail -c 2000000 "$log" 2> /dev/null || true; } \
    | awk -v since="$(iso_ago "$RUN_MINUTES")" '
      /"kind": "(start|run)"/ && match($0, /"batch": [0-9]+/) {
        b = substr($0, RSTART + 9, RLENGTH - 9)
        if (match($0, /"network": "[^"]*"/)) b = b ":" substr($0, RSTART + 12, RLENGTH - 13)
        if ($0 ~ /"kind": "run"/) { done[b] = 1; next }
        if (match($0, /"at": "[^"]*"/) && substr($0, RSTART + 7, RLENGTH - 8) >= since) started[b] = 1
      }
      END { for (b in started) if (!(b in done)) print b }' | sort -n | paste -sd ' ' -
}

chromes() { { pgrep -f "$APP/Contents/" || true; } | wc -l | tr -d ' '; }

# Wait for a quiet moment: no Chrome of the app and no open run; exit 3 after WAIT seconds.
quiet() {
  local waited=0 runs count
  while :; do
    runs=$(open_runs) count=$(chromes)
    if [ -z "$runs" ] && [ "$count" = 0 ]; then return 0; fi
    if [ "$waited" -ge "$WAIT" ]; then
      say "busy for ${WAIT} s (open runs: ${runs:-none}; Chrome processes: $count); nothing changed"
      exit 3
    fi
    if [ "$waited" = 0 ]; then say "waiting for a quiet moment (open runs: ${runs:-none})"; fi
    sleep "$POLL"
    waited=$((waited + POLL))
  done
}

# Put STAGE in place of the installed app, which becomes the previous one.
swap() {
  rm -rf "$PREV"
  if [ -e "$APP" ]; then mv "$APP" "$PREV" || die "the installed app could not be moved aside"; fi
  if ! mv "$STAGE" "$APP"; then
    if [ -e "$PREV" ]; then mv "$PREV" "$APP"; fi
    if [ "$MODE" = restore ]; then mv "$STAGE" "$PREV" || true; fi # keep the version to restore
    die "the new app could not be put in place; the installed one is back"
  fi
}

installed=$(version_of "$APP")
if [ "$MODE" = status ]; then
  runs=$(open_runs)
  say "installed: Chrome ${installed:-none}; previous: Chrome $(version_of "$PREV" | grep . || echo none)"
  say "open runs: ${runs:-none}; Chrome processes: $(chromes)"
  exit 0
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/atlas-chrome.XXXXXX")
MOUNT="$WORK/mnt"
cleanup() {
  if [ -d "$MOUNT" ]; then hdiutil detach -quiet "$MOUNT" || true; fi
  rm -rf "$WORK" "$STAGE"
}
trap cleanup EXIT

if [ "$MODE" = restore ]; then
  previous=$(version_of "$PREV")
  [ -n "$previous" ] || die "no previous Chrome kept beside $APP"
  quiet
  mv "$PREV" "$STAGE" || die "the previous app could not be staged"
  swap
  say "restored Chrome ${previous} (Chrome ${installed:-none} kept as the previous one)"
  exit 0
fi

curl -fsSL --retry 3 --max-time 600 -o "$WORK/chrome.dmg" "$URL" || die "download failed"
mkdir "$MOUNT"
hdiutil attach -quiet -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$WORK/chrome.dmg" \
  || die "the installer did not mount"
NEW="$MOUNT/Google Chrome.app"
codesign --verify --deep --strict "$NEW" || die "the installer's app fails its signature check"
team=$(codesign -dv "$NEW" 2>&1 | sed -n 's/^TeamIdentifier=//p')
[ "$team" = "$TEAM" ] || die "the app is not signed by Google (team ${team:-none})"
offered=$(version_of "$NEW")
[ -n "$offered" ] || die "the installer's app has no version"

if ! newer "$offered" "$installed"; then
  say "current: Chrome ${installed} (Stable offers ${offered})"
  exit 0
fi
if [ "$MODE" = dry ]; then
  say "would update Chrome ${installed:-none} to ${offered}"
  exit 0
fi

rm -rf "$STAGE"
ditto "$NEW" "$STAGE" || die "the new app could not be copied"
quiet
swap
now=$(version_of "$APP")
[ "$now" = "$offered" ] || die "installed Chrome reads ${now}, expected ${offered}"
say "updated Chrome ${installed:-none} to ${offered} (Chrome ${installed:-none} kept as the previous one)"

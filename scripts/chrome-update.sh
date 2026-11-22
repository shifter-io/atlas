#!/bin/bash
# Keeps a browser mini's Google Chrome at the current Stable (CHROME-CURRENT-1, docs/browser.html,
# "Keeping Chrome current"). A launchd agent runs it every hour
# (configs/launchd/atlas-chrome-update.plist.example); it may also be run by hand. Written for
# macOS bash 3.2.
#
#   chrome-update.sh [--app <path>] [--wait <seconds>] [--dry-run]
#
# It downloads Google's current Stable installer, checks Google's signature on the app in it, and
# installs it only when it is newer than the installed app (or there is none). Every browser try
# starts its own Chrome from the app, so the app is replaced only while none of its processes
# runs: it waits up to --wait seconds (default 1500) for such a gap, else leaves the update to
# its next start. The new app is copied next to the old one first; the swap is then two renames
# on the same disk, and the old app is deleted after. Run again, it changes nothing once the
# installed Chrome is current. Exit: 0 current or updated, 3 Chrome busy (try later), 1 an error
# (the installed app unchanged).
set -euo pipefail

APP="/Applications/Google Chrome.app"
WAIT=1500
POLL=2
DRY=false
URL=${ATLAS_CHROME_DMG_URL:-https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg}
TEAM=EQHXZ8M8AV # Google LLC's Apple developer team

say() { echo "chrome-update: $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { say "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case $1 in
    --app) APP=${2:?--app needs a path}; shift 2 ;;
    --wait) WAIT=${2:?--wait needs seconds}; shift 2 ;;
    --dry-run) DRY=true; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ $WAIT =~ ^[0-9]+$ ]] || die "--wait must be whole seconds"

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

busy() { pgrep -f "$APP/Contents/" > /dev/null; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/atlas-chrome.XXXXXX")
MOUNT="$WORK/mnt"
STAGE="$(dirname "$APP")/.atlas-chrome-new.app"
OLD="$(dirname "$APP")/.atlas-chrome-old.app"
cleanup() {
  if [ -d "$MOUNT" ]; then hdiutil detach -quiet "$MOUNT" || true; fi
  rm -rf "$WORK" "$STAGE"
}
trap cleanup EXIT

installed=$(version_of "$APP")
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
if [ "$DRY" = true ]; then
  say "would update Chrome ${installed:-none} to ${offered}"
  exit 0
fi

rm -rf "$STAGE" "$OLD"
ditto "$NEW" "$STAGE" || die "the new app could not be copied"
waited=0
while busy; do
  if [ "$waited" -ge "$WAIT" ]; then
    say "busy: Chrome ran for ${WAIT} s; Chrome ${installed} kept, update later"
    exit 3
  fi
  sleep "$POLL"
  waited=$((waited + POLL))
done
if [ -e "$APP" ]; then mv "$APP" "$OLD" || die "the old app could not be moved aside"; fi
if ! mv "$STAGE" "$APP"; then
  if [ -e "$OLD" ]; then mv "$OLD" "$APP"; fi
  die "the new app could not be put in place; the old one is back"
fi
rm -rf "$OLD"
now=$(version_of "$APP")
[ "$now" = "$offered" ] || die "installed Chrome reads ${now}, expected ${offered}"
say "updated Chrome ${installed:-none} to ${offered}"

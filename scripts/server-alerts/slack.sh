# shellcheck shell=bash
# shellcheck disable=SC2034 # POSTED, FAILED and OK are the caller's to read
# Slack posting and the once-per-problem state shared by server-alerts.sh (the Canada server) and
# mini-watch.sh (the browser minis, macOS bash 3.2: no bash 4 features here). The caller sets
# WEBHOOK (a file, mode 600, holding the webhook URL), STATE (a folder), TITLE (the message's
# "who": environment and host), LIMIT (seconds) and defines die and now. The URL reaches curl on
# its stdin (curl -K -), so it never appears in argv, ps, logs or output.
#
# The alert router (ALERT-POLICY-1, #1297, router.py and policy.txt next to this file): each check
# run collects what it saw (route) and hands it to the router once at its end (route_flush), which
# pages only what did not heal and keeps the rest for the daily digest. Dry run until the file
# alert-router next to the webhook reads "live": until then the messages below post as before.

ROUTER=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/router.py

router_live() { [ "$(head -n 1 "${WEBHOOK%/*}/alert-router" 2> /dev/null)" = live ]; }

route_begin() { ROUTE_OBS=$STATE/router.obs; : > "$ROUTE_OBS"; }

# route <open|once|ok|sweep> <object> [kind] [text] [since, unix time] [level: 2 major, 3 critical]
route() {
  if [ -z "${ROUTE_OBS:-}" ] || [ "$1" = skip ]; then return 0; fi
  local text=${4:-}
  text=${text//$'\t'/ } text=${text//$'\n'/ }
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "${3:-}" "$text" "${5:-}" "${6:-2}" >> "$ROUTE_OBS"
}

# Runs the router on the run's observations ($1 "quiet": no digest when nothing happened). A
# router that fails is posted once, when live, until it runs again; a failed post is retried.
route_flush() {
  local out rc=0
  [ -n "${ROUTE_OBS:-}" ] || return 0
  out=$("${ATLAS_PYTHON:-python3}" "$ROUTER" "$STATE" "$TITLE" "$WEBHOOK" ${1:+"$1"} \
    < "$ROUTE_OBS" 2>&1) || rc=$?
  ROUTE_OBS=
  if (( rc == 0 )); then rm -f "$STATE/router.down"; return 0; fi
  echo "${0##*/}: alert router: ${out:-exit $rc}" >&2
  FAILED=1
  if (( rc == 3 )) || ! router_live || [ -f "$STATE/router.down" ]; then return 0; fi
  if post "<!everyone> $TITLE: the alert router failed, alerts may be missing: ${out##*$'\n'}"; then
    touch "$STATE/router.down"
  fi
}

# A one-off line that no "recovered" follows (a failed hourly test): the router's digest.
event() { route once "$1" event "$3"; ROUTE_TYPE=skip alert "$@"; }

webhook_url() {
  [ -f "$WEBHOOK" ] || die "missing $WEBHOOK"
  [ -n "$(find "$WEBHOOK" -maxdepth 0 -perm 600)" ] || die "$WEBHOOK must be mode 600"
  local url
  url=$(head -n 1 "$WEBHOOK")
  case $url in
    http://*|https://*) ;;
    *) die "$WEBHOOK does not hold a webhook URL" ;;
  esac
  case $url in *[\"\\[:space:]]*) die "$WEBHOOK holds an unusable URL" ;; esac
  printf '%s' "$url"
}

json_string() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\t'/\\t}
  s=${s//$'\r'/}
  printf '"%s"' "$(printf '%s' "$s" | tr -d '\000-\037')"
}

post() {
  local url
  url=$(webhook_url)
  printf 'url = "%s"\n' "$url" | curl -fsS --max-time 10 -K - -o /dev/null \
    -H 'Content-Type: application/json' --data-binary "{\"text\": $(json_string "$1")}"
}

# One message per service and kind, at most every $LIMIT seconds; "recovered" is always posted.
# With a unit, the caller's alert_details <unit> follows (the journal lines on the server).
alert() {
  local service=$1 kind=$2 detail=$3 unit=${4:-} key text
  POSTED=0
  if [ "$kind" != recovered ]; then route "${ROUTE_TYPE:-open}" "$service" "$kind" "${detail%%$'\n'*}"; fi
  if router_live; then POSTED=1; return 0; fi  # the router posts (route_flush)
  key=$STATE/${service//\//_}.$kind
  if (( $(cat "$STATE/silence" 2> /dev/null || echo 0) > $(now) )); then return 0; fi  # posted after
  if [ "$kind" != recovered ] && [ -f "$key" ] && (( $(now) - $(cat "$key") < LIMIT )); then
    return 0
  fi
  text="<!everyone> $TITLE: $service: ${kind//-/ }: ${detail%%$'\n'*}"
  if [ -n "$unit" ]; then text+=$(alert_details "$unit"); fi
  if post "$text"; then now > "$key"; POSTED=1; else echo "${0##*/}: posting to Slack failed" >&2; FAILED=1; fi
}

down() { touch "$STATE/${1//\//_}.down"; }

up() {
  local down=$STATE/${1//\//_}.down
  route ok "$1"
  [ -f "$down" ] || return 0
  alert "$1" recovered "${2:-ok again}" "${3:-}"
  if [ "$POSTED" = 1 ]; then rm -f "$down"; fi
}

# Runs a check command; its first output line is the error. A failing check is posted once, until
# it has recovered (a post that failed is tried again by the next check). OK is 1 when it passed.
health() {
  local service=$1 out
  shift
  OK=0
  if out=$("$@" 2>&1); then
    OK=1
    up "$service"
  elif [ ! -f "$STATE/${service//\//_}.down" ]; then
    alert "$service" check-failed "${out:-failed}"
    if [ "$POSTED" = 1 ]; then down "$service"; fi
  fi
}

# A unix time as UTC in a date format (GNU date, else BSD date on macOS).
utc() { date -u -d "@$1" "$2" 2> /dev/null || date -u -r "$1" "$2"; }

HEARTBEAT_HOUR=${ATLAS_ALERTS_HEARTBEAT_HOUR:-8}  # the daily "all good" (UTC hour, "off": none)

# One "all good" a day, at HEARTBEAT_HOUR UTC, when nothing is down.
heartbeat() {
  local day
  [ "$HEARTBEAT_HOUR" != off ] || return 0
  ! router_live || return 0  # the router's daily digest replaces it
  (( 10#$(utc "$(now)" +%H) == HEARTBEAT_HOUR )) || return 0
  day=$(utc "$(now)" +%F)
  [ "$(cat "$STATE/heartbeat" 2> /dev/null)" != "$day" ] || return 0
  if compgen -G "$STATE/*.down" > /dev/null; then return 0; fi
  if (( $(cat "$STATE/silence" 2> /dev/null || echo 0) > $(now) )); then return 0; fi
  if post "$TITLE: all good"; then echo "$day" > "$STATE/heartbeat"; fi
}

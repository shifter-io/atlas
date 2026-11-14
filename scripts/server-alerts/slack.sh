# shellcheck shell=bash
# shellcheck disable=SC2034 # POSTED, FAILED and OK are the caller's to read
# Slack posting and the once-per-problem state shared by server-alerts.sh (the Canada server) and
# mini-watch.sh (the browser minis, macOS bash 3.2: no bash 4 features here). The caller sets
# WEBHOOK (a file, mode 600, holding the webhook URL), STATE (a folder), TITLE (the message's
# "who": environment and host), LIMIT (seconds) and defines die and now. The URL reaches curl on
# its stdin (curl -K -), so it never appears in argv, ps, logs or output.

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
  (( 10#$(utc "$(now)" +%H) == HEARTBEAT_HOUR )) || return 0
  day=$(utc "$(now)" +%F)
  [ "$(cat "$STATE/heartbeat" 2> /dev/null)" != "$day" ] || return 0
  if compgen -G "$STATE/*.down" > /dev/null; then return 0; fi
  if (( $(cat "$STATE/silence" 2> /dev/null || echo 0) > $(now) )); then return 0; fi
  if post "$TITLE: all good"; then echo "$day" > "$STATE/heartbeat"; fi
}

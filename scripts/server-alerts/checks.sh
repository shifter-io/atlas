# shellcheck shell=bash
# The wider checks of server-alerts.sh (MONITOR-1, #1094), sourced by it: the serving API colour
# and Caddy, the public site through the CDN and its certificate, the manager's jobs, timers,
# calculation and ingest, the browser minis in the manager's node list, the API login's
# connections, and the host's memory, load and failed system units. Each check is a health()
# call: one message when it fails, one "recovered" when it is fine again, nothing in between.
# Read only: SQL runs in a read-only transaction, the public site is fetched with GET.

PROC=${ATLAS_ALERTS_PROC:-/proc}
MEM_MIN=10            # percent of the memory still available
LOAD_MAX=2            # the 15-minute load per CPU
CALC_MAX=5400         # age of the published calculation while the manager is on (90 min)
INGEST_MAX=5400       # age of the newest stored test run while the hourly test is on (90 min)
TIMER_MAX=3780        # an hourly timer's last firing (1 hour 3 minutes)
NODE_MAX=1800         # a browser mini's last heartbeat (30 minutes)
CONN_PART=80          # percent of the API login's connection limit
CONN_LIMIT=20         # the API login's limit when the role has none
CERT_DAYS=14          # the public certificate's days left
HEARTBEAT_HOUR=${ATLAS_ALERTS_HEARTBEAT_HOUR:-8}  # the daily "all good" (UTC hour, "off": none)
# The manager's jobs and how long each may go without a run (any status: the scheduler fired it).
JOBS=(calculate:600 estimates_release:600 browser_plan:600 raw_upload:1200 dbip_update:93600)

mins() { echo "$(( $1 / 60 )) min"; }
say() { echo "$1"; return 1; }  # a check that has already failed

# A read with the manager's login (database.toml): its own tables.
msql() { SQL_DB=$CONFIG/$ENV/database.toml sql "$1"; }

# Is the environment's manager (orchestrator) running?
manager_on() { systemctl --user is-active --quiet "$PREFIX-orchestrator.service"; }

# The public URL answers 200 with its contract: /healthz "status" ok, /v1/version its fields.
public_ok() {
  local url="https://$PUBLIC$1" body out
  body=$(curl -fsS --max-time 10 "$url" 2>&1) || { echo "$url did not answer 200: ${body:-no answer}"; return 1; }
  out=$("${ATLAS_PYTHON:-python3}" -c '
import json, sys
try:
    body = json.loads(sys.stdin.read())
except ValueError:
    sys.exit("not JSON")
need = ["status"] if sys.argv[1] == "/healthz" else ["revision", "builtAt", "contract"]
missing = [k for k in need if not isinstance(body, dict) or k not in body]
if missing:
    sys.exit("missing " + ", ".join(missing))
if sys.argv[1] == "/healthz" and body["status"] != "ok":
    sys.exit("status is " + str(body["status"]))' "$1" <<< "$body" 2>&1) || { echo "$url: $out"; return 1; }
}

cert_ok() {
  local end left
  end=$(timeout 10 openssl s_client -servername "$PUBLIC" -connect "$PUBLIC:443" < /dev/null 2> /dev/null \
    | openssl x509 -noout -enddate 2> /dev/null || true)
  end=$(date -d "${end#notAfter=}" +%s 2> /dev/null) || { echo "no certificate read from $PUBLIC"; return 1; }
  left=$(( (end - $(now)) / 86400 ))
  (( left >= CERT_DAYS )) || { echo "the certificate of $PUBLIC expires in $left day(s)"; return 1; }
}

memory_ok() {
  local total avail
  total=$(awk '$1 == "MemTotal:" { print $2 }' "$PROC/meminfo")
  avail=$(awk '$1 == "MemAvailable:" { print $2 }' "$PROC/meminfo")
  (( ${avail:-0} * 100 >= ${total:-1} * MEM_MIN )) \
    || { echo "only $(( ${avail:-0} * 100 / ${total:-1} ))% of the memory is available"; return 1; }
}

load_ok() {
  local load cpus
  read -r _ _ load _ < "$PROC/loadavg"
  cpus=$(nproc)
  awk -v l="$load" -v c="$cpus" -v m="$LOAD_MAX" 'BEGIN { exit !(l <= c * m) }' \
    || { echo "the 15-minute load is $load on $cpus CPUs"; return 1; }
}

failed_units_ok() {
  local units
  units=$(systemctl --failed --plain --no-legend --no-pager | awk '{ print $1 }' | paste -sd ' ' -)
  [ -z "$units" ] || { echo "failed system units: $units"; return 1; }
}

# $1 seconds since the job's last run, $2 its last finished status, $3 that run's error, $4 limit.
job_ok() {
  [ -n "$1" ] || { echo "no run in the last 2 days"; return 1; }
  (( $1 <= $4 )) || { echo "late: last run $(mins "$1") ago"; return 1; }
  [ "$2" != failed ] || { echo "the last run failed: ${3:-no error text}"; return 1; }
}

check_jobs() {
  local rows entry job age status error
  if ! rows=$(msql "SELECT job, extract(epoch FROM now() - max(started_at))::bigint,
      (array_agg(status ORDER BY started_at DESC) FILTER (WHERE status IN ('ok', 'failed')))[1],
      (array_agg(translate(coalesce(error, ''), E'\\n\\t', '  ') ORDER BY started_at DESC)
        FILTER (WHERE status IN ('ok', 'failed')))[1]
      FROM job_runs WHERE started_at > now() - interval '2 days' GROUP BY job"); then
    health "manager jobs" say "the job_runs table could not be read"
    return 0
  fi
  up "manager jobs"
  for entry in "${JOBS[@]}"; do
    job=${entry%%:*} age='' status='' error=''
    IFS=$'\t' read -r _ age status error < <(awk -F '\t' -v j="$job" '$1 == j' <<< "$rows") || true
    health "manager job $job" job_ok "$age" "$status" "$error" "${entry#*:}"
  done
}

# $1 an hourly timer: it fired within TIMER_MAX of now (or of being switched on).
timer_ok() {
  local props last since
  props=$(systemctl --user show "$1" -p LastTriggerUSec -p ActiveEnterTimestamp --timestamp=unix)
  last=$(prop LastTriggerUSec "$props") since=$(prop ActiveEnterTimestamp "$props")
  last=${last#@} since=${since#@}
  [[ $last =~ ^[0-9]+$ ]] || last=0
  [[ $since =~ ^[0-9]+$ ]] || since=0
  (( since > last )) && last=$since
  (( last == 0 || $(now) - last <= TIMER_MAX )) || { echo "did not fire for $(mins $(( $(now) - last )))"; return 1; }
}

# $1 a query giving one age in seconds; $2 the limit; $3 what is old; $4 empty: the message.
age_ok() {
  local age
  age=$($1 "$2") || { echo "could not read $4"; return 1; }
  [ -n "$age" ] || { echo "no $4 yet"; return 1; }
  (( age <= $3 )) || { echo "the $4 is $(mins "$age") old"; return 1; }
}

# The API login's connections against its limit (the role's own, else CONN_LIMIT).
connections_ok() {
  local used limit
  IFS=$'\t' read -r used limit < <(sql "SELECT count(*) FILTER (WHERE usename = current_user),
    (SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user) FROM pg_stat_activity") \
    || { echo "could not read pg_stat_activity"; return 1; }
  (( ${limit:--1} > 0 )) || limit=$CONN_LIMIT
  (( ${used:-0} * 100 < limit * CONN_PART )) || { echo "the API login uses $used of its $limit connections"; return 1; }
}

# Each browser mini in the manager's node list (region browser) was seen within NODE_MAX.
node_ok() { (( $1 <= NODE_MAX )) || { echo "offline: last seen $(mins "$1") ago"; return 1; }; }

check_minis() {
  local rows host age
  rows=$(msql "SELECT n.host, extract(epoch FROM now() - max(n.last_heartbeat))::bigint FROM nodes n
    JOIN runner_regions r USING (region_id) WHERE r.slug = 'browser' AND n.host IS NOT NULL
    AND n.last_heartbeat IS NOT NULL GROUP BY n.host ORDER BY n.host") || return 0  # check_jobs tells
  while IFS=$'\t' read -r host age; do
    if [ -n "$host" ]; then health "mini $host" node_ok "$age"; fi
  done <<< "$rows"
}

check_manager() {
  local timer
  for timer in hourly pulse; do
    if systemctl --user is-active --quiet "$PREFIX-$timer.timer"; then
      health "$PREFIX-$timer.timer on time" timer_ok "$PREFIX-$timer.timer"
    fi
  done
  [ "$DB_OK" = 1 ] || return 0  # "database" tells
  if systemctl --user is-active --quiet "$PREFIX-hourly.timer" && [ -f "$CONFIG/$ENV/database.toml" ]; then
    health "ingest" age_ok msql "SELECT extract(epoch FROM now() - max(imported_at))::bigint FROM test_runs" \
      "$INGEST_MAX" "newest stored test run"
  fi
  manager_on || return 0
  if [ -f "$CONFIG/$ENV/database-api.toml" ]; then
    health "calculation" age_ok sql "SELECT extract(epoch FROM now() - b.created_at)::bigint
      FROM current_calculation c JOIN calculation_builds b USING (build_id)" "$CALC_MAX" "published calculation"
  fi
  [ -f "$CONFIG/$ENV/database.toml" ] || return 0
  check_jobs
  check_minis
}

# Host-wide checks run once: with production, or with staging when production has no alerts.
check_host() {
  [ "$ENV" = production ] || [ ! -d "$ATLAS_HOME/production/alerts-state" ] || return 0
  health memory memory_ok
  health load load_ok
  health "system units" failed_units_ok
}

check_public() {
  health "public $PUBLIC/healthz" public_ok /healthz
  health "public $PUBLIC/v1/version" public_ok /v1/version
  health "certificate $PUBLIC" cert_ok
}

# One "all good" a day, at HEARTBEAT_HOUR UTC, when nothing is down.
heartbeat() {
  local day
  [ "$HEARTBEAT_HOUR" != off ] || return 0
  (( 10#$(date -u -d "@$(now)" +%H) == HEARTBEAT_HOUR )) || return 0
  day=$(date -u -d "@$(now)" +%F)
  [ "$(cat "$STATE/heartbeat" 2> /dev/null)" != "$day" ] || return 0
  if compgen -G "$STATE/*.down" > /dev/null; then return 0; fi
  if (( $(cat "$STATE/silence" 2> /dev/null || echo 0) > $(now) )); then return 0; fi
  if post "Atlas $ENV on ${HOST:?}: all good"; then echo "$day" > "$STATE/heartbeat"; fi
}

check_more() {
  check_host
  check_public
  if [ "$DB_OK" = 1 ] && [ -f "$CONFIG/$ENV/database-api.toml" ]; then health "API connections" connections_ok; fi
  check_manager
  heartbeat
}

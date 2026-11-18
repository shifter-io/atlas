#!/usr/bin/env bash
# Live security check of one Atlas environment (SEC-ORIGIN-1): the Phase 2 runbook's part A
# (the server from outside its cloud network) and part D (the public name, through the CDN),
# the checks that need no password. Read-only and light: a few requests per check and one
# slow port scan. The operator's Canada server runbook has the list.
#
#   bash scripts/server-security-check.sh <production|staging> [--host <server>] [--name <public name>]
#        [--ssh <user>:<port>] [--db <database server>]...
#
# --host runs part A, --name part D (skipped while the name does not resolve), --ssh adds the
# SSH login check and every --db the database port checks. Run it from a host outside the
# server and its cloud network, as root (the UDP scan needs it), with curl, nmap, openssl and
# psql. It prints `<id> pass|fail|skip <evidence>` per check and exits 1 on any fail. The
# evidence names ports, codes and header names only: never an address, never a secret.
set -uo pipefail

usage() { echo "usage: server-security-check.sh <production|staging> [--host <server>] [--name <public name>] [--ssh <user>:<port>] [--db <database server>]..." >&2; exit 2; }

ENV=${1:-}
[ $# -gt 0 ] && shift
case $ENV in
production) SITE_PORT=8445; HTTP_PORT=8080; UPLOAD_PORT=8444 ;;
staging) SITE_PORT=9445; HTTP_PORT=9080; UPLOAD_PORT=9444 ;;
*) usage ;;
esac
ORIGIN_PORTS="8080 8445 9080 9445" # both environments' origins, HTTP and HTTPS; every other port must be closed
HOST="" NAME="" SSH_USER="" SSH_PORT="" DBS=()
while [ $# -gt 0 ]; do
	[ -n "${2:-}" ] || usage
	case $1 in
	--host) HOST=$2 ;;
	--name) NAME=$2 ;;
	--ssh) SSH_USER=${2%%:*} SSH_PORT=${2##*:}; [[ $SSH_PORT =~ ^[0-9]+$ && -n $SSH_USER ]] || usage ;;
	--db) DBS+=("$2") ;;
	*) usage ;;
	esac
	shift 2
done
[ -n "$HOST$NAME" ] || usage
FAILED=0
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

result() { # <id> <pass|fail|skip> <evidence>
	printf '%s %s %s\n' "$1" "$2" "$3"
	if [ "$2" = fail ]; then FAILED=1; fi
}

urlhost() { if [[ $1 == *:* ]]; then echo "[$1]"; else echo "$1"; fi; }

# shellcheck disable=SC2016 # $0 and $1 belong to the inner shell
tcp_open() { timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>/dev/null; }

# ports <nmap output> <proto> prints the open ports of a grepable (-oG) nmap output.
ports() { grep -o "[0-9]*/open/$2" <<<"$1" | cut -d/ -f1 | sort -n | xargs; }

check_a1() {
	local out open p extra=""
	out=$(nmap -Pn -p- -T3 --max-rate 300 -oG - "$HOST" 2>/dev/null) || { result A1 fail "nmap did not run"; return; }
	open=$(ports "$out" tcp)
	for p in $open; do
		case " $ORIGIN_PORTS " in *" $p "*) ;; *) extra="$extra $p" ;; esac
	done
	if [ -z "$extra" ]; then result A1 pass "open TCP ports: ${open:-none}"; else result A1 fail "open TCP ports beyond the origins:$extra"; fi
}

check_a2() {
	local out open
	out=$(nmap -Pn -sU --top-ports 20 -oG - "$HOST" 2>&1) || { result A2 fail "nmap -sU did not run (it needs root)"; return; }
	open=$(ports "$out" udp)
	if [ -z "$open" ]; then result A2 pass "no open UDP port in the top 20"; else result A2 fail "open UDP ports: $open"; fi
}

check_a3() {
	local err methods
	if [ -z "$SSH_PORT" ]; then result A3 skip "no --ssh given"; return; fi
	if ! tcp_open "$HOST" "$SSH_PORT"; then result A3 pass "the SSH port does not answer"; return; fi
	# Unpinned on purpose: no key or password is offered (BatchMode, PubkeyAuthentication=no) and
	# no command reaches the server, so an unknown host key exposes nothing (SEC-SSH-PIN-1).
	err=$(ssh -p "$SSH_PORT" -o BatchMode=yes -o PubkeyAuthentication=no -o ConnectTimeout=5 \
		-o PreferredAuthentications=password,keyboard-interactive -o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$SSH_USER@$HOST" true 2>&1 </dev/null)
	methods=$(grep -o 'Permission denied ([^)]*)' <<<"$err" | sed 's/.*(\(.*\))/\1/')
	if [ "$methods" = publickey ]; then result A3 pass "login methods offered: publickey"
	else result A3 fail "login methods offered: ${methods:-unknown}"; fi
}

# check_a4 asks the HTTPS origin without the secret header: each answer must be a closed connection
# or an empty 403 (no JSON, no version).
check_a4() {
	local base path extra got seen="" bad=0 fake hdr
	base=https://$(urlhost "$HOST"):$SITE_PORT
	fake=$(openssl rand -hex 32)
	for path in /healthz /v1/version /v1/networks; do
		for extra in "" "Host: ${NAME:-atlas-origin}" "X-Atlas-Origin-Key: $fake"; do
			hdr=()
			[ -z "$extra" ] || hdr=(-H "$extra")
			got=$(curl -sk -m 10 -o /dev/null -w '%{http_code} %{size_download}' "${hdr[@]}" "$base$path" 2>/dev/null)
			case $got in "000 0" | "403 0") ;; *) bad=1 ;; esac
			case " $seen " in *" ${got// /:} "*) ;; *) seen="$seen ${got// /:}" ;; esac
		done
	done
	if [ $bad = 0 ]; then result A4 pass "code:bytes seen:$seen"; else result A4 fail "code:bytes seen:$seen"; fi
}

# check_a11 asks the plain HTTP origin, the CDN's, without any header (ORIGIN-HTTP-1): a 200
# answered directly, without a redirect to HTTPS and without Strict-Transport-Security.
check_a11() {
	local code
	code=$(curl -s -m 20 -D "$WORK/a11" -o /dev/null -w '%{http_code}' "http://$(urlhost "$HOST"):$HTTP_PORT/v1/version" 2>/dev/null)
	if [ "$code" != 200 ]; then result A11 fail "plain HTTP $HTTP_PORT without a header: $code"
	elif grep -qi '^strict-transport-security:' "$WORK/a11"; then result A11 fail "plain HTTP $HTTP_PORT: 200 with Strict-Transport-Security"
	else result A11 pass "plain HTTP $HTTP_PORT without a header: 200, no Strict-Transport-Security"; fi
}

check_a5() {
	local code
	code=$(curl -s -m 10 -o "$WORK/a5" -w '%{http_code}' "http://$(urlhost "$HOST"):$SITE_PORT/" 2>/dev/null)
	if [ "$code" = 000 ]; then result A5 pass "plain HTTP: no answer"
	elif [ "$code" = 400 ] && ! grep -Eiq 'caddy|version|go[0-9]' "$WORK/a5"; then result A5 pass "plain HTTP: 400, no server details"
	else result A5 fail "plain HTTP: $code"; fi
}

# tls_grade <id> <host> <port> passes when nmap sees TLS 1.2/1.3 only and every cipher graded A.
tls_grade() {
	local out
	out=$(nmap -Pn --script ssl-enum-ciphers -p "$3" "$2" 2>/dev/null) || { result "$1" fail "nmap did not run"; return; }
	# NULL alone is nmap's compressor list on every TLS 1.2 server: match it only in a cipher name.
	if grep -Eq 'SSLv3|TLSv1\.[01]:|RC4|3DES|_NULL_|_anon_|_EXPORT' <<<"$out"; then result "$1" fail "an old protocol or cipher is offered"
	elif grep -q 'least strength: A$' <<<"$out"; then result "$1" pass "TLS 1.2/1.3 only, least strength A"
	else result "$1" fail "least strength: $(grep -o 'least strength: .*' <<<"$out" || echo none)"; fi
}

check_a7() {
	local pem names start end
	pem=$(openssl s_client -connect "$(urlhost "$HOST"):$SITE_PORT" </dev/null 2>/dev/null | openssl x509 2>/dev/null) || { result A7 fail "no certificate"; return; }
	names=$(openssl x509 -noout -subject -ext subjectAltName <<<"$pem" 2>/dev/null | grep -oE '(CN ?= ?|DNS:)[^,/]*' | sed -E 's/^(CN ?= ?|DNS:)//' | sort -u | xargs)
	start=$(date -d "$(openssl x509 -noout -startdate <<<"$pem" | cut -d= -f2)" +%s)
	end=$(openssl x509 -noout -enddate <<<"$pem" | cut -d= -f2)
	if [ "$start" -gt "$(date +%s)" ] || ! openssl x509 -noout -checkend 0 <<<"$pem" >/dev/null; then result A7 fail "not valid today (until $end)"
	elif [ "$names" != atlas-origin ]; then result A7 fail "names other than atlas-origin: $names"
	else result A7 pass "names atlas-origin only, valid until $end"; fi
}

check_a8() {
	if tcp_open "$HOST" "$UPLOAD_PORT"; then result A8 fail "upload port $UPLOAD_PORT accepts connections"
	else result A8 pass "upload port $UPLOAD_PORT filtered or closed"; fi
}

# check_db <n> <server>: A9 its port 5432 must not connect; A10 (only if it does) a fake
# password must be refused by the access list itself, with and without TLS.
check_db() {
	local out mode verdict="pass" seen=""
	if ! tcp_open "$2" 5432; then result "A9.$1" pass "database server $1: port 5432 filtered or closed"; return; fi
	result "A9.$1" fail "database server $1: port 5432 accepts connections"
	for mode in disable require; do
		out=$(PGPASSWORD=atlas-check-wrong PGCONNECT_TIMEOUT=5 psql -w -X \
			"host=$2 dbname=proxy_reviews user=proxy_api sslmode=$mode" -c 'select 1' 2>&1 </dev/null)
		case $out in
		*"no pg_hba.conf entry"* | *"does not support SSL"*) seen="$seen $mode:refused" ;;
		*"password authentication failed"*) seen="$seen $mode:password-asked"; verdict=fail ;;
		*) seen="$seen $mode:unclear"; verdict=fail ;;
		esac
	done
	result "A10.$1" "$verdict" "database server $1:$seen"
}

part_a() {
	local i=0 db
	result A0 skip "the cloud panel's firewall against the same provider's machines: not testable from here"
	check_a1
	check_a2
	check_a3
	check_a4
	check_a5
	tls_grade A6 "$HOST" "$SITE_PORT"
	check_a7
	check_a8
	if [ ${#DBS[@]} -eq 0 ]; then result A9 skip "no --db given"; fi
	for db in "${DBS[@]}"; do i=$((i + 1)); check_db "$i" "$db"; done
	check_a11
}

# cdn <url> fetches at most 1 MB of the url's answer: prints "<code> <bytes> <CDN-Cache value>".
cdn() {
	local code
	code=$(curl -s -m 30 --max-filesize 1048576 -D "$WORK/h" -o /dev/null -w '%{http_code} %{size_download}' "$1" 2>/dev/null)
	echo "${code:-000 0} $(grep -i '^cdn-cache:' "$WORK/h" 2>/dev/null | tr -d '\r' | awk '{print $2}' | tail -1)"
}

check_d2() {
	local r url file bad=0 seen="" code bytes cache
	r=zz=$(openssl rand -hex 6)
	file=$(curl -s -m 20 -o /dev/null -w '%{redirect_url}' "$BASE/v1/dataset" 2>/dev/null)
	file=${file:-$BASE/v1/dataset}
	if [[ $file == *\?* ]]; then file="$file&$r"; else file="$file?$r"; fi
	for url in "$BASE/v1/rankings?range=24h&$r" "$BASE/v1/version?$r" "$file"; do
		read -r code bytes cache <<<"$(cdn "$url")"
		seen="$seen $code:${cache:-none}"
		[ "$cache" = HIT ] || { [ "$code" = 400 ] && [ "$bytes" -lt 1024 ]; } || bad=1
	done
	if [ $bad = 0 ]; then result D2 pass "code:cache seen:$seen"; else result D2 fail "code:cache seen:$seen"; fi
}

check_d3() {
	local h missing=""
	h=$(curl -s -m 20 -D - -o /dev/null "$BASE/v1/version" 2>/dev/null | tr -d '\r' | tr '[:upper:]' '[:lower:]')
	grep -q '^strict-transport-security: max-age=' <<<"$h" || missing="$missing strict-transport-security"
	grep -q '^x-content-type-options: nosniff' <<<"$h" || missing="$missing x-content-type-options"
	grep -q '^access-control-allow-origin: \*$' <<<"$h" || missing="$missing access-control-allow-origin"
	grep -q '^access-control-allow-credentials:' <<<"$h" && missing="$missing (has access-control-allow-credentials)"
	if [ -z "$missing" ]; then result D3 pass "security headers present"; else result D3 fail "header problems:$missing"; fi
}

check_d5() {
	local code keys extra
	code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/networks" 2>/dev/null)
	keys=$(curl -s -m 20 "$BASE/healthz" 2>/dev/null | grep -o '"[A-Za-z_]*" *:' | tr -d '": ' | sort -u | xargs)
	extra=$(tr ' ' '\n' <<<"$keys" | grep -vxE 'status|version|builtAt|revision|reason' | xargs)
	case $code in 405 | 403 | 000) ;; *) result D5 fail "POST answered $code"; return ;; esac
	if [ -n "$keys" ] && [ -z "$extra" ]; then result D5 pass "POST $code; /healthz fields: $keys"
	else result D5 fail "POST $code; /healthz fields beyond status-only: ${extra:-no answer}"; fi
}

part_d() {
	local first second
	if ! getent hosts "$NAME" >/dev/null; then result D skip "the public name does not resolve yet"; return; fi
	BASE=https://$NAME
	first=$(cdn "$BASE/v1/rankings?range=24h" | awk '{print $3}')
	second=$(cdn "$BASE/v1/rankings?range=24h" | awk '{print $3}')
	if [ "$second" = HIT ]; then result D1 pass "CDN-Cache ${first:-none}, then HIT"; else result D1 fail "CDN-Cache ${first:-none}, then ${second:-none}"; fi
	check_d2
	check_d3
	if curl -s -m 20 -o /dev/null "$BASE/v1/version"; then tls_grade D4 "$NAME" 443
	else result D4 fail "the certificate is not valid for the name"; fi
	check_d5
}

if [ -n "$HOST" ]; then part_a; fi
if [ -n "$NAME" ]; then part_d; fi
exit "$FAILED"

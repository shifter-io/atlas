#!/usr/bin/env bash
# Installs or updates one Atlas environment on the Canada server, as the normal user and
# without root (docs/operations/canada-server.md): the pinned tools in ~/atlas/bin, the code
# of one main commit, the API's blue/green user services and the shared Caddy site.
# A second run with the same commit changes nothing; --dry-run prints every action instead.
#
#   bash scripts/server-setup.sh <production|staging> [--ref <commit>] [--dry-run]
#
# The code comes from a git repository on the server (ATLAS_REPO_URL, default
# ~/atlas/repo.git), which the caller fills first with `git push`.
set -euo pipefail

UV_VERSION=0.13.8
UV_SHA256=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
PYTHON_VERSION=3.12.14
GO_VERSION=1.27.4
GO_SHA256=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
CADDY_VERSION=2.11.9
CADDY_SHA256=5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb
PSQL_MIN_MAJOR=17

usage() { echo "usage: server-setup.sh <production|staging> [--ref <commit>] [--dry-run]" >&2; exit 2; }
die() { echo "server-setup: $*" >&2; exit 1; }
say() { echo "server-setup: $*"; }
plan() { printf '+ %s\n' "$*"; }

ENV=${1:-}
[ $# -gt 0 ] && shift
REF=""
DRY=false
while [ $# -gt 0 ]; do
	case $1 in
	--ref) REF=${2:-}; [[ $REF =~ ^[0-9a-f]{7,40}$ ]] || usage; shift 2 ;;
	--dry-run) DRY=true; shift ;;
	*) usage ;;
	esac
done
case $ENV in
production) NAME=atlas; BLUE_PORT=8090; GREEN_PORT=8091; SITE_PORT=8445; HTTP_PORT=8080 ;;
staging) NAME=atlas-staging; BLUE_PORT=9090; GREEN_PORT=9091; SITE_PORT=9445; HTTP_PORT=9080 ;;
esac
case $ENV in # the colours' admin listeners (the full /healthz), loopback only
production) BLUE_ADMIN=8190; GREEN_ADMIN=8191 ;;
staging) BLUE_ADMIN=9190; GREEN_ADMIN=9191 ;;
*) usage ;;
esac
[ "$(id -u)" -ne 0 ] || die "refusing to run as root: run it as the normal user"

ROOT=${ATLAS_ROOT:-$HOME/atlas}
CONF=${ATLAS_CONFIG:-$HOME/.config/atlas}
UNITS=${ATLAS_UNIT_DIR:-$HOME/.config/systemd/user}
REPO=${ATLAS_REPO_URL:-$ROOT/repo.git}
HEALTH_TIMEOUT=${ATLAS_HEALTH_TIMEOUT:-900}
# Caddy needs 5 passing health checks 2 s apart (site.caddy) before the new colour takes traffic.
HANDOVER_SECONDS=${ATLAS_HANDOVER_SECONDS:-15}
SELF_CHECK_ROUNDS=20 # each asks the version and the biggest answer; 1.5 s apart: 30 s
BIN=$ROOT/bin
CADDY=$ROOT/caddy
SRC=$ROOT/$ENV/src
API=$ROOT/$ENV/api
PRIV=$CONF/$ENV
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)
export PATH=$BIN:$PATH UV_CACHE_DIR=$ROOT/cache/uv UV_PYTHON_INSTALL_DIR=$ROOT/python
export UV_PROJECT_ENVIRONMENT=$ROOT/$ENV/venv UV_PYTHON_PREFERENCE=only-managed
COMMIT=""
CADDY_RELOAD=false
PUBLIC="" # the public URL, from $PRIV/public-url (secrets)

# run prints the action under --dry-run and runs it otherwise. Never pass a secret to it.
run() {
	if $DRY; then printf '+ %s\n' "$*"; else "$@"; fi
}

# put writes stdin to $1 with mode $2 when its content or mode differs; returns 1 when unchanged.
put() {
	local dest=$1 mode=$2 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if [ -f "$dest" ] && cmp -s "$tmp" "$dest" && [ "$(stat -c %a "$dest")" = "$mode" ]; then
		rm -f "$tmp"
		return 1
	fi
	if $DRY; then
		printf '+ write %s (mode %s)\n' "$dest" "$mode"
		rm -f "$tmp"
	else
		mkdir -p "$(dirname "$dest")"
		chmod "$mode" "$tmp"
		mv "$tmp" "$dest"
	fi
}

# render prints a template from scripts/server-setup/ with this environment's values. It runs
# in the shell, so no value (the origin key included) ever reaches a command line.
render() {
	local t
	t=$(<"$TPL/$1")
	t=${t//@ENV@/$ENV}; t=${t//@NAME@/$NAME}; t=${t//@API@/$API}; t=${t//@PRIV@/$PRIV}
	t=${t//@BIN@/$BIN}; t=${t//@CADDY@/$CADDY}; t=${t//@SITE_PORT@/$SITE_PORT}; t=${t//@HTTP_PORT@/$HTTP_PORT}
	t=${t//@BLUE_PORT@/$BLUE_PORT}; t=${t//@GREEN_PORT@/$GREEN_PORT}; t=${t//@KEY@/${2:-}}
	printf '%s\n' "$t"
}

ensure_dir() { # <mode> <dir>...
	local mode=$1 d
	shift
	for d in "$@"; do
		[ -d "$d" ] || run install -d -m "$mode" "$d"
	done
}

# private_file stops unless $1 is mode 600 in a mode-700 folder; it names the file, never its content.
private_file() {
	local f=$1
	if $DRY && [ ! -f "$f" ]; then plan "MISSING private file $f: a real run stops here"; return 0; fi
	[ -f "$f" ] || die "missing private file $f"
	[ "$(stat -c %a "$f")" = 600 ] || die "private file $f must be mode 600"
	[ "$(stat -c %a "$(dirname "$f")")" = 700 ] || die "folder $(dirname "$f") must be mode 700"
}

fetch() { # <url> <sha256> <dest>
	curl -fsSL --retry 3 -o "$3" "$1"
	echo "$2  $3" | sha256sum -c --quiet - || die "SHA-256 mismatch for $1"
}

tool_uv() {
	[ "$("$BIN/uv" --version 2>/dev/null | cut -d' ' -f2)" = "$UV_VERSION" ] && return 0
	if $DRY; then plan "install uv $UV_VERSION into $BIN (SHA-256 checked)"; return 0; fi
	fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" "$UV_SHA256" "$WORK/uv.tgz"
	tar -xzf "$WORK/uv.tgz" -C "$BIN" --strip-components 1
}

tool_python() {
	if $DRY; then
		[ -d "$ROOT/python" ] || plan "install Python $PYTHON_VERSION into $ROOT/python (uv checks it)"
	elif ! uv python find --no-python-downloads "$PYTHON_VERSION" >/dev/null 2>&1; then
		uv python install -q --no-bin "$PYTHON_VERSION"
	fi
}

tool_go() {
	local goroot=$ROOT/go-$GO_VERSION
	if [ ! -x "$goroot/bin/go" ]; then
		if $DRY; then plan "install Go $GO_VERSION into $goroot (SHA-256 checked)"; else
			fetch "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" "$GO_SHA256" "$WORK/go.tgz"
			mkdir -p "$WORK/go" && tar -xzf "$WORK/go.tgz" -C "$WORK/go" && mv "$WORK/go/go" "$goroot"
		fi
	fi
	[ "$(readlink "$BIN/go" 2>/dev/null)" = "$goroot/bin/go" ] || run ln -sfn "$goroot/bin/go" "$BIN/go"
}

tool_caddy() {
	case $("$BIN/caddy" version 2>/dev/null) in "v$CADDY_VERSION "*) return 0 ;; esac
	if $DRY; then plan "install Caddy $CADDY_VERSION into $BIN (SHA-256 checked)"; return 0; fi
	fetch "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" "$CADDY_SHA256" "$WORK/caddy.tgz"
	tar -xzf "$WORK/caddy.tgz" -C "$BIN" caddy
}

# tool_psql unpacks the release's newest PostgreSQL client (never installs it): apt checks each
# package against the release's signed index, and the SHA-256 is compared with the index again.
tool_psql() {
	local major pkg deb p v dir lib
	major=$("$BIN/psql" --version 2>/dev/null | awk '{print $3}' | cut -d. -f1) || true
	[ "${major:-0}" -ge "$PSQL_MIN_MAJOR" ] 2>/dev/null && return 0
	if $DRY; then plan "unpack psql (postgresql-client >= $PSQL_MIN_MAJOR, apt-get download + dpkg -x) into $ROOT"; return 0; fi
	pkg=$(apt-cache depends postgresql-client | awk '/Depends: postgresql-client-[0-9]/ {print $2; exit}')
	major=${pkg##*-}
	[ "${major:-0}" -ge "$PSQL_MIN_MAJOR" ] 2>/dev/null || die "the release offers no postgresql-client >= $PSQL_MIN_MAJOR"
	(cd "$WORK" && apt-get download -q "$pkg" libpq5 >/dev/null)
	dir=$ROOT/postgresql-$major
	for deb in "$WORK"/*.deb; do
		p=$(dpkg-deb -f "$deb" Package) v=$(dpkg-deb -f "$deb" Version)
		[ "$(sha256sum <"$deb" | cut -d' ' -f1)" = "$(apt-cache show "$p=$v" | awk '/^SHA256:/ {print $2; exit}')" ] || die "SHA-256 mismatch for $p"
		dpkg -x "$deb" "$dir"
	done
	lib=$(dirname "$(find "$dir/usr/lib" -name 'libpq.so.5' | head -1)")
	printf '#!/bin/sh\nLD_LIBRARY_PATH=%s exec %s "$@"\n' "$lib" "$dir/usr/lib/postgresql/$major/bin/psql" | put "$BIN/psql" 755 || true
}

# code checks out the commit (default: the repository's latest main) into src and syncs the venv.
code() {
	if [ ! -d "$SRC/.git" ]; then
		$DRY || git ls-remote -q "$REPO" refs/heads/main | grep -q . || die "no main in $REPO: push it there first (docs/operations/canada-server.md)"
		run git clone -q "$REPO" "$SRC"
		if $DRY; then COMMIT=${REF:-main}; return 0; fi
	fi
	if [ -z "$REF" ] || ! git -C "$SRC" cat-file -e "$REF^{commit}" 2>/dev/null; then
		run git -C "$SRC" fetch -q --no-write-fetch-head origin '+refs/heads/main:refs/remotes/origin/main'
	fi
	COMMIT=$(git -C "$SRC" rev-parse --verify "${REF:-origin/main}^{commit}") || die "unknown commit ${REF:-main}"
	git -C "$SRC" merge-base --is-ancestor "$COMMIT" origin/main || die "commit $COMMIT is not on main"
	[ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null)" = "$COMMIT" ] || run git -C "$SRC" checkout -q --detach "$COMMIT"
	$DRY || [ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$SRC" status --porcelain)" ] || die "$SRC has local changes"
	# uv rebuilds the project only when pyproject.toml changes: reinstall it on every run (#1090).
	(cd "$SRC" && run uv sync -q --locked --no-dev --no-editable --reinstall-package atlas --python "$PYTHON_VERSION")
	installed
}

# installed stops unless the venv's atlas package holds the commit's src/atlas, file for file
# (its configs/ comes from the repository's configs/, pyproject.toml's force-include).
installed() {
	local pkg
	if $DRY; then plan "check the venv's atlas package matches $SRC/src/atlas"; return 0; fi
	pkg=$("$UV_PROJECT_ENVIRONMENT/bin/python" -I -c 'import atlas, os; print(os.path.dirname(atlas.__file__))') ||
		die "no atlas package in $UV_PROJECT_ENVIRONMENT"
	diff -rq -x __pycache__ -x configs "$SRC/src/atlas" "$pkg" >&2 ||
		die "the installed atlas in $pkg is not $COMMIT's src/atlas; nothing was switched"
}

# worker_wheel builds the commit's wheel for the droplets once (IMAGE-AUTO-1): it goes to
# wheels/<sha12>/ with its requirements.txt, and wheels/current.json names it (commit, wheel,
# sha256, requirements_sha256); the daily and pulse hours ship it at launch
# (src/atlas/campaign/current_wheel.py).
worker_wheel() {
	local dir=$ROOT/$ENV/wheels out whl sha req
	if $DRY; then plan "build $COMMIT's worker wheel into $dir and name it in $dir/current.json"; return 0; fi
	grep -qs "\"commit\": \"$COMMIT\"" "$dir/current.json" && return 0
	out=$(mktemp -d "$WORK/wheel-XXXXXX")
	(cd "$SRC" && SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) uv build -q --wheel --python "$PYTHON_VERSION" --out-dir "$out")
	whl=$(find "$out" -name 'atlas-*-py3-none-any.whl')
	[ -n "$whl" ] && [ "$(printf '%s\n' "$whl" | wc -l)" -eq 1 ] || die "uv build made no single atlas wheel"
	sha=$(sha256sum <"$whl" | cut -d' ' -f1)
	# The requirements go beside the wheel: the daily image rebuild installs them (IMAGE-DAILY-1).
	(cd "$SRC" && uv export -q --frozen --no-dev --no-emit-project --format requirements-txt) >"$out/requirements.txt"
	req=$(grep -v -e '^#' -e '^[[:space:]]*$' "$out/requirements.txt" | sha256sum | cut -d' ' -f1)  # current_wheel.requirements_fingerprint
	install -d -m 755 "$dir" "$dir/${sha:0:12}"
	install -m 644 "$whl" "$out/requirements.txt" "$dir/${sha:0:12}/"
	printf '{"commit": "%s", "wheel": "%s/%s", "sha256": "%s", "requirements_sha256": "%s"}\n' \
		"$COMMIT" "${sha:0:12}" "$(basename "$whl")" "$sha" "$req" | put "$dir/current.json" 644 || true
	say "droplets get $COMMIT's wheel ${sha:0:12}"
}

# build compiles siteapi for the commit once; the binary must name the commit, unmodified.
build() {
	local out=$API/$COMMIT want
	[ -x "$out/siteapi" ] && return 0
	want=$(awk '$1 == "go" {print $2}' "$SRC/api/go.mod" 2>/dev/null || echo "$GO_VERSION")
	[ "$want" = "$GO_VERSION" ] || die "api/go.mod names Go $want; update GO_VERSION and GO_SHA256 in this script"
	if $DRY; then plan "build $out/siteapi (-trimpath)"; return 0; fi
	(cd "$SRC/api" && GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
		GOPATH=$ROOT/cache/gopath GOCACHE=$ROOT/cache/go-build go build -trimpath -o "$out.tmp/siteapi" ./cmd/siteapi)
	go version -m "$out.tmp/siteapi" | grep -q "vcs.revision=$COMMIT" || die "siteapi does not name $COMMIT"
	go version -m "$out.tmp/siteapi" | grep -q "vcs.modified=false" || die "siteapi was built from a modified tree"
	mv "$out.tmp" "$out"
}

# secrets checks the API's database file, makes the CDN's origin key when missing and reads the
# optional public URL the deploy's self-check asks (the owner's private runbook names it).
secrets() {
	ensure_dir 700 "$CONF" "$PRIV"
	if [ ! -e "$PRIV/origin-key" ]; then
		if $DRY; then plan "create $PRIV/origin-key (32 random bytes)"; else (umask 077 && openssl rand -hex 32 >"$PRIV/origin-key"); fi
	fi
	if [ -e "$PRIV/origin-key" ] || ! $DRY; then private_file "$PRIV/origin-key"; fi
	private_file "$PRIV/database-api.toml"
	[ -f "$PRIV/public-url" ] || return 0
	PUBLIC=$(<"$PRIV/public-url")
	PUBLIC=${PUBLIC%/}
	[[ $PUBLIC =~ ^https?://[^[:space:]/]+$ ]] || die "$PRIV/public-url must hold one URL such as https://host"
}

# db_tls (SEC-DBTLS-2) installs the database servers' root certificate, which the operator copies
# to $CONF/db-root.crt (scripts/db-tls-setup.sh), into the environment's folder, and gives each
# database file for a remote host sslmode = "verify-full" with it. Without it nothing changes.
db_tls() {
	local src=$CONF/db-root.crt root=$PRIV/db-root.crt f host text
	[ -f "$src" ] || return 0
	grep -q 'PRIVATE KEY' "$src" && die "$src holds a private key: copy only the CA's certificate"
	openssl x509 -noout -in "$src" 2>/dev/null || die "$src is not a certificate"
	put "$root" 644 <"$src" || true
	for f in "$PRIV"/database*.toml; do
		[ -f "$f" ] || continue
		private_file "$f"
		grep -q '^[[:space:]]*\[postgres\]' "$f" || die "$f has no [postgres] table"
		host=$(awk -F"[\"']" '/^[ \t]*host[ \t]*=/ {print $2; exit}' "$f")
		case $host in "" | localhost | 127.* | ::1 | /*) continue ;; esac # local: no certificate to check
		# Into a variable first: a failed awk (an older commit's templates lack it) must never
		# reach put, which would empty the file.
		text=$(awk -v root="$root" -f "$TPL/db-verify-full.awk" "$f") && [ -n "$text" ] ||
			die "could not set verify-full in $f: it is unchanged"
		if printf '%s\n' "$text" | put "$f" 600; then
			say "$(basename "$f") uses verify-full: its services read it at their next start"
		fi
	done
}

# units writes the API template, the colours' ports, Caddy's unit, the CDN edge refresh (its unit,
# timer and script) and the shared Caddyfile.
units() {
	local changed=false
	render api@.service | put "$UNITS/$NAME-api@.service" 644 && changed=true
	render atlas-caddy.service | put "$UNITS/atlas-caddy.service" 644 && changed=true
	# The CDN's edge servers for Caddy (SEC-RATE-1): written here, enabled by the operator.
	render atlas-cdn-edges.service | put "$UNITS/atlas-cdn-edges.service" 644 && changed=true
	render atlas-cdn-edges.timer | put "$UNITS/atlas-cdn-edges.timer" 644 && changed=true
	put "$BIN/atlas-cdn-edges" 755 <"$TPL/atlas-cdn-edges.py" || true
	# SITEAPI_ADMIN_LISTEN and SITEAPI_HOLD_FILE, not flags: an older siteapi ignores them.
	printf 'ATLAS_API_LISTEN=127.0.0.1:%s\nSITEAPI_ADMIN_LISTEN=127.0.0.1:%s\nSITEAPI_HOLD_FILE=%s\n' "$BLUE_PORT" "$BLUE_ADMIN" "$API/hold-blue" | put "$API/blue.env" 644 || true
	printf 'ATLAS_API_LISTEN=127.0.0.1:%s\nSITEAPI_ADMIN_LISTEN=127.0.0.1:%s\nSITEAPI_HOLD_FILE=%s\n' "$GREEN_PORT" "$GREEN_ADMIN" "$API/hold-green" | put "$API/green.env" 644 || true
	render Caddyfile | put "$CADDY/Caddyfile" 644 && CADDY_RELOAD=true
	ensure_dir 700 "$CADDY/sites" "$CADDY/logs"
	ensure_dir 755 "$API/files-blue" "$API/files-green"
	if $changed; then run systemctl --user daemon-reload; fi
}

port_of() { if [ "$1" = blue ]; then echo "$BLUE_PORT"; else echo "$GREEN_PORT"; fi; }
admin_of() { if [ "$1" = blue ]; then echo "$BLUE_ADMIN"; else echo "$GREEN_ADMIN"; fi; }

# healthy waits until the colour's full /healthz (its admin listener; the API port for a siteapi
# without one) is ok and names the commit ($2, default $COMMIT), and its answers are 200.
healthy() {
	local base admin deadline=$((SECONDS + HEALTH_TIMEOUT)) path want=${2:-$COMMIT}
	base=http://127.0.0.1:$(port_of "$1")
	admin=http://127.0.0.1:$(admin_of "$1")
	if $DRY; then plan "wait until $admin/healthz is ok and names $want"; return 0; fi
	until { curl -fsS -m 5 "$admin/healthz" || curl -fsS -m 5 "$base/healthz"; } 2>/dev/null |
		jq -e --arg c "$want" '.status == "ok" and .unserved == [] and .lastError == null and .binary.revision == $c' >/dev/null; do
		[ "$SECONDS" -lt "$deadline" ] || return 1
		sleep 2
	done
	for path in /v1/version '/v1/rankings?range=24h' /v1/networks/shifter '/v1/overlap?range=30d' '/v1/search?q=shi' /v1/dataset; do
		curl -fsSL -m 30 -o /dev/null "$base$path" || { echo "server-setup: $path failed" >&2; return 1; }
	done
}

# site writes this environment's Caddy site, then starts Caddy or, only when a file changed, reloads
# it. The site forwards both listeners (HTTPS and the CDN's plain HTTP) to both colours, whichever
# is healthy (site.caddy), so a colour switch changes no file and never reloads Caddy.
site() {
	local key="" changed=$CADDY_RELOAD
	# An older commit's template names one colour: refuse it before writing anything.
	grep -q @GREEN_PORT@ "$TPL/site.caddy" || die "$TPL/site.caddy forwards to one colour only (older than #1273); nothing was changed"
	if [ -e "$PRIV/origin-key" ]; then key=$(<"$PRIV/origin-key"); fi
	render site.caddy "$key" | put "$CADDY/sites/$ENV.caddy" 600 && changed=true
	if $changed && ! $DRY; then
		XDG_DATA_HOME=$CADDY/data XDG_CONFIG_HOME=$CADDY/config caddy validate --config "$CADDY/Caddyfile" --adapter caddyfile >/dev/null 2>&1 || die "caddy validate failed for $CADDY/Caddyfile"
	fi
	if $DRY; then plan "Caddy site :$SITE_PORT and plain HTTP :$HTTP_PORT forward to the healthy colour (blue 127.0.0.1:$BLUE_PORT, then green 127.0.0.1:$GREEN_PORT)"; fi
	if ! systemctl --user is-active --quiet atlas-caddy; then
		run systemctl --user enable --now atlas-caddy
	elif $changed; then
		run systemctl --user reload atlas-caddy
	fi
}

# start_held starts a colour ($2: start or restart) with its hold file, so Caddy sends it nothing
# while its checks run: siteapi's public /healthz answers 503 held while the file exists.
start_held() { # <colour> <start|restart>
	run touch "$API/hold-$1"
	run systemctl --user "$2" "$NAME-api@$1"
}

# release removes a colour's hold file after its checks, checks that its public /healthz (Caddy's
# health check) answers 200, then waits for Caddy's passing checks.
release() {
	run rm -f "$API/hold-$1"
	if ! $DRY; then curl -fsS -m 5 -o /dev/null "http://127.0.0.1:$(port_of "$1")/healthz" || return 1; fi
	run sleep "$HANDOVER_SECONDS"
}

# deploy follows docs/operations/api-deploy.md: start the idle colour held, wait for its checks,
# release it to Caddy, stop the old colour (Caddy moves its traffic, retrying), then check the
# public answers.
deploy() {
	local old new
	old=$(cat "$API/serving" 2>/dev/null || true)
	if [ -n "$old" ] && [ "$(readlink "$API/$old")" = "$COMMIT" ]; then
		if [ -e "$API/hold-$old" ]; then run rm -f "$API/hold-$old"; fi
		systemctl --user is-active --quiet "$NAME-api@$old" || run systemctl --user start "$NAME-api@$old"
		retire "$(other "$old")"
		return 0
	fi
	new=blue
	[ "$old" = blue ] && new=green
	run ln -sfn "$COMMIT" "$API/$new"
	start_held "$new" restart
	if ! healthy "$new" || ! release "$new"; then
		run systemctl --user stop "$NAME-api@$new"
		die "$NAME-api@$new did not become healthy on $COMMIT within ${HEALTH_TIMEOUT}s; nothing was switched"
	fi
	if [ -n "$old" ]; then run systemctl --user stop "$NAME-api@$old"; fi
	echo "$new" | put "$API/serving" 644 || true
	run systemctl --user enable "$NAME-api@$new"
	self_check || rollback "$old" "$new"
	if [ -n "$old" ]; then run systemctl --user disable "$NAME-api@$old"; fi
}

# self_check asks the public URL (Caddy's local plain HTTP listener without $PRIV/public-url) for
# the version and the biggest answer 20 times over 30 s, then /healthz; it fails on any answer
# that is not 200 or a /healthz that does not name the commit.
self_check() {
	local base=${PUBLIC:-http://127.0.0.1:$HTTP_PORT} i path code bad=0 n=0 line
	if $DRY; then plan "self-check: /v1/version and /v1/home?range=7d through $base, $SELF_CHECK_ROUNDS times over 30 s, each 200, then /healthz names $COMMIT"; return 0; fi
	for ((i = 1; i <= SELF_CHECK_ROUNDS; i++)); do
		for path in /v1/version '/v1/home?range=7d'; do
			code=$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "$base$path" 2>/dev/null) || true
			n=$((n + 1))
			[ "$code" = 200 ] || { bad=$((bad + 1)); echo "server-setup: self-check: $path answered ${code:-000}" >&2; }
		done
		[ "$i" -eq "$SELF_CHECK_ROUNDS" ] || sleep 1.5
	done
	n=$((n + 1)) # /healthz is never cached: through the CDN too it names the colour that answers
	curl -fsS -m 10 "$base/healthz" 2>/dev/null | jq -e --arg c "$COMMIT" '.revision == $c' >/dev/null ||
		{ bad=$((bad + 1)); echo "server-setup: self-check: /healthz does not name $COMMIT" >&2; }
	line="$(date -u +%FT%TZ) $COMMIT self-check $n requests, $bad failed"
	echo "$line" >>"$API/deploys.log"
	say "$line"
	[ "$bad" -eq 0 ]
}

# rollback puts the old colour back after a failed self-check: it starts it held on its own commit,
# waits for its checks and Caddy's, then stops the new colour (Caddy moves the traffic) and fails.
rollback() { # <old> <new>
	local was
	[ -n "$1" ] || die "the self-check failed on the first install; $NAME-api@$2 keeps serving"
	was=$(readlink "$API/$1")
	start_held "$1" start
	{ healthy "$1" "$was" && release "$1"; } || die "the self-check failed and $NAME-api@$1 did not come back on $was; $NAME-api@$2 keeps serving"
	echo "$1" | put "$API/serving" 644 || true
	run systemctl --user enable "$NAME-api@$1"
	run systemctl --user stop "$NAME-api@$2"
	run systemctl --user disable "$NAME-api@$2"
	echo "$(date -u +%FT%TZ) $COMMIT rolled back to $was" >>"$API/deploys.log"
	die "the self-check failed on $COMMIT: rolled back to $NAME-api@$1 ($was)"
}

other() { if [ "$1" = blue ]; then echo green; else echo blue; fi; }

# retire stops and disables a colour that is not serving (also one a cut-off run left running).
retire() {
	if systemctl --user is-active --quiet "$NAME-api@$1" || systemctl --user is-enabled --quiet "$NAME-api@$1"; then
		run systemctl --user stop "$NAME-api@$1"
		run systemctl --user disable "$NAME-api@$1"
	fi
}

main() {
	exec </dev/null # nothing below reads the caller's stdin (the script may come over it)
	ensure_dir 755 "$ROOT" "$BIN" "$API"
	if ! $DRY; then
		exec 9>>"$ROOT/.setup.lock"
		flock -n 9 || die "another server-setup.sh is running"
		WORK=$(mktemp -d)
		trap 'rm -rf "$WORK"' EXIT
	fi
	tool_uv
	tool_python
	tool_go
	tool_caddy
	tool_psql
	code
	TPL=$SRC/scripts/server-setup
	[ -d "$TPL" ] || TPL=$SELF_DIR/server-setup
	[ -d "$TPL" ] || die "no templates in $TPL"
	build
	worker_wheel
	secrets
	db_tls
	units
	site
	deploy
	# Slack alerts for production only: staging installs none and removes none (the owner, 2026-11-14).
	if [ "$ENV" = production ] && [ -f "$TPL/../server-alerts.sh" ]; then run bash "$TPL/../server-alerts.sh" install "$ENV"; fi
	say "$ENV serves $COMMIT"
}

main

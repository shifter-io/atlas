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
GO_VERSION=1.27.3
GO_SHA256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
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
production) NAME=atlas; BLUE_PORT=8090; GREEN_PORT=8091; SITE_PORT=8445 ;;
staging) NAME=atlas-staging; BLUE_PORT=9090; GREEN_PORT=9091; SITE_PORT=9445 ;;
*) usage ;;
esac
[ "$(id -u)" -ne 0 ] || die "refusing to run as root: run it as the normal user"

ROOT=${ATLAS_ROOT:-$HOME/atlas}
CONF=${ATLAS_CONFIG:-$HOME/.config/atlas}
UNITS=${ATLAS_UNIT_DIR:-$HOME/.config/systemd/user}
REPO=${ATLAS_REPO_URL:-$ROOT/repo.git}
HEALTH_TIMEOUT=${ATLAS_HEALTH_TIMEOUT:-900}
DRAIN_SECONDS=${ATLAS_DRAIN_SECONDS:-60}
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
	t=${t//@BIN@/$BIN}; t=${t//@CADDY@/$CADDY}; t=${t//@SITE_PORT@/$SITE_PORT}
	t=${t//@COLOUR@/${2:-}}; t=${t//@UPSTREAM@/${3:-}}; t=${t//@KEY@/${4:-}}
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
	(cd "$SRC" && run uv sync -q --locked --no-dev --no-editable --python "$PYTHON_VERSION")
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

# secrets checks the API's database file and makes the CDN's origin key when missing.
secrets() {
	ensure_dir 700 "$CONF" "$PRIV"
	if [ ! -e "$PRIV/origin-key" ]; then
		if $DRY; then plan "create $PRIV/origin-key (32 random bytes)"; else (umask 077 && openssl rand -hex 32 >"$PRIV/origin-key"); fi
	fi
	if [ -e "$PRIV/origin-key" ] || ! $DRY; then private_file "$PRIV/origin-key"; fi
	private_file "$PRIV/database-api.toml"
}

# units writes the API template, the colours' ports, Caddy's unit and the shared Caddyfile.
units() {
	local changed=false
	render api@.service | put "$UNITS/$NAME-api@.service" 644 && changed=true
	render atlas-caddy.service | put "$UNITS/atlas-caddy.service" 644 && changed=true
	printf 'ATLAS_API_LISTEN=127.0.0.1:%s\n' "$BLUE_PORT" | put "$API/blue.env" 644 || true
	printf 'ATLAS_API_LISTEN=127.0.0.1:%s\n' "$GREEN_PORT" | put "$API/green.env" 644 || true
	render Caddyfile | put "$CADDY/Caddyfile" 644 && CADDY_RELOAD=true
	ensure_dir 700 "$CADDY/sites"
	ensure_dir 755 "$API/files-blue" "$API/files-green"
	if $changed; then run systemctl --user daemon-reload; fi
}

port_of() { if [ "$1" = blue ]; then echo "$BLUE_PORT"; else echo "$GREEN_PORT"; fi; }

# healthy waits until the colour's /healthz is ok and names the commit, and its answers are 200.
healthy() {
	local base deadline=$((SECONDS + HEALTH_TIMEOUT)) path
	base=http://127.0.0.1:$(port_of "$1")
	if $DRY; then plan "wait until $base/healthz is ok and names $COMMIT"; return 0; fi
	until curl -fsS -m 5 "$base/healthz" 2>/dev/null |
		jq -e --arg c "$COMMIT" '.status == "ok" and .unserved == [] and .lastError == null and .binary.revision == $c' >/dev/null; do
		[ "$SECONDS" -lt "$deadline" ] || return 1
		sleep 5
	done
	for path in /v1/version '/v1/rankings?range=24h' /v1/networks/shifter '/v1/overlap?range=30d' /v1/search-index /v1/dataset; do
		curl -fsSL -m 30 -o /dev/null "$base$path" || { echo "server-setup: $path failed" >&2; return 1; }
	done
}

# site points this environment's Caddy site at the colour, then starts or reloads Caddy. A switch
# ($2 = switch) always validates and reloads: an earlier cut-off run may have written the file only.
site() {
	local key="" changed=$CADDY_RELOAD
	[ "${2:-}" = switch ] && changed=true
	if [ -e "$PRIV/origin-key" ]; then key=$(<"$PRIV/origin-key"); fi
	render site.caddy "$1" "$(port_of "$1")" "$key" | put "$CADDY/sites/$ENV.caddy" 600 && changed=true
	if $changed && ! $DRY; then
		XDG_DATA_HOME=$CADDY/data XDG_CONFIG_HOME=$CADDY/config caddy validate --config "$CADDY/Caddyfile" --adapter caddyfile >/dev/null 2>&1 || die "caddy validate failed for $CADDY/Caddyfile"
	fi
	if $DRY; then plan "Caddy site :$SITE_PORT forwards to $1 (127.0.0.1:$(port_of "$1"))"; fi
	if ! systemctl --user is-active --quiet atlas-caddy; then
		run systemctl --user enable --now atlas-caddy
	elif $changed; then
		run systemctl --user reload atlas-caddy
	fi
}

# deploy follows docs/operations/api-deploy.md: start the idle colour, wait, switch, drain, stop.
deploy() {
	local old new
	old=$(cat "$API/serving" 2>/dev/null || true)
	if [ -n "$old" ] && [ "$(readlink "$API/$old")" = "$COMMIT" ]; then
		systemctl --user is-active --quiet "$NAME-api@$old" || run systemctl --user start "$NAME-api@$old"
		site "$old"
		retire "$(other "$old")"
		return 0
	fi
	new=blue
	[ "$old" = blue ] && new=green
	run ln -sfn "$COMMIT" "$API/$new"
	run systemctl --user restart "$NAME-api@$new"
	if ! healthy "$new"; then
		run systemctl --user stop "$NAME-api@$new"
		die "$NAME-api@$new did not become healthy on $COMMIT within ${HEALTH_TIMEOUT}s; nothing was switched"
	fi
	site "$new" switch
	echo "$new" | put "$API/serving" 644 || true
	run systemctl --user enable "$NAME-api@$new"
	[ -n "$old" ] || return 0
	run sleep "$DRAIN_SECONDS"
	run systemctl --user stop "$NAME-api@$old"
	run systemctl --user disable "$NAME-api@$old"
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
	secrets
	units
	deploy
	if [ -f "$TPL/../server-alerts.sh" ]; then run bash "$TPL/../server-alerts.sh" install "$ENV"; fi
	say "$ENV serves $COMMIT"
}

main

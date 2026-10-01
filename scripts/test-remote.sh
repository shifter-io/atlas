#!/usr/bin/env bash
# Local source is authoritative. Each run gets a new, isolated remote copy.
set -euo pipefail
cd "$(dirname "$0")/.."
remote_host=${REMOTE_HOST:?Set REMOTE_HOST to an SSH alias or user@host}
# Conservative validation keeps shell/rsync interpolation unambiguous.
[[ "$remote_host" =~ ^[a-zA-Z0-9_.@-]+$ ]] || { echo "Invalid REMOTE_HOST" >&2; exit 2; }
run_stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
remote_dir="proxy-framework-tests/$run_stamp"
evidence="artifacts/remote-$run_stamp"
mkdir -p "$evidence"
ssh -o BatchMode=yes "$remote_host" "mkdir -p '$remote_dir'"
rsync -az --exclude=.venv --exclude=.git --exclude=artifacts --exclude=results \
  --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude='.coverage*' --exclude='.env*' --exclude='*.mmdb' --exclude=dist \
  --exclude='.secrets' --exclude='.state' --exclude='credentials.toml' --exclude='database.toml' \
  ./ "$remote_host:$remote_dir/"
if [[ "${INCLUDE_MMDB:-0}" == 1 ]]; then
  rsync -az db-ip/ "$remote_host:$remote_dir/db-ip/"
fi
remote_status=0
ssh -o BatchMode=yes "$remote_host" "cd '$remote_dir' && bash scripts/remote-check.sh" \
  >"$evidence/remote.log" 2>&1 || remote_status=$?
rsync -az "$remote_host:$remote_dir/artifacts/" "$evidence/" || true
cat "$evidence/remote.log"
exit "$remote_status"

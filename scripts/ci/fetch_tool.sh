#!/usr/bin/env bash
# PUBLIC-CI-1: download a pinned tool archive, check its SHA-256 and unpack it into <dest>, for
# the public run on GitHub's runners (public.yml).
# Usage: fetch_tool.sh <url> <sha256> <dest> [leading directories to strip, default 1]
# An existing <dest>/.complete is kept as it is; a failed download or check leaves no <dest>.
set -euo pipefail
[ $# -ge 3 ] || { echo "usage: fetch_tool.sh <url> <sha256> <dest> [strip]" >&2; exit 2; }
url=$1 sum=$2 dest=$3 strip=${4:-1}
[ -e "$dest/.complete" ] && exit 0
mkdir -p "$(dirname "$dest")"
tmp=$(mktemp -d "$(dirname "$dest")/.tmp.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp/a.tgz" "$url"
echo "$sum  $tmp/a.tgz" | sha256sum -c --quiet -
mkdir "$tmp/x"
tar -C "$tmp/x" --strip-components="$strip" -xzf "$tmp/a.tgz"
touch "$tmp/x/.complete"
mv -T "$tmp/x" "$dest"

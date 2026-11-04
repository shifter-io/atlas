#!/bin/sh
# The browser runner's live check (BP-RUNNER): the real pinned Camoufox against the local fake
# site and authenticated proxy (tests/test_browser_live.py). Run remotely only: on a disposable
# Linux server, and on macOS through the shared pool's Mac class. From the repository root.
#
# Browser hosts install with --no-dev (the dev group's cryptography has no macOS x86_64 wheel),
# so pytest and its plugins are added with `uv run --with` at their exact uv.lock versions.
set -eu

# The same uv and CPython as .github/workflows/ci.yml (some pinned wheels exist only for cp312).
UV_VERSION=0.12.10
PYTHON_VERSION=3.12.14
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) asset=x86_64-unknown-linux-gnu sha=173d95a0c32d18c896c46ba6fafbf3cf9c14ab74b033f81b76c883ef492a976b ;;
  Linux-aarch64) asset=aarch64-unknown-linux-gnu sha=9ff6b9d4665edcdd3a88dcc73cd1eb641754deb927f14e8c62ebfde6bf4f5f5e ;;
  Darwin-x86_64) asset=x86_64-apple-darwin sha=5296d5aa2b9143360405eea866f8ef4d5dc8986b164eb0dc35e8f876a9304d30 ;;
  Darwin-arm64) asset=aarch64-apple-darwin sha=51c6170e8e3a01cef9f33b94f582b7b81ac65046f55d40afb35f9cff5a68c179 ;;
  *) echo "browser_live: no pinned uv for $(uname -s)-$(uname -m)" >&2; exit 2 ;;
esac

tools="${PROXY_INDEX_TOOLS:-$HOME/.cache/proxy-index/tools}/uv-$UV_VERSION"
if ! command -v uv >/dev/null 2>&1 || [ "$(uv --version | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  if [ ! -x "$tools/uv" ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/uv.tgz" \
      "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$asset.tar.gz"
    if command -v sha256sum >/dev/null 2>&1; then
      echo "$sha  $tmp/uv.tgz" | sha256sum -c --quiet -
    else
      echo "$sha  $tmp/uv.tgz" | shasum -a 256 -c --quiet -
    fi
    mkdir -p "$tools"
    tar -xzf "$tmp/uv.tgz" -C "$tmp"
    mv "$tmp/uv-$asset/uv" "$tmp/uv-$asset/uvx" "$tools/"
    rm -rf "$tmp"
  fi
  PATH="$tools:$PATH"
  export PATH
fi

uv sync --locked --no-dev --extra browser --python "$PYTHON_VERSION"
PROXY_INDEX_BROWSER_DIR="${PROXY_INDEX_BROWSER_DIR:-$HOME/.cache/proxy-index/browser}"
export PROXY_INDEX_BROWSER_DIR
uv run --no-sync proxy-index browser fetch --dest "$PROXY_INDEX_BROWSER_DIR"

# pytest, its plugins and everything they depend on, at their uv.lock versions.
pins=$(uv run --no-sync python - <<'EOF'
import tomllib
packages = {p["name"]: p for p in tomllib.load(open("uv.lock", "rb"))["package"]}
todo, seen = ["pytest", "pytest-asyncio", "pytest-timeout"], set()
while todo:
    name = todo.pop()
    if name not in seen:
        seen.add(name)
        todo += [d["name"] for d in packages[name].get("dependencies", [])]
print(" ".join(f"--with {name}=={packages[name]['version']}" for name in sorted(seen)))
EOF
)
log="${TMPDIR:-/tmp}/browser-live.log"
status=0
# shellcheck disable=SC2086 # $pins is a list of --with options
uv run --no-sync $pins pytest -p no:cacheprovider -m browser_live -rs -s \
  tests/test_browser_live.py >"$log" 2>&1 || status=$?
cat "$log"
# A live check that skipped a test proves nothing.
if [ "$status" -eq 0 ] && grep -Eq '[0-9]+ skipped' "$log"; then
  echo "browser_live: tests were skipped" >&2
  status=1
fi
exit "$status"

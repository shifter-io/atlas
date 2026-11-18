#!/usr/bin/env bash
# Rename the product's old name to Atlas in this checkout (RENAME-ATLAS-1): every path and text
# outside scripts/rename_to_atlas.keep. Idempotent; run it again on any branch after the rename
# landed. Options: --dry-run,
# --check (list what is left; exit 1 if anything), --report (counts of kept and rewritten names).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# The first Python 3 that runs (a stale python3 earlier on PATH may not).
for python in python3 /usr/bin/python3; do
  if "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    exec "$python" "$here/rename_to_atlas.py" "$@"
  fi
done
echo "rename_to_atlas.sh: no Python 3.9 or later found" >&2
exit 2

#!/usr/bin/env bash
# Rename the product's old name to Atlas in this checkout (RENAME-ATLAS-1): every path and text
# outside scripts/rename_to_atlas.keep. Idempotent; run it again on any branch after the rename
# landed (docs/plans/atlas-rename.md, "Bringing a branch up to date"). Options: --dry-run,
# --check (list what is left; exit 1 if anything), --report (counts of kept and rewritten names).
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/rename_to_atlas.py" "$@"

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
mkdir -p artifacts
uv sync --locked --no-editable --python 3.12
uv run --no-editable ruff check src tests scripts > artifacts/lint.txt
uv run --no-editable pytest --junitxml=artifacts/tests.xml \
  --cov=proxy_index --cov-branch --cov-report=term-missing \
  --cov-report=json:artifacts/coverage.json > artifacts/tests.txt
uv run --no-editable python scripts/benchmark.py > artifacts/benchmark.txt


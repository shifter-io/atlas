source /etc/profile.d/proxy-index.sh
useradd -m ci; cp -r "$PWD" /home/ci/w; chown -R ci /home/ci/w
cat > /home/ci/run.sh <<'IN'
source /etc/profile.d/proxy-index.sh
cd /home/ci/w
uv sync --locked --no-editable >/dev/null 2>&1
uv run --no-editable python scripts/postgres_local.py init --bin-dir $PG_BIN --port 55432 --ephemeral 2>&1 | tail -3
export PG_TEST_ADMIN_FILE=$PWD/.secrets/postgres-admin.toml PG_TEST_REQUIRED=1
uv run --no-editable pytest -n 4 -rA -p no:cacheprovider tests/test_calc_run_lock.py tests/test_cov_orchestrator_scheduler.py tests/test_score_backfill*.py tests/test_orchestrator*.py tests/test_calculator*.py > /tmp/out.txt 2>&1
grep -E "PASSED.*calc_run_lock|FAILED|ERROR|passed|failed|skipped" /tmp/out.txt | tail -60
IN
su ci -s /bin/bash /home/ci/run.sh 2>&1
echo END

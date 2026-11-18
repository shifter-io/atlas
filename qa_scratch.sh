#!/bin/bash
if [ "$(id -u)" = 0 ]; then
  id qa >/dev/null 2>&1 || useradd -m qa
  rm -rf /home/qa/w; cp -a "$PWD" /home/qa/w; chown -R qa /home/qa/w
  exec runuser -u qa -- env PATH="/opt/proxy-index/uv:$PATH" HOME=/home/qa bash /home/qa/w/qa_scratch.sh
fi
cd /home/qa/w
uv sync --locked --no-editable >/dev/null 2>&1
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')
uv run --no-editable python scripts/postgres_local.py init --bin-dir /opt/proxy-index/postgresql/bin --port $PORT --ephemeral >/dev/null 2>&1
export PG_TEST_ADMIN_FILE=$PWD/.secrets/postgres-admin.toml
echo "== BEFORE (plain fork Pool, no close/join)"
python3 - <<'PY'
p="src/atlas/calculated/parallel_measure.py"
t=open(p).read()
t=t.replace('Pool(n, _started, (blocked,))','Pool(n)')
t=t.replace("            if self.FORK:  # each worker ends at the end of its queue: no signal needed\n                pool.close()\n                pool.join()\n","")
open(p,"w").write(t)
PY
grep -n "pool = \|pool.close" src/atlas/calculated/parallel_measure.py
uv run --no-editable pytest tests/test_calc_parallel_measure.py -k "sigterm or end_after" --timeout=30 -p no:faulthandler 2>&1 | tail -15

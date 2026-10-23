#!/usr/bin/env bash
# Provision the CAMP-1 worker image on a fresh ubuntu-24-04-x64 builder, as root.
# Inputs, next to this script: requirements.txt (hash-locked, from `uv export`) and one wheel.
# The last line of output is the hygiene verdict: "clean", or "dirty: <reasons>" and exit 1.
# The image holds no keys, host keys, passwords or credentials.
set -euo pipefail

UV_VERSION="0.12.10"
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="173d95a0c32d18c896c46ba6fafbf3cf9c14ab74b033f81b76c883ef492a976b"
WORKER_ROOT="/opt/proxy-index-worker"
MASKED="snapd.service snapd.socket apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer unattended-upgrades.service motd-news.service motd-news.timer"
HERE="$(cd "$(dirname "$0")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends python3.12-venv systemd-timesyncd ca-certificates curl

curl -fsSL --retry 3 -o /tmp/uv.tar.gz "$UV_URL"
echo "${UV_SHA256}  /tmp/uv.tar.gz" | sha256sum -c -
tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
  uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx
rm -f /tmp/uv.tar.gz

mkdir -p "$WORKER_ROOT"
python3.12 -m venv "$WORKER_ROOT/venv"
uv pip install --python "$WORKER_ROOT/venv/bin/python" --require-hashes -r "$HERE/requirements.txt"
wheel="$(ls "$HERE"/proxy_index-*.whl)"
uv pip install --python "$WORKER_ROOT/venv/bin/python" --no-deps "$wheel"
sha256sum "$wheel" | cut -d' ' -f1 > "$WORKER_ROOT/wheel.sha256"
"$WORKER_ROOT/venv/bin/proxy-index" --help > /dev/null

# Quiet, predictable boots: no snaps, no background apt, no motd fetches; the clock stays synced.
# shellcheck disable=SC2086
systemctl mask $MASKED
systemctl enable systemd-timesyncd
cat > /etc/cloud/cloud.cfg.d/90-pi-camp.cfg <<'CFG'
datasource_list: [ DigitalOcean ]
CFG
# sshd keeps the first value it reads and reads drop-ins in name order, so ours comes before
# 50-cloud-init.conf, which cloud-init writes on every worker's first boot.
cat > /etc/ssh/sshd_config.d/01-pi-camp.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
AuthenticationMethods publickey
SSHD
# The effective settings, read while the host keys still exist (sshd -T needs them).
SSHD_EFFECTIVE="$(/usr/sbin/sshd -T 2>/dev/null || true)"

# Strip every key, password and trace; each worker gets new host keys and its key at first boot.
passwd -l root > /dev/null
rm -rf "$HERE" /root/.cache /var/lib/apt/lists/*
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys \
  /root/.bash_history /root/.wget-hsts
apt-get clean
cloud-init clean --logs --machine-id
journalctl --rotate > /dev/null 2>&1 || true
journalctl --vacuum-time=1s > /dev/null 2>&1 || true
# The build's SSH logins (with the Mac's address) must not travel into every worker.
LOGIN_LOGS="/var/log/auth.log /var/log/wtmp /var/log/btmp /var/log/lastlog /var/log/syslog"
for log in $LOGIN_LOGS; do
  if [ -f "$log" ]; then : > "$log"; fi
done

hygiene() {
  local reasons=()
  compgen -G "/etc/ssh/ssh_host_*" > /dev/null && reasons+=("host keys")
  compgen -G "/root/.ssh/authorized_keys" > /dev/null && reasons+=("root authorized_keys")
  compgen -G "/home/*/.ssh/authorized_keys" > /dev/null && reasons+=("user authorized_keys")
  [ "$(passwd -S root | cut -d' ' -f2)" = "L" ] || reasons+=("root password")
  [ -e "$HERE" ] && reasons+=("build inputs")
  [ -e /run/proxy-index-worker ] && reasons+=("worker secrets")
  secrets="$(find / -xdev \( -name 'credentials*.toml' -o -name 'do-token' \) -print -quit \
    2>/dev/null || true)"
  [ -n "$secrets" ] && reasons+=("credential files")
  dpkg -s droplet-agent > /dev/null 2>&1 && reasons+=("droplet agent")
  for unit in $MASKED; do
    [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = "masked" ] || reasons+=("$unit")
  done
  [ -x "$WORKER_ROOT/venv/bin/proxy-index" ] || reasons+=("venv")
  grep -qx "passwordauthentication no" <<< "$SSHD_EFFECTIVE" || reasons+=("sshd passwords")
  grep -qx "authenticationmethods publickey" <<< "$SSHD_EFFECTIVE" || reasons+=("sshd methods")
  for log in $LOGIN_LOGS; do
    if [ -s "$log" ]; then reasons+=("login trace $log"); fi
  done
  if [ ${#reasons[@]} -eq 0 ]; then
    echo clean
  else
    echo "dirty: ${reasons[*]}"
    return 1
  fi
}
sync
hygiene

#!/usr/bin/env bash
# Provision or update the CAMP-1 manager (ubuntu-24-04-x64; lon1, or the region `deploy --region`
# chose), as root.
# Inputs, next to this script: requirements.txt (hash-locked, from `uv export`), one wheel,
# mac.pub (the only key allowed to log in) and units/ (the campaign's systemd units).
# Output: one line "worker-key <public key>", then the verdict as the last line:
# "hardened", or "weak: <reasons>" and exit 1. No token or credential passes through here.
set -euo pipefail

BASE="/opt/atlas-campaign"
HERE="$(cd "$(dirname "$0")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q > /dev/null
apt-get install -y -q --no-install-recommends python3.12-venv systemd-timesyncd \
  unattended-upgrades ca-certificates > /dev/null
# No web console: the droplet agent stays off the manager even if the image carried it.
if dpkg -s droplet-agent > /dev/null 2>&1; then apt-get purge -y -q droplet-agent > /dev/null; fi

# Security updates stay on; the manager never reboots by itself mid-campaign.
cat > /etc/apt/apt.conf.d/52pi-camp-no-reboot <<'APT'
Unattended-Upgrade::Automatic-Reboot "false";
APT
systemctl enable --now unattended-upgrades systemd-timesyncd > /dev/null 2>&1

# Key-only SSH for root, read before 50-cloud-init.conf (sshd keeps the first value).
cat > /etc/ssh/sshd_config.d/01-pi-camp.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
AuthenticationMethods publickey
SSHD
install -d -m 700 /root/.ssh
install -m 600 "$HERE/mac.pub" /root/.ssh/authorized_keys
passwd -l root > /dev/null
/usr/sbin/sshd -t
# Ubuntu 24.04 socket-activates sshd: a running daemon reloads, a later one reads the file.
systemctl try-reload-or-restart ssh

umask 077
install -d -m 755 "$BASE" "$BASE/wheels"
install -d -m 700 "$BASE/.secrets" "$BASE/.secrets/credentials" "$BASE/campaigns"
[ -d "$BASE/venv" ] || python3.12 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q --require-hashes -r "$HERE/requirements.txt"
wheel="$(ls "$HERE"/atlas-*.whl)"
"$BASE/venv/bin/pip" install -q --no-deps --force-reinstall "$wheel"
install -m 644 "$wheel" "$BASE/wheels/"
"$BASE/venv/bin/atlas" --help > /dev/null
[ -f "$BASE/.secrets/worker-key" ] || ssh-keygen -q -t ed25519 -N "" -C atlas-camp-worker \
  -f "$BASE/.secrets/worker-key"
install -m 644 "$HERE"/units/* /etc/systemd/system/
systemctl daemon-reload
echo "worker-key $(cut -d' ' -f1,2 "$BASE/.secrets/worker-key.pub")"
rm -rf "$HERE"

verdict() {
  local reasons=() effective
  effective="$(/usr/sbin/sshd -T 2>/dev/null || true)"
  grep -qx "passwordauthentication no" <<< "$effective" || reasons+=("sshd passwords")
  grep -qx "kbdinteractiveauthentication no" <<< "$effective" || reasons+=("sshd keyboard")
  grep -qx "permitrootlogin without-password" <<< "$effective" || reasons+=("sshd root")
  grep -qx "authenticationmethods publickey" <<< "$effective" || reasons+=("sshd methods")
  [ "$(wc -l < /root/.ssh/authorized_keys)" -eq 1 ] || reasons+=("authorized_keys")
  [ "$(passwd -S root | cut -d' ' -f2)" = "L" ] || reasons+=("root password")
  dpkg -s droplet-agent > /dev/null 2>&1 && reasons+=("droplet agent")
  [ "$(stat -c %a "$BASE/.secrets")" = "700" ] || reasons+=("secrets mode")
  if [ ${#reasons[@]} -eq 0 ]; then
    echo hardened
  else
    echo "weak: ${reasons[*]}"
    return 1
  fi
}
verdict

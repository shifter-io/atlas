"""MANAGER-HOST-1: `campaign manager deploy --host <address or ssh alias> [--ssh-user <user>]`
installs the manager on an existing server, as that normal user, beside the other environment.

No droplet is created or read, nothing moves into a project, no manager firewall is built and
there is no hardening step: root and `authorized_keys` are never touched. An ssh alias is used
as it is (its config gives the port, user and key; `ssh -G` gives its address); an address uses
the Mac key and `--ssh-port`. The deploy streams the secrets over SSH stdin into
`~/.config/atlas/<env>/` (0600), pushes the commit into the server's bare repository
`~/atlas/repo.git` (the server has no GitHub access), sends the repository's
`scripts/server-setup.sh` (with `scripts/server-setup/`) and runs it for the environment and
commit (SERVER-SETUP-1), installs the wheel into `~/atlas/<env>/venv` when the setup has not,
makes the worker key and admits SSH from the server's address in the environment's worker
firewall. The inventory records `manager_kind: host`, `host`, `ssh_user` and `environment`.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..onboarding.files import digest
from . import deploy, host_layout, manager_daily, ops_api
from .deploy import DeployError, DeployJob, Manager
from .host_layout import HOST, Layout
from .hostkeys import known_hosts, known_hosts_option
from .image import Shell, run_shell

SETUP = Path("scripts/server-setup.sh")
USER_RE = r"[a-z_][a-z0-9_-]{0,31}"
REF_RE = r"[0-9a-f]{7,40}"
ALIAS_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
REPO = "atlas/repo.git"  # the setup script's source, relative to the user's home
HOST_KEYS = ("manager_kind", "environment", "worker_firewall_id", "worker_key_name")
HOST_KEYS += ("worker_key_id", "wheel_sha256", "credentials", "deployed_at")
KEY_RE = r"worker-key ssh-ed25519 [A-Za-z0-9+/=]+"


@dataclass(frozen=True)
class HostTarget:
    """The existing server (its public IPv4, or an ssh alias), its normal user, the commit
    (default: this checkout's HEAD), the setup script and an ssh port for an address."""

    host: str
    ssh_user: str | None = None
    ref: str | None = None
    setup: Path = SETUP
    ssh_port: int | None = None


async def resolved(target: HostTarget, shell: Shell) -> tuple[dict[str, Any], str]:
    """The inventory's ssh fields (address, alias, user, port) and the commit, all checked."""
    alias, user = None, target.ssh_user
    try:
        address = ipaddress.IPv4Address(target.host)
    except ValueError:
        if not re.fullmatch(ALIAS_RE, target.host):
            raise DeployError("--host must be a public IPv4 or an ssh alias") from None
        _, out = await shell(["ssh", "-G", target.host], b"")
        found = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        try:
            address = ipaddress.IPv4Address(found.get("hostname", ""))
        except ValueError:
            raise DeployError("the ssh alias must name the server by its public IPv4") from None
        alias, user = target.host, user or found.get("user")
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise DeployError("--host must be a public IPv4")
    if not re.fullmatch(USER_RE, user or "") or user == "root":
        raise DeployError("--ssh-user must be the server's normal (non-root) user")
    if target.ssh_port is not None and not 0 < target.ssh_port < 65536:
        raise DeployError("--ssh-port must be a port number")
    if not target.setup.is_file():
        raise DeployError(f"{target.setup} is missing: run deploy from the repository")
    ref = target.ref or (await shell(["git", "rev-parse", "HEAD"], b""))[1].strip()
    if not re.fullmatch(REF_RE, ref):
        raise DeployError("--ref must be a commit hash")
    ssh = {"host": str(address), "ssh_alias": alias, "ssh_user": user, "ssh_port": target.ssh_port}
    return ssh, ref


def ssh_options(inventory: dict[str, Any], path: Path) -> list[str]:
    """ssh options for a host inventory: an alias brings its own key, port and host key;
    an address uses the Mac key and the inventory's own known-hosts file."""
    found = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if not inventory.get("ssh_alias"):
        found += ["-i", inventory["identity"], "-o", "IdentitiesOnly=yes"]
        found += ["-o", "StrictHostKeyChecking=accept-new"]
        found += ["-o", known_hosts_option(known_hosts(path, inventory))]
    if inventory.get("ssh_port"):
        found += ["-p", str(inventory["ssh_port"])]
    return found


async def push_commit(remote: Manager, ref: str) -> None:
    """Fill the server's bare repository (made once, shared by both environments) so its main
    holds `ref`, from this checkout. A commit its main already holds is not pushed: the other
    environment may run ahead, and main never moves backwards."""
    repo = f'"$HOME/{REPO}"'
    found = await remote.run(
        f"git init -q --bare {repo} && if git --git-dir {repo} merge-base --is-ancestor "
        f"{ref} refs/heads/main 2>/dev/null; then echo held; fi",
        "bare repository",
    )
    if found.strip() == "held":
        return
    ssh = shlex.join(["ssh", *ssh_options(remote.inventory, remote.path)])
    url = f"{host_layout.destination(remote.inventory)}:{REPO}"
    push = ["git", "push", "--quiet", url, f"{ref}:refs/heads/main"]
    code, _ = await remote.shell(["env", f"GIT_SSH_COMMAND={ssh}", *push], b"")
    if code != 0:
        raise DeployError(f"pushing the commit to the server's repository failed (exit {code})")


def setup_payload(setup: Path) -> bytes:
    """The setup script and its templates folder (`scripts/server-setup/`), as a tar."""
    found = [setup, *sorted(p for p in setup.with_suffix("").rglob("*") if p.is_file())]
    base = setup.parent.parent
    return deploy._tar([(p.relative_to(base).as_posix(), p.read_bytes(), 0o700) for p in found])


def setup_script(layout: Layout, target: HostTarget, ref: str) -> str:
    """Run the setup script from a private stage folder; it changes nothing on a second run."""
    return "\n".join(
        [
            "set -eu",
            'stage=$(mktemp -d "$HOME/atlas-setup-XXXXXX")',
            "trap 'rm -rf \"$stage\"' EXIT",
            'tar -x --no-same-owner -C "$stage"',
            'cd "$stage"',
            f"bash scripts/{target.setup.name} {layout.environment} --ref {ref}",
        ]
    )


def install_script(layout: Layout) -> str:
    """The folders (private 0700), the wheel in the venv unless the setup made it, the worker
    key (made once); prints `home <dir>`, `worker-key <public key>`, then `installed`."""
    root, private = layout.root, layout.private
    cli = layout.translate(manager_daily.PROXY_INDEX)
    uv, python = '"$HOME/atlas/bin/uv"', f'--python "{root}/venv/bin/python"'
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            f'mkdir -p "$HOME/.config/atlas" "{private}/credentials" "{root}/campaigns"',
            f'chmod 700 "$HOME/.config/atlas" "{private}" "{private}/credentials"',
            f'mkdir -p -m 755 "{root}/wheels" && ln -sfn "{private}" "{root}/.secrets"',
            'stage=$(mktemp -d "$HOME/atlas-deploy-XXXXXX")',
            "trap 'rm -rf \"$stage\"' EXIT",
            'tar -x --no-same-owner -C "$stage"',
            f'if [ ! -x "{cli}" ]; then',
            f'  {uv} venv --quiet --python 3.12 "{root}/venv"',
            f'  {uv} pip install --quiet {python} --require-hashes -r "$stage/requirements.txt"',
            f'  {uv} pip install --quiet {python} --no-deps "$stage"/*.whl',
            "fi",
            f'install -m 644 "$stage"/*.whl "{root}/wheels/"',
            f'"{cli}" --help > /dev/null',
            f'[ -f "{private}/worker-key" ] || ssh-keygen -q -t ed25519 -N "" '
            f'-C {layout.unit("worker")} -f "{private}/worker-key"',
            'echo "home $HOME"',
            f'echo "worker-key $(cut -d" " -f1,2 "{private}/worker-key.pub")"',
            "echo installed",
        ]
    )


def install_payload(job: DeployJob) -> bytes:
    return deploy._tar(
        [
            ("requirements.txt", job.requirements.read_bytes(), 0o600),
            (job.wheel.name, job.wheel.read_bytes(), 0o600),
        ]
    )


def _installed(out: str) -> tuple[str, str]:
    """(home folder, worker public key) from the install script's output."""
    lines = out.strip().splitlines() or [""]
    keys = [line.removeprefix("worker-key ") for line in lines if re.fullmatch(KEY_RE, line)]
    homes = [line.removeprefix("home ") for line in lines if re.fullmatch(r"home /\S+", line)]
    if lines[-1] != "installed" or len(keys) != 1 or len(homes) != 1:
        raise DeployError("the install on the server did not finish")
    return homes[0], keys[0]


def _recorded(job: DeployJob, address: str) -> dict[str, Any]:
    """The inventory, which must be empty or a host one (never a droplet manager's)."""
    inventory = deploy.load_inventory(job.inventory)
    if inventory and inventory.get("manager_kind") != HOST:
        raise DeployError("this inventory records a droplet manager: give the host its own")
    if inventory.get("host") not in (None, address):
        raise DeployError("this inventory records another server")
    if inventory.get("hourly", {}).get("on"):
        raise DeployError(manager_daily.BUSY)
    return inventory


async def deploy_host(
    cloud,
    job: DeployJob,
    target: HostTarget,
    *,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Install or update the environment's manager on the server; returns the recorded ids."""
    now, path = now or datetime.now(UTC), job.inventory
    ssh, ref = await resolved(target, shell)
    inventory = _recorded(job, ssh["host"])
    project_id = await deploy.check_account(cloud, inventory, job.account)
    layout = Layout(deploy.bound_environment(inventory, job.environment))
    deploy.save(
        path,
        inventory,
        manager_kind=HOST,
        **ssh,
        environment=layout.environment,
        account=job.account,
        project_id=project_id,
        manager_since=inventory.get("manager_since") or now.isoformat(),
        identity=str(job.mac_key),
        identity_file=str(job.mac_key),
        known_hosts=str(path.with_name(f"{path.stem}.known_hosts")),
    )
    remote = Manager(inventory, path, shell)
    await deploy.put_secrets(remote, job)  # the setup checks the private folder and files
    await push_commit(remote, ref)
    setup = setup_script(layout, target, ref)
    await remote.run(setup, "server setup", setup_payload(target.setup))
    out = await remote.run(install_script(layout), "install", install_payload(job))
    home, public = _installed(out)
    name = await deploy._worker_key_name(cloud, layout.environment, now, public)
    key_id, key_name, _ = await ops_api.ensure_ssh_key(cloud, name, public)
    rules = ops_api.worker_firewall(inventory["host"], layout.environment)
    firewall = await ops_api.apply_firewall(
        cloud, deploy.manager_scope(inventory), rules, inventory.get("worker_firewall_id")
    )
    deploy.save(
        path,
        inventory,
        worker_key_id=key_id,
        worker_key_name=key_name,
        worker_firewall_id=firewall,
        wheel_sha256=digest(job.wheel),
        framework_version=job.wheel.name.split("-")[1],
        credentials=sorted({*inventory.get("credentials", []), *job.credentials}),
        campaigns_root=f"{home}/atlas/{layout.environment}/campaigns",
        deployed_at=now.isoformat(),
    )
    return {key: inventory[key] for key in HOST_KEYS}


def allow_me() -> dict[str, Any]:
    """`allow-me` on a host inventory: there is no cloud firewall to change."""
    return {"manager_kind": HOST, "firewall": "the hosting provider's panel: nothing to change"}


def add_arguments(step) -> None:
    """The deploy options of the existing-server mode."""
    step.add_argument(
        "--host", help="an existing server's public IPv4 or ssh alias (MANAGER-HOST-1)"
    )
    step.add_argument("--ssh-user", help="with --host: the normal user (default: the alias's)")
    step.add_argument("--ssh-port", type=int, help="with --host: the ssh port of an address")
    step.add_argument("--ref", help="with --host: the commit to install (default: HEAD)")
    step.add_argument("--setup-script", type=Path, default=SETUP, help=f"default {SETUP}")
    step.add_argument(
        "--database-file", type=Path, help="the run-time database file, streamed to the manager"
    )

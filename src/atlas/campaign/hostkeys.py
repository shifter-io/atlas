"""SSH host keys: every SSH call checks the host key strictly (SEC-SSH-PIN-1).

A droplet we create: `DigitalOcean.create` makes a fresh ed25519 host key per create request
and sends it in that request's cloud-init user data (`user_data`), so the key reaches the
droplet over the provider's API and cloud-init installs it before sshd starts. Its public half
is pinned first, under the droplet's name, in this machine's pin store (`pin_store`), and SSH
checks it with `HostKeyAlias=<name>` (`pinned_options`). The manager's deploy also writes it,
by address, into the manager's own known-hosts file (`publish`), which every later call reads.

A server we did not create: its entry is pinned once with `campaign manager pin-host-key`
(`pin_scanned`), which keeps the scanned key only when its fingerprint is the one the operator
read through a trusted channel (the provider's console, or the server itself).

A droplet created before this machine's first pin (`legacy`) keeps trust on first use on its
own file, so an upgrade never strands a running droplet; one created after it never does.

`known_hosts` and `known_hosts_option`: the manager's pinned file, for every SSH call to the
manager from the Mac (`manager deploy`, `upload-spec`, `enable`, `disable`, `teardown`,
`collect` and the Mac watch), found the same way and passed to `ssh` as one quoted value.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..errors import UserError
from ..onboarding.files import private_dir

STORE_ENV = "ATLAS_HOST_KEYS"  # the pin store; else under the environment's root (`pin_store`)
ROOT_ENV = "ATLAS_MANAGER_ROOT"  # collector.ROOT_ENV, set by every unit of a host environment
SINCE = ".since"  # when this machine pinned its first key (`legacy`)
KEEP = 4  # pinned keys per name: an older droplet of a reused name still connects
PIN_DAYS = 30  # a pin unused this long is removed
KEY_TYPE = "ssh-ed25519"
NAME_RE = r"[a-z0-9][a-z0-9-]*"
FINGERPRINT_RE = r"SHA256:[A-Za-z0-9+/]{43}"
PIN_ACTION = "pin-host-key"


class HostKeyError(UserError):
    """A host key that could not be made, or is not the one expected: nothing was pinned."""


@dataclass(frozen=True)
class HostKey:
    private: str = field(repr=False)
    public: str  # `ssh-ed25519 <base64>`


def known_hosts(path: Path, inventory: dict) -> Path:
    """The manager's host-key file. `deploy` records it beside the inventory, so a relative
    record is found there, never in the current directory (another manager's file). `~` is
    expanded and the result is absolute, so it works from any directory."""
    recorded = Path(inventory["known_hosts"]).expanduser()
    if recorded.is_absolute():
        return recorded
    return path.expanduser().resolve().with_name(recorded.name)


def known_hosts_option(file: Path | str) -> str:
    """`UserKnownHostsFile="<file>"` for `ssh -o`. Unquoted, ssh splits the value at spaces
    (a list of files) and expands `%` tokens, so the value is quoted, `\\` and `"` are
    escaped and `%` is doubled."""
    text = str(file).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'UserKnownHostsFile="{text}"'


def new_host_key() -> HostKey:
    """A fresh ed25519 key pair, made by ssh-keygen in a private temporary folder."""
    with tempfile.TemporaryDirectory(prefix="atlas-hostkey-") as folder:
        path = Path(folder) / "key"
        made = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(path)],
            capture_output=True,
        )
        if made.returncode:
            raise HostKeyError("ssh-keygen could not make a host key")
        public = " ".join(path.with_name("key.pub").read_text().split()[:2])
        return HostKey(path.read_text(), public)


def cloud_config(key: HostKey) -> str:
    """Cloud-init user data that replaces the droplet's host keys with `key`."""
    private = "".join(f"    {line}\n" for line in key.private.strip().splitlines())
    return (
        "#cloud-config\nssh_deletekeys: true\nssh_keys:\n"
        f"  ed25519_private: |\n{private}  ed25519_public: {key.public}\n"
    )


def user_data(names: list[str]) -> dict[str, str]:
    """A create request's `user_data`: one new key for its droplets, pinned before it leaves."""
    key = new_host_key()
    pin(names, key.public)
    return {"user_data": cloud_config(key)}


def fingerprint(public: str) -> str:
    """OpenSSH's SHA256 fingerprint of a public key line (`<type> <base64> [comment]`)."""
    parts = public.split()
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (IndexError, binascii.Error):
        raise HostKeyError("not an OpenSSH public key") from None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def parse_fingerprints(text: str) -> set[str]:
    """The SHA256 fingerprints in `text`: `ssh-keygen -l` lines, cloud-init's console lines, or
    bare values."""
    return set(re.findall(rf"(?<![A-Za-z0-9+/]){FINGERPRINT_RE}(?![A-Za-z0-9+/])", text))


def pin_store() -> Path:
    """`$ATLAS_HOST_KEYS`; else `<environment root>/.host-keys`, so staging and production on one
    server never share one (nor its first pin's time); else `~/.local/state/atlas/host-keys`."""
    if os.environ.get(STORE_ENV):
        return Path(os.environ[STORE_ENV])
    if os.environ.get(ROOT_ENV):
        return Path(os.environ[ROOT_ENV]) / ".host-keys"
    return Path.home() / ".local" / "state" / "atlas" / "host-keys"


def _write(file: Path, text: str) -> None:
    """Replace `file` atomically, owner-only."""
    temporary = file.with_name(f".{file.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.replace(temporary, file)


def pin(names: list[str], public: str) -> None:
    """Pin `public` under each name, keeping each name's last KEEP keys; prune stale pins."""
    store = pin_store()
    private_dir(store)
    if not (store / SINCE).exists():
        _write(store / SINCE, datetime.now(UTC).isoformat() + "\n")
    cutoff = datetime.now(UTC).timestamp() - PIN_DAYS * 86400
    for old in store.glob("[a-z0-9]*"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    for name in names:
        if not re.fullmatch(NAME_RE, name):
            raise HostKeyError("a droplet name to pin must be lowercase letters, digits and -")
        file = store / name
        lines = file.read_text().splitlines() if file.is_file() else []
        _write(file, "\n".join([*lines, f"{name} {public}"][-KEEP:]) + "\n")


def pinned(name: str) -> Path | None:
    """The droplet's pin file, if this machine pinned its key."""
    file = pin_store() / name
    return file if re.fullmatch(NAME_RE, name) and file.is_file() else None


def pinned_options(name: str, file: Path) -> list[str]:
    """ssh options that accept only the ed25519 key pinned for `name`."""
    options = [
        f"HostKeyAlias={name}",
        known_hosts_option(file),
        "StrictHostKeyChecking=yes",
        f"HostKeyAlgorithms={KEY_TYPE}",
    ]
    return [item for option in options for item in ("-o", option)]


def legacy(created_at: datetime) -> bool:
    """A droplet created before this machine's first pin (or before any): trust on first use."""
    try:
        since = datetime.fromisoformat((pin_store() / SINCE).read_text().strip())
    except FileNotFoundError:
        return True
    except ValueError:
        return False
    return created_at < since


def publish(name: str | None, address: str, file: Path) -> bool:
    """Write the keys pinned for `name` into `file` by `address`; False when none are pinned
    (a manager created before SEC-SSH-PIN-1 keeps the file it has)."""
    found = pinned(name) if name else None
    if found is None:
        return False
    keys = [line.split(maxsplit=1)[1] for line in found.read_text().splitlines() if " " in line]
    _write(file, "".join(f"{address} {key}\n" for key in keys))
    return True


def keyscan(host: str, port: int) -> str:
    """The server's ed25519 key as `ssh-keyscan` prints it (no trust is given to it here)."""
    argv = ["ssh-keyscan", "-T", "10", "-t", "ed25519", "-p", str(port), host]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        raise HostKeyError("ssh-keyscan could not read the server's host key") from None


def pin_scanned(
    host: str,
    port: int,
    expected: str,
    file: Path,
    scan: Callable[[str, int], str] = keyscan,
) -> str:
    """Pin the server's ed25519 key into `file` only when its fingerprint is in `expected`;
    any other key is refused and `file` stays as it was. Returns the pinned fingerprint."""
    wanted = parse_fingerprints(expected)
    if not wanted:
        raise HostKeyError("--fingerprint must hold a SHA256:<43 characters> fingerprint")
    lines = [line for line in scan(host, port).splitlines() if not line.startswith("#")]
    scanned = [line.split(maxsplit=1)[1] for line in lines if len(line.split()) >= 3]
    keys = [key for key in scanned if key.split()[0] == KEY_TYPE and fingerprint(key) in wanted]
    if not keys:
        raise HostKeyError("the server's host key does not match the fingerprint: nothing pinned")
    entry = host if port == 22 else f"[{host}]:{port}"
    kept = file.read_text().splitlines() if file.is_file() else []
    kept = [line for line in kept if line.split(maxsplit=1)[:1] != [entry]]
    _write(file, "".join(f"{line}\n" for line in [*kept, f"{entry} {keys[0]}"]))
    return fingerprint(keys[0])


def add_parser(steps) -> None:
    """`campaign manager pin-host-key`: pin an existing server's key from a trusted fingerprint."""
    step = steps.add_parser(PIN_ACTION, help="Pin a server's SSH host key after checking it")
    step.add_argument("--host", required=True, help="the server's address or name")
    step.add_argument("--port", type=int, default=22)
    step.add_argument("--known-hosts", type=Path, required=True, help="the file to pin into")
    step.add_argument(
        "--fingerprint", required=True, help="SHA256:..., read through a trusted channel"
    )


def run_pin(args) -> dict[str, str]:
    file = args.known_hosts.expanduser()
    found = pin_scanned(args.host, args.port, args.fingerprint, file, keyscan)
    return {"pinned": found, "known_hosts": str(file)}

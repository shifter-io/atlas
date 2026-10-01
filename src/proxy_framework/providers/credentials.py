"""Read local credential TOML without exporting secrets into the process environment."""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path


def read_credentials(path: str | Path, provider: str) -> dict[str, str]:
    # Open first, then check the actual descriptor: no symlink/permission race.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Credential path must be a regular file")
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("Credential file must be owned by the current user with mode 600")
        data = tomllib.load(stream)
    credentials = data.get(provider)
    if not isinstance(credentials, dict) or not credentials or not all(
        isinstance(value, str) and value for value in credentials.values()
    ):
        raise ValueError("Credential file requires a provider table with nonempty string values")
    return credentials

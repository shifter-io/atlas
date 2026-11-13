"""The manager's pinned host-key file, for every SSH call to the manager from the Mac.

`manager deploy`, `upload-spec`, `enable`, `disable`, `teardown`, `collect` and the Mac watch
all find the file the same way and pass it to `ssh` as one quoted option value.
"""

from __future__ import annotations

from pathlib import Path


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

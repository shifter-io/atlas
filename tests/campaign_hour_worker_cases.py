"""Shared helpers of the worker-side hour engine tests: tar archives built in memory.

`tar_bytes` packs `member` entries the way a worker's `tar -cf -` or the start payload does.
"""

from __future__ import annotations

import io
import tarfile


def tar_bytes(*members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def member(name, data=b"x", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None

"""ORCH-MEM-2: glibc's allocator, measured and trimmed.

On 27-28 Nov 2026 the orchestrator's anonymous memory grew from 670 to 1,131 MB in 14 hours
while every structure the gauge logs and Python's allocated blocks (about 2.05 million) stayed
flat: the growth was in glibc's malloc, not in an object we keep. It rose in steps, at the
hours whose upload burst was the largest. A burst holds hundreds of TLS connections and
imports at once (the imports in the default executor's 32 threads, each with a malloc arena of
its own); when it ends, malloc keeps the memory it freed for the next one. In a soak of 400
uploads at once over TLS, malloc held 26 MB in use and 297 MB free; malloc_trim(0) after each
round kept the process at about 110 MB of anonymous memory instead of 370.

`stats()` reads glibc's own count of what malloc holds (mallinfo2, every arena) and `trim()`
gives the free pages back to the system (malloc_trim(0)); the job `memory` does both every 5
minutes (memory.py). Off glibc (macOS, musl) both do nothing.
"""

from __future__ import annotations

import ctypes
from typing import Any

MB = 1 << 20


class _Mallinfo2(ctypes.Structure):
    """glibc's struct mallinfo2 (glibc 2.33 and later): every field a size_t."""

    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",  # bytes taken from the system with brk/sbrk, every arena
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",  # bytes in blocks of their own (mmap)
            "usmblks",
            "fsmblks",
            "uordblks",  # bytes in use in the arenas
            "fordblks",  # bytes free in the arenas, resident or already given back
            "keepcost",
        )
    ]


def load() -> Any | None:
    """The process's C library when it is glibc with mallinfo2 and malloc_trim, else None."""
    try:
        libc = ctypes.CDLL(None)  # the C library this Python is linked with
        info, trim_ = libc.mallinfo2, libc.malloc_trim
    except (OSError, AttributeError):
        return None
    info.restype, info.argtypes = _Mallinfo2, []
    trim_.restype, trim_.argtypes = ctypes.c_int, [ctypes.c_size_t]
    return libc


LIBC = load()


def stats(libc: Any | None = LIBC) -> dict[str, int]:
    """What malloc holds in MB: in use (malloc_used_mb) and free inside its arenas
    (malloc_free_mb, which trim() can give back); {} off glibc. In use rising with time is a
    leak; free rising while in use stays flat is the allocator keeping freed memory."""
    if libc is None:
        return {}
    info = libc.mallinfo2()
    return {
        "malloc_used_mb": (info.uordblks + info.hblkhd) // MB,
        "malloc_free_mb": info.fordblks // MB,
    }


def trim(libc: Any | None = LIBC) -> bool:
    """Give the free pages of every arena back to the system (malloc_trim(0)): True when some
    were, False when none or off glibc. ctypes releases the GIL for the call."""
    return bool(libc is not None and libc.malloc_trim(0))

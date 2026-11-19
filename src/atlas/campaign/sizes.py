"""Worker droplet sizes (LAUNCHER-SIZE-FALLBACK-1): the slugs a campaign accepts and a region's
ordered fallback list. When DigitalOcean answers that a size is not available in a region, the
launcher creates with the next size of the region's list."""

from __future__ import annotations

import re

# CPU-Optimized (c-2, c2-4, c-4-intel) or Basic (s-2vcpu-4gb, s-4vcpu-8gb-amd) droplet slugs.
SIZE_RE = r"c2?-\d+(-intel)?|s-\d+vcpu-\d+gb(-amd|-intel)?"
# A slug's CPU class (c: dedicated, s: shared), its vCPU count and, for a Basic slug, its GB.
SIZE_CPU_RE = r"(c)2?-(\d+)(?:-intel)?|(s)-(\d+)vcpu-(\d+)gb(?:-amd|-intel)?"
DEDICATED_GB_PER_VCPU = 2  # every CPU-Optimized slug (c-N, c2-N, c-N-intel) has 2N GB


def _shape(size: str) -> tuple[str, int, int]:
    """(CPU class, vCPUs, GB of memory) of a SIZE_RE slug."""
    kind, cpus, *memory = [part for part in re.fullmatch(SIZE_CPU_RE, size).groups() if part]
    return kind, int(cpus), int(memory[0]) if memory else int(cpus) * DEDICATED_GB_PER_VCPU


def size_list(value: object) -> tuple[str, ...]:
    """A region's sizes in order: a slug is a one-element list. Each fallback is a distinct slug
    of the first one's CPU class with at least its vCPUs and memory, so a dedicated-CPU cell
    never falls back to a shared-CPU size and no cell to a smaller one. Empty when the value is
    not such a list."""
    sizes = (value,) if isinstance(value, str) else tuple(value) if isinstance(value, list) else ()
    if not sizes or len(set(sizes)) != len(sizes):
        return ()
    if any(not isinstance(s, str) or not re.fullmatch(SIZE_RE, s) for s in sizes):
        return ()
    kind, cpus, memory = _shape(sizes[0])
    if any(k != kind or c < cpus or m < memory for k, c, m in map(_shape, sizes[1:])):
        return ()
    return sizes


def size_unavailable(status: int | None, detail: str) -> bool:
    """DigitalOcean's refusal of a create because the size is not offered in the region (a 422
    whose message says the size is not available), never another refusal."""
    detail = detail.lower()
    return status == 422 and "size" in detail and "not available" in detail

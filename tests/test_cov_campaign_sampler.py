"""COV-2: the worker sampler reads only the `cpu` lines of a full `/proc/stat`."""

from __future__ import annotations

from proxy_index.campaign import sampler

# As the kernel writes it: the cpu lines, then counters that are not CPU time, and a blank line.
PROC_STAT = (
    "cpu  100 0 100 700 100 0 0 0 0 0\n"
    "cpu0 100 0 100 700 100 0 0 0 0 0\n"
    "intr 123456 1 2 3\n"
    "ctxt 987654\n"
    "\n"
    "btime 1798754400\n"
    "processes 4242\n"
    "softirq 55 1 2\n"
)


def test_only_the_cpu_lines_of_a_full_proc_stat_are_counted():
    assert sampler.cpu_ticks(PROC_STAT) == {"cpu": (1000, 800, 0), "cpu0": (1000, 800, 0)}


def test_a_stat_without_cpu_lines_counts_nothing():
    assert sampler.cpu_ticks("intr 1 2\n\nctxt 3\n") == {}

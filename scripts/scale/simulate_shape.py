"""SIM-MODEL-3: a daily test's shape from the profile's reference (SIMULATE.md, "Size of a run"):
its 5-minute buckets, the buckets each of its IPs is seen in, and the DB-IP release it is
enriched with (one a month, as `dbip update` installs them)."""

from __future__ import annotations

import bisect
import calendar
from datetime import date
from itertools import accumulate

import synthetic
from simulate_browser import dig

# Without a reference: 2 to 4 buckets a run (3 on average), an IP in 1 to all of them.
BUCKETS = {"2": 1, "3": 1, "4": 1}
RELEASE_DAY = 1  # without a reference release: DB-IP's of the 1st of each month


def table(counts: dict) -> tuple[list[float], list[int]] | None:
    """(cumulative shares, values) of {value: count} by value; None without a count."""
    found = sorted((int(value), n) for value, n in counts.items() if n)
    total = sum(n for _, n in found)
    if not total:
        return None
    return [n / total for n in accumulate(n for _, n in found)], [value for value, _ in found]


def draw(found: tuple[list[float], list[int]], u: float) -> int:
    """The value at quantile u (0 <= u < 1)."""
    shares, values = found
    return values[min(bisect.bisect_right(shares, u), len(values) - 1)]


class Buckets:
    """A network's buckets per run and, per run's buckets, the buckets an IP is seen in: the
    reference's of the network, else of all its daily runs, else BUCKETS and 1 to all."""

    def __init__(self, profile: dict, network: str):
        daily = dig(profile, "reference", "daily")
        sources = (dig(daily, "networks", network, "buckets"), dig(daily, "buckets"))
        runs = next((table(s["per_run"]) for s in sources if s.get("per_run")), None)
        self.run = runs or table(BUCKETS)
        self.ip: dict[int, tuple] = {}
        for source in reversed(sources):  # the network's own replace the pooled ones
            for buckets, counts in (source.get("per_ip") or {}).items():
                if found := table(counts):
                    self.ip[int(buckets)] = found

    def per_run(self, u: float) -> int:
        return draw(self.run, u)

    def per_ip(self, buckets: int, u: float) -> int:
        found = self.ip.get(buckets)
        return min(draw(found, u), buckets) if found else 1 + int(u * buckets)


def release_day(profile: dict) -> int:
    """The day of the month of DB-IP's releases: the reference runs' newest, else RELEASE_DAY."""
    found = sorted(dig(profile, "reference", "daily").get("releases") or [])
    return date.fromisoformat(found[-1]).day if found else RELEASE_DAY


def release_date(day: date, monthly: int) -> date:
    """The newest release on or before `day`, one on day `monthly` of each month (the month's
    last day when it is shorter)."""
    year, month = day.year, day.month
    found = date(year, month, min(monthly, calendar.monthrange(year, month)[1]))
    if found <= day:
        return found
    year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return date(year, month, min(monthly, calendar.monthrange(year, month)[1]))


def requests(profile: dict, network: str) -> tuple[list[float], list[tuple[int, int]]]:
    """(cumulative shares, (low, high) request rows) of the profile's requests per IP."""
    seen = dig(profile, "networks", network, "seen_count_log2") or {"1": 1}
    found = sorted((int(k.split("-")[0]), int(k.split("-")[-1]), n) for k, n in seen.items())
    total = sum(n for *_, n in found)
    cdf = [value / total for value in accumulate(n for *_, n in found)]
    return cdf, [(low, high) for low, high, _ in found]


def error_share(profile: dict, network: str, hour: int) -> float:
    """Errors over attempts in the network's profile hour; synthetic's share without one."""
    hours = dig(profile, "networks", network).get("hours", [])
    found = next((h for h in hours if h["hour"] == f"{hour:02d}"), {})
    total = (found.get("successes") or 0) + (found.get("errors") or 0)
    return (found.get("errors") or 0) / total if total else 1 / (synthetic.ERROR_SHARE + 1)

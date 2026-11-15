"""LOWAVAIL-1 (#1181): a network's countries with almost no exits, read only (step 1 of 5).

A country where a network has almost no exits is "low availability" (code too_few_ips,
verdicts.STATUS) or "no exits" (0 IPs): never an error. It is decided from data, never from a
status code alone: the daily test found fewer than verdicts.MIN_IPS unique IPs, and its failures
are the gateway's own "no exit" answers, not timeouts, refused connections or login errors.

The "no exit" answers of each network are its grammar's [testing] no_inventory_statuses, the
statuses it sends when it has no exits in the requested country (answers.py counts them as
no_inventory). This module measures them: for every network, the answers of its daily tests in
small countries (an October census peak under MIN_IPS, or a daily test under it) against those of
its big ones (census peak and daily test both at least BIG), per request. The October census is
the formulas' per-country hourly unique IPs (formulas/<network>.json); its peak is the highest
hour.

    python -m atlas.availability --database-file <toml> --formulas formulas [--since <ISO time>]

prints the Markdown tables of docs/reports/lowavail-answers.md, from one read-only transaction.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from psycopg.rows import dict_row

from .providers.grammar import load_grammar
from .verdicts import MIN_IPS

BIG = 50  # the owner's "normally big" country: 50 IPs or more (the two-days-in-a-row rule)
# The errors of a failed request that never carry a status: the gateway or the exit did not
# answer. A status-carrying error (http_status, proxy_connect or proxy_auth with the gateway's
# status) counts as its status; errors under a 200 (a block page, an unusable body) are the
# target's, never a gateway's. Every other request without a status is REFUSED: a gateway that
# refused the connection or the login without a status (proxy_connect, proxy_auth).
NO_STATUS = ("timeout", "connection", "tls", "protocol", "dns")
REFUSED = "refused"
SHOWN = 4  # answers listed per network and side

# Every daily test decided since %(since)s: its network, country, unique IPs, requests (started and
# completed) and answers.
TESTS = (
    "SELECT p.provider,v.country_code::text AS country,v.unique_ips,"
    "coalesce((t.summary->>'started')::bigint,0) AS requests,"
    "coalesce((t.summary->>'completed')::bigint,0) AS completed,"
    "coalesce(t.summary->'status_codes','{}') AS statuses,"
    "coalesce(t.summary->'errors','{}') AS errors "
    "FROM country_verdicts v JOIN test_runs t USING(run_key) JOIN products p USING(product_id) "
    "WHERE v.decided_at>=%(since)s ORDER BY p.provider,v.country_code,v.decided_at"
)


def census_peaks(directory: Path) -> dict[str, dict[str, int]]:
    """{network: {country: the October census's highest hourly unique IPs}} of every formula in
    `directory`; a country the census never found exits in is not there."""
    peaks = {}
    for path in sorted(Path(directory).glob("*.json")):
        formula = json.loads(path.read_text())
        peaks[formula["network"]] = {
            country: max((n for n in entry["unique_ips"] if n is not None), default=0)
            for country, entry in formula["countries"].items()
        }
    return peaks


def failures(statuses: dict, errors: dict, completed: int) -> Counter:
    """A test's failed answers: each status of 400 or more, each NO_STATUS error, and its
    other completed requests without a status (REFUSED)."""
    found = Counter({c: n for c, n in statuses.items() if c.isdigit() and int(c) >= 400 and n})
    found.update({code: errors[code] for code in NO_STATUS if errors.get(code)})
    rest = completed - sum(statuses.values()) - sum(errors.get(c, 0) for c in NO_STATUS)
    if rest > 0:
        found[REFUSED] = rest
    return found


def no_exit(network: str) -> tuple[str, ...]:
    """The network's "no exit" answers: its grammar's no_inventory_statuses (none without a
    grammar)."""
    try:
        grammar = load_grammar(network)
    except ValueError:
        return ()
    return tuple(map(str, grammar.no_inventory_statuses))


@dataclass
class Side:
    """The daily tests of a network's small or big countries: their requests and failures."""

    tests: int = 0
    requests: int = 0
    failed: Counter = field(default_factory=Counter)

    def add(self, test: dict) -> None:
        self.tests += 1
        self.requests += test["requests"]
        self.failed += failures(test["statuses"], test["errors"], test["completed"])

    def share(self, code: str) -> float:
        """The answer's share of the side's requests."""
        return self.failed[code] / self.requests if self.requests else 0.0


@dataclass
class Network:
    """One network's countries and answers (summarize())."""

    name: str
    census_small: int  # countries with an October census peak under MIN_IPS
    no_exit: tuple[str, ...]
    tested: set = field(default_factory=set)
    daily_small: set = field(default_factory=set)  # countries with a daily test under MIN_IPS
    zero: set = field(default_factory=set)  # countries with a daily test that found no IP
    big_small: set = field(default_factory=set)  # census peak >= BIG, a daily test < MIN_IPS
    small: Side = field(default_factory=Side)
    big: Side = field(default_factory=Side)

    def covered(self) -> float | None:
        """The share of the small countries' failures that are "no exit" answers."""
        total = sum(self.small.failed.values())
        return sum(self.small.failed[c] for c in self.no_exit) / total if total else None


def summarize(tests: list[dict], peaks: dict, no_exits: dict) -> dict[str, Network]:
    """{network: Network} of `tests` (TESTS rows), the census `peaks` and each network's "no
    exit" answers; a network of the census without a test is listed too."""
    found = {
        name: Network(name, sum(n < MIN_IPS for n in countries.values()), no_exits.get(name, ()))
        for name, countries in peaks.items()
    }
    for test in tests:
        name, country, ips = test["provider"], test["country"], test["unique_ips"]
        network = found.setdefault(name, Network(name, 0, no_exits.get(name, ())))
        peak = peaks.get(name, {}).get(country, 0)
        network.tested.add(country)
        if ips < MIN_IPS:
            network.daily_small.add(country)
            if peak >= BIG:
                network.big_small.add(country)
        if ips == 0:
            network.zero.add(country)
        if peak < MIN_IPS or ips < MIN_IPS:
            network.small.add(test)
        elif peak >= BIG and ips >= BIG:
            network.big.add(test)
    return found


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def _answers(network: Network) -> str:
    """The small side's most frequent failed answers: share of its requests, then the big
    side's share of the same answer."""
    parts = [
        f"{code} {_pct(network.small.share(code))} / {_pct(network.big.share(code))}"
        for code, _ in network.small.failed.most_common(SHOWN)
    ]
    return ", ".join(parts) or "-"


def render(networks: dict[str, Network]) -> str:
    """The Markdown tables: countries per network, then the answers."""
    lines = [
        "| Network | Census under 10 | Tested | Daily under 10 | 0 IPs | Big, daily under 10 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for n in sorted(networks.values(), key=lambda n: n.name):
        lines.append(
            f"| {n.name} | {n.census_small} | {len(n.tested)} | {len(n.daily_small)} | "
            f"{len(n.zero)} | {len(n.big_small)} |"
        )
    lines += [
        "",
        "| Network | Small tests | Failed answers, small / big (share of requests) "
        "| No-exit list | Its share of small failures |",
        "|---|---:|---|---|---:|",
    ]
    for n in sorted(networks.values(), key=lambda n: n.name):
        listed = ", ".join(n.no_exit) or "-"
        lines.append(
            f"| {n.name} | {n.small.tests} | {_answers(n)} | {listed} | {_pct(n.covered())} |"
        )
    return "\n".join(lines) + "\n"


def report(database_file: Path, formulas: Path, since: datetime) -> str:
    """The tables of the daily tests decided since `since`, read in one read-only transaction."""
    from .storage import connect

    peaks = census_peaks(formulas)
    with connect(database_file) as conn:
        conn.read_only = True
        tests = conn.cursor(row_factory=dict_row).execute(TESTS, {"since": since}).fetchall()
    names = {*peaks, *(t["provider"] for t in tests)}
    return render(summarize(tests, peaks, {name: no_exit(name) for name in names}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.availability", description=__doc__)
    parser.add_argument("--database-file", type=Path, required=True)
    parser.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
    parser.add_argument("--since", type=datetime.fromisoformat, default=datetime.min)
    args = parser.parse_args(argv)
    print(report(args.database_file, args.formulas, args.since), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

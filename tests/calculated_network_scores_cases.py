"""Shared helpers of the NET-SUM-2 tests (test_calculated_network_scores.py): the scoring
policy, hand-built network windows, seeded runs with placed IPs and the overlap set rule."""

from datetime import UTC, datetime

from calculated_cases import lenient_policy
from test_calculated_hourly_reads import BUCKET, HOUR
from test_storage import measurement

from atlas.calculated.policy import DIMENSIONS, composite
from atlas.partitions import partition
from atlas.storage import connect, ingest

AS_OF = datetime(2026, 11, 5, 12, tzinfo=UTC)
# Each provider's distinct IPs in a window (no browser run here records IPs).
IPS = (
    "SELECT p.provider,o.ip_id FROM ip_observations o JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE o.bucket_start>=%s AND o.bucket_start<%s"
)


def scoring_policy(tmp_path, coverage="0"):
    """The lenient policy scoring what the fixtures measure: generic_http only, on reliability,
    coverage and diversity (no load test, price, uptime batches or DB-IP usage types)."""
    text = lenient_policy(tmp_path).read_text()
    for old, new in (
        (
            'required_targets = ["generic_http", "generic_https"]',
            'required_targets = ["generic_http"]',
        ),
        ("generic_http = 1\ngeneric_https = 1\n", "generic_http = 1\n"),
        ("reliability = 80\nspeed = 20\n", "reliability = 80\nspeed = 0\n"),
        (
            "[weights]\nreliability = 22\nspeed = 14\nuptime = 10\ncoverage = 12\ndiversity = 8\n"
            "dcAsns = 10\nauthenticity = 10\nvalue = 14\n",
            "[weights]\nreliability = 50\nspeed = 0\nuptime = 0\ncoverage = 30\ndiversity = 20\n"
            "dcAsns = 0\nauthenticity = 0\nvalue = 0\n",
        ),
        ("minimum_coverage = 0\n", f"minimum_coverage = {coverage}\n"),
    ):
        assert old in text, old
        text = text.replace(old, new, 1)
    policy = tmp_path / f"scoring-{coverage}.toml"
    policy.write_text(text)
    return policy


CARDS = ("generic_http", "generic_https", "amazon_pdp", "instagram_web", "x_web")
CARDS += ("google_search", "cloudflare_challenge", "linkedin_public", "ai_answers")


def unit_network(provider, kind, success, latency, up, ips, scale, price):
    """A network as attach() completes it: one 30d window with known raw values. scale is the
    pool's share of the leader's places, dcAsns' clean share and geo accuracy."""

    def measured(rate):
        return {
            "completed": 5000,
            "successes": int(5000 * rate),
            "success_rate": rate,
            "request_success_rate": rate,
            "bytes_up": 100 * 5000,  # 1 KB of wire traffic per request: value's cost per 1,000
            "bytes_down": 900 * 5000,
            "coverage": 1.0,
            "last_observed": AS_OF - HOUR,
            # Speed reads the pulses' p50 (SPEED-PULSE-1); no load test is needed for it.
            "latency_ms": {"p50": latency},
            "latency_under_load_ms": {"p50": None},
            "under_load": {"reason": "No load test in this window"},
        }

    daily = {"unique_ips": ips} | {k: int(8 * 2**n * scale) for n, k in enumerate(DIMENSIONS)}
    window = {
        "metrics": measured(success),
        # Every card target measured alike: value's all-targets cost follows price / success.
        "targets": {t: {"metrics": measured(success)} for t in CARDS},
        "pool": {"unique_ips": ips, "daily": daily}
        | {"datacenter_asn_share": 1 - scale, "geo_accuracy": scale},
        "uptime": {"batches": 20, "value": up},
        "scores": {},
        "_lead": {"evidence": {}, "evidence_sources": []},
    }
    return {
        "id": f"{provider}:{kind}:network",
        "provider": provider,
        "product": "residential",
        "product_id": len(provider),
        "kind": kind,
        "pricing": {"lowest_per_gb": price, "basis_per_gb": price},
        "windows": {"30d": window},
    }


def unit_networks():
    return [
        unit_network("aa", "residential", 1.0, 100, 1.0, 1000, 1, 2),
        unit_network("bbb", "residential", 0.8, 200, 0.9, 500, 0.5, 4),
        unit_network("cccc", "residential", 0.5, 400, 0.5, 250, 0.25, 8),
        unit_network("d", "isp", 0.8, 200, 0.9, 500, 0.5, 4),
    ]


async def run(h, provider, back, requests, *, split=False):
    """A daily US run of `requests` successes, settled `back` before end (History.settle())."""
    directory = await measurement(
        h.tmp_path, requests=requests, provider=provider, requested_country="us"
    )
    return h.settle(ingest(directory, h.database)["run_key"], back, split=split)


def place(conn, run_key, observed):
    """Replace a run's observations: observed maps a bucket start to its IP ids."""
    conn.execute("DELETE FROM ip_observations WHERE run_key=%s", (run_key,))
    for at, ip_ids in observed.items():
        partition(conn, "ip_observations", at)
        for ip_id in ip_ids:
            conn.execute(
                "INSERT INTO ip_observations VALUES (%s,%s,%s,1,%s,%s)",
                (at, run_key, ip_id, at, at + BUCKET / 5),
            )


def expected_overlap(sets, products, as_of):
    """The full-mode set rule (calculator.build()'s, per network): shares against the union of
    the other networks with IPs, and one row per network sharing an IP, counted at as_of."""
    found = {}
    for name, own in sets.items():
        peers = sorted(q for q in sets if q != name and sets[q])
        if not own:
            found[name] = {"exclusive_share": None, "shared_share": None}
            found[name] |= {"compared_products": 0, "peers": [], "as_of": as_of}
            continue
        shared = set().union(*(sets[q] for q in peers)) & own
        rows = [
            {"provider": q, "product_id": products[q], "shared_ips": len(own & sets[q])}
            | {"ips_ours": len(own), "ips_theirs": len(sets[q])}
            for q in peers
            if own & sets[q]
        ]
        found[name] = {
            "exclusive_share": 1 - len(shared) / len(own),
            "shared_share": len(shared) / len(own),
            "compared_products": len(peers),
            "peers": rows,
            "as_of": as_of,
        }
    return found


def assert_set_rule(database, nets, labels, as_of):
    """Each labelled window's overlap equals the set rule over the database's IPs, counted at
    as_of, and ips_ours (and ips_theirs) the window's pool.unique_ips."""
    products = {p: n["product_id"] for p, n in nets.items()}
    with connect(database) as conn:
        for label in labels:
            period = nets["alpha"]["windows"][label]["period"]
            bounds = [datetime.fromisoformat(period[k]) for k in ("start", "end")]
            sets = {p: set() for p in nets}
            for provider, ip_id in conn.execute(IPS, bounds).fetchall():
                sets[provider].add(ip_id)
            expected = expected_overlap(sets, products, as_of)
            for provider, n in nets.items():
                w = n["windows"][label]
                assert w["overlap"] == expected[provider], (label, provider)
                assert w["pool"]["unique_ips"] == len(sets[provider])
                for peer in w["overlap"]["peers"]:
                    assert peer["ips_ours"] == w["pool"]["unique_ips"]
                    theirs = nets[peer["provider"]]["windows"][label]["pool"]["unique_ips"]
                    assert peer["ips_theirs"] == theirs


def swapped(score, group):
    """The network's balanced composite with its group's diversity component."""
    components = score["components"] | {"diversity": group["components"]["diversity"]}
    return composite(components, score["weights"])


def coverages(net, groups, label):
    """generic_http's time coverage of a network window and of each of its groups'."""
    found = net["windows"][label]["targets"]["generic_http"]["metrics"]["coverage"]
    return found, [
        g["windows"][label]["targets"]["generic_http"]["metrics"]["coverage"] for g in groups
    ]

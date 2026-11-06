"""Render saved live measurements and optional offline geography as standalone HTML."""

from __future__ import annotations

import argparse
import gzip
import html
import json
from collections import Counter, defaultdict
from pathlib import Path


def latency_rows(summary):
    """The latency table's rows: each percentile, then the mean (latency_mean_ms; "n/a" in a
    summary from before it was recorded or of a run without successes)."""
    rows = [
        (key.upper(), f"{value:,.0f}" if value is not None else "—")
        for key, value in summary["latency_ms"].items()
    ]
    mean = summary.get("latency_mean_ms")
    return [*rows, ("Mean", f"{mean:,.1f}" if mean is not None else "n/a")]


def report(directory: Path) -> Path:
    summary = json.loads((directory / "summary.json").read_text())
    config = summary["config"]
    provider = config.get("provider_config", {})

    def esc(value):
        return html.escape(str(value))

    def table(headers, rows):
        head = "".join(f"<th>{esc(value)}</th>" for value in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in row) + "</tr>" for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    geo_html = ""
    fallback = (
        "No strict flag was requested, so broader-pool fallback was permitted. "
        if provider.get("adapter") == "shifter" and not provider.get("parameters", {}).get("strict")
        else ""
    )
    enrichments = list(directory.glob("enrichment-*.json"))
    if len(enrichments) > 1:
        raise ValueError("Select a single enrichment release before rendering this report")
    if enrichments:
        enrichment = json.loads(enrichments[0].read_text())
        requests = Counter()
        addresses = defaultdict(set)
        asns, isps = set(), set()
        with gzip.open(directory / enrichment["file"], "rt") as stream:
            for line in stream:
                row = json.loads(line)
                data = row["enrichment"]
                country = data["country_code"] or "Unknown"
                requests[country] += row["count"]
                addresses[country].add(row["ip"])
                if data["asn"] is not None:
                    asns.add(data["asn"])
                if data["isp"]:
                    isps.add(data["isp"])
        assert requests.total() == summary["successes"]
        assert sum(map(len, addresses.values())) == summary["unique_ips"]
        country_rows = [
            (
                country,
                f"{count:,}",
                f"{len(addresses[country]):,}",
                f"{count / summary['successes']:.2%}",
            )
            for country, count in requests.most_common()
        ]
        geo_html = f"""<h2>Offline geography</h2>
<p>{len(asns):,} distinct ASNs and {len(isps):,} ISP names across successful exit IPs.</p>
{table(("MMDB country", "Successful requests", "Unique IPs", "Request share"), country_rows)}
<p class="caption">Source: local {esc(enrichment["release"]["file"])}.
No geolocation requests were sent through the proxies. Country labels reflect this MMDB
release; they are not independent proof of physical location. {esc(fallback)}
Differences can also reflect database disagreement.</p>"""

    errors = table(("Error category", "Requests"), summary["errors"].items())
    statuses = table(("HTTP status received", "Requests"), summary["status_codes"].items())
    latencies = table(("Percentile", "Milliseconds"), latency_rows(summary))
    cards = [
        (
            f"{summary['success_rate']:.2%}" if summary["success_rate"] is not None else "—",
            "Success",
        ),
        (f"{summary['successes']:,} / {summary['requested']:,}", "Successful / requested"),
        (
            f"{summary['unique_ips']:,}" if summary["unique_ips"] is not None else "—",
            "Unique exit IPs",
        ),
        (f"{summary['duration_seconds']:.2f} s", "Run duration"),
    ]
    metrics = "".join(
        f"<div><strong>{esc(value)}</strong><span>{esc(label)}</span></div>"
        for value, label in cards
    )
    settings = table(
        ("Setting", "Value"),
        [
            ("Run ID", summary["run_id"]),
            ("Run status", summary["status"]),
            ("Framework / adapter", f"{summary['framework_version']} / {provider.get('version')}"),
            ("Target", summary["target"]["id"]),
            ("URL", summary["target"]["url"]),
            ("Gateway", json.dumps(provider.get("gateway"))),
            ("Username parameters", json.dumps(provider.get("parameters"))),
            ("Session policy", provider.get("session_policy")),
            ("Concurrency", config["concurrency"]),
            ("Timeout", f"{config['timeout_seconds']} s"),
            ("Started / completed", f"{summary['started']:,} / {summary['completed']:,}"),
            ("Throughput", f"{summary['requests_per_second']:.2f} completed requests/s"),
            ("Response body bytes", f"{summary['response_body_bytes']:,} (not billed traffic)"),
        ],
    )
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(config.get("provider"))} · Live measurement</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#fafbf8;color:#192b34;
font:16px/1.65 system-ui,sans-serif}}main{{max-width:1080px;margin:auto;padding:40px 28px 70px}}
h1{{font-size:28px;line-height:1.2}}h2{{font-size:20px;margin:30px 0 12px}}
.label{{font:12px ui-monospace,monospace;letter-spacing:.1em;
text-transform:uppercase;color:#086e62}}
.metrics{{display:flex;flex-wrap:wrap;gap:24px 40px;padding:22px 0;
border-block:1px solid #d7dfdf;margin:24px 0}}
.metrics strong{{display:block;font-size:25px;color:#086e62}}
.metrics span,.caption{{font-size:13px;color:#556772}}
.columns{{display:grid;grid-template-columns:1fr 1fr;gap:32px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{padding:9px 8px;border-bottom:1px solid #d7dfdf;text-align:left;overflow-wrap:anywhere}}
th{{background:#eef3ee}}a{{color:#086e62}}.note{{background:#eef4ef;padding:15px 18px}}
@media(max-width:680px){{main{{padding:24px 16px}}.columns{{grid-template-columns:1fr}}}}
</style><main><header><div class="label">Live measurement / {esc(config.get("provider"))}</div>
<h1>{summary["requested"]:,} requests · {esc(summary["target"]["id"])}</h1>
<p>{esc(summary["started_at"])} → {esc(summary["finished_at"])} (UTC)</p></header>
<div class="metrics">{metrics}</div>
<p class="note">Real network requests with fresh connections; retries and redirects disabled.
Username parameters: {esc(json.dumps(provider.get("parameters", {})))}.
Session policy: {esc(provider.get("session_policy"))}.
Success requires HTTP 200 and a single valid IP address.</p>
<div class="columns"><section><h2>Successful-request latency</h2>{latencies}
<p class="caption">Connection setup through complete response body; successful requests
only. Nearest-rank percentiles and the mean from the 1 ms ceiling histogram.</p></section>
<section><h2>Failures</h2>{errors}<p class="caption">Raw response bodies were not retained,
so the invalid-body response cannot be diagnosed further from this run.</p></section></div>
<h2>HTTP statuses</h2>{statuses}<p class="caption">A 200 response can still fail body
validation. Requests without a received status, including connection timeouts, have no
status-code entry. Redirects are counted as failures and are not followed.</p>
{geo_html}<h2>Run settings and evidence</h2>{settings}
<p><a href="summary.json">Summary JSON</a> · <a href="ips.jsonl.gz">IP observations</a></p>
<p class="caption">Credentials are stored separately in an owner-only local file and
are absent from this report and run metadata. Results describe this run and are not a
long-term provider ranking.</p></main></html>"""
    destination = directory / "report.html"
    destination.write_text(content)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    print(report(parser.parse_args().directory))

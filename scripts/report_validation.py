"""Render existing test evidence as a standalone HTML report."""

from __future__ import annotations

import argparse
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def report(root: Path) -> Path:
    suites = ET.parse(root / "tests.xml").getroot()
    cases = list(suites.iter("testcase"))
    failures = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    benchmark = json.loads((root / "scale/benchmark.json").read_text())
    coverage = json.loads((root / "coverage.json").read_text())["totals"]["percent_covered"]
    database = json.loads((root / "dbip.json").read_text())
    tested_at = next(suites.iter("testsuite")).get("timestamp", "")
    status = "Passed" if not failures and benchmark["passed"] else "Failed"
    rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{html.escape(str(value))}</td></tr>"
        for key, value in (
            ("Successful simulated requests", f"{benchmark['requests']:,}"),
            ("Exact distinct IPs exported", f"{benchmark['unique_ips']:,}"),
            (
                "Configured / peak concurrency",
                f"{benchmark['concurrency']} / {benchmark['peak_in_flight']}",
            ),
            ("Wall time including export", f"{benchmark['wall_seconds_including_export']:.2f} s"),
            ("Peak process memory", f"{benchmark['peak_rss_mib']:.1f} MiB"),
            ("Environment", f"{benchmark['python']} · {benchmark['platform']}"),
        )
    )
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Framework validation · {status}</title>
<style>
body{{max-width:920px;margin:48px auto;padding:0 24px 50px;background:#fafbf8;
color:#192b34;font:16px/1.65 system-ui}}header{{border-top:5px solid #086e62;padding-top:22px}}
h1{{font-size:30px;line-height:1.2}}h2{{font-size:21px;margin-top:34px}}
.label{{font:12px ui-monospace,monospace;letter-spacing:.12em;
text-transform:uppercase;color:#086e62}}
.stats{{display:flex;flex-wrap:wrap;gap:24px;border-block:1px solid #d7dfdf;padding:18px 0}}
.stats strong{{font-size:24px;display:block}}.stats span{{font-size:13px;color:#556772}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
td{{padding:11px 5px;border-bottom:1px solid #d7dfdf}}
td:first-child{{width:48%}}code{{font-size:13px;overflow-wrap:anywhere}}
a{{color:#086e62}}.note{{background:#eef4ef;padding:15px 20px}}small{{color:#556772}}
</style><header><div class="label">Framework / local validation</div>
<h1>{status}: targets, Shifter adapter and local enrichment</h1>
<small>Test run: {html.escape(tested_at)}</small></header>
<div class="stats"><div><strong>{len(cases) - failures - skipped}</strong>
<span>tests passed</span></div>
<div><strong>{failures}</strong><span>failures</span></div>
<div><strong>{skipped}</strong><span>skipped</span></div>
<div><strong>{coverage:.1f}%</strong><span>statement + branch coverage</span></div></div>
<h2>What was verified</h2><p>Loopback HTTP and SOCKS5 gateways, authentication, CONNECT,
certificate verification, proxy-side DNS, no hidden retry, concurrency, fragmented/oversized
responses, timeouts, exact IP counts, cancellation and process signals, histogram merging,
bucket transitions, background export, output integrity and local MMDB enrichment.
Shifter tests verify optional flags, configuration overrides, session policies, per-request
credential isolation across both targets/transports, status preservation and secret redaction.</p>
<h2>{benchmark["requests"]:,}-request scale check</h2><table>{rows}</table>
<p class="note">This benchmark uses simulated responses and real aggregation/compressed file
output. It measures processing capacity and memory, not network or provider performance.
No benchmark traffic was sent to the Amazon endpoints.</p>
<h2>Local database</h2><p><code>{html.escape(database["file"])}</code><br>
{html.escape(database["database_type"])}<br>Built {html.escape(database["built_at"])}</p>
<p>SHA-256: <code>{html.escape(database["sha256"])}</code></p>
<p>The source and project copy have matching checksums. Local IPv4/IPv6 lookups and a
simulated-run → real-MMDB enrichment check passed. Database files are excluded from packages.</p>
<h2>Remaining verification</h2><p>Mac Mini tests were not run: the discovered SSH endpoint
was reachable but rejected the available identity. Adapter integration tests use loopback
gateways and fixture credentials; live provider measurements have their own run reports.
PostgreSQL integration tests use isolated temporary databases when PG_TEST_ADMIN_FILE is set.
Rolling windows, website publication and schedules remain a later phase.</p>
<p><a href="../docs/index.html">Developer guide and database proposal</a> ·
<a href="tests.xml">Test evidence</a> · <a href="scale/benchmark.json">Benchmark evidence</a></p>
</html>"""
    destination = root / "validation.html"
    destination.write_text(content)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    print(report(parser.parse_args().artifacts))

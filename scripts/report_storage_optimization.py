"""Standalone report of verified schema-v4 storage and query measurements."""

import html
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
data = json.loads((root / "artifacts/postgresql.json").read_text())
ingestion = json.loads((root / "artifacts/optimized-ingestion.json").read_text())
before = sum(v["total_bytes"] for v in data["sizes_before"].values())
after = sum(v["total_bytes"] for v in data["sizes_after"].values())
queries = "".join(
    f"<tr><td>{html.escape(name)}</td><td>{old['median_ms']:.3f}</td>"
    f"<td>{data['queries_after'][name]['median_ms']:.3f}</td></tr>"
    for name, old in data["queries_before"].items()
)
relations = "".join(
    f"<tr><td>{html.escape(name)}</td><td>{value['table_bytes'] / 1024:,.0f}</td>"
    f"<td>{value['index_bytes'] / 1024:,.0f}</td></tr>"
    for name, value in data["sizes_after"].items()
    if value["total_bytes"]
)
checks = ", ".join(html.escape(key.replace("_", " ")) for key in data["logical_checksums"])
page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Storage optimization · schema v4</title>
<style>
body{{max-width:960px;margin:40px auto;padding:0 24px 48px;background:#fafbf8;
color:#213039;font:16px/1.65 system-ui}}h1{{font-size:28px}}h2{{font-size:21px;margin-top:30px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:9px;
border-bottom:1px solid #d0d9d5}}.lead{{padding:16px 20px;background:#eaf3ef;
border-left:4px solid #087768}}code{{font-size:13px;overflow-wrap:anywhere}}
a{{color:#086e62}}small{{color:#53636b}}
</style><h1>Storage optimization completed</h1>
<p>September 26, 2026 · framework 0.7.0 · PostgreSQL schema v4</p>
<p class="lead">Active table and index allocation: <strong>{before / 1048576:.2f} MiB →
{after / 1048576:.2f} MiB</strong>, a <strong>{(1 - after / before) * 100:.1f}% reduction</strong>.
All 4,967 IPs, observations and enrichment records are preserved. The saved Shifter run
still contains 5,000 completed requests, 4,997 successes and three errors.</p>
<h2>What changed</h2><ul>
<li>UUIDs remain unique external run identifiers in test_runs. Observations, bucket counters
and enrichment-run records reference an internal bigint run_key.</li>
<li>The observation primary key now starts with run_key, removing the separate run index.
The IP/time index remains. Product and release references use integers; long-lived IP
and run identities retain bigint capacity.</li>
<li>ip_observations and product_ips remain separate: detailed run/bucket history and the
rebuildable product/IP lifetime cache. No history was deleted or coarsened.</li>
<li>pool_ips shares membership across concurrency, timeout and rate changes. Pool scope
retains provider/product, target semantics, runner location, gateway, session policy and
all geographic/adapter flags. cached_pools and cached_scopes remain separate. Exact
scope counts use indexed history checks during ingestion rather than duplicated sets.</li>
<li>Full run summaries are reconstructed from compact metadata, scope configuration and
bucket histograms. Original artifacts and checksums are unchanged. Read through
<code>storage.read_summary()</code> or <code>test_run_details.summary</code>.</li>
<li>Shared typed location and network tuples remove repeated enrichment values. All 14
fields remain available through ip_enrichment_details. Coordinates distinguish locations;
ISP, both organizations, ASN, connection type and usage type distinguish network tuples.
Unknown future fields remain lossless; empty extra_attributes uses SQL NULL.</li>
<li>A staged rebuild removed dropped-column and update/index overhead left by earlier
migrations. The previous database and dump remain available for recovery.</li>
</ul>
<h2>Preservation and validation</h2>
<p>Logical checksums matched before the database swap for: {checks}. Re-importing the
same artifacts left counters unchanged. Tests cover atomic rollback, concurrent replay,
multi-provider overlap, shared pools with distinct performance scopes, IPv6, all enrichment
fields, unknown/null attributes, same-city/different-coordinate records and multi-bucket
summary reconstruction. <a href="validation.html">Full test report</a>.</p>
<p>The original import with the initial join implementation took {data["ingest_seconds"]:.2f}
seconds. A revised hashable, null-safe tuple join imported the same saved 5,000-request
artifact in <strong>{ingestion["ingest_seconds"]:.2f} seconds</strong> in an isolated database;
all logical datasets matched the active database. This is an offline import measurement,
not network throughput. No additional proxy requests were sent.</p>
<h2>Read query tradeoffs</h2>
<p>Median execution time in milliseconds, five EXPLAIN ANALYZE samples per query on the
same local server. These small-sample timings are diagnostic, not statistically robust
production performance claims. Joins can cost CPU even when storage decreases.</p>
<table><thead><tr><th>Query</th><th>Before, ms</th><th>After, ms</th></tr></thead>
<tbody>{queries}</tbody></table>
<p>Every tested read remains under four milliseconds. ISP grouping became slightly slower;
the production website should consume cached aggregates. The original 4,967-row sample
has only one provider and run. Larger multi-run workloads are still required to characterize
partition scans, scope-count checks and ongoing ingestion under concurrency.</p>
<h2>How to interpret the storage reduction</h2>
<p>The 51% reduction includes reclaimed migration overhead, not just schema improvements.
A fresh equivalent v3 enrichment copy measured 1.63 MiB; v4 enrichment plus both typed
lookup tables and their indexes occupies 1.58 MiB. The controlled observation-copy audit
measured a 14.7% reduction with numeric run references and two indexes. Actual allocation
also depends on insert order, page packing and update history; percentages are not
extrapolated to future data.</p>
<p>Figures exclude retained backup databases, dumps, WAL and sequence files. Backups are
intentional additional disk copies; none was deleted to make this result smaller.</p>
<h2>Current active relations</h2><table><thead><tr><th>Relation</th><th>Table KiB</th>
<th>Index KiB</th></tr></thead><tbody>{relations}</tbody></table>
<h2>Recovery and operation</h2>
<p>Connection settings remain unchanged. Reconnect and refresh DBeaver. Browse
product_ip_details, ip_enrichment_details and test_run_details for readable joins.</p>
<p>Previous database: <code>{html.escape(data["previous_database"])}</code> (offline).
Dump: <code>{html.escape(data["backup"])}</code>. The controlled rebuild script checks every
stored run against saved artifacts and verifies logical data before swapping databases.</p>
<p>Future methodology work includes rolling windows, website publication and any policy
for archiving old detail. All existing detailed history remains retained.</p>
<p><a href="../docs/index.html#database">Database guide</a> ·
<a href="postgresql.json">Migration, size and query evidence</a> ·
<a href="optimized-ingestion.json">Import benchmark</a> ·
<a href="storage-audit.html">Original audit</a></p></html>"""
path = root / "artifacts/storage-optimization.html"
path.write_text(page)
print(path)

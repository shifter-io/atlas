"""Render measured storage findings as a standalone HTML review."""

import html
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
audit = json.loads((root / "artifacts/storage-audit.json").read_text())
comparison = json.loads((root / "artifacts/storage-comparison.json").read_text())
rows = "".join(
    f"<tr><td>{html.escape(name)}</td><td>{heap / 1024:,.0f}</td>"
    f"<td>{indexes / 1024:,.0f}</td><td>{(heap + indexes) / 1024:,.0f}</td></tr>"
    for name, heap, indexes, _ in audit["sizes"]
    if heap + indexes
)
variants = "".join(
    f"<tr><td>{label}</td><td>{comparison[key]['total_bytes'] / 1024:,.0f} KiB</td></tr>"
    for key, label in [
        ("audit_uuid", "UUID, three indexes"),
        ("audit_bigint", "bigint, three indexes"),
        ("audit_bigint_two_indexes", "bigint, run-leading primary key + IP/time index"),
    ]
)
page = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Database storage audit · September 26, 2026</title>
<style>body{max-width:1040px;margin:40px auto;padding:0 24px 50px;font:16px/1.6
system-ui;color:#203039;background:#fafbf8}
h1{font-size:29px}
h2{font-size:21px;margin-top:32px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px;border-bottom:1px solid #cdd7d4}
code{font-size:13px;overflow-wrap:anywhere}
.note{border-left:4px solid #087768;padding:8px 20px;background:#edf4f0}
small{color:#53616a}
a{color:#086e62}</style>
<h1>Database storage audit</h1><p>September 26, 2026 · local PostgreSQL 17 · schema v3</p>
<p class="note">One Shifter residential run: 5,000 completed requests, 4,997 successes and 4,967
unique IPs. This audit changes no production schema or data. Measurements use disposable temporary
tables; proposed changes have not been deployed.</p>
<h2>1. Replace repeated UUID references</h2>
<p><code>test_runs</code> already holds one record per run. Add an internal <code>run_key
bigint</code>, keep the UUID unique there for artifact identity and replay protection, and
reference the numeric key from observations, bucket counters and enrichment-run records. Another
mapping table is unnecessary. UUID is 16 bytes; bigint is 8 bytes. At 100 million observations
this removes 800 million bytes of raw run-reference payload before indexes, alignment and row
overhead; it does not halve the entire table.</p>
<table><thead><tr><th>Controlled copy of the same 4,967 rows</th><th>Table +
indexes</th></tr></thead><tbody>VARIANTS</tbody></table>
<p>Numeric references alone saved 8.6%; combined with the alternative index layout, 14.7%. These
are small-sample storage results, not production throughput or query-speed benchmarks. The extra
unique UUID index on the run table is not included in this observation-only comparison.</p>
<h2>2. Why overlap is empty</h2>
<p>Only provider <code>shifter</code>, product <code>residential</code>, is present. Both a direct
intersection query and the cache return no pairs. A shared IP needs at least two distinct
providers. A new provider does not guarantee overlap: it must actually share an observed IP. The
current cache stores positive lifetime intersections only; absent pairs mean zero under the
current complete ingestion process. It does not describe a rolling window.</p>
<h2>3. History versus lifetime summaries</h2>
<p><code>ip_observations</code> contains one row per run × five-minute bucket × IP.
<code>product_ips</code> contains one row per product × IP across all runs, with lifetime counts
and first/last seen. With only one run, their 4,967 rows look alike. After 100 runs that see the
same IP, history may contain 100 or more rows while product membership still contains one.</p>
<p>The lifetime table is deliberately derived data for fast reads and overlap updates. My
recommendation is to retain it as a documented, rebuildable cache. If storage is the overriding
priority, remove it and use a grouped view over history, accepting more work for product lookups
and ingestion, or build equivalent summaries in a separate publication step. Merely merging the
tables cannot preserve both detailed history and precomputed summaries without retaining
equivalent information.</p>
<p>Do not move lifetime fields onto every historical row; that repeats them more often. Do not
replace all history with one lifetime row; that loses when and in which run the IP was observed. A
single product/IP header plus child history still amounts to two logical datasets.</p>
<h2>4. Additional findings and priorities</h2>
<ol>
<li><strong>Reclaim migration overhead first.</strong> Current enrichment table + indexes: 6.35
MiB; fresh equivalent copy: 1.63 MiB, approximately 74% less. The previous migration updated rows
before dropping JSONB, leaving a dropped physical column and allocated update/index overhead. A
table rewrite or controlled rebuild can reclaim this; ordinary VACUUM is not equivalent. I should
have measured physical storage after the migration. Preserve backup and compare every value after
compaction.</li>
<li><strong>Review observation indexes.</strong> Current primary key starts with bucket time,
requiring another index for run lookups. A primary key starting with run_key, followed by
bucket_start and ip_id, retains the partition key and may cover these lookups. Keep the IP/time
index for history. Benchmark run deletion/FK checks, time-range reads and IP lookups before
removing the standalone run index. The two product membership indexes serve opposite lookup
directions; neither is automatically redundant.</li>
<li><strong>Separate pool identity from measurement settings.</strong> <code>scope_ips</code>
repeats membership for every full scope. Current scope identity includes concurrency, timeout,
gateway and session policy. Changing a measurement setting creates another membership set even for
the same intended pool. Define a narrower pool scope using provider/product and the methodology’s
pool-defining parameters, while retaining full measurement scope for performance metrics. Country,
region, ASN, targeting and endpoint semantics must not be casually merged.</li>
<li><strong>Remove redundant persisted run payloads.</strong> <code>test_runs.summary</code>
repeats bucket counters already in <code>run_buckets</code>, and stores an overall histogram as
well as bucket histograms. Cache histograms in <code>scope_stats</code> have a separate purpose:
incremental percentile aggregation. Keep a minimal run record plus authoritative bucket data;
retain the original artifact and checksum with durable backup/recovery. Preserve the existing
summary API through reconstruction or artifact access before removing fields. Current compressed
summary is 10,585 bytes; nested JSON sizes are decompressed and must not be added to it as
physical storage.</li>
<li><strong>Normalize repeated enrichment values selectively.</strong> There are 347 ISP strings,
335 ASNs, four connection types and four usage types across 4,967 records; only 644 distinct
combined network tuples and 2,258 distinct complete location tuples. Candidate typed dimension
tables can reduce repeated text, with a readable joined view. Retain all fields, and do not assume
ASN determines ISP/organization or city name determines coordinates. Benchmark dimensions and
their indexes before choosing them; latest typed-column requirements remain compatible with typed
dimension columns.</li>
<li><strong>Consider narrower small-domain references.</strong> Products and MMDB releases could
use integer IDs if explicit bounds justify them; global IPs and long-lived history should retain
adequate range. Alignment and index overhead mean declared byte savings do not always translate
directly into table savings. Empty extra_attributes could use NULL in future, but that is lower
priority than the findings above.</li>
<li><strong>Define historical detail retention.</strong> All historically observed IP identities
can be retained while old detailed bucket observations are archived or compacted. This changes
available historical queries and is a methodology decision, not an automatic deletion
recommendation. Keep all existing history until that decision is made.</li>
</ol>
<h2>Measured current allocation</h2><p>KiB, including relation overhead. Partition parents occupy
no data pages; their child rows below include the actual storage. Figures exclude backup
databases, dumps and WAL. The retained pre-rebuild database and dumps are deliberate extra copies
outside these active tables.</p>
<table><thead><tr><th>Relation</th><th>Table KiB</th><th>Indexes KiB</th><th>Total
KiB</th></tr></thead><tbody>ROWS</tbody></table>
<h2>Recommended next change set</h2><p>First: compact enrichment, use numeric internal run keys,
benchmark and simplify observation indexes, and trim duplicated run JSON while preserving APIs and
durable artifacts. Keep product_ips as the lifetime cache. Next: define pool scopes with the
methodology and benchmark enrichment dimensions against expected query patterns. Perform the
changes as one reviewed migration with before/after storage, ingest throughput, replay, history
and overlap checks.</p>
<p><small>Evidence: <a href="storage-audit.json">live inspection</a> · <a
href="storage-comparison.json">temporary-copy comparisons</a>. Temporary copies have identical
selected data and equivalent indexes for each comparison; allocation differs with packing and
build history. Current data has one provider/run and mostly IPv4, so results cannot predict larger
diverse workloads.</small></p>
<p><small>PostgreSQL references: <a
href="https://www.postgresql.org/docs/17/datatype-uuid.html">UUID storage</a> · <a
href="https://www.postgresql.org/docs/17/datatype-numeric.html">integer sizes</a> · <a
href="https://www.postgresql.org/docs/17/routine-vacuuming.html">vacuum and table
rewrites</a>.</small></p></html>"""
(root / "artifacts/storage-audit.html").write_text(
    page.replace("VARIANTS", variants).replace("ROWS", rows)
)
print(root / "artifacts/storage-audit.html")

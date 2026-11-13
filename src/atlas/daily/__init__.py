"""DAILY-1: the hourly daily test of each network's countries due that UTC hour (operator).

How a daily-test run is found (a frozen contract): a test_runs row whose summary has
MARKER = DAILY. `daily ingest` writes it into every run it converts and nothing else does, so a
census, pulse or ad hoc run is never one. storage.ingest keeps it in test_runs.summary. The
run's network is scope_stats.product_id; its country is upper(pool_stats.requested_country).
"""

MARKER = "test_kind"
DAILY = "daily"

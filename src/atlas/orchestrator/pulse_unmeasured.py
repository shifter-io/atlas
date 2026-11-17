"""OUTAGE-NOT-MEASURED-1 (#1277): a network outage is never extended by our not measuring.

On 16 Nov 2026 proxy-cheap's pulses of the 17:00 hour began late (its daily test ran late: the
budgeted network's late start, campaign/pulse_late.py) and stopped at their last start. The
17:50 window was down, the windows after it had no pulse data, and its network outage stayed
open in 129 countries, counting as downtime, until the next hour's pulses: a "down" made by our
not measuring. Its countries whose pulses were never opened that hour were "down" too. So:

- a window without pulse data for a network (none of its pulse runs has requests in it, after
  pulse_no_exit.strip) ends the network's open outage at the window's start, where measuring
  ended, with end_reason 'not_measured' and no tape event, as verdicts.CLOSE ends one with
  'too_few_ips'. The next down window opens a new one, with its own pulse_down. A window mostly
  refused at login (pulse_login) is measured but not judged: the open outage stays as it is
  (PROXY-AUTH-ALERT-1, unchanged).
- a network outage opens only in the countries the network was pulsed in by its last pulse
  interval: a pulse batch of a slot in that interval with a run of the network that was not
  failed, skipped or killed (PULSED_SQL). A country whose pulses were not opened in the hour is
  not down.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

END_REASON = "not_measured"

# The country c was pulsed for %(product)s in a slot of the pulse interval before %(end)s (the
# window's end); test_batches is read through its (kind, target_id, scheduled_for) index.
PULSED_SQL = (
    "AND EXISTS (SELECT 1 FROM test_batches b JOIN targets t ON t.target_id=b.target_id "
    "JOIN batch_runs br ON br.batch_id=b.batch_id AND br.product_id=%(product)s "
    "AND br.status NOT IN ('failed','skipped','killed') WHERE b.kind='pulse' "
    "AND b.country_code=c.country_code AND b.scheduled_for<%(end)s "
    "AND b.scheduled_for>=%(end)s-make_interval(mins=>t.pulse_interval_minutes)) "
)
# The open network outages of the networks without pulse data in the window starting %(start)s.
CLOSE_SQL = (
    "UPDATE outages SET ended_at=greatest(started_at,%(start)s),end_reason=%(reason)s "
    "WHERE source='pulse' AND batch_id IS NULL AND ended_at IS NULL AND started_at<%(start)s "
    "AND NOT product_id=ANY(%(measured)s::int[]) RETURNING product_id"
)


async def close_unmeasured(
    conn: psycopg.AsyncConnection, start: datetime, stats: dict[int, dict[str, Any]]
) -> int:
    """End the open network outages of every network without pulse data in the window (stats:
    pulse.window_stats) at its start; returns how many networks' outages ended."""
    measured = sorted(product for product, found in stats.items() if found["requests"])
    params = {"start": start, "reason": END_REASON, "measured": measured}
    rows = await (await conn.execute(CLOSE_SQL, params)).fetchall()
    return len({row["product_id"] for row in rows})

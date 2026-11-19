"""FLEET-POOL-SERIES-1 (#1302): the `*` fleet's IP slots (calculated/fleet.py), the distinct
exit IPs of every UTC hour of the 24h window and every UTC day of the 7d and 30d windows, each IP
once however many networks saw it. The counts from the memos are checked against the full
calculation in test_calc_fleet.py."""

from datetime import UTC, datetime, timedelta

import pytest

from atlas.calculated.fleet import fleet_totals, ip_slots, ip_spans, slot_counts, slot_spans

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
AS_OF = datetime(2026, 11, 17, 4, 10, tzinfo=UTC)  # a build ten minutes into its hour


@pytest.mark.parametrize(("hours", "slots"), [(24, 24), (168, 7), (720, 30)])
def test_the_whole_window_has_its_slots_the_last_partial(hours, slots):
    spans = slot_spans(AS_OF, hours)
    assert len(spans) == slots
    step = HOUR if hours == 24 else DAY
    assert all(end - start == step for start, end in spans[:-1])
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
    assert spans[0][0] >= AS_OF - timedelta(hours=hours)  # inside the window
    last = datetime(2026, 11, 17, 4 if hours == 24 else 0, tzinfo=UTC)
    assert spans[-1] == (last, AS_OF)  # the current hour or day, partial


def test_a_cutoff_on_the_hour_ends_with_the_hour_just_closed_as_the_series():
    """The newest slot is the hour or day holding the window's last instant, as the series'
    points (aggregate.series_axis(), SERIES-HOURLY-DAILY-1) and successScoreCurve's axis (#1301):
    a cutoff on the hour or at midnight ends a whole hour or day, never an empty one."""
    midnight = datetime(2026, 11, 17, tzinfo=UTC)
    assert slot_spans(midnight, 24)[-1] == (midnight - HOUR, midnight)
    assert slot_spans(midnight, 24)[0] == (midnight - 24 * HOUR, midnight - 23 * HOUR)
    assert slot_spans(midnight, 168)[-1] == (midnight - DAY, midnight)
    assert slot_spans(midnight, 168)[0] == (midnight - 7 * DAY, midnight - 6 * DAY)
    assert slot_spans(midnight + HOUR, 168)[-1] == (midnight, midnight + HOUR)
    assert ip_slots([(midnight, (midnight, midnight + HOUR))], {})[0]["unique_ips"] is None


def member(*observations):
    """A fleet member (product, rows, observations, member) of its (bucket, ip) observations."""
    obs = [{"bucket_start": t, "ip_id": ip} for t, ip in observations]
    return ({}, [], obs, None)


def test_a_day_counts_a_shared_ip_once_and_an_empty_day_is_null():
    pairs = ip_spans(AS_OF, 168)
    assert all(t == span[0] for t, span in pairs)  # a 7d slot counts its own day
    spans = [span for _, span in pairs]
    d1, d2 = spans[3][0], spans[4][0]
    # Two networks: IP 1 seen by both on d1 (and twice by one), IP 2 by one; IP 3 on d2.
    alpha = member((d1, 1), (d1 + 5 * MINUTE, 1), (d1, 2), (d2, 3))
    beta = member((d1 + 30 * MINUTE, 1))
    # Before the first day (in the window's first 4 h 10 min): no slot holds it.
    early = member((AS_OF - 7 * DAY + 5 * MINUTE, 9))
    counts = slot_counts(spans, [alpha, beta, early])
    assert counts == {spans[3]: 2, spans[4]: 1}  # the union; the networks' sum would be 3
    assert sum(slot_counts(spans, [m]).get(spans[3], 0) for m in (alpha, beta)) == 3
    served = ip_slots(pairs, counts)
    assert [s["unique_ips"] for s in served[2:5]] == [None, 2, 1]  # null, never 0
    assert [s["at"] for s in served] == [start for start, _ in slot_spans(AS_OF, 168)]


def test_the_partial_current_day_counts_what_it_holds():
    spans = [span for _, span in ip_spans(AS_OF, 168)]
    now = member((AS_OF - 5 * MINUTE, 7), (AS_OF - 3 * HOUR, 8))
    assert slot_counts(spans, [now]) == {spans[-1]: 2}


def test_a_24h_point_is_the_trailing_24_hours_to_its_hours_end():
    """FLEET-POOL-SERIES-3: each 24h slot counts the 24 hours before its end, the newest the
    window itself; an IP is in every point whose 24 hours hold it, once however many networks
    saw it, so the points are the brute-force distinct counts of their spans."""
    pairs = ip_spans(AS_OF, 24)
    assert [t for t, _ in pairs] == [start for start, _ in slot_spans(AS_OF, 24)]
    assert all(e - s == DAY for _, (s, e) in pairs)
    assert pairs[-1][1] == (AS_OF - DAY, AS_OF) and pairs[0][1][1] == pairs[0][0] + HOUR
    seen = [(AS_OF - 40 * HOUR, 1), (AS_OF - 30 * HOUR, 2), (AS_OF - 20 * HOUR, 1)]
    seen += [(AS_OF - 2 * HOUR, 3), (AS_OF - 7 * MINUTE, 4)]
    other = [(AS_OF - 30 * HOUR + 5 * MINUTE, 2), (AS_OF - HOUR, 3)]  # a second network
    spans = sorted({span for _, span in pairs})
    counts = slot_counts(spans, [member(*seen), member(*other)])
    for s, e in spans:
        assert counts.get((s, e), 0) == len({ip for t, ip in seen + other if s <= t < e})
    served = [slot["unique_ips"] for slot in ip_slots(pairs, counts)]
    assert served[0] == 2 and served[-1] == 3  # 05:00's day: IPs 1 and 2; the window: 1, 3, 4


def test_only_the_every_group_entries_carry_slots():
    product = {
        "provider": "alpha",
        "kind": "residential",
        "windows": {"24h": {"period": {"hours": 24, "start": AS_OF - DAY}}},
    }
    seen = [{"bucket_start": AS_OF - HOUR, "ip_id": 1}]
    groups = {("us", "24h"): [(product, [], seen, None)]}
    out = fleet_totals(groups, AS_OF, 1, 30, None)
    assert [e["cohort"] for e in out] == ["*", "*:residential"]
    slots = [slot["unique_ips"] for slot in out[0]["ip_slots"]]
    assert len(slots) == 24 and slots[-2:] == [1, 1] and slots[:-2] == [None] * 22  # rolling
    assert "ip_slots" not in out[1]

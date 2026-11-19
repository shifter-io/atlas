"""CALC-PERF-2: time_slots() inside slot_memo() counts each time slot's rows once and gives what
counting them again gives (calculated/underload.py): the same slots, in the same order, so every
figure made from them is the same, byte for byte."""

import json
import random
from collections.abc import Mapping
from datetime import timedelta

from scoring_cases import T, row

from atlas.calculated import underload
from atlas.calculated.aggregate import census_split, floor_time, in_window, timed
from atlas.calculated.underload import latency_detail, slot_memo, time_slots, under_load

SLOT = 30 * 60
END = T + timedelta(hours=50)


class Held(Mapping):
    """A row the build holds (as inputs.BucketRow): a Mapping, not a dict."""

    def __init__(self, found):
        self.found = found

    def __getitem__(self, key):
        return self.found[key]

    def __iter__(self):
        return iter(self.found)

    def __len__(self):
        return len(self.found)

    def __or__(self, other):
        return dict(self) | other


def history(seed):
    """Three groups' rows, each group's in time order and the groups one after another (as a
    network's union), with latencies spread over several bins, and the done pulse runs."""
    rng = random.Random(seed)
    rows = []
    for group in range(3):
        starts = sorted(T + timedelta(minutes=5 * rng.randrange(600)) for _ in range(150))
        for at in starts:
            test = rng.choice(["pulse", "pulse", None, "census", "load"])
            census = ("c1",) if test in ("pulse", "load") and rng.random() < 0.2 else ()
            target = rng.choice(["generic_http", "other_http"])
            n = rng.randint(0, 40)
            r = row(at, 100, n=n, successes=rng.randint(0, n), test=test, census=census)
            bins = sorted({rng.randint(20, 900): rng.randint(1, 9) for _ in range(5)}.items())
            r["counters"]["latency_histogram"]["bins"] = [list(b) for b in bins]
            r |= {"target": target, "country": rng.choice(["US", "DE", "FR"]), "run_key": group}
            r |= {"slot": floor_time(at, SLOT), "last_observed": at}
            rows.append(Held(r))
    pulses = [
        {
            "slot": floor_time(T + timedelta(minutes=30 * k), SLOT),
            "scheduled_for": T + timedelta(minutes=30 * k),
            "target": "generic_http",
            "requests": rng.randint(1, 20),
            "successes": rng.randint(0, 1),
            "counted": rng.random() < 0.5,
            "during_census": rng.random() < 0.2,
            "census_batches": [],
        }
        for k in range(0, 100, 3)
    ]
    return rows, pulses


def windows(rows, pulses):
    """(rows, pulses, since, hours) of windows and days that share most of their slots, cut at
    slot edges and inside slots, per target and all of them."""
    found = []
    for hours, cut in ((48, 0), (24, 0), (24, 10), (12, 5), (6, 0)):
        since = END - timedelta(hours=hours, minutes=cut)
        own = in_window(rows, since, END, hours)
        slots = [d for d in pulses if since <= d["scheduled_for"] < END]
        found.append((own, slots, since, hours))
        for target in ("generic_http", "other_http"):
            mine = [r for r in own if r["target"] == target]
            found.append((mine, [d for d in slots if d["target"] == target], since, hours))
    return found


def figures(cases):
    """Every figure made of time slots, of each case, as JSON text (in its key order)."""
    out = []
    for own, slots, since, hours in cases:
        m = timed(own, hours, 3, slots, 30, since)
        out.append([m, under_load(own, 3), latency_detail(own, 3)])
        out.append(census_split(own, slots, 3, 30, "generic_http", since))
        found = time_slots(own, slots)
        out.append([[key, s] for key, s in found.items()])  # each slot, in its order
    return json.dumps(out, default=str)


def test_the_memo_gives_every_figure_counting_again_gives():
    for seed in range(5):
        rows, pulses = history(seed)
        cases = windows(rows, pulses)
        alone, backwards = figures(cases), figures(cases[::-1])
        assert underload._MEMOS == []
        with slot_memo():
            assert figures(cases) == alone
            assert figures(cases) == alone  # once all of them were counted, and with pulses
            assert figures(cases[::-1]) == backwards
        assert underload._MEMOS == []
        assert figures(cases) == alone


def test_a_slot_held_by_the_build_is_counted_once(monkeypatch):
    rows, pulses = history(7)
    counted = []
    count = underload._counted

    def counting(found):
        counted.append(len(found))
        return count(found)

    monkeypatch.setattr(underload, "_counted", counting)
    cases = windows(rows, pulses)
    with slot_memo():
        for own, slots, *_ in cases:
            time_slots(own, slots)
        first = sum(counted)
        for own, slots, *_ in cases:
            time_slots(own, slots)
        assert sum(counted) == first  # nothing counted again
        # A slot whose rows differ (a row more or less) is a slot of its own; copies (plain dict
        # rows, as census_split() makes) are counted each time.
        time_slots(rows[1:], ())
        assert sum(counted) > first
        again = sum(counted)
        copies = [dict(r) for r in rows[:20]]
        time_slots(copies, ())
        time_slots(copies, ())
        assert sum(counted) == again + 2 * len(copies)
    # In the 48 h window, the rows of the 24 h window's whole slots were counted once only.
    assert first < sum(len(own) for own, *_ in cases)


def test_pulses_never_change_the_counts_kept():
    rows, pulses = history(3)
    own = in_window(rows, END - timedelta(hours=24), END, 24)
    alone = time_slots(own)
    with slot_memo():
        time_slots(own)
        added = time_slots(own, [d | {"counted": False} for d in pulses])
        assert added != alone
        assert time_slots(own) == alone and list(time_slots(own)) == list(alone)

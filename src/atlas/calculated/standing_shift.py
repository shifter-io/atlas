"""The API's success shift around a frozen network (STANDING-FREEZE-2, docs/index.html).

The API shows the browser overalls, the challenge overall and each target's success, challenge
and block scores shifted (SUCCESS-SHIFT-1, api/internal/build/success_shift.go): each group's
score gains 10 minus the group's best, so the best reads 10. A held window (standing.py) is in no
group, and a frozen one is shifted by the group bests of the build its values are from (bests(),
kept with it as its standing's "bests"), so it serves its last published values whatever the
measured networks' best. The browser and challenge overalls rank on these served values
(served()), so a rank and its served score always agree.
"""

from __future__ import annotations

# The networks of one kind in one build: the peers whose best the API shifts by (bests()).
PEERS = (
    "SELECT n FROM calculated_snapshots s, jsonb_array_elements(coalesce(s.payload->'networks',"
    "'[]')) n WHERE s.build_id=%s AND n->>'kind'=%s"
)
# The target score metrics the API shifts (success_shift.go targetScores()).
SHIFTED = ("success", "challenge", "block")


def peers(cursor, build_id, kind):
    """The network entries of one kind in one stored build."""
    return [entry for (entry,) in cursor.execute(PEERS, (build_id, kind))]


def bests(windows):
    """{group: best score} of windows (one build's measured windows of one kind and range), by
    the API's success shift groups (success_shift.go window()), each the group's key without the
    kind and range: "browser|<engine>" (a browser overall), "challenge_overall" and
    "target|<target>|<engine>|<metric>" (an HTTP target's engine "http")."""
    found = {}

    def add(key, score):
        if score is not None:
            found[key] = max(found.get(key, score), score)

    for w in windows:
        for engine, own in (w.get("browser_overall") or {}).items():
            add(f"browser|{engine}", (own.get("success") or {}).get("score"))
        add("challenge_overall", (w.get("challenge_overall") or {}).get("score"))
        for target, t in w["targets"].items():
            groups = [("http", t.get("scores"))]
            if b := t.get("browser"):
                groups.append((b["engine"], b.get("scores")))
                groups += [(e, own.get("scores")) for e, own in b.get("engines", {}).items()]
            for engine, scores in groups:
                for metric in SHIFTED:
                    score = ((scores or {}).get(metric) or {}).get("score")
                    add(f"target|{target}|{engine}|{metric}", score)
    return found


def group(key):
    """The bests() group of a headline ranking (standing.rerank() key) the API shifts: the
    challenge overall and each browser engine's overall ("browser:<engine>"); None for the
    Success x/10 (shifted by the calculation itself) and the Index Score (never shifted)."""
    if key == "challenge_overall":
        return key
    return "browser|" + key.removeprefix("browser:") if key.startswith("browser:") else None


def served(key, live, held):
    """{id(score object): the score the API serves} of one ranking's (key: its rerank() key) live
    and held ((score object, its window's stored bests)) scores: in a shifted group the API adds
    10 minus the best live score to each live one (the best reads 10) and 10 minus its window's
    stored best to a frozen one (success_shift.go heldShift()); a frozen one without a stored
    best for the group, and every score of an unshifted ranking, serves its own score."""
    out = {id(s): s["score"] for s in live} | {id(s): s["score"] for s, _ in held}
    if (name := group(key)) is None:
        return out
    best = max((s["score"] for s in live), default=None)
    for s in live:
        out[id(s)] = _shifted(s["score"], best)
    for s, stored in held:
        if (top := stored.get(name)) is not None:
            out[id(s)] = _shifted(s["score"], top)
    return out


def _shifted(score, best):
    """A score shifted by its group's best, which reads exactly 10 (success_shift.go member())."""
    return 10.0 if score == best else score + 10 - best

"""docs/api-web-map-gaps.json lists every gap of docs/api-web-map.md, each as the map closes it.

The map's "Gaps summary" lists give the gap IDs; section 11 names the work item that closes an
API gap, section 12 the website and owner gaps, section 13 the withdrawn ones. `siteapi bench
--gaps` runs the API entries' checks against a running siteapi (api/README.md).
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "docs" / "api-web-map.md"
MANIFEST = ROOT / "docs" / "api-web-map-gaps.json"

GAP_ID = r"[A-Z][A-Z]+(?:-[A-Z0-9]+)+"
CLASSES = {"api", "website", "owner", "withdrawn"}
PLACEHOLDERS = {"slug", "kind", "range", "cc"}
PATH = re.compile(r"^[A-Za-z0-9_]+(\[\])?(\.([A-Za-z0-9_]+(\[\])?|\*))*\??$")


def sections(text: str) -> dict[int, str]:
    """The body of each numbered `## N.` section."""
    parts = re.split(r"^## (\d+)\. .*$", text, flags=re.M)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def summary_gaps(body: str) -> list[str]:
    """The gap IDs of a page section's "Gaps summary" list, in order."""
    match = re.search(r"^### Gaps summary$(.*?)(?=^#|\Z)", body, flags=re.M | re.S)
    if match is None:
        return []
    return re.findall(rf"^\d+\.\s+({GAP_ID})", match.group(1), flags=re.M)


def expand(cell: str, known: set[str]) -> list[str]:
    """The gap IDs a map cell names, with "A-1 to A-7" ranges spelled out."""
    out = []
    for first, last in re.findall(rf"({GAP_ID}) to ({GAP_ID})", cell):
        prefix, start = first.rsplit("-", 1)
        out += [f"{prefix}-{i}" for i in range(int(start), int(last.rsplit("-", 1)[1]) + 1)]
    return [g for g in out + re.findall(GAP_ID, cell) if g in known]


def table_rows(body: str) -> list[list[str]]:
    rows = [
        line.strip().strip("|").split("|") for line in body.splitlines() if line.startswith("| ")
    ]
    return [[c.strip() for c in row] for row in rows[1:]]


class Map:
    def __init__(self, text: str):
        secs = sections(text)
        self.page = {g: n for n, body in secs.items() if n <= 9 for g in summary_gaps(body)}
        self.order = [g for n in sorted(secs) if n <= 9 for g in summary_gaps(secs[n])]
        known = set(self.page)
        self.work: dict[str, set[str]] = {}
        self.items = set()
        for row in table_rows(secs[11]):
            self.items.add(row[0])
            for g in expand(row[2], known):
                self.work.setdefault(g, set()).add(row[0])
        self.decided = {}
        for row in table_rows(secs[12]):
            for g in expand(row[0], known):
                self.decided[g] = f"{row[1]}: {row[2]}"
        self.withdrawn = {}
        for item in secs[13].split("\n- "):
            item = " ".join(item.split())
            for g in expand(item, known) if " was withdrawn" in item else []:
                self.withdrawn[g] = item


def problems(gap_map: Map, manifest: dict) -> list[str]:
    """Every way the manifest disagrees with the map; empty when it matches."""
    out = []
    ids = [g["id"] for g in manifest["gaps"]]
    out += [f"{g}: listed twice" for g in sorted({g for g in ids if ids.count(g) > 1})]
    out += [f"{g}: missing from the manifest" for g in gap_map.order if g not in ids]
    out += [f"{g}: not a gap of the map" for g in ids if g not in gap_map.page]
    issues = manifest["workItems"]
    out += [f"{w}: no issue in workItems" for w in sorted(gap_map.items - set(issues)) if w != "W0"]
    for gap in manifest["gaps"]:
        gid, cls = gap["id"], gap["class"]
        if gid in gap_map.page and gap["page"] != gap_map.page[gid]:
            out.append(f"{gid}: page {gap['page']}, the map has it in section {gap_map.page[gid]}")
        if cls not in CLASSES:
            out.append(f"{gid}: unknown class {cls}")
        elif cls == "api":
            out += api_problems(gap_map, issues, gap)
        elif cls == "withdrawn" and gap.get("reason") != gap_map.withdrawn.get(gid):
            out.append(f"{gid}: withdrawn but not as section 13 says")
        elif cls in ("website", "owner") and gap.get("reason") != gap_map.decided.get(gid):
            out.append(f"{gid}: {cls} but not as section 12 says")
    return out


def api_problems(gap_map: Map, issues: dict, gap: dict) -> list[str]:
    gid, work, out = gap["id"], gap.get("work"), []
    if work not in gap_map.work.get(gid, set()):
        out.append(
            f"{gid}: work item {work}, section 11 has {sorted(gap_map.work.get(gid, set()))}"
        )
    if gap.get("issue") != issues.get(work):
        out.append(f"{gid}: issue {gap.get('issue')}, {work} is #{issues.get(work)}")
    if not gap.get("checks"):
        out.append(f"{gid}: an api gap with no check")
    for check in gap.get("checks", []):
        names = set(re.findall(r"\{(\w+)\}", check.get("request", "")))
        if not check.get("request", "").startswith("/v1/") or names - PLACEHOLDERS:
            out.append(f"{gid}: bad request {check.get('request')!r}")
        paths = check.get("paths") or []
        if not paths:
            out.append(f"{gid}: {check.get('request')} checks no path")
        for path in paths + list(check.get("match", {})):
            if not PATH.match(path):
                out.append(f"{gid}: bad path {path!r}")
        for path, pattern in check.get("match", {}).items():
            if path not in paths:
                out.append(f"{gid}: match on {path}, which the check does not list")
            re.compile(pattern)
    return out


@pytest.fixture(scope="module")
def gap_map() -> Map:
    return Map(MAP.read_text())


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_the_manifest_matches_the_map(gap_map, manifest):
    assert len(gap_map.order) == len(set(gap_map.order)) > 200
    assert problems(gap_map, manifest) == []


def test_every_gap_has_a_class_in_the_map(gap_map):
    unclassed = [
        g for g in gap_map.order if g not in gap_map.work | gap_map.decided | gap_map.withdrawn
    ]
    assert unclassed == []


def mutate(manifest: dict, gid: str, **change) -> dict:
    out = json.loads(json.dumps(manifest))
    for gap in out["gaps"]:
        if gap["id"] == gid:
            gap.update(change)
    return out


def test_each_disagreement_is_found(gap_map, manifest):
    gaps = manifest["gaps"]
    dropped = dict(manifest, gaps=[g for g in gaps if g["id"] != "HOME-4"])
    assert problems(gap_map, dropped) == ["HOME-4: missing from the manifest"]
    twice = dict(manifest, gaps=gaps + [gaps[0]])
    assert problems(gap_map, twice) == [f"{gaps[0]['id']}: listed twice"]
    assert problems(gap_map, mutate(manifest, "HOME-4", checks=[])) == [
        "HOME-4: an api gap with no check"
    ]
    assert problems(gap_map, mutate(manifest, "HOME-4", work="W2", issue=710)) == [
        "HOME-4: work item W2, section 11 has ['W6']"
    ]
    assert problems(gap_map, mutate(manifest, "HOME-4", **{"class": "website", "reason": "x"})) == [
        "HOME-4: website but not as section 12 says"
    ]
    assert problems(gap_map, mutate(manifest, "HOME-1", **{"class": "owner"})) == []
    assert problems(gap_map, mutate(manifest, "TGT-8", **{"class": "withdrawn"})) == [
        "TGT-8: withdrawn but not as section 13 says"
    ]
    assert problems(gap_map, mutate(manifest, "EV-CORS", **{"class": "owner"})) == [
        "EV-CORS: owner but not as section 12 says"
    ]


def test_bad_checks_are_found(gap_map, manifest):
    bad = [
        {"request": "/v1/home?range={r}", "paths": ["market.x", "a..b"], "match": {"other": "^x$"}}
    ]
    assert problems(gap_map, mutate(manifest, "HOME-4", checks=bad)) == [
        "HOME-4: bad request '/v1/home?range={r}'",
        "HOME-4: bad path 'a..b'",
        "HOME-4: match on other, which the check does not list",
    ]
    assert problems(gap_map, mutate(manifest, "HOME-4", issue=1)) == ["HOME-4: issue 1, W6 is #712"]
    assert problems(gap_map, mutate(manifest, "HOME-4", page=2)) == [
        "HOME-4: page 2, the map has it in section 1"
    ]
    extra = dict(manifest, gaps=manifest["gaps"] + [{"id": "HOME-999", "page": 1, "class": "nope"}])
    assert problems(gap_map, extra) == [
        "HOME-999: not a gap of the map",
        "HOME-999: unknown class nope",
    ]

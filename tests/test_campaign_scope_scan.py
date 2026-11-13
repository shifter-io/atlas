"""The scope suite's static scans: delete words, raw client access and tampering with the
guard's private state appear nowhere outside scope.py and do_api.py, and nothing in src/ or
scripts/ goes around the client."""

from __future__ import annotations

import ast
import re

import pytest
from campaign_scope_cases import CAMPAIGN_SRC, ROOT


def _calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _strings(tree):
    """String constants that are not docstrings."""
    docs = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
    ]


def _writes(tree, attribute):
    """An assignment to `<x>.attribute` (also in a tuple), or `attribute=` passed to a call."""
    kinds = ast.Assign | ast.AugAssign | ast.AnnAssign
    targets = [
        n
        for node in ast.walk(tree)
        if isinstance(node, kinds)
        for t in getattr(node, "targets", [getattr(node, "target", None)])
        for n in ast.walk(t)
    ]
    return any(isinstance(n, ast.Attribute) and n.attr == attribute for n in targets) or any(
        keyword.arg == attribute for call in _calls(tree) for keyword in call.keywords
    )


def _names(tree):
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {alias.name.split(".")[0] for alias in node.names}
            names |= {node.module.split(".")[0]} if getattr(node, "module", None) else set()
    return names


# "delete" as a method or command word in any letter case; not inside a name like delete-audit.
DELETE_WORD = re.compile(r"(?<![\w-])delete(?![\w-])", re.IGNORECASE)


PRIVATE_STATE = {"_GUARD", "_OWNED", "_created", "_projects", "_named", "_assign_failed"}
TABLES = {"ALLOWED", "PATHS", "BODIES", "OWNED_WRITES"}
PATCHABLE = TABLES | {
    "monotonic",
    "utcnow",
    "require_guard",
    "check_allowed",
    "OWN_UNASSIGNED_SECONDS",
    "PROJECT_CACHE_SECONDS",
}
MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}


def _target_names(node):
    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
    for target in targets:
        while isinstance(target, ast.Subscript):
            target = target.value
        if isinstance(target, (ast.Attribute, ast.Name)):
            yield target.attr if isinstance(target, ast.Attribute) else target.id


def tampering(source: str, reflective: bool) -> list[str]:
    """r1: touching the guard's private state or patching its tables, from outside the pair.

    `reflective` also refuses vars/setattr/delattr/globals and a getattr that is not a plain
    public attribute of `self` (campaign code only; the rest of src/ uses getattr freely).
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in PRIVATE_STATE | {"__dict__"}:
            found.append(f"private {node.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (node.value in PRIVATE_STATE or node.value.startswith("_GU"))
        ):
            found.append(f"private name {node.value!r}")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            found += [f"patches {t}" for t in _target_names(node) if t in PATCHABLE]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (node.func.attr in MUTATORS)
            and _call_name(ast.Call(func=node.func.value, args=[], keywords=[])) in TABLES
        ):
            found.append(f"mutates {node.func.attr}")
        elif (
            reflective
            and isinstance(node, ast.Call)
            and _call_name(node) in {"vars", "setattr", "delattr", "globals"}
        ):
            found.append(f"reflection {_call_name(node)}")
        elif (
            reflective
            and isinstance(node, ast.Call)
            and _call_name(node) == "getattr"
            and not (isinstance(node.args[0], ast.Name) and node.args[0].id == "self")
        ):
            found.append("reflection getattr")
    return found


def scan_violations(source: str, name: str) -> list[str]:
    """Rule breaks in one campaign source file; scope.py and do_api.py are the guarded pair."""
    tree, guarded = ast.parse(source), name in ("scope.py", "do_api.py")
    strings, names, found = _strings(tree), _names(tree), []
    if not guarded and any(DELETE_WORD.search(v) for v in strings):
        found.append("delete word")
    if not guarded and names & {"_GUARD", "_OWNED", "request", "_attempt", "_session", "aiohttp"}:
        found.append("raw client access")
    if re.search(r"/v2/tags/[^\n]*resources", source):  # CAMP-1h: never tag or untag
        found.append("tag resources")
    if name not in ("scope.py", "contracts.py") and "assign_failed" in strings:
        found.append("assign_failed set outside scope.py")
    if name != "scope.py" and _writes(tree, "assign_failed_at"):  # CAMP-1k: the ledger mark
        found.append("assign_failed_at set outside scope.py")
    for call in _calls(tree):
        call_name = _call_name(call).lower()
        if call_name != "guarded_delete" and any(
            word in call_name for word in ("delete", "destroy", "remove_droplet")
        ):
            found.append(f"call {call_name}")
        if name != "scope.py" and call_name == "assign_project":
            found.append("assign_project outside scope.py")
    return found + ([] if guarded else tampering(source, reflective=True))


def side_doors(source: str) -> list[str]:
    """Ways around the client from any module or script (m4)."""
    private = {"_GUARD", "_OWNED", "_attempt", "_session", "_created", "_assign_failed", "_named"}
    tree, found = ast.parse(source), []
    if "api.digitalocean.com" in source or re.search(r"\bdoctl\b", source):
        found.append("direct API or doctl")
    imports = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith(("do_api", "scope"))
    ]
    if {a.name for n in imports for a in n.names} & private or _names(tree) & {
        "_GUARD",
        "_OWNED",
        "_assign_failed",
    }:
        found.append("private client name")
    return found + tampering(source, reflective=False)


@pytest.mark.parametrize(
    "source",
    [
        'await cloud.request("delete", "/v2/droplets", params={"tag_name": "prod"})',
        'await cloud._session.request("Delete", url)',
        'run(["doctl", "compute", "droplet", "delete", "1"])',
        "await cloud.assign_project(pid, ids, guard=token)",
        "import aiohttp",
        'record.status = "assign_failed"',
        "worker.assign_failed_at = now",
        'DropletRecord("n", "lon1", 3, NAME, "1", assign_failed_at="x")',
        "await cloud.destroy(droplet)",
    ],
)
def test_the_scans_catch_known_bypasses(source):
    """B1 and m4: each of these passed the 53ca3ea scan."""
    assert scan_violations(source, "launch.py")
    assert (
        side_doors('URL = "https://api.digitalocean.com/v2"')
        and side_doors("from atlas.campaign.scope import _GUARD")
        and side_doors("x = ['doctl']")
    )
    assert side_doors("from atlas.campaign.do_api import _OWNED")


@pytest.mark.parametrize(
    "source",
    [
        'do_api.ALLOWED[("POST", "/v2/droplets/1/actions")] = (frozenset(), False)',
        "do_api.ALLOWED.update({})",
        'scope.PATHS["volume"] = "/v2/volumes/{}"',
        'cloud._created.add("123")',
        'getattr(cloud, "_created")',
        'scope._projects[cloud] = {"p": (0, {"1"})}',
        'scope._named[cloud] = {"p"}',
        "scope.monotonic = lambda: 0.0",
        'token = vars(scope)["_GU" + "ARD"]',
        "del scope.PATHS",
        'await cloud.request("POST", "/v2/firewalls", body=rules, guard=do_api._OWNED)',
        'token = vars(do_api)["_OWNED"]',
        "do_api.OWNED_WRITES = {}",
        "do_api.BODIES.clear()",
    ],
)
def test_the_scans_catch_tampering_with_private_state(source):
    """r1: each of these passed both scans at the reviewed head."""
    assert scan_violations(source, "launch.py") and side_doors(source)


@pytest.mark.parametrize("name", ["do_api.py", "scope.py", "launch.py"])
def test_the_scan_refuses_tagging_resources_even_in_the_guarded_pair(name):
    """CAMP-1h: tags are read and created only; nothing may put one on or off a resource."""
    for source in (
        'PATH = f"/v2/tags/{tag}/resources"',
        'await self._session.request("DELETE", "/v2/tags/" + tag + "/resources")',
    ):
        assert "tag resources" in scan_violations(source, name)


def test_static_scan_allows_delete_only_behind_the_guard():
    """Covers every file in src/atlas/campaign/, including later tasks' files."""
    files = sorted(CAMPAIGN_SRC.rglob("*.py"))
    assert {"scope.py", "do_api.py", "contracts.py"} <= {f.name for f in files}
    for path in files:
        assert scan_violations(path.read_text(), path.name) == [], path.name
    scope_text = (CAMPAIGN_SRC / "scope.py").read_text()
    assert "tag_name" not in scope_text
    deletes = [
        c
        for c in _calls(ast.parse(scope_text))
        if any(isinstance(a, ast.Constant) and str(a.value).upper() == "DELETE" for a in c.args)
    ]
    assert len(deletes) == 1 and {k.arg for k in deletes[0].keywords} == {"guard"}


def test_static_scan_of_all_sources_finds_no_side_door():
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        if not (path.parent == CAMPAIGN_SRC and path.name in ("do_api.py", "scope.py")):
            assert side_doors(path.read_text()) == [], path

"""The calculation's two implementation ids: what a deploy changes (CALC-PERF-2).

The implementation id (calculator.IMPLEMENTATION_ID) names the code of a build: the build's input
fingerprint (incremental.read()), the snapshot head the API serves and what a build carries from
the current one (carried_lists.py, network_scores.overlap()) change with it. The list id
(calculator.LISTS_ID) is the implementation in every daily list's signature (lists.signatures()):
a new one makes the next build measure every list again.

Both were the SHA-256 of the hashed modules' text, comments and docstrings included, so a
docstring edit re-measured every list: the first build after such a deploy took minutes more
and about twice the memory. ids() hashes each module's code instead (code(): its syntax tree
without docstrings; comments are not in it), and leaves NOT_LISTS out of the list id: modules
that score, rank, price or carry what the lists hold, never a list's rows or figures. A deploy
that changes only comments, docstrings or those modules keeps every stored list. A module added
to the hashed ones counts for both ids until it is named here. Should a module left out change a
list after all, a recount still finds it: it verifies every signed list against its stored twin
and fails (list_write.write_lists()).

A syntax tree's text can change with the Python version, so a Python upgrade may change both ids.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

# The hashed modules (by file stem) that make none of a daily list's rows or figures.
NOT_LISTS = frozenset(
    "network network_scores scores target_scores value_rank value_all latency_days latency_store "
    "latency_carry score_history card_success traffic".split()
)
DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def code(text: str) -> str:
    """The code of a module's text: its syntax tree without docstrings, as ast.dump() writes it
    (no comments, no line numbers)."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, DOCUMENTED) and node.body and _docstring(node.body[0]):
            node.body = node.body[1:]
    return ast.dump(tree)


def _docstring(statement) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def ids(paths) -> tuple[str, str]:
    """The implementation id and the list id of the modules at paths, in order: the digest of
    each module's name and code() digest, the list id without NOT_LISTS."""
    every, listed = hashlib.sha256(), hashlib.sha256()
    for path in map(Path, paths):
        line = f"{path.name}:{hashlib.sha256(code(path.read_text()).encode()).hexdigest()}\n"
        every.update(line.encode())
        if path.stem not in NOT_LISTS:
            listed.update(line.encode())
    return every.hexdigest(), listed.hexdigest()

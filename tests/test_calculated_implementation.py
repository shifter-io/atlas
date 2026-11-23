"""The implementation id and the list id (CALC-PERF-2): a deploy that changes only comments,
docstrings or modules that make no list keeps every stored list."""

from pathlib import Path

from atlas import detect, no_exit_answers, traffic, verdicts
from atlas.calculated import (
    aggregate,
    browser,
    calculator,
    implementation,
    incremental,
    list_write,
    lists,
    poolfig,
    sqlwindow,
)
from atlas.calculated.implementation import NOT_LISTS, code, ids

MODULE = '''"""A module."""
import math

# A comment.
def area(r):
    """The area."""
    return math.pi * r * r  # inline


class Shape:
    """A shape."""

    async def size(self):
        "One line."
        return "not a docstring" if self else None
'''
BARE = """import math
def area(r):
    return math.pi * r * r
class Shape:
    async def size(self):
        return "not a docstring" if self else None
"""


def test_comments_and_docstrings_are_not_code():
    edited = MODULE.replace("A module.", "Another text,\non two lines.").replace("The area", "Pi")
    edited = edited.replace("# A comment.", "# Changed.\n# One more.").replace("  # inline", "")
    assert code(edited) == code(MODULE) == code(BARE)


def test_a_change_to_the_code_changes_it():
    changes = (
        ("r * r", "r ** 2"),
        ('"not a docstring"', '"another string"'),  # a string that is not a docstring is code
        ("def area(r):", "def area(r=1):"),
        ("import math", "import math as m\nmath = m"),
    )
    for old, new in changes:
        assert code(MODULE.replace(old, new)) != code(MODULE), old


def test_only_a_module_of_the_lists_changes_the_list_id(tmp_path):
    paths = [tmp_path / "aggregate.py", tmp_path / "scores.py"]
    for path in paths:
        path.write_text(MODULE)
    before = ids(paths)
    paths[0].write_text(MODULE.replace("The area.", "The area of a circle."))
    assert ids(paths) == before  # a docstring: neither id
    paths[1].write_text(MODULE.replace("r * r", "r ** 2"))  # scores.py makes no list
    changed = ids(paths)
    assert changed[0] != before[0] and changed[1] == before[1]
    paths[0].write_text(MODULE.replace("r * r", "r ** 2"))  # aggregate.py makes the lists
    assert ids(paths)[0] not in (before[0], changed[0]) and ids(paths)[1] != before[1]
    # The same code under another module name is another module.
    assert ids([tmp_path / "scores.py"])[0] != ids([tmp_path / "aggregate.py"])[0]


def test_the_list_id_leaves_out_only_hashed_modules_that_make_no_list():
    here = Path(calculator.__file__).parent
    paths = [here / f"{name}.py" for name in calculator.SOURCES.split()]
    paths += [Path(m.__file__) for m in (detect, traffic, browser, verdicts, no_exit_answers)]
    assert (calculator.IMPLEMENTATION_ID, calculator.LISTS_ID) == implementation.ids(paths)
    assert calculator.IMPLEMENTATION_ID != calculator.LISTS_ID
    assert NOT_LISTS <= {p.stem for p in paths}  # no stale name
    # What counts, signs, writes and reads back a daily list counts for the list id.
    makers = (aggregate.breakdowns, aggregate.pool, aggregate.pool_lists, poolfig.pool_figures)
    makers += (sqlwindow.window_inputs, list_write.prepare, list_write.write_lists)
    makers += (lists.signatures, lists.persist, incremental.read, incremental.Incremental)
    for maker in makers:
        stem = maker.__module__.rsplit(".", 1)[1]
        assert stem in {p.stem for p in paths} and stem not in NOT_LISTS, maker

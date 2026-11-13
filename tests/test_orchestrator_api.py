"""The checks /v1/results makes on an upload's summary.json before it is ingested."""

import pytest

from atlas.orchestrator.api import _check_summary
from atlas.orchestrator.archive import ArchiveError

NODE = {"provider": "fixture", "product": "residential"}
RUN = {"target_id": 7}
OURS = {"config": {"provider": "fixture", "product": "residential"}, "target": {"id": 7}}


def test_a_summary_of_this_nodes_run_passes():
    _check_summary(OURS, NODE, RUN)


@pytest.mark.parametrize(
    ("summary", "error"),
    [
        ([], "summary.json must be an object"),
        ({**OURS, "config": {"provider": "other"}}, "another provider or product"),
        ({"target": {"id": 7}}, "another provider or product"),
        ({**OURS, "target": {"id": 8}}, "results are for another target"),
        ({**OURS, "target": None}, "results are for another target"),
    ],
)
def test_a_summary_of_another_run_is_refused(summary, error):
    with pytest.raises(ArchiveError, match=error):
        _check_summary(summary, NODE, RUN)


def test_a_config_that_is_not_an_object_is_unreadable():
    with pytest.raises(AttributeError):
        _check_summary({**OURS, "config": ["fixture"]}, NODE, RUN)

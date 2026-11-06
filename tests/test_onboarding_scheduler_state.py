"""The region checkpoint helpers moved out of scheduler.py (FILE-8)."""

from test_onboarding import START
from test_onboarding_sequential import make

from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding import scheduler
from proxy_index.onboarding.files import atomic_json
from proxy_index.onboarding.scheduler_state import job_directory, new_state, recover_running

SAVED = {
    "status": "completed",
    "finished_at": "2026-01-01T00:10:00Z",
    "observations": 7,
    "requests": 9,
    "errors": 2,
    "duration_seconds": 600.0,
    "stop_reason": "unique_ratio",
}


def test_scheduler_keeps_exporting_the_state_helpers():
    assert scheduler.new_state is new_state
    assert scheduler.job_directory is job_directory


def test_recover_running_restores_a_saved_summary_and_interrupts_the_rest(tmp_path):
    m = campaign.load(make(tmp_path))
    region = next(iter(m["regions"]))
    state = new_state(m, region, m["settings"])
    saved_job, partial_job = state["jobs"][0], state["jobs"][1]
    saved_job["status"] = partial_job["status"] = "running"
    directory = tmp_path / "regions" / region
    atomic_json(job_directory(directory, saved_job) / "summary.json", {**saved_job, **SAVED})
    meta = job_directory(directory, partial_job) / "observations-000.jsonl.meta.json"
    atomic_json(meta, {"rows": 3})
    state["progress"] = {"requests": 5, "successes": 4}

    recover_running(state, directory, START.timestamp())

    assert {key: saved_job[key] for key in SAVED} == SAVED
    assert "counters_partial" not in saved_job
    assert partial_job["status"] == "interrupted"
    assert partial_job["finished_at"] == campaign.timestamp(START.timestamp())
    counts = (partial_job["observations"], partial_job["requests"], partial_job["errors"])
    assert counts == (3, 5, 1)
    assert partial_job["counters_partial"] is True
    assert all(job["status"] == "pending" for job in state["jobs"][2:])

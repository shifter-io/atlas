"""LOWAVAIL-1 (#1181) step 1: the read-only data on countries where a network has almost no exits.

The October census peak of each country (the formulas' highest hourly unique IPs), a daily
test's failed answers, each network's "no exit" answers (its grammar's no_inventory_statuses) and
the report's tables comparing the answers of small and big countries.
"""

import json
from datetime import UTC, datetime

from test_country_verdicts import daily_test

from atlas import availability
from atlas.availability import Network, census_peaks, failures, no_exit, render, summarize
from atlas.storage import ingest

pytest_plugins = ["test_storage"]

# The networks whose daily tests showed their "no exit" answers (docs/reports/lowavail-answers.md).
NO_EXIT = {
    "dataimpulse": ("502", "503"),
    "decodo": ("502", "522"),
    "evomi": ("409", "500"),
    "fleetproxy": ("502",),
    "geonode": ("465",),
    "infatica": ("503",),
    "iproyal": ("503",),
    "massive": ("470", "471"),
    "nodemaven": ("406", "503"),
    "oxylabs": ("502", "522"),
    "proxy-seller": ("503",),
    "proxyempire": ("502",),
    "rayobyte": ("551",),
    "shifter": ("502",),
    "soax": ("502",),
    "webshare": ("422", "502"),
}


def formula(directory, network, countries):
    """A formula file of `network` whose countries have these hourly unique IPs."""
    entries = {c: {"unique_ips": hours} for c, hours in countries.items()}
    path = directory / f"{network}.json"
    path.write_text(json.dumps({"network": network, "countries": entries}))


def test_census_peaks_are_each_countrys_highest_hour(tmp_path):
    formula(tmp_path, "shifter", {"AD": [3, 9, None, 7], "DE": [400, 900], "TV": [None]})
    formula(tmp_path, "evomi", {"FR": [12]})
    (tmp_path / "notes.txt").write_text("not a formula")
    assert census_peaks(tmp_path) == {
        "evomi": {"FR": 12},
        "shifter": {"AD": 9, "DE": 900, "TV": 0},
    }


def test_failures_are_error_statuses_and_answers_without_a_status():
    statuses = {"200": 50, "302": 4, "502": 30, "551": 0, "unknown": 2}
    errors = {"timeout": 5, "connection": 1, "http_status": 30, "body_too_large": 3, "tls": 0}
    assert failures(statuses, errors) == {"502": 30, "timeout": 5, "connection": 1}


def test_no_exit_answers_come_from_the_grammars():
    for network, answers in NO_EXIT.items():
        assert no_exit(network) == answers
    for network in ("byteful", "proxy-cheap"):
        assert no_exit(network) == ()  # no daily test of a small country yet
    assert no_exit("fixture") == ()  # a network without a grammar


def row(provider, country, ips, statuses, errors=None, requests=100):
    """A TESTS row."""
    return {
        "provider": provider,
        "country": country,
        "unique_ips": ips,
        "requests": requests,
        "statuses": statuses,
        "errors": errors or {},
    }


def test_summarize_splits_small_and_big_countries():
    peaks = {"rayobyte": {"AD": 4, "DE": 900, "FR": 60, "IT": 30}, "evomi": {"NE": 2}}
    tests = [
        row("rayobyte", "AD", 2, {"200": 10, "551": 85}, {"timeout": 5}),
        row("rayobyte", "DE", 700, {"200": 96, "551": 2}, {"timeout": 2}),
        row("rayobyte", "FR", 0, {"551": 100}),  # big in the census, none today
        row("rayobyte", "IT", 25, {"200": 100}),  # neither small nor big
        row("massive", "VA", 1, {"200": 50, "470": 50}),  # a network without a formula
    ]
    found = summarize(tests, peaks, {"rayobyte": ("551",)})
    rayobyte = found["rayobyte"]
    assert (rayobyte.census_small, rayobyte.tested) == (1, {"AD", "DE", "FR", "IT"})
    assert (rayobyte.daily_small, rayobyte.zero, rayobyte.big_small) == (
        {"AD", "FR"},
        {"FR"},
        {"FR"},
    )
    assert (rayobyte.small.tests, rayobyte.small.requests) == (2, 200)
    assert rayobyte.small.failed == {"551": 185, "timeout": 5}
    assert (rayobyte.big.tests, rayobyte.big.share("551")) == (1, 0.02)
    assert rayobyte.covered() == 185 / 190
    assert found["evomi"].small.tests == 0 and found["evomi"].covered() is None
    assert found["evomi"].small.share("409") == 0.0
    assert (found["massive"].census_small, found["massive"].small.tests) == (0, 1)


def test_render_lists_countries_then_answers():
    peaks = {"rayobyte": {"AD": 4, "DE": 900}}
    tests = [row("rayobyte", "AD", 2, {"551": 90}), row("rayobyte", "DE", 700, {"551": 1})]
    network = summarize(tests, peaks, {"rayobyte": ("551",)})["rayobyte"]
    idle = Network("shifter", 19, ())
    text = render({"shifter": idle, "rayobyte": network})
    lines = text.splitlines()
    assert lines[2] == "| rayobyte | 1 | 2 | 1 | 0 | 0 |"
    assert lines[3] == "| shifter | 19 | 0 | 0 | 0 | 0 |"
    assert "| rayobyte | 1 | 551 90.0% / 1.0% | 551 | 100.0% |" in lines
    assert "| shifter | 0 | - | - | - |" in lines
    assert text.endswith("|\n")


async def test_report_reads_the_daily_tests_since_a_time(database, tmp_path, capsys):
    formula(tmp_path, "fixture", {"DE": [400], "FR": [3]})
    for directory in (await daily_test(tmp_path, 12, "de"), await daily_test(tmp_path, 6, "fr")):
        assert ingest(directory, database)["imported"]
    text = availability.report(database, tmp_path, datetime(2000, 1, 1, tzinfo=UTC))
    assert "| fixture | 1 | 2 | 1 | 0 | 0 |" in text
    assert "| fixture | 1 | - | - | - |" in text  # FR answered every request
    later = availability.report(database, tmp_path, datetime(2999, 1, 1, tzinfo=UTC))
    assert "| fixture | 1 | 0 | 0 | 0 | 0 |" in later
    assert availability.main(["--database-file", str(database), "--formulas", str(tmp_path)]) == 0
    assert capsys.readouterr().out == text

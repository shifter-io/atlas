"""LOWAVAIL-1 (#1181) step 2: a network's country status from its daily test.

The cases are real daily tests of 14-15 November 2026 (their counts, docs/reports/
lowavail-answers.md): a country, its October census peak, its unique IPs and how its requests
ended.
"""

import tomllib

import pytest

from atlas.availability import FEW_IPS, no_exit
from atlas.country_status import (
    COUNTS,
    EXPECTED,
    FAILING,
    LOW,
    NETWORK,
    NO_EXITS,
    STATUSES,
    DailyTest,
    explained,
    first_day,
    network_problem,
    normally_big,
    status,
)
from atlas.detect import METHODOLOGY_FILE
from atlas.verdicts import STATUS

# (network, country, census peak, unique IPs, requests, status codes, errors) of real tests
RAYOBYTE_CK = ("rayobyte", "CK", 2, 1, 2999, {"200": 71, "504": 10, "551": 2890}, {"timeout": 28})
MASSIVE_CK = ("massive", "CK", 1, 1, 2999, {"200": 6, "470": 2993}, {})
WEBSHARE_CF = ("webshare", "CF", 7, 0, 2999, {"502": 2998}, {"timeout": 1})
PROXYEMPIRE_BI = ("proxyempire", "BI", 5, 0, 2999, {"502": 2999}, {})
FLEETPROXY_BI = ("fleetproxy", "BI", 8, 1, 2999, {"200": 63, "502": 1}, {"timeout": 2935})
# Normally big countries whose test found under 10 IPs
INFATICA_MX = ("infatica", "MX", 34241, 0, 2999, {"407": 2999}, {})  # a login error, 01:00
FLEETPROXY_NE = ("fleetproxy", "NE", 184, 1, 2999, {"200": 27}, {"timeout": 2972})
EVOMI_LU = ("evomi", "LU", 400, 6, 2899, {"200": 2899}, {})  # few exits, answered normally
WEBSHARE_SC = ("webshare", "SC", 253, 0, 2999, {"502": 2999}, {})
GEONODE_BI = ("geonode", "BI", 57, 0, 1499, {"200": 1492, "464": 1}, {"timeout": 6})
# A big country's normal test
SHIFTER_US = ("shifter", "US", 212191, 4917, 3000, {"200": 2990, "502": 1}, {"timeout": 9})


def case(row, previous=None, network_down=False, census=True, recent=None):
    """The status of a real test, as its country's first daily test (previous None) or after a
    previous test that found `previous` IPs (and recent ones up to `recent`)."""
    network, _, peak, ips, requests, statuses, errors = row
    return status(
        DailyTest.of(ips, requests, statuses, errors),
        no_exit(network),
        previous=previous,
        census=peak if census else None,
        network_down=network_down,
        recent=recent,
    )


def test_the_statuses_extend_the_apis_codes():
    assert STATUSES[:2] == STATUS[:2] and LOW == "too_few_ips" and NO_EXITS == STATUS[3]
    assert EXPECTED == {LOW, NO_EXITS}


@pytest.mark.parametrize(
    ("row", "expected"),
    [(RAYOBYTE_CK, LOW), (MASSIVE_CK, LOW), (WEBSHARE_CF, NO_EXITS), (PROXYEMPIRE_BI, NO_EXITS)],
)
def test_a_small_country_answering_no_exit_is_low_availability(row, expected):
    assert case(row) == expected
    assert case(row, previous=3) == expected


def test_timeouts_explain_a_country_that_is_not_normally_big():
    # The owner's answer to the report's Question 1 (option 1, 2026-11-15).
    assert case(FLEETPROXY_BI) == LOW  # census 8: its timeouts are its "no exit" answer
    assert case(FLEETPROXY_BI, previous=2) == LOW
    assert case(FLEETPROXY_BI, previous=2, network_down=True) == NETWORK
    assert case(FLEETPROXY_BI, previous=2, recent=300) == FAILING  # recently big: visible
    assert case(FLEETPROXY_NE, previous=1) == FAILING  # census 184: a big country stays visible
    gateway = DailyTest.of(0, 100, {"504": 70}, {"timeout": 30})  # the gateway's own timeout
    assert status(gateway, (), census=8) == NO_EXITS
    assert status(gateway, (), census=50, previous=3) == FAILING
    refused = DailyTest.of(2, 100, {"200": 20}, {"connection": 80})  # never a timeout
    assert status(refused, (), census=8) == FAILING


def test_a_big_country_needs_two_tests_in_a_row_under_ten():
    for row in (INFATICA_MX, FLEETPROXY_NE, EVOMI_LU, WEBSHARE_SC, GEONODE_BI):
        assert case(row) == COUNTS  # the census had 50 or more: the first day counts
        assert case(row, previous=120) == COUNTS  # so did the previous test
    assert case(EVOMI_LU, previous=6) == LOW  # answered normally, from 6 exits, twice
    assert case(WEBSHARE_SC, previous=0) == NO_EXITS
    assert case(GEONODE_BI, previous=7) == NO_EXITS  # its answers came from other countries
    assert case(INFATICA_MX, previous=0) == FAILING  # 407 is a login error: visible
    assert case(FLEETPROXY_NE, previous=1) == FAILING


def test_the_census_and_the_previous_test_both_make_a_country_big():
    assert case(EVOMI_LU, census=False) == LOW  # no census exit and no previous test
    assert case(EVOMI_LU, previous=30) == COUNTS  # the census had 400: a first day under 10
    assert first_day(None, None) is False and first_day(None, 50) is True
    assert first_day(49, 900) is True and first_day(50, None) is True
    assert first_day(9, 900) is False  # the previous test was already under 10
    assert first_day(None, 20, recent=300) is True and first_day(30, 20, recent=49) is False
    assert normally_big(None, None, None) is False and normally_big(3, None, 50) is True


def test_ten_ips_count_whatever_the_answers():
    assert case(SHIFTER_US) == COUNTS
    assert status(DailyTest.of(10, 100, {"551": 90}, {}), ("551",), network_down=True) == COUNTS


def test_the_network_guard_keeps_every_small_country_visible():
    for row in (RAYOBYTE_CK, WEBSHARE_CF, FLEETPROXY_BI):
        assert case(row, network_down=True) == NETWORK
    assert case(EVOMI_LU, previous=6, network_down=True) == NETWORK


def test_explained_failures_are_mostly_no_exit_or_hardly_any():
    assert explained(DailyTest.of(3, 100, {"200": 91, "502": 9}, {}), ())  # 9% failed
    assert not explained(DailyTest.of(3, 100, {"200": 90, "502": 10}, {}), ())
    assert explained(DailyTest.of(3, 100, {"502": 50}, {"timeout": 50}), ("502",))  # half
    assert not explained(DailyTest.of(3, 100, {"502": 49}, {"timeout": 51}), ("502",))
    assert explained(DailyTest(0, 0), ())  # no request: nothing failed


def test_the_network_guard_holds_when_most_big_countries_fail():
    down = DailyTest.of(0, 100, {"200": 40, "502": 60}, {})  # a "no exit" answer fails too
    slow = DailyTest.of(5, 100, {"200": 50}, {"timeout": 50})
    fine = DailyTest.of(900, 100, {"200": 98}, {"timeout": 2})
    assert network_problem([down, slow, fine])
    assert not network_problem([down, slow, fine, fine])  # half is not most
    assert not network_problem([fine, fine, down])
    assert not network_problem([DailyTest(0, 0), fine])  # a test without requests is not counted
    assert not network_problem([])
    big = [DailyTest.of(*row[3:]) for row in (INFATICA_MX, FLEETPROXY_NE, WEBSHARE_SC)]
    assert network_problem(big)
    assert not network_problem(big[:2])  # under GUARD_MIN big countries: their own failures


def test_a_gateway_that_refuses_without_a_status_is_never_no_exits():
    # A refused gateway connection, an unresolved gateway or a SOCKS login carries no status.
    refused = DailyTest.of(0, 2999, {}, {"proxy_connect": 2990, "dns": 9})
    assert refused.failing() == 2999
    assert status(refused, ("502",)) == FAILING
    assert status(refused, ("502",), previous=0, census=900) == FAILING
    assert network_problem([refused, refused, DailyTest.of(900, 100, {"200": 100}, {})])
    # A proxy_connect with the gateway's status counts once, as that status.
    with_status = DailyTest.of(0, 100, {"502": 100}, {"proxy_connect": 100}, completed=100)
    assert with_status.failed == {"502": 100} and status(with_status, ("502",)) == NO_EXITS


def test_the_pulse_pacing_stays_under_few_ips_whatever_the_methodologys_minimum():
    """COUNTRY-MIN-IPS-1 (#1253): a country whose daily test found 1 to 9 IPs counts in the
    figures (the methodology's country_min_ips) but is still low availability for the pulses
    (availability.FEW_IPS): not pulsed, its numbers come from its daily tests alone."""
    minimum = tomllib.loads(METHODOLOGY_FILE.read_text())["country_min_ips"]
    assert minimum < FEW_IPS
    answered = {"200": 2899}  # few exits, every request answered (EVOMI_LU)
    for ips in range(minimum, FEW_IPS):
        assert status(DailyTest.of(ips, 2899, answered, {}), no_exit("evomi")) == LOW, ips
    assert status(DailyTest.of(FEW_IPS, 2899, answered, {}), no_exit("evomi")) == COUNTS

"""storage.ingest_many's waits on the database (INGEST-RTT-1): a pulse takes at most five round
trips, and a batch takes as many round trips and statements as a batch of one.

ingest_cases.Proxy counts the client's protocol messages between it and PostgreSQL; the
connection's startup and authentication are not counted.
"""

from ingest_cases import Proxy, daily_test, proxied, pulse

from atlas.providers.credentials import read_credentials
from atlas.storage import ingest, ingest_many

pytest_plugins = ["test_storage"]


def counted(database, tmp_path, batches, **options):
    """(round trips, statements) of ingest_many for each of batches, through the proxy."""
    values = read_credentials(database, "postgres")
    found = []
    with Proxy(values["host"], values["port"]) as proxy:
        credentials = proxied(values, proxy, tmp_path)
        for batch in batches:
            proxy.reset()
            ingest_many(batch, credentials, **options)
            assert len(proxy.frontends) == 1
            found.append((proxy.round_trips(), proxy.statements()))
    return found


def test_a_pulse_takes_at_most_five_round_trips(database, tmp_path):
    first, second = pulse(tmp_path / "p0", 0), pulse(tmp_path / "p1", 1)
    values = read_credentials(database, "postgres")
    with Proxy(values["host"], values["port"]) as proxy:
        credentials = proxied(values, proxy, tmp_path)
        assert ingest(first, credentials, ip_rows=False)["imported"]
        assert proxy.round_trips() <= 5
        proxy.reset()
        assert not ingest(first, credentials, ip_rows=False)["imported"]  # a replay
        assert ingest(second, credentials, ip_rows=False)["imported"]
        assert proxy.round_trips() <= 10


def test_pulse_batches_take_the_round_trips_of_one(database, tmp_path):
    pulses = [pulse(tmp_path / f"p{n}", n) for n in range(10)]
    ingest(pulses[0], database, ip_rows=False)  # the day's partitions, so no batch creates them
    one, many = counted(database, tmp_path, [pulses[1:2], pulses[2:]], ip_rows=False)
    assert one == many and one[0] <= 5


def test_daily_test_batches_take_the_round_trips_of_one(database, tmp_path):
    tests = [daily_test(tmp_path / f"d{n}", n, ips=60, repeats=20) for n in range(5)]
    ingest(tests[0], database)
    one, many = counted(database, tmp_path, [tests[1:2], tests[2:]])
    assert one == many and one[0] <= 5

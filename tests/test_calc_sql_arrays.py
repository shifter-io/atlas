"""CALC-PERF-3: the hourly memos and window counts send their list parameters as array texts
(calculated/arrays.py), made at C speed instead of psycopg's dump of each element in Python. The
database reads each text as the array the list gives."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from atlas.calculated.arrays import array, arrays
from atlas.storage import connect

pytest_plugins = ["test_storage"]

AT = datetime(2026, 11, 19, 10, 5, 0, 123456, tzinfo=UTC)
CASES = {
    "bigint": [1, 0, -7, 2**62, None],
    "int": [3, None, 4],
    "text": ["hourly-v1-ab", "", "NULL", 'a "quoted" \\ back,slash {brace}', "ünï cödé", None],
    "timestamptz": [AT, AT.astimezone(timezone(timedelta(hours=5, minutes=30))), None],
}


def test_the_database_reads_each_array_text_as_its_list(database):
    with connect(database) as conn:
        for kind, values in CASES.items():
            text = array(values)
            assert isinstance(text, str), kind
            query = f"SELECT %s::{kind}[],%s::{kind}[]"
            from_text, from_list = conn.execute(query, (text, values)).fetchone()
            assert from_text == from_list == values, kind
        assert conn.execute("SELECT %s::bigint[]", (array([]),)).fetchone() == ([],)


def test_other_values_stay_a_list():
    for values in ([True, False], [Decimal("1.5")], [datetime(2026, 11, 19)], [1, 2.5], [[1]]):
        assert array(values) is values
    found = arrays({"keys": [1, 2], "per_run": True, "release": None})
    assert found == {"keys": "{1,2}", "per_run": True, "release": None}
    assert arrays(([AT], 5)) == ["{" + AT.isoformat() + "}", 5]

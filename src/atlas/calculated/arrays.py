"""List parameters as PostgreSQL array texts (CALC-PERF-3).

psycopg dumps a list parameter element by element in Python, about 16 µs an element, and the hourly
memos and window counts send hundreds of thousands of them every hour (sqlwindow.window_inputs(),
hourly_windows._rows()). array() makes the same array as one PostgreSQL array text at C speed. A
statement casts the parameter (%s::bigint[], %s::timestamptz[], ...), which reads either form as
the same array.
"""

from __future__ import annotations

from datetime import datetime


def _text(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _moment(value):
    if value.tzinfo is None:  # a naive time: the database's time zone would read it
        raise TypeError("a datetime without its time zone")
    return value.isoformat()


# Each kind of element an array text holds, by its exact type (a bool is not an int here).
ELEMENTS = {type(None): lambda _: "NULL", int: str, str: _text, datetime: _moment}


def array(values):
    """values as one PostgreSQL array literal (text) when each is an int, a str, a datetime with
    its time zone or None, else values itself."""
    try:
        return "{" + ",".join(ELEMENTS[type(v)](v) for v in values) + "}"
    except (KeyError, TypeError):
        return values


def arrays(params):
    """params (a sequence or a mapping) with each list as array() makes it."""
    if isinstance(params, dict):
        return {k: array(v) if isinstance(v, list) else v for k, v in params.items()}
    return [array(v) if isinstance(v, list) else v for v in params]

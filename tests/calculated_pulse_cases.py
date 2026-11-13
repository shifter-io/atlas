"""Shared helpers of the calculated pulse tests: pulse batches on a fixed clock, their settled
detection times and the detection thresholds."""

from datetime import UTC, datetime, timedelta

from census_pulse_cases import batch, batch_run

from atlas.detect import thresholds

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SETTINGS = thresholds() | {"initial_lookback_minutes": 60 * 24 * 30}


def pulses(conn, product_id, results, *, start=0, run_keys=None, **options):
    """One finished pulse batch per entry (successes or a dict of counters), 30 minutes apart."""
    for i, result in enumerate(results, start):
        at = T0 + timedelta(minutes=30 * i)
        counters = result if isinstance(result, dict) else {"successes": result}
        b = batch(conn, "pulse", at, **options)
        batch_run(
            conn,
            b,
            product_id,
            at + timedelta(minutes=1),
            run_key=(run_keys or {}).get(i),
            **counters,
        )


def after(i):
    """A detection time that covers pulse i (settled)."""
    return T0 + timedelta(minutes=30 * i + 10)

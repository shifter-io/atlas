# Tests that involve time

CI runs every test on a shared server, next to other jobs. A test that passes only when
the runner is fast fails there about once a run, a different test each time
(CI-FLAKY-TIMING-1). The rule: **a test's result never depends on how fast the runner
is.** Wall-clock time and real sleeps decide nothing.

## The rules

1. **Fake clock for schedules.** Code that waits, retries, times out or checks an age
   takes its clock as a parameter (a `clock=` callable in Python, a `now`/`wait` field
   in Go). The test passes a fake clock that moves only when the code asks for it, then
   asserts the exact schedule. Examples: `StallWatch(limit, clock=...)`
   (`src/atlas/browser/run.py`), `Purger.now` / `Purger.wait` with `fakeClock`
   (`api/internal/server/cdn.go`, `cdn_purge_test.go`).
2. **Wait for the condition, not for a time.** Do not `sleep(0.1)` and then expect
   something to have happened. Wait for the event itself (an `asyncio.Event`, a channel,
   a counter reaching its value) with a generous limit (30 s, a minute) that only stops
   a test that hangs. Example: `test_the_hourly_loop_writes_lines_until_stopped` stops
   the loop once two lines are written.
3. **Overlap by event-loop turns, not by sleeps.** A fake transport that must keep
   requests in flight together holds each one until nothing new has started for a few
   event-loop turns (`await asyncio.sleep(0)`), not for a millisecond. Example:
   `in_flight_counter` in `tests/test_runner_stops.py`.
4. **Lower bounds are safe; upper bounds are not.** "Waited at least `BusyWait`" holds
   on any runner. "Answered within 1 s" does not. To prove that something does not wait,
   make the wait it must not take very long (an hour) and require an answer within the
   generous limit (`answers` in `api/internal/server/uncached_test.go`).
5. **Wall-clock moments are fixtures.** A test that uses `datetime.now()` must pass at
   any time of day, in the last minutes of an hour too. Browser runs end 2 minutes
   before their slot ends, so tests that start one now set
   `run_module.DEADLINE_MARGIN_S = -600`.
6. **Measure work, not elapsed time.** A budget for how much work something does uses
   processor time (`processorTime` in `datasetfile_runa_test.go`), counts, sizes or
   allocations, not `time.Since`.
7. **Do not depend on how concurrent work interleaves.** Two clients sharing one
   rotating fake gateway each see exits that depend on the order of their requests;
   give each its own (`test_census_and_pulse_end_to_end`).

## Real measurements

A few tests measure real latency or memory on purpose: `TestServeFromDatabase` and
`TestPageLatency` (each alone in its own CI step, against `bench.CIGuard`, 5 times the
staging budgets, with one retry) and `TestPeakRSS` (its own process and database).
Add a new one only with a reason in its pull request, run it alone in CI the same way,
and give it a guard well above what a busy runner reaches.

## Before you push

Run a new or changed time-related test 50 times on a loaded test server
(`maestro-run`, with every CPU busy beside it) and expect 0 failures.

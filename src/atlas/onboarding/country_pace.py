"""The optional `country_pace` key of a daily test's manifest (LOWAVAIL-1 step 3, daily.pace).

{country: {"concurrency": n[, "requests": m][, "low_requests": r, "low_seconds": s]}}: a
country's requests at once (its run's start_concurrency, never above the manifest's own
concurrency, which stays the run's and its scope's), for a probe its request limit, and for a
low-availability country its cap while it finds few IPs (RunConfig.low_*; such a country runs
beside the cell's other low ones, onboarding.rounds). Only an ephemeral (version 3) manifest with
`due_countries` has it, and only for countries due. A country without an entry runs at the
manifest's settings, as before.
"""

from __future__ import annotations

KEY = "country_pace"
# A round's low-availability countries running at once (each stops within its low cap: about
# daily.pace.LOW_SECONDS plus one request's timeout), so a dozen of them take about a minute.
LOW_PARALLEL = 12
FIELDS = ("concurrency", "requests", "low_requests", "low_seconds")


def put(manifest: dict, pace: dict | None) -> None:
    """Record the pace of the countries due (nothing when there is none)."""
    if pace:
        manifest[KEY] = {country: dict(entry) for country, entry in pace.items()}


def validate(data: dict) -> None:
    """Refuse a `country_pace` key that is not a pace of due countries at or under the
    manifest's concurrency."""
    pace = data.get(KEY)
    if pace is None:
        return
    due = data.get("due_countries")
    if data["schema_version"] != 3 or not due or not isinstance(pace, dict) or not pace:
        raise ValueError("country_pace needs due_countries, ephemeral only")
    countries = {code for codes in due.values() for code in codes}
    ceiling = data["settings"]["concurrency"]
    for country, entry in pace.items():
        if country not in countries or not isinstance(entry, dict) or "concurrency" not in entry:
            raise ValueError("country_pace must give a due country its concurrency")
        if set(entry) - set(FIELDS):
            raise ValueError("country_pace allows only concurrency, requests and low_*")
        for key, value in entry.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"country_pace {key} must be a positive integer")
        if entry["concurrency"] > ceiling:
            raise ValueError("country_pace concurrency cannot exceed the manifest's")


def of(manifest: dict, country: str) -> dict:
    """The country's pace: its requests at once and request limit (none without an entry)."""
    return dict(manifest.get(KEY, {}).get(country, {}))


def low(manifest: dict, country: str) -> bool:
    """Whether the country is a low-availability one (its pace has a low cap)."""
    return _is_low(of(manifest, country))


def low_width(manifest: dict, concurrency: int) -> int:
    """How many low-availability countries a round runs at once: at most LOW_PARALLEL, and their
    start paces together within the round's `concurrency`. Each grows to at most
    concurrency // width (RunConfig.grow_ceiling), so together they never exceed it."""
    paces = [entry["concurrency"] for entry in manifest.get(KEY, {}).values() if _is_low(entry)]
    return max(1, min(LOW_PARALLEL, concurrency // max(paces, default=1)))


def _is_low(entry: dict) -> bool:
    return not {"low_requests", "low_seconds"}.isdisjoint(entry)

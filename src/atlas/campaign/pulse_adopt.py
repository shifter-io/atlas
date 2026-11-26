"""PULSE-TEARDOWN-1: the droplets of pulse creates whose outcome is unknown, taken by proof of
their own create, never by a name alone.

A create whose answer was lost (`uncertain`) may still have made its droplets, and DigitalOcean's
tag listing may show them only seconds later: on 2026-11-23 at 19:57 two regions' creates lost
their answers, the one listing right after showed nothing, and 10 droplets ran with no record
until the operator removed them. A stopped process leaves its create `requested` the same way.
Pulse names carry no hour, so a droplet of an earlier hour may hold the same name: a droplet is
this record's only when it is under the pulse tag with the record's exact name, was created no
earlier than the record's request (scope.created_after_request), no ledger records its id
(`exclude`), and it is the only one. The pulse fleet reads again ADOPT_DELAYS apart right after
the create (an adopted node goes on to the project and arming as if the create had answered);
the hour's removal and the next hour's leftover sweep adopt again (pulse_removal).
"""

from __future__ import annotations

from collections.abc import Collection

from . import scope
from .contracts import PULSE_TAG, Droplet, DropletRecord

ADOPT_DELAYS = (5.0, 10.0, 15.0, 30.0)  # the reads after a lost create's answer: about 60 s


def unsure(record: DropletRecord) -> bool:
    """A record whose create may have made a droplet it does not know: left `requested` by a
    stopped process, or `create_failed` by a lost answer (`uncertain`)."""
    lost = record.status == "create_failed" and record.error_class == "uncertain"
    return not record.droplet_id and (record.status == "requested" or lost)


async def adopt(
    cloud, records: dict[str, DropletRecord], exclude: Collection[str] = frozenset()
) -> dict[str, Droplet]:
    """For each record (by name), its own create's droplet, when exactly one is proven; only
    these become this client's creates (`created_ids`)."""

    def ours(droplet: Droplet) -> bool:
        record = records.get(droplet.name)
        return (
            record is not None
            and droplet.id not in exclude
            and scope.created_after_request(droplet, record)
        )

    found: dict[str, list[Droplet]] = {}
    for droplet in await cloud.adopt_uncertain(sorted(records), PULSE_TAG, keep=ours):
        found.setdefault(droplet.name, []).append(droplet)
    return {name: droplets[0] for name, droplets in found.items() if len(droplets) == 1}


async def adopt_waiting(
    cloud, records: dict[str, DropletRecord], clock, exclude: Collection[str] = frozenset()
) -> dict[str, Droplet]:
    """`adopt`, read again ADOPT_DELAYS apart until every record has its droplet; those found."""
    found: dict[str, Droplet] = {}
    for delay in ADOPT_DELAYS:
        await clock.sleep(delay)
        waiting = {n: r for n, r in records.items() if n not in found}
        found |= await adopt(cloud, waiting, exclude)
        if len(found) == len(records):
            break
    return found

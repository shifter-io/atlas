"""LAUNCHER-SIZE-FALLBACK-1: a region's ordered worker sizes. When DigitalOcean answers that a
size is not available in the region, the launcher creates with the next size of the list, the
worker record holds the size that ran and one high alert names the region and that size.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from campaign_fakes import CID, FakeDigitalOcean, Fault
from campaign_hour_cases import (
    NETWORKS,
    PRE_CREATE,
    REGIONS,
    SCHEDULED,
    FakeClock,
    launched,
    make_spec,
)

from atlas.campaign import launch
from atlas.campaign.contracts import CampaignSpec, DropletRecord, HourRecord
from atlas.campaign.do_api import ApiError
from atlas.campaign.sizes import size_list, size_unavailable

UNAVAILABLE = {"id": "unprocessable_entity", "message": "Size is not available in this region."}
FIRST, OTHER = REGIONS[:2]
FALLBACK = {FIRST: ["c-8", "c-8-intel"], OTHER: "c-8"}


class SizeRefusingCloud(FakeDigitalOcean):
    """Answers 422 'Size is not available in this region' for the (region, size) pairs in
    `refused`, after `accepted_first` creates have passed; records each create's size."""

    def __init__(self, refused, accepted_first=0):
        super().__init__()
        self.refused, self.accepted_first = set(refused), accepted_first
        self.created_sizes: dict[str, str] = {}
        self.tried: list[tuple[str, str, int]] = []

    async def _create(self, request):
        body = await request.json()
        self.tried.append((body["region"], body["size"], len(body["names"])))
        if (body["region"], body["size"]) in self.refused:
            if self.accepted_first <= 0:
                return web.json_response(UNAVAILABLE, status=422)
            self.accepted_first -= 1
        self.created_sizes.update(dict.fromkeys(body["names"], body["size"]))
        return await super()._create(request)


def fallbacks(record):
    return [a for a in record.alerts if a["kind"] == "size_fallback"]


@pytest.mark.parametrize(
    ("value", "sizes"),
    [
        ("c-8", ("c-8",)),
        (["c-8"], ("c-8",)),
        (["c-8", "c-8-intel"], ("c-8", "c-8-intel")),
        (["c-8", "c2-8", "c-16"], ("c-8", "c2-8", "c-16")),
        (["s-2vcpu-4gb", "s-2vcpu-4gb-amd"], ("s-2vcpu-4gb", "s-2vcpu-4gb-amd")),
        (["c-8", "s-8vcpu-16gb"], ()),  # a dedicated-CPU cell never falls back to shared CPU
        (["s-2vcpu-4gb", "c-2"], ()),
        (["c-8", "c-4"], ()),  # fewer vCPUs
        (["c-8", "c-8"], ()),
        ([], ()),
        (["c-8", 8], ()),
        (["c-8", "m-8vcpu-64gb"], ()),
        ("c-8,c-8-intel", ()),
        (("c-8",), ()),
        (None, ()),
    ],
)
def test_a_region_s_sizes_are_one_slug_or_an_ordered_list_of_its_cpu_class(value, sizes):
    assert size_list(value) == sizes


def test_a_spec_holds_a_size_list_and_a_single_size_serialises_as_before():
    spec = make_spec(size_by_region=FALLBACK)
    assert spec.sizes(FIRST) == ("c-8", "c-8-intel") and spec.sizes(OTHER) == ("c-8",)
    assert CampaignSpec.from_json(spec.to_json()) == spec
    single = make_spec()
    assert json.loads(single.to_json())["size_by_region"] == dict.fromkeys(REGIONS[:2], "c-2")
    assert CampaignSpec.from_json(single.to_json()).sizes(FIRST) == ("c-2",)
    with pytest.raises(ValueError, match="size_by_region"):
        make_spec(size_by_region={FIRST: ["c-8", "s-8vcpu-16gb"], OTHER: "c-8"})


@pytest.mark.parametrize(
    ("status", "detail", "unavailable"),
    [
        (422, UNAVAILABLE["message"], True),
        (422, "The size is not available in the selected region.", True),
        (422, "You specified an invalid image for Droplet creation.", False),
        (500, UNAVAILABLE["message"], False),
    ],
)
def test_only_a_size_not_available_answer_is_a_size_refusal(status, detail, unavailable):
    error = ApiError("POST", "/v2/droplets", status)
    error.detail = detail
    assert size_unavailable(error) is unavailable


async def test_a_refused_size_falls_back_to_the_next_with_one_alert(tmp_path):
    """The first size refused in one region: its workers are created with the next size at
    once (no create retry), the other region keeps its size, one alert names region and size."""
    fake = SizeRefusingCloud({(FIRST, "c-8")})
    spec = make_spec(size_by_region=FALLBACK)
    record, *_ = await launched(tmp_path, fake, spec)
    assert {d.status for d in record.droplets} == {"armed"}
    assert {d.attempts for d in record.droplets} == {1}
    assert {(d.region, d.size) for d in record.droplets} == {
        (FIRST, "c-8-intel"),
        (OTHER, "c-8"),
    }
    assert {d.name: d.size for d in record.droplets} == fake.created_sizes
    assert sorted(fake.tried) == sorted(
        [(FIRST, "c-8", 2), (FIRST, "c-8-intel", 2), (OTHER, "c-8", 2)]
    )
    (alert,) = record.alerts
    assert alert["level"] == "high" and alert["kind"] == "size_fallback"
    assert alert["name"] == f"{FIRST}/c-8-intel" and alert["refused"] == ["c-8"]


async def test_a_batch_refused_part_way_creates_only_the_rest_with_the_next_size(tmp_path):
    """12 workers in one region: the first batch of 10 lands on c-8, the last 2 on c-8-intel."""
    fake = SizeRefusingCloud({(FIRST, "c-8")}, accepted_first=1)
    spec = make_spec(NETWORKS[:12], [FIRST], size_by_region={FIRST: ["c-8", "c-8-intel"]})
    record, *_ = await launched(tmp_path, fake, spec)
    assert {d.status for d in record.droplets} == {"armed"} and len(record.droplets) == 12
    assert fake.tried == [(FIRST, "c-8", 10), (FIRST, "c-8", 2), (FIRST, "c-8-intel", 2)]
    assert sorted(d.size for d in record.droplets) == ["c-8"] * 10 + ["c-8-intel"] * 2
    assert {d.name: d.size for d in record.droplets} == fake.created_sizes
    assert [a["name"] for a in fallbacks(record)] == [f"{FIRST}/c-8-intel"]


async def test_every_size_refused_fails_the_cell_as_before_trying_only_the_last_again(tmp_path):
    fake = SizeRefusingCloud({(FIRST, "c-8"), (FIRST, "c-8-intel")})
    spec = make_spec(size_by_region=FALLBACK)
    record, *_ = await launched(tmp_path, fake, spec)
    first = [d for d in record.droplets if d.region == FIRST]
    assert {d.status for d in first} == {"create_failed"}
    assert len(first) == 2 * (1 + launch.REPLACEMENTS_PER_CELL)
    assert {d.size for d in first} == {"c-8-intel"}
    tried = [size for region, size, _ in fake.tried if region == FIRST]
    assert tried[:2] == ["c-8", "c-8-intel"] and set(tried[2:]) == {"c-8-intel"}
    assert {d.status for d in record.droplets if d.region == OTHER} == {"armed"}
    assert {a["kind"] for a in record.alerts} == {"create_failed", "cell_failed"}


async def test_another_refusal_keeps_the_size_and_retries_as_before(tmp_path):
    fake = SizeRefusingCloud(set())
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    spec = make_spec(size_by_region={r: ["c-8", "c-8-intel"] for r in REGIONS[:2]})
    record, *_ = await launched(tmp_path, fake, spec)
    assert {d.status for d in record.droplets} == {"armed"}
    assert {d.size for d in record.droplets} == {"c-8"} and not fallbacks(record)
    assert {size for _, size, _ in fake.tried} == {"c-8"}


def test_a_new_worker_starts_at_the_size_the_region_fell_back_to(tmp_path):
    """A replacement or a resumed launch never asks again for a size refused this hour."""
    spec = make_spec(size_by_region=FALLBACK)
    record = HourRecord(CID, 0, SCHEDULED)
    work = launch._Launch(spec, record, tmp_path, None, lambda hour: None, FakeClock(PRE_CREATE))
    assert work._size(FIRST) == "c-8"
    record.droplets.append(DropletRecord("net00", FIRST, 0, "a", size="c-2"))  # not in the list
    assert work._size(FIRST) == "c-8"
    record.droplets.append(DropletRecord("net01", FIRST, 0, "b", size="c-8-intel"))
    record.droplets.append(DropletRecord("net02", FIRST, 0, "c", size="c-8"))
    assert work._size(FIRST) == "c-8-intel" and work._size(OTHER) == "c-8"
    assert work._worker("net03", FIRST, 0, 2000).size == "c-8-intel"

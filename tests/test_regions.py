from datetime import time

import pytest

from atlas.regions import (
    REGIONS_FILE,
    load_countries,
    load_regions,
    load_targets,
    parse_regions,
    resolve_region,
)

COUNTS = {
    "syd1": 27,
    "blr1": 41,
    "tor1": 3,
    "sgp1": 24,
    "ams3": 12,
    "fra1": 58,
    "lon1": 31,
    "nyc3": 53,
}


def minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def test_shipped_mapping_covers_every_country_once():
    regions = load_regions()
    assert {r.slug: len(r.countries) for r in regions.regions} == COUNTS
    codes = [code for r in regions.regions for code in r.countries]
    assert len(codes) == len(set(codes)) == len(regions.names) == 249
    # YAML 1.1 would read NO as false; every code stays a string.
    assert regions.region_of("NO") == "ams3" and regions.names["NO"] == "Norway"
    assert regions.region_of("de") == "fra1" and regions.region_of("US") == "nyc3"
    assert len({r.region_id for r in regions.regions}) == 8


def test_host_resolution():
    assert resolve_region("prod-fra1-shifter") == "fra1"
    assert resolve_region("fra1") == "fra1"
    assert resolve_region("NODE-SYD1-02.example") == "syd1"
    assert resolve_region("nyc3", REGIONS_FILE) == "nyc3"
    for host in ("prod-sfo3-shifter", "", "fra1-and-lon1"):
        with pytest.raises(ValueError, match="matches"):
            resolve_region(host)


def test_census_times_are_staggered_within_each_region():
    regions = load_regions()
    times = regions.census_times()
    assert len(times) == 249 and times == load_regions().census_times()
    for region in regions.regions:
        slots = sorted(minutes(times[code]) for code in region.countries)
        assert len(set(slots)) == len(slots)
        gaps = [b - a for a, b in zip(slots, slots[1:], strict=False)]
        gaps.append(slots[0] + 1440 - slots[-1])
        # Evenly spread: no gap is shorter than the even share rounded down.
        assert min(gaps) >= 1440 // len(slots) - 1
        assert all(t.second == 0 and t.microsecond == 0 for t in map(times.get, region.countries))


def mapping(**patterns):
    lines = ["mapping:"]
    for pattern, codes in patterns.items():
        lines.append(f'  "{pattern}": [{", ".join(codes)}]')
    return "\n".join(lines) + "\n"


SLUGS = ["syd1", "blr1", "tor1", "sgp1", "ams3", "fra1", "lon1", "nyc3"]
NAMES = {
    "AU": "Australia",
    "IN": "India",
    "CA": "Canada",
    "SG": "Singapore",
    "NO": "Norway",
    "DE": "Germany",
    "GB": "United Kingdom",
    "US": "United States",
}


def valid():
    return {f".*{slug}.*": [code] for slug, code in zip(SLUGS, NAMES, strict=True)}


def test_small_mapping_parses():
    regions = parse_regions(mapping(**valid()), NAMES)
    assert regions.region_of("NO") == "ams3"


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda m: m[".*fra1.*"].append("GB"), "both"),
        (lambda m: m[".*fra1.*"].append("XX"), "Unknown country"),
        (lambda m: m.update({".*1.*": m.pop(".*fra1.*")}), "exactly one region"),
        (lambda m: m.update({".*sfo3.*": m.pop(".*fra1.*")}), "exactly one region"),
        (lambda m: m.update({"fra1": ["DE"], ".*fra1.*": []}), "list of country codes"),
        (lambda m: m.update({"(fra1": m.pop(".*fra1.*")}), "Invalid region pattern"),
    ],
)
def test_invalid_mappings_are_rejected(change, message):
    patterns = valid()
    change(patterns)
    with pytest.raises(ValueError, match=message):
        parse_regions(mapping(**patterns), NAMES)


def test_two_patterns_for_one_region_and_duplicate_keys_are_rejected():
    with pytest.raises(ValueError, match=r"Countries without a region: \['FR'\]"):
        parse_regions(mapping(**valid()), NAMES | {"FR": "France"})
    patterns = valid()
    patterns.pop(".*fra1.*")
    patterns["fra1.*"] = ["DE"]
    patterns[".*fra1"] = ["GB"]
    patterns.pop(".*lon1.*")
    with pytest.raises(ValueError, match="exactly one pattern"):
        parse_regions(mapping(**patterns), NAMES)
    text = mapping(**valid()) + '  ".*fra1.*": [DE]\n'
    with pytest.raises(ValueError, match="Duplicate key"):
        parse_regions(text, NAMES)
    with pytest.raises(ValueError, match="only a mapping"):
        parse_regions("regions: {}\n", NAMES)


def test_country_names_file(tmp_path):
    assert load_countries()["DE"] == "Germany"
    bad = tmp_path / "countries.json"
    bad.write_text('{"de": "Germany"}')
    with pytest.raises(ValueError, match="alpha-2"):
        load_countries(bad)


def test_targets_seed(tmp_path):
    targets = {row["target_id"]: row for row in load_targets() if row["engine"] == "http"}
    assert set(targets) == {"generic_http", "generic_https"}
    http, https = targets["generic_http"], targets["generic_https"]
    assert http["census"] and http["pulse_enabled"] and http["census_concurrency"] == 1000
    assert not https["census"] and https["pulse_enabled"]
    assert http["pulse_interval_minutes"] == 30 and http["pulse_requests"] == 100
    assert https["url"].startswith("https://")
    base = '[[target]]\nid = "x"\nlabel = "X"\nurl = "http://x/"\n'
    for extra, message in [
        ("", None),
        ("pulse_interval_minutes = 0\n", "> 0"),
        ("census = 1\n", "census must be bool"),
        ("future = 1\n", "Unknown target fields"),
        ('url2 = "x"\n', "Unknown target fields"),
        ('category = "social"\n', None),
        ('category = "games"\n', "category must be one of baseline"),
        ("category = 1\n", "category must be str"),
    ]:
        path = tmp_path / "targets.toml"
        path.write_text(base + extra)
        if message is None:
            assert load_targets(path)[0]["pulse_concurrency"] == 25
        else:
            with pytest.raises(ValueError, match=message):
                load_targets(path)
    path.write_text(base + base)
    with pytest.raises(ValueError, match="duplicate"):
        load_targets(path)
    path.write_text(base.replace('label = "X"\n', ""))
    with pytest.raises(ValueError, match="label must be str"):
        load_targets(path)

"""The methodology checks of calculated/policy.py: every setting validate(), read(), events() and
load() refuse, each with its own message, and merge()'s benchmark-weight trim."""

import copy

import pytest
from calculated_cases import POLICY

from atlas.calculated import policy
from atlas.calculated.policy import events, load, merge, read, validate


@pytest.fixture(scope="module")
def shipped():
    return load(POLICY)[0]


def altered(config, path, value):
    """A copy of config with the setting at path (keys) set to value; None deletes it."""
    result = copy.deepcopy(config)
    *parents, key = path
    target = result
    for name in parents:
        target = target[name]
    if value is None:
        del target[key]
    else:
        target[key] = value
    return result


def test_the_shipped_methodology_is_valid_and_validate_returns_it(shipped):
    assert validate(copy.deepcopy(shipped)) == shipped


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("profiles", "balanced"), None, "balanced profile is required"),
        (("weights", "speed"), None, "Weights must cover known components"),
        (("weights", "speed"), -1, "Weights must cover known components"),
        (("weights", "speed"), True, "Weights must cover known components"),
        (("weights", "speed"), float("inf"), "Weights must cover known components"),
        (("weights",), dict.fromkeys(policy.KEYS, 0), "positive total"),
        (("target_weights",), {"reliability": 1}, "Weights must cover known components"),
        (("profiles", "latency_first", "value"), None, "Weights must cover known components"),
        (("uptime",), {"min_batches": 0}, "Invalid uptime settings"),
        (("uptime",), {"max_batches": 1}, "Invalid uptime settings"),
        (("uptime",), {"min_successes": 1.0}, "Invalid uptime settings"),
        (("slots",), {"min_latency_successes": 0}, "Invalid slot settings"),
        (("payload",), {"country": -1}, "Invalid payload settings"),
        (("payload",), {"planet": 3}, "Invalid payload settings"),
        (("minimum_requests",), 0, "Invalid minimum_requests"),
        (("score_valid_hours",), 1.5, "Invalid score_valid_hours"),
        (("refresh_seconds",), "60", "Invalid refresh_seconds"),
        (("sample_limit",), -5, "Invalid sample_limit"),
        (("minimum_coverage",), 1.5, "Invalid coverage"),
        (("minimum_coverage",), -0.1, "Invalid coverage"),
        (("windows",), [24, 168], "Windows must be 24, 168 and 720"),
        (("required_targets",), [], "distinct and nonempty"),
        (("required_targets",), ["generic_http", "generic_http"], "distinct and nonempty"),
        (("peer",), {"diversity": {}, "extra": 1}, "Unknown peer setting"),
        (("peer", "diversity", "asns"), 0.5, "Diversity weights must sum to one"),
        (("grades", "A+"), None, "Invalid grade labels"),
        (("grades", "A+"), 101, "Invalid grade thresholds"),
        (("grades", "D"), 1, "Invalid grade thresholds"),
        (("grades", "B"), 99, "Invalid grade thresholds"),
        (("latency_target",), 7, "Invalid latency target"),
        (("latency_target",), "", "Invalid latency target"),
        (("hosting_usage_types",), [], "Invalid hosting usage types"),
        (("hosting_usage_types",), "hosting", "Invalid hosting usage types"),
        (("hosting_usage_types",), ["hosting", ""], "Invalid hosting usage types"),
        (("target_groups",), [], "Invalid target groups"),
        (("target_groups",), {"g": []}, "Invalid target groups"),
        (("target_groups",), {"g": ["a", "a"]}, "Invalid target groups"),
        (("target_groups",), {"g": ["a", ""]}, "Invalid target groups"),
        (("target_groups",), {"g": ["h"], "h": ["i"]}, "Invalid target groups"),
        (("target_groups",), {"g": ["generic_https"]}, "cannot be a required target"),
    ],
)
def test_validate_refuses_each_invalid_setting(shipped, path, value, message):
    with pytest.raises(ValueError, match=message):
        validate(altered(shipped, path, value))


def test_a_benchmark_weight_must_exist_for_every_required_target(shipped):
    # Only the benchmark weights know the required targets; a missing one is refused.
    config = altered(shipped, ("benchmark_weights", "generic_https"), None)
    with pytest.raises(ValueError, match="Weights must cover known components"):
        validate(config)


def test_merge_keeps_only_the_benchmark_weights_of_the_overridden_required_targets():
    base = {
        "required_targets": ["a", "b"],
        "benchmark_weights": {"a": 1, "b": 2},
        "weights": {"x": 1, "y": 2},
    }
    merged = merge(base, {"required_targets": ["b", "c"], "weights": {"y": 5}})
    # c has no weight to keep; a is no longer required. Tables merge key by key.
    assert merged["benchmark_weights"] == {"b": 2}
    assert merged["weights"] == {"x": 1, "y": 5}
    # The base is never changed, and an override without required_targets keeps every weight.
    assert base["benchmark_weights"] == {"a": 1, "b": 2}
    assert merge(base, {"weights": {"x": 3}})["benchmark_weights"] == {"a": 1, "b": 2}
    # Without benchmark weights there is nothing to trim.
    assert "benchmark_weights" not in merge({"required_targets": ["a"]}, {"required_targets": []})


@pytest.mark.parametrize("parent", ["", "7", "[1]"])
def test_extends_must_name_a_file(tmp_path, parent):
    path = tmp_path / "methodology.toml"
    value = '""' if parent == "" else parent
    path.write_text(f'extends = {value}\nname = "child"\n')
    with pytest.raises(ValueError, match="extends must name a methodology file"):
        read(path)


def test_read_merges_a_relative_parent_under_the_child(tmp_path):
    (tmp_path / "base.toml").write_text('name = "base"\nsample_limit = 5\n[slots]\na = 1\nb = 2\n')
    (tmp_path / "child.toml").write_text('extends = "base.toml"\nname = "child"\n[slots]\nb = 3\n')
    assert read(tmp_path / "child.toml") == {
        "name": "child",
        "sample_limit": 5,
        "slots": {"a": 1, "b": 3},
    }


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("unknown_threshold = 1", "Unknown event setting"),
        ("min_requests = 2.5", "Invalid event setting min_requests"),
        ("success_drop_pp = -0.1", "Invalid event setting success_drop_pp"),
        ('success_drop_pp = "high"', "Invalid event setting success_drop_pp"),
        ("down_baseline = nan", "Invalid event setting down_baseline"),
        ("latency_spike_ratio = 1.0", "Latency ratios must exceed one"),
        ("pulse_latency_ratio = 0.5", "Latency ratios must exceed one"),
        ("hosting_usage_types = []", "Invalid hosting usage types"),
    ],
)
def test_events_refuses_invalid_thresholds(tmp_path, section, message):
    path = tmp_path / "methodology.toml"
    path.write_text(f"[events]\n{section}\n")
    with pytest.raises(ValueError, match=message):
        events(path)


def test_events_defaults_and_overrides(tmp_path):
    defaults = events()
    assert defaults == policy.EVENTS | {"hosting_usage_types": policy.HOSTING_USAGE_TYPES}
    # A missing file is the defaults too.
    assert events(tmp_path / "absent.toml") == defaults
    path = tmp_path / "methodology.toml"
    # An integer is accepted for a float threshold, never a float for an integer one.
    path.write_text("[events]\nsuccess_drop_pp = 1\nmin_requests = 7\n")
    result = events(path)
    assert result["success_drop_pp"] == 1 and result["min_requests"] == 7
    assert result["baseline_batches"] == policy.EVENTS["baseline_batches"]


def test_load_refuses_an_unknown_override(tmp_path):
    path = tmp_path / "methodology.toml"
    path.write_text(f'extends = "{POLICY.resolve()}"\n[targets.generic_http]\nwindows = [24]\n')
    with pytest.raises(ValueError, match="Unknown methodology override"):
        load(path)


def test_load_validates_each_override_merged_over_the_methodology(tmp_path):
    path = tmp_path / "methodology.toml"
    path.write_text(f'extends = "{POLICY.resolve()}"\n[products."p/q"]\nminimum_requests = 0\n')
    with pytest.raises(ValueError, match="Invalid minimum_requests"):
        load(path)
    path.write_text(f'extends = "{POLICY.resolve()}"\n[products."p/q"]\nminimum_requests = 9\n')
    config, digest = load(path)
    assert config["products"]["p/q"] == {"minimum_requests": 9}
    assert digest != load(POLICY)[1]

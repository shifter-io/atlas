"""configs/staging.toml validation: every section's error names the section and key."""

import re

import pytest

from proxy_index.orchestrator.staging import load_profile
from proxy_index.regions import CONFIGS


@pytest.mark.parametrize(
    ("good", "bad", "message"),
    [
        ('name = "proxy_reviews_staging"', 'name = "Bad-Name"', "[database] name"),
        ('countries = ["RO", "BG"]', 'countries = ["ROU"]', "[scope] countries"),
        ("census = true", 'census = "yes"', "[scheduler] census"),
        ('dir = "reports/staging"', 'dir = ""', "[reports] dir"),
        ("[[nodes]]", "[nodes]", "[[nodes]] must be an array"),
        ('product = "residential"', 'product = "a b"', "needs provider, product and region"),
        ("every_minutes = 4", "every_minutes = 0", "[census] every_minutes"),
    ],
)
def test_staging_profile_rejects_each_invalid_section(tmp_path, good, bad, message):
    text = (CONFIGS / "staging.toml").read_text()
    assert text.count(good) == 1
    path = tmp_path / "staging.toml"
    path.write_text(text.replace(good, bad))
    with pytest.raises(ValueError, match=re.escape(message)):
        load_profile(path)

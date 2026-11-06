"""Answer classes on the command line (split from test_answer_classes.py): --answer-classes
needs a provider config, and with --mmdb builds the run's classes and country enricher."""

import json
from dataclasses import replace
from pathlib import Path

from answer_classes_cases import GEONODE, Countries, ok, scripted

from proxy_index import cli as cli_module
from proxy_index import run
from proxy_index.cli import main

ROOT = Path(__file__).parents[1]


def test_cli_answer_classes_needs_a_provider_config(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:9")
    assert main(["run", "--target", "generic_http", "--answer-classes"]) == 2
    assert capsys.readouterr().err == "Run refused: --answer-classes needs --provider-config.\n"


def test_cli_answer_classes_and_mmdb_build_the_config_and_enricher(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("GEONODE_ACCOUNT", "example")
    monkeypatch.setenv("GEONODE_PASSWORD", "example-password")
    seen = {}

    class Enricher(Countries):
        def __init__(self, path):
            seen["mmdb"] = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            seen["closed"] = True

    async def fake_run(config, sink, options):
        seen.update(classes=config.answer_classes, enricher=options.enricher)
        factory = scripted(lambda n: ok("192.0.2.1"))
        return await run(config, sink, replace(options, transport_factory=factory))

    monkeypatch.setattr("proxy_index.enrichment.LocalEnricher", Enricher)
    monkeypatch.setattr(cli_module, "run", fake_run)
    assert (
        main(
            [
                "run",
                "--target",
                "generic_http",
                "--requests",
                "2",
                "--answer-classes",
                "--provider-config",
                str(ROOT / "configs/geonode.toml"),
                "--param",
                "country=us",
                "--mmdb",
                "db.mmdb",
                "--output",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert seen["classes"] == GEONODE and isinstance(seen["enricher"], Enricher)
    assert seen["mmdb"] == "db.mmdb" and seen["closed"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["other_country"] == 0 and printed["rate_limited"] == {"429": 0, "469": 0}

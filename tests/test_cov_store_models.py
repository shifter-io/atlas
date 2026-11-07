"""Input checks of the proxy model, the regions and targets files, the MMDB loader and the CLI."""

import asyncio
import json
import signal

import pytest

from proxy_index import cli, enrichment
from proxy_index.models import ProxyConfig
from proxy_index.regions import load_regions, load_targets, parse_regions


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"scheme": "https", "host": "gw", "port": 80}, "HTTP or SOCKS5 host"),
        ({"scheme": "http", "host": "", "port": 80}, "HTTP or SOCKS5 host"),
        ({"scheme": "http", "host": "gw", "port": 0}, "between 1 and 65535"),
        ({"scheme": "http", "host": "gw", "port": True}, "between 1 and 65535"),
        ({"scheme": "http", "host": "gw", "port": 80, "username": "u"}, "both proxy username"),
        (
            {"scheme": "http", "host": "gw", "port": 80, "username": "a:b", "password": "p"},
            "cannot contain colons",
        ),
    ],
)
def test_proxy_config_refuses_invalid_fields(fields, message):
    with pytest.raises(ValueError, match=message):
        ProxyConfig(**fields)


def test_socks5_usernames_may_contain_colons():
    proxy = ProxyConfig("socks5", "gw", 1080, "a:b", "p")
    assert proxy.public() == {"proxy_protocol": "socks5"} and "a:b" not in repr(proxy)


@pytest.mark.parametrize(
    "url",
    ["http://gw:8080/path", "http://gw:8080/?q=1", "http://gw:8080/#f", "ftp://gw:21"],
)
def test_proxy_url_refuses_paths_queries_fragments_and_other_schemes(url):
    with pytest.raises(ValueError, match="Invalid proxy URL") as raised:
        ProxyConfig.from_url(url)
    assert raised.value.__cause__ is None


def test_proxy_url_defaults_the_port_per_scheme():
    assert ProxyConfig.from_url("socks5h://gw").port == 1080
    assert ProxyConfig.from_url("http://gw/").port == 8080


def test_region_of_an_unmapped_country_is_a_key_error():
    regions = load_regions()
    assert regions.region_of("de") == regions.region_of("DE")
    with pytest.raises(KeyError):
        regions.region_of("ZZ")


def test_regions_mapping_must_not_be_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        parse_regions("mapping: {}\n", {})


def targets(tmp_path, text):
    path = tmp_path / "targets.toml"
    path.write_text(text)
    return path


def test_targets_file_holds_only_target_tables(tmp_path):
    with pytest.raises(ValueError, match=r"only \[\[target\]\] tables"):
        load_targets(targets(tmp_path, 'title = "x"\n'))


def test_target_urls_must_be_http(tmp_path):
    entry = '[[target]]\nid = "echo"\nlabel = "Echo"\nurl = "{}"\n'
    [row] = load_targets(targets(tmp_path, entry.format("https://example.test/ip")))
    assert row["target_id"] == "echo" and row["timeout_ms"] == 15000
    with pytest.raises(ValueError, match="must be http or https"):
        load_targets(targets(tmp_path, entry.format("ftp://example.test/ip")))


def only_cwd_db_ip(tmp_path, monkeypatch):
    """find_source searches only tmp_path/db-ip: not $DBIP_MMDB, nor the checkout's db-ip."""
    monkeypatch.delenv("DBIP_MMDB", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(enrichment, "CHECKOUT_DB_IP", tmp_path / "db-ip")
    (tmp_path / "db-ip").mkdir()


@pytest.mark.parametrize("files", [[], ["a.mmdb", "b.mmdb"]])
def test_find_source_needs_exactly_one_mmdb(tmp_path, monkeypatch, files):
    only_cwd_db_ip(tmp_path, monkeypatch)
    for name in files:
        (tmp_path / "db-ip" / name).write_bytes(b"")
    with pytest.raises(ValueError, match="exactly one local MMDB file"):
        enrichment.find_source()


def test_find_source_takes_the_only_mmdb(tmp_path, monkeypatch):
    only_cwd_db_ip(tmp_path, monkeypatch)
    (tmp_path / "db-ip" / "release.mmdb").write_bytes(b"")
    assert enrichment.find_source() == (tmp_path / "db-ip" / "release.mmdb").resolve()


def test_open_closes_the_reader_when_reading_it_fails(tmp_path, monkeypatch):
    path = tmp_path / "x.mmdb"
    path.write_bytes(b"")

    class Reader:
        closed = False

        def metadata(self):
            raise RuntimeError("corrupt")

        def close(self):
            Reader.closed = True

    monkeypatch.setattr(enrichment.maxminddb, "open_database", lambda name: Reader())
    with pytest.raises(RuntimeError, match="corrupt"):
        enrichment.LocalEnricher(path)
    assert Reader.closed


def test_enrich_run_needs_a_pool_result(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"schema_version": "1"}))
    with pytest.raises(ValueError, match="version 1 Generic HTTP pool result"):
        enrichment.enrich_run(tmp_path)


def test_stop_on_signals_skips_loops_without_signal_handlers():
    class Loop:
        def __init__(self, supported):
            self.supported, self.handlers = supported, {}

        def add_signal_handler(self, sig, callback):
            if not self.supported:
                raise NotImplementedError
            self.handlers[sig] = callback

    stop = asyncio.Event()
    assert cli._stop_on_signals(Loop(False), stop) == []
    loop = Loop(True)
    assert cli._stop_on_signals(loop, stop) == [signal.SIGINT, signal.SIGTERM]
    loop.handlers[signal.SIGTERM]()
    assert stop.is_set()

"""scripts/server-security-check.sh: verdicts, exit code and output, with fake network tools.

Every tool the script calls (curl, nmap, openssl, ssh, psql, getent, timeout) is a stub that
answers like a hardened server (SCENARIO=good) or a weak one (SCENARIO=bad); nothing leaves
the machine.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server-security-check.sh"
HOST, NAME, DB = "192.0.2.10", "api.example.test", "198.51.100.7"

STUBS = {
    "curl": r"""
import os, re, sys
good = os.environ["SCENARIO"] == "good"
a = sys.argv[1:]
w = a[a.index("-w") + 1] if "-w" in a else ""
url = a[-1]
out = a[a.index("-o") + 1] if "-o" in a else None
if out and out != "/dev/null":
    body = "Client sent an HTTP request to an HTTPS server.\n" if good else "Caddy v2"
    open(out, "w").write(body)
if re.match(r"http://192\.0\.2\.10:[89]080/", url):  # the CDN's plain HTTP origin, no header
    if good:
        open(a[a.index("-D") + 1], "w").write("HTTP/1.1 200 OK\r\n\r\n")
    print("200" if good else "308", end="")  # a weak origin redirects to HTTPS
elif url.startswith("https://192.0.2.10"):  # the origin, without the secret
    print("403 0" if good else "200 120", end="")
elif url.startswith("http://"):
    print("400" if good else "200", end="")
elif "-X" in a:
    print("405" if good else "200", end="")
elif w == "%{redirect_url}":
    print("https://files.example.test/dataset.bin", end="")
elif "-D" in a:
    hit = good or "zz=" not in url
    head = "HTTP/2 200\r\nCDN-Cache: %s\r\n" % ("HIT" if hit else "MISS")
    head += "Strict-Transport-Security: max-age=31536000\r\n" if good else ""
    head += "X-Content-Type-Options: nosniff\r\nAccess-Control-Allow-Origin: *\r\n\r\n"
    if a[a.index("-D") + 1] == "-":
        print(head, end="")
    else:
        open(a[a.index("-D") + 1], "w").write(head)
        print("200 5000", end="")
elif url.endswith("/healthz"):
    print('{"status":"ok","version":"v","builtAt":"t","revision":null,"reason":null'
          + ("}" if good else ',"lastError":"x"}'))
""",
    "nmap": r"""
import os, sys
good = os.environ["SCENARIO"] == "good"
a = " ".join(sys.argv[1:])
if "ssl-enum-ciphers" in a:
    print("|   TLSv1.2:\n|     ciphers:\n"
          "|       TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256 (secp256r1) - A\n"
          "|     compressors:\n|       NULL\n"
          "|_  least strength: " + ("A" if good else "C"))
elif "-sU" in a:
    bad = "" if good else ", 161/open/udp//snmp///"
    print("Host: x ()\tPorts: 53/open|filtered/udp//domain///" + bad)
else:
    bad = "" if good else ", 8090/open/tcp//"
    origins = ", ".join(f"{p}/open/tcp//" for p in (8080, 8445, 9080, 9445))
    print("Host: x ()\tPorts: " + origins + bad)
""",
    "openssl": r"""
import os, sys
a = sys.argv[1:]
if a[0] == "rand":
    print("ab" * int(a[2]))
elif a[0] == "s_client":
    print("PEM")
elif "-subject" in a:
    print("subject=\nX509v3 Subject Alternative Name: critical\n    DNS:atlas-origin")
elif "-startdate" in a:
    print("notBefore=Jan  1 00:00:00 2020 GMT")
elif "-enddate" in a:
    print("notAfter=Jan  1 00:00:00 2099 GMT")
elif a == ["x509"]:
    print(sys.stdin.read(), end="")
""",
    "ssh": r"""
import os, sys
methods = "publickey" if os.environ["SCENARIO"] == "good" else "publickey,password"
sys.stderr.write("u@192.0.2.10: Permission denied (%s).\n" % methods)
sys.exit(255)
""",
    "psql": r"""
import os, sys
sys.stderr.write('connection to server at "198.51.100.7", port 5432 failed: '
                 + ("no pg_hba.conf entry for host" if os.environ["SCENARIO"] == "good"
                    else "password authentication failed for user") + "\n")
sys.exit(2)
""",
    "getent": "import os, sys\nsys.exit(int(os.environ.get('RESOLVES', '0')))\n",
    # timeout 5 bash -c '<open a TCP connection>' <host> <port>: open when the port is listed.
    "timeout": "import os, sys\nsys.exit(0 if sys.argv[-1] in os.environ['OPEN'].split() else 1)\n",
}


def check(
    tmp_path: Path,
    *args: str,
    scenario: str = "good",
    open_ports: str = "2222",
    stubs: dict[str, str] = STUBS,
    **env,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!{sys.executable}\n{body}")
        (bin_dir / name).chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SCENARIO": scenario,
            "OPEN": open_ports,
            **env,
        },
    )


def verdicts(out: str) -> dict[str, str]:
    lines = out.splitlines()
    assert all(re.match(r"^[A-Z][0-9.]* (pass|fail|skip) \S", line) for line in lines), out
    return {line.split()[0]: line.split()[1] for line in lines}


FULL = ("production", "--host", HOST, "--name", NAME, "--ssh", "u:2222", "--db", DB)


def test_hardened_server_passes_every_check(tmp_path):
    out = check(tmp_path, *FULL)
    assert out.returncode == 0, out.stdout + out.stderr
    got = verdicts(out.stdout)
    assert got.pop("A0") == "skip"
    want = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9.1", "A11"]
    want += ["D1", "D2", "D3", "D4", "D5"]
    assert got == dict.fromkeys(want, "pass")
    assert "A11 pass plain HTTP 8080 without a header: 200" in out.stdout


def test_weak_server_fails_and_exits_1(tmp_path):
    out = check(tmp_path, *FULL, scenario="bad", open_ports="2222 5432 8444")
    assert out.returncode == 1
    got = verdicts(out.stdout)
    for check_id in ("A1", "A2", "A3", "A4", "A5", "A6", "A8", "A9.1", "A10.1", "A11"):
        assert got[check_id] == "fail", check_id
    for check_id in ("D2", "D3", "D5"):
        assert got[check_id] == "fail", check_id
    assert "A11 fail plain HTTP 8080 without a header: 308" in out.stdout
    assert "8090" in out.stdout and "publickey,password" in out.stdout
    assert "password-asked" in out.stdout


@pytest.mark.parametrize("scenario", ["good", "bad"])
def test_output_never_names_an_address_or_the_probe_secret(tmp_path, scenario):
    out = check(tmp_path, *FULL, scenario=scenario, open_ports="2222 5432")
    text = out.stdout + out.stderr
    for value in (HOST, NAME, DB, "abab" * 8):
        assert value not in text


def test_plain_http_port_per_environment(tmp_path):
    out = check(tmp_path, "staging", "--host", HOST)
    assert "A11 pass plain HTTP 9080 without a header: 200" in out.stdout


def test_hsts_on_the_plain_http_port_fails(tmp_path):
    ok = '"HTTP/1.1 200 OK\\r\\n'
    curl = STUBS["curl"].replace(ok, ok + 'Strict-Transport-Security: max-age=1\\r\\n')
    assert curl != STUBS["curl"]
    out = check(tmp_path, "production", "--host", HOST, stubs={**STUBS, "curl": curl})
    assert "A11 fail plain HTTP 8080: 200 with Strict-Transport-Security" in out.stdout


def test_part_d_waits_for_the_name(tmp_path):
    out = check(tmp_path, "staging", "--name", NAME, RESOLVES="2")
    assert out.returncode == 0
    assert verdicts(out.stdout) == {"D": "skip"}


def test_database_port_closed_skips_the_password_try(tmp_path):
    out = check(tmp_path, "staging", "--host", HOST, "--db", DB, open_ports="")
    got = verdicts(out.stdout)
    assert got["A9.1"] == "pass" and "A10.1" not in got
    assert got["A3"] == "skip"


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("prod", "--host", HOST),
        ("staging",),
        ("staging", "--host"),
        ("staging", "--ssh", "u"),
        ("staging", "--ssh", ":22"),
        ("staging", "--bogus", "x"),
    ],
)
def test_bad_arguments(tmp_path, args):
    out = check(tmp_path, *args)
    assert out.returncode == 2
    assert "usage: server-security-check.sh" in out.stderr

"""Tests for check-endpoint.py. Run with:  python3 -m pytest -q tests

Needs pycurl (and openssl on PATH for the TLS test). Every network test talks
to a throwaway server on 127.0.0.1, so nothing leaves the machine.
"""

import importlib.util
import io
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pycurl
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "check-endpoint.py"
_spec = importlib.util.spec_from_file_location("check_endpoint", SCRIPT)
ce = importlib.util.module_from_spec(_spec)
sys.modules["check_endpoint"] = ce
_spec.loader.exec_module(ce)

COLS = [f[1] for f in ce.FIELDS]

# ----------------------------------------------------------- local servers


def _listener(handler):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def loop():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1]


def _ok(conn):
    try:
        conn.recv(65536)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
    finally:
        conn.close()


def _silent(conn):
    time.sleep(30)


def _close_now(conn):
    conn.close()


def _stall_body(conn):
    conn.recv(65536)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nhello")
    time.sleep(30)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def servers(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))

    def tls(conn):
        try:
            _ok(ctx.wrap_socket(conn, server_side=True))
        except Exception:
            pass

    return {
        "ok": f"http://127.0.0.1:{_listener(_ok)}/",
        "refused": f"http://127.0.0.1:{_free_port()}/",
        "silent": f"http://127.0.0.1:{_listener(_silent)}/",
        "closed": f"http://127.0.0.1:{_listener(_close_now)}/",
        "stall": f"http://127.0.0.1:{_listener(_stall_body)}/",
        "tls_bad_cert": f"https://127.0.0.1:{_listener(tls)}/",
    }


def run_cli(*args, env=None):
    e = dict(os.environ)
    e.pop("NO_COLOR", None)
    e.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-t", "2", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=e,
    )


def row_cells(stdout):
    """{column: cell text} for the first data row, split at header offsets."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    header = next(ln for ln in lines if ln.startswith("#"))
    row = next(ln for ln in lines if ln.startswith("1 "))
    starts = [header.index(c) for c in COLS]
    cells = {}
    for i, c in enumerate(COLS):
        end = starts[i + 1] if i + 1 < len(COLS) else len(row)
        cells[c] = row[starts[i] : end]
    return cells


def filled(cells):
    return {
        k: v.strip()
        for k, v in cells.items()
        if k not in ("#", "IP_ADDRESS") and v.strip() not in ("", "-", "n/a")
    }


# --------------------------------------------- failure placement (the bug)

EXPECTED = {
    # scenario: (column the marker must be in, marker, columns that must be empty)
    "refused": ("TCP_CONNECT", "<CONN-FAIL>", ["PRE-TRANSFER", "1ST_BYTE"]),
    "tls_bad_cert": ("TLS_HANDSHAKE", "<TLS-FAIL>", ["PRE-TRANSFER", "1ST_BYTE"]),
    "closed": ("1ST_BYTE", "<RECV-FAIL>", ["BODY_DL"]),
    "silent": ("1ST_BYTE", "<TO>", ["BODY_DL"]),
    "stall": ("BODY_DL", "<TO>", []),
}


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_failure_marker_lands_on_failed_phase(servers, scenario):
    column, marker, must_be_empty = EXPECTED[scenario]
    r = run_cli(servers[scenario])
    cells = row_cells(r.stdout)
    assert cells[column].strip() == marker, filled(cells)
    for col in must_be_empty:
        assert cells[col].strip() in ("", "-", "n/a"), (col, filled(cells))
    # every cell keeps at least one trailing space: nothing overflows
    for col, text in cells.items():
        if col != COLS[-1]:
            assert text.endswith(" "), (col, text)


def test_success_row(servers):
    cells = filled(row_cells(run_cli(servers["ok"]).stdout))
    assert cells["HTTP_CODE"] == "200"
    assert "TLS_HANDSHAKE" not in cells  # plain http: n/a


def test_marker_columns_fit_every_marker():
    longest = max(len(m) for m in ce.ERROR_MARKERS.values())
    for key, _label, width in ce.FIELDS:
        if key in ce.LIVE_FIELD_KEYS or key in ("redirect", "download"):
            assert width > longest, key


def test_phase_deltas_ignore_timers_set_by_a_failed_transfer():
    c = pycurl.Curl()
    c.setopt(c.URL, f"http://127.0.0.1:{_free_port()}/")
    c.setopt(c.WRITEDATA, io.BytesIO())
    with pytest.raises(pycurl.error):
        c.perform()
    phases = ce.compute_phase_deltas(c)
    assert phases["tcp"] is None
    assert phases["pretransfer"] is None
    assert phases["ttfb"] is None
    assert phases["download"] is None


# ------------------------------------------------------------- exit codes


def test_exit_0_when_all_good(servers):
    assert run_cli(servers["ok"]).returncode == 0


def test_exit_3_when_a_request_fails_without_assertions(servers):
    assert run_cli(servers["refused"]).returncode == 3


def test_exit_1_when_an_assertion_breaches(servers):
    assert run_cli(servers["ok"], "--assert-status", "404").returncode == 1
    assert run_cli(servers["refused"], "--assert-status", "200").returncode == 1


# ------------------------------------------------------------------ color


def test_no_escape_codes_when_piped(servers):
    for s in ("ok", "refused"):
        r = run_cli(servers[s], "-c", "2", "--stats")
        assert "\x1b" not in r.stdout, s


def _tty_run(*args, env=None):
    """Run under a pseudo-terminal so stdout.isatty() is true."""
    import pty

    master, slave = pty.openpty()
    e = dict(os.environ)
    e.pop("NO_COLOR", None)
    e.update(env or {})
    p = subprocess.Popen(
        [sys.executable, str(SCRIPT), "-t", "2", *args],
        stdout=slave,
        stderr=slave,
        env=e,
    )
    os.close(slave)
    out = b""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    p.wait()
    os.close(master)
    return out.decode("utf-8", "replace")


def test_color_on_a_terminal_and_off_with_flag_or_env(servers):
    assert "\x1b[" in _tty_run(servers["ok"])
    assert "\x1b[" not in _tty_run(servers["ok"], "--no-color")
    assert "\x1b[" not in _tty_run(servers["ok"], env={"NO_COLOR": "1"})


# ------------------------------------------------------------ prometheus


def _res(failed=False, total=0.01, marker=None):
    return {
        "failed": failed,
        "marker": marker,
        "code": None if failed else 200,
        "bytes": None if failed else 2,
        "insecure": False,
        "phases": {"total": None if failed else total, "dns": 0.001},
    }


def _metrics(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name.split("{")[0]] = (name, value)
    return out


def test_up_reflects_the_most_recent_probe():
    results = [_res(), _res(), _res(failed=True, marker="<CONN-FAIL>")]
    m = _metrics(ce.build_prometheus_text("http://x/", results, None))
    assert m["check_endpoint_up"][1] == "0"
    assert 'reason="conn-fail"' in m["check_endpoint_last_error"][0]
    assert m["check_endpoint_scrape_probe_failures"][1] == "1"


def test_up_is_1_when_last_probe_succeeded_after_a_failure():
    results = [_res(failed=True, marker="<TO>"), _res()]
    m = _metrics(ce.build_prometheus_text("http://x/", results, None))
    assert m["check_endpoint_up"][1] == "1"
    assert "check_endpoint_last_error" not in m


def test_total_suffix_only_on_counters():
    text = ce.build_prometheus_text(
        "http://x/", [_res()], None, counters={"requests": 5, "failures": 1}
    )
    types = dict(
        line.split()[2:4] for line in text.splitlines() if line.startswith("# TYPE")
    )
    for name, mtype in types.items():
        assert name.endswith("_total") == (mtype == "counter"), name
    assert types["check_endpoint_requests_total"] == "counter"


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, []),
        (3, ["p50"]),
        (12, ["p50", "p90"]),
        (25, ["p50", "p90", "p95"]),
        (100, ["p50", "p90", "p95", "p99"]),
    ],
)
def test_percentiles_need_enough_samples(n, expected):
    results = [_res(total=0.001 * (i + 1)) for i in range(n)]
    m = _metrics(ce.build_prometheus_text("http://x/", results, None))
    got = [
        k.rsplit("_", 1)[1] for k in m if k.startswith("check_endpoint_total_seconds_p")
    ]
    assert got == expected


def test_exporter_counters_accumulate_across_scrapes(servers):
    import urllib.request

    port = _free_port()
    p = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            servers["refused"],
            "-t",
            "2",
            "--prometheus",
            "--prometheus-port",
            str(port),
            "--prometheus-bind",
            "127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/metrics"
        urllib.request.urlopen(url, timeout=30).read()
        text = urllib.request.urlopen(url, timeout=30).read().decode()
    finally:
        p.terminate()
        p.wait()
    m = _metrics(text)
    assert m["check_endpoint_requests_total"][1] == "2"
    assert m["check_endpoint_failures_total"][1] == "2"
    assert m["check_endpoint_up"][1] == "0"


# --------------------------------------------------------------- contrib


def test_contrib_copies_match_root_script():
    root = SCRIPT.read_bytes()
    for sub in ("check-endpoint-cli", "check-endpoint-exporter"):
        assert (
            SCRIPT.parent / "contrib" / sub / "check-endpoint.py"
        ).read_bytes() == root

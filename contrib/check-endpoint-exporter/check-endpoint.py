#!/usr/bin/env python3
"""
check-endpoint.py: live per-phase HTTP timing probe, curl-style.

Usage:
    ./check-endpoint.py -c 10 https://example.com

Requires pycurl (libcurl Python binding). Recommended install via pyenv so it
stays isolated from your system Python:

    pyenv virtualenv 3.12.0 check-endpoint-env
    pyenv activate check-endpoint-env
    pip install pycurl

If you don't use pyenv and want to install into the system Python directly:

    pip install pycurl --break-system-packages

macOS users may need libcurl headers first:  brew install curl
Linux users may need:                        apt install libcurl4-openssl-dev
"""

import argparse
import math
import re
import socket
import statistics
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# Cap how much response body we buffer for --expect-body / --expect-regex
# checks, so a huge download can't exhaust memory. We only need enough to
# match against; anything past this is counted but not stored.
BODY_CAPTURE_LIMIT = 5 * 1024 * 1024  # 5 MiB

try:
    import pycurl
except ImportError:
    sys.stderr.write(
        "error: pycurl is not installed.\n\n"
        "recommended (pyenv virtualenv):\n"
        "  pyenv virtualenv 3.12.0 check-endpoint-env && pyenv activate check-endpoint-env\n"
        "  pip install pycurl\n\n"
        "or, to install into the system Python directly:\n"
        "  pip install pycurl --break-system-packages\n\n"
        "macOS may need:  brew install curl\n"
        "Linux may need:  apt install libcurl4-openssl-dev\n"
    )
    sys.exit(1)


APP_VERSION = "1.0.0"
DEFAULT_USER_AGENT = f"check-endpoint/{APP_VERSION}"

# CURL_VERSION_HTTP2 feature bit - set when libcurl was built with nghttp2.
# If this is False, --http2 will be silently ignored by libcurl (it falls
# back to HTTP/1.1 without an error). Use this flag to warn the user early.
_HAS_HTTP2 = bool(pycurl.version_info()[4] & (1 << 16))

USER_AGENTS = {
    "chrome": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "firefox": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    "edge": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    "safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
    ),
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
}

# ── Catppuccin Mocha theme ────────────────────────────────────────────────────
# Colors are only emitted when stdout is a real terminal.
# Pipe output to a file or another command and you get plain text.

USE_COLOR = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"


def _fg(h: str) -> str:
    """24-bit foreground color from a hex string."""
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[38;2;{r};{g};{b}m"


# Mocha palette references
_TEXT = _fg("#cdd6f4")  # primary text
_SUBTEXT0 = _fg("#a6adc8")  # secondary text (even rows)
_OVERLAY0 = _fg("#6c7086")  # dim (row numbers, sub-ms times)
_BLUE = _fg("#89b4fa")  # header labels
_LAVENDER = _fg("#b4befe")  # IP addresses
_SKY = _fg("#89dceb")  # fast ms (< 10 ms)
_TEAL = _fg("#94e2d5")  # moderate ms (10-99 ms)
_YELLOW = _fg("#f9e2af")  # slow ms (≥ 100 ms)
_PEACH = _fg("#fab387")  # seconds / redirect
_RED = _fg("#f38ba8")  # minutes / errors / 5xx
_GREEN = _fg("#a6e3a1")  # 2xx / bytes
_MAUVE = _fg("#cba6f7")  # 3xx
_MAROON = _fg("#eba0ac")  # 4xx

# Compiled color constants
C_HEADER = BOLD + _BLUE  # header row - bold blue
C_ROW_ODD = _TEXT  # odd data rows
C_ROW_EVEN = _SUBTEXT0  # even data rows - slightly dimmer
C_LINENUM = _OVERLAY0  # row counter (#)
C_IP = _LAVENDER  # IP address
C_ERROR = BOLD + _RED  # <ERROR-MARKER> values
C_REDIR = _PEACH  # redirect count×time
C_BYTES = _GREEN  # response body size
C_H2 = _TEAL  # HTTP/2 (teal - preferred)
C_H1 = _OVERLAY0  # HTTP/1.1 (dim - older protocol)


def _col(s: str) -> str:
    """Return s unchanged if color is disabled."""
    return s if USE_COLOR else ""


def _row_color(run_num: int) -> str:
    return _col(C_ROW_ODD if run_num % 2 == 1 else C_ROW_EVEN)


# ── empty-cell conventions ────────────────────────────────────────────────────
#
# A cell can come back empty for two different reasons, and we distinguish
# them visually (both rendered dim/grey, same shade as the row-number column):
#
#   "n/a"   the phase is structurally not applicable to this request, e.g.
#           TLS HANDSHAKE on a plain http:// URL (there is no TLS phase at
#           all), or REDIRECT when no redirects were followed.
#   "-"     the field is empty for any other reason (truncated by a failure
#           mid-transfer, a value libcurl never reported, etc).
#
NA_TEXT = "n/a"
DASH_TEXT = "-"
NA_FIELDS = {"tls", "redirect", "avggap", "maxgap"}


def _empty_cell_text(key: str) -> str:
    return NA_TEXT if key in NA_FIELDS else DASH_TEXT


def write_empty_cell(key: str, width: int) -> None:
    """Write the grey n/a-or-dash placeholder for an empty field."""
    write_cell(_empty_cell_text(key), width, color=_col(C_LINENUM))


# ── timing colorizer ──────────────────────────────────────────────────────────
#
# Each timing value gets two colors: one for the numeric part, one for the
# unit suffix.  Larger/slower units use warmer, bolder colors so at a glance
# you immediately see which phases are slow.
#
#   <1ms   dim overlay  (sub-millisecond - not worth highlighting)
#   Nms    sky / teal / yellow  (fast → moderate → slow within ms range)
#   N.NNs  bold peach  (seconds - definitely slow)
#   NmNs   bold red    (minutes - very slow)
#
def _colorize_time(value: str) -> str:
    """Return an ANSI-colored timing string, or the original if color is off."""
    if not USE_COLOR or not value:
        return value

    # Error markers travel through here too sometimes
    if value.startswith("<"):
        return C_ERROR + value + RESET

    if value == "<1ms":
        return _col(_OVERLAY0) + value + RESET

    # Minutes: "1m30s" (guard against "ms" values, which also contain "m")
    if "m" in value and value[0].isdigit() and not value.endswith("ms"):
        return _col(BOLD + _RED) + value + RESET

    # Seconds: "1.23s"
    if value.endswith("s") and not value.endswith("ms"):
        num, unit = value[:-1], "s"
        return _col(BOLD + _PEACH) + num + _col(_YELLOW) + unit + RESET

    # Milliseconds: "Nms" - color by magnitude
    if value.endswith("ms"):
        try:
            ms = float(value[:-2])
        except ValueError:
            return value
        if ms < 10:
            num_c, unit_c = _col(_SKY), _col(_TEAL)
        elif ms < 100:
            num_c, unit_c = _col(_TEAL), _col(_SKY)
        else:
            num_c, unit_c = _col(_YELLOW), _col(_PEACH)
        return num_c + value[:-2] + unit_c + "ms" + RESET

    return value


def _colorize_bytes(value: str) -> str:
    if not USE_COLOR or not value:
        return value
    # Larger sizes → warmer color
    if value.endswith("GB") or value.endswith("TB"):
        return _col(BOLD + _RED) + value + RESET
    if value.endswith("MB"):
        return _col(BOLD + _PEACH) + value + RESET
    if value.endswith("KB"):
        return _col(_YELLOW) + value + RESET
    return _col(_GREEN) + value + RESET  # Bytes


def _colorize_code(value: str) -> str:
    if not USE_COLOR or not value:
        return value
    try:
        code = int(value)
    except ValueError:
        return value
    if 200 <= code < 300:
        return _col(_GREEN) + value + RESET
    if 300 <= code < 400:
        return _col(_MAUVE) + value + RESET
    if 400 <= code < 500:
        return _col(_MAROON) + value + RESET
    if 500 <= code < 600:
        return _col(BOLD + _RED) + value + RESET
    return value


# ── field definitions ─────────────────────────────────────────────────────────

FIELDS = [
    ["num", "#", 4],
    ["ip", "IP ADDRESS", 16],
    ["dns", "DNS", 9],
    ["tcp", "TCP CONNECT", 13],
    ["tls", "TLS HANDSHAKE", 15],
    ["pretransfer", "PRE-TRANSFER", 14],
    ["ttfb", "1ST BYTE", 10],
    ["redirect", "REDIRECT", 13],
    ["download", "BODY DL", 10],
    ["total", "TOTAL TIME", 12],
    ["code", "HTTP CODE", 11],
    ["bytes", "TOTAL BYTES", 13],
    ["proto", "PROTO", 7],
]

IPV4_IP_WIDTH = 16
IPV6_IP_WIDTH = 42

# Extra columns shown only in --stream (-S) mode: per-chunk arrival timing
# for testing SSE / chunked-transfer responses. Appended to FIELDS and
# FINAL_FIELD_KEYS at startup in main() if -S is passed - never present
# otherwise, so normal runs are unaffected.
STREAM_FIELDS = [
    ["chunks", "CHUNKS", 8],
    ["avggap", "AVG GAP", 10],
    ["maxgap", "MAX GAP", 10],
]
STREAM_FIELD_KEYS = [f[0] for f in STREAM_FIELDS]


def set_ip_column_width(width):
    for field in FIELDS:
        if field[0] == "ip":
            field[2] = width
            return


# NOTE: "ip" is intentionally NOT in here. curl only reports PRIMARY_IP
# once a connection is actually established, but the IP column has to be
# the leftmost thing printed on the row (stdout is append-only left to
# right - there's no going back to fill it in later). Gating column 1 on
# "connection succeeded" meant that any stall between DNS-done and
# connect-done (a hung/black-holed TCP connect, the actual common case)
# left the pointer stuck at "ip" forever, so the eventual <TO>/<ERR>
# marker always landed on IP ADDRESS - even when DNS had already
# resolved fine - and blanked out DNS too since it was never reached.
# The IP is now resolved independently up front (see run_once) so it
# prints immediately and doesn't block on curl's connection state at
# all; this list only covers the phases that genuinely depend on it.
LIVE_FIELD_KEYS = ["dns", "tcp", "tls", "pretransfer", "ttfb"]
FINAL_FIELD_KEYS = ["redirect", "download", "total", "code", "bytes", "proto"]

TIMEOUT_MARK = "<TO>"
ERROR_MARK = "<ERR>"

ERROR_MARKERS = {
    pycurl.E_COULDNT_RESOLVE_PROXY: "<DNS-FAIL>",
    pycurl.E_COULDNT_RESOLVE_HOST: "<DNS-FAIL>",
    pycurl.E_COULDNT_CONNECT: "<CONN-FAIL>",
    pycurl.E_OPERATION_TIMEDOUT: TIMEOUT_MARK,
    pycurl.E_SSL_CONNECT_ERROR: "<TLS-FAIL>",
    pycurl.E_SSL_CERTPROBLEM: "<TLS-FAIL>",
    pycurl.E_SSL_CACERT: "<TLS-FAIL>",
    pycurl.E_PEER_FAILED_VERIFICATION: "<TLS-FAIL>",
    pycurl.E_GOT_NOTHING: "<NO-DATA>",
    pycurl.E_SEND_ERROR: "<SEND-FAIL>",
    pycurl.E_RECV_ERROR: "<RECV-FAIL>",
    pycurl.E_TOO_MANY_REDIRECTS: "<RDR-FAIL>",
    pycurl.E_URL_MALFORMAT: "<BAD-URL>",
    pycurl.E_LOGIN_DENIED: "<AUTH-FAIL>",
    pycurl.E_REMOTE_ACCESS_DENIED: "<DENIED>",
}


def marker_for_errno(errno):
    return ERROR_MARKERS.get(errno, ERROR_MARK)


# ── human-readable formatting ─────────────────────────────────────────────────


def human_time(seconds):
    if seconds is None:
        return ""
    if seconds < 0.001:
        return "<1ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    rem = seconds - minutes * 60
    return f"{minutes}m{rem:.0f}s"


def human_bytes(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ── output helpers ────────────────────────────────────────────────────────────


def write_cell(text: str, width: int, color: str = "", reset: bool = True) -> None:
    """
    Write a padded cell. Padding is applied to the PLAIN text first so that
    ANSI escape codes don't inflate the visual width. Color wraps the padded
    string on the outside.
    """
    padded = text.ljust(width)
    if color and USE_COLOR:
        sys.stdout.write(color + padded + (RESET if reset else ""))
    else:
        sys.stdout.write(padded)
    sys.stdout.flush()


def print_header():
    for _, label, width in FIELDS:
        write_cell(label, width, color=C_HEADER)
    sys.stdout.write(RESET + "\n")
    sys.stdout.flush()


# ── live field helpers ────────────────────────────────────────────────────────

RAW_TIME_GETTERS = {
    "dns": lambda c: c.getinfo(pycurl.NAMELOOKUP_TIME),
    "tcp": lambda c: c.getinfo(pycurl.CONNECT_TIME),
    "tls": lambda c: c.getinfo(pycurl.APPCONNECT_TIME),
    "pretransfer": lambda c: c.getinfo(pycurl.PRETRANSFER_TIME),
    "ttfb": lambda c: c.getinfo(pycurl.STARTTRANSFER_TIME),
}


def try_print_live_field(curl, key, prev_time, row_col=""):
    raw = RAW_TIME_GETTERS[key](curl)
    if raw <= 0:
        return False, prev_time
    delta = max(raw - prev_time, 0.0)
    value = human_time(delta)
    # timing color takes precedence over row color
    t_color = _colorize_time(value)
    if USE_COLOR and t_color != value:
        # _colorize_time already returns fully escaped string - write raw
        padded = value.ljust(field_width(key))
        # re-apply coloring around the padded version
        colored = _colorize_time_padded(value, padded)
        sys.stdout.write(colored)
        sys.stdout.flush()
    else:
        write_cell(value, field_width(key), color=row_col)
    return True, raw


def _colorize_time_padded(value: str, padded: str) -> str:
    """
    Like _colorize_time but wraps the already-padded string.
    The padding spaces get the reset color so they don't carry stray hues.
    """
    if not USE_COLOR or not value:
        return padded

    if value.startswith("<"):
        return C_ERROR + padded + RESET

    if value == "<1ms":
        return _col(_OVERLAY0) + padded + RESET

    if "m" in value and value[0].isdigit() and not value.endswith("ms"):
        return _col(BOLD + _RED) + padded + RESET

    if value.endswith("s") and not value.endswith("ms"):
        num, unit = value[:-1], "s"
        spaces = padded[len(value) :]
        return _col(BOLD + _PEACH) + num + _col(_YELLOW) + unit + RESET + spaces

    if value.endswith("ms"):
        try:
            ms = float(value[:-2])
        except ValueError:
            return padded
        if ms < 10:
            num_c, unit_c = _col(_SKY), _col(_TEAL)
        elif ms < 100:
            num_c, unit_c = _col(_TEAL), _col(_SKY)
        else:
            num_c, unit_c = _col(_YELLOW), _col(_PEACH)
        spaces = padded[len(value) :]
        return num_c + value[:-2] + unit_c + "ms" + RESET + spaces

    return padded


def get_proto_label(curl) -> str:
    """Short label for the HTTP version actually used for the transfer.

    CURLINFO_HTTP_VERSION return values (these are NOT the same as the
    CURL_HTTP_VERSION_* request options - they are a separate enum):
        0  = unknown / not set
        1  = HTTP/1.0
        2  = HTTP/1.1
        3  = HTTP/2
        30 = HTTP/3   (31 = HTTP/3-only)
    """
    try:
        v = curl.getinfo(pycurl.INFO_HTTP_VERSION)
        if v in (30, 31):
            return "h3"
        if v == 3:
            return "h2"
        if v == 1:
            return "h1.0"
    except Exception:
        pass
    return "h1"


def get_final_value(curl, key):
    if key == "proto":
        return get_proto_label(curl)
    if key == "redirect":
        count = int(curl.getinfo(pycurl.REDIRECT_COUNT))
        if count == 0:
            return ""
        rtime = curl.getinfo(pycurl.REDIRECT_TIME)
        return f"{count}\u00d7 {human_time(rtime)}"
    if key == "download":
        total = curl.getinfo(pycurl.TOTAL_TIME)
        ttfb = curl.getinfo(pycurl.STARTTRANSFER_TIME)
        return human_time(max(total - ttfb, 0.0))
    if key == "total":
        return human_time(curl.getinfo(pycurl.TOTAL_TIME))
    if key == "code":
        return str(curl.getinfo(pycurl.RESPONSE_CODE))
    if key == "bytes":
        try:
            size = curl.getinfo(pycurl.SIZE_DOWNLOAD_T)
        except AttributeError:
            size = curl.getinfo(pycurl.SIZE_DOWNLOAD)
        return human_bytes(size)
    return ""


def _write_final_cell(key: str, value: str, width: int, row_col: str) -> None:
    """Write a final-phase cell with the right color for its content type."""
    if not value:
        write_empty_cell(key, width)
        return

    if not USE_COLOR:
        write_cell(value, width, color=row_col)
        return

    if value.startswith("<"):  # error marker
        write_cell(value, width, color=C_ERROR)
        return

    if key in (
        "dns",
        "tcp",
        "tls",
        "pretransfer",
        "ttfb",
        "download",
        "total",
        "avggap",
        "maxgap",
    ):
        padded = value.ljust(width)
        colored = _colorize_time_padded(value, padded)
        sys.stdout.write(colored)
        sys.stdout.flush()
        return

    if key == "proto":
        proto_color = _col(C_H2) if value == "h2" else _col(C_H1)
        write_cell(value, width, color=proto_color)
        return

    if key == "redirect":
        write_cell(value, width, color=_col(C_REDIR) if value else row_col)
        return

    if key == "code":
        code_color = (
            _col(_GREEN)
            if value.startswith("2")
            else _col(_MAUVE)
            if value.startswith("3")
            else _col(_MAROON)
            if value.startswith("4")
            else _col(BOLD + _RED)
            if value.startswith("5")
            else row_col
        )
        write_cell(value, width, color=code_color)
        return

    if key == "bytes":
        padded = value.ljust(width)
        b_color = (
            _col(BOLD + _RED)
            if value.endswith(("GB", "TB"))
            else _col(BOLD + _PEACH)
            if value.endswith("MB")
            else _col(_YELLOW)
            if value.endswith("KB")
            else _col(_GREEN)
        )
        sys.stdout.write(b_color + padded + RESET)
        sys.stdout.flush()
        return

    write_cell(value, width, color=row_col)


def field_width(key):
    for fkey, _, width in FIELDS:
        if fkey == key:
            return width
    return 10


# ── single request ────────────────────────────────────────────────────────────


def run_once(
    run_num,
    url,
    timeout,
    ip_version="4",
    user_agent=DEFAULT_USER_AGENT,
    headers=None,
    data=None,
    method=None,
    force_dns=False,
    resolve=None,
    http_version=None,
    stream_mode=False,
    pin_ip=None,
    quiet=False,
    capture_body=False,
    capture_headers=False,
    capture_cert=False,
):
    # quiet=True drives the request but writes nothing to stdout (used by
    # --prometheus mode); the collected result dict is returned either way so
    # --stats, assertions, --tls-info and --show-headers can consume it.
    rcol = _row_color(run_num)  # base color for this row

    if not quiet:
        write_cell(str(run_num), field_width("num"), color=_col(C_LINENUM))

    # IP ADDRESS is resolved here, independently of curl, and printed
    # immediately - see the LIVE_FIELD_KEYS comment for why it can't be
    # sourced from curl's PRIMARY_IP without risking the whole row
    # blanking out on a slow/hung connect. When pinned (-p/-P) the IP is
    # already known, so this is just a plain lookup in that case, not a
    # second DNS round-trip.
    #
    # If this lookup fails, that does NOT necessarily mean the request
    # itself will fail - e.g. behind an HTTP(S) proxy, curl can complete
    # the request by handing the hostname to the proxy without ever
    # doing its own forward resolution, so a plain socket.getaddrinfo()
    # here can fail even though curl succeeds moments later. So a
    # failure here is non-fatal: print a plain "-" (not an error marker)
    # and let curl still make its own attempt. If curl's own resolution
    # also fails, that failure now correctly lands on the DNS column via
    # the normal LIVE_FIELD_KEYS/pointer mechanism below - not here.
    if pin_ip:
        ip_display = pin_ip
    else:
        hostname, port = url_host_port(url)
        ip_display = resolve_ip(hostname, port, ip_version) if hostname else None

    if not quiet:
        if ip_display is None:
            write_empty_cell("ip", field_width("ip"))
        else:
            write_cell(ip_display, field_width("ip"), color=_col(C_IP) or rcol)

    curl = pycurl.Curl()
    curl.setopt(curl.URL, url)

    chunk_times = []
    body_buf = bytearray()

    def _write_cb(chunk):
        if stream_mode:
            chunk_times.append(time.perf_counter())
        if capture_body and len(body_buf) < BODY_CAPTURE_LIMIT:
            body_buf.extend(chunk[: BODY_CAPTURE_LIMIT - len(body_buf)])
        return len(chunk)

    curl.setopt(curl.WRITEFUNCTION, _write_cb)

    header_lines = []
    if capture_headers:

        def _header_cb(line):
            try:
                header_lines.append(line.decode("iso-8859-1"))
            except Exception:
                pass

        curl.setopt(curl.HEADERFUNCTION, _header_cb)

    if capture_cert:
        try:
            curl.setopt(pycurl.OPT_CERTINFO, 1)
        except Exception:
            pass

    curl.setopt(curl.FOLLOWLOCATION, True)
    curl.setopt(curl.TIMEOUT_MS, int(timeout * 1000))
    curl.setopt(curl.CONNECTTIMEOUT_MS, int(timeout * 1000))
    curl.setopt(curl.NOSIGNAL, 1)
    curl.setopt(curl.SSL_VERIFYPEER, 1)
    curl.setopt(curl.SSL_VERIFYHOST, 2)
    curl.setopt(curl.USERAGENT, user_agent)
    curl.setopt(
        curl.IPRESOLVE,
        pycurl.IPRESOLVE_V6 if ip_version == "6" else pycurl.IPRESOLVE_V4,
    )

    if http_version is not None:
        curl.setopt(curl.HTTP_VERSION, http_version)

    if resolve:
        curl.setopt(curl.RESOLVE, resolve)

    if force_dns:
        curl.setopt(curl.DNS_CACHE_TIMEOUT, 0)
        curl.setopt(curl.FRESH_CONNECT, 1)
        curl.setopt(curl.FORBID_REUSE, 1)

    if headers:
        curl.setopt(curl.HTTPHEADER, headers)

    if data is not None:
        curl.setopt(curl.POSTFIELDS, data)

    if method:
        curl.setopt(curl.CUSTOMREQUEST, method)

    multi = pycurl.CurlMulti()
    multi.add_handle(curl)

    pointer = 0
    prev_time = 0.0
    failed = False
    fail_errno = None

    try:
        while True:
            ret, num_active = multi.perform()
            while ret == pycurl.E_CALL_MULTI_PERFORM:
                ret, num_active = multi.perform()

            if not quiet:
                while pointer < len(LIVE_FIELD_KEYS):
                    printed, prev_time = try_print_live_field(
                        curl, LIVE_FIELD_KEYS[pointer], prev_time, row_col=rcol
                    )
                    if not printed:
                        break
                    pointer += 1

            if num_active == 0:
                break

            multi.select(0.001)

        num_q, ok_list, err_list = multi.info_read()
        for handle, errno, errmsg in err_list:
            failed = True
            fail_errno = errno

    except pycurl.error as exc:
        failed = True
        fail_errno = exc.args[0] if exc.args else None

    finally:
        multi.remove_handle(curl)
        multi.close()

    res = {
        "run": run_num,
        "ip": ip_display,
        "failed": failed,
        "errno": fail_errno,
        "marker": None,
        "phases": compute_phase_deltas(curl),
        "code": None,
        "bytes": None,
        "proto": None,
        "redirect_count": 0,
        "redirect_time": 0.0,
        "chunks": None,
        "avggap": None,
        "maxgap": None,
        "headers": parse_response_headers(header_lines) if capture_headers else None,
        "body": bytes(body_buf) if capture_body else None,
        "cert": extract_cert_info(curl) if capture_cert else None,
    }

    if failed:
        res["marker"] = marker_for_errno(fail_errno)
        if not quiet:
            if pointer < len(LIVE_FIELD_KEYS):
                write_cell(
                    res["marker"],
                    field_width(LIVE_FIELD_KEYS[pointer]),
                    color=_col(C_ERROR),
                )
                pointer += 1
            while pointer < len(LIVE_FIELD_KEYS):
                write_empty_cell(
                    LIVE_FIELD_KEYS[pointer], field_width(LIVE_FIELD_KEYS[pointer])
                )
                pointer += 1
            for key in FINAL_FIELD_KEYS:
                write_empty_cell(key, field_width(key))
            sys.stdout.write(RESET + "\n")
            sys.stdout.flush()
        curl.close()
        return res

    # success: finish printing any remaining live fields (display only)
    if not quiet:
        while pointer < len(LIVE_FIELD_KEYS):
            key = LIVE_FIELD_KEYS[pointer]
            printed, prev_time = try_print_live_field(
                curl, key, prev_time, row_col=rcol
            )
            if not printed:
                write_empty_cell(key, field_width(key))
            pointer += 1

    res["code"] = int(curl.getinfo(pycurl.RESPONSE_CODE))
    res["proto"] = get_proto_label(curl)
    res["redirect_count"] = int(curl.getinfo(pycurl.REDIRECT_COUNT))
    res["redirect_time"] = curl.getinfo(pycurl.REDIRECT_TIME)
    try:
        res["bytes"] = int(curl.getinfo(pycurl.SIZE_DOWNLOAD_T))
    except (AttributeError, pycurl.error):
        res["bytes"] = int(curl.getinfo(pycurl.SIZE_DOWNLOAD))

    stream_stats = {}
    if stream_mode:
        chunk_count = len(chunk_times)
        res["chunks"] = chunk_count
        stream_stats["chunks"] = str(chunk_count)
        # AVG GAP / MAX GAP measure the cadence BETWEEN chunks only - the
        # gap from request start to the first chunk is already the DNS+TCP+
        # TLS+PRE-TRANSFER+1ST BYTE span shown in the earlier columns, so
        # including it here would double-count that time as if it were
        # in-stream stutter. With fewer than 2 chunks there's no inter-chunk
        # gap to measure at all, so it's a genuine "n/a", not just missing.
        if chunk_count >= 2:
            gaps = [chunk_times[i] - chunk_times[i - 1] for i in range(1, chunk_count)]
            res["avggap"] = sum(gaps) / len(gaps)
            res["maxgap"] = max(gaps)
            stream_stats["avggap"] = human_time(res["avggap"])
            stream_stats["maxgap"] = human_time(res["maxgap"])
        else:
            stream_stats["avggap"] = ""
            stream_stats["maxgap"] = ""

    if not quiet:
        for key in FINAL_FIELD_KEYS:
            value = (
                stream_stats[key] if key in stream_stats else get_final_value(curl, key)
            )
            _write_final_cell(key, value, field_width(key), rcol)
        sys.stdout.write(RESET + "\n")
        sys.stdout.flush()

    curl.close()
    return res


# ── helpers ───────────────────────────────────────────────────────────────────


def resolve_data_arg(raw):
    if raw.startswith("@"):
        path = raw[1:]
        with open(path, "rb") as fh:
            return fh.read()
    return raw


def url_host_port(url):
    """
    Extract (hostname, port) from url, filling in the scheme default
    port. Returns (None, None) - does NOT exit - if no hostname can be
    parsed out, since this is also called once per row from run_once():
    a malformed URL should surface there as the same <BAD-URL> marker
    curl itself already reports (via CURLE_URL_MALFORMAT), not abort the
    whole run. Startup-time callers that DO want to fail fast on a bad
    URL (e.g. build_pin_resolve, before anything has been printed) check
    for None themselves and exit there.
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        return None, None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return hostname, port


def resolve_ip(hostname, port, ip_version):
    """
    One-shot forward lookup used to populate the IP ADDRESS column up
    front, independent of curl's own connection state (see the
    LIVE_FIELD_KEYS comment for why that separation matters). Returns
    the first resolved address, or None if resolution fails.
    """
    family = socket.AF_INET6 if ip_version == "6" else socket.AF_INET
    try:
        infos = socket.getaddrinfo(hostname, port, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return infos[0][4][0]


def build_pin_resolve(url, pin_value, ip_version):
    hostname, port = url_host_port(url)
    if hostname is None:
        sys.stderr.write(f"error: could not parse a hostname out of: {url}\n")
        sys.exit(1)

    if pin_value == "auto":
        family = socket.AF_INET6 if ip_version == "6" else socket.AF_INET
        try:
            infos = socket.getaddrinfo(hostname, port, family, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            sys.stderr.write(f"error: could not resolve {hostname}: {exc}\n")
            sys.exit(1)
        ip = infos[0][4][0]
    else:
        ip = pin_value

    return [f"{hostname}:{port}:{ip}"], ip, hostname


# ── result collection ─────────────────────────────────────────────────────────


def compute_phase_deltas(curl):
    """Per-phase deltas (seconds) from libcurl timers, matching the live
    display: DNS, TCP CONNECT, TLS HANDSHAKE, PRE-TRANSFER, 1ST BYTE, plus
    BODY DL and TOTAL. A phase that never happened (e.g. TLS on plain http,
    or anything after a mid-connection failure) comes back as None."""
    steps = (
        ("dns", pycurl.NAMELOOKUP_TIME),
        ("tcp", pycurl.CONNECT_TIME),
        ("tls", pycurl.APPCONNECT_TIME),
        ("pretransfer", pycurl.PRETRANSFER_TIME),
        ("ttfb", pycurl.STARTTRANSFER_TIME),
    )
    out = {}
    prev = 0.0
    for key, info in steps:
        raw = curl.getinfo(info)
        if not raw or raw <= 0:
            out[key] = None
            continue
        out[key] = max(raw - prev, 0.0)
        prev = raw
    total = curl.getinfo(pycurl.TOTAL_TIME)
    ttfb_raw = curl.getinfo(pycurl.STARTTRANSFER_TIME)
    out["download"] = (
        max(total - ttfb_raw, 0.0) if total and ttfb_raw and ttfb_raw > 0 else None
    )
    out["total"] = total if total and total > 0 else None
    return out


def parse_response_headers(lines):
    """Fold captured header lines into a dict of the FINAL response. Headers
    reset at each status line, so after redirects only the last block wins."""
    headers = {}
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.upper().startswith("HTTP/"):
            headers = {"_status_line": line}
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers


def _cert_expiry_days(datestr):
    """Days from now until an OpenSSL-style 'Expire date' string, or None."""
    s = " ".join(datestr.split())
    if s.upper().endswith(" GMT"):
        s = s[:-4]
    try:
        dt = datetime.strptime(s, "%b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (dt - datetime.now(UTC)).days


def extract_cert_info(curl):
    """Pull leaf-certificate details from CURLINFO_CERTINFO (populated only
    when OPT_CERTINFO was set and the connection used TLS). Returns None for
    plain http:// or if the SSL backend did not provide cert data."""
    try:
        chain = curl.getinfo(pycurl.INFO_CERTINFO)
    except Exception:
        return None
    if not chain:
        return None
    leaf = {}
    for item in chain[0]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            leaf[item[0]] = item[1]
    info = {
        "subject": leaf.get("Subject"),
        "issuer": leaf.get("Issuer"),
        "start": leaf.get("Start date"),
        "expire": leaf.get("Expire date"),
        "san": leaf.get("X509v3 Subject Alternative Name"),
        "days_left": None,
    }
    if info["expire"]:
        info["days_left"] = _cert_expiry_days(info["expire"])
    return info


# ── assertions / thresholds ───────────────────────────────────────────────────


def parse_duration(s):
    """Parse a threshold duration: '500ms', '1s', '1.5s', or a bare number of
    seconds. Raises ValueError on anything else."""
    s = s.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


def evaluate_assertions(res, cfg):
    """Return a list of human-readable failure reasons for one run (empty list
    means it passed). A network failure fails outright; otherwise the status
    code, per-phase timing thresholds, and body checks are all evaluated."""
    if res["failed"]:
        return [f"request failed ({res.get('marker') or 'error'})"]
    fails = []
    if cfg["status"] is not None and res["code"] != cfg["status"]:
        fails.append(f"status {res['code']} != {cfg['status']}")
    for key, limit in cfg["thresholds"].items():
        val = res["phases"].get(key)
        if val is not None and val > limit:
            fails.append(f"{key} {human_time(val)} > {human_time(limit)}")
    if cfg["expect_body"] is not None or cfg["expect_regex"] is not None:
        text = (res.get("body") or b"").decode("utf-8", "replace")
        if cfg["expect_body"] is not None and cfg["expect_body"] not in text:
            fails.append(f"body missing substring {cfg['expect_body']!r}")
        if cfg["expect_regex"] is not None and not cfg["expect_regex"].search(text):
            fails.append(f"body did not match /{cfg['expect_regex'].pattern}/")
    return fails


_TIME_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:ms|s)$")
_MIN_TOKEN = re.compile(r"^\d+m\d+s$")
_CODE_TOKEN = re.compile(r"^\d{3}$")


def _colorize_reason(text):
    """Color the HTTP codes, timing values, and error markers embedded in an
    assertion reason string using the same scheme as the main table, leaving
    the connective words (status, >, !=, ...) in the default color."""
    if not USE_COLOR:
        return text
    parts = []
    for tok in text.split(" "):
        if "<" in tok and ">" in tok:  # <CONN-FAIL>, <TO>, ...
            parts.append(C_ERROR + tok + RESET)
        elif _TIME_TOKEN.match(tok) or _MIN_TOKEN.match(tok):
            parts.append(_colorize_time(tok))
        elif _CODE_TOKEN.match(tok):
            parts.append(_colorize_code(tok))
        else:
            parts.append(tok)
    return " ".join(parts)


# ── percentile summary ────────────────────────────────────────────────────────


def _percentile(sorted_vals, p):
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    k = math.ceil(p / 100.0 * len(sorted_vals)) - 1
    return sorted_vals[max(0, min(k, len(sorted_vals) - 1))]


_SUMMARY_PHASES = [
    ("dns", "DNS"),
    ("tcp", "TCP CONNECT"),
    ("tls", "TLS HANDSHAKE"),
    ("pretransfer", "PRE-TRANSFER"),
    ("ttfb", "1ST BYTE"),
    ("download", "BODY DL"),
    ("total", "TOTAL TIME"),
]


def print_summary(results):
    """Percentile footer across successful runs. Shown only with 2+ successes,
    since percentiles are meaningless with fewer samples."""
    ok = [r for r in results if not r["failed"]]
    nfail = len(results) - len(ok)
    if len(ok) < 2:
        return
    cols = ["min", "p50", "p90", "p95", "p99", "max", "mean", "stdev"]
    title = f"SUMMARY  ({len(ok)} ok, {nfail} failed)"
    head = "PHASE".ljust(14) + "".join(c.rjust(9) for c in cols)
    end = RESET if USE_COLOR else ""
    sys.stdout.write("\n" + _col(C_HEADER) + title + end + "\n")
    sys.stdout.write(_col(C_HEADER) + head + end + "\n")

    # Right-justify on the PLAIN text width, then wrap in the standard timing
    # / byte colorizer so ANSI codes do not throw off column alignment.
    def _cell_time(plain, width=9):
        return " " * max(width - len(plain), 0) + _colorize_time(plain)

    def _cell_bytes(plain, width=9):
        return " " * max(width - len(plain), 0) + _colorize_bytes(plain)

    def stat_row(label, values, fmt, cell):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return
        computed = [
            vals[0],
            _percentile(vals, 50),
            _percentile(vals, 90),
            _percentile(vals, 95),
            _percentile(vals, 99),
            vals[-1],
            statistics.fmean(vals),
            statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        ]
        lbl = _col(_SUBTEXT0) + label.ljust(14) + end
        sys.stdout.write(lbl + "".join(cell(fmt(v)) for v in computed) + "\n")

    for key, label in _SUMMARY_PHASES:
        stat_row(label, [r["phases"].get(key) for r in ok], human_time, _cell_time)
    stat_row("TOTAL BYTES", [r["bytes"] for r in ok], human_bytes, _cell_bytes)
    sys.stdout.flush()


# ── TLS / header blocks ───────────────────────────────────────────────────────


def print_tls_info(cert):
    end = RESET if USE_COLOR else ""
    sys.stdout.write("\n" + _col(C_HEADER) + "TLS CERTIFICATE" + end + "\n")
    if not cert:
        sys.stdout.write(
            _col(C_LINENUM)
            + "  (no certificate: not an HTTPS connection, or cert data unavailable)"
            + end
            + "\n"
        )
        sys.stdout.flush()
        return

    def line(label, value):
        if value:
            key = _col(_BLUE) + f"  {label:<9}" + end
            val = _col(_TEXT) + str(value) + end
            sys.stdout.write(f"{key} {val}\n")

    line("subject:", cert.get("subject"))
    line("issuer:", cert.get("issuer"))

    days = cert.get("days_left")
    exp = cert.get("expire") or "?"
    key = _col(_BLUE) + f"  {'expires:':<9}" + end
    if days is None:
        sys.stdout.write(f"{key} {_col(_TEXT)}{exp}{end}\n")
    else:
        # green = healthy, yellow = close, orange = near, red = expired
        if days < 0:
            col, note = _col(BOLD + _RED), f"EXPIRED {abs(days)} days ago"
        elif days < 15:
            col, note = _col(_PEACH), f"{days} days left (near expiration)"
        elif days < 30:
            col, note = _col(_YELLOW), f"{days} days left (close to expiration)"
        else:
            col, note = _col(_GREEN), f"{days} days left"
        tail = f"{exp}  ({note})"
        body = (col + tail + RESET) if USE_COLOR else tail
        sys.stdout.write(f"{key} {body}\n")
    line("san:", cert.get("san"))
    sys.stdout.flush()


_CURATED_HEADERS = [
    "server",
    "content-type",
    "content-encoding",
    "content-length",
    "age",
    "cache-control",
    "x-cache",
    "cf-cache-status",
    "cf-ray",
    "via",
    "x-served-by",
    "etag",
    "strict-transport-security",
]


def _detect_cache(h):
    for key in ("x-cache", "cf-cache-status", "x-cache-status"):
        v = h.get(key, "")
        low = v.lower()
        if "hit" in low:
            return f"HIT (via {key})"
        if "miss" in low:
            return f"MISS (via {key})"
    if h.get("age"):
        return f"likely HIT (age={h['age']})"
    return None


def print_headers_block(results):
    end = RESET if USE_COLOR else ""
    sys.stdout.write(
        "\n" + _col(C_HEADER) + "RESPONSE HEADERS (final response)" + end + "\n"
    )
    ok = [r for r in results if not r["failed"] and r.get("headers")]
    if not ok:
        sys.stdout.write(_col(C_LINENUM) + "  (no headers captured)" + end + "\n")
        sys.stdout.flush()
        return
    h = ok[-1]["headers"]
    shown = False
    for key in _CURATED_HEADERS:
        if key in h:
            k = _col(_LAVENDER) + f"  {key}:" + end
            v = _col(_SUBTEXT0) + h[key] + end
            sys.stdout.write(f"{k} {v}\n")
            shown = True
    cache = _detect_cache(h)
    if cache:
        if "HIT" in cache:
            cache_col = _col(_GREEN)
        elif "MISS" in cache:
            cache_col = _col(_PEACH)
        else:
            cache_col = _col(_YELLOW)
        k = _col(_LAVENDER) + "  cache:" + end
        body = (cache_col + cache + RESET) if USE_COLOR else cache
        sys.stdout.write(f"{k} {body}\n")
        shown = True
    if not shown:
        sys.stdout.write(
            _col(C_LINENUM) + "  (none of the common headers were present)" + end + "\n"
        )
    sys.stdout.flush()


# ── Prometheus exporter ───────────────────────────────────────────────────────


def _prom_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def build_prometheus_text(url, results, cert):
    """Build an OpenMetrics/text-exposition string from one probe cycle:
    check_endpoint_up, the last successful run's per-phase *_seconds gauges,
    aggregate total-time percentiles (when -c > 1), response code/bytes, and
    (over HTTPS) the TLS expiry in days."""
    host = url_host_port(url)[0] or ""
    labels = f'url="{_prom_escape(url)}",host="{_prom_escape(host)}"'
    ok = [r for r in results if not r["failed"]]
    last = ok[-1] if ok else None
    out = []

    def emit(name, help_text, value):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.append(f"{name}{{{labels}}} {value}")

    emit(
        "check_endpoint_up",
        "1 if the most recent probe succeeded, else 0",
        1 if last else 0,
    )
    emit(
        "check_endpoint_requests_total",
        "Number of probes performed this scrape",
        len(results),
    )
    emit(
        "check_endpoint_failures_total",
        "Number of failed probes this scrape",
        len(results) - len(ok),
    )

    if last is not None:
        emit(
            "check_endpoint_http_response_code",
            "HTTP status code of the last successful probe",
            last["code"],
        )
        phase_metrics = [
            ("dns", "check_endpoint_dns_seconds", "DNS lookup time (seconds)"),
            ("tcp", "check_endpoint_tcp_connect_seconds", "TCP connect time (seconds)"),
            (
                "tls",
                "check_endpoint_tls_handshake_seconds",
                "TLS handshake time (seconds)",
            ),
            (
                "pretransfer",
                "check_endpoint_pretransfer_seconds",
                "Pre-transfer time (seconds)",
            ),
            (
                "ttfb",
                "check_endpoint_first_byte_seconds",
                "Time to first byte (seconds)",
            ),
            (
                "download",
                "check_endpoint_body_download_seconds",
                "Body download time (seconds)",
            ),
            ("total", "check_endpoint_total_seconds", "Total request time (seconds)"),
        ]
        for key, name, help_text in phase_metrics:
            v = last["phases"].get(key)
            if v is not None:
                emit(name, help_text, f"{v:.6f}")
        if last["bytes"] is not None:
            emit(
                "check_endpoint_response_bytes",
                "Response body size (bytes)",
                last["bytes"],
            )

    totals = sorted(
        r["phases"]["total"] for r in ok if r["phases"].get("total") is not None
    )
    if len(totals) >= 2:
        for p in (50, 90, 95, 99):
            emit(
                f"check_endpoint_total_seconds_p{p}",
                f"p{p} of total request time across this scrape's runs (seconds)",
                f"{_percentile(totals, p):.6f}",
            )

    if cert and cert.get("days_left") is not None:
        emit(
            "check_endpoint_tls_expiry_days",
            "Days until the TLS certificate expires",
            cert["days_left"],
        )

    return "\n".join(out) + "\n"


class _MetricsHandler(BaseHTTPRequestHandler):
    """Runs a fresh probe cycle on every GET (any path) and returns metrics."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        try:
            results, cert = self.server.probe_fn()
            body = build_prometheus_text(self.server.probe_url, results, cert).encode(
                "utf-8"
            )
            status = 200
        except Exception as exc:  # never let a scrape crash the server
            body = f"# probe error: {exc}\n".encode()
            status = 500
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    # HEAD is used by some health checks; answer it without a body.
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "check-endpoint: scrape from %s - %s\n"
            % (self.address_string(), fmt % args)
        )


def serve_prometheus(bind, port, url, probe_fn):
    """Block serving the Prometheus exporter until Ctrl+C. Each scrape calls
    probe_fn() to run a fresh probe cycle and returns the resulting metrics."""
    httpd = ThreadingHTTPServer((bind, port), _MetricsHandler)
    httpd.daemon_threads = True
    httpd.probe_fn = probe_fn
    httpd.probe_url = url
    shown = bind or "0.0.0.0"
    sys.stderr.write(
        f"check-endpoint: Prometheus exporter on http://{shown}:{port}/  "
        f"(probes {url} on each scrape; Ctrl+C to stop)\n"
    )
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\ncheck-endpoint: shutting down\n")
    finally:
        httpd.server_close()


# ── main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="check-endpoint.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "check-endpoint.py: live per-phase HTTP timing probe, curl-style.\n\n"
            "Sends one or more requests to a URL and prints DNS, TCP, TLS, and\n"
            "transfer timing for each phase as it happens (not all at once at the\n"
            "end), so a hung request visibly stalls at whichever phase is stuck.\n"
            "On failure, a short marker (e.g. <DNS-FAIL>, <CONN-FAIL>, <TO>) is\n"
            "printed at the phase that failed and the run moves on to the next one."
        ),
        epilog=f"""\
FIELDS REPORTED (in column order)
  #              request counter (1-based) across -c N runs
  IP ADDRESS     resolved IP of the remote host
  DNS            time spent on DNS lookup (that phase only)
  TCP CONNECT    time spent on the TCP handshake (that phase only)
  TLS HANDSHAKE  time spent on the TLS handshake (blank for plain http://)
  PRE-TRANSFER   time from connect-ready to request-send-ready
  1ST BYTE       time from request sent to first byte of the response body
  REDIRECT       redirects followed: count and total time (blank if none).
                 REDIRECT time is why TOTAL TIME can exceed the sum of other
                 columns - it accounts for all redirect round-trips.
  BODY DL        time to receive the full response body after the first byte
  TOTAL TIME     total end-to-end request time including any redirects
  HTTP CODE      response status code
  TOTAL BYTES    size of the response body received
  PROTO          HTTP version actually used: h1 (HTTP/1.1), h1.0 (HTTP/1.0),
                 h2 (HTTP/2), or h3 (HTTP/3)

  Every column except TOTAL TIME is a per-phase delta.
  DNS + TCP + TLS + PRE-TRANSFER + 1ST BYTE + REDIRECT + BODY DL ≈ TOTAL TIME

  With -S/--stream, three extra columns are appended:
  CHUNKS         number of chunks the response body arrived in
  AVG GAP        average time BETWEEN consecutive chunks (excludes the
                 first chunk's arrival - that span is already the DNS +
                 TCP + TLS + PRE-TRANSFER + 1ST BYTE columns, so counting
                 it again here would double as fake in-stream stutter)
  MAX GAP        longest of those inter-chunk gaps
  These only appear with -S; a normal run's columns are unaffected. With
  fewer than 2 chunks there's no inter-chunk gap to measure, so both show
  n/a rather than a number.

  All times are shown in human-readable units (e.g. 17ms, 1.20s, 1m30s).
  All byte sizes are shown in human-readable units (e.g. 980B, 1.2KB, 4.0MB).

EMPTY CELLS
  A dim/grey "n/a" means the phase structurally doesn't apply to this
  request (e.g. TLS HANDSHAKE on a plain http:// URL, or REDIRECT when no
  redirects were followed). A dim/grey "-" means the field is empty for
  any other reason (e.g. truncated by a failure mid-transfer).

COLOR SCHEME (Catppuccin Mocha - auto-disabled when output is piped)
  Header row     bold blue
  PROTO h2       teal (HTTP/2 - preferred, modern)
  PROTO h1       dim (HTTP/1.1 - older protocol)
  Odd rows       primary text color
  Even rows      slightly dimmed text color
  <1ms           dim (sub-millisecond, not worth highlighting)
  1-9ms          sky blue (fast)
  10-99ms        teal (moderate)
  ≥100ms         yellow/peach (getting slow)
  seconds        bold peach (slow)
  minutes        bold red (very slow)
  REDIRECT       peach (stands out as an unexpected addition to total time)
  Error markers  bold red
  n/a and -      dim overlay (same shade as the row number column)
  2xx codes      green
  3xx codes      mauve
  4xx codes      maroon
  5xx codes      bold red
  Bytes          green → yellow → peach → red (B → KB → MB → GB)
  IP address     lavender
  Row number     dim overlay

NOTES ON COMMON OBSERVATIONS
  PRE-TRANSFER = 0ms on direct HTTPS
    This is correct. Once TLS completes, libcurl is immediately ready to
    transfer. The gap between APPCONNECT_TIME and PRETRANSFER_TIME is
    genuinely near zero for direct HTTPS connections.

  TLS HANDSHAKE has a value even when URL is http://
    This is correct. The http:// URL redirected to https://, and the TLS
    column shows the handshake time for that final HTTPS connection.
    The redirect itself is accounted for in the REDIRECT column.

FAILURE MARKERS
  <TO>          the request timed out (-t/--timeout exceeded)
  <DNS-FAIL>    DNS resolution failed
  <CONN-FAIL>   TCP connection was refused/failed
  <TLS-FAIL>    TLS handshake or certificate verification failed
  <NO-DATA>     connection succeeded but the server sent nothing back
  <SEND-FAIL>   failed sending the request mid-transfer
  <RECV-FAIL>   failed receiving the response mid-transfer
  <RDR-FAIL>    too many redirects (redirects are followed by default)
  <BAD-URL>     the URL is malformed
  <AUTH-FAIL>   login/authentication was denied
  <DENIED>      remote access was denied
  <ERR>         any other libcurl error not covered above

USER-AGENT ALIASES (-a/--user-agent)
  Without -a, the default User-Agent is '{DEFAULT_USER_AGENT}'.
  Built-in aliases (send a real browser/bot UA string instead):
    chrome     Chrome on Windows 10/11
    firefox    Firefox on Windows 10/11
    edge       Microsoft Edge on Windows 10/11
    safari     Safari on macOS
    googlebot  Googlebot crawler UA

WHAT IT CAN FIND

  DNS & Resolution
    - Slow or flaky resolvers: high or variable DNS times across -c N runs
    - Missing local DNS cache: DNS time stays high on every request instead
      of dropping to ~0ms after the first lookup
    - Short TTLs: DNS time spikes when the record expires mid-test
    - Resolution failures: <DNS-FAIL> when a hostname cannot be resolved at all

  TCP & Network
    - Geographic latency: high TCP CONNECT reveals distance to the server
    - Server connection backlog: TCP time increases under load as the server
      runs out of accept queue capacity
    - Firewall / filtering: <CONN-FAIL> on specific ports or from specific
      network paths
    - Routing instability: variable TCP times across runs on the same IP

  TLS & Security
    - Missing session resumption: TLS time stays high on every repeat request
      instead of dropping on run 2+; compare first request vs subsequent ones
    - Slow OCSP validation or long certificate chains: consistently high TLS
    - Certificate problems: <TLS-FAIL> for expired cert, hostname mismatch,
      or untrusted CA

  Server Processing (1ST BYTE - most diagnostic column)
    - Slow backend: high 1ST BYTE reveals heavy server work (DB queries,
      auth checks, rendering, computation) before the first byte is sent
    - Queue depth behind a reverse proxy: fast TCP + slow 1ST BYTE means
      the proxy accepted the connection but the backend was busy
    - Backend inconsistency: variable 1ST BYTE across runs reveals hot/cold
      cache states, uneven DB load, or connection pool exhaustion
    - Classic pattern - high 1ST BYTE + fast BODY DL: the server is slow
      to produce the response but fast to send it once ready; the problem
      is computation or IO on the server, not the network

  Body Transfer & Server-side IO
    - Slow server IO: high BODY DL relative to content size (slow disk reads,
      DB streaming, rate-limited send buffers)
    - Bandwidth throttling: BODY DL grows disproportionately with response size
    - Inconsistent content size: TOTAL BYTES varies across -c N runs - reveals
      A/B testing, CDN inconsistencies, partial/truncated responses, or bugs
      where the server occasionally sends the wrong payload

  Intermittent & Flaky Behavior
    - Mixed response codes (200, 502, 503) across -c N runs reveal backend
      instability, pods cycling in Kubernetes, or upstream timeouts
    - Occasional <TO> markers among otherwise successful runs indicate
      connection pool exhaustion, GC pauses, or health check races
    - Outlier requests dramatically slower than the rest - cold cache misses,
      JVM garbage collection pauses, or single-threaded lock contention

  Load Balancing & Round-Robin
    - Uneven backends: without -P, different IPs per request show which
      backends are in rotation; timing differences per IP identify slow ones
    - Isolate one backend: use -P to pin all requests to a single IP and
      measure it in isolation, then switch IPs to compare
    - Intermittent errors from specific backends: combine IP column with HTTP
      CODE column to see which backend is returning errors

  Authentication & Specific Endpoints
    - Test authenticated APIs: -H "Authorization: Bearer token" - a 401/403
      or <AUTH-FAIL> points to auth configuration issues
    - Test POST/PUT/PATCH endpoints: -d @payload.json combined with
      -H "Content-Type: application/json" and -X PUT/-X PATCH
    - Token expiry under load: combine auth headers with -c 20 to see if
      token validation slows down or fails after repeated calls
    - Custom routing headers: -H "X-Forwarded-For: ..." or
      -H "X-Feature-Flag: ..." to test header-conditional behavior

  Client-Side
    - Non-zero PRE-TRANSFER on every request: this phase is internal libcurl
      bookkeeping and is normally ~0ms; consistently high values indicate
      CPU pressure on the machine running check-endpoint.py itself

HTTP/2 SUPPORT
  --http2
    Request HTTP/2 via ALPN negotiation on HTTPS connections. libcurl sends
    the "h2" ALPN token during the TLS handshake; the server replies with
    "h2" if it supports HTTP/2 or "http/1.1" to fall back. The PROTO column
    shows which version was actually used.

    Without --http2, libcurl defaults to HTTP/1.1 even if the server supports
    HTTP/2. Use --http2 explicitly to verify whether a server speaks HTTP/2.

    Requires libcurl built with nghttp2. Check with: curl --version | grep HTTP2

  --http2-prior-knowledge
    Send HTTP/2 frames directly over a plain http:// connection without TLS
    (h2c - HTTP/2 cleartext, RFC 7540 Section 3.4). Only use when you
    control both client and server and know the server accepts h2c.
    Most public web servers reject this; use --http2 for HTTPS instead.

  HTTP/2 and the timing columns
    HTTP/2 multiplexes requests over a single persistent connection.
    On repeated -c N runs:
    - Run 1: full DNS + TCP + TLS handshake
    - Run 2+: TCP CONNECT and TLS HANDSHAKE drop to <1ms (connection reused)
    This connection reuse is one of HTTP/2's main performance benefits.
    Use -F (force-dns) to get fresh connections and see the full handshake
    on every run rather than the reuse shortcut.

ANALYSIS, CHECKS, AND EXPORT
  --stats
    Print a percentile summary (min, p50, p90, p95, p99, max, mean, stdev)
    for every phase across the -c N runs. Shown only when at least 2
    requests succeeded, since percentiles need multiple samples; p95/p99
    only become meaningful once you have roughly 20 or more runs.

  Assertions (turn the probe into a pass/fail check)
    Setting any assertion makes the tool exit non-zero if ANY single
    request breaches, so it drops straight into CI, cron, and alerting:
      --assert-status CODE   require an exact HTTP status (e.g. 200)
      --max-total DUR        ceiling on TOTAL TIME       (DUR = 500ms, 1s, 1.5s)
      --max-ttfb DUR         ceiling on 1ST BYTE
      --max-dns / --max-tcp / --max-tls / --max-download DUR
                             ceilings on those individual phases
      --expect-body STR      body must contain the substring STR
      --expect-regex RE      body must match the regex RE
    Exit codes: 0 = all good, 1 = an assertion breached, 2 = bad arguments.

  --tls-info
    After the run, print the server certificate's subject, issuer, expiry
    date with days remaining (yellow under 30 days, red under 15), and
    Subject Alternative Names.

  --show-headers
    After the run, print selected response headers (server, content-type,
    caching headers, and so on) from the final response, plus a detected
    cache HIT/MISS verdict.

  --prometheus  (with --prometheus-port, --prometheus-bind)
    Run as a Prometheus exporter daemon instead of printing the table:
    serve metrics over HTTP (default port 9109, all interfaces) and re-probe
    the URL on every scrape, so Prometheus always pulls fresh numbers. Any
    GET path returns the metrics; the process blocks until Ctrl+C. Each
    scrape runs -c probes, so -c > 1 also exposes per-scrape total-time
    percentiles. Reports check_endpoint_up, per-phase *_seconds gauges for
    the last successful probe, response code and bytes, and (over HTTPS)
    check_endpoint_tls_expiry_days. Point Prometheus at it with a scrape job:
      scrape_configs:
        - job_name: check-endpoint
          static_configs:
            - targets: ["HOST:9109"]

EXAMPLES
  Basic single request:
      ./check-endpoint.py https://example.com

  10 requests, 5 second timeout:
      ./check-endpoint.py -c 10 -t 5 https://example.com

  Force IPv6, use a Chrome User-Agent:
      ./check-endpoint.py -6 -a chrome https://example.com

  Send a custom header:
      ./check-endpoint.py -H "Authorization: Bearer xyz123" https://example.com/api

  Send multiple headers:
      ./check-endpoint.py -H "X-Trace-Id: 42" -H "Accept: application/json" https://example.com

  POST a JSON body inline (implies POST automatically):
      ./check-endpoint.py -d '{{"foo":"bar"}}' -H "Content-Type: application/json" https://example.com/api

  POST a body read from a file (curl-style @file):
      ./check-endpoint.py -d @payload.json -H "Content-Type: application/json" https://example.com/api

  Force a specific method, e.g. PUT with no body:
      ./check-endpoint.py -X PUT https://example.com/api/resource/1

  Force a fresh DNS lookup + connection on every request (no reuse/caching):
      ./check-endpoint.py -c 10 -F https://example.com

  Pin all repeats to the IP first resolved (avoid round-robin drift):
      ./check-endpoint.py -c 10 -P https://example.com

  Pin all repeats to a specific known IP:
      ./check-endpoint.py -c 10 -p 93.184.216.34 https://example.com

  Test an SSE / chunked-streaming endpoint and see per-chunk cadence:
      ./check-endpoint.py -c 10 -S -H "Accept: text/event-stream" https://example.com/stream

  Percentile summary across 20 runs:
      ./check-endpoint.py -c 20 --stats https://example.com

  CI health check (nonzero exit if slow or not 200):
      ./check-endpoint.py --assert-status 200 --max-ttfb 300ms --max-total 1s https://example.com/health

  Validate the body and inspect the TLS certificate:
      ./check-endpoint.py --expect-body '"status":"ok"' --tls-info https://example.com/health

  Show response headers and cache status:
      ./check-endpoint.py --show-headers https://example.com

  Run a Prometheus exporter that re-probes on every scrape:
      ./check-endpoint.py --prometheus --prometheus-port 9109 https://example.com
      # then: curl localhost:9109   (Prometheus scrapes the same endpoint)

STREAMING RESPONSES (SSE / CHUNKED TRANSFER) - THE -S/--stream FLAG
  Without -S, a streaming response is still measured meaningfully:
  1ST BYTE is the time until the first chunk/token arrives (streaming
  start latency), and BODY DL is the total duration of the whole stream
  from first byte to last. What you don't get without -S is the *rhythm*
  of the stream - whether chunks arrive steadily or in bursts with stalls.

  -S records a timestamp for every chunk as it arrives (not just the
  first and last) and adds CHUNKS / AVG GAP / MAX GAP columns. These
  measure the gaps BETWEEN chunks only - the first chunk's arrival time
  is already covered by DNS/TCP/TLS/PRE-TRANSFER/1ST BYTE, so it isn't
  counted again here. A high MAX GAP relative to AVG GAP means the
  stream stalled somewhere in the middle even though the overall BODY DL
  time looked fine - useful for catching intermittent stutter that an
  aggregate-only view would hide. Fewer than 2 chunks means there's no
  inter-chunk gap to measure, so both columns show n/a.

NOTE ON -p/-P (IP pinning)
  When pinning, libcurl is told the IP directly and skips real DNS
  resolution for that hostname, so the DNS column will read ~0ms -- that's
  expected, not a bug. The Host header and TLS SNI sent on the wire are
  unaffected and still match the URL, so the target server still sees a
  normal request for that hostname.
""",
    )
    parser.add_argument("url", help="URL to test")
    parser.add_argument(
        "-c",
        "--count",
        type=int,
        default=1,
        help="number of requests to perform (default: 1)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=10.0,
        help="total per-request timeout in seconds (default: 10)",
    )

    ip_group = parser.add_mutually_exclusive_group()
    ip_group.add_argument(
        "-4", "--ipv4", action="store_true", help="force IPv4 resolution (default)"
    )
    ip_group.add_argument(
        "-6", "--ipv6", action="store_true", help="force IPv6 resolution"
    )

    http_group = parser.add_mutually_exclusive_group()
    http_group.add_argument(
        "--http2",
        action="store_true",
        help=(
            "request HTTP/2 via ALPN (requires HTTPS). "
            "Falls back to HTTP/1.1 if the server does not support HTTP/2. "
            "Requires libcurl built with nghttp2."
        ),
    )
    http_group.add_argument(
        "--http2-prior-knowledge",
        dest="http2_prior_knowledge",
        action="store_true",
        help=(
            "send HTTP/2 frames directly on a plain http:// connection (h2c, RFC 7540 §3.4). "
            "Only use when the server is known to speak HTTP/2 cleartext."
        ),
    )

    parser.add_argument(
        "-a",
        "--user-agent",
        dest="user_agent_alias",
        choices=sorted(USER_AGENTS.keys()),
        default=None,
        help=(
            "send a baked-in User-Agent string instead of the default "
            f"('{DEFAULT_USER_AGENT}'). choices: "
            + ", ".join(sorted(USER_AGENTS.keys()))
        ),
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        dest="headers",
        metavar="'Key: Value'",
        help="custom request header, curl-style (repeatable)",
    )
    parser.add_argument(
        "-d",
        "--data",
        default=None,
        help="request body, sent as POST (prefix with @ to read from a file)",
    )
    parser.add_argument(
        "-X",
        "--request",
        dest="method",
        default=None,
        help="force a specific HTTP method (e.g. PUT, DELETE)",
    )
    parser.add_argument(
        "-F",
        "--force-dns",
        action="store_true",
        help="force a fresh DNS lookup on every request",
    )
    parser.add_argument(
        "-p",
        "--pin-ip",
        default=None,
        metavar="IP",
        help="pin every request to this specific IP",
    )
    parser.add_argument(
        "-P",
        "--auto-pin",
        action="store_true",
        help="resolve once and pin all repeats to that IP",
    )
    parser.add_argument(
        "-S",
        "--stream",
        action="store_true",
        help=(
            "time every chunk as it arrives (not just first/last byte) and report "
            "extra CHUNKS / AVG GAP / MAX GAP columns - useful for testing SSE or "
            "chunked-transfer streaming responses"
        ),
    )

    # ── output / analysis ──────────────────────────────────────────────
    out_group = parser.add_argument_group("output and analysis")
    out_group.add_argument(
        "--stats",
        action="store_true",
        help="print a percentile summary (min/p50/p90/p95/p99/max/mean/stdev) "
        "across runs; needs -c 2 or more",
    )
    out_group.add_argument(
        "--tls-info",
        dest="tls_info",
        action="store_true",
        help="after the run, print TLS certificate details (subject, issuer, "
        "expiry with days remaining, SAN)",
    )
    out_group.add_argument(
        "--show-headers",
        dest="show_headers",
        action="store_true",
        help="after the run, print selected response headers and detected "
        "cache HIT/MISS",
    )
    out_group.add_argument(
        "--prometheus",
        action="store_true",
        help="run as a Prometheus exporter daemon: serve metrics over HTTP and "
        "re-probe the URL on every scrape (blocks until Ctrl+C)",
    )
    out_group.add_argument(
        "--prometheus-port",
        dest="prometheus_port",
        type=int,
        default=9109,
        metavar="PORT",
        help="port for the --prometheus exporter (default: 9109)",
    )
    out_group.add_argument(
        "--prometheus-bind",
        dest="prometheus_bind",
        default="",
        metavar="ADDR",
        help="bind address for the --prometheus exporter (default: all interfaces)",
    )

    # ── assertions / thresholds (exit non-zero on any breach) ──────────
    assert_group = parser.add_argument_group(
        "assertions",
        "Set any of these to turn the probe into a check: if ANY single request "
        "breaches, the program exits non-zero (for CI, cron, and alerting).",
    )
    assert_group.add_argument(
        "--assert-status",
        dest="assert_status",
        type=int,
        default=None,
        metavar="CODE",
        help="fail if the HTTP status is not CODE (e.g. 200)",
    )
    assert_group.add_argument(
        "--max-total",
        dest="max_total",
        default=None,
        metavar="DUR",
        help="fail if TOTAL TIME exceeds DUR (e.g. 500ms, 1s, 1.5s)",
    )
    assert_group.add_argument(
        "--max-ttfb",
        dest="max_ttfb",
        default=None,
        metavar="DUR",
        help="fail if 1ST BYTE (time to first byte) exceeds DUR",
    )
    assert_group.add_argument(
        "--max-dns",
        dest="max_dns",
        default=None,
        metavar="DUR",
        help="fail if the DNS phase exceeds DUR",
    )
    assert_group.add_argument(
        "--max-tcp",
        dest="max_tcp",
        default=None,
        metavar="DUR",
        help="fail if the TCP CONNECT phase exceeds DUR",
    )
    assert_group.add_argument(
        "--max-tls",
        dest="max_tls",
        default=None,
        metavar="DUR",
        help="fail if the TLS HANDSHAKE phase exceeds DUR",
    )
    assert_group.add_argument(
        "--max-download",
        dest="max_download",
        default=None,
        metavar="DUR",
        help="fail if BODY DL (body download) exceeds DUR",
    )
    assert_group.add_argument(
        "--expect-body",
        dest="expect_body",
        default=None,
        metavar="STR",
        help="fail if the response body does not contain the substring STR",
    )
    assert_group.add_argument(
        "--expect-regex",
        dest="expect_regex",
        default=None,
        metavar="RE",
        help="fail if the response body does not match the regex RE",
    )

    args = parser.parse_args()

    if args.stream:
        FIELDS.extend(STREAM_FIELDS)
        FINAL_FIELD_KEYS.extend(STREAM_FIELD_KEYS)

    ip_version = "6" if args.ipv6 else "4"
    set_ip_column_width(IPV6_IP_WIDTH if ip_version == "6" else IPV4_IP_WIDTH)

    user_agent = (
        USER_AGENTS[args.user_agent_alias]
        if args.user_agent_alias
        else DEFAULT_USER_AGENT
    )
    data = resolve_data_arg(args.data) if args.data is not None else None

    pin_resolve = None
    pinned_ip = None
    if args.pin_ip is not None:
        pin_resolve, pinned_ip, pinned_host = build_pin_resolve(
            args.url, args.pin_ip, ip_version
        )
        print(f"# pinned: {pinned_host} -> {pinned_ip}")
    elif args.auto_pin:
        pin_resolve, pinned_ip, pinned_host = build_pin_resolve(
            args.url, "auto", ip_version
        )
        print(f"# pinned: {pinned_host} -> {pinned_ip}")

    http_version = None
    if getattr(args, "http2", False):
        if not _HAS_HTTP2:
            sys.stderr.write(
                "error: --http2 requested but your libcurl was not built with nghttp2.\n\n"
                "Diagnose:\n"
                '  python3 -c "import pycurl; print(pycurl.version_info())"\n'
                "  curl --version | grep HTTP2\n\n"
                "Fix on macOS (Homebrew):\n"
                "  brew install curl nghttp2\n"
                "  pip uninstall pycurl\n"
                "  PYCURL_CURL_CONFIG=$(brew --prefix curl)/bin/curl-config \\\n"
                "    pip install --no-cache-dir --compile pycurl\n\n"
                "Fix on Linux (Debian/Ubuntu):\n"
                "  sudo apt install libcurl4-openssl-dev libnghttp2-dev\n"
                "  pip uninstall pycurl && pip install --no-cache-dir pycurl\n"
            )
            sys.exit(1)
        http_version = pycurl.CURL_HTTP_VERSION_2TLS
    elif getattr(args, "http2_prior_knowledge", False):
        if not _HAS_HTTP2:
            sys.stderr.write(
                "error: --http2-prior-knowledge requires libcurl built with nghttp2. See --http2 error for fix.\n"
            )
            sys.exit(1)
        http_version = pycurl.CURL_HTTP_VERSION_2_PRIOR_KNOWLEDGE

    # Build assertion config (only active if any assertion flag was set).
    def _dur_or_exit(val, flag):
        if val is None:
            return None
        try:
            return parse_duration(val)
        except ValueError:
            sys.stderr.write(f"error: invalid duration for {flag}: {val!r}\n")
            sys.exit(2)

    thresholds = {}
    for key, val, flag in [
        ("dns", args.max_dns, "--max-dns"),
        ("tcp", args.max_tcp, "--max-tcp"),
        ("tls", args.max_tls, "--max-tls"),
        ("ttfb", args.max_ttfb, "--max-ttfb"),
        ("download", args.max_download, "--max-download"),
        ("total", args.max_total, "--max-total"),
    ]:
        parsed = _dur_or_exit(val, flag)
        if parsed is not None:
            thresholds[key] = parsed

    expect_regex = None
    if args.expect_regex is not None:
        try:
            expect_regex = re.compile(args.expect_regex)
        except re.error as exc:
            sys.stderr.write(f"error: invalid --expect-regex: {exc}\n")
            sys.exit(2)

    assertions_active = (
        args.assert_status is not None
        or bool(thresholds)
        or args.expect_body is not None
        or expect_regex is not None
    )
    assert_cfg = (
        {
            "status": args.assert_status,
            "thresholds": thresholds,
            "expect_body": args.expect_body,
            "expect_regex": expect_regex,
        }
        if assertions_active
        else None
    )

    capture_body = args.expect_body is not None or expect_regex is not None
    capture_headers = args.show_headers
    capture_cert = args.tls_info

    def run_probe_cycle(quiet, want_cert):
        """Run args.count probes and return (results, first captured cert)."""
        collected = []
        for i in range(1, args.count + 1):
            res = run_once(
                i,
                args.url,
                args.timeout,
                ip_version=ip_version,
                user_agent=user_agent,
                headers=args.headers,
                data=data,
                method=args.method,
                force_dns=args.force_dns,
                resolve=pin_resolve,
                http_version=http_version,
                stream_mode=args.stream,
                pin_ip=pinned_ip,
                quiet=quiet,
                capture_body=capture_body,
                capture_headers=capture_headers,
                capture_cert=want_cert,
            )
            if assert_cfg is not None:
                res["_assert_fails"] = evaluate_assertions(res, assert_cfg)
            collected.append(res)
        first_cert = next((r["cert"] for r in collected if r.get("cert")), None)
        return collected, first_cert

    # ── Prometheus exporter daemon: re-probe on every scrape ───────────
    if args.prometheus:
        serve_prometheus(
            args.prometheus_bind,
            args.prometheus_port,
            args.url,
            lambda: run_probe_cycle(quiet=True, want_cert=True),
        )
        return

    print_header()
    results, cert = run_probe_cycle(quiet=False, want_cert=capture_cert)

    if args.stats:
        print_summary(results)
    if args.tls_info:
        print_tls_info(cert)
    if args.show_headers:
        print_headers_block(results)
    if assert_cfg is not None:
        end = RESET if USE_COLOR else ""
        failed_runs = [r for r in results if r["_assert_fails"]]
        n = len(results)
        if not failed_runs:
            sys.stdout.write(
                "\n"
                + _col(BOLD + _GREEN)
                + f"ASSERTIONS: PASSED ({n} run{'s' if n != 1 else ''})"
                + end
                + "\n"
            )
        else:
            sys.stdout.write(
                "\n"
                + _col(BOLD + _RED)
                + f"ASSERTIONS: FAILED ({len(failed_runs)}/{n} runs)"
                + end
                + "\n"
            )
            for r in failed_runs:
                prefix = _col(_MAROON) + f"  run {r['run']}:" + end
                colored = "; ".join(_colorize_reason(x) for x in r["_assert_fails"])
                sys.stdout.write(f"{prefix} {colored}\n")
        sys.stdout.flush()

    # Strict exit code: any single breaching run fails the whole invocation.
    if assert_cfg is not None and any(r["_assert_fails"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()

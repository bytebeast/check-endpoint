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
import os
import re
import shlex
import socket
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

__author__ = "github.com/bytebeast"

# datetime.UTC is an alias for datetime.timezone.utc that was only added in
# Python 3.11. Importing it unconditionally makes the whole script fail on 3.9
# and 3.10 with "ImportError: cannot import name 'UTC' from 'datetime'", so
# fall back to the older spelling when it is missing.
try:
    from datetime import UTC
except ImportError:  # Python < 3.11
    from datetime import timezone

    UTC = timezone.utc

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


APP_VERSION = "2.7.2"
DEFAULT_USER_AGENT = f"check-endpoint/{APP_VERSION}"

# Sent on every request unless the caller supplies their own Accept header
# with -H. This matches what curl(1) sends by default; setting it explicitly
# means the probe's request looks the same regardless of how libcurl was
# built or configured.
DEFAULT_ACCEPT = "*/*"

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
    # What the curl(1) command line tool itself sends. Use this when you want
    # the request to be indistinguishable from a plain `curl` invocation, e.g.
    # when a WAF or CDN treats unknown agents differently.
    "curl": "curl/8.8.0",
    # The previous value of the "curl" alias, kept under its own name so
    # existing scripts can still reach it.
    "pycurl": "pycurl/8.8.0",
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
    ["ip", "IP_ADDRESS", 16],
    ["dns", "DNS", 9],
    ["tcp", "TCP_CONNECT", 13],
    ["tls", "TLS_HANDSHAKE", 15],
    ["pretransfer", "PRE-TRANSFER", 14],
    ["ttfb", "1ST_BYTE", 10],
    ["redirect", "REDIRECT", 13],
    ["download", "BODY_DL", 10],
    ["total", "TOTAL_TIME", 12],
    ["code", "HTTP_CODE", 11],
    ["bytes", "TOTAL_BYTES", 13],
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
    ["avggap", "AVG_GAP", 10],
    ["maxgap", "MAX_GAP", 10],
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


def _live_phase_already_passed(curl, key):
    """
    True if this live phase's window has definitely closed even though its
    OWN timer is still 0 - i.e. the phase structurally does not apply to
    this connection (the only current case: TLS HANDSHAKE on a plain
    http:// URL, which never sets APPCONNECT_TIME) - as opposed to merely
    "not reached yet".

    Detected by checking whether any LATER LIVE_FIELD_KEYS timer has
    already been set: PRETRANSFER_TIME/STARTTRANSFER_TIME can only be set
    once curl has moved past the TLS phase, whether or not TLS actually
    happened, so a later timer being live is proof this one legitimately
    never will be.

    Without this check, the live-printing loop in run_once() blocks on
    RAW_TIME_GETTERS[key](curl) forever for a structurally-n/a phase, so
    `pointer` gets stuck there for the rest of the request. That is
    harmless if the request goes on to succeed (the success path force-
    advances pointer past it), but if the request instead FAILS later -
    e.g. a timeout mid BODY_DL on a plain http:// URL - the failure
    marker ends up mis-attributed to TLS_HANDSHAKE instead of the phase
    that actually failed, because pointer never left it.
    """
    idx = LIVE_FIELD_KEYS.index(key)
    return any(
        RAW_TIME_GETTERS[later](curl) > 0 for later in LIVE_FIELD_KEYS[idx + 1 :]
    )


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


# ── request headers ───────────────────────────────────────────────────────────


def header_field_name(header):
    """
    The field name of a curl-style "Key: Value" header, lowercased.

    Splitting on the first colon only, so a value containing colons (a URL in
    Referer, say) does not confuse the name.
    """
    return header.split(":", 1)[0].strip().lower()


def build_request_headers(headers):
    """
    Return the header list to hand to libcurl, with defaults filled in.

    Adds "Accept: */*" unless an Accept header is already present. The match is
    on the field name alone, so -H "Accept-Encoding: gzip" and
    -H "Accept-Language: en" are NOT treated as overriding Accept.

    Passing -H "Accept:" with an empty value is curl's idiom for suppressing a
    header entirely; that counts as an override too, so the default is not
    added back underneath it.
    """
    headers = list(headers or [])
    if not any(header_field_name(h) == "accept" for h in headers):
        # Prepended so explicit -H values still read last in --show-headers.
        headers.insert(0, f"Accept: {DEFAULT_ACCEPT}")
    return headers


# ── cookies ────────────────────────────────────────────────────────────────


def resolve_cookie_arg(raw):
    """
    curl-style -b/--cookie argument. Two forms, exactly like curl -b:

      -b "name=value; name2=value2"   literal Cookie data to send
      -b cookies.txt                  a filename to read cookies from
                                       (Netscape jar format, or raw
                                       Set-Cookie header lines)

    curl's own rule for telling them apart is simply: if the argument
    contains a '=' character, it's literal cookie data; otherwise it's a
    filename. Returns (literal_or_None, filename_or_None).
    """
    if raw is None:
        return None, None
    if "=" in raw:
        return raw, None
    return None, raw


NETSCAPE_HTTPONLY_PREFIX = "#HttpOnly_"


def parse_cookielist_line(line):
    """
    Parse one line from CURLINFO_COOKIELIST: Netscape cookie-file format
    (domain, include-subdomains flag, path, secure flag, expiry as a unix
    timestamp where 0 means "session cookie", name, value - tab separated).

    libcurl marks HttpOnly cookies by prefixing the domain field with
    '#HttpOnly_' rather than adding an eighth column, so that has to be
    stripped off and tracked separately. Plain '#'-comment lines (jar file
    headers) are skipped. Returns None for anything that isn't a real
    cookie line.
    """
    http_only = False
    if line.startswith(NETSCAPE_HTTPONLY_PREFIX):
        http_only = True
        line = line[len(NETSCAPE_HTTPONLY_PREFIX) :]
    elif line.startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) != 7:
        return None
    domain, include_sub, path, secure, expiry, name, value = parts
    try:
        expiry_i = int(expiry)
    except ValueError:
        expiry_i = 0
    return {
        "domain": domain,
        "include_subdomains": include_sub == "TRUE",
        "path": path,
        "secure": secure == "TRUE",
        "expiry": expiry_i,
        "http_only": http_only,
        "name": name,
        "value": value,
    }


def get_cookie_jar(curl):
    """
    Structured cookies currently held by curl's cookie engine (populated
    once the engine has been turned on via COOKIE / COOKIEFILE / SHARE).
    Safe to call even when the engine was never enabled - libcurl just
    returns an empty list rather than raising.
    """
    try:
        raw_lines = curl.getinfo(pycurl.INFO_COOKIELIST)
    except Exception:
        return []
    cookies = []
    for line in raw_lines or []:
        parsed = parse_cookielist_line(line)
        if parsed is not None:
            cookies.append(parsed)
    return cookies


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
    insecure=False,
    cacert=None,
    cookie_literal=None,
    cookie_file=None,
    cookie_jar=None,
    cookie_share=None,
    capture_cookies=False,
    body_limit=BODY_CAPTURE_LIMIT,
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
        # HOT PATH - runs for every chunk while libcurl's phase timers are
        # still live. Everything here has to stay O(1)-ish and syscall-free,
        # or the cost lands in BODY_DL/TOTAL_TIME and the tool ends up
        # measuring itself. In particular: no disk I/O, no formatting, no
        # timestamps beyond the -S counter that is the point of -S.
        if stream_mode:
            chunk_times.append(time.perf_counter())
        if capture_body:
            buffered = len(body_buf)
            if buffered < body_limit:
                room = body_limit - buffered
                # Slicing copies. Only pay for it on the one chunk that
                # straddles the limit; every earlier chunk extends directly.
                body_buf.extend(chunk if len(chunk) <= room else chunk[:room])
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
    # TLS verification. Default is full verification; -k drops it entirely and
    # --cacert keeps it but points at a different trust store.
    #
    # SSL_VERIFYHOST takes 0 or 2, never 1. Old libcurl treated 1 as "check the
    # name but only warn"; since 7.28.1 passing 1 is a hard error
    # (CURLE_BAD_FUNCTION_ARGUMENT), so the disabled case must use 0.
    if insecure:
        curl.setopt(curl.SSL_VERIFYPEER, 0)
        curl.setopt(curl.SSL_VERIFYHOST, 0)
    else:
        curl.setopt(curl.SSL_VERIFYPEER, 1)
        curl.setopt(curl.SSL_VERIFYHOST, 2)
        if cacert:
            curl.setopt(curl.CAINFO, cacert)
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

    # cookies_wanted is true if the caller asked for cookie handling in any
    # form: sending some (-b), collecting a jar to write out (-j), or just
    # displaying whatever the server sets (--show-cookies). Any of those
    # needs the cookie ENGINE turned on, not just a literal Cookie header -
    # COOKIEFILE "" does that without loading a file. cookie_share (a
    # pycurl.CurlShare with LOCK_DATA_COOKIE) is what lets cookies persist
    # across separate run_once() calls / curl handles for -c N > 1, the same
    # way a real curl session persists them across separate invocations via
    # a shared cookie jar file.
    cookies_wanted = capture_cookies or bool(
        cookie_literal or cookie_file or cookie_jar
    )
    if cookie_share is not None:
        curl.setopt(curl.SHARE, cookie_share)
    if cookie_file:
        curl.setopt(curl.COOKIEFILE, cookie_file)
    elif cookies_wanted:
        curl.setopt(curl.COOKIEFILE, "")
    if cookie_literal:
        curl.setopt(curl.COOKIE, cookie_literal)
    if cookie_jar:
        curl.setopt(curl.COOKIEJAR, cookie_jar)
        # Normally COOKIEJAR is written automatically when the easy handle
        # is cleaned up, but that auto-save does NOT fire when the cookie
        # store is external to the handle (cookie_share, above) - the
        # handle no longer "owns" what it would be saving at close time.
        # An explicit FLUSH after perform() sidesteps that: it writes the
        # jar immediately regardless of sharing, so this is done
        # unconditionally rather than only when cookie_share is set.

    # Always set, because build_request_headers() supplies a default Accept
    # even when the caller passed no -H flags at all.
    curl.setopt(curl.HTTPHEADER, build_request_headers(headers))

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
                    key = LIVE_FIELD_KEYS[pointer]
                    printed, prev_time = try_print_live_field(
                        curl, key, prev_time, row_col=rcol
                    )
                    if printed:
                        pointer += 1
                        continue
                    # Not printed because raw <= 0 - either the phase
                    # hasn't happened yet (keep waiting) or it structurally
                    # never will (e.g. TLS on http://). Only the latter
                    # should advance the pointer; see
                    # _live_phase_already_passed for why this matters for
                    # failure-marker placement.
                    if _live_phase_already_passed(curl, key):
                        write_empty_cell(key, field_width(key))
                        pointer += 1
                        continue
                    break

            if num_active == 0:
                break

            multi.select(0.001)

        num_q, ok_list, err_list = multi.info_read()
        for _handle, errno, _errmsg in err_list:
            failed = True
            fail_errno = errno

    except pycurl.error as exc:
        failed = True
        fail_errno = exc.args[0] if exc.args else None

    finally:
        multi.remove_handle(curl)
        multi.close()

    if cookie_jar:
        # See the setup comment above COOKIEJAR: with a shared cookie store
        # the implicit write-on-cleanup doesn't fire, so force it here.
        try:
            curl.setopt(pycurl.COOKIELIST, "FLUSH")
        except pycurl.error:
            pass

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
        # Carried into --json / --prometheus so a run made without certificate
        # verification is never mistaken for a clean one. With -k the whole
        # <TLS-FAIL> family (E_PEER_FAILED_VERIFICATION, E_SSL_CACERT,
        # E_SSL_CERTPROBLEM) can no longer fire, so a table from a broken
        # endpoint looks identical to one from a healthy endpoint.
        "insecure": insecure,
        "cookies": get_cookie_jar(curl) if cookies_wanted else None,
    }

    if failed:
        res["marker"] = marker_for_errno(fail_errno)
        # FIX: previously, if pointer already reached len(LIVE_FIELD_KEYS) -
        # i.e. DNS through 1ST_BYTE had ALL already printed real values, and
        # the failure (e.g. a timeout) only happened afterward, during
        # redirect-following or while the body was downloading - the code
        # fell straight to blanking every FINAL_FIELD_KEYS cell with
        # write_empty_cell and never wrote the marker anywhere. That made a
        # genuine failure (e.g. <TO> mid-download) look identical to a
        # normal empty/n-a cell, with no visible sign anything had gone
        # wrong. res["marker"] was always set correctly for callers (e.g.
        # assertions), so only the live table display was affected.
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
                        LIVE_FIELD_KEYS[pointer],
                        field_width(LIVE_FIELD_KEYS[pointer]),
                    )
                    pointer += 1
                for key in FINAL_FIELD_KEYS:
                    write_empty_cell(key, field_width(key))
            else:
                # All live phases already completed - the failure happened
                # later. E_TOO_MANY_REDIRECTS is a REDIRECT-phase failure
                # (curl gave up after completing the live phases for the
                # last hop it followed); everything else (timeout, send/recv
                # error, no-data) happened during body download, since any
                # redirects are already resolved by the time STARTTRANSFER_
                # TIME/1ST_BYTE is set. Put the marker on whichever column
                # matches and blank the rest, so the failure is visible
                # instead of silently dashed out.
                marker_key = (
                    "redirect"
                    if fail_errno == pycurl.E_TOO_MANY_REDIRECTS
                    else "download"
                )
                for key in FINAL_FIELD_KEYS:
                    if key == marker_key:
                        write_cell(res["marker"], field_width(key), color=_col(C_ERROR))
                    else:
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
    ("tcp", "TCP_CONNECT"),
    ("tls", "TLS_HANDSHAKE"),
    ("pretransfer", "PRE-TRANSFER"),
    ("ttfb", "1ST_BYTE"),
    ("download", "BODY_DL"),
    ("total", "TOTAL_TIME"),
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
    stat_row("TOTAL_BYTES", [r["bytes"] for r in ok], human_bytes, _cell_bytes)
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


def _cookie_expiry_note(expiry_unix):
    """(color, text) describing when a cookie expires, colored the same way
    as --tls-info's certificate countdown: green healthy, yellow close,
    peach near, red already expired. 0 means a session cookie - it has no
    fixed expiry at all, so it gets a neutral note instead of a countdown."""
    if not expiry_unix:
        return _col(_OVERLAY0), "session (cleared when the client closes)"
    when = datetime.fromtimestamp(expiry_unix, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    days_left = (expiry_unix - time.time()) / 86400.0
    if days_left < 0:
        return _col(BOLD + _RED), f"EXPIRED {when}"
    if days_left < 1:
        return _col(_PEACH), f"{when}  (expires today)"
    if days_left < 7:
        return _col(_YELLOW), f"{when}  ({days_left:.0f} days left)"
    return _col(_GREEN), f"{when}  ({days_left:.0f} days left)"


def print_cookies_block(cookies):
    """End-of-run block, styled like print_tls_info: every cookie currently
    held by the (possibly shared, possibly jar-backed) cookie engine after
    all -c N runs - not just what the final request happened to receive."""
    end = RESET if USE_COLOR else ""
    sys.stdout.write("\n" + _col(C_HEADER) + "COOKIES" + end + "\n")
    if not cookies:
        sys.stdout.write(
            _col(C_LINENUM) + "  (no cookies sent or received)" + end + "\n"
        )
        sys.stdout.flush()
        return

    for c in cookies:
        name_val = (
            _col(_LAVENDER) + f"  {c['name']}=" + end + _col(_TEXT) + c["value"] + end
        )
        sys.stdout.write(name_val + "\n")

        flags = []
        if c["secure"]:
            flags.append("secure")
        if c["http_only"]:
            flags.append("httponly")
        if c["include_subdomains"]:
            flags.append("includes subdomains")
        flag_txt = f"   [{', '.join(flags)}]" if flags else ""

        scope_key = _col(_BLUE) + "    domain/path:" + end
        scope_val = _col(_SUBTEXT0) + f"{c['domain']}{c['path']}" + end
        scope_flags = _col(_OVERLAY0) + flag_txt + end
        sys.stdout.write(f"{scope_key} {scope_val}{scope_flags}\n")

        exp_key = _col(_BLUE) + "    expires:" + end
        exp_color, exp_text = _cookie_expiry_note(c["expiry"])
        exp_body = (exp_color + exp_text + RESET) if USE_COLOR else exp_text
        sys.stdout.write(f"{exp_key} {exp_body}\n")
    sys.stdout.flush()


# Headers that hint at WHICH server / edge / CDN / backend produced the
# response. Grouped loosely by source. Many are per-request identifiers
# (cf-ray, x-amz-cf-id, *-request-id): those change every request even from a
# single backend, so the summary treats "all unique" differently from "varied
# between a few distinct values" (which is the real which-backend signal).
SERVER_HINT_HEADERS = [
    # Generic origin / framework identity
    "server",
    "x-powered-by",
    "x-aspnet-version",
    "x-aspnetmvc-version",
    # Proxy / cache chain
    "via",
    "x-served-by",
    "x-cache",
    "x-cache-hits",
    "x-cache-status",
    "age",
    # Which specific backend / node / pod answered
    "x-backend",
    "x-backend-server",
    "x-server",
    "x-server-name",
    "x-host",
    "x-node",
    "x-instance",
    "x-instance-id",
    "x-upstream",
    "x-envoy-upstream-service-time",
    # Per-request / trace IDs (usually unique per request)
    "x-request-id",
    "x-amzn-requestid",
    "x-amzn-trace-id",
    "x-vcap-request-id",
    "x-github-request-id",
    # Cloudflare
    "cf-ray",
    "cf-cache-status",
    "cf-worker",
    # AWS CloudFront
    "x-amz-cf-id",
    "x-amz-cf-pop",
    # Fastly / Akamai / other CDNs (x-served-by already listed above)
    "x-timer",
    "x-fastly-request-id",
    "akamai-grn",
    "x-akamai-transformed",
    # PaaS providers
    "fly-request-id",
    "x-render-origin-server",
    "x-vercel-id",
    "x-vercel-cache",
    "x-nf-request-id",  # Netlify
]


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


# ── request provenance (who served each request) ──────────────────────────────
#
# Unlike print_headers_block (which shows only the FINAL response's curated
# headers), this walks EVERY request and reports the server-identifying headers
# per run, so across -c N you can see which edge/backend answered each one.
# It shows:
#   * one row per successful request: run #, IP, and each header as key=value
#   * a rollup per header classifying it as:
#       constant   - same value on every run (e.g. server=cloudflare)
#       varied     - a few distinct values (the real "different backend" signal,
#                    e.g. x-cache = HIT×6 / MISS×4)
#       per-request- a different value every run (trace/request IDs like cf-ray)


def _provenance_keys(results, want_hints, user_keys):
    """Ordered list of header names to display: the server-hint headers that
    actually showed up (only if --server-hints), followed by any user-requested
    --capture-header names (always kept, even if absent, so their absence is
    visible)."""
    present = set()
    for r in results:
        h = r.get("headers")
        if h:
            present.update(h.keys())
    keys = []
    if want_hints:
        for k in SERVER_HINT_HEADERS:
            if k in present and k not in keys:
                keys.append(k)
    for k in user_keys:
        if k not in keys:
            keys.append(k)
    return keys


# CDN / cache headers whose value is a comma-separated CHAIN of hops, oldest
# (origin-shield) first, newest (the edge that actually served you) last. BY
# DEFAULT we report only that final hop, which is the one you care about;
# pass --full-cdn to show the entire raw chain instead.
CDN_CHAINED_HEADERS = {
    "x-served-by",
    "x-cache",
    "x-cache-hits",
    "x-cache-status",
    "via",
}


def _final_hop(value):
    """Last segment of a comma-separated hop chain, trimmed."""
    return value.split(",")[-1].strip()


def _hop_count(value):
    return len([p for p in value.split(",")]) if value else 0


def _kv(key, value, missing=False):
    """Render one key=value token with Catppuccin coloring."""
    if missing or value is None:
        return _col(C_LINENUM) + f"{key}=-" + (RESET if USE_COLOR else "")
    end = RESET if USE_COLOR else ""
    return _col(_LAVENDER) + key + "=" + end + _col(_SUBTEXT0) + value + end


def print_provenance_summary(results, want_hints, user_keys, full_cdn=False):
    end = RESET if USE_COLOR else ""
    user_keys = [k.strip().lower() for k in user_keys]
    title = "REQUEST PROVENANCE (server-identifying headers, per request)"
    if full_cdn:
        title += "  [--full-cdn: full hop chain]"
    sys.stdout.write("\n" + _col(C_HEADER) + title + end + "\n")

    keys = _provenance_keys(results, want_hints, user_keys)
    ok = [r for r in results if not r["failed"] and r.get("headers")]

    if not keys:
        sys.stdout.write(
            _col(C_LINENUM)
            + "  (no server-identifying headers found in any response)"
            + end
            + "\n"
        )
        sys.stdout.flush()
        return
    if not ok:
        sys.stdout.write(
            _col(C_LINENUM)
            + "  (no headers captured - did every request fail?)"
            + end
            + "\n"
        )
        sys.stdout.flush()
        return

    # By DEFAULT chained CDN headers are collapsed to their final hop; only
    # --full-cdn shows the whole comma-separated chain.
    def _collapse(k, v):
        # fmt off
        return not full_cdn and k in CDN_CHAINED_HEADERS and v is not None and "," in v
        # fmt on

    ip_w = field_width("ip")
    # Per-request rows.
    for r in ok:
        h = r["headers"]
        toks = []
        for k in keys:
            v = h.get(k)
            if _collapse(k, v):
                toks.append(_kv(f"{k}(final)", _final_hop(v)))
            else:
                toks.append(_kv(k, v))
        runlbl = _col(C_LINENUM) + f"  {r['run']:<3}" + end
        iplbl = _col(C_IP) + f"{(r.get('ip') or '-'):<{ip_w}}" + end
        sys.stdout.write(f"{runlbl} {iplbl}  {'  '.join(toks)}\n")

    # Rollup: classify each header as constant / varied / per-request.
    sys.stdout.write("\n")
    n = len(ok)
    any_collapsed = False
    for k in keys:
        raw_vals = [r["headers"].get(k) for r in ok]
        # By default collapse chained headers to their final hop and note the
        # chain depth; with --full-cdn use the raw values verbatim.
        max_hops = max((_hop_count(v) for v in raw_vals if v), default=0)
        collapse = (not full_cdn) and k in CDN_CHAINED_HEADERS and max_hops > 1
        if collapse:
            any_collapsed = True
            vals = [(_final_hop(v) if v else "-") for v in raw_vals]
            label_key = f"{k}(final)"
            suffix = _col(_OVERLAY0) + f"   [{max_hops} hops in chain]" + end
        else:
            vals = [(v or "-") for v in raw_vals]
            label_key = k
            suffix = ""

        counts = Counter(vals)
        distinct = len(counts)
        if distinct == 1:
            only = next(iter(counts))
            label = _col(_OVERLAY0) + f"  constant     {label_key}: " + end
            body = _col(_SUBTEXT0) + only + end
        elif distinct == n and n > 1:
            label = _col(_MAUVE) + f"  per-request  {label_key}: " + end
            body = (
                _col(_TEXT)
                + f"{distinct} distinct (unique each run - looks like a request/trace id)"
                + end
            )
        else:
            label = _col(BOLD + _PEACH) + f"  varied       {label_key}: " + end
            top = ", ".join(f"{val}×{cnt}" for val, cnt in counts.most_common(6))
            more = "" if distinct <= 6 else f", +{distinct - 6} more"
            body = (
                _col(_TEXT)
                + f"{distinct} distinct "
                + end
                + _col(_SUBTEXT0)
                + f"({top}{more})"
                + end
            )
        sys.stdout.write(label + body + suffix + "\n")

    # Let the user know the chain was trimmed and how to see all of it.
    if any_collapsed:
        sys.stdout.write(
            _col(C_LINENUM)
            + "  (CDN hop chains shown as final hop; pass --full-cdn for the full chain)"
            + end
            + "\n"
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
    # Without this, a -k exporter reports a healthy endpoint indistinguishably
    # from a verified one, and check_endpoint_tls_expiry_days below describes a
    # certificate that was never validated. Alert on it being 1 if you don't
    # expect it.
    emit(
        "check_endpoint_tls_verification_disabled",
        "1 if probes ran with -k/--insecure (certificate not verified), else 0",
        1 if any(r.get("insecure") for r in results) else 0,
    )

    if last is not None:
        emit(
            "check_endpoint_http_response_code",
            "HTTP status code of the last successful probe",
            last["code"],
        )
        phase_metrics = [
            ("dns", "check_endpoint_dns_seconds", "DNS lookup time (seconds)"),
            (
                "tcp",
                "check_endpoint_tcp_connect_seconds",
                "TCP connect time (seconds)",
            ),
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
            (
                "total",
                "check_endpoint_total_seconds",
                "Total request time (seconds)",
            ),
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
            f"check-endpoint: scrape from {self.address_string()} - {fmt % args}\n"
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


# ── output capture ────────────────────────────────────────────────────────────
#
# Writes the full response (status, headers, body) plus this tool's own
# measurements to disk, one file per recorded run, so a failure seen in the
# table can be opened and read afterwards instead of re-run and hoped for.
#
# LATENCY SAFETY - the whole point of this tool is the timing numbers, so
# capture is built so it cannot move them:
#
#   1. The capture directory and command-statement.out are created ONCE at
#      startup, before the first request. No mkdir/open ever happens between
#      a request starting and its timers being read.
#   2. Nothing is written to disk while a transfer is in flight. The only
#      work done during a transfer is appending bytes to an in-memory
#      bytearray in _write_cb (see the HOT PATH note there), which is what
#      --expect-body/--expect-regex already did.
#   3. Run files are written after run_once() has returned - i.e. after
#      libcurl has stopped the clock and every phase timer has been read off
#      the handle. Those values are frozen numbers in a dict by then; no
#      amount of subsequent I/O can change them.
#   4. Body buffering is capped (--capture-body-limit, default 256 KiB).
#      Past the cap the callback does one integer comparison and returns, so
#      capturing a 2 GB download does not turn into a 2 GB memcpy that would
#      show up as inflated BODY_DL.
#
# The residual effect is that writing a file delays the NEXT request by the
# cost of that write. That shifts when run N+1 starts; it does not touch what
# run N+1 measures, since each run's phases are timed by libcurl internally
# from its own start. Deliberately preferred over buffering every run in
# memory and flushing at the end, which would make -c 10000 a memory problem.

CAPTURE_MODES = ("never", "all", "failed", "assert", "error")

CAPTURE_BODY_LIMIT_DEFAULT = 256 * 1024  # 256 KiB

# Argument values that get redacted in command-statement.out. This file is
# written into the working directory and is meant to be attached to tickets
# and handed to other people - the same way this tool's table output already
# is - so a bearer token baked into the reproduction command is a real leak,
# not a hypothetical one. --capture-secrets turns redaction off for the case
# where the capture is staying local and the exact command matters more.
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "auth-token",
    "x-access-token",
    "access-token",
    "x-session-token",
    "x-csrf-token",
    "x-amz-security-token",
    "private-token",
}

REDACTED = "<redacted>"


def parse_size(s):
    """Parse a byte size: '512', '256K', '1M', '2MB', '1G'. Raises ValueError
    on anything else. Used by --capture-body-limit."""
    txt = str(s).strip().upper().replace("IB", "B")
    mult = 1
    for suffix, factor in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if txt.endswith(suffix):
            txt, mult = txt[: -len(suffix)], factor
            break
    else:
        for suffix, factor in (("K", 1024), ("M", 1024**2), ("G", 1024**3)):
            if txt.endswith(suffix):
                txt, mult = txt[: -len(suffix)], factor
                break
        else:
            if txt.endswith("B"):
                txt = txt[:-1]
    value = float(txt)
    if value < 0:
        raise ValueError("size cannot be negative")
    return int(value * mult)


def _redact_header_arg(value):
    """Redact the value half of a 'Name: secret' header argument, keeping the
    header name visible so the command still reads correctly."""
    name, sep, _ = value.partition(":")
    if sep and name.strip().lower() in SENSITIVE_HEADER_NAMES:
        return f"{name}: {REDACTED}"
    return value


def build_command_statement(argv, redact=True):
    """Rebuild the invocation as a copy-pasteable, shell-quoted command.

    Reconstructed from sys.argv rather than echoed raw, so the result is
    correctly quoted even when the original shell did the quoting - and so
    secrets can be filtered on the way through."""
    if not argv:
        return ""
    parts = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None

        if not redact:
            parts.append(arg)
            i += 1
            continue

        # Split forms: -H "Authorization: Bearer x" / --header "..."
        if arg in ("-H", "--header") and nxt is not None:
            parts.extend([arg, _redact_header_arg(nxt)])
            i += 2
            continue
        # Joined forms: -H"Authorization: ..." and --header=...
        if arg.startswith("--header="):
            parts.append("--header=" + _redact_header_arg(arg[len("--header=") :]))
            i += 1
            continue
        if arg.startswith("-H") and len(arg) > 2:
            parts.append("-H" + _redact_header_arg(arg[2:]))
            i += 1
            continue

        # Literal cookie data (a filename has no '=' and stays visible, since
        # the path is useful and the secret lives in the file, not the arg).
        if arg in ("-b", "--cookie") and nxt is not None:
            parts.extend([arg, REDACTED if "=" in nxt else nxt])
            i += 2
            continue
        if arg.startswith("--cookie=") and "=" in arg[len("--cookie=") :]:
            parts.append("--cookie=" + REDACTED)
            i += 1
            continue

        parts.append(arg)
        i += 1

    return " ".join(shlex.quote(p) for p in parts)


class CaptureWriter:
    """Owns the capture directory and decides which runs get recorded.

    Directory layout, one directory per invocation:

        {YYYYMMDDHHMMSS}-{pid}/command-statement.out
        {YYYYMMDDHHMMSS}-{pid}/{run}.out

    The timestamp is when the command was kicked off, not when each file was
    written, so every file from one invocation lands together. Run files are
    named for the run number exactly as it appears in the table's # column,
    so a failing row maps to its file with no arithmetic.
    """

    def __init__(
        self,
        mode,
        base_dir,
        url,
        argv,
        started,
        body_limit=CAPTURE_BODY_LIMIT_DEFAULT,
        want_body=True,
        redact=True,
        count=1,
    ):
        self.mode = mode or "never"
        self.base_dir = base_dir
        self.url = url
        self.argv = argv
        self.started = started
        self.body_limit = body_limit
        self.want_body = want_body
        self.redact = redact
        self.count = count
        self.dir = None
        self.written = []

    @property
    def active(self):
        return self.mode != "never"

    def open(self):
        """Create the capture directory and write command-statement.out.

        Called once before the first probe: it fails fast on an unwritable
        path (rather than after a 20-minute run), and gets every mkdir/open
        out of the way before any timing starts."""
        if not self.active:
            return
        stamp = self.started.strftime("%Y%m%d%H%M%S")
        self.dir = os.path.join(self.base_dir, f"{stamp}-{os.getpid()}")
        os.makedirs(self.dir, exist_ok=True)
        self._write_command_statement()

    def _write_command_statement(self):
        try:
            libcurl_ver = pycurl.version
        except Exception:
            libcurl_ver = "unknown"
        py_ver = "%d.%d.%d" % sys.version_info[:3]

        lines = [
            "# check-endpoint capture session",
            "",
            f"started-local:   {self.started.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}",
            f"started-utc:     {self.started.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"pid:             {os.getpid()}",
            f"host:            {socket.gethostname()}",
            f"cwd:             {os.getcwd()}",
            f"capture-dir:     {os.path.abspath(self.dir)}",
            f"capture-on:      {self.mode}",
            f"capture-body:    {'yes' if self.want_body else 'no'}"
            + (f" (limit {human_bytes(self.body_limit)})" if self.want_body else ""),
            f"url:             {self.url}",
            f"runs-requested:  {self.count}",
            f"version:         check-endpoint/{APP_VERSION}",
            f"python:          {py_ver}",
            f"libcurl:         {libcurl_ver}",
            "",
            "# command statement",
            "",
            build_command_statement(self.argv, redact=self.redact),
            "",
        ]
        if self.redact:
            lines += [
                "# note: values of sensitive arguments (Authorization and similar",
                f"#       headers, literal -b cookie data) are shown as '{REDACTED}'.",
                "#       Re-run with --capture-secrets to record them verbatim.",
                "",
            ]
        with open(
            os.path.join(self.dir, "command-statement.out"), "w", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines))

    def should_capture(self, res):
        """Whether this run's result matches the --capture-on mode.

        Note 'assert' and 'failed' can only be evaluated after the request is
        done, which is why the body/headers are buffered on every run
        regardless of mode - you cannot retroactively capture a response you
        chose not to keep. Only the writing is conditional."""
        if not self.active:
            return False
        if self.mode == "all":
            return True
        failed = bool(res.get("failed"))
        asserted = bool(res.get("_assert_fails"))
        if self.mode == "error":
            return failed
        if self.mode == "assert":
            return asserted
        if self.mode == "failed":
            return failed or asserted
        return False

    def write_run(self, res):
        """Write one {run}.out. Called only after run_once() has returned, so
        every timing in res is already a frozen number - see the LATENCY
        SAFETY note at the top of this section."""
        if self.dir is None:
            return
        path = os.path.join(self.dir, f"{res['run']}.out")
        try:
            with open(path, "wb") as fh:
                fh.write(self._render_meta(res).encode("utf-8"))
                self._write_body(fh, res)
        except OSError as exc:
            sys.stderr.write(f"warning: could not write capture {path}: {exc}\n")
            return
        self.written.append(res["run"])

    def _render_meta(self, res):
        phases = res.get("phases") or {}

        if res.get("failed"):
            outcome = f"REQUEST-FAILED {res.get('marker') or '<ERR>'}"
        elif res.get("_assert_fails"):
            outcome = "ASSERTION-FAILED"
        else:
            outcome = "OK"

        out = [
            "# check-endpoint capture",
            "",
            f"run:             {res['run']}",
            f"outcome:         {outcome}",
            f"captured-at:     {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}",
            f"url:             {self.url}",
            f"ip:              {res.get('ip') or '-'}",
            f"http-code:       {res.get('code') if res.get('code') is not None else '-'}",
            f"proto:           {res.get('proto') or '-'}",
            f"bytes:           {res['bytes'] if res.get('bytes') is not None else '-'}",
            f"tls-verified:    {'no (-k/--insecure)' if res.get('insecure') else 'yes'}",
        ]
        if res.get("errno") is not None:
            out.append(f"curl-errno:      {res['errno']}")

        out += ["", "[timings]"]
        for key, label in (
            ("dns", "dns"),
            ("tcp", "tcp-connect"),
            ("tls", "tls-handshake"),
            ("pretransfer", "pre-transfer"),
            ("ttfb", "1st-byte"),
            ("download", "body-dl"),
            ("total", "total"),
        ):
            val = phases.get(key)
            if val is None:
                out.append(f"{label + ':':17}-")
            else:
                # Human units for reading, raw seconds for machine parsing -
                # the raw value is the one to feed a spreadsheet or a script.
                out.append(f"{label + ':':17}{human_time(val):<10}({val:.6f}s)")

        if res.get("redirect_count"):
            out.append(
                f"{'redirects:':17}{res['redirect_count']} "
                f"({res.get('redirect_time') or 0.0:.6f}s total)"
            )
        else:
            out.append(f"{'redirects:':17}none")

        if res.get("chunks") is not None:
            out += ["", "[stream]", f"{'chunks:':17}{res['chunks']}"]
            for key, label in (("avggap", "avg-gap"), ("maxgap", "max-gap")):
                val = res.get(key)
                out.append(
                    f"{label + ':':17}-"
                    if val is None
                    else f"{label + ':':17}{human_time(val):<10}({val:.6f}s)"
                )

        fails = res.get("_assert_fails")
        if fails:
            out += ["", "[assertions]"]
            out += [f"FAIL: {reason}" for reason in fails]
        elif fails is not None:
            out += ["", "[assertions]", "PASS"]

        cookies = res.get("cookies")
        if cookies:
            out += ["", "[cookies]"]
            for c in cookies:
                flags = ",".join(
                    f
                    for f, on in (
                        ("secure", c["secure"]),
                        ("httponly", c["http_only"]),
                        ("subdomains", c["include_subdomains"]),
                    )
                    if on
                )
                out.append(
                    f"{c['name']}={c['value']}  "
                    f"[{c['domain']}{c['path']}{(' ' + flags) if flags else ''}]"
                )

        headers = res.get("headers")
        out += ["", "[response-headers]"]
        if not headers:
            out.append("(none captured)")
        else:
            if headers.get("_status_line"):
                out.append(headers["_status_line"])
            for k, v in headers.items():
                if k != "_status_line":
                    out.append(f"{k}: {v}")

        body = res.get("body")
        out.append("")
        if body is None:
            out.append("[response-body] (not captured)")
        elif not body:
            # Distinguishes "nothing came back" from "0 bytes shown", which
            # reads as a truncation artifact in a file you opened precisely
            # because the run failed.
            out.append("[response-body] (none received)")
        else:
            shown = min(len(body), self.body_limit)
            total = res.get("bytes")
            note = f"{shown} bytes shown"
            if total is not None and total > shown:
                note += f", {total} received - TRUNCATED"
            out.append(f"[response-body] ({note})")
        out.append("")
        return "\n".join(out)

    def _write_body(self, fh, res):
        """Body goes to disk as raw bytes, not decoded text, so a capture is
        byte-identical to what came off the wire - binary payloads, odd
        encodings and mojibake all survive for later inspection."""
        body = res.get("body")
        if not body:
            return
        fh.write(body[: self.body_limit])
        if not body.endswith(b"\n"):
            fh.write(b"\n")

    def close(self):
        """Append the captured-run index to command-statement.out and tell the
        user on stderr where everything landed (stderr so it stays out of a
        piped table)."""
        if not self.active or self.dir is None:
            return
        try:
            with open(
                os.path.join(self.dir, "command-statement.out"), "a", encoding="utf-8"
            ) as fh:
                fh.write("\n# captured runs\n\n")
                fh.write(
                    ", ".join(str(r) for r in self.written)
                    if self.written
                    else f"(none - no run matched --capture-on {self.mode})"
                )
                fh.write("\n")
        except OSError:
            pass
        n = len(self.written)
        sys.stderr.write(
            f"# capture: {n} run{'' if n == 1 else 's'} recorded in {self.dir}/\n"
        )
        sys.stderr.flush()


# ── main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]),
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
  IP_ADDRESS     resolved IP of the remote host
  DNS            time spent on DNS lookup (that phase only)
  TCP_CONNECT    time spent on the TCP handshake (that phase only)
  TLS_HANDSHAKE  time spent on the TLS handshake (blank for plain http://)
  PRE-TRANSFER   time from connect-ready to request-send-ready
  1ST_BYTE       time from request sent to first byte of the response body
  REDIRECT       redirects followed: count and total time (blank if none).
                 REDIRECT time is why TOTAL_TIME can exceed the sum of other
                 columns - it accounts for all redirect round-trips.
  BODY_DL        time to receive the full response body after the first byte
  TOTAL_TIME     total end-to-end request time including any redirects
  HTTP_CODE      response status code
  TOTAL_BYTES    size of the response body received
  PROTO          HTTP version actually used: h1 (HTTP/1.1), h1.0 (HTTP/1.0),
                 h2 (HTTP/2), or h3 (HTTP/3)

  Every column except TOTAL_TIME is a per-phase delta.
  DNS + TCP + TLS + PRE-TRANSFER + 1ST_BYTE + REDIRECT + BODY_DL ≈ TOTAL_TIME

  With -S/--stream, three extra columns are appended:
  CHUNKS         number of chunks the response body arrived in
  AVG_GAP        average time BETWEEN consecutive chunks (excludes the
                 first chunk's arrival - that span is already the DNS +
                 TCP + TLS + PRE-TRANSFER + 1ST_BYTE columns, so counting
                 it again here would double as fake in-stream stutter)
  MAX_GAP        longest of those inter-chunk gaps
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
    curl       curl/8.8.0, what the curl(1) command line tool sends
    pycurl     pycurl/8.8.0

DEFAULT REQUEST HEADERS
  Every request carries 'Accept: */*' unless you supply your own Accept
  header with -H, which replaces it:

    -H "Accept: application/json"     send that instead
    -H "Accept:"                      send no Accept header at all

  Accept-Encoding and Accept-Language are separate headers and do not
  replace the default Accept.

COOKIES (-b/--cookie, -j/--cookie-jar, --show-cookies)
  -b DATA|FILE   curl-compatible, and told apart the same way curl does: if
                 the argument contains '=' it's literal cookie data to send
                 ('session=abc123; theme=dark'); otherwise it's a filename
                 to read cookies from (Netscape jar format, or raw
                 Set-Cookie lines). Either form also turns the cookie
                 engine ON, so Set-Cookie responses are captured and resent
                 automatically on later requests.

  -j FILE        write every cookie accumulated across all -c N runs to
                 FILE in Netscape jar format once the run finishes. This is
                 curl's -c/--cookie-jar - renamed here to -j because -c
                 already means --count.

  --show-cookies print every cookie sent or received (name, value,
                 domain/path, secure/httponly/subdomain flags, and expiry)
                 after the run. Works with or without -b/-j - by itself it
                 just turns the cookie engine on for the duration of the
                 run so you can see what the server sets.

  Persistence across -c N: each run normally gets a brand-new libcurl
  handle with nothing carried over. Cookie handling is the one exception -
  all runs in a single invocation share one cookie engine (via a
  pycurl.CurlShare), the same way separate `curl -b jar -c jar` invocations
  share state through a jar file on disk. That means a Set-Cookie from run
  1 is automatically sent back on run 2+, so you can test login flows,
  session affinity, and anything else that depends on cookie state
  persisting across repeated requests - not just a single one.

  Examples:
    -b "session=abc123" https://example.com                     send one
    -b cookies.txt https://example.com                           read a jar
    -b cookies.txt -j cookies.txt -c 5 https://example.com        round-trip
    --show-cookies https://example.com                        see what's set

  A literal -b "name=value" is sent correctly but will NOT itself appear in
  --show-cookies or a -j jar - libcurl sends it directly as the Cookie
  header without adding it to the cookie engine's store. It only shows up
  there if the server also sends it back via Set-Cookie. Cookies loaded
  from a FILE (-b cookies.txt) or received via Set-Cookie always go through
  the store, so those do appear.

WHAT IT CAN FIND
  Sections below follow the left-to-right order of the output columns, so
  you can read a row of output and jump straight to the section for
  whichever column looks wrong. Run with -c 10 or -c 20 to surface
  patterns invisible in a single request.

  Column                        Section
  #                             Run count, warm-up & outliers
  IP_ADDRESS                    Load balancing & round-robin
  DNS                           DNS & resolution
  TCP_CONNECT                   TCP & network
  TLS_HANDSHAKE                 TLS & security
  PRE-TRANSFER                  Client-side & proxy setup
  1ST_BYTE                      Server processing
  REDIRECT                      Redirect chains
  BODY_DL                       Body transfer & server-side IO
  TOTAL_TIME                    End-to-end budget
  HTTP_CODE                     Status codes & flakiness
  TOTAL_BYTES                   Response size & content drift
  PROTO                         HTTP version
  CHUNKS/AVG_GAP/MAX_GAP        Streaming responses (-S)

  [#] Run count, warm-up & outliers
    - Warm-up on run 1: the first request pays for cold DNS, a fresh TCP
      handshake, and a full TLS negotiation; runs 2+ reuse all three.
      Compare run 1 against the rest before concluding anything is slow.
      If runs 2+ don't drop, that itself is the finding (see DNS,
      TCP_CONNECT, and TLS_HANDSHAKE below)
    - Outlier requests: a single request dramatically slower than the rest
      reveals cold cache misses, JVM garbage collection pauses, or lock
      contention
    - Intermittent timeouts: one or two <TO> markers among otherwise
      successful requests indicate connection pool exhaustion, GC pauses,
      or health check races
    - How many runs you need: -c 10 is enough to spot round-robin and
      obvious flakiness; --stats p95/p99 only become meaningful around
      -c 20 and up

  [IP_ADDRESS] Load balancing & round-robin
    - Uneven backends: without -P, different IPs per request show which
      backends are in rotation; timing differences per IP identify the
      slow ones
    - Isolate one backend: use -P to pin all requests to a single IP; then
      switch IPs to compare them individually
    - Backend-specific errors: correlate IP_ADDRESS with HTTP_CODE to see
      which backend is misbehaving
    - Rotation mid-test: the IP changing partway through a -c N run means
      a DNS TTL expired and the resolver handed back a different member
      of the pool
    - One IP is not one server: behind a CDN or an anycast address, every
      request hits the same IP while landing on different edge nodes.
      IP_ADDRESS cannot see that; --server-hints can
    - IPv4 vs IPv6 paths differ: run -4 and -6 separately against the same
      host; a large gap points at a misconfigured or unoptimised AAAA path

  [DNS] DNS & resolution
    - Slow or flaky resolvers: high or variable DNS times across runs
    - Missing local DNS cache: DNS stays high every request instead of
      dropping to ~0ms after the first lookup
    - Short TTLs: DNS spikes when the record expires mid-test
    - libcurl's own cache hides the real cost: runs 2+ normally show ~0ms
      because libcurl caches within the process, not because your
      resolver is fast. Use -F to force a fresh lookup every run and
      measure the true cost
    - Deep CNAME chains: a hostname pointing through several CNAMEs
      before the final A/AAAA record costs extra round-trips, showing as
      consistently elevated DNS even on a healthy resolver
    - <DNS-FAIL>: hostname cannot be resolved at all

  [TCP_CONNECT] TCP & network
    - Geographic latency: high TCP_CONNECT reveals round-trip time to the
      server
    - Connection backlog: TCP time grows as the server runs out of accept
      queue capacity under load
    - Firewall / filtering: <CONN-FAIL> on specific ports or from
      specific network paths
    - Connection reuse not happening: TCP should collapse to ~0ms from
      run 2 onwards. If it stays high on every run without -F, something
      is closing the connection each time: Connection: close, a proxy,
      or a load-balancer idle timeout
    - Packet loss: an occasional TCP time several times the median, with
      the rest steady, suggests a lost SYN being retransmitted
    - A proxy shortens what you're measuring: with a proxy in the path
      this column is the time to the proxy, not to the origin

  [TLS_HANDSHAKE] TLS & security
    - Missing session resumption: TLS time stays high on every repeat
      request instead of dropping after the first; compare run 1 vs
      run 2+
    - Slow OCSP validation or long cert chains: consistently elevated TLS
      time even without load
    - TLS 1.2 vs 1.3: 1.3 completes in one round-trip and 1.2 needs two,
      so a handshake at roughly twice the TCP time suggests the server
      negotiated 1.2
    - n/a on an http:// URL is expected; a value here on an http:// URL
      means the request was redirected to HTTPS - check REDIRECT
    - Certificate expiry: pair with --tls-info for issuer, SANs, and days
      remaining before the certificate lapses
    - <TLS-FAIL>: expired cert, hostname mismatch, or untrusted CA

  [PRE-TRANSFER] Client-side & proxy setup
    - Non-zero PRE-TRANSFER: this phase is internal libcurl bookkeeping
      and is normally ~0ms; consistently high values indicate CPU
      pressure on the machine running check-endpoint.py itself
    - Proxy tunnel setup: when connecting through an HTTP proxy, the
      CONNECT exchange lands in this column rather than in TCP_CONNECT
    - A useful control: because it should be ~0ms on a direct connection,
      a non-zero value warns that the measurements themselves may be
      distorted by local load; treat the rest of that row with suspicion

  [1ST_BYTE] Server processing (the most diagnostic column in the table)
    - Slow backend: high 1ST_BYTE reveals heavy server work: DB queries,
      auth checks, computation, rendering
    - Queue depth behind a reverse proxy: fast TCP but slow 1ST_BYTE means
      the proxy accepted the connection but the backend was busy
    - Backend inconsistency: variable 1ST_BYTE across runs reveals
      hot/cold cache states, uneven DB load, or connection pool
      exhaustion
    - Classic pattern - high 1ST_BYTE + fast BODY_DL: the server is slow
      to produce the response but fast to deliver it; the bottleneck is
      computation or IO server-side, not the network
    - Slow DB providing response data: consistently high 1ST_BYTE while
      BODY_DL is fast points directly at backend data retrieval time
    - Turn it into a check: --max-ttfb 300ms fails the run when the
      backend crosses your threshold, the single most useful assertion
      for CI

  [REDIRECT] Redirect chains
    - Why TOTAL_TIME exceeds the sum of the other columns: every other
      column describes the final connection only. Redirect round-trips
      are accounted for here and nowhere else
    - The cost of an http:// -> https:// upgrade: hitting the plain-HTTP
      URL pays for an extra DNS + TCP round-trip before the real request
      starts. Request the https:// URL directly and this column drops
      to n/a
    - Redirects to a different host: when the redirect crosses hostnames,
      the DNS, TCP_CONNECT and TLS_HANDSHAKE columns describe the
      destination, not the URL you asked for, and IP_ADDRESS will not
      match the original hostname
    - Cross-region redirects: a .com that redirects to a country-specific
      domain can add latency invisible in any other column
    - <RDR-FAIL>: a redirect loop, or a chain longer than libcurl will
      follow

  [BODY_DL] Body transfer & server-side IO
    - Slow server IO: high BODY_DL relative to content size (slow disk
      reads, DB result streaming)
    - Bandwidth throttling: BODY_DL scales disproportionately with
      response size
    - Responses are uncompressed by default: the probe does not send
      Accept-Encoding, so servers return identity encoding. Add
      -H "Accept-Encoding: gzip" to measure what a browser actually
      experiences; BODY_DL and TOTAL_BYTES should both drop sharply, and
      if they don't, compression isn't configured, which is the finding
    - TCP slow-start on large bodies: the first response over a fresh
      connection transfers more slowly than later ones; compare run 1
      against runs 2+ before blaming the server

  [TOTAL_TIME] End-to-end budget
    - The only cumulative column: every other timing column is that
      phase alone. Use this one for SLOs and user-facing budgets
    - When it doesn't add up: if TOTAL_TIME is much larger than the sum
      of the phases, the difference is almost always in REDIRECT
    - Tail latency, not averages: --stats reports p50/p90/p95/p99; a
      healthy p50 alongside a p99 several times higher is the signature
      of an intermittent problem that averages hide
    - Turn it into a check: --max-total 1s exits non-zero when breached,
      so the probe drops straight into CI or cron

  [HTTP_CODE] Status codes & flakiness
    - Mixed response codes: running -c 20 surfaces occasional 502/503
      mixed with 200s, revealing backend instability, pods cycling in
      Kubernetes, or upstream timeouts
    - Rate limiting under repetition: 429s appearing partway through a
      -c 20 run mean you found the rate limit, not an outage; slow the
      probe down before reading anything else into the results
    - Auth problems: 401/403, or the <AUTH-FAIL> marker, when testing
      protected endpoints with -H "Authorization: ..."
    - A 3xx here means redirects were followed: the code shown is the
      final response; check REDIRECT for what happened on the way
    - Assert on it: --assert-status 200 fails the run on anything else

  [TOTAL_BYTES] Response size & content drift
    - Inconsistent content size: TOTAL_BYTES varies across -c N runs,
      revealing A/B tests, CDN inconsistencies, partial or truncated
      responses, or outright payload bugs
    - Suspiciously small 200s: a successful status with a tiny body is
      often a soft error page or an empty JSON envelope; --expect-body
      or --expect-regex turn that into a real failure
    - Truncated transfers: a byte count well below the rest of the run,
      especially alongside <RECV-FAIL>, means the response was cut short
    - Compression state: see the BODY_DL note above; byte counts are for
      the encoding actually received

  [PROTO] HTTP version
    - Verify HTTP/2 is actually active: --http2 with the PROTO column
      confirms whether the server is serving h2 or falling back to h1.
      Useful to verify CDN or load balancer HTTP/2 configuration
    - Connection reuse visible in timing: on repeated -c N runs with
      --http2, TCP_CONNECT and TLS_HANDSHAKE drop to <1ms from run 2
      onwards, confirming the persistent connection is being reused, one
      of HTTP/2's main performance benefits
    - Detect HTTP/2 connection issues: if PROTO shows h1 despite --http2,
      the server or an intermediate proxy is downgrading the connection
    - Values you may see: h1 (HTTP/1.1), h1.0 (HTTP/1.0), h2 (HTTP/2), h3
      (HTTP/3). An h1.0 is worth investigating on its own: it usually
      means an old proxy in the path, and HTTP/1.0 disables keep-alive by
      default

  [CHUNKS] [AVG_GAP] [MAX_GAP] Streaming responses (-S/--stream only)
    Without -S, a streaming response is still measured meaningfully:
    1ST_BYTE is the time until the first chunk/token arrives, and
    BODY_DL is the total duration of the whole stream. What's missing
    without -S is the rhythm of the stream - whether it arrives steadily
    or in bursts with stalls.

    AVG_GAP and MAX_GAP measure the time strictly between chunks. The
    first chunk's arrival is deliberately excluded, since that span is
    already the DNS + TCP + TLS + PRE-TRANSFER + 1ST_BYTE columns;
    counting it again here would misreport ordinary connection setup as
    if it were an in-stream stall. With fewer than 2 chunks there's no
    inter-chunk gap to measure, so both columns correctly show n/a
    rather than a misleading number.

    This makes AVG_GAP functionally the same metric LLM serving
    benchmarks call Inter-Token Latency (ITL), the average time between
    successive tokens. Measuring it over the wire, rather than trusting
    server-side logs, captures what the client actually experiences:
    network jitter, reverse-proxy buffering, and load-balancer hops are
    all included, not just model-side generation time.

    - Token stutter / uneven generation: a large gap between AVG_GAP and
      MAX_GAP means the stream paused somewhere in the middle, even
      though BODY_DL and TOTAL_TIME look fine in aggregate. This is
      exactly the kind of thing that makes a chat UI feel like it
      "hangs then dumps text"
    - Buffering misconfigurations: if a reverse proxy is accidentally
      buffering the whole response before forwarding it (a common nginx
      proxy_buffering misconfiguration), CHUNKS collapses to 1 or 2,
      AVG_GAP/MAX_GAP show n/a, and 1ST_BYTE balloons to roughly equal
      TOTAL_TIME, so the "stream" isn't actually streaming
    - Inconsistency across backend replicas: combine with IP_ADDRESS to
      see whether one particular backend produces the stutter (uneven
      load, resource pressure) while others stream smoothly
    - ITL benchmarking without server-side instrumentation: if you don't
      have access to your model server's internal metrics (or you're
      testing someone else's API), -c 20 -S gives you a client-side ITL
      measurement for free: AVG_GAP is your typical inter-token latency,
      MAX_GAP is your worst-case, and running multiple requests shows
      whether ITL is consistent or degrades under concurrent load
    - Works with auth and POST bodies: -S composes with -H/-d/-X, so you
      can test real chat-completion or SSE endpoints directly:
      -X POST -d '{{"stream": true, ...}}' -H "Authorization: Bearer ..." -S

  BEYOND THE COLUMNS
  These two aren't tied to a single column. They're driven by flags, and
  read from the response headers or from what you send.

  --server-hints / --capture-header - CDN, edge & backend provenance
    When a single IP hides many backends (a CDN or a reverse proxy in
    front of a pool), IP_ADDRESS alone cannot tell them apart.
    --server-hints reads the response headers that do, one row per
    request, then rolls each header up as constant (same every run),
    varied (a few distinct values, the real "which backend served it"
    signal), or per-request (a different value every run, typically a
    trace or request id).
    - Which edge/PoP served each request: x-served-by, x-amz-cf-pop, and
      cf-ray reveal CDN point-of-presence and cache-node rotation across
      -c N runs, even though every request hit the same anycast IP
    - Cache hit ratio over the wire: x-cache / cf-cache-status varying
      HIT vs MISS across runs shows how often you are actually served
      from cache
    - Which backend pod answered: track your own routing headers with
      --capture-header x-backend --capture-header x-pod-name; a header
      that varies between a handful of values maps directly to the pods
      in rotation
    - Confirm a header is present at all: a --capture-header value that
      shows "-" on every run tells you the server never sent it
    - Readable multi-hop chains: Fastly/Varnish chains like
      x-served-by = shield-IAD, shield-IAD, edge-PAO collapse by default
      to just x-served-by(final) = edge-PAO [3 hops in chain]; add
      --full-cdn when you want the whole chain

  -H / -d / -X - Authentication & specific endpoints
    - Authenticated APIs: use -H "Authorization: Bearer token" to test
      protected endpoints; <AUTH-FAIL> or 401/403 reveals auth
      configuration problems
    - POST/PUT/PATCH endpoints: use -d @payload.json
      -H "Content-Type: application/json" -X PUT to test write endpoints
      with real payloads
    - Token expiry under load: combine auth headers with -c 20 to
      observe if validation degrades or fails on repeated calls
    - Header-conditional behavior: send routing or feature-flag headers
      (-H "X-Feature: beta") to test conditional server logic
    - Content negotiation: the probe sends Accept: */* unless you
      override it; -H "Accept: application/json" reveals endpoints that
      serve HTML to generic clients and JSON to specific ones

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

  --server-hints
    After the run, print a PER-REQUEST summary of the headers that hint at
    which server / edge / CDN / backend produced each response (server, via,
    x-served-by, x-cache, cf-ray, cf-cache-status, x-amz-cf-pop, x-backend,
    x-envoy-upstream-service-time, fly-request-id, x-vercel-id, and more).
    Each successful run gets a row (# + IP + key=value headers), followed by a
    rollup that classifies every header as:
      constant     same value every run   (e.g. server=cloudflare)
      varied       a few distinct values  (the real which-backend signal,
                   e.g. x-cache = HIT×6 / MISS×4, or two x-amz-cf-pop codes)
      per-request  a different value each run (request/trace ids like cf-ray)
    Combine with -c N (and optionally -F to avoid connection reuse) to reveal
    load-balancer rotation and CDN PoP selection.

  --capture-header NAME   (repeatable)
    Also track one or more specific response headers by name and show their
    value per request in the same end-of-run summary. Missing values render as
    "-", so you can confirm whether an expected header is present at all and
    whether it changes between backends. Works with or without --server-hints.

  --full-cdn
    Some CDN/cache headers are a comma-separated CHAIN of hops (Fastly/Varnish
    x-served-by, x-cache, x-cache-hits, via), oldest shield first and the edge
    that actually served you last. BY DEFAULT the provenance summary collapses
    those to just the final hop and appends "[N hops in chain]", so
      x-served-by = cache-iad-...-IAD, cache-iad-...-IAD, cache-pao-kpao1770024-PAO
    reads as
      x-served-by(final) = cache-pao-kpao1770024-PAO   [4 hops in chain]
    and x-cache "MISS, HIT, HIT" collapses to the edge verdict "HIT". Pass
    --full-cdn to show the entire raw chain instead. Only the known chained
    headers are collapsed by default; every other header is shown verbatim
    either way.

  OUTPUT CAPTURE (--capture-on)
    Record full responses to disk so a failure can be read afterwards
    instead of reproduced. Not to be confused with --capture-header, which
    only tracks a header name in the end-of-run summary.

      --capture-on WHEN   never   (default) capture nothing
                          all     record every run
                          failed  record any request failure OR assertion
                                  breach - the usual choice
                          assert  record assertion breaches only
                          error   record transport/network failures only
                          Bare --capture-on means 'failed'.

    Each invocation writes one directory, named for when the command was
    kicked off plus the pid, so concurrent probes never collide:

      {{YYYYMMDDHHMMSS}}-{{pid}}/command-statement.out   the command used
      {{YYYYMMDDHHMMSS}}-{{pid}}/{{run}}.out              one per recorded run

    Run files are named for the run number as shown in the table's # column,
    so a failing row maps straight to its file. Each contains the outcome,
    every phase timing (human units and raw seconds), status, IP, protocol,
    assertion results, response headers, and the response body as raw bytes.

    Other flags:
      --capture-dir DIR        where to put the capture directory (default: .)
      --capture-body-limit SZ  max body bytes per run (default: 256.0KB;
                               accepts 512, 256K, 1M, 2MB)
      --capture-no-body        record timings/status/headers but no body
      --capture-secrets        write the command verbatim; by default
                               Authorization-style header values and literal
                               -b cookie data are recorded as <redacted>,
                               since capture files get shared

    Capture does not affect the timing numbers. The directory is created
    before the first request; nothing is written to disk during a transfer;
    run files are written between requests, after libcurl has stopped the
    clock and every phase timer has been read; and body buffering is capped
    so a large download can't turn into a large memcpy inside the transfer.
    Because a run's fate isn't known until it finishes, responses are
    buffered for every run once capture is on - only the write is
    conditional. Cannot be combined with --prometheus.

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

  See which backend/edge served each of 10 requests (CDN chains collapse to
  the final serving edge/PoP by default):
      ./check-endpoint.py -c 10 --server-hints https://example.com

  Same, but show the FULL multi-hop CDN chain instead of just the final hop:
      ./check-endpoint.py -c 10 --server-hints --full-cdn https://example.com

  Track a specific header per request (repeatable), e.g. a backend id:
      ./check-endpoint.py -c 10 --capture-header x-backend --capture-header x-pod https://example.com

  Force fresh connections so every run can land on a different backend:
      ./check-endpoint.py -c 10 -F --server-hints https://example.com

  Run a Prometheus exporter that re-probes on every scrape:
      ./check-endpoint.py --prometheus --prometheus-port 9109 https://example.com
      # then: curl localhost:9109   (Prometheus scrapes the same endpoint)

  Capture the response of any run that fails an assertion:
      ./check-endpoint.py -c 20 --assert-status 200 --capture-on failed https://example.com

  Capture every run into a chosen directory:
      ./check-endpoint.py -c 5 --capture-on all --capture-dir /tmp/probes https://example.com

  Capture only transport failures, headers and timings but no body:
      ./check-endpoint.py -c 50 --capture-on error --capture-no-body https://example.com

  Send a literal cookie (curl -b style):
      ./check-endpoint.py -b "session=abc123; theme=dark" https://example.com

  Send cookies read from a Netscape-format jar file:
      ./check-endpoint.py -b cookies.txt https://example.com

  Save whatever cookies the server sets to a jar file (curl's -c, renamed -j here):
      ./check-endpoint.py -j cookies.txt https://example.com

  Round-trip a session across repeated requests: load, save, reuse next time:
      ./check-endpoint.py -b cookies.txt -j cookies.txt -c 5 https://example.com

  See exactly what cookies were sent/received across all -c N runs:
      ./check-endpoint.py -c 5 --show-cookies https://example.com

  Test a login endpoint, then confirm the session cookie carries into the next request:
      ./check-endpoint.py -c 2 -X POST -d '{{"user":"me","pass":"x"}}' \\
          -H "Content-Type: application/json" -b cookies.txt -j cookies.txt \\
          --show-cookies https://example.com/login

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
        "-4",
        "--ipv4",
        action="store_true",
        help="force IPv4 resolution (default)",
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
        help=(
            "custom request header, curl-style (repeatable). "
            f"An Accept header here replaces the default '{DEFAULT_ACCEPT}'; "
            'use -H "Accept:" to send none at all'
        ),
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
        "-k",
        "--insecure",
        action="store_true",
        help=(
            "skip TLS certificate verification (like curl -k). Timings stay "
            "accurate, but certificate failures stop being reported - see "
            "--cacert for the safer option against internal CAs"
        ),
    )
    parser.add_argument(
        "--cacert",
        default=None,
        metavar="FILE",
        help=(
            "verify against this CA bundle instead of the system trust store; "
            "keeps verification on for endpoints using a private CA"
        ),
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

    # ── cookies ──────────────────────────────────────────────────────
    cookie_group = parser.add_argument_group(
        "cookies", "curl-compatible cookie handling."
    )
    cookie_group.add_argument(
        "-b",
        "--cookie",
        dest="cookie",
        default=None,
        metavar="DATA|FILE",
        help=(
            "curl-style: a literal cookie string ('name=value; name2=value2') "
            "to send, OR a filename to read cookies from (Netscape jar format "
            "or raw Set-Cookie lines). Like curl, it's a filename if there's "
            "no '=' in the argument, literal data otherwise. Either way this "
            "also turns on the cookie engine, so Set-Cookie responses are "
            "captured and resent on subsequent -c N runs"
        ),
    )
    # curl itself uses -c for --cookie-jar, but -c is already --count here -
    # -j is the substitute (mnemonic: "jar").
    cookie_group.add_argument(
        "-j",
        "--cookie-jar",
        dest="cookie_jar",
        default=None,
        metavar="FILE",
        help=(
            "write all cookies accumulated across every -c N run to FILE in "
            "Netscape jar format when the run finishes (curl's -c/--cookie-jar, "
            "renamed here since -c means --count)"
        ),
    )

    # ── output / analysis ──────────────────────────────────────────────
    out_group = parser.add_argument_group("output and analysis")
    out_group.add_argument(
        "--show-cookies",
        dest="show_cookies",
        action="store_true",
        help="after the run, print every cookie sent or received (name, "
        "value, domain/path, flags, and expiry) across all -c N runs",
    )
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
        "--server-hints",
        dest="server_hints",
        action="store_true",
        help="after the run, print a per-request summary of server/edge/CDN "
        "identifying headers (server, via, x-served-by, cf-ray, x-cache, "
        "x-backend, x-amz-cf-pop, ...), flagging which values are constant, "
        "which vary across -c N runs (the real 'which backend served it' "
        "signal), and which are unique per request",
    )
    out_group.add_argument(
        "--capture-header",
        dest="capture_header_names",
        action="append",
        default=[],
        metavar="NAME",
        help="capture a specific response header by name and show its value "
        "per request in the end-of-run provenance summary (repeatable)",
    )
    out_group.add_argument(
        "--full-cdn",
        dest="full_cdn",
        action="store_true",
        help="in the provenance summary, show the FULL comma-chained CDN/cache "
        "headers (x-served-by, x-cache, x-cache-hits, via). By default these "
        "are collapsed to just their final hop - the edge/PoP that actually "
        "served the request - with the chain depth noted; pass this flag to "
        "see every hop in the chain",
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

    # ── output capture (write responses to disk) ───────────────────────
    capture_group = parser.add_argument_group(
        "output capture",
        "Record full responses to disk so a failure can be read after the fact "
        "instead of reproduced. Unrelated to --capture-header, which only "
        "tracks a header name in the end-of-run summary.",
    )
    capture_group.add_argument(
        "--capture-on",
        dest="capture_on",
        nargs="?",
        const="failed",
        default="never",
        choices=CAPTURE_MODES,
        metavar="WHEN",
        help="record runs to disk. WHEN is one of: never (default), all "
        "(every run), failed (any request failure OR assertion breach), "
        "assert (assertion breaches only), error (transport/network "
        "failures only). Bare --capture-on means 'failed'",
    )
    capture_group.add_argument(
        "--capture-dir",
        dest="capture_dir",
        default=".",
        metavar="DIR",
        help="parent directory for the capture directory (default: current "
        "directory). Each invocation creates DIR/{YYYYMMDDHHMMSS}-{pid}/",
    )
    capture_group.add_argument(
        "--capture-body-limit",
        dest="capture_body_limit",
        default=None,
        metavar="SIZE",
        help="max response body bytes to record per run: 512, 256K, 1M, 2MB "
        f"(default: {human_bytes(CAPTURE_BODY_LIMIT_DEFAULT)}). Bodies past "
        "the cap are truncated, keeping capture off the latency hot path",
    )
    capture_group.add_argument(
        "--capture-no-body",
        dest="capture_no_body",
        action="store_true",
        help="record timings, status and headers but not the response body - "
        "for large downloads, or when the body is sensitive",
    )
    capture_group.add_argument(
        "--capture-secrets",
        dest="capture_secrets",
        action="store_true",
        help="write the command statement verbatim. By default the values of "
        "Authorization-style headers and literal -b cookie data are replaced "
        "with '<redacted>', since capture files get shared",
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

    cookie_literal, cookie_file = resolve_cookie_arg(args.cookie)
    cookies_active = bool(
        cookie_literal or cookie_file or args.cookie_jar or args.show_cookies
    )
    # A CurlShare with LOCK_DATA_COOKIE is what lets cookies persist across
    # separate run_once() calls / curl handles on repeated -c N runs -
    # without it, each run would get a brand-new, empty cookie jar and
    # never see cookies set by an earlier run in the same invocation.
    cookie_share = None
    if cookies_active:
        cookie_share = pycurl.CurlShare()
        cookie_share.setopt(pycurl.SH_SHARE, pycurl.LOCK_DATA_COOKIE)

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

    # ── TLS verification flags ────────────────────────────────────────
    # -k and --cacert are mutually exclusive in effect: -k discards the trust
    # store entirely, so silently ignoring a --cacert the user bothered to
    # supply would be the wrong kind of quiet. Fail instead.
    if args.insecure and args.cacert:
        sys.stderr.write(
            "error: -k/--insecure and --cacert are mutually exclusive.\n"
            "  -k disables verification entirely, so a CA bundle is unused.\n"
            "  Drop -k to verify against --cacert, or drop --cacert to skip verification.\n"
        )
        sys.exit(1)
    if args.cacert and not os.path.isfile(args.cacert):
        sys.stderr.write(f"error: --cacert file not found: {args.cacert}\n")
        sys.exit(1)
    if args.insecure:
        # stderr, so it stays out of piped table output but is impossible to
        # miss interactively. This tool's output gets handed to other people as
        # evidence; a -k run that produced a clean table has NOT shown that the
        # certificate is good, and the run needs to say so.
        sys.stderr.write(
            "warning: -k/--insecure - certificate verification is OFF. "
            "Certificate failures will not be reported.\n"
        )

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

    # ── output capture setup ──────────────────────────────────────────
    # Everything here happens before the first request: bad arguments and
    # unwritable directories fail immediately rather than 500 runs in, and
    # the mkdir is done and dusted before any clock starts.
    capture_body_limit = CAPTURE_BODY_LIMIT_DEFAULT
    if args.capture_body_limit is not None:
        try:
            capture_body_limit = parse_size(args.capture_body_limit)
        except ValueError:
            sys.stderr.write(
                f"error: invalid --capture-body-limit: {args.capture_body_limit!r} "
                "(expected e.g. 512, 256K, 1M, 2MB)\n"
            )
            sys.exit(2)

    if args.capture_on != "never" and args.prometheus:
        # The exporter re-probes on every scrape and never exits, so capture
        # would grow without bound for as long as Prometheus keeps polling.
        sys.stderr.write(
            "error: --capture-on cannot be used with --prometheus.\n"
            "  The exporter re-probes on every scrape and runs indefinitely, so\n"
            "  capturing would fill the disk. Run a normal probe to capture.\n"
        )
        sys.exit(2)

    capture = CaptureWriter(
        mode=args.capture_on,
        base_dir=args.capture_dir,
        url=args.url,
        argv=sys.argv,
        started=datetime.now().astimezone(),
        body_limit=capture_body_limit,
        want_body=not args.capture_no_body,
        redact=not args.capture_secrets,
        count=args.count,
    )
    try:
        capture.open()
    except OSError as exc:
        sys.stderr.write(f"error: cannot create capture directory: {exc}\n")
        sys.exit(2)

    capture_body = args.expect_body is not None or expect_regex is not None
    # Headers must be captured for --show-headers, --server-hints, or any
    # --capture-header NAME. Any one of them turns on the per-response capture.
    capture_headers = (
        args.show_headers or args.server_hints or bool(args.capture_header_names)
    )
    capture_cert = args.tls_info

    # Whether a run will be worth capturing isn't known until it has finished,
    # so once capture is on the response has to be buffered for EVERY run
    # regardless of mode - there is no going back for a body that was thrown
    # away. Only the disk write is conditional.
    body_limit = BODY_CAPTURE_LIMIT
    if capture.active:
        capture_headers = True
        if capture.want_body:
            capture_body = True
            # Assertions scan up to BODY_CAPTURE_LIMIT; capture keeps its own,
            # usually smaller, cap. Buffer whichever needs more and let the
            # writer trim to the capture limit on the way to disk.
            body_limit = (
                max(BODY_CAPTURE_LIMIT, capture_body_limit)
                if assert_cfg is not None and (args.expect_body or expect_regex)
                else capture_body_limit
            )

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
                insecure=args.insecure,
                cacert=args.cacert,
                cookie_literal=cookie_literal,
                cookie_file=cookie_file,
                cookie_jar=args.cookie_jar,
                cookie_share=cookie_share,
                capture_cookies=cookies_active,
                body_limit=body_limit,
            )
            if assert_cfg is not None:
                res["_assert_fails"] = evaluate_assertions(res, assert_cfg)
            # Written here, between requests - never during one. run_once has
            # returned, so every timer has already been read off the handle
            # and res holds frozen numbers that no later I/O can affect.
            if capture.should_capture(res):
                capture.write_run(res)
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

    if args.cookie_jar:
        sys.stderr.write(f"# cookie jar written: {args.cookie_jar}\n")
        sys.stderr.flush()

    if args.stats:
        print_summary(results)
    if args.tls_info:
        print_tls_info(cert)
    if args.show_headers:
        print_headers_block(results)
    if args.show_cookies:
        # Thanks to cookie_share, every run's cookie snapshot reflects the
        # full accumulated state, not just what that one request received -
        # so the last run captured (successful or not) is representative of
        # all of them. Checked with "is not None", not truthiness, since an
        # empty list is a meaningful "engine was on, nothing was set."
        last_cookies = next(
            (r["cookies"] for r in reversed(results) if r.get("cookies") is not None),
            [],
        )
        print_cookies_block(last_cookies)
    if args.server_hints or args.capture_header_names:
        print_provenance_summary(
            results, args.server_hints, args.capture_header_names, args.full_cdn
        )
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

    # Before the exit below, so the capture index is finalised and the path is
    # reported even on a failing run - which is exactly the run you captured.
    capture.close()

    # Strict exit code: any single breaching run fails the whole invocation.
    if assert_cfg is not None and any(r["_assert_fails"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()

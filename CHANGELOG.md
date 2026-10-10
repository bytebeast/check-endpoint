# Changelog

## v3.0.0 (2026-10-10)

### Breaking changes

- **exit codes**: a request that gets no HTTP response (DNS, connect, TLS,
  timeout) now exits `3` when no assertion is set. It used to exit `0`, so
  `check-endpoint URL && deploy` carried on against a dead endpoint.
- **exporter**: `check_endpoint_requests_total` / `check_endpoint_failures_total`
  are now real counters (cumulative since start). As per-scrape gauges they
  broke Prometheus naming rules and `promtool check metrics` rejected the
  output. The per-scrape values are `check_endpoint_scrape_probes` and
  `check_endpoint_scrape_probe_failures`.

### Bug Fixes

- **failure placement**: with newer libcurl (seen on 8.22), a refused
  connection or failed TLS handshake printed invented `<1ms` PRE-TRANSFER and
  1ST_BYTE times and put `<CONN-FAIL>` / `<TLS-FAIL>` under BODY_DL instead of
  TCP_CONNECT / TLS_HANDSHAKE. libcurl fills those timers in when a transfer
  ends, even a failed one; phases now only count once the phases before them
  really completed. The same fix applies to the timings in `--capture` files.
- **column overflow**: `<CONN-FAIL>`, `<RECV-FAIL>`, `<SEND-FAIL>`,
  `<AUTH-FAIL>` and `<DNS-FAIL>` were wider than the columns they land in and
  shifted the rest of the row. Marker columns are now sized from the marker
  table.
- **piped output**: an ANSI reset code was written at the end of every row and
  in the bytes column even when stdout was not a terminal.
- **exporter**: `check_endpoint_up` was `1` when any probe in the scrape
  succeeded, contradicting its help text; it now reflects the most recent
  probe.
- **exporter**: p90/p95/p99 were published from as few as 2 samples, where
  they are just the max. Each percentile now needs enough samples (p90: 10,
  p95: 20, p99: 100), matching `--stats`.

### Features

- `--no-color`, and the `NO_COLOR` environment variable is honoured
- exporter: `check_endpoint_last_error{reason="..."}` and
  `check_endpoint_total_seconds_samples`
- test suite (`tests/test_check_endpoint.py`, 23 tests, all against local
  servers)

## v2.10.0 (2026-09-03)

### Features

- **check-endpoint.py **: add streaming gap... (AH-2026090361314)

### Documentation

- **README**: update readme (AH-2026081480030)

<!-- also in this release: style -->

## v2.9.0 (2026-08-13)

### Features

- **check-endpoint.py**: add support for body capture (AH-202608128171)

### Bug Fixes

- **check-endpoint.py **: fix UP031 (AH-2026081213840)
- **release**: fix release workflow (AH-2026081216284)

### Documentation

- **cli/dockerfile**: update EXTRA_PACKAGES comment (AH-2026081169578)

<!-- also in this release: chore -->

## v2.7.2 (2026-08-11)

### Bug Fixes

- **$0 filename**: fix $0 filename in Dockerfile (AH-2026081034454)

<!-- also in this release: chore -->

## v2.7.1 (2026-08-10)

### Bug Fixes

- **release**: ensure image have semantic ver tag (AH-2026081080303)
- **release**: noop retrigger (AH-2026081091253)

<!-- also in this release: revert -->

## v2.7.0 (2026-08-10)

### Features

- **Dockerfile**: pip only need tmp, rm it (AH-2026081069166)

## v2.6.0 (2026-08-10)

### Features

- **check-endpoint-cli**: add check-endpoint-cli... (AH-2026081021585)
- **release.py **: add contrib cli to release (AH-2026081047341)

### Bug Fixes

- **image & contrib-checks**: pin actions and... (AH-2026081031648)
- **debug-pod **: add security context (AH-2026081039924)

### Documentation

- **README**: update readme with check-endpoint-cli... (AH-2026081054969)

## v2.5.1 (2026-08-07)

### Bug Fixes

- **check-endpoint.py **: chmod +x (AH-2026080799342)

## v2.5.0 (2026-08-07)

### Features

- **check-endpoint.py **: add insecure tls support (AH-2026080797082)

### Documentation

- **README.md**: update install/platform notes (AH-2026080683293)

<!-- also in this release: chore, ci -->

## v2.4.0 (2026-08-02)

### Features

- **cookies**: add cookie support (AH-2026080266299)

## v2.3.0 (2026-08-02)

### Features

- **sync**: ensure contrib is insync (AH-2026080129821)

### Bug Fixes

- **timeout**: fix timeout is being swallowed (AH-2026080128841)
- **release.py**: fix bug, and improve ver regex... (AH-2026080136108)

<!-- also in this release: chore -->

## v2.2.1 (2026-07-31)

### Bug Fixes

- **check-endpoint.py**: usage & utc (AH-2026073094305)

<!-- also in this release: chore -->

## v2.2.0 (2026-07-28)

### Features

- **contrib workflow**: add contrib checks workflow (AH-2026072726942)
- **contrib checks workflow**: aquasecurity/trivy-action@0.28.0 → @v0.36.0 (the old ref didn't exist) Added persist-credentials: false to all 6 checkouts (AH-2026072729950)

### Bug Fixes

- **version pinning**: commit hash pinning for actions, and version pinning for image (AH-2026072735164)

## v2.1.0 (2026-07-28)

### Features

- **readme**: update screenshots on readme (AH-2026072075080)
- **images**: add curl-timings.png (AH-2026072075773)
- **check-endpoint.py**: show addtitional headers (AH-2026072482228)
- **imports**: fix datetime import (AH-2026072597495)
- **return eval**: collapse short circuit eval (AH-2026072515102)
- **headers**: ensure "accept: */*" is sent by default & add curl, pycurl agent aliases (AH-2026072682263)
- **readme**: update readme, per column desc and ordering (AH-2026072690349)
- **readme**: small blurb about most common (AH-2026072691523)
- **workflow**: initial github workflow (aka learning) (AH-202607268842)
- **ruff applied**: ruff applied (AH-2026072616858)
- **sync-check**: ensure check-endpoint.py in contrib is same as the one in root of repo (AH-2026072619396)
- **pyproject**: update (AH-2026072625466)
- **ruff fmt**: ruff fmt changes (AH-2026072627502)
- **contrib**: update contrib copy of check-endpoint (AH-2026072627825)
- **workflows**: add python security workflow (AH-2026072785398)
- **workflow**: add zizmor cfg (AH-2026072789392)
- **workflows**: update sync-check for zizmor (AH-2026072793117)
- **workflows**: py-sec updates (AH-202607276265)
- **release workflow**: initial commit (AH-202607279473)
- **release workflow**: supress checkout (AH-2026072711621)
- **release workflow**: review workflow method (AH-2026072713800)

### Bug Fixes

- **ruff issues**: fixed ruff issues (AH-2026072632515)

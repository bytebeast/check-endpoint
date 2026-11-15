# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`check-endpoint.py` is a single-file Python CLI (about 4,400 lines) that probes a URL with pycurl and prints per-phase timing (DNS, TCP, TLS, pre-transfer, first byte, redirect, body, total) live as each phase completes. Standard library plus `pycurl`, nothing else. There is deliberately no `[build-system]`; users install it by copying the script onto their PATH, so `pyproject.toml` is tooling config only.

## Commands

```bash
pip install -r requirements.txt pytest ruff   # pycurl needs libcurl headers (brew install curl / apt install libcurl4-openssl-dev)
python3 -m pytest                             # full suite
python3 -m pytest -k test_exit_0_when_all_good   # single test
ruff check .                                  # lint, includes flake8-bandit "S" rules
ruff format --check .                         # formatting, line length 88, double quotes
```

Tests spin up throwaway servers on `127.0.0.1` (plain, refused, silent, stalled, TLS) and need `openssl` on PATH for the self-signed cert. In a sandbox that blocks local port binding, every network test fails with `PermissionError`; that is the environment, not the code.

Tests load the script by path with `importlib` (the filename has a hyphen) as module `ce`, and also run it as a subprocess via `run_cli` to check exit codes and rendered output.

## Rules that are easy to break

- **Contrib copies must be byte-identical to the root script.** `contrib/check-endpoint-cli/check-endpoint.py` and `contrib/check-endpoint-exporter/check-endpoint.py` exist so each Docker build context is standalone. After any edit to `check-endpoint.py`, run `cp check-endpoint.py contrib/check-endpoint-cli/ && cp check-endpoint.py contrib/check-endpoint-exporter/`. Enforced by `test_contrib_copies_match_root_script` and the `sync-check` workflow. Ruff excludes the copies, so lint findings only ever point at the root script.
- **Never hand-edit versions or the CHANGELOG.** `.github/scripts/release.py` owns `APP_VERSION`, `pyproject.toml` `version`, both contrib copies, the README badge, `Chart.yaml` `appVersion` and the image tag in `kubernetes/deployment.yaml` (see `VERSION_FILES`). A hand-edited version without a matching `## vX.Y.Z` CHANGELOG heading is skipped by the tag job on purpose; tagging it would hide every releasable commit from the next release.
- **Python 3.9 floor.** `requires-python = ">=3.9"` is the single source of truth and ruff derives its target from it; do not set `[tool.ruff] target-version`. The `datetime.UTC` try/except shim near the top of the script must survive. Raising the floor to 3.11+ enables UP017, which would rewrite that shim into a NameError on 3.9/3.10.
- **Conventional commits drive releases.** `feat` is minor, `fix`/`perf` patch, `!` or `BREAKING CHANGE:` major, everything else no release. Ticket refs can go as a legacy prefix, trailing `(PROJ-123)`, or a `Refs:`/`Closes:` footer (see the `release.py` docstring).
- `try/except/pass` (S110) is globally ignored because the probe intentionally swallows failures around optional libcurl features (CERTINFO, header decoding, HTTP version lookup). Do not add new bare swallows elsewhere without a reason.

## Architecture

The flow through the script, which takes reading several distant sections to piece together:

- `main()` parses args (the help epilog is long and doubles as user docs), builds request config, then loops `run_once()` `-c` times. Stream mode (`-S`) appends the CHUNKS/AVG_GAP/MAX_GAP columns to `FIELDS` at runtime here.
- `run_once()` drives one request through a `pycurl.CurlMulti` loop rather than a blocking `perform()`, so it can print each phase cell the moment libcurl's timer for that phase is set. `LIVE_FIELD_KEYS` (dns, tcp, tls, pretransfer, ttfb) print during the transfer; `FINAL_FIELD_KEYS` (redirect, download, total, code, bytes, proto) print after. It returns a result dict that everything downstream consumes. `quiet=True` suppresses output for exporter mode.
- **Failure markers** (`<DNS-FAIL>`, `<CONN-FAIL>`, `<TLS-FAIL>`, `<TO>`, ...) come from `ERROR_MARKERS` / `marker_for_errno()` and must land in the column of the phase that failed. Newer libcurl fills in later timers even on a failed transfer, so `_phase_reached()` and `compute_phase_deltas()` only count a phase once the phases before it really completed. This is the historically buggy area; when touching it, test failure paths (timeout mid-body, DNS fail, refused, plain `http://` with no TLS phase), not just success.
- Downstream consumers of the result dicts: `print_summary` / `print_stream_summary` (percentiles), `evaluate_assertions` (`--expect-*` flags), `print_headers_block` / `print_provenance_summary` (`--server-hints`, `--capture-header`), `print_tls_info` / `print_tls_diagnosis`, `CaptureWriter` (`--capture-on`, redacts sensitive headers unless `--capture-secrets`), and `build_prometheus_text`.
- `--prometheus` turns the script into an exporter: `serve_prometheus()` re-probes the target on every scrape via a `probe_fn` closure. `/healthz` answers without probing. `_total` metrics are true counters accumulated across scrapes; per-scrape values use `scrape_` names.
- Colour (Catppuccin Mocha) is on only when stdout is a TTY and `NO_COLOR` is unset. Changes to `_col`, `write_cell` or `_colorize_*` should be checked both on a terminal and piped through `cat`.

Exit codes are part of the contract: `0` all good, `1` an assertion breached, `2` bad arguments, `3` a request got no HTTP response and no assertion was set.

## contrib and CI

- `contrib/check-endpoint-cli/` is the interactive image (Dockerfile + `debug-pod.yaml`). `contrib/check-endpoint-exporter/` is the daemon (Dockerfile, compose, Helm chart, plain manifests). Both Dockerfiles build with the repo root as context.
- `contrib-checks` runs hadolint, yamllint (`--strict`), `docker compose config`, `helm lint`/`template`, kubeconform and kube-linter on rendered output, and trivy, configured by `.hadolint.yaml`, `.yamllint.yaml` and `.kube-linter.yaml` at the root. Helm templates are excluded from yamllint and validated after rendering.
- `python-security` runs ruff S rules, `pip-audit`, gitleaks and zizmor. Keep workflows zizmor-clean; YAML anchors are avoided on purpose because zizmor cannot follow them.
- `image.yml` publishes `ghcr.io/bytebeast/check-endpoint` and `check-endpoint-exporter` (multi-arch). Release images are dispatched by `release.yml`'s tag job because a `GITHUB_TOKEN`-pushed tag does not trigger workflows.
- `main` is protected and needs a PR plus code scanning. Release PRs (`chore(release): vX.Y.Z`) are opened by a GitHub App token so their checks run.
- `site/index.html` is the GitHub Pages landing page, deployed by `pages.yml`.

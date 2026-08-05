<!--
location: .github/PULL_REQUEST_TEMPLATE.md

Thanks for contributing to check-endpoint! A few things below are specific
to how this repo is set up (conventional-commit release automation, a
duplicated exporter copy, ruff config with a couple of intentional
exceptions) - the rest is standard. Delete any section that doesn't apply.
-->

## What does this PR do?

<!-- One or two sentences. If it fixes a bug, describe the observable
symptom (e.g. "a timeout mid-download left every column after 1ST_BYTE
blank instead of showing <TO>") before the mechanism. -->

## Why

<!-- What prompted this - a bug you hit, a feature you needed, a cleanup.
Link an issue if there is one. -->

## Type of change

- [ ] `fix` - bug fix (patch release)
- [ ] `feat` - new flag/column/output mode (minor release)
- [ ] `perf` - performance improvement (patch release)
- [ ] `refactor` / `docs` / `chore` / `ci` - no user-facing behavior change (no
      release)
- [ ] Breaking change (`!` after the type, or `BREAKING CHANGE:` in the commit
      body - triggers a major release)

## Commit message format

This repo computes version bumps and the CHANGELOG entry from your commit
messages (`.github/scripts/release.py`), not from this PR description. Please
use conventional commits: `type(scope): description`, e.g.
`fix(timeout): show <TO> marker when BODY_DL fails`. Only `feat`/`fix`/`perf`
(or a breaking change) trigger a release; `docs`/`chore`/`refactor`/`ci` don't.
Ticket refs go in one of the three recognized spots (legacy
`PROJ-123 fix(...): ...` prefix, trailing `(PROJ-123)`, or a `Refs:`/`Closes:`
footer) - see the module docstring in `.github/scripts/release.py` for the full
list.

## Do NOT hand-edit for this PR

- [ ] `APP_VERSION` in `check-endpoint.py`
- [ ] `version` in `pyproject.toml`
- [ ] `APP_VERSION` in `contrib/check-endpoint-exporter/check-endpoint.py`
- [ ] `CHANGELOG.md`

These are written automatically by the `chore(release):` PR the release workflow
opens after your PR merges, based on the conventional-commit types above. A
manual bump here will conflict with that PR.

## Testing

- [ ] Ran against at least one real endpoint and checked the table renders
      correctly, columns aligned, colors sane
- [ ] If this touches the live-printing/column/pointer logic (`run_once`,
      `LIVE_FIELD_KEYS`, `FINAL_FIELD_KEYS`, the failure-marker placement):
      tested a **failure path**, not just success - e.g. a request that times
      out mid-`BODY_DL`, a `DNS-FAIL`, or `-6`/plain `http://` (no TLS phase)
      combined with a failure, since these are exactly the cases where a marker
      has previously gone missing or landed on the wrong column
- [ ] Tested with `--stream`/`-S` too, if the change touches
      `FIELDS`/`FINAL_FIELD_KEYS` (stream mode appends columns at runtime in
      `main()`)
- [ ] Checked output with color both on (TTY) and off (piped, e.g. `... | cat`),
      if this touches any `_col(...)`/`write_cell`/`_colorize_*` code
- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass (see `pyproject.toml` for
      the configured rule set -
      `select = ["E4","E7","E9","F","W","I","UP","B","S"]`, line length 88,
      double quotes)

## contrib/check-endpoint-exporter/check-endpoint.py

- [ ] N/A - this PR doesn't touch core request/output logic
- [ ] This is a bug fix or behavior change that also applies to the exporter's
      copy of the script, and I've mirrored it there
- [ ] This is exporter-specific and intentionally doesn't touch
      `check-endpoint.py`

(Ruff is configured to skip linting this file a second time since it's a
duplicate of the main script - see `extend-exclude` in `pyproject.toml`.)

## Anything reviewers should know

<!-- e.g.: "I left the S110 try/except/pass in X as-is - it's the same
intentional swallow pattern as CERTINFO/header-decode/HTTP-version lookup,
not a new one" - or note if you added a NEW bare except and why it's safe,
since that's the one pattern this codebase is deliberately careful about. -->

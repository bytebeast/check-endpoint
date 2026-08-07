# Changelog

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

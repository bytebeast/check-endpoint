#!/usr/bin/env python3
"""
Compute the next version from conventional commits, then write it into the
files that carry it and prepend a CHANGELOG entry.

Ticket and issue references are picked up from any of four placements, so the
old house style and the spec-conformant ones can coexist in one history:

    PROJ-123 fix(dns): handle an empty remote_ip      legacy prefix
    fix(dns): handle an empty remote_ip (PROJ-123)    trailing, most common
    fix(dns): handle an empty remote_ip               footer, spec-canonical
    <blank>
    Refs: PROJ-123

    fix(dns): handle an empty remote_ip (PROJ-123)    both, for Jira link
    <blank>                                           plus GitHub auto-close
    Closes #42

The legacy prefix has to be stripped before the conventional part is parsed,
because parsers anchor `type(scope):` at the start of the subject and would
otherwise see no releasable commits at all.

Bump rules:
    !  or  "BREAKING CHANGE:" footer        ->  major
    feat                                    ->  minor
    fix, perf                               ->  patch
    anything else (docs, chore, ci, ...)    ->  no release

Exit codes:
    0  a release is due; version written to stdout and $GITHUB_OUTPUT
    1  an error
    2  nothing to release
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# Files carrying the version, and the pattern that matches it. Each pattern
# must capture the version number itself (and ONLY the version number) in a
# group named "version" - nothing else about the pattern's shape matters.
# write_versions()/current_version() splice text in/out using that named
# group's span, so prefixes/suffixes of any length or structure (a closing
# quote, a trailing "-" in a badge URL, or nothing at all) are preserved
# untouched without the two ends needing matching group counts.
VERSION_FILES = [
    (
        "check-endpoint.py",
        re.compile(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"'),
    ),
    (
        "pyproject.toml",
        re.compile(r'(?m)^version\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"'),
    ),
    (
        "contrib/check-endpoint-exporter/check-endpoint.py",
        re.compile(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"'),
    ),
    (
        "contrib/check-endpoint-cli/check-endpoint.py",
        re.compile(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"'),
    ),
    (
        "README.md",
        re.compile(r"Version-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-"),
    ),
    (
        "contrib/check-endpoint-exporter/helm/check-endpoint-exporter/Chart.yaml",
        re.compile(r'(?m)^appVersion:\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"'),
    ),
    (
        "contrib/check-endpoint-exporter/kubernetes/deployment.yaml",
        re.compile(r"check-endpoint-exporter:(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"),
    ),
]

CHANGELOG = "CHANGELOG.md"

# Jira issue keys. Narrow this to your real project keys, e.g.
# r"(?:PROJ|OPS|INFRA)", and false positives become impossible. The generic
# pattern below also matches things like UTF-8 and HTTP-2, which is why refs
# are only ever read from the three designated positions and never from
# free-running description text.
JIRA_PROJECT = r"[A-Z][A-Z0-9]+"
JIRA = rf"{JIRA_PROJECT}-\d+"
GH_ISSUE = r"#\d+"
REF = rf"(?:{JIRA}|{GH_ISSUE})"

# Optional legacy Jira prefix, then the conventional-commit part.
COMMIT_RE = re.compile(
    rf"^(?:(?P<prefix_ref>{JIRA})\s+)?"
    r"(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]*)\))?"
    r"(?P<bang>!)?"
    r":\s*(?P<desc>.+)$"
)

# A trailing "(PROJ-123)" or "(PROJ-1, #42)" at the very end of the
# description. Required to be refs and nothing else, so an ordinary
# parenthetical such as "(see the TLS notes)" is left alone.
TRAILING_REF_RE = re.compile(rf"\s*\((?P<refs>{REF}(?:\s*,\s*{REF})*)\)\s*$")

# Footer lines in git trailer form. The spec allows ": " or " #" as the
# separator, so both "Refs: PROJ-1" and "Closes #42" are matched. The
# separator is required: a prose line such as "Fixes the UTF-8 path" must
# not turn UTF-8 into a ticket ref.
FOOTER_RE = re.compile(
    r"(?mi)^(?:refs?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r"(?::[ \t]*| (?=#))(?P<value>.+)$"
)

REF_RE = re.compile(REF)

MINOR_TYPES = {"feat"}
PATCH_TYPES = {"fix", "perf"}

# Section headings in the changelog, in the order they should appear.
SECTIONS = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
]


def run(*args, check=True):
    """stdout of a command. A failure exits with its stderr unless
    check=False, so a broken checkout cannot pass as "nothing to release"."""
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and proc.returncode:
        sys.exit(f"error: {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def require_full_history():
    """A shallow clone has no tags, so it would look like a first release
    and the changelog would hold whatever history happened to be fetched."""
    if run("git", "rev-parse", "--is-shallow-repository") == "true":
        sys.exit("error: shallow clone; check out with fetch-depth: 0")


def last_tag():
    """Most recent v* tag reachable from HEAD, or None on the first release."""
    # describe exits non-zero when there is no tag yet, which is expected
    tag = run("git", "describe", "--tags", "--abbrev=0", "--match", "v*", check=False)
    return tag or None


def commits_since(tag):
    """[(subject, body)] for every commit after `tag`, oldest first."""
    span = f"{tag}..HEAD" if tag else "HEAD"
    sep = "\x1e"
    out = run("git", "log", "--reverse", f"--format=%s%x1f%b{sep}", span)
    entries = []
    for chunk in out.split(sep):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        subject, _, body = chunk.partition("\x1f")
        entries.append((subject.strip(), body.strip()))
    return entries


def collect_refs(prefix_ref, desc, body):
    """
    Every ticket or issue reference on a commit, in the order encountered,
    de-duplicated, gathered from the three designated positions only.

    Returns (refs, cleaned_description). The trailing "(PROJ-123)" is removed
    from the description so the changelog does not print it twice.
    """
    refs = []

    def add(value):
        for r in REF_RE.findall(value or ""):
            if r not in refs:
                refs.append(r)

    add(prefix_ref)

    if m := TRAILING_REF_RE.search(desc):
        add(m.group("refs"))
        desc = desc[: m.start()].rstrip()

    for m in FOOTER_RE.finditer(body or ""):
        add(m.group("value"))

    return refs, desc


def parse(subject, body):
    """Return a dict for a conventional commit, or None if it is not one."""
    m = COMMIT_RE.match(subject)
    if not m:
        return None
    d = m.groupdict()
    d["refs"], d["desc"] = collect_refs(d["prefix_ref"], d["desc"], body)
    # The spec makes BREAKING-CHANGE a synonym for BREAKING CHANGE.
    d["breaking"] = bool(d["bang"]) or bool(
        re.search(r"(?m)^BREAKING[ -]CHANGE:", body)
    )
    return d


def decide_bump(parsed):
    """major / minor / patch, or None when nothing warrants a release."""
    if any(c["breaking"] for c in parsed):
        return "major"
    if any(c["type"] in MINOR_TYPES for c in parsed):
        return "minor"
    if any(c["type"] in PATCH_TYPES for c in parsed):
        return "patch"
    return None


def current_version():
    """Read the version from the first VERSION_FILES entry that exists. Only
    used before the first tag; after that the tag is the source of truth."""
    for name, pattern in VERSION_FILES:
        path = Path(name)
        if not path.is_file():
            continue
        m = pattern.search(path.read_text())
        if m:
            return m.group("version"), name
    sys.exit("error: no version found in any of the version files")


def bump(version, level):
    major, minor, patch = (int(x) for x in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_versions(new, dry_run):
    """Rewrite every version file. Returns the paths actually changed.

    A listed file that is missing or no longer matches its pattern is an
    error, not a skip: skipping it shipped a release with that file still on
    the old version. Every file is checked before any is written."""
    updates = []
    for name, pattern in VERSION_FILES:
        path = Path(name)
        if path.is_symlink():
            # Same inode as its target, which is already in the list.
            continue
        m = pattern.search(path.read_text()) if path.is_file() else None
        if not m:
            sys.exit(f"error: no version found in {name}; update VERSION_FILES")
        start, end = m.span("version")
        updates.append((name, m.string[:start] + new + m.string[end:]))

    changed = []
    for name, new_text in updates:
        if new_text != Path(name).read_text():
            if not dry_run:
                Path(name).write_text(new_text)
            changed.append(name)
    return changed


def changelog_entry(version, parsed):
    lines = [f"## v{version} ({date.today().isoformat()})", ""]
    seen_types = {c["type"] for c in parsed}
    for type_key, heading in SECTIONS:
        group = [c for c in parsed if c["type"] == type_key]
        if not group:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        for c in group:
            scope = f"**{c['scope']}**: " if c["scope"] else ""
            refs = f" ({', '.join(c['refs'])})" if c["refs"] else ""
            lines.append(f"- {scope}{c['desc']}{refs}")
        lines.append("")

    breaking = [c for c in parsed if c["breaking"]]
    if breaking:
        lines.append("### Breaking Changes")
        lines.append("")
        for c in breaking:
            scope = f"**{c['scope']}**: " if c["scope"] else ""
            lines.append(f"- {scope}{c['desc']}")
        lines.append("")

    unlisted = seen_types - {k for k, _ in SECTIONS}
    if unlisted:
        lines.append(f"<!-- also in this release: {', '.join(sorted(unlisted))} -->")
        lines.append("")
    return "\n".join(lines)


def write_changelog(entry, dry_run):
    path = Path(CHANGELOG)
    header = "# Changelog\n\n"
    if path.is_file():
        existing = path.read_text()
        body = existing[len(header) :] if existing.startswith(header) else existing
        new_text = header + entry + "\n" + body.lstrip("\n")
    else:
        new_text = header + entry
    if not dry_run:
        path.write_text(new_text)
    return CHANGELOG


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, change nothing")
    ap.add_argument(
        "--force", choices=["major", "minor", "patch"], help="skip commit parsing"
    )
    args = ap.parse_args()

    require_full_history()
    tag = last_tag()
    raw = commits_since(tag)
    parsed = [p for p in (parse(s, b) for s, b in raw) if p]

    print(f"last tag:  {tag or '(none, first release)'}")
    print(f"commits:   {len(raw)} since tag, {len(parsed)} conventional")

    level = args.force or decide_bump(parsed)
    if not level:
        print("nothing to release: no feat/fix/perf or breaking change found")
        return 2

    # Bump from the tag, not the files: a hand-edited version is left
    # untagged by the release workflow's CHANGELOG gate, and bumping from it
    # would skip version numbers. write_versions() then overwrites it.
    if tag:
        old, source = tag.removeprefix("v"), f"tag {tag}"
    else:
        old, source = current_version()
    new = bump(old, level)
    print(f"bump:      {level}  {old} -> {new}   (read from {source})")

    changed = write_versions(new, args.dry_run)
    changed.append(write_changelog(changelog_entry(new, parsed), args.dry_run))
    print("files:     " + ", ".join(changed))

    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as fh:
            fh.write(f"version={new}\ntag=v{new}\nfiles={' '.join(changed)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

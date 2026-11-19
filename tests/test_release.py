"""Tests for .github/scripts/release.py. Run with:  python3 -m pytest -q tests

Standard library only; the main() tests build a throwaway git repo in tmp_path.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

APP_RE = re.compile(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"')

# ----------------------------------------------------------- commit parsing


def test_footer_refs_ignore_prose_lines():
    body = "Fixes the UTF-8 decode and HTTP-2 upgrade path.\n\nRefs: AH-1"
    assert release.parse("fix(dns): handle empty ip (AH-2)", body)["refs"] == [
        "AH-2",
        "AH-1",
    ]


def test_footer_refs_in_trailer_form():
    body = "Closes #42\nFixes: AH-3, #7\nResolved: AH-4"
    assert release.parse("fix: x", body)["refs"] == ["#42", "AH-3", "#7", "AH-4"]


def test_breaking_change_footer_both_spellings():
    assert release.parse("feat: x", "BREAKING-CHANGE: dropped y")["breaking"]
    assert release.parse("feat: x", "BREAKING CHANGE: dropped y")["breaking"]


# ----------------------------------------------------------- version files


def _version_files(tmp_path, monkeypatch, files):
    monkeypatch.chdir(tmp_path)
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(release, "VERSION_FILES", [(n, APP_RE) for n in files])


def test_write_versions_refuses_a_file_that_stopped_matching(tmp_path, monkeypatch):
    _version_files(
        tmp_path,
        monkeypatch,
        {"a.py": 'APP_VERSION = "1.0.0"\n', "b.py": "APP_VERSION = 'v1.0.0'\n"},
    )
    with pytest.raises(SystemExit, match="b.py"):
        release.write_versions("1.0.1", dry_run=False)
    # nothing written: a half-bumped tree is worse than none
    assert (tmp_path / "a.py").read_text() == 'APP_VERSION = "1.0.0"\n'


def test_write_versions_refuses_a_missing_file(tmp_path, monkeypatch):
    _version_files(tmp_path, monkeypatch, {"a.py": 'APP_VERSION = "1.0.0"\n'})
    monkeypatch.setattr(
        release, "VERSION_FILES", [("a.py", APP_RE), ("gone.py", APP_RE)]
    )
    with pytest.raises(SystemExit, match="gone.py"):
        release.write_versions("1.0.1", dry_run=True)


def test_write_versions_bumps_every_file(tmp_path, monkeypatch):
    _version_files(
        tmp_path,
        monkeypatch,
        {"a.py": 'APP_VERSION = "1.0.0"\n', "b.py": 'APP_VERSION = "0.9.0"\n'},
    )
    assert release.write_versions("1.0.1", dry_run=False) == ["a.py", "b.py"]
    assert 'APP_VERSION = "1.0.1"' in (tmp_path / "b.py").read_text()


# ----------------------------------------------------------- main()


def _git(repo, *args):
    # --no-verify and a non-main branch keep local commit hooks out of it
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _commit(repo, message):
    _git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-am", message)


def _main(repo, monkeypatch, capsys, *argv):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "argv", ["release.py", *argv])
    monkeypatch.setattr(release, "VERSION_FILES", [("app.py", APP_RE)])
    rc = release.main()
    return rc, capsys.readouterr().out


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "work")
    (tmp_path / "app.py").write_text('APP_VERSION = "3.3.0"\n')
    _git(tmp_path, "add", "app.py")
    _commit(tmp_path, "chore(release): v3.3.0")
    _git(tmp_path, "tag", "-a", "v3.3.0", "-m", "v3.3.0")
    return tmp_path


def test_hand_edited_version_does_not_skip_versions(repo, monkeypatch, capsys):
    (repo / "app.py").write_text('APP_VERSION = "3.5.0"\n')
    _commit(repo, "chore: bump version by hand")
    _commit(repo, "feat: something")
    rc, out = _main(repo, monkeypatch, capsys)
    assert rc == 0
    assert "3.3.0 -> 3.4.0" in out
    assert (repo / "app.py").read_text() == 'APP_VERSION = "3.4.0"\n'


def test_first_release_reads_version_from_files(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q", "-b", "work")
    (tmp_path / "app.py").write_text('APP_VERSION = "0.1.0"\n')
    _git(tmp_path, "add", "app.py")
    _commit(tmp_path, "fix: first")
    rc, out = _main(tmp_path, monkeypatch, capsys, "--dry-run")
    assert rc == 0 and "0.1.0 -> 0.1.1" in out


def test_nothing_to_release(repo, monkeypatch, capsys):
    _commit(repo, "docs: words")
    rc, _ = _main(repo, monkeypatch, capsys, "--dry-run")
    assert rc == 2


def test_shallow_clone_is_refused(repo, tmp_path_factory, monkeypatch, capsys):
    # a shallow checkout has no tags, so it would silently look like a first
    # release and parse whatever history happened to be fetched
    _commit(repo, "feat: something")
    shallow = tmp_path_factory.mktemp("shallow")
    _git(shallow, "clone", "-q", "--depth", "1", repo.as_uri(), ".")
    with pytest.raises(SystemExit, match="shallow"):
        _main(shallow, monkeypatch, capsys, "--dry-run")


def test_git_failure_is_loud(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    (tmp_path / "app.py").write_text('APP_VERSION = "1.0.0"\n')
    with pytest.raises(SystemExit, match="git"):
        _main(tmp_path, monkeypatch, capsys, "--dry-run")

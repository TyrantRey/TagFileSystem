# Code by AkinoAlice@TyrantRey

import importlib
import re
import subprocess
from pathlib import Path

import pytest

from tag_file_system import version
from tests.fakerepo import git, make_repo


def test_version_is_the_pyproject_version():
    repo = version.repo_dir()
    assert repo is not None, "the suite runs from a checkout"
    assert version.VERSION == version.version_from_pyproject(repo / "pyproject.toml")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.VERSION)


def test_commit_is_head_of_the_checkout():
    repo = version.repo_dir()
    assert repo is not None
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert version.COMMIT == expected
    assert re.fullmatch(r"\d+\.\d+\.\d+ \([0-9a-f]{7}\)", version.describe())


def test_git_head_reads_loose_packed_and_worktree_refs(tmp_path: Path):
    repo = make_repo(tmp_path, at=None)  # on master
    head = repo.head()
    assert version.git_head(repo.path) == head  # loose ref

    git(repo.path, "pack-refs", "--all")
    assert not (repo.path / ".git" / "refs" / "heads" / "master").exists()
    assert version.git_head(repo.path) == head  # packed-refs

    repo.checkout("v0.9.0")
    assert version.git_head(repo.path) == repo.tags["v0.9.0"]  # detached

    worktree = tmp_path / "wt"
    git(repo.path, "worktree", "add", "-q", str(worktree), "master")
    assert (worktree / ".git").is_file()  # a gitdir pointer
    assert version.git_head(worktree) == head


def test_git_head_outside_a_checkout_is_none(tmp_path: Path):
    assert version.git_head(tmp_path) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ('[project]\nname = "x"\nversion = "1.2.3"\n', "1.2.3"),
        ('[tool.other]\nversion = "9"\n', None),
        ("not toml [", None),
    ],
)
def test_version_from_pyproject(tmp_path: Path, text: str, expected: str | None):
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    assert version.version_from_pyproject(path) == expected


def test_identity_formats_short_hashes():
    assert version.identity("0.2.0", "abcdef0123456789") == "0.2.0 (abcdef0)"
    assert version.identity("0.2.0", None) == "0.2.0 (unknown)"
    assert version.describe() == version.identity(version.VERSION, version.COMMIT)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("a" * 40, "a" * 40),
        ("ABCDEF" + "0" * 34, "abcdef" + "0" * 34),  # as git prints it
        (" " + "b" * 40 + "\n", "b" * 40),
        ("abc1234", None),  # a short hash is not an identity
        ("v0.5.0", None),
        ("", None),
    ],
)
def test_commit_from_env_takes_only_a_full_hash(value: str, expected: str | None):
    """The Docker image stamps TFS_COMMIT (DESIGN/v0-5-0.md §10.2); anything
    but a full hash is "unknown", never a guess."""
    assert version.commit_from_env({version.COMMIT_ENV: value}) == expected


def test_commit_from_env_without_the_variable_is_none():
    assert version.commit_from_env({}) is None


def test_a_checkout_head_beats_the_stamp(monkeypatch: pytest.MonkeyPatch):
    """The image sets TFS_COMMIT because it has no checkout; a checkout
    reports its HEAD whatever the environment says."""
    monkeypatch.setenv(version.COMMIT_ENV, "f" * 40)
    reloaded = importlib.reload(version)
    try:
        assert reloaded.REPO is not None
        assert reloaded.COMMIT == version.git_head(reloaded.REPO)
        assert reloaded.COMMIT != "f" * 40
    finally:
        monkeypatch.delenv(version.COMMIT_ENV)
        importlib.reload(version)

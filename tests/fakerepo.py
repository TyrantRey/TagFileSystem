# Code by AkinoAlice@TyrantRey

"""A tiny git checkout shaped like this project — ``pyproject.toml``, the
``SCHEMA_VERSION`` line, a marker file that differs per release — with two
annotated release tags and a bare ``origin``, for the self-update tests."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_FILE = "src/tag_file_system/database/migrations.py"
RELEASES = (("0.9.0", 1, "old"), ("1.0.0", 2, "new"))


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


@dataclass
class FakeRepo:
    path: Path
    origin: Path
    tags: dict[str, str] = field(default_factory=dict)  # tag -> commit

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD")

    def marker(self) -> str:
        return (self.path / "marker.txt").read_text(encoding="utf-8").strip()

    def checkout(self, ref: str) -> None:
        git(self.path, "checkout", "-q", ref)


def write_release(repo: Path, version: str, schema: int, marker: str) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "{version}"\n', encoding="utf-8"
    )
    schema_file = repo / SCHEMA_FILE
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(
        f'"""Fake migrations module."""\n\nSCHEMA_VERSION = {schema}\n',
        encoding="utf-8",
    )
    (repo / "marker.txt").write_text(marker, encoding="utf-8")


def make_repo(
    base: Path,
    releases: tuple[tuple[str, int, str], ...] = RELEASES,
    at: str | None = "v0.9.0",
) -> FakeRepo:
    """``base/repo`` with one commit and annotated tag per release, plus a
    lightweight ``latest`` and a non-release annotated ``docs-1`` at the last
    commit; ``base/origin.git`` is its bare clone, added as ``origin``.
    ``at`` is checked out last (detached at that tag)."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "master")
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(repo, "config", key, value)
    tags: dict[str, str] = {}
    for version, schema, marker in releases:
        write_release(repo, version, schema, marker)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"release {version}")
        git(repo, "tag", "-a", f"v{version}", "-m", f"v{version}")
        tags[f"v{version}"] = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "latest")
    git(repo, "tag", "-a", "docs-1", "-m", "not a release")
    origin = base / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(repo), str(origin)],
        check=True,
        capture_output=True,
    )
    git(repo, "remote", "add", "origin", str(origin))
    if at is not None:
        git(repo, "checkout", "-q", at)
    return FakeRepo(path=repo, origin=origin, tags=tags)

# Code by AkinoAlice@TyrantRey

"""Version identity (DESIGN/v0-2-0.md §3): tags select, hashes record.

``VERSION`` is the ``A.B.C`` string of the code on disk and ``COMMIT`` the
git commit it was loaded from (``None`` outside a checkout). Both are fixed
at import, so a long-running daemon keeps reporting the code it actually
runs even after the checkout underneath it moved on.

The version is read from ``pyproject.toml`` in the checkout before the
installed metadata is consulted: ``tfs upgrade`` syncs with
``--no-install-project`` (see ``updater.py``), which leaves the editable
install's ``.dist-info`` at the previous version on purpose.
"""

import re
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path

PACKAGE = "tag_file_system"

_HASH = re.compile(r"[0-9a-f]{40}")


def repo_dir() -> Path | None:
    """The git checkout this package runs from (``<repo>/src/tag_file_system``
    layout), or ``None`` for any other kind of install."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists():
        return candidate
    return None


def version_from_pyproject(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    value = project.get("version") if isinstance(project, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _version_from_metadata() -> str | None:
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def git_head(repo: Path) -> str | None:
    """The commit ``HEAD`` points at, read from ``.git`` without running git
    (loose refs, ``packed-refs``, worktree pointers); ``git rev-parse`` is
    the fallback for layouts this does not understand."""
    git_dir = repo / ".git"
    try:
        if git_dir.is_file():  # a worktree or submodule: "gitdir: <path>"
            text = git_dir.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return _git_rev_parse(repo)
            git_dir = (repo / text[len("gitdir:") :].strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return _git_rev_parse(repo)
    if not head.startswith("ref:"):
        return head if _HASH.fullmatch(head) else _git_rev_parse(repo)
    ref = head[len("ref:") :].strip()

    common = git_dir
    try:
        common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
    except OSError:
        pass
    for base in (git_dir, common):
        try:
            value = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _HASH.fullmatch(value):
            return value
    try:
        packed = (common / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return _git_rev_parse(repo)
    for line in packed:
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref and _HASH.fullmatch(parts[0]):
            return parts[0]
    return _git_rev_parse(repo)


def _git_rev_parse(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and _HASH.fullmatch(value) else None


def short(commit: str | None) -> str:
    return commit[:7] if commit else "unknown"


def identity(version: str, commit: str | None) -> str:
    """``0.2.0 (abc1234)``: a version and the commit it came from."""
    return f"{version} ({short(commit)})"


def describe() -> str:
    """What ``tfs --version`` prints: the identity of the code on disk."""
    return identity(VERSION, COMMIT)


REPO = repo_dir()
VERSION: str = (
    (version_from_pyproject(REPO / "pyproject.toml") if REPO else None)
    or _version_from_metadata()
    or "0.0.0"
)
COMMIT: str | None = git_head(REPO) if REPO else None

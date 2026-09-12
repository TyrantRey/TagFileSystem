# Code by AkinoAlice@TyrantRey

"""The 0.4.0 commands: ``tfs explain``, ``tfs migrate``, the ``init``
skeleton (DESIGN/v0-4-0.md §9–§10)."""

import json
import socket
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.cli import app
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.functions import load_file
from tag_file_system.root import FUNCTIONS_FILE, Root
from tag_file_system.services.daemon import Daemon

runner = CliRunner()

PHOTO = """
    from tag_file_system import action

    @action.added()
    def resize(path, metadata, ctx, width: int, quality: int = 80):
        return width

    @action.tagged("photo")
    def on_photo(path, metadata, ctx):
        return "photo"
"""

MAKE_COPY = """
    from tag_file_system import action

    @action.added()
    def run(path, metadata, ctx, suffix: str, dst: action.Remote):
        return suffix

    @action.removed(on_move=True)
    def gone(path, metadata, ctx, suffix: str, dst: action.Remote):
        pass
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


def tfs(*args: str):
    return runner.invoke(app, [str(a) for a in args])


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Root:
    result = tfs("init", str(tmp_path / "vault"))
    assert result.exit_code == 0, result.output
    root = Root(tmp_path / "vault")
    Config(daemon=DaemonConfig(port=free_port(), stop_timeout_seconds=0.5)).write(
        root.config_path
    )
    write(root.script_dir / "photo.py", PHOTO)
    write(
        root.path / "own" / FUNCTIONS_FILE,
        """
        version: 1
        functions:
          photo:
            resize: {width: 800, exclude: [{tag: draft}]}
            on_photo: {}
        """,
    )
    write(root.path / "own" / "a--photo.txt", "a")
    write(root.path / "own" / "b--photo--draft.txt", "b")
    return root


@pytest.fixture
def daemon(root: Root):
    d = Daemon(root, control=True, poll_ms=50)
    d.startup()
    thread = threading.Thread(target=d.run_forever, daemon=True)
    thread.start()
    yield d
    d.request_stop()
    thread.join(10)


# -------------------------------------------------------------------- init


def test_init_writes_a_valid_skeleton(tmp_path: Path):
    result = tfs("init", str(tmp_path / "vault"))

    assert result.exit_code == 0, result.output
    skeleton = tmp_path / "vault" / FUNCTIONS_FILE
    assert skeleton.exists() and "functions:" in result.output
    parsed = load_file(skeleton, "")
    assert parsed.entries == [] and parsed.invalid == []
    assert "DESIGN/v0-4-0.md" in skeleton.read_text(encoding="utf-8")

    # an existing file is never overwritten
    other = tmp_path / "other"
    other.mkdir()
    (other / FUNCTIONS_FILE).write_text("version: 1\nfunctions: {}\n", encoding="utf-8")
    assert tfs("init", str(other)).exit_code == 0
    assert (other / FUNCTIONS_FILE).read_text(encoding="utf-8") == (
        "version: 1\nfunctions: {}\n"
    )


# ----------------------------------------------------------------- explain


def test_explain_through_the_daemon(root: Root, daemon: Daemon):
    result = tfs("explain", "own/b--photo--draft.txt", "--root", str(root.path))

    assert result.exit_code == 0, result.output
    assert "tags: draft, photo" in result.output
    assert (
        "photo.on_photo()" in result.output and f"own/{FUNCTIONS_FILE}" in result.output
    )
    assert "suppressed:" in result.output and "excluded by tag draft" in result.output
    assert "tagged defaults:" in result.output and "suppressed by" in result.output

    as_json = json.loads(
        tfs("explain", "own/a--photo.txt", "--root", str(root.path), "--json").output
    )
    assert as_json["known"] is True and as_json["tags"] == ["photo"]
    assert as_json["source"] == "daemon"
    assert [e["handler"] for e in as_json["applied"]] == ["resize", "on_photo"]
    assert as_json["suppressed"] == []
    assert "no daemon running" not in result.output

    absolute = tfs(
        "explain", str(root.path / "own" / "a--photo.txt"), "--root", str(root.path)
    )
    assert absolute.exit_code == 0 and "photo.resize(width=800)" in absolute.output


def test_explain_offline_and_its_argument_checks(root: Root):
    result = tfs("explain", "own/new--photo.txt", "--root", str(root.path))

    assert result.exit_code == 0, result.output
    assert "(no daemon running:" in result.output  # the disk, not a daemon, answered
    assert "(from the name; not indexed yet)" in result.output
    assert "photo.resize(width=800)" in result.output
    assert "added" in result.output
    as_json = json.loads(
        tfs("explain", "own/new--photo.txt", "--root", str(root.path), "--json").output
    )
    assert as_json["source"] == "disk" and as_json["known"] is False

    nothing = tfs("explain", "elsewhere/x.txt", "--root", str(root.path))
    assert nothing.exit_code == 0 and "(nothing applies)" in nothing.output

    for bad in ("../x.txt", f"own/{FUNCTIONS_FILE}", ".", str(root.path.parent / "y")):
        refused = tfs("explain", bad, "--root", str(root.path))
        assert refused.exit_code == 2, bad


def test_list_offline_shows_functions_problems(root: Root):
    write(
        root.path / "bad" / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  nosuch: {run: {}}\n",
    )

    result = tfs("list", "--root", str(root.path))

    assert result.exit_code == 0, result.output
    assert "photo" in result.output and "resize" in result.output
    assert "[err] functions.unbound" in result.output


# ----------------------------------------------------------------- migrate


@pytest.fixture
def legacy(tmp_path: Path) -> Root:
    """A root laid out the v1 way: functions in the folder names."""
    result = tfs("init", str(tmp_path / "legacy"))
    assert result.exit_code == 0, result.output
    root = Root(tmp_path / "legacy")
    Config(remotes={"backup": str(tmp_path / "backup")}).write(root.config_path)
    write(root.script_dir / "make_copy.py", MAKE_COPY)
    write(root.script_dir / "photo.py", PHOTO)
    write(root.path / "@@make_copy__.jpg__backup" / "2024--trip" / "a.jpg", "a")
    write(root.path / "@@photo__800--raw" / "x" / "@@photo__400" / "b.txt", "b")
    write(root.path / "@@nosuch" / "c.txt", "c")
    write(root.path / "f@@copy.txt", "f")
    write(
        root.path / "done" / "@@photo__1" / FUNCTIONS_FILE,
        "version: 1\nfunctions: {}\n",
    )
    return root


def test_migrate_dry_run_shows_the_plan_and_writes_nothing(legacy: Root):
    result = tfs("migrate", "--root", str(legacy.path))

    assert result.exit_code == 0, result.output
    assert "would write 3 file(s):" in result.output
    assert "width: 800" in result.output and "width: 400" in result.output
    assert "suffix: .jpg" in result.output and "dst: backup" in result.output
    assert "@@nosuch: script/nosuch.py is not loaded" in result.output
    assert "f@@copy.txt: a function on a file name" in result.output
    assert f"done/@@photo__1: {FUNCTIONS_FILE} exists" in result.output
    assert "(dry run" in result.output
    assert not (legacy.path / "@@make_copy__.jpg__backup" / FUNCTIONS_FILE).exists()


def test_migrate_apply_writes_typed_entries_then_renames(legacy: Root):
    result = tfs("migrate", "--root", str(legacy.path), "--apply")

    assert result.exit_code == 0, result.output
    assert "wrote 3 file(s)" in result.output
    copy = load_file(legacy.path / "@@make_copy__.jpg__backup" / FUNCTIONS_FILE, "")
    assert [(e.ref, e.args) for e in copy.entries] == [
        ("make_copy.run", {"suffix": ".jpg", "dst": "backup"}),
        ("make_copy.gone", {"suffix": ".jpg", "dst": "backup"}),
    ]
    outer = load_file(legacy.path / "@@photo__800--raw" / FUNCTIONS_FILE, "")
    (resize,) = outer.entries
    assert resize.args == {"width": 800} and isinstance(resize.args["width"], int)
    inner = load_file(
        legacy.path / "@@photo__800--raw" / "x" / "@@photo__400" / FUNCTIONS_FILE, ""
    )
    assert inner.entries[0].args == {"width": 400}
    # the files are read back by the loader exactly as written by hand
    listed = tfs("list", "--root", str(legacy.path))
    assert "[err] functions." not in listed.output  # no unbound/signature problems

    again = tfs("migrate", "--root", str(legacy.path), "--apply")
    assert again.exit_code == 1 and "nothing to migrate" in again.output
    assert f"{FUNCTIONS_FILE} exists" in again.output

    dry = tfs("migrate", "--root", str(legacy.path), "--rename")
    assert dry.exit_code == 0 and "would rename 5 folder(s):" in dry.output
    assert "@@photo__800--raw -> --raw" in dry.output  # the tag stays on the folder
    assert "@@make_copy__.jpg__backup -> make_copy-.jpg-backup" in dry.output
    assert "done/@@photo__1 -> photo-1" in dry.output  # its own file travels with it
    assert (legacy.path / "@@nosuch").exists()

    renamed = tfs("migrate", "--root", str(legacy.path), "--rename", "--apply")
    assert renamed.exit_code == 0 and "renamed 5 folder(s)" in renamed.output
    assert (legacy.path / "--raw" / "x" / "photo-400" / "b.txt").exists()
    assert (legacy.path / "--raw" / "x" / "photo-400" / FUNCTIONS_FILE).exists()
    assert (legacy.path / "make_copy-.jpg-backup" / "2024--trip" / "a.jpg").exists()
    assert (legacy.path / "nosuch" / "c.txt").exists()
    assert (legacy.path / "done" / "photo-1" / FUNCTIONS_FILE).exists()
    assert not (legacy.path / "@@photo__800--raw").exists()
    assert (
        tfs("migrate", "--root", str(legacy.path), "--rename", "--apply").exit_code == 1
    )

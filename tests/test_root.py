# Code by AkinoAlice@TyrantRey

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path, PurePosixPath

import pytest

from tag_file_system.config import Config
from tag_file_system.root import (
    Lock,
    LockHeld,
    LockInfo,
    NotARoot,
    OutsideRoot,
    Root,
    RootError,
    RootExists,
    Zone,
    find_root,
    pid_alive,
)

HOST = socket.gethostname()


@pytest.fixture
def root(tmp_path: Path) -> Root:
    return Root.init(tmp_path / "vault")


# ------------------------------------------------------------------ layout


def test_init_creates_the_layout(root: Root):
    assert root.tfs_dir.is_dir()
    assert root.db_dir.is_dir()
    assert root.script_dir.is_dir()
    assert root.config_path.is_file()
    assert root.token_path.is_file()
    assert not root.lock_path.exists()
    assert root.db_path == root.tfs_dir / "db" / "system.db"
    assert sorted(p.name for p in root.tfs_dir.iterdir()) == [
        "config.toml",
        "db",
        "token",
    ]

    assert root.load_config() == Config()
    assert len(root.read_token()) >= 32


def test_init_does_not_touch_existing_content(tmp_path: Path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep--me.txt").write_text("data")
    (target / "script").mkdir()
    (target / "script" / "old.py").write_text("# keep")

    root = Root.init(target)

    assert (target / "keep--me.txt").read_text() == "data"
    assert (root.script_dir / "old.py").read_text() == "# keep"


def test_init_accepts_a_missing_directory(tmp_path: Path):
    root = Root.init(tmp_path / "new" / "deep")

    assert root.path == (tmp_path / "new" / "deep").resolve()
    assert root.config_path.is_file()


def test_init_refuses_inside_an_existing_root(root: Root):
    with pytest.raises(RootExists):
        Root.init(root.path)
    nested = root.path / "a" / "b"
    nested.mkdir(parents=True)
    with pytest.raises(RootExists):
        Root.init(nested)
    with pytest.raises(RootExists):
        Root.init(root.path / "does-not-exist-yet")


def test_init_refuses_above_an_existing_root(tmp_path: Path):
    inner = Root.init(tmp_path / "outer" / "inner")

    with pytest.raises(RootExists) as exc:
        Root.init(tmp_path / "outer")
    assert str(inner.path) in str(exc.value)
    assert not (tmp_path / "outer" / ".tfs").exists()


def test_init_refuses_files_in_the_way(tmp_path: Path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(RootError):
        Root.init(tmp_path / "file")

    target = tmp_path / "dir"
    target.mkdir()
    (target / ".tfs").write_text("not a dir")
    with pytest.raises(RootError):
        Root.init(target)
    assert (target / ".tfs").is_file()  # untouched


def test_partial_init_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def explode(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(Config, "write", explode)
    target = tmp_path / "vault"
    target.mkdir()
    (target / "data.txt").write_text("x")

    with pytest.raises(OSError):
        Root.init(target)

    assert not (target / ".tfs").exists()
    assert not (target / "script").exists()
    assert (target / "data.txt").exists()
    monkeypatch.undo()
    assert Root.init(target).config_path.is_file()  # retry works


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_token_is_owner_only(root: Root):
    assert root.token_path.stat().st_mode & 0o777 == 0o600


def test_read_token_on_a_broken_root_is_a_root_error(root: Root):
    root.token_path.unlink()
    with pytest.raises(RootError):
        root.read_token()


# --------------------------------------------------------------- discovery


def test_find_root_walks_up(root: Root):
    nested = root.path / "x" / "y"
    nested.mkdir(parents=True)

    assert find_root(nested) == root.path
    assert find_root(root.path) == root.path
    assert Root.discover(nested) == root
    assert Root.discover(root.path / "not" / "there") == root


def test_discover_outside_any_root_raises(tmp_path: Path):
    assert find_root(tmp_path) is None
    with pytest.raises(NotARoot):
        Root.discover(tmp_path)


def test_discover_defaults_to_cwd(root: Root, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(root.path)

    assert Root.discover() == root


# ------------------------------------------------------------------- paths


def test_relative_is_posix_and_absolute_roundtrips(root: Root):
    path = root.path / "2024--trip" / "img--raw.jpg"

    rel = root.relative(path)

    assert rel == PurePosixPath("2024--trip/img--raw.jpg")
    assert isinstance(rel, PurePosixPath)
    assert root.absolute(rel) == path
    assert root.absolute("2024--trip/img--raw.jpg") == path
    assert root.relative(root.path) == PurePosixPath()


def test_relative_normalizes_dot_dot_and_rejects_escapes(root: Root):
    assert root.relative(root.path / "a" / ".." / "b.txt") == PurePosixPath("b.txt")
    with pytest.raises(OutsideRoot):
        root.relative(root.path / ".." / "outside.txt")
    with pytest.raises(OutsideRoot):
        root.relative(root.path / "a" / ".." / ".." / "outside.txt")


def test_relative_rejects_paths_outside_the_root(root: Root, tmp_path: Path):
    with pytest.raises(OutsideRoot):
        root.relative(tmp_path / "elsewhere.txt")
    with pytest.raises(OutsideRoot):
        root.relative(root.path.parent)


def test_relative_accepts_other_spellings_of_the_root(root: Root):
    if sys.platform == "win32":
        odd = Path(str(root.path).lower()) / "A.txt"
        assert root.relative(odd) == PurePosixPath("A.txt")
    via_dots = root.path / "." / "sub" / "." / "f.txt"
    assert root.relative(via_dots) == PurePosixPath("sub/f.txt")


def test_relative_resolves_symlinked_roots(root: Root, tmp_path: Path):
    link = tmp_path / "link"
    try:
        link.symlink_to(root.path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    assert Root.discover(link) == root
    assert root.relative(link / "data.txt") == PurePosixPath("data.txt")
    assert root.zone(link / ".tfs" / "lock") == Zone.TFS


def test_absolute_rejects_anchored_and_escaping_keys(root: Root):
    for bad in ("../x", "a/../../x", "/etc/passwd", "C:/win", "\\\\srv\\share"):
        with pytest.raises(OutsideRoot):
            root.absolute(bad)


def test_zone(root: Root):
    assert root.zone(root.db_path) == Zone.TFS
    assert root.zone(root.tfs_dir) == Zone.TFS
    assert root.zone(root.config_path) == Zone.TFS
    assert root.zone(root.tfs_dir / "lock.123.tmp") == Zone.TFS
    assert root.zone(root.script_dir / "make_copy.py") == Zone.SCRIPT
    assert root.zone(root.script_dir) == Zone.SCRIPT
    assert root.zone(root.path / "a" / "b.txt") == Zone.DATA
    assert root.zone(root.path / "scripts" / "x.py") == Zone.DATA  # not "script"
    assert root.zone(root.path / ".tfs-not" / "x") == Zone.DATA
    assert root.zone(root.path) == Zone.DATA
    # a nested .tfs anywhere is never data (protects against foreign roots)
    assert root.zone(root.path / "sub" / ".tfs" / "db" / "x.db") == Zone.TFS
    assert root.zone(root.path / "script" / ".." / ".tfs" / "lock") == Zone.TFS


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystem")
def test_zone_ignores_case_where_the_filesystem_does(root: Root):
    assert root.zone(root.path / ".TFS" / "lock") == Zone.TFS
    assert root.zone(root.path / "Script" / "x.py") == Zone.SCRIPT


# -------------------------------------------------------------------- lock


def write_lock(root: Root, pid: int, hostname: str, age: float = 0, **extra) -> None:
    root.lock_path.write_text(
        json.dumps(
            {"pid": pid, "hostname": hostname, "created_at": time.time() - age, **extra}
        )
    )


def dead_pid() -> int:
    """A pid that refers to a process that has already exited."""
    popen = subprocess.Popen([sys.executable, "-c", "pass"])
    popen.wait()
    return popen.pid


@pytest.fixture
def sleeper():
    """A live child process for the duration of the test."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    proc.kill()
    proc.wait()


def test_pid_alive(sleeper: subprocess.Popen):
    assert pid_alive(os.getpid())
    assert pid_alive(sleeper.pid)
    assert not pid_alive(-1)
    assert not pid_alive(0)
    assert not pid_alive(2**31)
    assert not pid_alive(2**40)
    assert not pid_alive(dead_pid())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access rights")
def test_pid_alive_treats_protected_processes_as_alive():
    assert pid_alive(4)  # the System process: alive, not openable


def test_acquire_writes_pid_and_host(root: Root):
    lock = Lock(root)

    info = lock.acquire()

    assert info.pid == os.getpid()
    assert info.hostname == HOST
    assert info.is_mine()
    on_disk = json.loads(root.lock_path.read_text())
    assert on_disk["pid"] == os.getpid()
    assert lock.holder() == info
    assert not list(root.tfs_dir.glob("lock.*"))  # no temp/stale leftovers

    lock.release()
    assert not root.lock_path.exists()
    assert lock.holder() is None


def test_second_acquire_by_a_live_process_is_refused(
    root: Root, sleeper: subprocess.Popen
):
    write_lock(root, pid=sleeper.pid, hostname=HOST)

    with pytest.raises(LockHeld) as exc:
        Lock(root).acquire()

    assert exc.value.info.pid == sleeper.pid
    with pytest.raises(LockHeld):
        Lock(root).acquire(force=True)  # force never displaces a live local pid
    assert Lock(root).read().pid == sleeper.pid  # type: ignore[union-attr]

    # a live lock from *another* host is what --force is for
    write_lock(root, pid=sleeper.pid, hostname="other-nas")
    assert Lock(root).acquire(force=True).is_mine()


def test_live_lock_on_this_host_never_ages_out(root: Root, sleeper: subprocess.Popen):
    write_lock(root, pid=sleeper.pid, hostname=HOST, age=8 * 3600)

    assert Lock(root).holder() is not None
    with pytest.raises(LockHeld):
        Lock(root).acquire()


def test_reacquire_by_the_same_process_is_fine(root: Root):
    first = Lock(root).acquire()
    second = Lock(root).acquire()

    assert first.is_mine() and second.is_mine()


def test_dead_pid_on_this_host_is_stale(root: Root):
    write_lock(root, pid=dead_pid(), hostname=HOST)

    assert Lock(root).holder() is None
    assert Lock(root).acquire().is_mine()


def test_lock_from_another_host_is_held_until_old(root: Root):
    write_lock(root, pid=12345, hostname="other-nas", age=60)
    with pytest.raises(LockHeld):
        Lock(root).acquire()

    write_lock(root, pid=12345, hostname="other-nas", age=7 * 3600)
    assert Lock(root).acquire().is_mine()


def test_stale_after_is_configurable(root: Root):
    write_lock(root, pid=12345, hostname="other-nas", age=120)

    with pytest.raises(LockHeld):
        Lock(root, stale_after=600).acquire()
    assert Lock(root, stale_after=60).acquire().is_mine()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        "null",
        json.dumps({"pid": "x"}),
        json.dumps({"pid": True, "hostname": "h", "created_at": 1.0}),
        json.dumps({"pid": 3.9, "hostname": "h", "created_at": 1.0}),
        json.dumps({"pid": 2**32, "hostname": "h", "created_at": 1.0}),
        json.dumps({"pid": 1, "hostname": None, "created_at": 1.0}),
        json.dumps({"pid": 1, "hostname": "", "created_at": 1.0}),
        '{"pid": 1, "hostname": "h", "created_at": NaN}',
        '{"pid": 1, "hostname": "h", "created_at": Infinity}',
        json.dumps({"pid": 1, "hostname": "h", "created_at": 1e300}),
        json.dumps({"pid": 1, "hostname": "h", "created_at": "soon"}),
        json.dumps({"pid": 1, "hostname": "h"}),
    ],
)
def test_corrupt_lock_files_are_stale(root: Root, content: str):
    root.lock_path.write_text(content)

    assert Lock(root).read() is None
    assert Lock(root).holder() is None
    assert Lock(root).acquire().is_mine()


def test_future_timestamps_are_held_not_evicted(root: Root):
    # A file server whose clock runs fast stamps locks in the future: still live.
    write_lock(root, pid=12345, hostname="other-nas", age=-3600)

    info = Lock(root).read()
    assert info is not None and info.age == 0.0
    with pytest.raises(LockHeld):
        Lock(root).acquire()

    # ...but an absurd value is corrupt
    write_lock(root, pid=12345, hostname="other-nas", age=-(20 * 365 * 24 * 3600))
    assert Lock(root).read() is None


def test_corrupt_lock_with_a_future_mtime_is_evictable(root: Root):
    root.lock_path.write_text("{corrupt")
    future = time.time() + 600
    os.utime(root.lock_path, (future, future))

    assert Lock(root).acquire().is_mine()


@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative paths")
def test_absolute_rejects_drive_relative_keys(root: Root):
    for bad in ("D:foo", "C:foo", "c:"):
        with pytest.raises(OutsideRoot):
            root.absolute(bad)


def test_lock_without_a_root_is_not_a_root(tmp_path: Path):
    with pytest.raises(NotARoot):
        Lock(Root(tmp_path / "nowhere")).acquire()


def test_lock_that_is_a_directory_is_an_error(root: Root):
    root.lock_path.mkdir()

    with pytest.raises(RootError):
        Lock(root).acquire()


def test_release_only_removes_own_lock(root: Root):
    lock = Lock(root)
    lock.acquire()
    # Someone else took over meanwhile (e.g. --force from another process).
    write_lock(root, pid=12345, hostname="other-host")

    lock.release()

    assert root.lock_path.exists()
    assert Lock(root).read() == LockInfo(
        pid=12345,
        hostname="other-host",
        created_at=json.loads(root.lock_path.read_text())["created_at"],
    )


def test_release_without_acquire_is_a_noop(root: Root):
    write_lock(root, pid=12345, hostname="other-nas")

    Lock(root).release()

    assert root.lock_path.exists()


def test_lock_as_context_manager(root: Root):
    with Lock(root) as info:
        assert info.is_mine()
        assert root.lock_path.exists()
    assert not root.lock_path.exists()


CONTENDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from tag_file_system.root import Lock, LockHeld, Root

    root, go = Root(Path(sys.argv[1])), Path(sys.argv[2])
    while not go.exists():
        time.sleep(0.005)
    try:
        Lock(root).acquire()
        print("WON")
        time.sleep(0.5)  # hold it while the others try
    except LockHeld:
        print("HELD")
    """
)


def test_concurrent_acquire_has_exactly_one_winner(root: Root, tmp_path: Path):
    script = tmp_path / "contender.py"
    script.write_text(CONTENDER)
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(root.path), str(go)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    time.sleep(0.3)  # let them all reach the spin loop
    go.write_text("go")
    outputs = [p.communicate(timeout=30)[0].strip() for p in procs]

    assert all(p.returncode == 0 for p in procs), outputs
    assert outputs.count("WON") == 1, outputs
    assert outputs.count("HELD") == 7, outputs

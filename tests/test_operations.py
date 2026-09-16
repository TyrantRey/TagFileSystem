# Code by AkinoAlice@TyrantRey

"""``tfs plan``, the confirmation threshold, ``rerun``, ``retry`` and the
file commands at the daemon level (DESIGN/v0-5-0.md §11.2, §11.3, §11.6)."""

import sys
import textwrap
from pathlib import Path

import pytest
from watchfiles import Change

from tag_file_system.addons.loader import MODULE_PREFIX
from tag_file_system.config import Config, DaemonConfig
from tag_file_system.core.interface.action import RunSource, RunStatus
from tag_file_system.root import FUNCTIONS_FILE, Root
from tag_file_system.services.api import BadRequest, Conflict, NotFound
from tag_file_system.services.daemon import Daemon
from tag_file_system.services.plan import plan_view
from tag_file_system.services.views import DISK, load_root


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


PHOTO = """
    from tag_file_system import action
    calls = []

    @action.added()
    def resize(path, metadata, ctx, width: int, quality: int = 80):
        if path.name.startswith("bad"):
            raise ValueError("cannot resize")
        calls.append(("resize", path.name, width))
        return width

    @action.tagged("photo")
    def on_photo(path, metadata, ctx):
        calls.append(("photo", path.name))
        return "photo"

    @action.removed(on_move=True)
    def gone(path, metadata, ctx, width: int, quality: int = 80):
        calls.append(("gone", path.name))
"""

OWN = """
    version: 1
    functions:
      photo:
        resize: {width: 800, exclude: [{tag: draft}]}
        gone: {width: 800}
        on_photo: {}
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Root:
    root = Root.init(tmp_path / "vault")
    Config(daemon=DaemonConfig(max_concurrent_runs=0, stop_timeout_seconds=0.5)).write(
        root.config_path
    )
    write(root.script_dir / "photo.py", PHOTO)
    write(root.path / "own" / FUNCTIONS_FILE, OWN)
    write(root.path / "own" / "a--photo.txt", "a")
    write(root.path / "own" / "b--photo--draft.txt", "b")
    write(root.path / "plain" / "c.txt", "c")
    return root


@pytest.fixture
def daemon(root: Root):
    d = Daemon(root)
    d.startup()
    yield d
    d.shutdown()


def calls(daemon: Daemon) -> list:
    addon = daemon.loader.addon_for("photo")
    assert addon is not None
    return addon.module.calls


def by_path(plan: dict) -> dict[str, dict]:
    return {f["path"]: f for f in plan["files"]}


# -------------------------------------------------------------------- plan


def test_plan_is_a_no_op_when_nothing_changed(daemon: Daemon):
    plan = daemon.plan()

    assert plan["source"] == "daemon" and plan["compared"] is True
    summary = plan["summary"]
    assert summary["files"] == 3 and summary["would_run"] == 0
    assert summary["already_ran"] == 3  # resize + on_photo on a, on_photo on b
    assert summary["leaving"] == 0 and summary["over_threshold"] is False
    assert plan["files"] == [] and plan["truncated"] is False
    assert [(f["file"], f["state"]) for f in plan["functions"]] == [
        ("own/.tfsfunctions.yaml", "unchanged")
    ]
    assert [(s["name"], s["state"]) for s in plan["scripts"]] == [
        ("photo", "unchanged")
    ]


def test_plan_explains_an_edited_functions_file(root: Root, daemon: Daemon):
    write(
        root.path / "own" / FUNCTIONS_FILE,
        """
        version: 1
        functions:
          photo:
            resize: {width: 900}
            on_photo: {}
        """,
    )
    write(
        root.path / "plain" / FUNCTIONS_FILE,
        """
        version: 1
        functions:
          photo:
            resize: {width: 100}
        """,
    )

    plan = daemon.plan()

    files = {f["file"]: f for f in plan["functions"]}
    assert files["own/.tfsfunctions.yaml"]["state"] == "changed"
    assert files["own/.tfsfunctions.yaml"]["added"] == ["photo.resize(width=900)"]
    assert sorted(files["own/.tfsfunctions.yaml"]["removed"]) == [
        "photo.gone(width=800)",
        "photo.resize(width=800)",
    ]
    assert files["plain/.tfsfunctions.yaml"]["state"] == "new"
    assert files["plain/.tfsfunctions.yaml"]["added"] == ["photo.resize(width=100)"]

    per_file = by_path(plan)
    a = per_file["own/a--photo.txt"]
    assert [(r["display"], r["hook"], r["reason"]) for r in a["would_run"]] == [
        (
            "photo.resize(width=900)",
            "added",
            "arguments changed from photo.resize(width=800)",
        )
    ]
    assert [r["display"] for r in a["skipped"]] == ['photo.on_photo(tag="photo")']
    assert a["skipped"][0]["reason"].startswith("already ran: ok at ")
    assert [(l["display"], l["reason"]) for l in a["leaving"]] == [
        ("photo.resize(width=800)", "entry removed from own/.tfsfunctions.yaml"),
        ("photo.gone(width=800)", "entry removed from own/.tfsfunctions.yaml"),
    ]
    b = per_file["own/b--photo--draft.txt"]  # newly included: the exclusion went
    assert [r["reason"] for r in b["would_run"]] == [
        "newly included: was excluded by tag draft"
    ]
    c = per_file["plain/c.txt"]
    assert [(r["display"], r["reason"]) for r in c["would_run"]] == [
        ("photo.resize(width=100)", "new entry from plain/.tfsfunctions.yaml")
    ]
    summary = plan["summary"]
    assert summary["would_run"] == 3 and summary["leaving"] == 3  # b leaves gone too
    assert summary["by_handler"][0] == {
        "display": "photo.resize(width=900)",
        "hook": "added",
        "would_run": 2,
        "already_ran": 0,
        "stale": 0,
    }
    # the daemon's own store is untouched by a plan
    assert daemon.functions.entries("own")[0].args == {"width": 800}
    assert "plain" not in daemon.functions.files


def test_plan_scopes_to_a_file_or_a_prefix(root: Root, daemon: Daemon):
    write(
        root.path / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 50}\n",
    )
    whole = daemon.plan()
    one = daemon.plan(key="plain/c.txt")
    under = daemon.plan(prefix="own")

    assert whole["summary"]["files"] == 3 and whole["summary"]["would_run"] == 3
    assert one["summary"]["files"] == 1 and one["scope"] == {
        "path": "plain/c.txt",
        "prefix": None,
    }
    assert under["summary"]["files"] == 2 and under["scope"]["prefix"] == "own"
    assert whole["functions"][0]["file"] == FUNCTIONS_FILE  # root first
    assert whole["functions"][0]["state"] == "new"
    limited = daemon.plan(limit=1)
    assert limited["listed"] == 1 and limited["truncated"] is True
    assert limited["summary"]["would_run"] == 3  # counts are never truncated


def test_plan_reports_a_broken_file_and_a_changed_script(root: Root, daemon: Daemon):
    write(root.path / "own" / FUNCTIONS_FILE, "version: 1\nfunctions: [oops]\n")
    script = root.script_dir / "photo.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )

    plan = daemon.plan()

    own = plan["functions"][0]
    assert own["state"] == "error" and own["error"]["kind"] == "functions.parse"
    assert own["note"] == "the loaded version stays in force at reload"
    a = by_path(plan)["own/a--photo.txt"]
    assert [l["reason"] for l in a["leaving"]] == [
        "own/.tfsfunctions.yaml no longer loads"
    ] * 3
    (photo,) = plan["scripts"]
    assert photo["state"] == "changed" and photo["loaded_hash"] != photo["disk_hash"]


def test_plan_offline_compares_nothing_and_reads_the_history(
    root: Root, daemon: Daemon
):
    a_before = calls(daemon)[:]
    daemon.shutdown()
    write(
        root.path / "own" / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 900}\n    on_photo: {}\n",
    )
    loader, functions = load_root(root)
    from tag_file_system.database.action_store import ActionStore
    from tag_file_system.database.sqlite import SQLiteBackend

    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    try:
        plan = plan_view(
            loader,
            functions,
            None,
            backend,
            ActionStore(backend),
            source=DISK,
            threshold=2,
        )
    finally:
        backend.close()

    assert plan["source"] == "disk" and plan["compared"] is False
    assert [f["state"] for f in plan["functions"]] == ["loaded"]
    assert "state" not in plan["scripts"][0]
    files = by_path(plan)
    assert files["own/a--photo.txt"]["would_run"][0]["reason"] == (
        "arguments changed: last ran as photo.resize(width=800)"
    )
    assert files["own/b--photo--draft.txt"]["would_run"][0]["reason"] == "never ran"
    assert plan["summary"]["over_threshold"] is False  # 2 would run, threshold 2
    assert len(a_before) == 3


# ---------------------------------------------------------------- reload


def test_reload_refuses_above_the_threshold_unless_consented(
    root: Root, daemon: Daemon
):
    config = root.load_config()
    config.daemon.confirm_above = 1
    config.write(root.config_path)
    write(
        root.path / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 50}\n",
    )

    result = daemon.reload()

    assert result["refused"] == {"would_run": 3, "threshold": 1}
    assert result["config"] == "reloaded" and result["addons"] == []
    assert daemon.config.daemon.confirm_above == 1  # the config did reload
    assert "" not in daemon.functions.files  # the tree did not
    assert len(calls(daemon)) == 3
    refused = [p for p in daemon.store.query_problems() if p.kind == "reload.refused"]
    assert refused and "tfs reload --yes" in refused[0].message

    accepted = daemon.reload(yes=True)
    assert "refused" not in accepted and accepted["functions"]["files"] == 2
    assert daemon.plan()["summary"]["would_run"] == 0
    # the reload re-imported the script (a fresh `calls`): the new runs only
    assert sorted(c for c in calls(daemon) if c[0] == "resize") == [
        ("resize", "a--photo.txt", 50),
        ("resize", "b--photo--draft.txt", 50),
        ("resize", "c.txt", 50),
    ]
    assert daemon.store.count_runs(action_name="photo", handler="resize") == 4


def test_reload_below_the_threshold_just_reloads(root: Root, daemon: Daemon):
    write(
        root.path / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 50}\n",
    )
    result = daemon.reload()
    assert "refused" not in result and result["functions"]["files"] == 2


# ----------------------------------------------------------------- rerun


def test_rerun_retries_finished_runs_under_their_keys(root: Root, daemon: Daemon):
    before = daemon.store.query_runs(action_name="photo", handler="resize")
    assert [r.status for r in before] == [RunStatus.OK]

    dry = daemon.rerun("photo.resize", dry=True)
    assert dry == {
        "handler": "photo.resize",
        "files": 1,
        "candidates": 1,
        "queued": 0,
        "skipped": {"in_flight": 0, "not_failed": 0, "not_stale": 0},
        "threshold": 500,
        "over_threshold": False,
        "dry_run": True,
    }
    assert len(calls(daemon)) == 3

    result = daemon.rerun("photo.resize")
    daemon.tick()
    assert result["queued"] == 1
    runs = daemon.store.query_runs(action_name="photo", handler="resize")
    assert [r.status for r in runs] == [RunStatus.OK, RunStatus.OK]
    newest = runs[0]
    assert newest.retry_of == before[0].id and newest.source is RunSource.RERUN
    assert newest.key == before[0].key
    assert calls(daemon)[-1] == ("resize", "a--photo.txt", 800)

    # --failed: nothing failed here
    assert daemon.rerun("photo.resize", failed=True)["skipped"]["not_failed"] == 1
    # --stale: the script did not change
    assert daemon.rerun("photo.resize", stale=True)["skipped"]["not_stale"] == 1
    with pytest.raises(NotFound):
        daemon.rerun("photo.nope")
    with pytest.raises(BadRequest):
        daemon.rerun("resize")


def test_rerun_respects_the_threshold_and_the_scope(root: Root, daemon: Daemon):
    write(
        root.path / FUNCTIONS_FILE,
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 50}\n",
    )
    daemon.reload()
    daemon.config.daemon.confirm_above = 2

    held = daemon.rerun("photo.resize")
    assert held["candidates"] == 4 and held["queued"] == 0 and held["over_threshold"]
    assert daemon.rerun("photo.resize", prefix="plain")["queued"] == 1
    assert daemon.rerun("photo.resize", key="own/a--photo.txt")["queued"] == 2
    assert daemon.rerun("photo.resize", yes=True)["queued"] == 4
    daemon.tick()
    assert daemon.store.count_runs(action_name="photo", handler="resize") == 4 + 7


def test_rerun_stale_finds_runs_made_by_an_older_script(root: Root, daemon: Daemon):
    script = root.script_dir / "photo.py"
    script.write_text(script.read_text(encoding="utf-8") + "\n# v2\n", encoding="utf-8")
    daemon.process_changes({(Change.modified, str(script))})  # hot reload
    plan = daemon.plan()
    a = by_path(plan)["own/a--photo.txt"]
    assert all(r["stale"] for r in a["skipped"]) and plan["summary"]["stale"] == 3
    assert "with an older script/photo.py" in a["skipped"][0]["reason"]

    result = daemon.rerun("photo.resize", stale=True)
    daemon.tick()
    assert result["queued"] == 1
    assert (
        daemon.plan()["summary"]["stale"] == 2
    )  # on_photo runs are still the old ones


# ----------------------------------------------------------------- retry


def test_retry_run_validates_then_queues(root: Root, daemon: Daemon):
    write(root.path / "own" / "bad--photo.txt", "boom")
    daemon.process_changes({(Change.added, str(root.path / "own" / "bad--photo.txt"))})
    failed = daemon.store.query_runs(status=RunStatus.FAILED)
    assert len(failed) == 1
    ok = daemon.store.query_runs(status=RunStatus.OK)[0]

    with pytest.raises(NotFound):
        daemon.retry_run("nope")
    with pytest.raises(Conflict, match="only a failed or interrupted run"):
        daemon.retry_run(ok.id)

    result = daemon.retry_run(failed[0].id)
    assert result == {"queued": True, "retry_of": failed[0].id, "slug": failed[0].slug}
    assert daemon.queue.depth == 1
    daemon.tick()
    retried = daemon.store.query_runs(action_name="photo", handler="resize")[0]
    assert retried.retry_of == failed[0].id and retried.source is RunSource.RETRY
    assert retried.status is RunStatus.FAILED  # it raises again; that is a retry

    (root.path / "own" / "bad--photo.txt").unlink()
    daemon.process_changes(
        {(Change.deleted, str(root.path / "own" / "bad--photo.txt"))}
    )
    with pytest.raises(Conflict, match="no longer in the root"):
        daemon.retry_run(retried.id)


# ------------------------------------------------------------ file commands


def test_touch_creates_indexes_and_says_what_runs(root: Root, daemon: Daemon):
    result = daemon.touch("own/new--photo.txt", "hello")

    assert result["created"] is True and result["path"] == "own/new--photo.txt"
    assert result["file"]["tags"] == ["photo"]
    assert result["applies"] == [
        "photo.resize(width=800)",
        "photo.gone(width=800)",
        "photo.on_photo()",
    ]
    assert (root.path / "own" / "new--photo.txt").read_text() == "hello"
    assert ("resize", "new--photo.txt", 800) in calls(daemon)
    run = daemon.store.query_runs(file_path="own/new--photo.txt")[0]
    assert run.source is RunSource.CLI

    again = daemon.touch("own/new--photo.txt")
    assert again["created"] is False
    assert daemon.store.count_runs(file_path="own/new--photo.txt") == 2  # no new run
    # the watcher's own event for the file is a no-op
    daemon.process_changes({(Change.added, str(root.path / "own" / "new--photo.txt"))})
    assert daemon.store.count_runs(file_path="own/new--photo.txt") == 2

    with pytest.raises(BadRequest):
        daemon.touch(".tfs/x.txt")
    with pytest.raises(BadRequest):
        daemon.touch("script/x.py")
    with pytest.raises(BadRequest):
        daemon.touch(f"own/{FUNCTIONS_FILE}")
    with pytest.raises(Conflict):
        daemon.touch("own")


def test_copy_move_and_remove_keep_the_database_in_step(root: Root, daemon: Daemon):
    copied = daemon.copy("plain/c.txt", "own/")
    assert copied["path"] == "own/c.txt" and copied["from"] == "plain/c.txt"
    assert copied["applies"] == [
        "photo.resize(width=800)",
        "photo.gone(width=800)",
        "photo.on_photo()",
    ]
    assert ("resize", "c.txt", 800) in calls(daemon)
    with pytest.raises(Conflict):
        daemon.copy("plain/c.txt", "own/c.txt")
    with pytest.raises(NotFound):
        daemon.copy("plain/nope.txt", "own/")

    row_before = daemon.backend.query_file("own/c.txt")
    assert row_before is not None
    moved = daemon.move("own/c.txt", "plain/moved--photo.txt")
    assert moved["path"] == "plain/moved--photo.txt" and moved["from"] == "own/c.txt"
    assert moved["file"]["tags"] == ["photo"] and moved["applies"] == []
    row_after = daemon.backend.query_file("plain/moved--photo.txt")
    assert row_after is not None and row_after.file_id == row_before.file_id
    assert daemon.backend.query_file("own/c.txt") is None
    assert calls(daemon)[-2:] == [("gone", "c.txt"), ("photo", "moved--photo.txt")]
    # the watcher's events for the move find everything done
    daemon.process_changes(
        {
            (Change.deleted, str(root.path / "own" / "c.txt")),
            (Change.added, str(root.path / "plain" / "moved--photo.txt")),
        }
    )
    assert daemon.backend.query_file("plain/moved--photo.txt") is not None
    assert len(calls(daemon)) == 6

    removed = daemon.remove("plain/moved--photo.txt")
    assert removed == {"removed": ["plain/moved--photo.txt"]}
    assert not (root.path / "plain" / "moved--photo.txt").exists()
    assert daemon.backend.query_file("plain/moved--photo.txt") is None
    assert daemon.backend.query_file("plain/moved--photo.txt", include_deleted=True)
    with pytest.raises(NotFound):
        daemon.remove("plain/moved--photo.txt")
    with pytest.raises(BadRequest, match="pass recursive"):
        daemon.remove("own")
    gone = daemon.remove("own", recursive=True)
    assert sorted(gone["removed"]) == ["own/a--photo.txt", "own/b--photo--draft.txt"]
    assert not (root.path / "own").exists()
    # `gone` has no exclusion: it fires for b as well
    assert sorted(calls(daemon)[-2:]) == [
        ("gone", "a--photo.txt"),
        ("gone", "b--photo--draft.txt"),
    ]


def test_mkdir_names_the_tags_a_folder_gives(root: Root, daemon: Daemon):
    result = daemon.mkdir("2024--trip/day-1--beach")
    assert result == {"path": "2024--trip/day-1--beach", "tags": ["trip", "beach"]}
    assert (root.path / "2024--trip" / "day-1--beach").is_dir()
    with pytest.raises(Conflict):
        daemon.mkdir("plain/c.txt")
    with pytest.raises(BadRequest):
        daemon.mkdir(".tfs/backups")


# ------------------------------------------------------ tags (DESIGN §12.2)


def test_retag_runs_tagged_handlers_and_moves_across_exclusions(
    root: Root, daemon: Daemon
):
    before = len(calls(daemon))
    result = daemon.retag("plain/c.txt", ["photo"], [])
    daemon.tick()

    assert result["added"] == ["photo"] and result["file"]["tags"] == ["photo"]
    assert calls(daemon)[before:] == [("photo", "c.txt")]  # the global tagged default
    row = daemon.backend.query_file("plain/c.txt")
    assert row is not None and [t.name for t in row.tags] == ["photo"]
    run = daemon.store.query_runs(file_path="plain/c.txt")[0]
    assert run.source is RunSource.API

    # a tag that excludes: resize no longer applies (gone has no exclusion
    # of its own and stays; only a handler's *own* removed mark would fire)
    excluded = daemon.retag("own/a--photo.txt", ["draft"], [])
    daemon.tick()
    assert "photo.resize(width=800)" not in excluded["applies"]
    assert "photo.gone(width=800)" in excluded["applies"]
    # and back in: the key already has a run, so resize does not run again
    result = daemon.retag("own/a--photo.txt", [], ["draft"])
    daemon.tick()
    assert result["removed"] == ["draft"]
    assert result["applies"][0] == "photo.resize(width=800)"
    assert daemon.store.count_runs(file_path="own/a--photo.txt", handler="resize") == 1

    kept = daemon.retag("own/a--photo.txt", [], ["photo"])
    assert kept["kept"] == ["photo"] and kept["removed"] == []
    with pytest.raises(BadRequest):
        daemon.retag("own/a--photo.txt", [], [])
    with pytest.raises(BadRequest, match="tag 'a:b'"):
        daemon.retag("own/a--photo.txt", ["a:b"], [])
    with pytest.raises(NotFound):
        daemon.retag("own/nope.txt", ["x"], [])
    # API-added tags survive a re-index (the name is authoritative for its own)
    daemon.retag("plain/c.txt", ["extra"], [])
    (root.path / "plain" / "c.txt").write_text("changed")
    daemon.process_changes({(Change.modified, str(root.path / "plain" / "c.txt"))})
    row = daemon.backend.query_file("plain/c.txt")
    assert row is not None and sorted(t.name for t in row.tags) == ["extra", "photo"]


def test_upload_and_content_at_the_daemon_level(root: Root, daemon: Daemon):
    import io

    result = daemon.upload("own/up--photo.txt", io.BytesIO(b"abc"), 3)
    assert result["created"] and result["size"] == 3
    assert result["applies"][0] == "photo.resize(width=800)"
    assert ("resize", "up--photo.txt", 800) in calls(daemon)
    with pytest.raises(BadRequest, match="ended after 2 of 3"):
        daemon.upload("own/short.txt", io.BytesIO(b"ab"), 3)
    assert not (root.path / "own" / "short.txt").exists()
    with pytest.raises(BadRequest):
        daemon.upload("own", io.BytesIO(b""), 0)
    with pytest.raises(BadRequest):
        daemon.upload("own/x.txt", io.BytesIO(b""), -1)
    response = daemon.content("own/up--photo.txt")
    assert response.size == 3 and response.filename == "up--photo.txt"
    assert response.content_type == "text/plain"
    with pytest.raises(NotFound):
        daemon.content("own/nope.txt")

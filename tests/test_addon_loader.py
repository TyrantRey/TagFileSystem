# Code by AkinoAlice@TyrantRey

import sys
import textwrap
from pathlib import Path

import pytest

from tag_file_system.addons.loader import MODULE_PREFIX, AddonLoader
from tag_file_system.core.interface.action import Hook, Severity
from tag_file_system.database.action_store import ActionStore
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import Root

MAKE_COPY = """
    from pathlib import Path
    from tag_file_system import action
    from _helper import SUFFIX_DEFAULT

    @action.added()
    def run(path: Path, metadata, ctx, suffix: str = SUFFIX_DEFAULT, dst: action.Remote = Path("x")):
        return "v1"

    @action.removed(on_move=True)
    def gone(path, metadata, ctx):
        pass

    @action.tagged("Photos")
    def on_photo(path, metadata, ctx):
        pass

    @action.err()
    def notify(problem, ctx):
        pass
"""

HELPER = "SUFFIX_DEFAULT = '.jpg'\n"


@pytest.fixture
def root(tmp_path: Path) -> Root:
    return Root.init(tmp_path / "vault")


@pytest.fixture
def problems() -> list[tuple]:
    return []


@pytest.fixture
def loader(root: Root, problems: list[tuple]) -> AddonLoader:
    def report(severity, kind, message, *, action_name=None):
        problems.append((severity, kind, action_name))

    return AddonLoader(root, store=None, report=report)


def write(root: Root, name: str, source: str) -> Path:
    path = root.script_dir / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


def test_load_all_discovers_handlers(root: Root, loader: AddonLoader, problems):
    write(root, "_helper.py", HELPER)
    write(root, "make_copy.py", MAKE_COPY)

    loaded = loader.load_all()

    assert [a.name for a in loaded] == ["make_copy"]
    addon = loader.addon_for("make_copy")
    assert addon is not None
    assert addon.key.as_posix() == "script/make_copy.py"
    assert len(addon.script_hash) == 64
    assert addon.hooks == [Hook.TAGGED, Hook.REMOVED, Hook.ADDED] or set(addon.hooks) == {
        Hook.ADDED, Hook.REMOVED, Hook.TAGGED
    }
    assert [h.name for h in loader.handlers_for("make_copy", Hook.ADDED)] == ["run"]
    assert loader.handlers_for("make_copy", Hook.REMOVED)[0].spec.on_move is True
    assert loader.handlers_for("make_copy", Hook.MODIFIED) == []
    assert loader.handlers_for("nope", Hook.ADDED) == []
    assert [h.name for h in loader.tag_handlers("photos")] == ["on_photo"]
    assert loader.tag_handlers("other") == []
    assert [h.name for h in loader.problem_handlers(Severity.CRIT)] == ["notify"]
    assert loader.problem_handlers(Severity.WARN) == []  # err handler does not cover warn
    assert addon.signature["added"]["properties"]["dst"]["x-tfs-path"] == "remote"
    assert addon.signature["added"]["properties"]["suffix"]["default"] == ".jpg"
    assert problems == []


def test_helpers_and_bad_names_are_not_addons(root: Root, loader: AddonLoader, problems):
    write(root, "_helper.py", HELPER)
    write(root, "Make_Copy.py", MAKE_COPY)
    write(root, "1st.py", "x = 1\n")
    write(root, "notes.txt", "not python")
    (root.script_dir / "sub").mkdir()
    write(root, "sub/nested.py", "x = 1\n")

    assert loader.load_all() == []
    assert loader.load(root.script_dir / "sub" / "nested.py") is None
    assert loader.load(root.script_dir / "notes.txt") is None
    assert sorted(p[1] for p in problems) == ["addon.filename", "addon.filename"]
    assert all(p[0] is Severity.WARN for p in problems)


def test_import_errors_isolate_the_broken_addon(root: Root, loader: AddonLoader, problems):
    write(root, "broken.py", "def run(:\n")
    write(root, "raises.py", "raise RuntimeError('at import')\n")
    write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")

    loaded = loader.load_all()

    assert [a.name for a in loaded] == ["ok"]
    assert [(p[0], p[1], p[2]) for p in problems] == [
        (Severity.ERR, "addon.import", "broken"),
        (Severity.ERR, "addon.import", "raises"),
    ]
    assert MODULE_PREFIX + "broken" not in sys.modules


def test_bad_handler_signatures_are_reported_and_skipped(root: Root, loader: AddonLoader, problems):
    write(
        root,
        "sig.py",
        """
        from tag_file_system import action

        @action.added()
        def too_few(path, metadata):
            pass

        @action.added()
        def varargs(path, metadata, ctx, *args):
            pass

        @action.warn()
        def bad_problem(problem):
            pass

        @action.added()
        def fine(path, metadata, ctx, n: int):
            pass
        """,
    )

    addon = loader.load(root.script_dir / "sig.py")

    assert addon is not None
    assert [h.name for h in addon.file_handlers] == ["fine"]
    assert addon.problem_handlers == []
    assert [p[1] for p in problems] == ["addon.signature"] * 3


def test_second_handler_for_the_same_hook_is_reported_and_skipped(
    root: Root, loader: AddonLoader, problems
):
    write(
        root,
        "dup.py",
        """
        from tag_file_system import action

        @action.added()
        def first(path, metadata, ctx):
            pass

        @action.added()
        def second(path, metadata, ctx):
            pass

        @action.tagged("a")
        def tag_a(path, metadata, ctx):
            pass

        @action.tagged("b")
        def tag_b(path, metadata, ctx):
            pass

        @action.removed()
        @action.removed(on_move=True)
        def both(path, metadata, ctx):
            pass
        """,
    )

    addon = loader.load(root.script_dir / "dup.py")

    assert addon is not None
    assert [(h.name, h.spec.describe()) for h in addon.file_handlers] == [
        ("first", "added"),  # source order
        ("tag_a", "tagged:a"),
        ("tag_b", "tagged:b"),
        ("both", "removed"),  # marks are top-down: the first removed() wins
    ]
    assert [p[1] for p in problems] == ["addon.duplicate_handler"] * 2
    assert all(p[0] is Severity.ERR for p in problems)


def test_reload_replaces_handlers_and_keeps_old_on_failure(root: Root, loader: AddonLoader, problems):
    write(root, "_helper.py", HELPER)
    path = write(root, "make_copy.py", MAKE_COPY)
    first = loader.load(path)
    assert first is not None
    assert first.file_handlers[0].func(None, None, None) in ("v1", None)

    write(root, "make_copy.py", MAKE_COPY.replace('return "v1"', 'return "v2"'))
    second = loader.load(path)

    assert second is not None and second is not first
    assert second.script_hash != first.script_hash
    run = loader.handlers_for("make_copy", Hook.ADDED)[0].func
    assert run(None, None, None) == "v2"
    assert first.module is not second.module  # in-flight runs keep the old code

    write(root, "make_copy.py", "def run(:\n")
    assert loader.load(path) is None
    assert loader.addon_for("make_copy") is second  # previous version kept
    assert loader.handlers_for("make_copy", Hook.ADDED)[0].func(None, None, None) == "v2"
    assert problems[-1][1] == "addon.import"


def test_unload(root: Root, loader: AddonLoader):
    path = write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")
    loader.load(path)
    assert MODULE_PREFIX + "ok" in sys.modules

    gone = loader.unload(path)

    assert gone is not None and gone.name == "ok"
    assert loader.addon_for("ok") is None
    assert loader.handlers_for("ok", Hook.ADDED) == []
    assert MODULE_PREFIX + "ok" not in sys.modules
    assert loader.unload(path) is None


def test_future_annotations_dataclasses_and_sys_exit(root: Root, loader: AddonLoader, problems):
    write(
        root,
        "modern.py",
        """
        from __future__ import annotations
        import dataclasses
        from tag_file_system import action

        @dataclasses.dataclass
        class Opts:
            width: int = 1

        @action.added()
        def run(path, metadata, ctx, n: int, dst: action.Remote):
            return Opts(n)
        """,
    )
    write(root, "quitter.py", "import sys\nsys.exit(0)\n")
    write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")

    loaded = loader.load_all()

    assert [a.name for a in loaded] == ["modern", "ok"]
    (handler,) = loader.handlers_for("modern", Hook.ADDED)
    assert handler.schema["properties"]["dst"]["x-tfs-path"] == "remote"
    assert [(p[1], p[2]) for p in problems] == [("addon.import", "quitter")]
    module = loader.addon_for("modern").module  # type: ignore[union-attr]
    assert sys.modules[MODULE_PREFIX + "modern"] is module
    assert module.__spec__ is not None and module.__package__ == "tfs_addons"


def test_helper_imports_leave_no_pycache_and_hot_reload(root: Root, loader: AddonLoader):
    write(root, "_helper.py", "X = 'A'\n")
    path = write(root, "use.py", "from tag_file_system import action\nfrom _helper import X\n@action.added()\ndef run(p, m, c):\n    return X\n")
    loader.load(path)

    assert not (root.script_dir / "__pycache__").exists()
    assert loader.handlers_for("use", Hook.ADDED)[0].func(None, None, None) == "A"

    write(root, "_helper.py", "X = 'B'\n")
    assert loader.load(root.script_dir / "_helper.py") is None  # reloads dependants
    assert loader.handlers_for("use", Hook.ADDED)[0].func(None, None, None) == "B"
    assert "_helper" not in sys.modules or sys.modules["_helper"].X == "B"


def test_helpers_are_per_root(tmp_path: Path):
    roots = [Root.init(tmp_path / name) for name in ("one", "two")]
    for root, value in zip(roots, ("ONE", "TWO")):
        write(root, "_helper.py", f"X = '{value}'\n")
        write(root, "use.py", "from tag_file_system import action\nfrom _helper import X\n@action.added()\ndef run(p, m, c):\n    return X\n")
    loaders = [AddonLoader(r) for r in roots]

    results = []
    for loader in loaders:
        loader.load_all()
        results.append(loader.handlers_for("use", Hook.ADDED)[0].func(None, None, None))

    assert results == ["ONE", "TWO"]


def test_sibling_import_does_not_double_register(root: Root, loader: AddonLoader, problems):
    write(root, "make_copy.py", "from tag_file_system import action\ncalls = []\ncalls.append(1)\n@action.added()\ndef run(p, m, c):\n    return 'copy'\n")
    write(root, "sibling.py", "from tag_file_system import action\nimport make_copy\nfrom make_copy import run\n@action.modified()\ndef own(p, m, c):\n    return run(p, m, c)\n")

    loader.load_all()

    assert loader.handlers_for("sibling", Hook.ADDED) == []  # run belongs to make_copy
    assert [h.name for h in loader.handlers_for("sibling", Hook.MODIFIED)] == ["own"]
    assert loader.addon_for("make_copy").module.calls == [1]  # type: ignore[union-attr]
    assert sys.modules["make_copy"] is loader.addon_for("make_copy").module  # type: ignore[union-attr]
    assert not (root.script_dir / "__pycache__").exists()
    assert problems == []


def test_addons_never_shadow_installed_modules(root: Root, loader: AddonLoader, problems):
    import json as stdlib_json

    write(root, "json.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")
    write(root, "user.py", "from tag_file_system import action\nimport json\n@action.added()\ndef run(p, m, c):\n    return json.dumps({'a': 1})\n")

    loader.load_all()

    assert sys.modules["json"] is stdlib_json
    assert loader.handlers_for("user", Hook.ADDED)[0].func(None, None, None) == '{"a": 1}'
    loader.unload(root.script_dir / "json.py")
    assert sys.modules["json"] is stdlib_json


def test_lazy_helper_imports_work_after_load(root: Root, loader: AddonLoader):
    write(root, "_late.py", "VALUE = 'lazy'\n")
    write(
        root,
        "lazy.py",
        """
        import threading
        from tag_file_system import action

        @action.added()
        def run(p, m, c):
            from _late import VALUE
            box = []
            def worker():
                from _late import VALUE as V2
                box.append(V2)
            t = threading.Thread(target=worker)
            t.start(); t.join()
            return VALUE, box[0]
        """,
    )
    loader.load_all()

    assert loader.handlers_for("lazy", Hook.ADDED)[0].func(None, None, None) == ("lazy", "lazy")


def test_helpers_are_shared_within_a_root_and_reloaded_on_change(root: Root, loader: AddonLoader):
    write(root, "_state.py", "CACHE = {}\nCOUNT = [0]\nCOUNT[0] += 1\n")
    write(root, "a.py", "from tag_file_system import action\nimport _state\n@action.added()\ndef run(p, m, c):\n    return _state\n")
    write(root, "b.py", "from tag_file_system import action\nimport _state\n@action.added()\ndef run(p, m, c):\n    return _state\n")

    loader.load_all()

    a_state = loader.handlers_for("a", Hook.ADDED)[0].func(None, None, None)
    b_state = loader.handlers_for("b", Hook.ADDED)[0].func(None, None, None)
    assert a_state is b_state and a_state.COUNT == [1]

    write(root, "_state.py", "CACHE = {'v': 2}\n")
    loader.load(root.script_dir / "_state.py")
    assert loader.handlers_for("a", Hook.ADDED)[0].func(None, None, None).CACHE == {"v": 2}


def test_reverse_order_sibling_import_executes_once(root: Root, loader: AddonLoader, problems):
    write(root, "a.py", "from tag_file_system import action\nimport zed\n@action.modified()\ndef own(p, m, c):\n    return zed.run(p, m, c)\n")
    write(root, "zed.py", "from tag_file_system import action\nruns = []\nruns.append(1)\n@action.added()\ndef run(p, m, c):\n    return 'zed'\n")

    loaded = loader.load_all()

    assert [x.name for x in loaded] == ["a", "zed"]
    zed = loader.addon_for("zed")
    assert zed is not None and zed.module.runs == [1]
    assert loader.addon_for("a").module.zed is zed.module  # type: ignore[union-attr]
    assert loader.handlers_for("a", Hook.MODIFIED)[0].func(None, None, None) == "zed"
    assert problems == []


def test_partials_and_helper_factories_are_handlers(root: Root, loader: AddonLoader, problems):
    write(root, "_factory.py", "from tag_file_system import action\ndef make(tag):\n    @action.tagged(tag)\n    def h(p, m, c):\n        return tag\n    return h\n\n@action.warn()\ndef shared_notify(problem, ctx):\n    pass\n")
    write(
        root,
        "fac.py",
        """
        import functools
        from tag_file_system import action
        from _factory import make, shared_notify

        def base(p, m, c, b=1):
            return b

        run = action.added()(functools.partial(base, b=2))
        on_photo = make("photos")
        """,
    )

    addon = loader.load(root.script_dir / "fac.py")

    assert addon is not None
    # a partial has no source line, so it sorts after real functions
    assert [h.spec.describe() for h in addon.file_handlers] == ["tagged:photos", "added"]
    assert [h.name for h in addon.problem_handlers] == ["shared_notify"]
    assert loader.handlers_for("fac", Hook.ADDED)[0].func(None, None, None) == 2
    assert problems == []


def test_circular_sibling_imports_resolve_like_packages(root: Root, loader: AddonLoader, problems):
    write(root, "a.py", "from tag_file_system import action\nimport b\n@action.added()\ndef run(p, m, c):\n    return 'a'\n")
    write(root, "b.py", "from tag_file_system import action\nimport a\n@action.modified()\ndef run(p, m, c):\n    return 'b'\n")

    loaded = loader.load_all()

    assert [x.name for x in loaded] == ["a", "b"]
    assert problems == []
    assert loader.addon_for("b").module.a is loader.addon_for("a").module  # type: ignore[union-attr]


def test_lazy_helper_imports_write_no_pycache(root: Root, loader: AddonLoader):
    write(root, "_late.py", "VALUE = 1\n")
    write(root, "lazy.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c):\n    from _late import VALUE\n    return VALUE\n")
    loader.load_all()

    assert loader.handlers_for("lazy", Hook.ADDED)[0].func(None, None, None) == 1
    assert not (root.script_dir / "__pycache__").exists()


def test_load_all_ignores_non_py_files_sharing_a_stem(root: Root, loader: AddonLoader, problems):
    write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")
    (root.script_dir / "ok.pyc").write_bytes(b"\x00")
    (root.script_dir / "ok.txt").write_text("x")
    write(root, "user.py", "from tag_file_system import action\nimport broken\n@action.added()\ndef run(p, m, c): pass\n")
    write(root, "broken.py", "def run(:\n")

    loaded = loader.load_all()

    assert [x.name for x in loaded] == ["ok"]
    assert [p[2] for p in problems] == ["broken", "user"]  # broken reported once


def test_classes_defined_in_addons_can_be_pickled(root: Root, loader: AddonLoader):
    import pickle

    write(root, "kinds.py", "import enum\nfrom tag_file_system import action\nclass Kind(enum.Enum):\n    A = 1\n@action.added()\ndef run(p, m, c):\n    return Kind.A\n")
    loader.load(root.script_dir / "kinds.py")
    value = loader.handlers_for("kinds", Hook.ADDED)[0].func(None, None, None)

    assert pickle.loads(pickle.dumps(value)) is value


def test_symlinked_scripts_do_not_abort_loading(root: Root, loader: AddonLoader, tmp_path: Path):
    outside = tmp_path / "outside.py"
    outside.write_text("from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")
    try:
        (root.script_dir / "linked.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    write(root, "zz_ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): pass\n")

    loaded = loader.load_all()

    assert "zz_ok" in [a.name for a in loaded]
    linked = loader.addon_for("linked")
    assert linked is None or linked.key.as_posix() == "script/linked.py"


def test_empty_and_all_broken_addons(root: Root, loader: AddonLoader, problems):
    write(root, "empty.py", "x = 1\n")
    assert loader.load(root.script_dir / "empty.py") is not None
    assert [p[1] for p in problems] == ["addon.empty"]

    path = write(root, "r.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): return 1\n")
    good = loader.load(path)
    write(root, "r.py", "from tag_file_system import action\n@action.added()\ndef run(p): pass\n")
    assert loader.load(path) is None  # all handlers broken: previous kept
    assert loader.addon_for("r") is good
    assert problems[-1][1] == "addon.signature"


def test_filename_rules(root: Root, loader: AddonLoader, problems):
    write(root, "make_copy_.py", "x = 1\n")  # slug could never be spelled
    (root.script_dir / "upper.PY").write_text("x = 1\n")

    assert loader.load(root.script_dir / "make_copy_.py") is None
    assert loader.load(root.script_dir / "upper.PY") is None
    assert loader.load(root.script_dir / "missing.py") is None
    assert [p[1] for p in problems] == ["addon.filename", "addon.filename"]
    assert loader.tag_handlers("Photos") == loader.tag_handlers("photos")


def test_store_failure_keeps_the_previous_addon(root: Root, tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    store = ActionStore(backend)
    reports: list[tuple] = []
    loader = AddonLoader(root, store=store, report=lambda s, k, m, *, action_name=None: reports.append((s, k)))
    path = write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): return 1\n")
    first = loader.load(path)
    assert first is not None and first.record is not None

    backend.close()
    write(root, "ok.py", "from tag_file_system import action\n@action.added()\ndef run(p, m, c): return 2\n")
    assert loader.load(path) is None
    assert loader.addon_for("ok") is first
    assert reports[-1] == (Severity.CRIT, "db.unwritable")


def test_handlers_are_in_source_order(root: Root, loader: AddonLoader):
    write(
        root,
        "order.py",
        """
        from tag_file_system import action

        @action.modified()
        def second(p, m, c): pass

        @action.added()
        def first(p, m, c): pass

        @action.tagged("z")
        def Zed(p, m, c): pass
        """,
    )

    addon = loader.load(root.script_dir / "order.py")

    assert [h.name for h in addon.file_handlers] == ["second", "first", "Zed"]  # type: ignore[union-attr]


def test_missing_script_dir_is_critical(root: Root, loader: AddonLoader, problems):
    import shutil

    shutil.rmtree(root.script_dir)

    assert loader.load_all() == []
    assert problems == [(Severity.CRIT, "script.unreadable", None)]


def test_loader_registers_actions_in_the_store(root: Root, tmp_path: Path):
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    store = ActionStore(backend)
    loader = AddonLoader(root, store=store)
    write(root, "_helper.py", HELPER)
    path = write(root, "make_copy.py", MAKE_COPY)

    addon = loader.load(path)

    assert addon is not None and addon.record is not None
    record = store.latest_action("make_copy")
    assert record is not None
    assert record.id == addon.record.id
    assert record.script_path.as_posix() == "script/make_copy.py"
    assert record.script_hash == addon.script_hash
    assert set(record.hooks) == {Hook.ADDED, Hook.REMOVED, Hook.TAGGED}
    assert record.signature["added"].get("required", []) == []

    write(root, "make_copy.py", MAKE_COPY.replace('return "v1"', 'return "v1"  # changed'))
    loader.load(path)
    assert len(store.query_actions("make_copy")) == 2
    backend.close()

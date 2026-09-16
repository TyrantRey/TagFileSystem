# Code by AkinoAlice@TyrantRey

"""The per-folder ``.tfsfunctions.yaml`` (DESIGN/v0-4-0.md §3–§4): reading
one file, merging the chain, exclusion, validation against the add-ons."""

import sys
import textwrap
from pathlib import Path

import pytest

from tag_file_system.addons.loader import MODULE_PREFIX, AddonLoader
from tag_file_system.core.interface.action import Severity
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.functions import (
    AllOf,
    AnyOf,
    Exclude,
    FilenameMatches,
    FunctionsError,
    FunctionsStore,
    Not,
    Subject,
    TagIs,
    chain,
    explain_payload,
    file_label,
    folder_of,
    load_file,
    parse_predicate,
)
from tag_file_system.root import FUNCTIONS_FILE, Root, Zone
from tag_file_system.services.indexer import Indexer

PHOTO = """
    from tag_file_system import action

    @action.added()
    def resize(path, metadata, ctx, width: int, quality: int = 80):
        return width

    @action.modified()
    @action.removed(on_move=True)
    def make_copy(path, metadata, ctx, suffix: str, dst: action.Remote):
        return suffix

    @action.tagged("photo")
    def on_photo(path, metadata, ctx):
        return "photo"

    @action.on_start()
    def up(ctx):
        pass
"""

EXAMPLE = """
    version: 1
    functions:
      photo:
        resize:
          width: 800
          exclude:
            - tag: tag2
            - filename: test2.jpg
        make_copy:
          suffix: .jpg
          dst: backup
"""


@pytest.fixture(autouse=True)
def clean_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def root(tmp_path: Path) -> Root:
    root = Root.init(tmp_path / "vault")
    (root.script_dir / "photo.py").write_text(textwrap.dedent(PHOTO), encoding="utf-8")
    return root


@pytest.fixture
def loader(root: Root) -> AddonLoader:
    loader = AddonLoader(root)
    loader.load_all()
    return loader


@pytest.fixture
def reported() -> list[tuple[Severity, str, str]]:
    return []


@pytest.fixture
def store(root: Root, loader: AddonLoader, reported: list) -> FunctionsStore:
    def report(severity, kind, message, *, action_name=None):
        reported.append((severity, kind, message))

    return FunctionsStore(root, loader, report=report)


def write(root: Root, folder: str, text: str) -> Path:
    directory = root.path.joinpath(*folder.split("/")) if folder else root.path
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FUNCTIONS_FILE
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def applied(store: FunctionsStore, key: str, *tags: str) -> list[tuple[str, str]]:
    return [
        (item.entry.display(), item.folder)
        for item in store.effective(key, tags).applied
    ]


# ------------------------------------------------------------------- model


def test_load_file_reads_entries_in_document_order_with_yaml_types(tmp_path: Path):
    path = tmp_path / FUNCTIONS_FILE
    path.write_text(textwrap.dedent(EXAMPLE), encoding="utf-8")

    parsed = load_file(path, "--tag1")

    assert parsed.folder == "--tag1" and parsed.path == path and parsed.digest
    assert parsed.invalid == []
    resize, make_copy = parsed.entries
    assert (resize.script, resize.handler, resize.order) == ("photo", "resize", 0)
    assert resize.args == {"width": 800} and isinstance(resize.args["width"], int)
    assert resize.exclude == Exclude.parse([{"tag": "tag2"}, {"filename": "test2.jpg"}])
    assert resize.exclude.text() == "any(tag tag2, filename test2.jpg)"
    assert resize.display() == "photo.resize(width=800)"
    assert resize.identity == ("photo", "resize", '{"width":800}')
    assert make_copy.args == {"suffix": ".jpg", "dst": "backup"}
    assert make_copy.exclude == Exclude.parse(None) and make_copy.exclude.text() == ""
    assert make_copy.display() == 'photo.make_copy(suffix=".jpg", dst="backup")'


def test_typed_values_survive_into_the_key(tmp_path: Path):
    path = tmp_path / FUNCTIONS_FILE
    path.write_text(
        "version: 1\nfunctions:\n  s:\n    h:\n      flag: true\n      ratio: 1.5\n"
        "      sizes: [1, 2]\n      name: '800'\n",
        encoding="utf-8",
    )
    (entry,) = load_file(path, "").entries
    assert entry.args == {"flag": True, "ratio": 1.5, "sizes": [1, 2], "name": "800"}
    assert entry.args_json == '{"flag":true,"name":"800","ratio":1.5,"sizes":[1,2]}'


@pytest.mark.parametrize(
    "text, fragment",
    [
        (
            "version: 1\nfunctions:\n  photo:\n    resize:\n      width: 8\n     x",
            "YAML syntax",
        ),
        ("- a\n- b\n", "must be a mapping"),
        ("version: 1\nextra: 1\n", "extra"),
        ("version: 1\nfunctions: [a]\n", "functions"),
        ("version: 1\nfunctions:\n  photo: [resize]\n", "photo"),
    ],
)
def test_skeleton_errors_refuse_the_whole_file(
    tmp_path: Path, text: str, fragment: str
):
    path = tmp_path / FUNCTIONS_FILE
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FunctionsError) as exc:
        load_file(path, "")
    assert exc.value.kind == "functions.parse"
    assert fragment in exc.value.message
    assert exc.value.folder == ""


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("functions: {}\n", "version is required"),
        ("", "version is required"),
        ("version: '1'\n", "must be an integer"),
        ("version: true\n", "must be an integer"),
        ("version: 2\n", "newer than this release"),
    ],
)
def test_version_errors_have_their_own_kind(tmp_path: Path, text: str, fragment: str):
    path = tmp_path / FUNCTIONS_FILE
    path.write_text(text, encoding="utf-8")
    with pytest.raises(FunctionsError) as exc:
        load_file(path, "a/b")
    assert exc.value.kind == "functions.version"
    assert fragment in exc.value.message


def test_a_bad_handler_block_skips_only_that_entry(tmp_path: Path):
    path = tmp_path / FUNCTIONS_FILE
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            functions:
              photo:
                resize: 800
                thumbnail:
                blurred:
                  exclude:
                    tags: [x]
                sharpened:
                  exclude:
                    tag: [""]
                cropped:
                  exclude:
                  width: 1
                make_copy:
                  suffix: .jpg
                  dst: backup
            """
        ),
        encoding="utf-8",
    )

    parsed = load_file(path, "")

    assert [e.ref for e in parsed.entries] == [
        "photo.thumbnail",
        "photo.cropped",
        "photo.make_copy",
    ]
    assert parsed.entries[0].args == {}  # `thumbnail:` with nothing: switched on
    assert parsed.entries[1].args == {"width": 1}  # `exclude:` with nothing: none
    assert [(ref, kind) for ref, kind, _ in parsed.invalid] == [
        ("photo.resize", "functions.signature"),
        ("photo.blurred", "functions.signature"),
        ("photo.sharpened", "functions.signature"),
    ]
    messages = [message for _, _, message in parsed.invalid]
    assert "expected a mapping of parameters, got int" in messages[0]
    assert "exclude: unknown predicate 'tags'" in messages[1]
    assert "exclude.tag: tag '' is empty once normalized" in messages[2]


def subject(name: str, *tags: str) -> Subject:
    return Subject(name=name, tags=frozenset(tags))


def test_exclude_is_a_predicate_tree():
    # A list is "any"; a leaf takes one value or a list of them; document
    # order decides which reason is reported.
    exclude = Exclude.parse(
        [{"tag": ["Draft", "wip"]}, {"filename": ["*.TMP", "test2.jpg"]}]
    )
    assert exclude.predicate == AnyOf(
        (TagIs(("draft", "wip")), FilenameMatches(("*.TMP", "test2.jpg")))
    )
    assert exclude.text() == "any(tag (draft, wip), filename (*.TMP, test2.jpg))"
    assert exclude.reason(subject("a.jpg", "draft", "x")) == "tag draft"
    assert exclude.reason(subject("A.tmp", "x")) == "filename *.TMP"  # case-insensitive
    assert exclude.reason(subject("TEST2.JPG")) == "filename test2.jpg"
    assert exclude.reason(subject("test2.jpg", "draft")) == "tag draft"  # first branch
    assert exclude.reason(subject("keep.jpg", "x")) is None
    assert Exclude.parse(None).reason(subject("anything", "draft")) is None
    assert Exclude.parse([]).reason(subject("anything", "draft")) is None

    # all / not nest; the reason is the branch that held, rendered.
    tree = Exclude.parse({"all": [{"tag": "raw"}, {"not": {"tag": "keep"}}]})
    assert tree.predicate == AllOf((TagIs(("raw",)), Not(TagIs(("keep",)))))
    assert tree.text() == "all(tag raw, not(tag keep))"
    assert tree.reason(subject("a.jpg", "raw")) == "all(tag raw, not(tag keep))"
    assert tree.reason(subject("a.jpg", "raw", "keep")) is None
    assert tree.reason(subject("a.jpg", "keep")) is None
    nested = Exclude.parse(
        {
            "any": [
                {"filename": "*.tmp"},
                {"all": [{"tag": "big"}, {"not": {"filename": "*.jpg"}}]},
            ]
        }
    )
    assert nested.reason(subject("x.png", "big")) == "all(tag big, not(filename *.jpg))"
    assert nested.reason(subject("x.jpg", "big")) is None
    assert nested.reason(subject("x.tmp")) == "filename *.tmp"


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"tags": ["x"]}, "exclude: unknown predicate 'tags'"),
        (
            {"tag": "x", "filename": "y"},
            "one of any, all, not, tag, filename as its single key",
        ),
        ("draft", "expected a mapping"),
        ({"tag": []}, "exclude.tag: needs at least one value"),
        ({"tag": 3}, "exclude.tag: expected a string, got int"),
        ({"tag": "!!!"}, "exclude.tag: tag '!!!' is empty once normalized"),
        ({"filename": " "}, "exclude.filename: an empty pattern matches nothing"),
        ({"all": []}, "exclude.all: an empty all would exclude every file"),
        ({"all": {"tag": "x"}}, "exclude.all: expected a list of predicates"),
        (
            {"not": [{"tag": "x"}, {"tag": "y"}]},
            None,
        ),  # a list under not is "any": fine
        (
            {"any": [{"tag": "x"}, {"nope": 1}]},
            "exclude.any[1]: unknown predicate 'nope'",
        ),
        (
            {"all": [{"tag": "x"}, {"not": {"tag": ""}}]},
            "exclude.all[1].not.tag: tag ''",
        ),
    ],
)
def test_predicate_errors_name_the_node(raw, fragment):
    if fragment is None:
        assert parse_predicate(raw).text() == "not(any(tag x, tag y))"
        return
    with pytest.raises(ValueError) as exc:
        parse_predicate(raw)
    assert fragment in str(exc.value)


def test_helpers():
    assert folder_of("a.jpg") == "" and folder_of("a/b/c.jpg") == "a/b"
    assert chain("") == [""] and chain("a/b/c") == ["", "a", "a/b", "a/b/c"]
    assert file_label("") == FUNCTIONS_FILE
    assert file_label("a/b") == f"a/b/{FUNCTIONS_FILE}"


# ------------------------------------------------------------------- store


def test_load_tree_merges_parent_first_in_document_order(
    root: Root, store: FunctionsStore
):
    write(
        root,
        "",
        "version: 1\nfunctions:\n  photo:\n    make_copy: {suffix: .jpg, dst: backup}\n",
    )
    write(root, "a", "version: 1\nfunctions:\n  photo:\n    resize: {width: 800}\n")
    write(root, "a/b", "version: 1\nfunctions:\n  photo:\n    resize: {width: 400}\n")
    (root.path / "unrelated").mkdir()

    report = store.load_tree()

    assert report.folders == ["", "a", "a/b"] and report.problems == 0
    assert applied(store, "a/b/x.jpg") == [
        ('photo.make_copy(suffix=".jpg", dst="backup")', ""),
        ("photo.resize(width=800)", "a"),
        ("photo.resize(width=400)", "a/b"),
    ]
    assert applied(store, "a/x.jpg") == [
        ('photo.make_copy(suffix=".jpg", dst="backup")', ""),
        ("photo.resize(width=800)", "a"),
    ]
    assert applied(store, "unrelated/x.jpg") == [
        ('photo.make_copy(suffix=".jpg", dst="backup")', "")
    ]
    assert store.named("a/b/x.jpg") == {("photo", "make_copy"), ("photo", "resize")}


def test_identical_entries_collapse_and_different_args_both_apply(
    root: Root, store: FunctionsStore
):
    write(root, "", "version: 1\nfunctions:\n  photo:\n    resize: {width: 800}\n")
    write(root, "a", "version: 1\nfunctions:\n  photo:\n    resize: {width: 800}\n")
    write(root, "a/b", "version: 1\nfunctions:\n  photo:\n    resize: {width: 1600}\n")
    store.load_tree()

    assert applied(store, "a/b/x.jpg") == [
        ("photo.resize(width=800)", ""),  # a/ repeats the root: one run, first wins
        ("photo.resize(width=1600)", "a/b"),
    ]


def test_exclusion_by_tag_or_filename_per_entry(root: Root, store: FunctionsStore):
    # The worked example of DESIGN/v0-4-0.md §4.
    write(root, "--tag1", EXAMPLE)
    store.load_tree()

    copy = 'photo.make_copy(suffix=".jpg", dst="backup")'
    assert applied(store, "--tag1/test1.jpg", "tag1") == [
        ("photo.resize(width=800)", "--tag1"),
        (copy, "--tag1"),
    ]
    assert applied(store, "--tag1/test2.jpg", "tag1") == [(copy, "--tag1")]
    assert applied(store, "--tag1/--tag2/test1.jpg", "tag1", "tag2") == [
        (copy, "--tag1")
    ]
    assert applied(store, "--tag1/TEST2.JPG", "tag1") == [(copy, "--tag1")]

    effective = store.effective("--tag1/--tag2/test2.jpg", ["tag1", "tag2"])
    assert [(s.entry.handler, s.reason) for s in effective.suppressed] == [
        ("resize", "tag tag2")
    ]
    assert store.effective("--tag1/test2.jpg", ["tag1"]).suppressed[0].reason == (
        "filename test2.jpg"
    )
    # Naming is not applying: a suppressed entry still takes over the default.
    assert ("photo", "resize") in effective.named


def test_a_deeper_folder_may_redeclare_what_an_ancestor_excluded(
    root: Root, store: FunctionsStore
):
    write(
        root,
        "",
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 800, exclude: [{tag: tag2}]}\n",
    )
    write(
        root, "--tag2", "version: 1\nfunctions:\n  photo:\n    resize: {width: 800}\n"
    )
    store.load_tree()

    effective = store.effective("--tag2/x.jpg", ["tag2"])
    assert [(a.entry.display(), a.folder) for a in effective.applied] == [
        ("photo.resize(width=800)", "--tag2")
    ]
    assert [s.folder for s in effective.suppressed] == [""]


def test_rebind_reports_unbound_and_signature_and_keeps_the_rest(
    root: Root, store: FunctionsStore, reported: list
):
    write(
        root,
        "",
        """
        version: 1
        functions:
          nosuch:
            run: {}
          photo:
            thumbnail: {}
            up: {}
            resize: {width: wide, widht: 1}
            make_copy: {suffix: .jpg, dst: backup}
        """,
    )
    store.load_tree()

    assert [(p["kind"], p["file"]) for p in store.problems] == [
        ("functions.unbound", FUNCTIONS_FILE),
        ("functions.unbound", FUNCTIONS_FILE),
        ("functions.unbound", FUNCTIONS_FILE),
        ("functions.signature", FUNCTIONS_FILE),
    ]
    messages = [p["message"] for p in store.problems]
    assert messages[0].startswith(
        f"{FUNCTIONS_FILE}: nosuch.run: script/nosuch.py is not loaded"
    )
    assert "has no file handler thumbnail" in messages[1]
    assert "has no file handler up" in messages[2]  # lifecycle handlers need no entry
    assert "unknown parameter(s) widht" in messages[3]
    assert "width: Input should be a valid integer" in messages[3]
    assert applied(store, "x.jpg") == [
        ('photo.make_copy(suffix=".jpg", dst="backup")', "")
    ]
    assert [(s, k) for s, k, _ in reported] == [
        (Severity.ERR, "functions.unbound"),
        (Severity.ERR, "functions.unbound"),
        (Severity.ERR, "functions.unbound"),
        (Severity.ERR, "functions.signature"),
    ]

    store.rebind()  # a script save re-validates; nothing new is re-reported
    assert len(reported) == 4 and len(store.problems) == 4

    # A fixed script makes the entry bind — and the problem's return is news.
    (root.script_dir / "nosuch.py").write_text(
        "from tag_file_system import action\n\n@action.added()\ndef run(path, metadata, ctx):\n    pass\n",
        encoding="utf-8",
    )
    store.loader.load_all()
    store.rebind()
    assert len(store.problems) == 3
    assert ("nosuch", "run") in store.named("x.jpg")
    (root.script_dir / "nosuch.py").unlink()
    store.loader.unload(root.script_dir / "nosuch.py")
    store.rebind()
    assert len(store.problems) == 4 and len(reported) == 5


def test_a_refused_file_keeps_the_previous_version(
    root: Root, store: FunctionsStore, reported: list
):
    path = write(
        root, "a", "version: 1\nfunctions:\n  photo:\n    resize: {width: 800}\n"
    )
    store.load_tree()
    assert store.is_current("a", path)

    path.write_text(
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 8\n", encoding="utf-8"
    )
    assert not store.is_current("a", path)
    report = store.load_tree()

    assert report.problems == 1
    assert store.problems[0]["kind"] == "functions.parse"
    assert store.problems[0]["message"].startswith(f"a/{FUNCTIONS_FILE}: YAML syntax")
    assert applied(store, "a/x.jpg") == [("photo.resize(width=800)", "a")]  # still
    assert not store.is_current("a", path)
    assert [k for _, k, _ in reported] == ["functions.parse"]

    path.write_text("version: 2\nfunctions: {}\n", encoding="utf-8")
    store.load_tree()
    assert store.problems[0]["kind"] == "functions.version"
    assert applied(store, "a/x.jpg") == [("photo.resize(width=800)", "a")]

    path.write_text(
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 1600}\n",
        encoding="utf-8",
    )
    store.load_tree()
    assert store.problems == [] and store.is_current("a", path)
    assert applied(store, "a/x.jpg") == [("photo.resize(width=1600)", "a")]

    path.unlink()
    store.load_tree()
    assert applied(store, "a/x.jpg") == [] and "a" not in store.files
    assert not store.is_current("a", path)


def test_explain_payload_is_json_safe_and_names_suppressors(
    root: Root, store: FunctionsStore, loader: AddonLoader
):
    write(
        root,
        "",
        "version: 1\nfunctions:\n  photo:\n    resize: {width: 800, exclude: [{tag: draft}]}\n",
    )
    write(
        root, "own", "version: 1\nfunctions:\n  photo:\n    on_photo: {}\n    bad: {}\n"
    )
    store.load_tree()

    payload = explain_payload(
        store, loader, "own/a--draft.jpg", ["draft", "photo"], True
    )

    assert payload["path"] == "own/a--draft.jpg" and payload["known"] is True
    assert payload["tags"] == ["draft", "photo"]
    assert payload["applied"] == [
        {
            "script": "photo",
            "handler": "on_photo",
            "args": {},
            "folder": "own",
            "file": f"own/{FUNCTIONS_FILE}",
            "display": "photo.on_photo()",
            "hooks": ["tagged:photo"],
        }
    ]
    assert payload["suppressed"] == [
        {
            "script": "photo",
            "handler": "resize",
            "args": {"width": 800},
            "folder": "",
            "file": FUNCTIONS_FILE,
            "display": "photo.resize(width=800)",
            "reason": "tag draft",
        }
    ]
    assert payload["defaults"] == [
        {
            "script": "photo",
            "handler": "on_photo",
            "tag": "photo",
            "suppressed_by": f"own/{FUNCTIONS_FILE}",
        }
    ]
    assert [p["kind"] for p in payload["problems"]] == ["functions.unbound"]

    elsewhere = explain_payload(store, loader, "x.jpg", ["photo"], False)
    assert elsewhere["defaults"][0]["suppressed_by"] is None
    # The root file reaches x.jpg too; nothing there excludes it.
    assert [a["display"] for a in elsewhere["applied"]] == ["photo.resize(width=800)"]
    assert elsewhere["problems"] == []  # own/'s problem is not in x.jpg's chain
    assert elsewhere["known"] is False


# ------------------------------------------------------------------- zones


def test_the_file_is_its_own_zone_and_never_a_row(root: Root):
    path = write(root, "a", "version: 1\nfunctions: {}\n")
    backend = SQLiteBackend()
    backend.init_database(root.db_path, root_dir=root.path)
    try:
        assert root.zone(path) is Zone.FUNCTIONS
        assert root.zone(root.path / FUNCTIONS_FILE) is Zone.FUNCTIONS
        assert root.zone(root.script_dir / FUNCTIONS_FILE) is Zone.SCRIPT
        assert Indexer(root, backend).index(path) is None
        assert backend.query_file(f"a/{FUNCTIONS_FILE}") is None
    finally:
        backend.close()


def test_a_reserved_parameter_name_is_a_signature_error(root: Root):
    problems: list[tuple[str, str]] = []
    (root.script_dir / "bad.py").write_text(
        "from tag_file_system import action\n\n@action.added()\n"
        "def run(path, metadata, ctx, exclude: str = ''):\n    pass\n",
        encoding="utf-8",
    )
    loader = AddonLoader(
        root, report=lambda s, k, m, *, action_name=None: problems.append((k, m))
    )
    loader.load_all()
    assert [k for k, _ in problems] == ["addon.signature"]
    assert f"'exclude' is reserved by {FUNCTIONS_FILE}" in problems[0][1]

# Code by AkinoAlice@TyrantRey

from pathlib import PurePath, PurePosixPath, PureWindowsPath

import pytest

from tag_file_system.core.interface.tag import ActionCall, Tag
from tag_file_system.services.tagging import TaggingParser


@pytest.fixture
def parser() -> TaggingParser:
    return TaggingParser()


# ------------------------------------------------------------------ segments


def test_segment_yields_tags_and_action_with_args(parser: TaggingParser):
    out = parser.parse("@@make_copy__.jpg__photos--archive--Q3")

    assert out.actions == [ActionCall(name="make_copy", args=(".jpg", "photos"))]
    assert [t.name for t in out.tags] == ["archive", "q3"]
    assert out.problems == []


def test_label_is_ignored_and_optional(parser: TaggingParser):
    with_label = parser.parse("report--finance")
    without_label = parser.parse("--finance")

    assert [t.name for t in with_label.tags] == ["finance"]
    assert [t.name for t in without_label.tags] == ["finance"]
    assert parser.parse("plain-name").tags == []


def test_function_slug_is_lowercased_but_args_keep_case(parser: TaggingParser):
    out = parser.parse("@@Make_Copy__.JPG__Photos")

    assert out.actions == [ActionCall(name="make_copy", args=(".JPG", "Photos"))]


def test_action_without_args(parser: TaggingParser):
    out = parser.parse("@@backup")

    assert out.actions == [ActionCall(name="backup", args=())]
    assert out.actions[0].slug == "backup"
    assert str(out.actions[0]) == "@@backup"


def test_args_cannot_contain_other_markers(parser: TaggingParser):
    # "--" and "@@" always start a new marker, so args never contain them
    out = parser.parse("@@resize__800--photo@@rotate__90")

    assert out.actions == [
        ActionCall(name="resize", args=("800",)),
        ActionCall(name="rotate", args=("90",)),
    ]
    assert [t.name for t in out.tags] == ["photo"]


@pytest.mark.parametrize(
    "segment",
    [
        "a--x:y",  # colon
        "a@@f__a/b",  # slash
        "a@@f__a\\b",  # backslash
        "a--x<y",
        "a--x>y",
        "a--x|y",
        "a--x?y",
        "a--x*y",
        'a--x"y',
    ],
)
def test_illegal_characters_make_the_marker_a_problem(
    parser: TaggingParser, segment: str
):
    out = parser.parse(segment + "--ok")

    assert [t.name for t in out.tags] == ["ok"]
    assert out.actions == []
    assert len(out.problems) == 1
    assert "illegal characters" in out.problems[0].message
    assert out.problems[0].segment == segment + "--ok"


@pytest.mark.parametrize(
    "segment, marker",
    [
        ("notes--valid--", "--"),  # trailing tag marker
        ("x@@make_copy__", "@@make_copy__"),  # trailing arg separator
        ("x@@_private", "@@_private"),  # name may not start with _
        ("x@@trailing_", "@@trailing_"),  # name may not end with _
        ("x@@1st", "@@1st"),  # name must start with a letter
        ("x@@f___a", "@@f___a"),  # arg may not start with _
        ("x@@f__a_", "@@f__a_"),  # arg may not end with _
        ("x@@", "@@"),  # empty function
        ("x--!!!", "--!!!"),  # tag empty after normalization
    ],
)
def test_invalid_markers_are_skipped_not_fatal(
    parser: TaggingParser, segment: str, marker: str
):
    out = parser.parse(segment + "--rest")

    assert [t.name for t in out.tags][-1:] == ["rest"]
    assert [p.marker for p in out.problems] == [marker]
    assert out.problems[0].message  # a human-readable reason


def test_tag_normalization(parser: TaggingParser):
    # "--" always starts a marker, so runs of hyphens only arise once other
    # characters are stripped: "a.-.-b" -> "a--b" -> "a-b"
    out = parser.parse("x--Hello World--a.-.-b--é")

    assert [t.name for t in out.tags] == ["helloworld", "a-b", "é"]


def test_action_call_is_hashable_and_dedupes_by_value():
    a = ActionCall(name="f", args=("1",))
    b = ActionCall.from_marker("f__1")

    assert a == b
    assert len({a, b}) == 1
    assert repr(a) == "ActionCall('f', ['1'])"


# -------------------------------------------------------------------- paths


def test_parse_path_merges_parent_first(parser: TaggingParser):
    out = parser.parse_path(
        PurePosixPath("@@make_copy__.jpg__photos--archive/2024--trip/img--raw.jpg")
    )

    assert out.path == PurePosixPath(
        "@@make_copy__.jpg__photos--archive/2024--trip/img--raw.jpg"
    )
    assert out.tag_names == ["archive", "trip", "raw"]
    assert out.actions == [ActionCall(name="make_copy", args=(".jpg", "photos"))]
    assert out.problems == []


def test_parse_path_orders_actions_parent_first_filename_last(
    parser: TaggingParser,
):
    out = parser.parse_path(PurePosixPath("@@a/@@b__1/x@@c.txt"))

    assert [c.slug for c in out.actions] == ["a", "b__1", "c"]


def test_parse_path_dedupes_identical_calls_but_keeps_different_args(
    parser: TaggingParser,
):
    out = parser.parse_path(
        PurePosixPath("@@resize__800/x/@@resize__400/@@resize__800/f--t--t.png")
    )

    assert [c.slug for c in out.actions] == ["resize__800", "resize__400"]
    assert out.tag_names == ["t"]


def test_parse_path_uses_stem_for_files_and_full_name_for_directories(
    parser: TaggingParser,
):
    as_file = parser.parse_path(PurePosixPath("a/b--tag.txt"))
    as_dir = parser.parse_path(PurePosixPath("a/b--tag.txt"), is_file=False)

    assert as_file.tag_names == ["tag"]
    assert as_dir.tag_names == ["tagtxt"]  # "." is stripped by normalization
    assert parser.parse_path(PurePosixPath("archive.tar.gz")).tags == []
    assert parser.parse_path(PurePosixPath("README")).tags == []


def test_parse_path_collects_problems_with_their_segment(parser: TaggingParser):
    out = parser.parse_path(PurePosixPath("@@bad_/ok--fine/x--.txt"))

    assert out.tag_names == ["fine"]
    assert out.actions == []
    assert [(p.segment, p.marker) for p in out.problems] == [
        ("@@bad_", "@@bad_"),
        ("x--", "--"),
    ]


def test_parse_path_accepts_windows_paths_and_reports_posix(parser: TaggingParser):
    out = parser.parse_path(PureWindowsPath("@@f__1\\sub--t\\file.txt"))

    assert out.path == PurePosixPath("@@f__1/sub--t/file.txt")
    assert out.tag_names == ["t"]
    assert [c.slug for c in out.actions] == ["f__1"]


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("/abs/file.txt"),
        PureWindowsPath("C:\\abs\\file.txt"),
        PureWindowsPath("/abs/file.txt"),  # rooted, no drive
        PureWindowsPath("\\abs\\file.txt"),
        PureWindowsPath("C:abs\\file.txt"),  # drive-relative
        PureWindowsPath("\\\\server\\share\\f.txt"),
        "/abs/file--t.txt",
        "\\abs\\file--t.txt",
    ],
)
def test_parse_path_rejects_anchored_paths(parser: TaggingParser, path):
    with pytest.raises(ValueError):
        parser.parse_path(path)


def test_parse_path_rejects_parent_components(parser: TaggingParser):
    with pytest.raises(ValueError):
        parser.parse_path(PurePosixPath("../a--t/b.txt"))
    with pytest.raises(ValueError):
        parser.parse_path(PurePosixPath("a--t/../b.txt"))


@pytest.mark.parametrize(
    "filename, tags, slugs",
    [
        ("img--raw.jpg", ["raw"], []),
        ("archive.tar.gz", [], []),
        ("v1.2--beta", ["beta"], []),  # suffix ".2--beta" holds a marker
        ("photo.2024--trip", ["trip"], []),
        ("@@make_copy__.jpg__photos", [], ["make_copy__.jpg__photos"]),
        ("x@@f__1.tar.gz", [], ["f__1.tar"]),  # last dotted part is the extension
        ("notes--v1.0.txt", ["v10"], []),  # ".txt" cut, "." then normalized away
    ],
)
def test_filename_extension_rule(parser: TaggingParser, filename, tags, slugs):
    out = parser.parse_path(PurePosixPath(filename))

    assert out.tag_names == tags
    assert [c.slug for c in out.actions] == slugs


def test_line_breaks_inside_a_marker_are_a_problem(parser: TaggingParser):
    for segment in ("--a\nb--c", "@@f__a\nb", "--a\n", "--a\r\nb"):
        out = parser.parse(segment)
        assert [p.marker for p in out.problems][0].startswith(segment[:3])
        assert "illegal characters" in out.problems[0].message
    assert [t.name for t in parser.parse("--a\nb--c").tags] == ["c"]


@pytest.mark.parametrize("segment", ["@@ f__a", "@@f __a", "@@f\t__a"])
def test_whitespace_in_function_name_is_a_problem(parser: TaggingParser, segment):
    out = parser.parse(segment)

    assert out.actions == []
    assert len(out.problems) == 1


def test_unicode_is_nfc_normalized(parser: TaggingParser):
    nfc = parser.parse("--café@@f__café")
    nfd = parser.parse("--cafe\u0301@@f__cafe\u0301")

    assert nfc.tags == nfd.tags == [Tag(name="café")]
    assert nfc.actions == nfd.actions
    assert nfc.actions[0].args == ("café",)


def test_parse_dedupes_within_a_segment(parser: TaggingParser):
    out = parser.parse("--a--a--A@@f@@F@@f__1")

    assert [t.name for t in out.tags] == ["a"]
    assert [c.slug for c in out.actions] == ["f", "f__1"]


def test_factory_errors_and_wrong_models_become_problems(parser: TaggingParser):
    def picky(value: str) -> Tag:
        if value == "boom":
            raise ValueError("no boom allowed")
        return Tag(name=value)

    parser.register("--", "tags")(picky)
    parser.register("%%", "actions")(lambda value: Tag(name=value))  # wrong model

    out = parser.parse("x--ok--boom%%y")

    assert [t.name for t in out.tags] == ["ok"]
    assert out.actions == []
    assert [(p.marker, p.message) for p in out.problems] == [
        ("--boom", "no boom allowed"),
        ("%%y", "factory for '%%' returned Tag, expected ActionCall"),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "f", "args": ("a--b",)},
        {"name": "f", "args": ("a__b",)},
        {"name": "f", "args": ("a@@b",)},
        {"name": "f", "args": ("a:b",)},
        {"name": "f--g"},
        {"name": " f"},
    ],
)
def test_action_call_built_directly_still_obeys_the_grammar(parser, kwargs):
    with pytest.raises(ValueError):
        ActionCall(**kwargs)


def test_tag_rejects_illegal_characters_directly():
    with pytest.raises(ValueError):
        Tag(name="a:b")
    with pytest.raises(ValueError):
        Tag(name="a--b")
    assert "category" not in Tag.model_fields


def test_double_underscore_is_allowed_inside_a_tag(parser: TaggingParser):
    # "__" only separates args of a @@ marker; a tag ends at -- or @@ only.
    out = parser.parse_path(PurePosixPath("docs--api__v2/readme--draft__1.md"))

    assert out.tag_names == ["api__v2", "draft__1"]
    assert out.problems == []


def test_problem_caused_by_extension_stripping_says_so(parser: TaggingParser):
    out = parser.parse_path(PurePosixPath("photo@@make_copy__.jpg"))

    assert out.actions == []
    assert len(out.problems) == 1
    assert "extension '.jpg' was stripped" in out.problems[0].message
    # directories are never stripped, so no hint there
    as_dir = parser.parse_path(PurePosixPath("@@bad_"), is_file=False)
    assert "stripped" not in as_dir.problems[0].message


def test_parse_path_of_empty_path_is_empty(parser: TaggingParser):
    out = parser.parse_path(PurePath(""))

    assert out.tags == [] and out.actions == [] and out.problems == []


# ----------------------------------------------------------------- registry


def test_registered_marker_overrides_default_factory(parser: TaggingParser):
    seen: list[str] = []

    @parser.register("--", "tags")
    def build_tag(value: str) -> Tag:
        seen.append(value)
        return Tag(name=f"custom-{value}")

    out = parser.parse("a--x--y")

    assert seen == ["x", "y"]
    assert [t.name for t in out.tags] == ["custom-x", "custom-y"]
    assert set(parser.markers) == {"--", "@@"}


def test_new_prefix_routes_to_chosen_field(parser: TaggingParser):
    parser.register("%%", "actions")(lambda value: ActionCall(name=f"by_{value}"))

    out = parser.parse("photo--trip%%alice@@resize__100")

    assert [t.name for t in out.tags] == ["trip"]
    assert [a.slug for a in out.actions] == ["by_alice", "resize__100"]
    assert "%%" in parser.pattern


def test_register_validates_inputs(parser: TaggingParser):
    with pytest.raises(ValueError):
        parser.register("", "tags")
    with pytest.raises(ValueError):
        parser.register("##", "nope")
    with pytest.raises(ValueError):
        parser.register("##", "problems")  # reserved for the parser itself

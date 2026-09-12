# Code by AkinoAlice@TyrantRey

from pathlib import PurePath, PurePosixPath, PureWindowsPath

import pytest

from tag_file_system.core.interface.tag import ParseProblem, Tag
from tag_file_system.services.tagging import TaggingParser, split_extension


@pytest.fixture
def parser() -> TaggingParser:
    return TaggingParser()


# ------------------------------------------------------------------ segments


def test_segment_yields_tags(parser: TaggingParser):
    out = parser.parse("report--archive--Q3")

    assert [t.name for t in out.tags] == ["archive", "q3"]
    assert out.problems == []


def test_label_is_ignored_and_optional(parser: TaggingParser):
    with_label = parser.parse("report--finance")
    without_label = parser.parse("--finance")

    assert [t.name for t in with_label.tags] == ["finance"]
    assert [t.name for t in without_label.tags] == ["finance"]
    assert parser.parse("plain-name").tags == []


def test_function_markers_are_rejected_with_a_pointer(parser: TaggingParser):
    # DESIGN/v0-4-0.md §2: `@@` builds nothing; the marker is reported and the
    # rest of the name still parses.
    out = parser.parse("@@make_copy__.jpg__photos--archive--Q3")

    assert [t.name for t in out.tags] == ["archive", "q3"]
    assert [p.marker for p in out.problems] == ["@@make_copy__.jpg__photos"]
    assert ".tfsfunctions.yaml" in out.problems[0].message
    assert "tfs migrate" in out.problems[0].message

    path = parser.parse_path(PurePosixPath("@@resize__800/x--t/@@rotate/a.txt"))
    assert path.tag_names == ["t"]
    assert [(p.segment, p.marker) for p in path.problems] == [
        ("@@resize__800", "@@resize__800"),
        ("@@rotate", "@@rotate"),
    ]


@pytest.mark.parametrize(
    "segment",
    [
        "a--x:y",  # colon
        "a--x/y",  # slash
        "a--x\\y",  # backslash
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
    assert len(out.problems) == 1
    assert "illegal characters" in out.problems[0].message
    assert out.problems[0].segment == segment + "--ok"


@pytest.mark.parametrize(
    "segment, marker",
    [
        ("notes--valid--", "--"),  # trailing tag marker
        ("x--!!!", "--!!!"),  # tag empty after normalization
        ("x@@", "@@"),  # a function marker, empty or not: gone
        ("x@@make_copy__.jpg", "@@make_copy__.jpg"),
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


# -------------------------------------------------------------------- paths


def test_parse_path_merges_parent_first(parser: TaggingParser):
    out = parser.parse_path(PurePosixPath("2024--archive/2024--trip/img--raw.jpg"))

    assert out.path == PurePosixPath("2024--archive/2024--trip/img--raw.jpg")
    assert out.tag_names == ["archive", "trip", "raw"]
    assert out.problems == []


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
    out = parser.parse_path(PurePosixPath("x--/ok--fine/y--.txt"))

    assert out.tag_names == ["fine"]
    assert [(p.segment, p.marker) for p in out.problems] == [
        ("x--", "--"),
        ("y--", "--"),
    ]


def test_parse_path_accepts_windows_paths_and_reports_posix(parser: TaggingParser):
    out = parser.parse_path(PureWindowsPath("2024--f\\sub--t\\file.txt"))

    assert out.path == PurePosixPath("2024--f/sub--t/file.txt")
    assert out.tag_names == ["f", "t"]


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
    "filename, tags",
    [
        ("img--raw.jpg", ["raw"]),
        ("archive.tar.gz", []),
        ("v1.2--beta", ["beta"]),  # suffix ".2--beta" holds a marker
        ("photo.2024--trip", ["trip"]),
        ("notes--v1.0.txt", ["v10"]),  # ".txt" cut, "." then normalized away
        ("report__final.jpg", []),  # "__" is plain text now
        ("a__b--x.tar.gz", ["xtar"]),  # only the last dotted part is cut
    ],
)
def test_filename_extension_rule(parser: TaggingParser, filename, tags):
    out = parser.parse_path(PurePosixPath(filename))

    assert out.tag_names == tags


def test_split_extension_keeps_only_a_tag_marker(parser: TaggingParser):
    assert split_extension("img--raw.jpg") == "img--raw"
    assert split_extension("v1.2--beta") == "v1.2--beta"
    assert split_extension("README") == "README"
    # a `@@` in the suffix no longer holds anything worth keeping
    assert split_extension("photo.jpg@@x") == "photo"
    assert split_extension("a__b.txt") == "a__b"


def test_line_breaks_inside_a_marker_are_a_problem(parser: TaggingParser):
    for segment in ("--a\nb--c", "--a\n", "--a\r\nb"):
        out = parser.parse(segment)
        assert [p.marker for p in out.problems][0].startswith(segment[:3])
        assert "illegal characters" in out.problems[0].message
    assert [t.name for t in parser.parse("--a\nb--c").tags] == ["c"]


def test_unicode_is_nfc_normalized(parser: TaggingParser):
    nfc = parser.parse("--café")
    nfd = parser.parse("--cafe\u0301")

    assert nfc.tags == nfd.tags == [Tag(name="café")]


def test_parse_dedupes_within_a_segment(parser: TaggingParser):
    out = parser.parse("--a--a--A--b")

    assert [t.name for t in out.tags] == ["a", "b"]


def test_factory_errors_and_wrong_models_become_problems(parser: TaggingParser):
    def picky(value: str) -> Tag:
        if value == "boom":
            raise ValueError("no boom allowed")
        return Tag(name=value)

    parser.register("--", "tags")(picky)
    parser.register("%%", "tags")(
        lambda value: ParseProblem(segment="", marker="", message=value)  # wrong model
    )

    out = parser.parse("x--ok--boom%%y")

    assert [t.name for t in out.tags] == ["ok"]
    assert [(p.marker, p.message) for p in out.problems] == [
        ("--boom", "no boom allowed"),
        ("%%y", "factory for '%%' returned ParseProblem, expected Tag"),
    ]


def test_tag_rejects_illegal_characters_directly():
    with pytest.raises(ValueError):
        Tag(name="a:b")
    with pytest.raises(ValueError):
        Tag(name="a--b")
    with pytest.raises(ValueError):
        Tag(name="a@@b")  # still a marker boundary
    assert "category" not in Tag.model_fields


def test_double_underscore_is_plain_text(parser: TaggingParser):
    # "__" separated a function's arguments once; nothing gives it meaning now.
    out = parser.parse_path(PurePosixPath("docs--api__v2/readme--draft__1.md"))

    assert out.tag_names == ["api__v2", "draft__1"]
    assert out.problems == []
    plain = parser.parse_path(PurePosixPath("reports/q3__final__v2.xlsx"))
    assert plain.tags == [] and plain.problems == []


def test_problem_caused_by_extension_stripping_says_so(parser: TaggingParser):
    out = parser.parse_path(PurePosixPath("photo--.jpg"))

    assert out.tags == []
    assert len(out.problems) == 1
    assert "extension '.jpg' was stripped" in out.problems[0].message
    # directories are never stripped, so no hint there
    as_dir = parser.parse_path(PurePosixPath("photo--"), is_file=False)
    assert "stripped" not in as_dir.problems[0].message


def test_parse_path_of_empty_path_is_empty(parser: TaggingParser):
    out = parser.parse_path(PurePath(""))

    assert out.tags == [] and out.problems == []


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


def test_new_prefix_routes_to_tags(parser: TaggingParser):
    parser.register("%%", "tags")(lambda value: Tag(name=f"by-{value}"))

    out = parser.parse("photo--trip%%alice")

    assert [t.name for t in out.tags] == ["trip", "by-alice"]
    assert "%%" in parser.pattern


def test_register_validates_inputs(parser: TaggingParser):
    with pytest.raises(ValueError):
        parser.register("", "tags")
    with pytest.raises(ValueError):
        parser.register("##", "nope")
    with pytest.raises(ValueError):
        parser.register("##", "actions")  # gone with the function markers
    with pytest.raises(ValueError):
        parser.register("##", "problems")  # reserved for the parser itself

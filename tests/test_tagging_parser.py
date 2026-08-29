# Code by AkinoAlice@TyrantRey

import pytest

from tag_file_system.core.interface.tag import Tag, TagAction
from tag_file_system.services.tagging import TaggingParser


def test_default_markers_build_tags_and_actions():
    parser = TaggingParser()

    out = parser.parse("report--Finance--Q3@@archive:days=30,keep=yes")

    assert [t.name for t in out.tags] == ["finance", "q3"]
    assert out.actions == [TagAction(name="archive:days=30,keep=yes")]
    assert out.actions[0].params == {"days": "30", "keep": "yes"}


def test_invalid_marker_is_skipped_not_fatal():
    parser = TaggingParser()

    out = parser.parse("notes--valid--@@")

    assert [t.name for t in out.tags] == ["valid"]
    assert out.actions == []


def test_registered_marker_overrides_default_factory():
    parser = TaggingParser()
    seen: list[str] = []

    @parser.register("--", "tags")
    def build_tag(value: str) -> Tag:
        seen.append(value)
        return Tag(name=f"custom-{value}")

    out = parser.parse("a--x--y")

    assert seen == ["x", "y"]
    assert [t.name for t in out.tags] == ["custom-x", "custom-y"]
    assert set(parser.markers) == {"--", "@@"}


def test_new_prefix_routes_to_chosen_field():
    parser = TaggingParser()

    # "%%" pieces are collected into the actions list via a new factory
    parser.register("%%", "actions")(lambda value: TagAction(name=f"by:{value}"))

    out = parser.parse("photo--trip%%alice@@resize:w=100")

    assert [t.name for t in out.tags] == ["trip"]
    assert [a.name for a in out.actions] == ["by", "resize"]
    assert out.actions[0].params == {}  # "alice" has no k=v form
    assert out.actions[1].params == {"w": "100"}
    assert "%%" in parser.pattern


def test_register_validates_inputs():
    parser = TaggingParser()

    with pytest.raises(ValueError):
        parser.register("", "tags")
    with pytest.raises(ValueError):
        parser.register("##", "nope")

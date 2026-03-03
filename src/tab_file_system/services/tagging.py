# Code by AkinoAlice@TyrantRey
import re

from tab_file_system.core.interface.tag import Tag, TagAction, TagParserOutput


class TaggingParser:
    def __init__(self):
        self.pattern = r"(--|@@)(.*?)(?=--|@@|$)"

    def parse(self, path_string: str) -> TagParserOutput:
        matches = re.findall(self.pattern, path_string)
        tags_results: list[Tag] = []
        actions_results: list[TagAction] = []

        for prefix, value in matches:
            if prefix == "--":
                tags_results.append(Tag(name=value))
            elif prefix == "@@":
                actions_results.append(TagAction(name=value))

        return TagParserOutput(
            tags=tags_results,
            actions=actions_results,
        )

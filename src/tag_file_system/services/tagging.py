# Code by AkinoAlice@TyrantRey

import re
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from tag_file_system.core.interface.tag import Tag, TagAction, TagParserOutput
from tag_file_system.core.logger import logger

MarkerFactory = Callable[[str], BaseModel]


@dataclass(frozen=True)
class Marker:
    prefix: str  # e.g. "--"
    field: str  # TagParserOutput field the built objects are collected into
    factory: MarkerFactory  # raw marker text -> model


class TaggingParser:
    """Split a filename into marker-prefixed pieces and build a model for each.

    Markers are registered, not hard-coded: ``register(prefix, field)`` maps a
    prefix to the ``TagParserOutput`` field its results land in and to a
    factory that turns the raw text into a model. ``--`` (tags) and ``@@``
    (actions) are registered by default.
    """

    def __init__(self) -> None:
        self.logger = logger
        self._markers: dict[str, Marker] = {}

        self.register("--", "tags")(lambda value: Tag(name=value))
        self.register("@@", "actions")(lambda value: TagAction(name=value))

    def register(
        self, prefix: str, field: str
    ) -> Callable[[MarkerFactory], MarkerFactory]:
        if not prefix:
            raise ValueError("Marker prefix cannot be empty")
        if field not in TagParserOutput.model_fields:
            raise ValueError(
                f"Unknown output field {field!r}; "
                f"expected one of {list(TagParserOutput.model_fields)}"
            )

        def decorator(factory: MarkerFactory) -> MarkerFactory:
            self._markers[prefix] = Marker(prefix, field, factory)
            return factory

        return decorator

    @property
    def markers(self) -> dict[str, Marker]:
        return dict(self._markers)

    @property
    def pattern(self) -> str:
        prefixes = "|".join(
            re.escape(prefix) for prefix in sorted(self._markers, key=lambda p: -len(p))
        )
        return rf"({prefixes})(.*?)(?={prefixes}|$)"

    def parse(self, path_string: str) -> TagParserOutput:
        results: dict[str, list[BaseModel]] = {
            marker.field: [] for marker in self._markers.values()
        }

        for prefix, value in re.findall(self.pattern, path_string):
            marker = self._markers[prefix]
            try:
                results[marker.field].append(marker.factory(value))
            except ValidationError as e:
                # One malformed marker (e.g. a trailing "--") must not lose the rest.
                self.logger.warning(
                    f"Skipping invalid marker '{prefix}{value}' in '{path_string}': {e}"
                )

        return TagParserOutput.model_validate(results)

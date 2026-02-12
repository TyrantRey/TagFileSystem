# Code by AkinoAlice@TyrantRey
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field
from watchfiles import Change

EventFilter = Callable[[Path], bool]


class HandlerEntry(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    func: Callable
    filters: list[EventFilter] = Field(default_factory=list)

    def match(self, path: Path) -> bool:
        return all(f(path) for f in self.filters)

    def __call__(self, path: Path) -> None:
        if self.match(path):
            self.func(path)


class WatchEventRouter(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    handlers: dict[Change, list[HandlerEntry]] = Field(
        default_factory=lambda: {
            Change.added: list[HandlerEntry](),
            Change.modified: list[HandlerEntry](),
            Change.deleted: list[HandlerEntry](),
        }
    )

    def _build_filters(self, **kwargs) -> list[EventFilter]:
        filters: list[EventFilter] = []

        if "tag" in kwargs:
            tag = kwargs["tag"]

            def tag_filter(p: Path, t: str = tag) -> bool:
                return t in p.parts

            filters.append(tag_filter)

        if "ext" in kwargs:
            ext = kwargs["ext"]

            def ext_filter(p: Path, e: str = ext) -> bool:
                return p.suffix == e

            filters.append(ext_filter)

        return filters
    def register(self, *events: Change, **kwargs) -> Callable:
        filters = self._build_filters(**kwargs)

        def decorator(func: Callable) -> Callable:
            entry = HandlerEntry(func=func, filters=filters)
            for event in events:
                self.handlers[event].append(entry)
            return func

        return decorator

    def on_added(self, **kwargs) -> Callable:
        return self.register(Change.added, **kwargs)

    def on_modified(self, **kwargs) -> Callable:
        return self.register(Change.modified, **kwargs)

    def on_deleted(self, **kwargs) -> Callable:
        return self.register(Change.deleted, **kwargs)

    def dispatch(self, operation: Change, path: Path) -> None:
        for entry in self.handlers[operation]:
            entry(path)

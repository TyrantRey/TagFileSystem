# Code by AkinoAlice@TyrantRey

from datetime import datetime
from pathlib import Path
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, Field

from tab_file_system.file_data.file_metadata import FileMetadata

EventFilter = Callable[[Path], bool]
T = TypeVar("T")


class HandlerEntry(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    func: Callable
    filters: list[EventFilter] = Field(default_factory=list)

    def match(self, path: Path) -> bool:
        return all(f(path) for f in self.filters)

    def __call__(self, path: Path, metedate: FileMetadata) -> None:
        if self.match(path):
            self.func(path)


class EventRouter(BaseModel, Generic[T]):
    model_config = {"arbitrary_types_allowed": True}

    handlers: dict[T, list[HandlerEntry]] = Field(default_factory=dict)

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

    def register(self, *events: T, **kwargs) -> Callable:
        filters = self._build_filters(**kwargs)

        def decorator(func: Callable) -> Callable:
            entry = HandlerEntry(func=func, filters=filters)
            for event in events:
                if event not in self.handlers:
                    self.handlers[event] = []
                self.handlers[event].append(entry)
            return func

        return decorator

    def dispatch(self, operation: T, path: Path) -> None:
        for entry in self.handlers.get(operation, []):
            file_metadata = FileMetadata(
                file_size=path.stat().st_size, time_added=datetime.utcnow()
            )
            entry(path, file_metadata)

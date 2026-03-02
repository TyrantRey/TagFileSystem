# Code by AkinoAlice@TyrantRey

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, Field

from tab_file_system.file_data.file_metadata import FileMetadata

EventFilter = Callable[[Path, FileMetadata], bool]
T = TypeVar("T")


class HandlerEntry(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    func: Callable
    filters: list[EventFilter] = Field(default_factory=list)

    def match(self, path: Path, metadata: FileMetadata) -> bool:
        return all(f(path, metadata) for f in self.filters)

    def __call__(self, path: Path, metadata: FileMetadata) -> None:
        if self.match(path, metadata):
            self.func(path, metadata)


class EventRouter(BaseModel, Generic[T]):
    model_config = {"arbitrary_types_allowed": True}

    handlers: dict[T, list[HandlerEntry]] = Field(default_factory=dict)

    def _build_filters(
        self,
        tags: list[str] | None = None,
        ext: str | None = None,
        size_gt: int | None = None,
        size_lt: int | None = None,
    ) -> list[EventFilter]:
        filters: list[EventFilter] = []
        if tags:

            def tag_filter(p: Path, m: FileMetadata, t: list[str] = tags) -> bool:
                return any(tag in p.parts for tag in t)

            filters.append(tag_filter)

        if ext:

            def ext_filter(p: Path, m: FileMetadata, e: str = ext) -> bool:
                return m.file_format == e

            filters.append(ext_filter)

        if size_gt is not None:

            def size_gt_filter(p: Path, m: FileMetadata, s: int = size_gt) -> bool:
                return m.file_size > s

            filters.append(size_gt_filter)

        if size_lt is not None:

            def size_lt_filter(p: Path, m: FileMetadata, s: int = size_lt) -> bool:
                return m.file_size < s

            filters.append(size_lt_filter)

        return filters

    def register(
        self,
        *events: T,
        tags: list[str] | None = None,
        ext: str | None = None,
        size_gt: int | None = None,
        size_lt: int | None = None,
        **kwargs,
    ) -> Callable:
        filters = self._build_filters(
            tags=tags, ext=ext, size_gt=size_gt, size_lt=size_lt, **kwargs
        )

        def decorator(func: Callable) -> Callable:
            entry = HandlerEntry(func=func, filters=filters)
            for event in events:
                if event not in self.handlers:
                    self.handlers[event] = []
                self.handlers[event].append(entry)
            return func

        return decorator

    def dispatch(self, operation: T, path: Path) -> None:
        handlers = self.handlers.get(operation, [])
        if not handlers:
            return

        stat = path.stat()
        metadata = FileMetadata(
            file_size=stat.st_size,
            time_added=datetime.now(UTC),
            file_format=path.suffix,
            file_type=path.suffix[1:] if path.suffix else None,
        )
        for entry in handlers:
            entry(path, metadata)

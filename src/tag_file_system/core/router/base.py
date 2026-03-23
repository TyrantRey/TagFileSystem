# Code by AkinoAlice@TyrantRey

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, Field

from tag_file_system.core.interface.file_metadata import FileMetadata
from tag_file_system.core.interface.filter import FileMetadataFilter
from tag_file_system.core.logger import logger

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
        self, file_object: FileMetadataFilter | None
    ) -> list[EventFilter]:
        filters: list[EventFilter] = []

        if file_object is None:
            return filters

        if file_object.tags:
            t_list: list[str] = list(file_object.tags)
            filters.append(lambda p, m: any(tag in p.parts for tag in t_list))

        if file_object.ext:
            target_ext: str = file_object.ext
            filters.append(lambda p, m: m.file_format == target_ext)

        if file_object.size_gt is not None:
            gt_val = file_object.size_gt
            filters.append(lambda p, m: m.file_size > gt_val)

        if file_object.size_lt is not None and file_object.size_lt > 0:
            lt_val = file_object.size_lt
            filters.append(lambda p, m: m.file_size < lt_val)

        return filters

    def register(
        self,
        *events: T,
        file_metadata_filter: FileMetadataFilter | None = None,
        **kwargs,
    ) -> Callable:
        filters = self._build_filters(file_metadata_filter)

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

        try:
            stat = path.stat()
            metadata = FileMetadata(
                file_size=stat.st_size,
                time_added=datetime.now(UTC),
                file_format=path.suffix,
                file_type=path.suffix[1:] if path.suffix else None,
            )
            for entry in handlers:
                entry(path, metadata)
        except FileNotFoundError:
            logger.warning(f"File not found during dispatch: {path}")

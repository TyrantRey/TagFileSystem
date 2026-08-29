# Code by AkinoAlice@TyrantRey

from typing import Callable

from pydantic import Field
from watchfiles import Change

from tag_file_system.core.interface.filter import FileMetadataFilter
from tag_file_system.core.router.base import EventRouter


class WatchEventRouter(EventRouter[Change]):
    allow_missing: set[Change] = Field(default_factory=lambda: {Change.deleted})

    def on_file_added(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.added, **filters)

    def on_file_modified(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.modified, **filters)

    def on_file_deleted(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.deleted, **filters)


watchfile_router = WatchEventRouter()

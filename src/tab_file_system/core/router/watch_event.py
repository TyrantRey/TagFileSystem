# Code by AkinoAlice@TyrantRey

from typing import Callable

from watchfiles import Change

from tab_file_system.core.router.base import EventRouter
from tab_file_system.core.interface.filter import FileMetadataFilter


class WatchEventRouter(EventRouter[Change]):
    def on_file_added(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.added, **filters)

    def on_file_modified(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.modified, **filters)

    def on_file_deleted(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(Change.deleted, **filters)


watchfile_router = WatchEventRouter()

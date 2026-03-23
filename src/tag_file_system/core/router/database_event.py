# Code by AkinoAlice@TyrantRey

from typing import Callable

from tag_file_system.core.interface.database import DatabaseOperation
from tag_file_system.core.interface.filter import FileMetadataFilter
from tag_file_system.core.router.base import EventRouter


class DatabaseEventRouter(EventRouter[DatabaseOperation]):
    def on_insert(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(DatabaseOperation.INSERT, **filters)

    def on_update(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(DatabaseOperation.UPDATE, **filters)

    def on_delete(self, **filters: FileMetadataFilter) -> Callable:
        return self.register(DatabaseOperation.DELETE, **filters)


database_event_router = DatabaseEventRouter()

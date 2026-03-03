# Code by AkinoAlice@TyrantRey

from enum import StrEnum, auto
from typing import Callable, TypeVar, Generic, Any

from tab_file_system.core.router.base import EventRouter
from tab_file_system.core.interface.filter import FileMetadataFilter

T = TypeVar("T")


class DatabaseOperation(StrEnum):
    INSERT = auto()
    UPDATE = auto()
    DELETE = auto()


class DBEvent(Generic[T]):
    op: DatabaseOperation
    data: T
    extra: dict[str, Any]


class DatabaseEventRouter(EventRouter[DatabaseOperation]):
    def on_insert(
        self, **filters: FileMetadataFilter
    ) -> Callable:
        return self.register(DatabaseOperation.INSERT, **filters)

    def on_update(
        self, **filters: FileMetadataFilter
    ) -> Callable:
        return self.register(DatabaseOperation.UPDATE, **filters)

    def on_delete(
        self, **filters: FileMetadataFilter
    ) -> Callable:
        return self.register(DatabaseOperation.DELETE, **filters)


database_event_router = DatabaseEventRouter()

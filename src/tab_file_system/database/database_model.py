# Code by AkinoAlice@TyrantRey

from enum import StrEnum
from typing import Callable

from tab_file_system.event.event_router import EventRouter


class DatabaseOperation(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class DatabaseEventRouter(EventRouter[DatabaseOperation]):
    def on_insert(self, **kwargs) -> Callable:
        return self.register(DatabaseOperation.INSERT, **kwargs)

    def on_update(self, **kwargs) -> Callable:
        return self.register(DatabaseOperation.UPDATE, **kwargs)

    def on_delete(self, **kwargs) -> Callable:
        return self.register(DatabaseOperation.DELETE, **kwargs)


database_event_router = DatabaseEventRouter()

# Code by AkinoAlice@TyrantRey

from typing import Callable, Literal

from tab_file_system.event.event_router import EventRouter

DatabaseOperation = Literal["insert", "update", "delete"]


class SQLiteEventRouter(EventRouter[DatabaseOperation]):
    def on_insert(self, **kwargs) -> Callable:
        return self.register("insert", **kwargs)

    def on_update(self, **kwargs) -> Callable:
        return self.register("update", **kwargs)

    def on_delete(self, **kwargs) -> Callable:
        return self.register("delete", **kwargs)

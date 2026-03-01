# Code by AkinoAlice@TyrantRey

from typing import Callable

from watchfiles import Change

from tab_file_system.event.event_router import EventRouter


class WatchEventRouter(EventRouter[Change]):
    def on_file_added(self, **kwargs) -> Callable:
        return self.register(Change.added, **kwargs)

    def on_file_modified(self, **kwargs) -> Callable:
        return self.register(Change.modified, **kwargs)

    def on_file_deleted(self, **kwargs) -> Callable:
        return self.register(Change.deleted, **kwargs)

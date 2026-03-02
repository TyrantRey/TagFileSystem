# Code by AkinoAlice@TyrantRey

from pathlib import Path

from tab_file_system.engine.model import watchfile_router


@watchfile_router.on_file_added()
def handle_added(path: Path) -> None: ...

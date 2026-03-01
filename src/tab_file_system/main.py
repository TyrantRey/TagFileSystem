# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path
from sys import stdout

from tab_file_system.engine.model import WatchEventRouter
from tab_file_system.engine.watchfile_engine import WatchFileEngine
from tab_file_system.setting import DatabaseSetting, FolderSetting, LoggingSetting

logging_setting = LoggingSetting()
database_setting = DatabaseSetting()
folder_setting = FolderSetting()
logger = logging.getLogger(name=__name__)
logging.basicConfig(
    level=logging_setting.log_level,
    filename=logging_setting.log_file,
    filemode=logging_setting.filemode,
    format="[%(levelname)s] - %(asctime)s - %(message)s - %(pathname)s:%(lineno)d",
)
handler = logging.StreamHandler(stream=stdout)
logger.addHandler(handler)

watch_router = WatchEventRouter()
watchfile_engine = WatchFileEngine(
    files_dir=Path("./system_testing/files"),
    tags_dir=Path("./system_testing/tags"),
    watch_event_router=watch_router,
)
database_file = Path("./system_testing/tag_file_system.sqlite")


@watch_router.on_file_added()
def handle_added(path: Path):
    logger.info(f"File added: {path}")


@watch_router.on_file_deleted()
def handle_delete(path: Path):
    logger.info(f"File deleted: {path}")


@watch_router.on_file_modified()
def handle_modify(path: Path) -> None:
    logger.info(f"File modified: {path}")


if __name__ == "__main__":
    watchfile_engine.start()

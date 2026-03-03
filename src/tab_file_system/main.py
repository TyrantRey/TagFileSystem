# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path
from sys import stdout

from tab_file_system.core.router.database_event import database_event_router
from tab_file_system.core.router.watch_event import watchfile_router
from tab_file_system.services.engine import TagFileEngine
from tab_file_system.config import DatabaseSetting, FolderSetting, LoggingSetting
from tab_file_system.core.interface.file_metadata import FileMetadata


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

tag_file_engine = TagFileEngine(
    files_dir=Path("./system_testing/files"),
    tags_dir=Path("./system_testing/tags"),
    watch_event_router=watchfile_router,
    database_event_router=database_event_router,
)
# database_file = Path("./system_testing/tag_file_system.sqlite")


@watchfile_router.on_file_added()
def handle_added(path: Path, file_metadata: FileMetadata) -> None:
    logger.info(f"File added: {path}, metadata: {file_metadata}")


if __name__ == "__main__":
    tag_file_engine.start()

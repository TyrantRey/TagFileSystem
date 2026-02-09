# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path
from sys import stdout
from typing import Annotated

import typer

from engine._watchfile import WatchFileEngine
from setting import DatabaseSetting, FolderSetting, LoggingSetting

logging_setting = LoggingSetting()
database_setting = DatabaseSetting()
folder_setting = FolderSetting()
logger = logging.getLogger(name=__name__)
logging.basicConfig(
    level=logging_setting.log_level,
    filename=logging_setting.log_file,
    filemode=logging_setting.filemode,
)
handler = logging.StreamHandler(stream=stdout)
logger.addHandler(handler)

app = typer.Typer()


@app.command()
def init(
    files_dir: Annotated[
        Path,
        typer.Argument(
            help="Path for the input file (required)",
        ),
    ] = Path(folder_setting.files_dir),
    tags_dir: Annotated[
        Path,
        typer.Argument(
            help="Path for the tag file (required)",
        ),
    ] = Path(folder_setting.tags_dir),
    database_file: Annotated[
        Path,
        typer.Argument(
            help="Path for the database file (required)",
        ),
    ] = Path(database_setting.db_file),
):
    logger.info(f"Initializing system with file directory: {files_dir}")
    logger.info(f"Initializing system with tag directory: {tags_dir}")
    logger.info(f"Initializing system with database file: {database_file}")

    watchfile_engine = WatchFileEngine(
        files_dir=files_dir,
        tags_dir=tags_dir,
        database_file=database_file,
    )
    watchfile_engine.create_watchfolder()


if __name__ == "__main__":
    app()

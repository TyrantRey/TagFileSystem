# Code by AkinoAlice@TyrantRey

import logging
from pathlib import Path
from sys import stdout
from typing import Annotated

import typer

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
)
handler = logging.StreamHandler(stream=stdout)
logger.addHandler(handler)

app = typer.Typer()


@app.command()
def init(
    files_dir: Annotated[
        Path,
        typer.Argument(
            help="Path for the input file",
        ),
    ] = Path(folder_setting.files_dir),
    tags_dir: Annotated[
        Path,
        typer.Argument(
            help="Path for the tag file",
        ),
    ] = Path(folder_setting.tags_dir),
    database_file: Annotated[
        Path,
        typer.Argument(
            help="Path for the database file",
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


@app.command()
def add(
    file_path: Annotated[
        Path,
        typer.Argument(
            help="Path for the input file",
        ),
    ],
    tags: Annotated[
        str,
        typer.Argument(
            help="Comma-separated list of tags to add to the file",
        ),
    ],
) -> None:
    logger.info(f"Adding file: {file_path} with tags: {tags}")


@app.command()
def remove_tag(
    file_hash: Annotated[
        str,
        typer.Argument(
            help="Hash of the file to remove tags from",
        ),
    ],
    tags: Annotated[
        str,
        typer.Argument(
            help="Comma-separated list of tags to remove from the file",
        ),
    ],
) -> None:
    logger.info(f"Removing tags: {tags} from: {file_hash=}")


@app.command()
def delete(
    file_hash: Annotated[
        str,
        typer.Argument(
            help="Hash of the file to delete",
        ),
    ],
) -> None:
    logger.info(f"Deleting file with: {file_hash=}")


@app.command()
def find(
    tags: Annotated[
        str,
        typer.Argument(
            help="Comma-separated list of tags to find files with",
        ),
    ],
) -> None:
    logger.info(f"Finding files with tags: {tags}")


if __name__ == "__main__":
    app()

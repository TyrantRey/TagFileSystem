# Code by AkinoAlice@TyrantRey

from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


def settings_factory(env_prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


class LoggingSetting(BaseSettings):
    log_level: str = "INFO"
    log_file: Path = Path("tag_file_system.log")
    filemode: str = "w+"

    model_config = settings_factory(env_prefix="LOGGING_")


class DatabaseSetting(BaseSettings):
    db_file: Path = Path("tag_file_system.sqlite")

    model_config = settings_factory(env_prefix="DATABASE_")


class FolderSetting(BaseSettings):
    files_dir: Path = Path("./files")
    tags_dir: Path = Path("./tags")
    
    model_config = settings_factory(env_prefix="FOLDER_")

if __name__ == "__main__":
    logging_setting = LoggingSetting()

    print(logging_setting.model_dump())

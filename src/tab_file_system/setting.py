# Code by AkinoAlice@TyrantRey

from pathlib import Path

from pydantic import field_validator
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

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        level = v.strip().upper()
        if level not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return level

    @field_validator("log_file")
    @classmethod
    def validate_folder_path(cls, v: Path) -> Path:
        if v.is_absolute():
            raise ValueError(f"Path must not be absolute: {v}")
        return v

    model_config = settings_factory(env_prefix="LOGGING_")


class DatabaseSetting(BaseSettings):
    db_file: Path = Path("tag_file_system.sqlite")

    @field_validator("db_file")
    @classmethod
    def validate_folder_path(cls, v: Path) -> Path:
        if v.is_absolute():
            raise ValueError(f"Path must not be absolute: {v}")
        return v

    model_config = settings_factory(env_prefix="DATABASE_")


class FolderSetting(BaseSettings):
    files_dir: Path = Path("./files")
    tags_dir: Path = Path("./tags")

    @field_validator("files_dir", "tags_dir")
    @classmethod
    def validate_folder_path(cls, v: Path) -> Path:
        if v.is_absolute():
            raise ValueError(f"Path must not be absolute: {v}")
        return v

    model_config = settings_factory(env_prefix="FOLDER_")


if __name__ == "__main__":
    logging_setting = LoggingSetting()

    print(logging_setting.model_dump())

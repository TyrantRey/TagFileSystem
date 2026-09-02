# Code by AkinoAlice@TyrantRey

"""``Config``: the ``.tfs/config.toml`` model of DESIGN.md §2."""

import ipaddress
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tag_file_system.core.paths import has_parent_reference, is_anchored

LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


class ConfigError(ValueError):
    """``config.toml`` could not be read or does not validate."""


def _relative_path(v: Path) -> Path:
    # The file is shared between hosts, so an absolute path of *either*
    # flavour is wrong, whichever OS happens to be validating it.
    text = str(v)
    if not text.strip() or text in (".", "./", ".\\"):
        raise ValueError("Path cannot be empty")
    if is_anchored(text) or PureWindowsPath(text).drive:
        # A drive-relative "c:x" is not root-relative on any host either.
        raise ValueError(f"Path must be relative to the root, got {v}")
    if has_parent_reference(PurePosixPath(text.replace("\\", "/"))):
        raise ValueError(f"Path may not leave the root ('..'): {v}")
    return v


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    file: Path = Path(".tfs/tag_file_system.log")  # root-relative

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        level = v.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(
                f"Invalid log level {v!r}; expected one of {sorted(LOG_LEVELS)}"
            )
        return level

    _relative = field_validator("file")(_relative_path)


class DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind: str = "127.0.0.1"
    # strict: TOML is typed, so "80" or true for a port is a mistake to report
    port: int = Field(default=7411, ge=1, le=65535, strict=True)
    stop_timeout_seconds: float = Field(
        default=30, ge=0, strict=True, allow_inf_nan=False
    )
    run_warn_after_seconds: float = Field(
        default=300, gt=0, strict=True, allow_inf_nan=False
    )

    @field_validator("bind")
    @classmethod
    def validate_bind(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"bind must be an IP address, got {v!r}") from None
        return v.strip()


class Config(BaseModel):
    """Contents of ``.tfs/config.toml``. Unknown keys are errors, so a typo in
    the file is reported instead of silently ignored.

    Remote targets are kept as the text written in the file: the file is
    shared between hosts, and converting to a host ``Path`` would rewrite
    ``/home/photo`` as ``\\home\\photo`` on Windows.
    """

    model_config = ConfigDict(extra="forbid")

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    remotes: dict[str, str] = Field(default_factory=dict)

    @field_validator("remotes", mode="before")
    @classmethod
    def validate_remotes(cls, v: object) -> object:
        if isinstance(v, dict):
            for name, target in v.items():
                if not str(name).strip():
                    raise ValueError("Remote names cannot be empty")
                if not isinstance(target, str) or not target.strip():
                    raise ValueError(f"Remote {name!r} must be a non-empty path string")
                rooted = bool(
                    PurePosixPath(target).root or PureWindowsPath(target).root
                )
                if not is_anchored(target) or not rooted:
                    raise ValueError(
                        f"Remote {name!r} must be an absolute path (outside the root), got {target!r}"
                    )
                if has_parent_reference(PurePosixPath(target.replace("\\", "/"))):
                    raise ValueError(
                        f"Remote {name!r} may not contain '..': {target!r}"
                    )
        return v

    # ------------------------------------------------------------------- io

    @classmethod
    def loads(cls, text: str) -> "Config":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"config.toml is not valid TOML: {e}") from e
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"config.toml is invalid: {e}") from e

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as e:
            raise ConfigError(f"{path} is not UTF-8 text: {e}") from e
        except OSError as e:
            raise ConfigError(f"Cannot read {path}: {e}") from e
        return cls.loads(text)

    def dumps(self) -> str:
        """Render as TOML. ``tomllib`` is read-only, and the shape is fixed, so
        a small writer keeps the file human-friendly (comments) without a new
        dependency."""
        lines = [
            "# TagFileSystem root configuration (see DESIGN.md §2).",
            "# Paths are relative to the root unless noted.",
            "",
            "[logging]",
            f"level = {_toml_str(self.logging.level)}",
            f"file  = {_toml_str(self.logging.file.as_posix())}",
            "",
            "[daemon]",
            f"bind = {_toml_str(self.daemon.bind)}     # 0.0.0.0 inside Docker, then port-map",
            f"port = {self.daemon.port}",
            f"stop_timeout_seconds = {_toml_num(self.daemon.stop_timeout_seconds)}",
            f"run_warn_after_seconds = {_toml_num(self.daemon.run_warn_after_seconds)}   # running longer raises P2",
            "",
            '[remotes]   # named destinations outside the root, e.g. photos = "/home/photo"',
        ]
        for name, target in self.remotes.items():
            lines.append(f"{_toml_key(name)} = {_toml_str(target)}")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.write_text(self.dumps(), encoding="utf-8")


def _toml_str(value: str) -> str:
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    if key.isascii() and key.replace("_", "").replace("-", "").isalnum():
        return key
    return _toml_str(key)


def _toml_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


if __name__ == "__main__":
    print(Config().dumps())

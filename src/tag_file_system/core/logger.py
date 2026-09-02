# Code by AkinoAlice@TyrantRey

"""The package logger and its one-time configuration.

importing this module configures nothing: ``configure_logging`` is called
by whatever runs the program (the daemon, ``main.py``), never at import.
"""

import logging
from pathlib import Path
from sys import stdout

FORMAT = "[%(levelname)s] - %(asctime)s - %(message)s - %(pathname)s:%(lineno)d"

logger = logging.getLogger("tag_file_system")
logger.addHandler(logging.NullHandler())

_configured: list[logging.Handler] = []


def configure_logging(
    level: str | int = "INFO",
    file: Path | None = None,
    filemode: str = "a",
    stream: bool = True,
) -> None:
    """Send the package's records to ``file`` and/or stdout at ``level``.

    Calling it again replaces the handlers installed by the previous call.
    """
    for handler in _configured:
        logger.removeHandler(handler)
        handler.close()
    _configured.clear()

    resolved = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    logger.setLevel(resolved)
    formatter = logging.Formatter(FORMAT)
    if file is not None:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file, mode=filemode, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        _configured.append(file_handler)
    if stream:
        stream_handler = logging.StreamHandler(stream=stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        _configured.append(stream_handler)

# Code by AkinoAlice@TyrantRey

"""Path helpers shared by config, root and database code.

Root-relative POSIX keys are what the database stores and what
``config.toml`` carries, so "is this text absolute" must be answered for
*either* OS flavour regardless of the host that asks.
"""

import posixpath
import re
from pathlib import PurePath

# "C:\..." / "C:/...": a drive is an anchor only with a separator after it,
# so that a POSIX filename such as "a:b.txt" is not mistaken for one.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def is_anchored(text: str) -> bool:
    """Absolute in either path flavour: a root, a drive or a UNC prefix."""
    return text.startswith(("/", "\\")) or bool(_DRIVE.match(text))


def posix_key(path: PurePath | str) -> str:
    """Canonical relative key: POSIX separators, ``.`` and ``a/..`` collapsed.

    Raises ``ValueError`` for anchored input or a key that escapes upward.
    """
    text = path if isinstance(path, str) else str(path)
    if is_anchored(text):
        raise ValueError(f"{text!r} is not a relative path")
    # Split on both separators ourselves: the host's PurePath would read a
    # top-level "a:b.txt" (a legal POSIX name) as a drive on Windows.
    joined = "/".join(part for part in re.split(r"[\\/]+", text) if part)
    key = posixpath.normpath(joined) if joined else "."
    if key == ".." or key.startswith("../"):
        raise ValueError(f"{text!r} escapes the root")
    return key


def has_parent_reference(path: PurePath | str) -> bool:
    pure = PurePath(path) if isinstance(path, str) else path
    return ".." in pure.parts

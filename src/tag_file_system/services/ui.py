# Code by AkinoAlice@TyrantRey

"""Serving the built web UI (DESIGN/v0-5-0.md §3.3).

The daemon serves ``<checkout>/Frontend/dist`` at ``/ui/`` — without a token,
because the files hold no data — resolving each request to exactly one file
under that directory: ``/ui`` and ``/ui/`` are ``index.html``, anything else
must name an existing file below ``dist`` after percent-decoding, with no
empty, ``.``, ``..``, backslash, drive or NUL component, resolving inside
``dist`` (symlinks included). There is no single-page fallback: the app routes
in the URL fragment, so every path the server sees is a real file. A daemon
without a build answers 503 so "not built" and "no such file" are told apart.
"""

import mimetypes
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from tag_file_system.core.paths import is_anchored
from tag_file_system.version import REPO

UI_PREFIX = "/ui"
# An install that is not a checkout (the Docker image, DESIGN/v0-5-0.md §10.2)
# says where the built UI lives; an explicit choice beats discovery.
UI_DIR_ENV = "TFS_UI_DIR"
NOT_BUILT = "UI not built: run `npm ci && npm run build` in Frontend/"
IMMUTABLE = "public, max-age=31536000, immutable"  # Vite's hashed assets/
NO_CACHE = "no-cache"  # index.html and root-level files

# The types a Vite build produces, before `mimetypes` is consulted: on Windows
# it reads the registry, which can call a .js file text/plain.
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}


def default_ui_dir(environ: Mapping[str, str] = os.environ) -> Path | None:
    """``$TFS_UI_DIR`` when set, else ``<checkout>/Frontend/dist``, else
    ``None`` (an install that is neither a checkout nor told where the UI
    is)."""
    named = environ.get(UI_DIR_ENV, "").strip()
    if named:
        return Path(named)
    return REPO / "Frontend" / "dist" if REPO is not None else None


def content_type_of(name: str) -> str:
    known = _TYPES.get(Path(name).suffix.lower())
    if known is not None:
        return known
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


@dataclass(frozen=True)
class Asset:
    path: Path
    content_type: str
    cache_control: str


class UiFiles:
    """The built UI on disk, read per request: a build that lands while the
    daemon runs is served at once."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    @property
    def built(self) -> bool:
        return self.directory is not None and (self.directory / "index.html").is_file()

    def describe(self) -> dict[str, object]:
        return {
            "built": self.built,
            "dist": str(self.directory) if self.directory is not None else None,
        }

    def resolve(self, url_path: str) -> Asset | None:
        """The file ``url_path`` names under ``dist``, or ``None``."""
        if self.directory is None or not self.built:
            return None
        if url_path in (UI_PREFIX, UI_PREFIX + "/"):
            relative = "index.html"
        elif url_path.startswith(UI_PREFIX + "/"):
            relative = unquote(url_path[len(UI_PREFIX) + 1 :])
        else:
            return None
        parts = relative.split("/")
        if is_anchored(relative) or any(
            part in ("", ".", "..") or "\\" in part or ":" in part or "\0" in part
            for part in parts
        ):
            return None
        base = self.directory.resolve()
        target = base.joinpath(*parts).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
        cache = IMMUTABLE if len(parts) > 1 and parts[0] == "assets" else NO_CACHE
        return Asset(
            path=target, content_type=content_type_of(target.name), cache_control=cache
        )

# Code by AkinoAlice@TyrantRey

import hashlib
import mimetypes
from pathlib import Path


def compute_file_hash(
    path: Path, algorithm: str = "sha256", chunk_size: int = 1 << 20
) -> str:
    """Hex digest of the file contents, read in ``chunk_size`` blocks."""
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def guess_mime_type(path: Path) -> str | None:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type

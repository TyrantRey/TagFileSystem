# Code by AkinoAlice@TyrantRey

"""Indexing one file into the database (DESIGN.md §5).

Shared by the daemon (watcher events, reconciliation) and the runner
(``ctx.emit`` outputs): stat the file, hash it unless ``(size, mtime_ns)``
match the stored row, insert or refresh the row, and set its tags from the
name grammar.
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.core.interface.tag import ParsedPath
from tag_file_system.database.sqlite import SQLiteBackend
from tag_file_system.root import OutsideRoot, Root
from tag_file_system.services.file_info import compute_file_hash, guess_mime_type
from tag_file_system.services.tagging import TaggingParser


@dataclass
class Indexed:
    file: TaggedFile
    parsed: ParsedPath
    previous: TaggedFile | None  # the row before this indexing (None if new)
    hashed: bool  # False when the stored hash was reused

    @property
    def key(self) -> PurePosixPath:
        return self.file.path

    @property
    def content_changed(self) -> bool:
        return self.previous is None or self.previous.file_hash != self.file.file_hash

    @property
    def new_tags(self) -> list[str]:
        before = {t.name for t in self.previous.tags} if self.previous else set()
        return [t.name for t in self.file.tags if t.name not in before]


class Indexer:
    def __init__(self, root: Root, backend: SQLiteBackend, parser: TaggingParser | None = None) -> None:
        self.root = root
        self.backend = backend
        self.parser = parser if parser is not None else TaggingParser()

    def index(self, path: Path) -> Indexed | None:
        """Insert or refresh the row for ``path``. ``None`` when the path is
        not a file under the root."""
        abs_path = Path(path)
        if not abs_path.is_absolute():
            abs_path = self.root.absolute(PurePosixPath(abs_path.as_posix()))
        try:
            key = self.root.relative(abs_path)
        except OutsideRoot:
            return None
        if not abs_path.is_file():
            return None

        stat = abs_path.stat()
        previous = self.backend.query_file(key, include_deleted=True)
        reuse = (
            previous is not None
            and previous.metadata is not None
            and previous.metadata.mtime_ns == stat.st_mtime_ns
            and previous.metadata.file_size == stat.st_size
        )
        file_hash = previous.file_hash if reuse and previous else compute_file_hash(abs_path)
        details = {
            "file_hash": file_hash,
            "file_size": stat.st_size,
            "file_format": abs_path.suffix or None,
            "file_mime_type": guess_mime_type(abs_path),
            "mtime_ns": stat.st_mtime_ns,
        }
        if previous is None or previous.status == "deleted":
            self.backend.insert(filename=abs_path.name, file_path=key, **details)
        else:
            self.backend.update(file_path=key, **details)

        parsed = self.parser.parse_path(key)
        # The name is authoritative for its own tags; tags added through
        # ctx.tag / the API (everything the previous row carried beyond what
        # its name spelled) survive a re-index.
        extra: list[str] = []
        if previous is not None and previous.status != "deleted":
            spelled = set(self.parser.parse_path(previous.path).tag_names)
            extra = [t.name for t in previous.tags if t.name not in spelled]
        self.backend.set_file_tags(key, [*parsed.tag_names, *extra])
        file = self.backend.query_file(key)
        assert file is not None
        active_previous = previous if previous is not None and previous.status != "deleted" else None
        return Indexed(file=file, parsed=parsed, previous=active_previous, hashed=not reuse)

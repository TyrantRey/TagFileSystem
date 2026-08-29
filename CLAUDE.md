# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

TagFileSystem watches a directory with `watchfiles`, routes file-change events to decorator-registered handlers, parses tags/actions out of filenames (`name--tag1--tag2@@action:key=val.ext`), and persists file metadata to SQLite. Python 3.12, managed with `uv` (src layout, `uv_build` backend). Every source file starts with `# Code by AkinoAlice@TyrantRey`.

**Target design vs. current code.** `DESIGN.md` is the approved v1 design (2026-08-30): git-like `.tfs/` root, `@@func__arg` directory-scoped functions, add-ons in `script/`, run/trace/problem tables, a `tfs` CLI and daemon. **None of it is implemented yet.** Read `DESIGN.md` before implementing any feature or changing the name grammar, schema, configuration, or CLI — it wins over this file for target behaviour; this file describes the code as it is and must be updated as each part of the design lands. `README.md` documents current behaviour and points at `DESIGN.md`.

## Commands

```bash
uv sync                                   # install deps + editable install of tag_file_system
uv run python -m tag_file_system.main     # run the watcher (blocks; Ctrl+C to stop)
uv run ty check src tests                 # type check
uv run pytest -q                          # tests (each test gets its own tmp_path SQLite DB)
uv run python -m tag_file_system.config   # print resolved LoggingSetting (quick config sanity check)
```

- Always run from the repo root: every configured path (`system.db`, `tag_file_system.log`, `./tag_file_system/files`) is relative to CWD, and the settings validators reject absolute paths.
- Run with `python -m tag_file_system.main`, never with a `src.` prefix — imports are `from tag_file_system...`, so `src.tag_file_system.main` breaks.
- Linting/formatting is configured for Trunk (`.trunk/trunk.yaml`: ruff `B,D3,E,F` with `E501` ignored, black, isort profile=black, bandit, markdownlint, prettier). The `trunk` CLI is not installed on this machine; if it is available, use `trunk check` / `trunk fmt`.
- Tests live in `tests/` (pytest is a dev dependency). `tests/helpers.py` has `fetch` / `metadata_of` / `tag_of`, which assert-narrow `Optional` query results so `ty check tests` stays clean.

## Configuration

Three `pydantic-settings` classes in `src/tag_file_system/config.py`, each reading `.env` (no `.env` is committed) with a prefix. Env-var names:

| Class            | Prefix      | Fields (defaults)                                                                 |
| ---------------- | ----------- | --------------------------------------------------------------------------------- |
| `LoggingSetting` | `LOGGING_`  | `LOG_LEVEL` (INFO), `LOG_FILE` (tag_file_system.log), `FILEMODE` (`w+`)           |
| `DatabaseSetting`| `DATABASE_` | `DB_FILE` (system.db)                                                             |
| `FolderSetting`  | `FOLDER_`   | `ROOT_DIR` (./tag_file_system), `FILES_DIR` (root/files), `TAGS_DIR` (root/tags)  |

`FILEMODE=w+` means the log file is truncated on every run.

## Architecture

Composition root is `main.py`: it builds a `TagFileEngine` from the two router singletons and a `SQLiteBackend`, wires the DB router to the backend with `pipeline.database.register_database_pipeline(database_event_router, backend)`, registers demo print handlers, and calls `start()`.

**Event routing (`core/router/`)** — `EventRouter[T]` in `base.py` is a Pydantic generic keyed by an event enum. `register(*events, file_metadata_filter=FileMetadataFilter(...))` returns a decorator; `dispatch(op, path)` `stat()`s the path, builds a `FileMetadata`, and calls every handler whose filters match. Two concrete routers, each exposing a module-level singleton that handlers register against by decorator:

- `WatchEventRouter` (`watch_event.py`), keyed by `watchfiles.Change` → `watchfile_router` with `on_file_added/modified/deleted`.
- `DatabaseEventRouter` (`database_event.py`), keyed by `DatabaseOperation` → `database_event_router` with `on_insert/update/delete`.

Handler signature is `(path: Path, metadata: FileMetadata) -> None`. `dispatch` `stat()`s the path first; if it is gone, handlers still run only for events in the router's `allow_missing` set (`Change.deleted` / `DatabaseOperation.DELETE`), with `file_size=0`. Each handler runs in its own `try`: a raising handler is logged with `logger.exception` and the remaining handlers still run, so one bad handler cannot kill the watch loop.

**Engine loop (`services/engine.py`)** — `TagFileEngine.start()` watches `files_dir.parent` (i.e. `root_dir`) and feeds each `watchfiles` batch to `process_changes(changes)` (callable directly from tests), which consolidates to one `Change` per path (priority `deleted > added > modified`), dispatches to `watchfile_router`, then maps `Change` → `DatabaseOperation` and dispatches to `database_event_router`. `start()` closes the DB connection on exit / Ctrl+C.

**DB pipeline (`pipeline/database.py`)** — `register_database_pipeline(router, backend)` registers the INSERT/UPDATE/DELETE handlers. They skip directories, hash the file (`services/file_info.py`: sha256 + `mimetypes`), call `backend.insert` / `backend.update` (UPDATE on an unknown path falls back to `insert`), then `backend.set_file_tags(path, names)` with the `--tags` parsed from `path.stem` by `TaggingParser`; `@@` markers (`ActionCall`s) are parsed but only logged until the add-on slice lands. DELETE calls `backend.delete` (soft delete).

**Persistence (`database/sqlite.py`)** — `SQLiteBackend` implements `DatabaseEngineProtocol` (`core/interface/database.py`). Schema lives inline in `init_database` (WAL, `foreign_keys=ON`; tables `files`, `tags`, `tagged_files`, `events`; ids are uuid4 strings, timestamps are unix-epoch ints from `unixepoch()`). `@transactional` opens `BEGIN IMMEDIATE`, commits on success, rolls back and re-raises on exception; a transactional method called from inside another one joins the outer transaction. Every mutation returns a `SQLResult` (`operation_id`: uuid for the call, reused as `events.id` when exactly one event is written; `record_id`: affected row id). Semantics:

- `insert`: new path → `SUCCESS` / `file.insert`; existing active path → `ALREADY_EXISTS` / `file.insert.duplicate` (row untouched); existing soft-deleted path → revived in place (same id, tags kept, new metadata) → `SUCCESS` / `file.insert.restore`.
- `update`: refreshes hash/size/format/mime and sets `status='active'`; `NOT_FOUND` for an unknown path.
- `delete`: **soft** — `status='deleted'`, `deleted_at` set, tags kept; `ALREADY_EXISTS` if already deleted, `NOT_FOUND` if unknown.
- `modify(path, new_path)`: rename/move; `ALREADY_EXISTS` if `new_path` is already a row.
- `upsert_tag`, `set_file_tags(path, names)`: tags are shared entities and are never deleted; `set_file_tags` makes `names` the exact link set for the file (writes `tag.assign` / `tag.unassign` events).
- `query_tag(tag_name | tag_id)`, `query_file(path)`, `query_files(...)` return `file_metadata.Tag` / `TaggedFile`. Queries exclude soft-deleted rows unless `include_deleted=True`. `tags` / `tag_ids` are ANDed (file must carry all of them); an unknown tag name yields `[]`. `filename` is a case-insensitive substring match; `file_format` is `.ext`, `file_type` is `ext`, `mime_type` matches `files.mime_type`.

**Name grammar (`services/tagging.py`, `core/interface/tag.py`)** — implements DESIGN.md §3. `TaggingParser.parse(segment)` parses one directory name or filename stem; `parse_path(root_relative_path, is_file=True)` parses every segment and merges parent-first (tags = union, first occurrence wins; actions = distinct `(name, args)` in parent-first order, filename last). It rejects absolute paths and always reports `ParsedPath.path` as `PurePosixPath`. The parser is registry-driven: `register(prefix, field)` maps a marker prefix to a `TagParserOutput` field (`tags` or `actions`; `problems` is reserved) and a `str -> model` factory; `--` → `Tag(name=...)` and `@@` → `ActionCall.from_marker` are registered in `__init__`. `ActionCall` is frozen (`name` lowercased and matched against `[a-z][a-z0-9_]*`, `args` split on `__` and kept verbatim; nothing may start or end with `_`); `.slug` is the on-disk text without `@@`. `Tag.name` is normalized (lowercase, non-`\w-` stripped, hyphens collapsed). A marker containing any of `: / \ < > | ? * "`, or one its factory rejects, becomes a `ParseProblem` (segment, marker, message) and is skipped; the rest of the segment still parses. The DB pipeline still calls `parse(path.stem)` only — `parse_path` is wired in by the engine slice.

**Gotchas**

- There are two unrelated `Tag` models: `core/interface/file_metadata.Tag` (`name`, `tag_id`, `time_added` — returned by DB queries) and `core/interface/tag.Tag` (`name`, `category` — produced by the parser). Check which one you're importing.
- `FileMetadata.file_format` is the suffix with its dot (`.txt`), `file_type` the suffix without it (`txt`), `mime_type` a separate optional field; the DB backend follows the same convention.
- `SQLiteBackend` holds one connection and is not thread-safe (default `check_same_thread`); the watch loop is single-threaded, so this is fine today.
- `core/logger.py` calls `logging.basicConfig(...)` and instantiates `LoggingSetting()` at import time; importing almost any module triggers it.
- `pipeline/default.py` registers a handler on import but is not imported anywhere; `core/interface/engine.py::operation_mapping` is currently unused.
- The package was renamed from `tab_file_system` to `tag_file_system`; stale references may linger in caches (`.mypy_cache`) but not in source.

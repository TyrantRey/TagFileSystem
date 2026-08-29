# TagFileSystem

Watches a directory, reads tags out of file names (`report--finance--urgent.pdf`),
and keeps file metadata and tags in SQLite so they can be queried later.

> **Where this is going.** The approved v1 design — directory-scoped functions
> (`@@make_copy__.jpg__photos/`), user-written add-ons that run on the files
> beneath them, a full run/trace/problem log, a `tfs` CLI and a per-root daemon
> — lives in [`DESIGN.md`](DESIGN.md). It is **not implemented yet**; this
> README documents what the code does today.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

## Install & run

```bash
git clone <repository-url>
cd TagFileSystem
uv sync                                  # installs deps and the package (editable)
uv run python -m tag_file_system.main    # start watching; Ctrl+C to stop
```

Run from the repository root: every configured path is relative to the current
directory, and absolute paths are rejected by the settings validators.

On first run the watcher creates `./tag_file_system/files/` and
`./tag_file_system/tags/`, opens `system.db`, and logs to
`tag_file_system.log`. Drop files into `tag_file_system/files/` and they are
recorded.

## Configuration

Settings are read from environment variables or a `.env` file in the CWD.

| Variable             | Default             | Meaning                                  |
| -------------------- | ------------------- | ---------------------------------------- |
| `LOGGING_LOG_LEVEL`  | `INFO`              | log level                                |
| `LOGGING_LOG_FILE`   | `tag_file_system.log` | log file (relative)                    |
| `LOGGING_FILEMODE`   | `w+`                | file mode — `w+` truncates on every run  |
| `DATABASE_DB_FILE`   | `system.db`         | SQLite file (relative)                   |
| `FOLDER_ROOT_DIR`    | `./tag_file_system` | root; the watcher watches this directory |
| `FOLDER_FILES_DIR`   | `<root>/files`      | where managed files live                 |
| `FOLDER_TAGS_DIR`    | `<root>/tags`       | created, currently unused                |

## Name syntax (today)

Markers are parsed from the file's stem:

- `--tag` — a tag. `photo--holiday--2024.jpg` gets tags `holiday`, `2024`.
  Tag names are lowercased, non-word characters stripped, hyphens collapsed.
- `@@action:key=val` — an action. Parsed and logged, **not executed**. On
  Windows `:` cannot appear in a file name at all, so such files can only exist
  on Linux/macOS. (`DESIGN.md` replaces this syntax.)

## What happens on a change

1. `watchfiles` reports a batch of changes; the engine keeps one change per
   path (`deleted` beats `added` beats `modified`).
2. Each change is dispatched to the watch router (`on_file_added` /
   `on_file_modified` / `on_file_deleted` handlers — `main.py` registers demo
   `print` handlers).
3. The change is mapped to a database operation and dispatched to the database
   router, whose handlers hash the file (sha256), guess its MIME type, write the
   `files` row and set the file's tags from its name.

Deletes are **soft**: the row gets `status = 'deleted'` and keeps its tags.
Re-adding the same path revives the row with its old id and tags.

## Database

`system.db`, WAL mode, foreign keys on. Tables:

- `files` — id (uuid4), filename, path, sha256, size, format (`.ext`),
  mime_type, status (`active` / `deleted` / `archived`), timestamps.
- `tags` — id, name (unique), category, description.
- `tagged_files` — (tag_id, file_id) links.
- `events` — audit trail: `file.insert`, `file.insert.restore`,
  `file.update`, `file.delete`, `tag.assign`, `tag.unassign`, …

The authoritative schema is in `src/tag_file_system/database/sqlite.py`.

## Project layout

```text
src/tag_file_system/
├── main.py                  # composition root: routers + SQLiteBackend + engine
├── config.py                # pydantic-settings classes (LOGGING_ / DATABASE_ / FOLDER_)
├── core/
│   ├── interface/           # protocols and models (database, file_metadata, tag, filter)
│   ├── router/              # EventRouter base, watch router, database router
│   └── logger.py
├── services/
│   ├── engine.py            # TagFileEngine: watch loop and change consolidation
│   ├── tagging.py           # TaggingParser (registry of marker prefixes)
│   └── file_info.py         # sha256 + MIME guessing
├── pipeline/
│   └── database.py          # INSERT/UPDATE/DELETE handlers that talk to the backend
└── database/
    └── sqlite.py            # SQLiteBackend
tests/                       # pytest suite; each test gets its own tmp SQLite DB
```

## Development

```bash
uv run pytest -q              # tests
uv run ty check src tests     # type check
uv run python -m tag_file_system.config   # print the resolved logging settings
```

Linting/formatting is configured for [Trunk](https://trunk.io) (ruff, black,
isort, bandit, markdownlint, prettier) in `.trunk/trunk.yaml`.

Every source file starts with `# Code by AkinoAlice@TyrantRey`.

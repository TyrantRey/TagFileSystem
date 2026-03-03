# TagFileSystem

A Python-based file management system that monitors directories in real-time, automatically extracts and manages tags from file and folder names, and stores metadata in SQLite.

## Overview

TagFileSystem provides a flexible, event-driven architecture for organizing and tagging files based on naming conventions. It watches your file system for changes and processes special syntax in filenames to assign tags and execute actions on files.

## Features

- **Real-time File Monitoring**: Automatically detects file additions, modifications, and deletions using efficient file system watching
- **Tag Parsing**: Extracts tags from filenames using intuitive syntax (e.g., `filename--tag1--tag2.ext`)
- **Action Handling**: Process actions embedded in filenames (e.g., `@@move:destination=/path`)
- **Persistent Storage**: SQLite backend stores file metadata and tags for quick lookup and reporting
- **Event Routing**: Generic, reusable event dispatcher with filtering capabilities
- **Configurable**: Environment-based configuration with Pydantic validation

## Installation

### Prerequisites

- Python 3.14+
- `uv` package manager (recommended)

### Setup

1. Clone the repository:

```bash
git clone <repository-url>
cd TagFileSystem
```

2. Install dependencies using `uv`:

```bash
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

## Quick Start

Run the main application to start monitoring your configured directories:

```bash
python -m src.tab_file_system.main
```

The system will:

1. Watch configured directories for file changes
2. Parse tags from file/folder names
3. Store metadata in SQLite
4. Log all events to configured output

## Configuration

Configure the system via environment variables or a `.env` file:

### Logging Settings

```text
LOGGING_LEVEL=INFO              # Log level (DEBUG, INFO, WARNING, ERROR)
LOGGING_FILE=logs/app.log       # Log file path (relative)
LOGGING_MODE=a                  # File mode (a=append, w=write)
```

### Database Settings

```text
DATABASE_FILE=data/tags.db      # SQLite database path (relative)
```

### Folder Settings

```text
FOLDER_FILES_DIR=files          # Directory to watch for files (relative)
FOLDER_TAGS_DIR=tags            # Directory to watch for tags (relative)
```

All paths must be relative (non-absolute).

## Usage

### Tag Syntax

Tags are embedded in filenames using special syntax:

**Tags**: Use `--` prefix followed by tag name

```text
myfile--project--urgent.txt
```

This extracts tags: `project`, `urgent`

**Actions**: Use `@@` prefix followed by action name and parameters

```text
myfile@@move:destination=/archive.txt
```

This triggers a `move` action with the `/archive` destination parameter

**Combined**:

```text
report--finance--review@@send:email=boss@company.txt
```

### Event Handling

Events are dispatched through the `WatchEventRouter` and can be handled by registering callbacks:

```python
from src.tab_file_system.engine.watchfile_engine import watch_engine

@watch_engine.on_file_event
def handle_file_change(event):
    # Process file event
    print(f"File {event.path} was {event.action}")
```

### API

See source code documentation for detailed API information:

- `src.tab_file_system.engine`: File watching and event routing
- `src.tab_file_system.tag`: Tag parsing and models
- `src.tab_file_system.database`: SQLite backend
- `src.tab_file_system.file_data`: File metadata models

## Architecture

```text
TagFileSystem
├── Engine Layer (engine/)
│   ├── WatchFileEngine: Monitors file system changes
│   └── WatchEventRouter: Routes file events
├── Event System (event/)
│   └── EventRouter: Generic event dispatcher with filtering
├── Tag Processing (tag/)
│   ├── TaggingParser: Extracts tags from filenames
│   └── Models: Tag, TagAction, and related data structures
├── Data Layer (file_data/ & database/)
│   ├── FileMetadata: File and tag models
│   └── SQLiteBackend: Persistent storage
└── Configuration (setting.py)
    └── Pydantic-based settings for logging, database, folders
```


## Database Schema

### `files`

| Column        | Type    | Constraints                                                                    |
| ------------- | ------- | ------------------------------------------------------------------------------ |
| `id`          | TEXT    | PRIMARY KEY                                                                    |
| `path`        | TEXT    | NOT NULL, UNIQUE                                                               |
| `hash`        | TEXT    | —                                                                              |
| `size`        | INTEGER | NOT NULL, DEFAULT 0                                                            |
| `format`      | TEXT    | —                                                                              |
| `mime_type`   | TEXT    | —                                                                              |
| `status`      | TEXT    | NOT NULL, DEFAULT `'active'`, CHECK IN (`'active'`, `'deleted'`, `'archived'`) |
| `created_at`  | INTEGER | NOT NULL, DEFAULT `unixepoch()`                                                |
| `modified_at` | INTEGER | NOT NULL, DEFAULT `unixepoch()`                                                |
| `deleted_at`  | INTEGER | —                                                                              |

> Indexes

- `idx_files_hash` ON `hash` WHERE `hash IS NOT NULL`
- `idx_files_status` ON `status`
- `idx_files_created_at` ON `created_at`

---

### `tags`

| Column        | Type    | Constraints                     |
| ------------- | ------- | ------------------------------- |
| `id`          | TEXT    | PRIMARY KEY                     |
| `name`        | TEXT    | NOT NULL, UNIQUE                |
| `category`    | TEXT    | —                               |
| `description` | TEXT    | —                               |
| `created_at`  | INTEGER | NOT NULL, DEFAULT `unixepoch()` |
| `updated_at`  | INTEGER | NOT NULL, DEFAULT `unixepoch()` |

> Indexes
- `idx_tags_category` ON `category`

---

### `actions`

| Column        | Type    | Constraints                     |
| ------------- | ------- | ------------------------------- |
| `id`          | TEXT    | PRIMARY KEY                     |
| `name`        | TEXT    | NOT NULL, UNIQUE                |
| `description` | TEXT    | —                               |
| `icon`        | TEXT    | —                               |
| `category`    | TEXT    | —                               |
| `is_active`   | INTEGER | NOT NULL, DEFAULT 1             |
| `created_at`  | INTEGER | NOT NULL, DEFAULT `unixepoch()` |

> Indexes
- `idx_actions_is_active` ON `is_active`

---

### `tagged_files`

| Column        | Type    | Constraints                                  |
| ------------- | ------- | -------------------------------------------- |
| `tag_id`      | TEXT    | NOT NULL, FK → `tags(id)` ON DELETE CASCADE  |
| `file_id`     | TEXT    | NOT NULL, FK → `files(id)` ON DELETE CASCADE |
| `assigned_at` | INTEGER | NOT NULL, DEFAULT `unixepoch()`              |

**Primary Key:** (`tag_id`, `file_id`)

> Indexes
- `idx_tagged_files_file_id` ON `file_id`

---

### `tag_actions`

| Column       | Type    | Constraints                                                                  |
| ------------ | ------- | ---------------------------------------------------------------------------- |
| `tag_id`     | TEXT    | NOT NULL, FK → `tags(id)` ON DELETE CASCADE                                  |
| `action_id`  | TEXT    | NOT NULL, FK → `actions(id)` ON DELETE CASCADE                               |
| `trigger_on` | TEXT    | NOT NULL, DEFAULT `'manual'`, CHECK IN (`'manual'`, `'auto'`, `'scheduled'`) |
| `created_at` | INTEGER | NOT NULL, DEFAULT `unixepoch()`                                              |

**Primary Key:** (`tag_id`, `action_id`)

---

### `events`

| Column        | Type    | Constraints                         |
| ------------- | ------- | ----------------------------------- |
| `id`          | TEXT    | PRIMARY KEY                         |
| `name`        | TEXT    | NOT NULL                            |
| `description` | TEXT    | —                                   |
| `file_id`     | TEXT    | FK → `files(id)` ON DELETE SET NULL |
| `tag_id`      | TEXT    | FK → `tags(id)` ON DELETE SET NULL  |
| `occurred_at` | INTEGER | NOT NULL, DEFAULT `unixepoch()`     |

> Indexes
- `idx_events_occurred_at` ON `occurred_at`
- `idx_events_file_id` ON `file_id`
- `idx_events_tag_id` ON `tag_id`

### Data Flow

1. File system changes detected → WatchFileEngine
2. Changes consolidated (e.g., delete+add → modified)
3. Events dispatched via WatchEventRouter
4. Handlers process events and parse tags
5. Metadata stored in SQLite database

## Development

### Project Structure

```text
src/tab_file_system/
├── main.py                 # Entry point
├── setting.py              # Configuration classes
├── engine/                 # File watching logic
├── event/                  # Event routing system
├── tag/                    # Tag parsing
├── file_data/              # Data models
├── database/               # SQLite backend
└── pipeline/               # Processing pipelines (extensible)
```

### Technologies

- **Pydantic**: Data validation and configuration management
- **watchfiles**: Efficient file system monitoring
- **SQLite3**: Built-in database backend
- **Typer**: CLI framework (foundation for future expansion)

### Running in Development

```bash
# Watch files with debug logging
LOGGING_LEVEL=DEBUG python -m src.tab_file_system.main
```

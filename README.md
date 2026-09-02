# TagFileSystem

[![test](https://github.com/TyrantRey/TagFileSystem/actions/workflows/test.yml/badge.svg)](https://github.com/TyrantRey/TagFileSystem/actions/workflows/test.yml)

Tags and functions live in your file and folder names. A daemon watches a
root, records every file in SQLite, and runs your own Python add-ons on the
files whose names ask for them:

```
photos/
  @@make_copy__.jpg__backup/       # every file below runs script/make_copy.py
    2024--trip/                    # ...and carries the tag "trip"
      beach--favorite.jpg          # tags: trip, favorite
```

Everything that happens — tags, runs, what a run produced, what went wrong —
is queryable, by you or by a tool. [`DESIGN/`](DESIGN) holds the approved
designs, one per release; this README is the short version.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)

## Install

```bash
git clone <repository-url>
cd TagFileSystem
uv sync                      # installs the package and the `tfs` command
```

## Quick start

```bash
uv run tfs init ~/photos     # creates ~/photos/.tfs/ and ~/photos/script/
cd ~/photos
uv run tfs start             # reconciles the tree, then watches it (Ctrl+C to stop)
```

In another shell:

```bash
uv run tfs query -t trip                 # files carrying the tag
uv run tfs query --under @@make_copy --runs --json
uv run tfs list                          # loaded add-ons and their arguments
uv run tfs reload                        # after editing config.toml
uv run tfs stop
```

`tfs start -d` runs the daemon in the background (output in
`.tfs/daemon.out`); every command accepts `--root <dir>` instead of
discovering the root from the current directory. `tfs --version` prints the
version and the commit it runs from (`0.3.0 (abc1234)`).

## How to use

The daemon does nothing you did not write into a name. A typical setup:

1. **Make a folder a root.** `tfs init ~/photos` creates `~/photos/.tfs/`
   (config, database, token) and `~/photos/script/`. Existing files are
   left alone; they are indexed when the daemon first starts. A root cannot
   sit inside another root.
2. **Put your add-ons in `script/`.** One file per function, named after
   it: `script/make_copy.py` is what `@@make_copy` calls. Start from
   [`examples/script/`](examples/script). Each add-on marks its handlers
   with `@action.added()`, `@action.removed()` and friends, and its
   arguments come from the name (see [Add-ons](#add-ons)).
3. **Name files and folders.** `--tag` gives a file a tag,
   `@@func__arg__arg` asks for a function; both are inherited from every
   parent directory (see [Names](#names)). Path-valued arguments are never
   literal paths: `@@make_copy__.jpg__backup` names the `[remotes]` entry
   `backup` in `.tfs/config.toml`.
4. **Edit `.tfs/config.toml`** for the control channel address (`[daemon]
   bind`/`port`), how long `stop` waits for running add-ons
   (`stop_timeout_seconds`), when a run is reported as overdue
   (`run_warn_after_seconds`), logging, and `[remotes]`.
5. **Start the daemon.** `tfs start` reconciles the tree (indexes every
   file, runs the functions the names ask for) and then watches it; use it
   in the foreground under Docker or a service manager, `tfs start -d` at a
   shell. A daemon holds `.tfs/lock`: one per root, on one machine.
6. **Ask questions.** `tfs query -t trip` lists files by tag, `--runs`
   adds what ran on each, `--json` is for scripts; `tfs list` shows the
   loaded add-ons, their arguments and anything that failed to load.
   Both talk to the daemon; with no daemon running they read `script/` and
   the database directly (never over a network mount).
7. **Change things while it runs.** Add-ons are re-imported the moment
   their file changes; `tfs reload` re-reads `config.toml` (and add-ons).
   A renamed `@@` directory re-runs its function on the files under it; an
   edited script does not re-run old files, because a run is keyed by file
   content, add-on, hook and arguments.
8. **Stop.** `tfs stop` asks the daemon to finish in-flight runs and exit;
   a run still going after `stop_timeout_seconds` is marked `interrupted`
   and reported as a problem.
9. **Keep it current.** `tfs update` says whether a newer release tag
   exists; `tfs upgrade` applies it, snapshotting every database first
   (see [Self-update](#self-update)).

Every command finds its root by walking up from the current directory,
exactly like git finds `.git`; `--root <dir>` (or `-r`) names it instead.
Problems — a script that failed to import, a run that raised, a file
changed while a run was using it — are recorded in the database and handed
to your problem handlers (`@action.err()` etc.); nothing is printed to a
console nobody is watching.

## Commands

All commands are `tfs <command>`; with `uv` that is `uv run tfs <command>`,
or activate the virtual environment once and call `tfs` directly. `--root DIR`
/ `-r DIR` may come before or after the command name.

| Command | Options | Does |
| --- | --- | --- |
| `tfs init [DIR]` | | Turn `DIR` (default: the current directory) into a root: `.tfs/`, `script/`, an empty database. Refuses inside or above an existing root. |
| `tfs list` | `--json` | The loaded add-ons with their hooks and typed arguments, and per-script load problems. From the daemon, or from `script/` when none runs. |
| `tfs query` | `-t/--tag TAG` (repeatable, ANDed), `--name TEXT`, `--format .EXT`, `--mime TYPE` or `family/*`, `--under DIR`, `--deleted`, `--runs`, `--json` | Files matching every criterion. `--under` is a root-relative directory such as `@@make_copy`; `--runs` adds each file's run history. |
| `tfs reload` | | Re-read `config.toml` and re-import every add-on in the running daemon. `[daemon] bind`/`port` take effect at the next `start`. |
| `tfs start` | `-d/--detach`, `--force` | Reconcile, then watch. `-d` detaches (output in `.tfs/daemon.out`) and returns once the control channel answers. `--force` takes over a lock left by a daemon that is gone or on another host, never one held by a live local process. |
| `tfs stop` | `--timeout SEC` | Stop gracefully; falls back to signalling the pid in `.tfs/lock` when the daemon does not answer, only for a lock written on this host. Default wait: `stop_timeout_seconds + 5`. |
| `tfs update` | `--json` | Fetch the release tags from `origin` and report the current and newest version, the schema change and every registered root. Changes nothing. |
| `tfs upgrade` | `--to TAG`, `--dry-run`, `-y/--yes`, `--skip-tests`, `--wait SEC` | Move the checkout to the newest release tag (or `--to`), snapshot every root, run the suite, restart the daemons, revert on failure. `--yes` consents to a schema change; `--wait` is how long to let in-flight runs finish first. |
| `tfs backup list` | `--json` | The snapshots in `.tfs/backups/`, newest first, with size and origin tag. |
| `tfs backup prune` | `--keep N` (default 3), `--dry-run`, `-y/--yes` | Delete all but the newest `N` snapshots; asks first unless `--yes`. |
| `tfs --version` | | `0.3.0 (abc1234)`: the version in `pyproject.toml` and the commit of the checkout. |

Exit status is `0` on success and `1` on any error (no root, no daemon
where one is needed, a refused lock, a failed check). `tfs query` exits
`2` for a bad `--under` or a blank `--tag`; `tfs upgrade` exits `2` when
an upgrade failed *and* could not be reverted, after printing the manual
steps. `tfs update` exits `0` whether or not an update is available; only
a failed check is non-zero.

## Names

Every directory segment and the filename stem follow the same grammar:

| Marker              | Meaning                                                |
| ------------------- | ------------------------------------------------------ |
| `--tag`             | the file carries `tag` (lowercased, `[\w-]` only)      |
| `@@func__a__b`      | run add-on `script/func.py` with args `a`, `b`         |

Tags and functions are inherited from every parent directory, parent first.
`:`, `/`, `\`, `<`, `>`, `|`, `?`, `*`, `"` cannot appear in a marker (the
names must work on Windows and on the NAS). Path-valued arguments are never
literal paths: `dst: action.TagDir` names the directory that carries that
tag, `dst: action.Remote` names an entry of `[remotes]` in `config.toml`.

## Add-ons

`script/make_copy.py`:

```python
from pathlib import Path
from tag_file_system import action

@action.added()
def run(path: Path, metadata, ctx, suffix: str = ".jpg", dst: action.Remote = None):
    if path.suffix != suffix:
        return "skipped"
    ctx.log(f"copying {path.name}")
    return ctx.copy(path, dst / path.name)     # traced, and recorded as produced by this run

@action.removed(on_move=True)
def gone(path, metadata, ctx, suffix: str = ".jpg", dst: action.Remote = None):
    ...

@action.err()
def notify(problem, ctx):                       # every P1 and P0
    ctx.log(f"{problem.kind}: {problem.message}")

@action.on_start()                              # the daemon is up, before any file
def up(ctx):                                    # (@action.on_stop() when it goes down)
    ctx.log("ready")
```

- Hooks: `added`, `modified`, `removed(on_move=...)`, `tagged("x")`; problem
  handlers `crit`, `err`, `warn`, `info` receive their level and above;
  `on_start` / `on_stop` bracket the daemon session.
- Arguments after `(path, metadata, ctx)` come from the name, coerced by
  their annotations; one handler per hook per add-on. Problem handlers take
  `(problem, ctx)`, lifecycle handlers `(ctx)` — they have no file, so
  `ctx.file` and `ctx.path` are `None`.
- `on_start` runs once per daemon session per add-on: at `tfs start`, and as
  soon as a script that appears later is loaded. `on_stop` runs at shutdown,
  before in-flight runs are waited for — the place to signal a service thread
  an `on_start` spawned (`ctx.spawn` … `ctx.done()`) that it should leave.
- `ctx` offers `copy/move/write/delete/emit`, `record/log`, `spawn/done` for
  background work, `tag/untag`, `query`, `problem`, `retry`, `resolve`.
- Scripts are hot-reloaded when they change; helpers are `_name.py`.
- [`examples/script/`](examples/script) holds `make_copy.py` (copy to a
  remote, drop the copy when the source leaves) and `notify.py` (a problem
  handler): copy them into `script/` to try the daemon.
- A run happens once per `(file content, add-on, hook, args)`: editing a
  script does not re-run old files, renaming an `@@` directory does.

## Root layout

```
<root>/
  .tfs/config.toml     [logging], [daemon] bind/port, [remotes]
  .tfs/db/system.db    files, tags, runs, traces, provenance, problems, upgrades
  .tfs/backups/        database snapshots taken by `tfs upgrade`
  .tfs/token           bearer token of the control channel
  script/              add-ons
  ...                  your files
```

The daemon exposes a small HTTP API on `[daemon] bind:port` (default
`127.0.0.1:7411`), authenticated with the token: `/health` (which reports
the version and commit the daemon runs), `/stop`, `/reload`, `/actions`,
`/files`. In Docker, set `bind = "0.0.0.0"` and map the port; mount the root
as a volume.

## Self-update

The install is a git checkout, and it can move itself to the next release:

```bash
uv run tfs update                    # fetch the release tags, report, change nothing
uv run tfs update --json             # for a cron wrapper: exit 0, "available": true/false
uv run tfs upgrade --dry-run         # print the plan: target, schema change, every known root
uv run tfs upgrade                   # do it (--yes for cron, --to v0.3.0 for a specific tag)
uv run tfs backup list               # the snapshots in .tfs/backups/
uv run tfs backup prune --keep 3     # the retention `upgrade` applies on its own
```

`upgrade` only follows **annotated release tags on `origin`** (never the tip
of a branch) and only from a clean checkout that is on `master` or exactly at
a release tag. One checkout serves every root on the machine, so it works on
all of them: every root a `tfs` command touches is remembered in
`%APPDATA%\tfs\roots.json` (Windows) or `~/.config/tfs/roots.json`
(`$XDG_CONFIG_HOME`), and roots that no longer exist are dropped. The
sequence, per [`DESIGN/v0-2-0.md`](DESIGN/v0-2-0.md):

1. Preflight with the daemons still running: git and `uv` present, `origin`
   reachable, the target's schema version read out of the tag with
   `git show` (never imported), no root newer than it, no run in flight
   (`--wait SEC` drains first). If the schema changes it asks `y/N`,
   default no; `--yes` answers it. `--dry-run` stops here.
2. Hand off to a copy of the orchestrator in a temporary directory: from
   here on nothing is imported from the checkout being replaced.
3. Mark every root's `.tfs/lock`, stop the daemons, snapshot every database
   (`VACUUM INTO .tfs/backups/<utc>-<from-tag>.db`).
4. `git checkout <tag>`, `uv sync`, run the test suite (`--skip-tests` skips
   the suite and nothing else), start the daemons, and check that `/health`
   reports the target's commit — the hash, not the version string, decides.
5. Record the upgrade in every root's `upgrades` table, keep the newest 3
   snapshots per root, and offer to delete this upgrade's snapshots (they
   are kept unless you say `y`).

A failure before the daemons restart reverts the code; a failure at start or
health check reverts the code **and** restores every snapshot. If the revert
itself fails the command stops, exits 2 and prints the exact manual steps
with the tag and snapshot paths. There is deliberately no `tfs downgrade`:
restoring a snapshot by hand is the escape hatch. While an upgrade holds a
root, `list`/`query` refuse to open the database directly, `start` and
`stop` refuse, and a marker left by a crashed upgrade is taken over by the
next `tfs start` with a warning.

On Windows, `tfs upgrade` must be run through the `tfs` command (`uv run
tfs upgrade`), not `python -m tag_file_system.cli upgrade`: the launcher
routes `upgrade` to a standard-library-only path before pydantic is loaded,
because `uv` cannot replace a file a running process has mapped. For the
same reason the sync leaves the project's own metadata untouched
(`--no-install-project --inexact`); the next plain `uv sync` or `uv run`
refreshes it.

## Development

```bash
uv run pytest -q                # tests (each test gets its own temporary root)
uv run ty check src tests       # type check
uv run ruff format src tests    # format (CI runs it with --check)
```

All three run in CI on every push and pull request
([`.github/workflows/test.yml`](.github/workflows/test.yml)), on Linux and
Windows against Python 3.12 and 3.13, followed by a smoke test that inits a
root, starts the daemon, queries it and stops it. The self-update sequence
is exercised on both platforms against a fake checkout and fake `uv` /
`pytest` / daemon stand-ins (`tests/test_upgrade.py`), including the revert
of code and database after a failed start. The suite points the root
registry at a temporary file through `TFS_REGISTRY`.

Linting/formatting is configured for [Trunk](https://trunk.io) (ruff, black,
isort, bandit, markdownlint, prettier) in `.trunk/trunk.yaml`. Every source
file starts with `# Code by AkinoAlice@TyrantRey`.

## Versioning

`version` in [`pyproject.toml`](pyproject.toml) is `A.B.C`:

| Part | Bump it for | Example |
| --- | --- | --- |
| **A** — major | A major update — a change that breaks an existing root, the name grammar or the CLI | `1.4.2` → `2.0.0` |
| **B** — feature | A feature added, backwards compatible | `1.4.2` → `1.5.0` |
| **C** — change | A changes update: a fix, a refactor, a typo | `1.4.2` → `1.4.3` |

A typo is a small change, so it is a C: `+0.0.1`. Bumping A resets B and C to
`0`; bumping B resets C. Bump the version in the same commit as the change it
describes.

Design documents carry the same numbers: `DESIGN/v{A}-{B}-{C}.md` is the
approved design for that release — [`v0-1-0.md`](DESIGN/v0-1-0.md) is what
0.1.0 shipped, [`v0-2-0.md`](DESIGN/v0-2-0.md) is self-update, shipped in
0.2.0, [`v0-3-0.md`](DESIGN/v0-3-0.md) is the `on_start` / `on_stop` hooks,
shipped in 0.3.0. Releases are annotated tags `vA.B.C` on `origin`; that is
what `tfs update` looks for.

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
discovering the root from the current directory.

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
```

- Hooks: `added`, `modified`, `removed(on_move=...)`, `tagged("x")`; problem
  handlers `crit`, `err`, `warn`, `info` receive their level and above.
- Arguments after `(path, metadata, ctx)` come from the name, coerced by
  their annotations; one handler per hook per add-on.
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
  .tfs/db/system.db    files, tags, runs, traces, provenance, problems
  .tfs/token           bearer token of the control channel
  script/              add-ons
  ...                  your files
```

The daemon exposes a small HTTP API on `[daemon] bind:port` (default
`127.0.0.1:7411`), authenticated with the token: `/health`, `/stop`,
`/reload`, `/actions`, `/files`. In Docker, set `bind = "0.0.0.0"` and map
the port; mount the root as a volume.

## Development

```bash
uv run pytest -q                # tests (each test gets its own temporary root)
uv run ty check src tests       # type check
uv run ruff format src tests    # format (CI runs it with --check)
```

All three run in CI on every push and pull request
([`.github/workflows/test.yml`](.github/workflows/test.yml)), on Linux and
Windows against Python 3.12 and 3.13, followed by a smoke test that inits a
root, starts the daemon, queries it and stops it.

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
0.1.0 shipped, [`v0-2-0.md`](DESIGN/v0-2-0.md) is self-update, approved and not
yet implemented.

# TagFileSystem

[![test](https://github.com/TyrantRey/TagFileSystem/actions/workflows/test.yml/badge.svg)](https://github.com/TyrantRey/TagFileSystem/actions/workflows/test.yml)

Tags live in your file and folder names; which add-ons run on a folder lives
in a small YAML file inside it. A daemon watches a root, records every file
in SQLite, and runs your own Python add-ons on the files the folders ask for:

```
photos/
  .tfsfunctions.yaml               # make_copy.run: {suffix: .jpg, dst: backup}
  2024--trip/                      # every file below carries the tag "trip"
    beach--favorite.jpg            # tags: trip, favorite — and make_copy runs on it
```

Everything that happens — tags, runs, what a run produced, what went wrong —
is queryable, by you or by a tool. [`DESIGN/`](DESIGN) holds the approved
designs, one per release; this README is the short version.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 20+ and `npm` — only to build the web UI (`Frontend/`); the daemon
  and the CLI need neither
- or just Docker: the image carries all of the above (see [Docker](#docker))

## Install

```bash
git clone <repository-url>
cd TagFileSystem
uv sync                      # installs the package and the `tfs` command
```

Or skip the toolchain and run the daemon in a container: see
[Docker](#docker).

## Quick start

```bash
uv run tfs init ~/photos     # creates ~/photos/.tfs/ and ~/photos/script/
cd ~/photos
uv run tfs start             # reconciles the tree, then watches it (Ctrl+C to stop)
```

In another shell:

```bash
uv run tfs query -t trip                 # files carrying the tag
uv run tfs query --under 2024--trip --runs --json
uv run tfs explain 2024--trip/beach--favorite.jpg   # what applies to one file, and why
uv run tfs list                          # loaded add-ons and their handlers
uv run tfs plan                          # what a reload would start, and why — look before you reload
uv run tfs reload                        # after editing config.toml or a .tfsfunctions.yaml
uv run tfs status                        # the daemon: queue, runs in flight, limits, drift
uv run tfs ui --open                     # the web dashboard (see Web UI: build it once first)
uv run tfs stop
```

`tfs start -d` runs the daemon in the background. Its log goes to
`[logging] file` in `config.toml` (`.tfs/tag_file_system.log` by default),
foreground or not; `.tfs/daemon.out` holds only what a detached daemon
printed — where to look if it died. Every command accepts `--root <dir>`
instead of discovering the root from the current directory. `tfs --version`
prints the version and the commit it runs from (`0.5.0 (abc1234)`).

## How to use

The daemon does nothing you did not write into a name or a
`.tfsfunctions.yaml`. A typical setup:

1. **Make a folder a root.** `tfs init ~/photos` creates `~/photos/.tfs/`
   (config, database, token), `~/photos/script/` and a commented
   `~/photos/.tfsfunctions.yaml` skeleton. Existing files are left alone;
   they are indexed when the daemon first starts. A root cannot sit inside
   another root.
2. **Put your add-ons in `script/`.** One file per script, named after it:
   `script/photo.py` is what `photo:` in a `.tfsfunctions.yaml` refers to.
   Start from [`examples/script/`](examples/script). A script marks its
   handlers with `@action.added()`, `@action.removed()` and friends; each
   handler is addressed by its function name (see [Add-ons](#add-ons)).
3. **Tag files and folders.** `--tag` in a name gives a file a tag,
   inherited from every parent directory (see [Names](#names)). Names carry
   nothing else.
4. **Switch handlers on per folder.** A `.tfsfunctions.yaml` in a folder
   names the handlers that apply to it and everything below, with their
   parameters by name, and what to exclude (see
   [Functions file](#functions-file)). Path-valued parameters are never
   literal paths: `dst: backup` names the `[remotes]` entry `backup` in
   `.tfs/config.toml`.
5. **Edit `.tfs/config.toml`** for the control channel address (`[daemon]
   bind`/`port`), how long `stop` waits for running add-ons
   (`stop_timeout_seconds`), when a run is reported as overdue
   (`run_warn_after_seconds`), logging, and `[remotes]`.
6. **Start the daemon.** `tfs start` reads every `.tfsfunctions.yaml`,
   reconciles the tree (indexes every file, runs the handlers the folders
   ask for) and then watches it; use it in the foreground under Docker or a
   service manager, `tfs start -d` at a shell. A daemon holds `.tfs/lock`:
   one per root, on one machine.
7. **Ask questions.** `tfs query -t trip` lists files by tag, `--runs`
   adds what ran on each, `--json` is for scripts; `tfs explain <file>`
   says which handlers apply to one file, from which folder's file, and
   what excluded the rest; `tfs list` shows the loaded add-ons, their
   handlers and anything that failed to load. All talk to the daemon; with
   no daemon running they read `script/`, the `.tfsfunctions.yaml` files
   and the database directly (never over a network mount). `tfs ui` opens
   the same answers in a browser (see [Web UI](#web-ui)).
8. **Change things while it runs.** Add-ons are re-imported the moment
   their file changes. A `.tfsfunctions.yaml` is **not**: an edit is
   reported as drift and applied by `tfs reload`, which also re-reads
   `config.toml`. Changed parameters re-run the handler on the files they
   cover; an edited script does not re-run old files, because a run is
   keyed by file content, script, handler, hook and parameters.
9. **Stop.** `tfs stop` asks the daemon to finish in-flight runs and exit;
   a run still going after `stop_timeout_seconds` is marked `interrupted`
   and reported as a problem.
10. **Keep it current.** `tfs update` says whether a newer release tag
    exists; `tfs upgrade` applies it, snapshotting every database first
    (see [Self-update](#self-update)). A root from 0.3.x keeps its tags and
    history; `tfs migrate` turns its `@@func__arg` folder names into
    `.tfsfunctions.yaml` files.

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
| `tfs init [DIR]` | | Turn `DIR` (default: the current directory) into a root: `.tfs/`, `script/`, an empty database, a `.tfsfunctions.yaml` skeleton. Refuses inside or above an existing root. |
| `tfs list` | `--json` | The loaded add-ons, one line per handler with its hooks and typed parameters, and the load problems of `script/` and of every `.tfsfunctions.yaml`. From the daemon, or read directly when none runs. |
| `tfs query` | `-t/--tag TAG` (repeatable, ANDed), `--name TEXT`, `--format .EXT`, `--mime TYPE` or `family/*`, `--under DIR`, `--deleted`, `--runs`, `--json` | Files matching every criterion. `--under` is a root-relative directory such as `2024--trip`; `--runs` adds each file's run history. |
| `tfs explain PATH` | `--json` | The handlers that apply to one file (root-relative or absolute), each with the folder whose `.tfsfunctions.yaml` contributed it; the ones an `exclude` suppressed and why; the `tagged` defaults and who took them over. |
| `tfs migrate` | `--apply`, `--rename` | Turn a 0.3.x root's `@@func__arg` folder names into `.tfsfunctions.yaml` files, typed from the handlers' signatures. A dry run unless `--apply`; `--rename` (its own dry run) is the separate second step that strips the markers from the folder names. |
| `tfs ui` | `--open` | Print the web UI's address with the token in the URL fragment (`http://<bind>:<port>/ui/#token=…`); `--open` opens it in the browser. Needs the daemon; warns when `Frontend/dist` is not built. |
| `tfs reload` | `-y/--yes` | Re-read `config.toml` and every `.tfsfunctions.yaml`, re-import every add-on and reconcile, in the running daemon. Refused, with the count, when it would start more runs than `[daemon] confirm_above` unless `--yes`. `[daemon] bind`/`port` take effect at the next `start`. |
| `tfs plan [PATH]` | `--all`, `--json` | What `tfs reload` would start, and why: per file, the handlers that would run (`new entry`, `arguments changed from …`, `newly included`, `never ran`), the ones that already ran, the entries a file leaves; how every `.tfsfunctions.yaml` and script differs from what the daemon loaded. `PATH` narrows it to one file or a directory. Offline: what the next `tfs start` runs. |
| `tfs status` | `--json` | The daemon in one screen: running/paused, uptime, the queue, runs in flight and failed, files, undelivered problems, drift, the limits in force. Offline: the lock and the database's counts. |
| `tfs doctor` | `--json` | Checks the root, the configuration, the token, the database, the lock, the daemon and its version, the scripts, the functions files, drift, failed runs, free space: `ok`/`warn`/`fail` per line, exit 1 on a `fail`. |
| `tfs pause` / `tfs resume` | | Stop starting runs / start them again. Watching and indexing go on; the work waits in the queue. |
| `tfs retry RUN_ID` | | A fresh run for a failed or interrupted run, under its own key (the reasons `ctx.retry` refuses apply). |
| `tfs rerun` | `--handler S.H`, `[PATH]`, `--failed`, `--stale`, `--dry-run`, `-y/--yes` | Run handler `H` of script `S` again on the files in its scope, finished runs included; `--failed` only where the last run failed, `--stale` only where it ran with an older version of the script; `--dry-run` counts. Bounded by `confirm_above` like `reload`. |
| `tfs cancel RUN_ID` | | End a run in flight: its record is final at once; the handler's next `ctx` call raises `action.Cancelled`. |
| `tfs touch PATH` | `--content TEXT` | Create a data file (or update its mtime) inside the root; the daemon indexes it at once and says which handlers will run. |
| `tfs cp SRC DST` / `tfs mv SRC DST` | | Copy / move a data file; `DST` may be a directory. A move is recorded as one: `removed(on_move=True)` for the entries the file leaves, `added` for those it enters. |
| `tfs rm PATH` | `-r/--recursive` | Delete a data file (or a directory with `-r`): the rows are retired and `removed` fires. Here `-r` is recursive; name the root as `--root DIR`, or with the global `-r` before the command. |
| `tfs mkdir PATH` | | Create a directory; prints the tags its name gives to files placed there. |
| `tfs tag PATH TAG...` / `tfs untag PATH TAG...` | | Add tags to a file without renaming it (a gained tag runs its `tagged` handlers), or remove tags added this way; tags the name spells stay. Needs the daemon. |
| `tfs start` | `-d/--detach`, `--force`, `--log-console` | Reconcile, then watch, logging to `[logging] file`. `-d` detaches (the child's own output, i.e. a crash, lands in `.tfs/daemon.out`) and returns once the control channel answers. `--force` takes over a lock left by a daemon that is gone or on another host, never one held by a live local process. `--log-console` (or `TFS_LOG_CONSOLE=1`) writes the log to the console as well, for Docker or a service manager; foreground only. |
| `tfs stop` | `--timeout SEC` | Stop gracefully; falls back to signalling the pid in `.tfs/lock` when the daemon does not answer, only for a lock written on this host. Default wait: `stop_timeout_seconds + 5`. |
| `tfs update` | `--json` | Fetch the release tags from `origin` and report the current and newest version, the schema change and every registered root. Changes nothing. |
| `tfs upgrade` | `--to TAG`, `--dry-run`, `-y/--yes`, `--skip-tests`, `--wait SEC` | Move the checkout to the newest release tag (or `--to`), snapshot every root, run the suite, restart the daemons, revert on failure. `--yes` consents to a schema change; `--wait` is how long to let in-flight runs finish first. |
| `tfs backup list` | `--json` | The snapshots in `.tfs/backups/`, newest first, with size and origin tag. |
| `tfs backup prune` | `--keep N` (default 3), `--dry-run`, `-y/--yes` | Delete all but the newest `N` snapshots; asks first unless `--yes`. |
| `tfs --version` | | `0.5.0 (abc1234)`: the version in `pyproject.toml` and the commit of the checkout. |

Exit status is `0` on success and `1` on any error (no root, no daemon
where one is needed, a refused lock, a failed check). `tfs query` exits
`2` for a bad `--under` or a blank `--tag`, `tfs explain` for a path that
is not a file in the root; `tfs upgrade` exits `2` when an upgrade failed
*and* could not be reverted, after printing the manual steps. `tfs update`
exits `0` whether or not an update is available; only a failed check is
non-zero. `tfs migrate --apply` exits `1` when there is nothing to write.
`tfs doctor` exits `1` when a check fails; `tfs reload` and `tfs rerun` exit
`1` when the threshold refuses them; `tfs plan` and the file commands exit
`2` for a path that is not a data path in the root (`.tfs/`, `script/`, a
`.tfsfunctions.yaml`, or outside).

## Names

Every directory segment and the filename stem follow the same grammar:

| Marker              | Meaning                                                |
| ------------------- | ------------------------------------------------------ |
| `--tag`             | the file carries `tag` (lowercased, `[\w-]` only)      |

Tags are inherited from every parent directory, parent first. `:`, `/`,
`\`, `<`, `>`, `|`, `?`, `*`, `"` cannot appear in a marker (the names must
work on Windows and on the NAS). `@@`, which asked for a function in 0.3.x,
is reported as a problem and otherwise ignored — see `tfs migrate`.

## Functions file

Any folder may hold a `.tfsfunctions.yaml`; it applies to that folder and
everything below it, and the files of nested folders add to it, parent
first:

```yaml
version: 1
functions:
  photo:                      # script/photo.py
    resize:                   # def resize(path, metadata, ctx, width: int, ...)
      width: 800              # parameters by name, in YAML's own types
      exclude:                # skip the file when any of these holds
        - tag: draft          # it carries the tag
        - filename: "*.tmp"   # its name matches a glob (case-insensitive)
    make_copy:
      suffix: .jpg
      dst: backup             # a Remote: an entry of [remotes] in config.toml
```

`exclude` is a predicate tree: a list means *any*, and `any`, `all` and
`not` nest — `exclude: {all: [{tag: big}, {not: {filename: "*.jpg"}}]}`
skips big files that are not JPEGs. A leaf takes one value or a list of
them (`tag: [draft, wip]`). `tfs explain` prints the branch that held.

- A file handler runs **only** where an entry names it; `thumbnail: {}`
  switches on a handler that needs no parameters. `@action.tagged("x")`
  handlers are the exception: they are global defaults, and a folder that
  names one takes it over for its subtree. `on_start`/`on_stop` and the
  problem handlers need no entry.
- The same handler with different parameters in a parent and a child folder
  runs twice, parent first; with identical parameters it runs once.
- `exclude` belongs to one entry and narrows only it: a deeper folder that
  names the handler again runs it inside the excluded subtree. `tag` tests
  the file's full tag set (inherited, and applied by `ctx.tag`).
- The files are read at `tfs start` and `tfs reload`, never live: an edit
  under a running daemon is reported as `functions.drift` until you reload.
  `tfs plan` shows what the reload would start before you do, and a reload
  that would start more runs than `[daemon] confirm_above` refuses until you
  say `--yes`. A file that does not parse keeps its last good version in
  force; an entry that names a missing script or handler, or a parameter
  that does not fit, is skipped and reported (`tfs list` shows it).
- `tfs explain <file>` prints the merged result for one file.
- Path-valued parameters are never literal paths: `dst: action.TagDir` names
  the directory that carries that tag, `dst: action.Remote` names an entry
  of `[remotes]` in `config.toml`.

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
  `on_start` / `on_stop` bracket the daemon session. `removed(on_move=True)`
  also fires when a file leaves the handler's scope — moved out of the
  folder, or newly excluded.
- Parameters after `(path, metadata, ctx)` come from the folder's
  `.tfsfunctions.yaml`, by name, coerced by their annotations. Handlers are
  addressed by function name, so one script may carry several on the same
  hook; `exclude` (and `tag`, on a `tagged` handler) cannot be parameter
  names. Problem handlers take `(problem, ctx)`, lifecycle handlers `(ctx)`
  — they have no file, so `ctx.file` and `ctx.path` are `None`.
- `on_start` runs once per daemon session per add-on: at `tfs start`, and as
  soon as a script that appears later is loaded. `on_stop` runs at shutdown,
  before in-flight runs are waited for — the place to signal a service thread
  an `on_start` spawned (`ctx.spawn` … `ctx.done()`) that it should leave.
- `ctx` offers `copy/move/write/delete/emit`, `record/log`, `spawn/done` for
  background work, `tag/untag`, `query`, `problem`, `retry`, `resolve`, and
  `check`/`cancelled` for a run that `tfs cancel` or `run_timeout_seconds`
  ended: the next `ctx` call with a side effect raises `action.Cancelled`, a
  long loop asks with `ctx.check()`.
- A handler that edits the file it was called on is recorded as that file's
  producer, so the change it made does not trigger it again (an in-place
  resize cannot loop). A handler that fails is a `failed` run: `tfs retry`
  starts a fresh one under the same key; `tfs rerun --handler` runs a
  handler again on files it already handled.
- Scripts are hot-reloaded when they change; helpers are `_name.py`.
- [`examples/script/`](examples/script) holds `make_copy.py` (copy to a
  remote, drop the copy when the source leaves) and `notify.py` (a problem
  handler): copy them into `script/` to try the daemon.
- A run happens once per `(file content, script, handler, hook,
  parameters)`: editing a script does not re-run old files, changing an
  entry's parameters (and reloading) does.

## Web UI

A read-only dashboard the daemon serves at `/ui/`
([`DESIGN/v0-5-0.md`](DESIGN/v0-5-0.md)). Build it once per checkout —
`tfs upgrade` rebuilds it for you whenever `npm` is on PATH:

```bash
cd Frontend && npm ci && npm run build && cd ..   # Node 20+; produces Frontend/dist
uv run tfs ui --open                              # http://127.0.0.1:7411/ui/#token=… — and opens it
```

The token rides in the URL fragment, which a browser never sends: the page
keeps it for the tab and sends it as a bearer on every request, so the
daemon's log never sees it. Without a build the daemon answers `503 UI not
built` at `/ui/` and `tfs ui` warns; a build that lands while the daemon
runs is served at once, no restart. A daemon bound to `0.0.0.0` exposes the
static shell of the UI without a token, and nothing else.

What it shows: **Status** (version, add-ons, the queue and whether it is
paused, runs in flight and failed, drift, the limits, upgrades),
**Files** (by tag, name and folder; paged), one **File** (its tags, what
applies to it and why, its history), **Runs** and one **Run** (arguments,
result, trace, what it produced, its problems), **Problems**, **Add-ons**
and **Functions** (every loaded `.tfsfunctions.yaml`). Three things on it
write ([`DESIGN/v0-5-0.md` §12](DESIGN/v0-5-0.md)): **Upload** on the
Files page puts files into the folder being browsed (the daemon indexes
each at once and says which handlers run on it), **Download** on a File
page fetches the bytes with the token and saves them, and the tags on a
File page are editable chips: add one, remove one the name does not spell.
Reload, retry, cancel and stop stay with the CLI.

Everything it shows comes from the versioned JSON API the daemon serves at
`/api/v1/...` with the same bearer token — `status`, `files`, `file`,
`file/history`, `file/explain`, `tags`, `runs`, `run`, `problems`,
`problem`, `addons`, `functions`, `upgrades`, `plan`, `queue`, `doctor`;
lists are paged (`limit`, `offset`, `total`), errors are `{"error": ...}`.
The operator's commands are `POST` endpoints of the same API (`pause`,
`resume`, `run/retry`, `run/cancel`, `rerun`, `files/touch|copy|move|remove|mkdir`);
the UI never calls them. The UI's own writes are `POST files/upload?path=`
(the body is the file), `GET file/content?path=` (the bytes back) and
`POST file/tags?path=` with `{"add": [...], "remove": [...]}`. A script
needs no more than:

```bash
curl -H "Authorization: Bearer $(cat .tfs/token)" http://127.0.0.1:7411/api/v1/status
```

For work on the UI itself, `npm run dev` in `Frontend/` serves it with
`/api` proxied to the daemon (`TFS_DAEMON` overrides the address); open the
address `tfs ui` prints with the dev server's origin in place of the
daemon's.

## Operations

The daemon runs what the tree says the moment it says it. Before a reload,
look ([`DESIGN/v0-5-0.md` §11](DESIGN/v0-5-0.md)):

```bash
uv run tfs plan                    # per file: what would run and why; what each .tfsfunctions.yaml and script changed
uv run tfs plan photos/            # one folder, or one file
uv run tfs reload                  # refused above confirm_above: `tfs reload --yes` consents
uv run tfs status                  # queue, runs in flight, failed runs, limits, drift
uv run tfs doctor                  # root, config, token, database, lock, daemon version, scripts, functions
uv run tfs pause; uv run tfs resume        # hold the work; nothing in flight is touched
uv run tfs retry RUN_ID                    # a failed or interrupted run, again
uv run tfs rerun --handler photo.resize --stale   # files whose run used an older photo.py
uv run tfs cancel RUN_ID                   # end a run in flight
uv run tfs touch 2024--trip/note.txt; uv run tfs mv note.txt 2024--trip/; uv run tfs rm old.txt
```

Runs go through a queue the daemon owns. `[daemon]` in `config.toml` sets
its limits — every one of them off at `0`:

```toml
[daemon]
max_concurrent_runs = 1      # worker threads; 0 = run on the watch loop
max_runs_per_minute = 0      # a token bucket over every run start
run_timeout_seconds = 0      # a file run longer than this is failed (run.timeout)
confirm_above = 500          # `tfs reload`/`rerun` refuse more runs than this without --yes
```

A run's key is `(file content, script, handler, hook, parameters)`: a run
happens once per key, `tfs retry` and `tfs rerun` are the two doors past
that, both recorded as a retry of the run they replace. A cancel or a
timeout ends the run's record at once; the handler is asked to stop
(`ctx.check()`, `action.Cancelled` on its next side effect) and its worker
is abandoned until it returns. Pausing is a session state, and so is the
queue: a restart reconciles and offers every file again, which the key
makes a no-op for finished work.

## Root layout

```
<root>/
  .tfs/config.toml     [logging], [daemon] bind/port and the work-control limits, [remotes]
  .tfs/db/system.db    files, tags, runs, traces, provenance, problems, upgrades
  .tfs/backups/        database snapshots taken by `tfs upgrade`
  .tfs/token           bearer token of the control channel
  .tfs/tag_file_system.log   the daemon's log ([logging] file)
  .tfs/daemon.out      what a detached daemon printed — its crash output, if any
  .tfsfunctions.yaml   the handlers that apply to the whole root (any folder may have one)
  script/              add-ons
  ...                  your files
```

The daemon exposes an HTTP API on `[daemon] bind:port` (default
`127.0.0.1:7411`), authenticated with the token: the CLI's own endpoints
(`/health`, which reports the version and commit the daemon runs, `/stop`,
`/reload`, `/actions`, `/files`, `/explain`) and the versioned, read-only
`/api/v1/...` the web UI and scripts use (see [Web UI](#web-ui)); `/ui/`
serves the built dashboard without a token — static files, nothing else.
In a container the root is a volume and `bind` is `0.0.0.0`: see
[Docker](#docker).

## Docker

The daemon as one container, the root as a volume
([`DESIGN/v0-5-0.md` §10](DESIGN/v0-5-0.md)). The image carries the built web
UI, so the host needs nothing but Docker:

```bash
mkdir -p data                       # the root: must exist, writable by PUID:PGID
docker compose up -d --build        # build the image, start the daemon on ./data
docker compose exec tfs tfs list    # every tfs command runs inside the container
docker compose exec tfs tfs ui      # http://127.0.0.1:7411/ui/#token=… — open it on the host
docker compose logs -f tfs          # the log (also in data/.tfs/tag_file_system.log)
docker compose down                 # SIGTERM: in-flight runs get stop_timeout_seconds
```

The first start turns `data/` into a root (`tfs init`) with
`bind = "0.0.0.0"`, the one address a published port reaches; from then on
`data/.tfs/config.toml`, `data/script/` and the `.tfsfunctions.yaml` files
are yours to edit as on any root (`docker compose exec tfs tfs reload` after
a change). Copy [`.env.example`](.env.example) to `.env` to change the root
(`TFS_ROOT`), the host port (`TFS_PORT`), the uid:gid the daemon runs as
(`PUID`/`PGID`: the owner of every file it creates), or to poll instead of
relying on inotify (`WATCHFILES_FORCE_POLLING=true`, for a root on NFS or
SMB; Docker Desktop on Windows and macOS is detected on its own). A
`[remotes]` target is a container path: mount it in `docker-compose.yml`
and name that path in `config.toml`. Packages your add-ons import go in
[`docker/requirements-addons.txt`](docker/requirements-addons.txt), then
`docker compose build`. `GIT_COMMIT=$(git rev-parse HEAD) docker compose
build` stamps the image so `tfs --version`, `/health` and every run record
the commit; without it the hash is `unknown`.

Stopping is `docker compose stop` or `down`: SIGTERM, a clean shutdown, the
lock released; keep `stop_grace_period` (45 s) above `stop_timeout_seconds`.
After a SIGKILL (a crash, `docker kill`, a power cut) the lock stays behind
and the restarted container removes it on its own, since nothing in a fresh
container can hold it. A lock from another host, or from a container that
was replaced rather than restarted, is refused the way a stale lock always
is (`root is held by pid N on HOST`): delete `data/.tfs/lock` once you are
sure that daemon is gone. One daemon per root: do not `tfs start` on the
host for a root a container manages. Upgrading is `git pull && docker
compose up -d --build`; `tfs upgrade` is for a checkout and does nothing
useful inside a container.

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
4. `git checkout <tag>`, `uv sync`, build the web UI (`npm ci && npm run
   build` in `Frontend/` — a warning in the report, never a failure, when
   `npm` is missing or the build fails: the daemon works without it), run
   the test suite (`--skip-tests` skips the suite and nothing else), start
   the daemons, and check that `/health` reports the target's commit — the
   hash, not the version string, decides.
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

cd Frontend && npm ci           # the web UI (Node 20+), once per checkout
npm run lint && npm test        # tsc + eslint; vitest
npm run build                   # Frontend/dist: what the daemon serves at /ui/
npm run dev                     # dev server, /api proxied to the daemon (TFS_DAEMON overrides)
```

The Python checks run in CI on every push and pull request
([`.github/workflows/test.yml`](.github/workflows/test.yml)), on Linux and
Windows against Python 3.12 and 3.13, followed by a smoke test that inits a
root, starts the daemon, queries it, checks the API and `/ui/` before
(`503`) and after (`200`) a build, and stops it; a `frontend` job lints,
format-checks, tests and builds `Frontend/` on Node 22; a `docker` job
builds the image, starts it with `docker compose` on a fresh root, checks
the API and the UI through the published port, and verifies a clean stop
releases the lock and a SIGKILL'd container comes back. The self-update
sequence is exercised on both platforms against a fake checkout and fake
`uv` / `pytest` / `npm` / daemon stand-ins (`tests/test_upgrade.py`),
including the revert of code and database after a failed start. The suite
points the root registry at a temporary file through `TFS_REGISTRY`.

Linting/formatting is configured for [Trunk](https://trunk.io) (ruff, black,
isort, bandit, markdownlint, prettier) in `.trunk/trunk.yaml`. Every source
file starts with `# Code by AkinoAlice@TyrantRey`.

## Versioning

`version` in [`pyproject.toml`](pyproject.toml) is `A.B.C`:

| Part | Bump it for | Example |
| --- | --- | --- |
| **A** — major | A major update — an existing root stops loading or loses data, or the CLI breaks | `1.4.2` → `2.0.0` |
| **B** — feature | New behaviour, backwards compatible — including a grammar change an existing root survives with a warning | `1.4.2` → `1.5.0` |
| **C** — change | A changes update: a fix, a refactor, a typo | `1.4.2` → `1.4.3` |

A typo is a small change, so it is a C: `+0.0.1`. Bumping A resets B and C to
`0`; bumping B resets C. Bump the version in the same commit as the change it
describes.

Design documents carry the same numbers: `DESIGN/v{A}-{B}-{C}.md` is the
approved design for that release — [`v0-1-0.md`](DESIGN/v0-1-0.md) is what
0.1.0 shipped, [`v0-2-0.md`](DESIGN/v0-2-0.md) is self-update, shipped in
0.2.0, [`v0-3-0.md`](DESIGN/v0-3-0.md) is the `on_start` / `on_stop` hooks,
shipped in 0.3.0, [`v0-4-0.md`](DESIGN/v0-4-0.md) moves functions out of
names into `.tfsfunctions.yaml`, shipped in 0.4.0,
[`v0-5-0.md`](DESIGN/v0-5-0.md) is the versioned API and the read-only web
UI, in its §10 the Docker image and in its §11 the operator's controls
(`tfs plan`, the work queue, `status`, `doctor`, the file commands), shipped
in 0.5.0. Releases are annotated tags `vA.B.C` on `origin`; that is what
`tfs update` looks for.

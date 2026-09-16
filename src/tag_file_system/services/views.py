# Code by AkinoAlice@TyrantRey

"""The views the CLI shows, built one way whether a daemon answers or not.

A running daemon is the truth about what applies *now*: it answers from the
add-ons and ``.tfsfunctions.yaml`` files it loaded at start or at the last
reload. With no daemon the CLI builds the very same views from ``script/``,
the files and the database as they are on disk — which is what the next
start would load — and labels them ``source: disk`` so nobody mistakes the
one for the other. Everything the two paths could disagree on lives here,
so they cannot drift apart:

- which add-ons and handlers there are (``Addon.describe()``),
- which load problems to show (the scripts' and the configuration files'),
- what applies to one file (``explain_payload``) and where its tags come
  from (the database row when there is one, else its name).

``tfs query`` needs nothing here: both of its paths run the same
``SQLiteBackend.query_files`` and ``file_payload``.
"""

from pathlib import PurePosixPath
from typing import Any

from tag_file_system.addons.loader import AddonLoader, ProblemReporter
from tag_file_system.core.interface.file_metadata import TaggedFile
from tag_file_system.functions import FunctionsStore, explain_payload
from tag_file_system.root import Root
from tag_file_system.services.tagging import TaggingParser

DAEMON = "daemon"  # the daemon's loaded state: what runs
DISK = "disk"  # script/ and the files as they are: what the next start would load


def load_root(
    root: Root, report: ProblemReporter | None = None
) -> tuple[AddonLoader, FunctionsStore]:
    """No daemon: load what one would. ``report`` receives the scripts'
    load problems; the configuration's are read from ``FunctionsStore.problems``
    (giving the store the same reporter would list each of them twice)."""
    loader = AddonLoader(root, report=report)
    loader.load_all()
    functions = FunctionsStore(root, loader)
    functions.load_tree()
    return loader, functions


def actions_view(
    loader: AddonLoader,
    functions: FunctionsStore,
    script_problems: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    """``tfs list`` / ``GET /actions``: every add-on per handler, and every
    load problem — ``{severity, kind, message}`` for a script, the same plus
    ``file`` for a ``.tfsfunctions.yaml``."""
    return {
        "source": source,
        "actions": [addon.describe() for _, addon in sorted(loader.addons.items())],
        "problems": [*script_problems, *functions.problems],
    }


def explain_view(
    loader: AddonLoader,
    functions: FunctionsStore,
    key: str,
    row: TaggedFile | None,
    source: str,
    parser: TaggingParser | None = None,
) -> dict[str, Any]:
    """``tfs explain`` / ``GET /explain``: the tags are the row's when the
    file is indexed (``known``), else what its name says."""
    if row is not None:
        tags = [t.name for t in row.tags]
    else:
        tags = (parser or TaggingParser()).parse_path(PurePosixPath(key)).tag_names
    payload = explain_payload(functions, loader, key, tags, row is not None)
    payload["source"] = source
    return payload

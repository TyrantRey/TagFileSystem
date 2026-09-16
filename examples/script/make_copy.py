# Code by AkinoAlice@TyrantRey

"""Demo add-on: copy every file in a folder that switches it on.

Put this file in ``<root>/script/``, add a remote to ``.tfs/config.toml``:

    [remotes]
    backup = "/home/photo/backup"      # or "D:\\backup" on Windows

then give a folder a ``.tfsfunctions.yaml`` (DESIGN/v0-4-0.md §3):

    version: 1
    functions:
      make_copy:
        run:  {suffix: .jpg, dst: backup}
        gone: {suffix: .jpg, dst: backup}

and drop files into it (``tfs reload`` applies a file edited while the
daemon runs; ``tfs explain <file>`` shows what applies to it). Edit this
script while the daemon runs: it is reloaded on save (``tfs list`` shows the
new signature), and the next file uses the new code.
"""

from pathlib import Path

from tag_file_system import action


@action.added()
def run(
    path: Path, metadata, ctx, suffix: str = ".jpg", dst: action.Remote = Path("./")
):
    """Copy ``path`` to the remote if its extension matches ``suffix``."""
    if path.suffix.lower() != suffix.lower():
        ctx.log(f"skipping {path.name}: not a {suffix}")
        return "skipped"
    if dst is None:
        raise ValueError("make_copy needs a remote: dst: <remote> in .tfsfunctions.yaml")
    target = ctx.copy(
        path, dst / path.name
    )  # traced, and recorded as produced by this run
    ctx.record(bytes=metadata.file_size)
    return str(target)


@action.removed(on_move=True)
def gone(
    path: Path, metadata, ctx, suffix: str = ".jpg", dst: action.Remote = Path("./")
):
    """The source left the folder (or was deleted): drop the copy."""
    if dst is None:
        return
    copy = dst / path.name
    if copy.exists():
        copy.unlink()
        ctx.log(f"removed {copy}")

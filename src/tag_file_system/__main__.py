# Code by AkinoAlice@TyrantRey

"""The ``tfs`` entry point: ``tfs upgrade`` goes to the standard-library
orchestrator *before* anything else is imported; every other command goes
to the Typer app.

That routing is load-bearing on Windows: ``uv sync`` cannot replace a
compiled dependency (``_pydantic_core.pyd``) that a running process has
mapped, and this process is the one waiting for the upgrade to finish. See
``updater.py``.
"""

import sys

UPGRADE = "upgrade"
_TAKES_VALUE = {"--root", "-r"}


def subcommand(argv: list[str]) -> tuple[str | None, int]:
    """The first bare word of ``argv`` (skipping ``--root X`` / ``-r X``
    and ``--root=X``) and its index: what Typer would dispatch on."""
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg in _TAKES_VALUE:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg, index
    return None, -1


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    name, index = subcommand(args)
    if name == UPGRADE:
        from tag_file_system.updater import main as upgrade

        sys.exit(upgrade(args[:index] + args[index + 1 :]))
    from tag_file_system.cli import app

    app(args)


if __name__ == "__main__":  # pragma: no cover - `python -m tag_file_system`
    main()

# Code by AkinoAlice@TyrantRey

"""The public add-on API (DESIGN/v0-1-0.md §4.2):

    from tag_file_system import action

    @action.added()
    def run(path: Path, metadata: FileMetadata, ctx: ActionContext, suffix: str, dst: action.Remote): ...

    @action.removed(on_move=True)
    def gone(path, metadata, ctx): ...

    @action.tagged("photos")
    def on_photo(path, metadata, ctx): ...

    @action.on_start()
    def up(ctx: ActionContext): ...

    @action.on_stop()
    def down(ctx: ActionContext): ...

    @action.err()
    def notify(problem: ProblemRecord, ctx: ActionContext): ...

A decorator only *marks* the function (``HandlerSpec`` entries under
``__tfs_handlers__``); the loader discovers marked functions when it imports
``script/<name>.py``. One function may carry several marks. Every decorator
is called with parentheses: ``@action.added`` (bare) is a ``TypeError``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Literal, TypeVar

from tag_file_system.core.interface.action import Hook, Severity
from tag_file_system.core.interface.tag import Tag

HANDLER_ATTR = "__tfs_handlers__"

F = TypeVar("F", bound=Callable)


@dataclass(frozen=True)
class HandlerSpec:
    kind: Literal["file", "problem", "lifecycle"]
    hook: Hook | None = None  # file and lifecycle handlers
    tag: str | None = None  # `tagged` handlers: the normalized tag name
    on_move: bool = False  # `removed`: also fire when the file leaves the @@ dir
    severity: Severity | None = None  # problem handlers: level and above

    def describe(self) -> str:
        if self.kind == "problem":
            assert self.severity is not None
            return self.severity.value
        assert self.hook is not None
        if self.kind == "lifecycle":
            return self.hook.value
        if self.hook is Hook.TAGGED:
            return f"tagged:{self.tag}"
        if self.hook is Hook.REMOVED and self.on_move:
            return "removed+move"
        return self.hook.value


def handlers_of(func: object) -> list[HandlerSpec]:
    return list(getattr(func, HANDLER_ATTR, ()))


def _mark(spec: HandlerSpec) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        if not callable(func):
            raise TypeError(f"@action decorators mark functions, got {func!r}")
        existing = list(getattr(func, HANDLER_ATTR, ()))
        if spec in existing:
            return func  # the same mark twice is one mark
        # Decorators apply bottom-up; prepend so the list reads like the source.
        try:
            setattr(func, HANDLER_ATTR, [spec, *existing])
        except (AttributeError, TypeError) as e:
            raise TypeError(
                f"@action decorators need a plain function, cannot mark {func!r}: {e}"
            ) from e
        return func

    return decorator


def _no_bare_use(value: object, name: str) -> None:
    if callable(value):
        raise TypeError(f"use @action.{name}() with parentheses")


def _file(hook: Hook, **fields) -> Callable[[F], F]:
    return _mark(HandlerSpec(kind="file", hook=hook, **fields))


def added(_bare: object = None) -> Callable[[F], F]:
    """The file appears under the ``@@`` directory (or is reconciled at start)."""
    _no_bare_use(_bare, "added")
    return _file(Hook.ADDED)


def modified(_bare: object = None) -> Callable[[F], F]:
    """The file's content changed (new hash)."""
    _no_bare_use(_bare, "modified")
    return _file(Hook.MODIFIED)


def removed(on_move: bool = False) -> Callable[[F], F]:
    """The file was deleted. With ``on_move=True`` also when it merely left
    the ``@@`` directory."""
    _no_bare_use(on_move, "removed")
    if not isinstance(on_move, bool):
        raise TypeError(f"on_move must be True or False, got {on_move!r}")
    return _file(Hook.REMOVED, on_move=on_move)


def tagged(tag: str) -> Callable[[F], F]:
    """The file acquires ``tag`` — from a name, ``ctx.tag`` or the API."""
    _no_bare_use(tag, "tagged")
    if not isinstance(tag, str):
        raise TypeError(f"tagged() takes a tag name, got {tag!r}")
    name = Tag(name=tag).name  # normalized exactly like tags parsed from names
    return _file(Hook.TAGGED, tag=name)


def _lifecycle(hook: Hook, bare: object) -> Callable[[F], F]:
    _no_bare_use(bare, hook.value)
    return _mark(HandlerSpec(kind="lifecycle", hook=hook))


def on_start(_bare: object = None) -> Callable[[F], F]:
    """The daemon is up: the add-ons are loaded, no file has been looked at
    yet. Runs once per daemon session (DESIGN/v0-3-0.md §2); an add-on that
    appears later runs it as soon as it is loaded."""
    return _lifecycle(Hook.ON_START, _bare)


def on_stop(_bare: object = None) -> Callable[[F], F]:
    """The daemon is stopping, before in-flight runs are waited for."""
    return _lifecycle(Hook.ON_STOP, _bare)


def _problem(severity: Severity, bare: object) -> Callable[[F], F]:
    _no_bare_use(bare, severity.value)
    return _mark(HandlerSpec(kind="problem", severity=severity))


def crit(_bare: object = None) -> Callable[[F], F]:
    return _problem(Severity.CRIT, _bare)


def err(_bare: object = None) -> Callable[[F], F]:
    return _problem(Severity.ERR, _bare)


def warn(_bare: object = None) -> Callable[[F], F]:
    return _problem(Severity.WARN, _bare)


def info(_bare: object = None) -> Callable[[F], F]:
    return _problem(Severity.INFO, _bare)


# ---------------------------------------------------------- path-valued args


@dataclass(frozen=True)
class PathArg:
    """Annotation metadata: the argument names a location, not a literal path
    (DESIGN/v0-1-0.md §4.4). The binder resolves it before the handler runs."""

    kind: Literal["tagdir", "remote"]


TagDir = Annotated[Path, PathArg("tagdir")]
"""A directory inside the root, named by the tag its own segment carries."""

Remote = Annotated[Path, PathArg("remote")]
"""A location outside the root, named in ``[remotes]`` of ``config.toml``."""

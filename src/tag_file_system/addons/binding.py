# Code by AkinoAlice@TyrantRey

"""Binding of slug args to a handler's parameters (DESIGN.md §4.2).

A file handler is ``(path, metadata, ctx, *typed_args)``: the three fixed
parameters come first, everything after them is an action argument. The
slug's positional strings are matched to those parameters by position and
coerced through a pydantic ``TypeAdapter`` per annotation; ``TagDir`` and
``Remote`` parameters are resolved to real paths first.
"""

import copy
import inspect
import types
import warnings
from dataclasses import dataclass, field
from functools import reduce
from operator import or_
from pathlib import Path, PurePath
from typing import Annotated, Any, Callable, Literal, Union, get_args, get_origin

from pydantic import TypeAdapter, ValidationError

from tag_file_system.action import PathArg

FIXED_PARAMETERS = 3  # path, metadata, ctx
PROBLEM_PARAMETERS = 2  # problem, ctx

PathResolver = Callable[[str, str], Path]  # (kind, raw) -> resolved path

_POSITIONAL = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
)
_CACHE_ATTR = "__tfs_binding__"


def callable_name(func: Callable) -> str:
    """``__qualname__`` when there is one; any callable is accepted."""
    return (
        getattr(func, "__qualname__", None)
        or getattr(func, "__name__", None)
        or repr(func)
    )


class SignatureError(TypeError):
    """The handler's signature cannot be bound (reported at load time)."""


class BindingError(ValueError):
    """The slug's args do not fit the handler (a failed run)."""


@dataclass(frozen=True)
class Parameter:
    name: str
    annotation: Any  # what pydantic validates against
    path_kind: str | None  # "tagdir" / "remote" for path-valued args
    default: Any = inspect.Parameter.empty

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty


@dataclass
class BoundArgs:
    """``kwargs`` is what the handler is called with; ``raw`` is the run key's
    ``args`` — the slug strings keyed by parameter name, so the key does not
    depend on how a remote resolves today."""

    kwargs: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------- annotations


def _split_annotation(annotation: Any) -> tuple[Any, str | None]:
    """Strip the ``PathArg`` marker (also inside ``Optional``/``Union`` and
    nested ``Annotated``) and return ``(validation annotation, kind)``."""
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        kinds = {m.kind for m in metadata if isinstance(m, PathArg)}
        rest = [m for m in metadata if not isinstance(m, PathArg)]
        inner, inner_kind = _split_annotation(base)
        if inner_kind:
            kinds.add(inner_kind)
        if len(kinds) > 1:
            raise SignatureError("an argument cannot be both TagDir and Remote")
        rebuilt = Annotated[(inner, *rest)] if rest else inner
        return rebuilt, (kinds.pop() if kinds else None)
    if origin in (Union, types.UnionType):
        bases: list[Any] = []
        kinds = set()
        for arg in get_args(annotation):
            base, kind = _split_annotation(arg)
            bases.append(base)
            if kind:
                kinds.add(kind)
        if len(kinds) > 1:
            raise SignatureError("an argument cannot be both TagDir and Remote")
        return reduce(or_, bases), (kinds.pop() if kinds else None)
    return annotation, None


def _mentions_path(annotation: Any) -> bool:
    """A bare ``Path`` annotation would let a literal path into a slug."""
    if isinstance(annotation, type):
        return issubclass(annotation, PurePath)
    return any(_mentions_path(arg) for arg in get_args(annotation))


def _mentions_path_arg(annotation: Any) -> bool:
    """A ``TagDir``/``Remote`` buried somewhere ``_split_annotation`` does not
    look (``list[TagDir]``): the intent is clear, the shape unsupported."""
    if get_origin(annotation) is Annotated:
        if any(isinstance(m, PathArg) for m in get_args(annotation)[1:]):
            return True
    return any(_mentions_path_arg(arg) for arg in get_args(annotation))


def parameters_of(func: Callable, fixed: int = FIXED_PARAMETERS) -> list[Parameter]:
    """The action parameters of ``func``: everything after the fixed ones."""
    if inspect.iscoroutinefunction(func):
        raise SignatureError(f"{callable_name(func)}: async handlers are not supported")
    try:
        signature = inspect.signature(func, eval_str=True)
    except (TypeError, ValueError, NameError, AttributeError, SyntaxError) as e:
        raise SignatureError(f"cannot inspect {callable_name(func)}: {e}") from e
    params = list(signature.parameters.values())
    if len(params) < fixed:
        raise SignatureError(
            f"{callable_name(func)} needs {fixed} leading parameters, has {len(params)}"
        )
    for param in params[:fixed]:
        if param.kind not in _POSITIONAL:
            raise SignatureError(
                f"{callable_name(func)}: the first {fixed} parameters must be positional "
                f"({param.name} is {param.kind.description})"
            )
    result: list[Parameter] = []
    for param in params[fixed:]:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise SignatureError(
                f"{callable_name(func)}: *args/**kwargs are not supported for action arguments"
            )
        if param.kind is param.POSITIONAL_ONLY:
            raise SignatureError(
                f"{callable_name(func)}: positional-only action arguments are not supported"
            )
        annotation = str if param.annotation is param.empty else param.annotation
        if isinstance(annotation, str):
            raise SignatureError(
                f"{callable_name(func)}: annotation {annotation!r} of {param.name} could not be resolved"
            )
        base, path_kind = _split_annotation(annotation)
        if path_kind is None and _mentions_path(base):
            if _mentions_path_arg(base):
                raise SignatureError(
                    f"{callable_name(func)}: {param.name}: path arguments cannot be nested "
                    f"in containers; use a plain action.TagDir / action.Remote"
                )
            raise SignatureError(
                f"{callable_name(func)}: {param.name} is annotated with a bare path type; "
                f"use action.TagDir or action.Remote (DESIGN.md §4.4)"
            )
        result.append(
            Parameter(
                name=param.name,
                annotation=base,
                path_kind=path_kind,
                default=param.default,
            )
        )
    return result


def _adapters(func: Callable) -> tuple[list[Parameter], list[TypeAdapter]]:
    cached = getattr(func, _CACHE_ATTR, None)
    if cached is not None:
        return cached
    params = parameters_of(func)
    adapters: list[TypeAdapter] = []
    for p in params:
        try:
            adapter = TypeAdapter(p.annotation)
            adapter.json_schema()  # un-schemable annotations fail here, at load
        except Exception as e:
            raise SignatureError(
                f"{callable_name(func)}: cannot bind {p.name} ({p.annotation!r}): {e}"
            ) from e
        adapters.append(adapter)
    try:
        setattr(func, _CACHE_ATTR, (params, adapters))
    except (AttributeError, TypeError):
        pass  # builtins/partials: rebuilt on every call
    return params, adapters


# ------------------------------------------------------------------ schema


def signature_schema(func: Callable) -> dict[str, Any]:
    """JSON Schema of the handler's action arguments (stored on ``actions``)."""
    params, adapters = _adapters(func)
    properties: dict[str, Any] = {}
    defs: dict[str, Any] = {}
    for p, adapter in zip(params, adapters):
        schema = adapter.json_schema()
        defs.update(schema.pop("$defs", {}))
        if not p.required:
            # A default that does not fit its own annotation (``n: int = None``)
            # is the add-on's business; record it without pydantic's warning.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    schema["default"] = adapter.dump_python(p.default, mode="json")
                except Exception:
                    schema["default"] = str(p.default)
        if p.path_kind:
            schema["x-tfs-path"] = p.path_kind
        properties[p.name] = schema
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": [p.name for p in params if p.required],
    }
    if defs:
        result["$defs"] = defs
    return result


# ----------------------------------------------------------------- binding


def _coerce_literal(annotation: Any, raw: str) -> Any:
    """``Literal[800, 1600]`` from the slug string ``"800"``: a literal's
    members carry their own type, so try each member's type."""
    if get_origin(annotation) is not Literal:
        return raw
    for member in get_args(annotation):
        if isinstance(member, str):
            if member == raw:
                return member
            continue
        if isinstance(member, bool):
            # bool("false") is True: use pydantic's yes/no/1/0 parsing instead
            try:
                if _BOOL.validate_python(raw) is member:
                    return member
            except ValidationError:
                pass
            continue
        try:
            if type(member)(raw) == member:
                return member
        except (TypeError, ValueError):
            continue
    return raw


_BOOL = TypeAdapter(bool)


def raw_by_name(
    func: Callable, raw_args: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    """The run-key ``args`` for a call: slug strings keyed by parameter name,
    computable even when binding will fail (so a failed binding and a later
    success share one key). Surplus positional strings go under ``_extra``."""
    params, _ = _adapters(func)
    raw: dict[str, Any] = {p.name: value for p, value in zip(params, raw_args)}
    if len(raw_args) > len(params):
        raw["_extra"] = list(raw_args[len(params) :])
    return raw


def bind(
    func: Callable,
    raw_args: tuple[str, ...] | list[str],
    resolver: PathResolver | None = None,
) -> BoundArgs:
    """Match the slug's positional strings to ``func``'s action parameters."""
    params, adapters = _adapters(func)
    name = callable_name(func)
    if len(raw_args) > len(params):
        raise BindingError(
            f"{name} takes {len(params)} argument(s), the name gives {len(raw_args)}"
        )
    missing = [p.name for p in params[len(raw_args) :] if p.required]
    if missing:
        raise BindingError(f"{name} is missing argument(s) {', '.join(missing)}")

    bound = BoundArgs()
    for p, adapter, raw in zip(params, adapters, raw_args):
        bound.raw[p.name] = raw
        if p.path_kind:
            if resolver is None:
                raise BindingError(f"{p.name}: no resolver for {p.path_kind} arguments")
            try:
                value: Any = resolver(p.path_kind, raw)
            except Exception as e:
                raise BindingError(f"{p.name}: {e}") from e
        else:
            value = _coerce_literal(p.annotation, raw)
        try:
            bound.kwargs[p.name] = adapter.validate_python(value)
        except ValidationError as e:
            details = "; ".join(err["msg"] for err in e.errors())
            raise BindingError(f"{name}: {p.name}: {details}") from e
    for p in params[len(raw_args) :]:
        # A fresh copy per run: a mutable default must not be shared. Things
        # that cannot be copied (a lock, a stream) are shared on purpose.
        try:
            bound.kwargs[p.name] = copy.deepcopy(p.default)
        except Exception:
            bound.kwargs[p.name] = p.default
    return bound

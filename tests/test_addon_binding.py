# Code by AkinoAlice@TyrantRey

from pathlib import Path
from typing import Annotated, Literal

import pytest

from tag_file_system import action
from tag_file_system.addons.binding import (
    BindingError,
    SignatureError,
    bind,
    parameters_of,
    signature_schema,
)
from tag_file_system.core.interface.action import Hook, Severity


# -------------------------------------------------------------- decorators


def test_decorators_mark_functions_without_wrapping():
    @action.added()
    @action.modified()
    @action.removed(on_move=True)
    @action.tagged("Photos")
    def run(path, metadata, ctx):
        return "called"

    assert run(None, None, None) == "called"
    specs = action.handlers_of(run)
    # listed top-down, as written in the source
    assert [s.describe() for s in specs] == [
        "added",
        "modified",
        "removed+move",
        "tagged:photos",
    ]
    assert specs[3].tag == "photos"  # normalized like a parsed tag
    assert specs[3].hook is Hook.TAGGED
    assert action.handlers_of(lambda: None) == []


def test_problem_decorators():
    @action.warn()
    def notify(problem, ctx): ...

    (spec,) = action.handlers_of(notify)
    assert spec.kind == "problem" and spec.severity is Severity.WARN
    assert (
        action.handlers_of(action.crit()(lambda p, c: None))[0].severity
        is Severity.CRIT
    )
    assert (
        action.handlers_of(action.err()(lambda p, c: None))[0].severity is Severity.ERR
    )
    assert (
        action.handlers_of(action.info()(lambda p, c: None))[0].severity
        is Severity.INFO
    )


def test_tagged_rejects_invalid_tags():
    with pytest.raises(ValueError):
        action.tagged("")
    with pytest.raises(ValueError):
        action.tagged("a:b")


def test_decorator_on_non_callable_is_a_type_error():
    with pytest.raises(TypeError):
        action.added()("not a function")  # type: ignore[arg-type]


# -------------------------------------------------------------- parameters


def test_parameters_after_the_fixed_three():
    def run(path, metadata, ctx, width: int, dst: action.Remote, mode="fast"): ...

    params = parameters_of(run)
    assert [p.name for p in params] == ["width", "dst", "mode"]
    assert params[0].annotation is int and params[0].required
    assert params[1].annotation is Path and params[1].path_kind == "remote"
    assert params[2].annotation is str and params[2].default == "fast"
    assert parameters_of(lambda p, m, c: None) == []


@pytest.mark.parametrize(
    "func",
    [
        lambda p, m: None,  # too few fixed params
        lambda p, m, c, *args: None,
        lambda p, m, c, **kw: None,
    ],
)
def test_unsupported_signatures_are_signature_errors(func):
    with pytest.raises(SignatureError):
        parameters_of(func)


def test_positional_only_action_args_are_rejected():
    def run(path, metadata, ctx, width, /): ...

    with pytest.raises(SignatureError):
        parameters_of(run)


# ----------------------------------------------------------------- binding


def resolver(kind: str, raw: str) -> Path:
    if raw == "missing":
        raise LookupError(f"unknown {kind} {raw!r}")
    return Path(f"/resolved/{kind}/{raw}")


def test_bind_coerces_and_keeps_raw_strings():
    def run(
        path,
        metadata,
        ctx,
        width: int,
        ratio: float,
        dst: action.Remote,
        flag: bool = False,
    ): ...

    bound = bind(run, ("800", "1.5", "photos"), resolver)

    assert bound.kwargs == {
        "width": 800,
        "ratio": 1.5,
        "dst": Path("/resolved/remote/photos"),
        "flag": False,
    }
    assert bound.raw == {"width": "800", "ratio": "1.5", "dst": "photos"}


def test_bind_defaults_and_untyped_params():
    def run(path, metadata, ctx, suffix, dst: action.TagDir = Path("out")): ...

    bound = bind(run, (".jpg",), resolver)
    assert bound.kwargs == {"suffix": ".jpg", "dst": Path("out")}
    assert bound.raw == {"suffix": ".jpg"}

    bound = bind(run, (".jpg", "photos"), resolver)
    assert bound.kwargs["dst"] == Path("/resolved/tagdir/photos")
    assert bind(lambda p, m, c: None, ()).kwargs == {}


def test_bind_literal_and_enum_like_types():
    def run(path, metadata, ctx, mode: Literal["fast", "slow"]): ...

    assert bind(run, ("fast",)).kwargs == {"mode": "fast"}
    with pytest.raises(BindingError) as exc:
        bind(run, ("medium",))
    assert "mode" in str(exc.value)


@pytest.mark.parametrize(
    "raw_args, message",
    [
        ((), "missing argument(s) width"),
        (("1", "2", "3"), "takes 2 argument(s), the name gives 3"),
        (("eight", "photos"), "width"),
        (("8", "missing"), "unknown remote"),
    ],
)
def test_bind_errors_are_binding_errors(raw_args, message):
    def run(path, metadata, ctx, width: int, dst: action.Remote): ...

    with pytest.raises(BindingError) as exc:
        bind(run, raw_args, resolver)
    assert message in str(exc.value)


def test_path_args_need_a_resolver():
    def run(path, metadata, ctx, dst: action.Remote): ...

    with pytest.raises(BindingError):
        bind(run, ("photos",))


def test_bool_coercion_follows_pydantic_lax_rules():
    def run(path, metadata, ctx, flag: bool): ...

    assert bind(run, ("true",)).kwargs == {"flag": True}
    assert bind(run, ("0",)).kwargs == {"flag": False}
    with pytest.raises(BindingError):
        bind(run, ("maybe",))


def test_bare_decorators_are_type_errors():
    with pytest.raises(TypeError, match="parentheses"):
        action.added(lambda p, m, c: None)
    with pytest.raises(TypeError, match="parentheses"):
        action.removed(lambda p, m, c: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="parentheses"):
        action.tagged(lambda p, m, c: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="parentheses"):
        action.warn(lambda p, c: None)
    with pytest.raises(TypeError, match="parentheses"):
        action.on_start(lambda c: None)
    with pytest.raises(TypeError, match="parentheses"):
        action.on_stop(lambda c: None)
    with pytest.raises(TypeError):
        action.removed(on_move="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        action.added()(len)  # builtins cannot carry marks


def test_identical_marks_collapse():
    @action.added()
    @action.added()
    @action.tagged("Photos")
    @action.tagged("photos")
    def run(p, m, c): ...

    assert [s.describe() for s in action.handlers_of(run)] == ["added", "tagged:photos"]


def test_path_marker_survives_optional_and_annotated_metadata():
    from typing import Optional

    from pydantic import Field

    def run(
        p,
        m,
        c,
        a: action.TagDir | None = None,
        b: Optional[action.Remote] = None,
        n: Annotated[int, Field(gt=0)] = 1,
    ): ...

    params = parameters_of(run)
    assert [(x.name, x.path_kind) for x in params] == [
        ("a", "tagdir"),
        ("b", "remote"),
        ("n", None),
    ]
    bound = bind(run, ("x", "y", "3"), resolver)
    assert bound.kwargs == {
        "a": Path("/resolved/tagdir/x"),
        "b": Path("/resolved/remote/y"),
        "n": 3,
    }
    with pytest.raises(BindingError):
        bind(run, ("x", "y", "0"), resolver)  # Field(gt=0) still enforced
    schema = signature_schema(run)
    assert schema["properties"]["a"]["x-tfs-path"] == "tagdir"
    assert schema["properties"]["b"]["x-tfs-path"] == "remote"


def test_bare_path_annotations_are_rejected_at_load():
    def run(p, m, c, dst: Path): ...

    with pytest.raises(SignatureError, match="TagDir"):
        parameters_of(run)

    def run2(p, m, c, dst: Path | None = None): ...

    with pytest.raises(SignatureError):
        parameters_of(run2)


def test_fixed_parameters_must_be_positional_and_sync():
    def kw(path, metadata, *, ctx): ...

    with pytest.raises(SignatureError):
        parameters_of(kw)

    async def coro(path, metadata, ctx): ...

    with pytest.raises(SignatureError, match="async"):
        parameters_of(coro)


def test_string_annotations_resolve_or_fail_loudly():
    def run(p, m, c, n: "int", dst: "action.Remote"): ...

    params = parameters_of(run)
    assert params[0].annotation is int and params[1].path_kind == "remote"

    namespace: dict = {}
    exec("def bad(p, m, c, x: 'NoSuchType'): ...", namespace)  # noqa: S102 - test input

    with pytest.raises(SignatureError):
        parameters_of(namespace["bad"])


def test_literal_members_are_coerced_by_their_own_type():
    def run(
        p, m, c, size: Literal[800, 1600], mode: Literal["fast", "slow"] = "fast"
    ): ...

    assert bind(run, ("800",)).kwargs == {"size": 800, "mode": "fast"}
    with pytest.raises(BindingError):
        bind(run, ("801",))


def test_any_parameter_name_is_bindable():
    def run(p, m, c, _n: int, model_config: str = "x", schema: str = "y"): ...

    bound = bind(run, ("1", "cfg"))
    assert bound.kwargs == {"_n": 1, "model_config": "cfg", "schema": "y"}
    assert set(signature_schema(run)["properties"]) == {"_n", "model_config", "schema"}


def test_literal_bools_use_yes_no_parsing():
    def run(p, m, c, flag: Literal[True, False]): ...

    assert bind(run, ("false",)).kwargs == {"flag": False}
    assert bind(run, ("0",)).kwargs == {"flag": False}
    assert bind(run, ("yes",)).kwargs == {"flag": True}


def test_defaults_are_copied_per_bind():
    def run(p, m, c, items: list = []): ...  # noqa: B006 - the point of the test

    first = bind(run, ()).kwargs["items"]
    first.append(9)
    assert bind(run, ()).kwargs["items"] == []


def test_uncopyable_defaults_are_shared_not_fatal():
    import threading

    lock = threading.Lock()

    def run(p, m, c, guard: object = lock): ...

    assert bind(run, ()).kwargs["guard"] is lock


def test_unschemable_annotations_are_signature_errors():
    from typing import Callable

    class Custom: ...

    def a(p, m, c, cb: Callable): ...
    def b(p, m, c, t: Custom): ...

    with pytest.raises(SignatureError):
        signature_schema(a)
    with pytest.raises(SignatureError):
        bind(b, ("x",))


def test_nested_path_args_have_a_clear_message():
    def run(p, m, c, dsts: list[action.TagDir]): ...

    with pytest.raises(SignatureError, match="nested in containers"):
        parameters_of(run)


def test_resolver_failures_of_any_kind_are_binding_errors():
    def run(p, m, c, dst: action.Remote): ...

    def broken(kind, raw):
        raise OSError("disk gone")

    with pytest.raises(BindingError, match="disk gone"):
        bind(run, ("x",), broken)


# ------------------------------------------------------------------ schema


def test_signature_schema_is_json_schema_with_path_markers():
    def run(
        path, metadata, ctx, width: int, dst: action.Remote, mode: str = "fast"
    ): ...

    schema = signature_schema(run)

    assert schema["type"] == "object"
    assert schema["required"] == ["width", "dst"]
    assert schema["properties"]["width"]["type"] == "integer"
    assert schema["properties"]["mode"]["default"] == "fast"
    assert schema["properties"]["dst"]["x-tfs-path"] == "remote"
    assert schema["properties"]["dst"]["format"] == "path"
    assert signature_schema(lambda p, m, c: None)["properties"] == {}

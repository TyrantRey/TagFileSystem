# Code by AkinoAlice@TyrantRey

"""Discovery and (re)loading of add-ons in ``script/`` (DESIGN/v0-1-0.md §4.1).

``script/<name>.py`` binds the function slug ``name``. Every top-level
``*.py`` is imported eagerly; a file starting with ``_`` is a helper module
and is never an add-on. A non-lowercase filename is a P2 warn and is not
loaded; a file that fails to import is a P1 err and the others still load.
Reloading a file that fails keeps its previous version.

Scripts are executed from source (no ``.pyc``): a cached bytecode file would
both defeat hot reload when an edit keeps size and mtime-second, and drop a
``__pycache__`` directory into ``script/`` for the watcher to see.

imports *from* a script — ``from _helper import X``, ``import make_copy`` —
are served by ``_ScriptFinder``, a meta-path finder that looks at the file
the importing code lives in and answers from that root's ``script/`` only.
It sits last in ``sys.meta_path`` and ``sys.modules`` is consulted first,
so a script can never shadow the standard library or a package.
"""

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import re
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ClassVar, Protocol, Sequence

from tag_file_system.action import HandlerSpec, handlers_of
from tag_file_system.addons.binding import (
    LIFECYCLE_PARAMETERS,
    PROBLEM_PARAMETERS,
    SignatureError,
    callable_name,
    parameters_of,
    signature_schema,
)
from tag_file_system.core.interface.action import ActionRecord, Hook, Severity
from tag_file_system.core.interface.tag import normalize_tag
from tag_file_system.core.logger import logger
from tag_file_system.database.action_store import ActionStore
from tag_file_system.root import SCRIPT_DIR, Root

# Same rule as ActionCall.name (core/interface/tag.py): a slug that a marker
# could never spell must not bind a script.
ADDON_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PACKAGE = "tfs_addons"
MODULE_PREFIX = PACKAGE + "."
_STACK_LIMIT = 60


class ProblemReporter(Protocol):
    def __call__(
        self,
        severity: Severity,
        kind: str,
        message: str,
        /,
        *,
        action_name: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class FileHandler:
    addon: "Addon"
    func: Callable
    spec: HandlerSpec
    schema: dict[str, Any]

    @property
    def hook(self) -> Hook:
        assert self.spec.hook is not None
        return self.spec.hook

    @property
    def name(self) -> str:
        return callable_name(self.func)


@dataclass(frozen=True)
class ProblemHandler:
    addon: "Addon"
    func: Callable
    severity: Severity

    @property
    def name(self) -> str:
        return callable_name(self.func)


@dataclass(frozen=True)
class LifecycleHandler:
    """``@action.on_start`` / ``@action.on_stop``: called with ``(ctx)`` only —
    there is no file (DESIGN/v0-3-0.md §2)."""

    addon: "Addon"
    func: Callable
    hook: Hook

    @property
    def name(self) -> str:
        return callable_name(self.func)


NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


@dataclass
class Addon:
    name: str
    path: Path
    key: PurePosixPath  # root-relative
    script_hash: str
    module: types.ModuleType
    file_handlers: list[FileHandler] = field(default_factory=list)
    problem_handlers: list[ProblemHandler] = field(default_factory=list)
    lifecycle_handlers: list[LifecycleHandler] = field(default_factory=list)
    record: ActionRecord | None = None

    @property
    def hooks(self) -> list[Hook]:
        seen: dict[Hook, None] = {}
        for handler in self.file_handlers:
            seen.setdefault(handler.hook)
        for lifecycle in self.lifecycle_handlers:
            seen.setdefault(lifecycle.hook)
        return list(seen)

    def handlers(self, hook: Hook) -> list[FileHandler]:
        return [h for h in self.file_handlers if h.hook is hook]

    def lifecycle(self, hook: Hook) -> list[LifecycleHandler]:
        return [h for h in self.lifecycle_handlers if h.hook is hook]

    @property
    def signature(self) -> dict[str, Any]:
        """Per-hook JSON Schema of the action arguments (``actions.signature_json``)."""
        return {
            **{h.spec.describe(): h.schema for h in self.file_handlers},
            **{h.hook.value: NO_ARGUMENTS for h in self.lifecycle_handlers},
        }

    def describe(self) -> dict[str, Any]:
        """What ``tfs list`` and ``/actions`` report about this add-on."""
        return {
            "name": self.name,
            "script": self.key.as_posix(),
            "script_hash": self.script_hash,
            "hooks": [
                *(h.spec.describe() for h in self.file_handlers),
                *(h.hook.value for h in self.lifecycle_handlers),
            ],
            "problem_hooks": [h.severity.value for h in self.problem_handlers],
            "signature": self.signature,
        }


def script_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _silent(*_args: Any, **_kwargs: Any) -> None:
    pass


def _source_order(item: tuple[str, Any]) -> tuple[int, str]:
    name, member = item
    code = getattr(member, "__code__", None)
    return (getattr(code, "co_firstlineno", 10**9), name)


def _ensure_package() -> None:
    """A synthetic ``tfs_addons`` package so that classes defined in add-ons
    can be pickled (``pickle`` imports ``tfs_addons.<name>``)."""
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = []
        package.__package__ = PACKAGE
        sys.modules[PACKAGE] = package


class _AddonModuleLoader(importlib.abc.Loader):
    """Serves ``import make_copy`` from a sibling: the add-on's own module,
    loaded through its ``AddonLoader`` if it is not yet."""

    def __init__(self, owner: "AddonLoader", path: Path) -> None:
        self.owner = owner
        self.path = path

    def create_module(
        self, spec: importlib.machinery.ModuleSpec
    ) -> types.ModuleType | None:
        addon = self.owner.addons.get(self.path.stem)
        if addon is not None:
            return addon.module
        executing = sys.modules.get(MODULE_PREFIX + self.path.stem)
        if executing is not None:
            # A circular import: hand back the partially initialised module,
            # exactly as the import system does for regular packages.
            return executing
        attempted = self.owner._loaded_this_pass
        if attempted is not None and self.path.stem in attempted:
            raise ImportError(
                f"add-on {self.path.name} failed to load"
            )  # reported already
        addon = self.owner.load(self.path)
        if addon is None:
            raise ImportError(f"add-on {self.path.name} failed to load")
        return addon.module

    def exec_module(self, module: types.ModuleType) -> None:
        pass  # already executed by AddonLoader


class _NoBytecodeLoader(importlib.machinery.SourceFileLoader):
    """A helper's source loader that never writes ``__pycache__``: the
    watcher must not see bytecode appear under ``script/``."""

    def set_data(self, path: str, data: Any, *, _mode: int = 0o666) -> None:
        pass


class _ScriptFinder(importlib.abc.MetaPathFinder):
    """Top-level imports made by code living in a ``script/`` directory are
    resolved against *that* directory: helpers (``_x.py``) as ordinary
    source modules, add-ons through their loader (executed once, shared)."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if path is not None or "." in fullname:
            return None
        owner = _importing_loader()
        if owner is None:
            return None
        candidate = owner.script_dir / f"{fullname}.py"
        if not candidate.is_file():
            return None
        if fullname.startswith("_"):
            return importlib.util.spec_from_file_location(
                fullname, candidate, loader=_NoBytecodeLoader(fullname, str(candidate))
            )
        return importlib.machinery.ModuleSpec(
            fullname, _AddonModuleLoader(owner, candidate), origin=str(candidate)
        )


def _importing_loader() -> "AddonLoader | None":
    """The loader whose ``script/`` the currently importing code lives in,
    found by walking the call stack (works for lazy imports in handlers and
    in threads they start), falling back to the loader executing a script."""
    frame = sys._getframe(1)
    depth = 0
    while frame is not None and depth < _STACK_LIMIT:
        file = frame.f_globals.get("__file__")
        if file:
            try:
                owner = AddonLoader._by_dir.get(Path(file).parent.resolve())
            except OSError:
                owner = None
            if owner is not None:
                return owner
        frame = frame.f_back
        depth += 1
    active = getattr(_executing, "stack", None)
    return active[-1] if active else None


_executing = threading.local()  # per-thread stack of loaders executing a script


class AddonLoader:
    _by_dir: ClassVar[dict[Path, "AddonLoader"]] = {}

    def __init__(
        self,
        root: Root,
        store: ActionStore | None = None,
        report: ProblemReporter | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.report: ProblemReporter = report if report is not None else _silent
        self.addons: dict[str, Addon] = {}
        self._loaded_this_pass: set[str] | None = None
        self.logger = logger
        self.script_dir = root.script_dir.resolve()
        AddonLoader._by_dir[self.script_dir] = self
        _ensure_package()
        if not any(isinstance(f, _ScriptFinder) for f in sys.meta_path):
            sys.meta_path.append(_ScriptFinder())

    # -------------------------------------------------------------- loading

    def load_all(self) -> list[Addon]:
        if not self.root.script_dir.is_dir():
            self.report(
                Severity.CRIT, "script.unreadable", f"{self.root.script_dir} is missing"
            )
            return []
        self._purge_helpers()
        self._loaded_this_pass = set()
        loaded: list[Addon] = []
        try:
            for path in sorted(self.root.script_dir.iterdir()):
                if (
                    path.name.startswith("_")
                    or not path.is_file()
                    or path.suffix != ".py"
                ):
                    if (
                        path.suffix.lower() == ".py"
                        and path.is_file()
                        and not path.name.startswith("_")
                    ):
                        self.load(path)  # reports the .PY warning
                    continue
                if path.stem in self._loaded_this_pass:
                    # Already executed (or already failed) through a sibling's
                    # import earlier in this pass.
                    if path.stem in self.addons:
                        loaded.append(self.addons[path.stem])
                    continue
                addon = self.load(path)
                if addon is not None:
                    loaded.append(addon)
        finally:
            self._loaded_this_pass = None
        return sorted(loaded, key=lambda a: a.name)

    def _in_script_dir(self, path: Path) -> bool:
        try:
            return path.parent.resolve() == self.script_dir
        except OSError:
            return False

    def is_addon_file(self, path: Path) -> bool:
        return (
            path.suffix == ".py"
            and not path.name.startswith("_")
            and self._in_script_dir(path)
        )

    def is_helper_file(self, path: Path) -> bool:
        return (
            path.suffix == ".py"
            and path.name.startswith("_")
            and self._in_script_dir(path)
        )

    def load(self, path: Path) -> Addon | None:
        """(Re)load one script. Returns the add-on, or ``None`` when the file
        is not an add-on or failed to load (the previous version, if any, is
        kept in that case). Loading a helper reloads every add-on."""
        path = Path(path)
        if self.is_helper_file(path):
            self.load_all()
            return None
        if not self._in_script_dir(path) or not path.is_file():
            return None
        if path.suffix.lower() == ".py" and path.suffix != ".py":
            self.report(
                Severity.WARN,
                "addon.filename",
                f"{path.name} is not loaded: the extension must be lowercase .py",
            )
            return None
        if not self.is_addon_file(path):
            return None
        name = path.stem
        if not ADDON_NAME.match(name) or name.endswith("_"):
            self.report(
                Severity.WARN,
                "addon.filename",
                f"{path.name} is not loaded: add-on filenames must match "
                f"[a-z][a-z0-9_]*.py and not end with '_'",
            )
            return None

        previous = self.addons.get(name)
        try:
            source = path.read_bytes()
            module, saved = self._execute(name, path, source)
        except (Exception, SystemExit) as e:  # SyntaxError, importError, anything
            self.report(
                Severity.ERR,
                "addon.import",
                f"{path.name} failed to load: {type(e).__name__}: {e}",
                action_name=name,
            )
            self.logger.exception(f"Add-on {name} failed to load")
            if self._loaded_this_pass is not None:
                self._loaded_this_pass.add(name)  # reported once per pass
            return None

        addon = Addon(
            name=name,
            path=path,
            key=PurePosixPath(SCRIPT_DIR) / path.name,
            script_hash=hashlib.sha256(source).hexdigest(),
            module=module,
        )
        signature_problems = self._collect(addon)
        if not (
            addon.file_handlers or addon.problem_handlers or addon.lifecycle_handlers
        ):
            if signature_problems and previous is not None:
                # Every handler is broken: that is a failed reload.
                self._restore(saved)
                return None
            if not signature_problems:
                self.report(
                    Severity.WARN,
                    "addon.empty",
                    f"{path.name} defines no @action handlers",
                    action_name=name,
                )

        if self.store is not None:
            try:
                addon.record = self.store.register_action(
                    name=name,
                    script_path=addon.key,
                    script_hash=addon.script_hash,
                    signature=addon.signature,
                    hooks=addon.hooks,
                )
            except Exception as e:
                self.report(
                    Severity.CRIT,
                    "db.unwritable",
                    f"{path.name} could not be registered: {type(e).__name__}: {e}",
                    action_name=name,
                )
                self.logger.exception(f"Add-on {name} could not be registered")
                self._restore(saved)
                return None

        self.addons[name] = addon
        if self._loaded_this_pass is not None:
            self._loaded_this_pass.add(name)
        # A sibling that imported the previous version keeps its reference;
        # the bare-name entry (if any) must point at the new module.
        if name in sys.modules and getattr(sys.modules[name], "__file__", None) == str(
            path
        ):
            sys.modules[name] = module
        self.logger.info(
            f"Loaded add-on {name} ({len(addon.file_handlers)} file handler(s), "
            f"{len(addon.problem_handlers)} problem handler(s), "
            f"{len(addon.lifecycle_handlers)} lifecycle handler(s))"
        )
        return addon

    def unload(self, path: Path) -> Addon | None:
        path = Path(path)
        if self.is_helper_file(path):
            self._purge_helpers()
            return None
        if not self.is_addon_file(path):
            return None
        name = path.stem
        addon = self.addons.pop(name, None)
        sys.modules.pop(MODULE_PREFIX + name, None)
        alias = sys.modules.get(name)
        if alias is not None and getattr(alias, "__file__", None) == str(path):
            del sys.modules[name]
        if addon is not None:
            self.logger.info(f"Unloaded add-on {name}")
        return addon

    # ------------------------------------------------------------ internals

    def _execute(
        self, name: str, path: Path, source: bytes
    ) -> tuple[types.ModuleType, dict[str, types.ModuleType | None]]:
        """Run the script's source in a fresh module object.

        The module is registered as ``tfs_addons.<name>`` *before* it runs,
        as the import system would (dataclasses, pickle and friends look it
        up); on failure the previous entry is restored. Returns the module
        and the entries to restore should a later step fail.
        """
        module_name = MODULE_PREFIX + name
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        module.__spec__ = importlib.util.spec_from_file_location(module_name, path)
        module.__package__ = PACKAGE
        saved = {module_name: sys.modules.get(module_name)}
        sys.modules[module_name] = module
        no_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True  # helpers imported here: no __pycache__
        stack = getattr(_executing, "stack", None)
        if stack is None:
            stack = _executing.stack = []
        stack.append(self)
        try:
            code = compile(source, str(path), "exec", dont_inherit=True)
            exec(code, module.__dict__)  # noqa: S102 - the user's own add-ons
        except BaseException:
            self._restore(saved)
            raise
        finally:
            stack.pop()
            sys.dont_write_bytecode = no_bytecode
        return module, saved

    @staticmethod
    def _restore(saved: dict[str, types.ModuleType | None]) -> None:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def _purge_helpers(self) -> None:
        """Forget the helper modules (``_x``) imported from this root's
        ``script/`` — and every helper of *another* root, which must never be
        found by this root's add-ons — so the next import reads the file."""
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("_") or module_name.startswith(MODULE_PREFIX):
                continue
            file = getattr(module, "__file__", None)
            if not file:
                continue
            try:
                parent = Path(file).parent.resolve()
            except OSError:
                continue
            if parent in AddonLoader._by_dir:
                del sys.modules[module_name]

    def _collect(self, addon: Addon) -> int:
        """Register the marked functions of the module. Returns how many
        handlers were skipped for a bad signature."""
        seen_slots: dict[tuple[Hook, str | None], tuple[str, str]] = {}
        seen_marks: set[tuple[int, HandlerSpec]] = set()
        skipped = 0
        for attr, member in sorted(vars(addon.module).items(), key=_source_order):
            marks = handlers_of(member)
            if not marks or not callable(member):
                continue
            owner = getattr(member, "__module__", None) or ""
            if owner.startswith(MODULE_PREFIX) and owner != addon.module.__name__:
                continue  # a sibling add-on's handler, merely imported here
            for spec in marks:
                if (id(member), spec) in seen_marks:
                    continue  # the same function under another name
                seen_marks.add((id(member), spec))
                try:
                    if spec.kind == "problem":
                        extra = parameters_of(member, fixed=PROBLEM_PARAMETERS)
                        required = [p.name for p in extra if p.required]
                        if required:
                            raise SignatureError(
                                f"{callable_name(member)}: problem handlers take (problem, ctx); "
                                f"{', '.join(required)} would never be supplied"
                            )
                        assert spec.severity is not None
                        addon.problem_handlers.append(
                            ProblemHandler(
                                addon=addon, func=member, severity=spec.severity
                            )
                        )
                    elif spec.kind == "lifecycle":
                        assert spec.hook is not None
                        extra = parameters_of(member, fixed=LIFECYCLE_PARAMETERS)
                        required = [p.name for p in extra if p.required]
                        if required:
                            raise SignatureError(
                                f"{callable_name(member)}: {spec.hook.value} handlers take (ctx); "
                                f"{', '.join(required)} would never be supplied"
                            )
                        slot = (spec.hook, None)
                        if slot in seen_slots:
                            winner, describe = seen_slots[slot]
                            self.report(
                                Severity.ERR,
                                "addon.duplicate_handler",
                                f"{addon.path.name}: handler {callable_name(member)} "
                                f"({spec.describe()}) skipped: {winner} already handles {describe}",
                                action_name=addon.name,
                            )
                            skipped += 1
                            continue
                        seen_slots[slot] = (callable_name(member), spec.describe())
                        addon.lifecycle_handlers.append(
                            LifecycleHandler(addon=addon, func=member, hook=spec.hook)
                        )
                    else:
                        assert spec.hook is not None
                        schema = signature_schema(member)  # SignatureError first
                        slot = (spec.hook, spec.tag)
                        if slot in seen_slots:
                            # Runs are keyed per (file, add-on, hook, args): a
                            # second handler on the same hook could never run.
                            winner, describe = seen_slots[slot]
                            self.report(
                                Severity.ERR,
                                "addon.duplicate_handler",
                                f"{addon.path.name}: handler {callable_name(member)} "
                                f"({spec.describe()}) skipped: {winner} already handles {describe}",
                                action_name=addon.name,
                            )
                            skipped += 1
                            continue
                        seen_slots[slot] = (callable_name(member), spec.describe())
                        addon.file_handlers.append(
                            FileHandler(
                                addon=addon, func=member, spec=spec, schema=schema
                            )
                        )
                except Exception as e:
                    skipped += 1
                    self.report(
                        Severity.ERR,
                        "addon.signature",
                        f"{addon.path.name}: handler {attr} skipped: {e}",
                        action_name=addon.name,
                    )
        return skipped

    # -------------------------------------------------------------- lookups

    def addon_for(self, name: str) -> Addon | None:
        return self.addons.get(name)

    def handlers_for(self, name: str, hook: Hook) -> list[FileHandler]:
        addon = self.addons.get(name)
        return addon.handlers(hook) if addon is not None else []

    def tag_handlers(self, tag: str) -> list[FileHandler]:
        wanted = normalize_tag(tag)
        return [
            h
            for addon in self.addons.values()
            for h in addon.file_handlers
            if h.hook is Hook.TAGGED and h.spec.tag == wanted
        ]

    def lifecycle_handlers(self, hook: Hook) -> list[LifecycleHandler]:
        """Every loaded add-on's handler for ``on_start``/``on_stop``, by
        add-on name so the order of a session does not depend on the disk."""
        return [
            h for name in sorted(self.addons) for h in self.addons[name].lifecycle(hook)
        ]

    def problem_handlers(self, severity: Severity) -> list[ProblemHandler]:
        """Handlers whose level covers ``severity`` (level and above)."""
        return [
            h
            for addon in self.addons.values()
            for h in addon.problem_handlers
            if h.severity.covers(severity)
        ]

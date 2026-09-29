"""Import missing in-repo Maven modules as jdtls workspace folders (#110).

jdtls imports only the opened file's Maven group. A dependency on a reactor module in
another group is then resolved by m2e from the local repository at ``${revision}``,
where it is usually missing, and every symbol from it is "cannot be resolved". m2e
reports it as an Error diagnostic on the dependent's pom.xml
(``[Offline / ]Missing artifact g:a:type:version``).

``DependencyModules`` turns that signal into a batched, debounced
``workspace/didChangeWorkspaceFolders`` add of the module directories found in the
reactor index, within a session budget. Modules added this way report their own
missing dependencies through the same signal, so the closure is imported round by
round, only as far as it is actually missing.

jdtls fixes the dependent's classpath within a fraction of a second, but it never
republishes diagnostics for documents that are already open. ``ClasspathRefresher``
therefore sends ``java.project.refreshDiagnostics`` for every open ``.java`` file
under a project whose classpath was updated.

Both classes are asyncio-only (no locks) and take their side effects as callables,
so they are unit-testable without jdtls.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .reactor import ReactorIndex, build_reactor_index

logger = logging.getLogger(__name__)

ENV_BUDGET = "JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES"
DEFAULT_BUDGET = 60
MAX_BUDGET = 200
ADD_DEBOUNCE_SEC = 0.5
REFRESH_DEBOUNCE_SEC = 1.0
REFRESH_TIMEOUT_SEC = 10.0
_WARN_LIST_MAX = 20

Folder = dict[str, str]
SendFolders = Callable[[list[Folder], list[Folder]], Awaitable[None]]


def resolve_budget(config: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Session budget of dependency-module folders.

    ``JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES`` wins over ``{"jdtls": {"dependencyModules": N}}``
    in ``.java-functional-lsp.json``. 0 disables the feature; values are clamped to
    ``[0, MAX_BUDGET]``; an invalid value logs a warning and uses ``DEFAULT_BUDGET``.
    """
    env = environ if environ is not None else os.environ
    raw: object = env.get(ENV_BUDGET)
    source = ENV_BUDGET
    if raw is None or raw == "":
        jdtls_cfg = (config or {}).get("jdtls")
        raw = jdtls_cfg.get("dependencyModules") if isinstance(jdtls_cfg, Mapping) else None
        source = "jdtls.dependencyModules"
    if raw is None:
        return DEFAULT_BUDGET
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError
        value = int(raw)
    except ValueError:
        logger.warning("jdtls: invalid %s %r, using default %d", source, raw, DEFAULT_BUDGET)
        return DEFAULT_BUDGET
    if value > MAX_BUDGET:
        logger.warning("jdtls: %s %d is above the maximum, using %d", source, value, MAX_BUDGET)
        return MAX_BUDGET
    return max(value, 0)


def path_from_uri(uri: str) -> Path | None:
    """Filesystem path of a ``file:`` URI in any of jdtls's forms (``file:/x/``, ``file:///x``)."""
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return None
    path = unquote(urlparse(uri).path)
    if not path:
        return None
    return Path(path.rstrip("/") or "/")


def _is_under(path: Path, folder: Path) -> bool:
    return path == folder or path.is_relative_to(folder)


def _display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return path.name


class DependencyModules:
    """Session registry and importer of dependency-module folders."""

    def __init__(
        self,
        send_folders: SendFolders,
        covered_roots: Callable[[], Iterable[Path]],
        reactor_root_for: Callable[[Path], Path],
        to_uri: Callable[[Path], str],
        *,
        budget: int = DEFAULT_BUDGET,
        debounce: float = ADD_DEBOUNCE_SEC,
        index_builder: Callable[[Path], ReactorIndex] = build_reactor_index,
    ) -> None:
        self._send_folders = send_folders
        self._covered_roots = covered_roots
        self._reactor_root_for = reactor_root_for
        self._to_uri = to_uri
        self._index_builder = index_builder
        self.budget = budget
        self.debounce = debounce
        self._indexes: dict[Path, asyncio.Task[ReactorIndex | None]] = {}
        #: GA → module dir, folders currently added by this class.
        self.registry: dict[str, Path] = {}
        #: Folders removed because a group expansion covered them; never re-added.
        self._retired: set[Path] = set()
        self._pending: dict[str, Path] = {}
        self._unresolved: list[str] = []
        self._used = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._roots: dict[Path, Path] = {}

    @property
    def enabled(self) -> bool:
        return self.budget > 0

    @property
    def used(self) -> int:
        return self._used

    def folders(self) -> list[Path]:
        return list(self.registry.values())

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no running loop (sync unit test): nothing to schedule
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def note_missing(self, pom_path: Path, gas: list[str]) -> None:
        """m2e reported *gas* missing for the module of *pom_path*: queue the in-repo ones."""
        if not self.enabled or not gas:
            return
        self._spawn(self._consider(pom_path.parent, gas))

    async def _index_for(self, module_dir: Path) -> ReactorIndex | None:
        """Reactor index for *module_dir*, built once per reactor root (single flight)."""
        root = self._roots.get(module_dir)
        if root is None:
            root = self._reactor_root_for(module_dir)
            self._roots[module_dir] = root
        task = self._indexes.get(root)
        if task is None:
            task = asyncio.get_running_loop().create_task(self._build_index(root))
            self._indexes[root] = task
            self._tasks.add(task)
        return await asyncio.shield(task)

    async def _build_index(self, root: Path) -> ReactorIndex | None:
        try:
            index = await asyncio.get_running_loop().run_in_executor(None, self._index_builder, root)
        except Exception:
            logger.warning("jdtls: reactor index of %s failed", root.name, exc_info=True)
            return None
        logger.info(
            "jdtls: reactor index of %s: %d modules from %d poms%s",
            root.name,
            len(index.modules),
            index.poms_read,
            " (truncated)" if index.truncated else "",
        )
        return index

    def _is_covered(self, module_dir: Path) -> bool:
        if any(_is_under(module_dir, folder) for folder in self.registry.values()):
            return True
        return any(_is_under(module_dir, folder) for folder in self._covered_roots())

    async def _consider(self, module_dir: Path, gas: list[str]) -> None:
        index = await self._index_for(module_dir)
        if index is None:
            return
        queued = False
        for ga in gas:
            target = index.get(ga)
            if target is None:
                continue  # not an in-repo module (a real external artifact, or ambiguous)
            if ga in self.registry or ga in self._pending or target in self._retired:
                continue
            if self._is_covered(target):
                continue
            self._pending[ga] = target
            queued = True
        if queued and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_after_debounce(index.root))
            self._tasks.add(self._flush_task)
            self._flush_task.add_done_callback(self._tasks.discard)

    async def _flush_after_debounce(self, root: Path) -> None:
        await asyncio.sleep(self.debounce)
        await self.flush(root)

    async def flush(self, root: Path) -> None:
        """Send one didChangeWorkspaceFolders add for the queued modules, within the budget."""
        pending, self._pending = self._pending, {}
        added: list[Folder] = []
        shown: list[str] = []
        overflow: list[str] = []
        for ga, target in pending.items():
            if ga in self.registry or target in self._retired or self._is_covered(target):
                continue
            if self._used >= self.budget:
                overflow.append(ga)
                continue
            self._used += 1
            self.registry[ga] = target
            added.append({"uri": self._to_uri(target), "name": target.name})
            shown.append(f"{ga} ({_display(target, root)})")
        if added:
            logger.info(
                "jdtls: importing %d dependency module(s) (%d/%d used): %s",
                len(added),
                self._used,
                self.budget,
                ", ".join(shown),
            )
            await self._send_folders(added, [])
        new_unresolved = [ga for ga in overflow if ga not in self._unresolved]
        if new_unresolved:
            self._unresolved.extend(new_unresolved)
            listed = ", ".join(new_unresolved[:_WARN_LIST_MAX])
            more = f" (+{len(new_unresolved) - _WARN_LIST_MAX} more)" if len(new_unresolved) > _WARN_LIST_MAX else ""
            logger.warning(
                "jdtls: dependency-module budget exhausted (%d); these in-repo modules stay unresolved: %s%s. "
                "Raise %s (max %d) or open a file in their group.",
                self.budget,
                listed,
                more,
                ENV_BUDGET,
                MAX_BUDGET,
            )

    def take_covered_by(self, folder: Path) -> list[Folder]:
        """A group folder is being added: retire the dependency folders it covers.

        Returns them as ``removed`` entries for the same didChangeWorkspaceFolders event.
        """
        removed: list[Folder] = []
        for ga, target in list(self.registry.items()):
            if _is_under(target, folder):
                del self.registry[ga]
                self._retired.add(target)
                removed.append({"uri": self._to_uri(target), "name": target.name})
        for ga, target in list(self._pending.items()):
            if _is_under(target, folder):
                del self._pending[ga]
        if removed:
            logger.info("jdtls: group %s covers %d dependency module folder(s)", folder.name, len(removed))
        return removed

    def reset(self) -> None:
        """Forget the session (jdtls stopped)."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._flush_task = None
        self._indexes.clear()
        self._roots.clear()
        self.registry.clear()
        self._retired.clear()
        self._pending.clear()
        self._unresolved.clear()
        self._used = 0


class ClasspathRefresher:
    """Re-validate open files after their project's classpath changed (debounced per project)."""

    def __init__(
        self,
        open_uris: Callable[[], Iterable[str]],
        send_refresh: Callable[[str], Awaitable[Any]],
        *,
        debounce: float = REFRESH_DEBOUNCE_SEC,
    ) -> None:
        self._open_uris = open_uris
        self._send_refresh = send_refresh
        self.debounce = debounce
        self._timers: dict[Path, asyncio.Task[None]] = {}

    def note_updated(self, project_dir: Path) -> None:
        """The classpath of *project_dir* changed; refresh its open files after the debounce."""
        timer = self._timers.get(project_dir)
        if timer is not None and not timer.done():
            timer.cancel()
        try:
            self._timers[project_dir] = asyncio.get_running_loop().create_task(self._refresh_later(project_dir))
        except RuntimeError:
            return

    def open_files_under(self, project_dir: Path) -> list[str]:
        result: list[str] = []
        try:
            uris = list(self._open_uris())
        except Exception:
            return result
        for uri in uris:
            if not uri.endswith(".java"):
                continue
            path = path_from_uri(uri)
            if path is not None and path.is_relative_to(project_dir):
                result.append(uri)
        return result

    async def _refresh_later(self, project_dir: Path) -> None:
        await asyncio.sleep(self.debounce)
        if self._timers.get(project_dir) is asyncio.current_task():
            del self._timers[project_dir]
        uris = self.open_files_under(project_dir)
        if not uris:
            return
        logger.info("jdtls: classpath of %s updated, refreshing %d open file(s)", project_dir.name, len(uris))
        for uri in uris:
            await self._send_refresh(uri)

    def reset(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

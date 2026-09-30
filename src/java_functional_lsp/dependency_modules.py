"""Import missing in-repo Maven modules as jdtls workspace folders, only as far as needed (#110).

jdtls imports only the opened file's Maven group. A dependency on a reactor module in
another group is then resolved by m2e from the local repository at ``${revision}``, where it
is usually missing, and every symbol from it is "cannot be resolved". m2e reports that as an
Error diagnostic on the pom.xml files (``[Offline / ]Missing artifact g:a:type:version``).

v0.14.0 imported every in-repo GA any pom reported missing; each import surfaced the next
layer of the transitive closure, up to the 60-module budget (3 GB of jdtls heap and 30 s
timeouts on products). ``DependencyModules`` now imports in bounded **rounds** that run only
while an open file has **fresh demand**:

* **Demand**: an open ``.java`` file whose diagnostics hold #110-class errors (unresolved
  import/type, "undefined for the type", "hierarchy ... inconsistent", ...).
* **Fresh**: jdtls never re-publishes an open file after its classpath changes, so the
  round asks for it: after **build-idle** (``BuildIdle``) it sends
  ``java.project.refreshDiagnostics`` for up to 3 demand files, one at a time. Only the last
  publish for that file between the send and the command's response counts, and only when
  the document still has the content it had then.
* **Frontier**: the in-repo ``<dependencies>`` (plus those inherited from in-repo parent
  poms) of the demand modules and of every module imported so far, minus what is imported,
  covered by an imported group, or retired. Test-scope edges only for a demand file under
  ``src/test``, and only from the demand module.
* **Candidates**: the frontier ∩ m2e "Missing artifact" markers (proof that no installed jar
  resolves it), in declaration order with high fan-in "hub" modules last, at most
  ``per_round`` per round, sent as **one** didChangeWorkspaceFolders event.
* **STOP** (fixed reasons, logged and sent once to the client): ``clean``,
  ``no-candidates(none|markers-pending|owner-has-jar)``, ``budget``, ``rounds``,
  ``wall-clock``, ``refresh-cap``, ``jdtls-busy``, ``no-fresh-diagnostics``. Opening a file
  in a module that has not had demand before re-arms it, unless a session limit is
  exhausted. A phase that stops ``jdtls-busy`` / ``no-fresh-diagnostics`` before importing
  anything un-arms its modules, so their next demand publish tries again.

Everything is asyncio-only (no locks). Side effects are callables, and every wait is a
constructor parameter, so the state machine is unit-testable without jdtls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .reactor import Marker, ReactorIndex, build_reactor_index

logger = logging.getLogger(__name__)

ENV_BUDGET = "JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES"
ENV_ROUNDS = "JAVA_FUNCTIONAL_LSP_DEPENDENCY_ROUNDS"
DEFAULT_BUDGET = 30
MAX_BUDGET = 200
DEFAULT_ROUNDS = 6
MAX_ROUNDS = 20

#: Internal constants (v0.14.1 design): not user knobs.
PER_ROUND = 8
WALL_CLOCK_SEC = 300.0
REFRESHES_PER_ROUND = 3
#: Refreshes per session, per allowed round (the probe counts as one), retries included: the
#: default cap is ``REFRESHES_PER_ROUND * (rounds + 1) * 2``, so the rounds limit fires first.
REFRESH_RETRY_FACTOR = 2
IDLE_QUIET_SEC = 10.0
ROUND_TIMEOUT_SEC = 90.0
#: A busy jdtls-busy wait is retried (still waiting for build-idle) instead of stopping at once:
#: a 95-module/127-project group build routinely outlasts one 90 s round timeout (measured on
#: the products monorepo, v0.14.1 gate). Bounded so a build that never goes idle still stops.
BUSY_WAIT_BUDGET_SEC = 600.0
BUSY_WAIT_RETRIES = 6
REFRESH_TIMEOUT_SEC = 20.0
REFRESH_BACKOFF_SEC = 5.0
MARKER_WAIT_SEC = 30.0
HUB_FAN_IN = 25
#: Bounded demand scan: diagnostics per file, characters per message.
MAX_DIAGNOSTICS_SCANNED = 500
MAX_MESSAGE_CHARS = 1000

Folder = dict[str, str]
SendFolders = Callable[[list[Folder], list[Folder]], Awaitable[None]]
#: ``(uri, on_response) -> answered``; *on_response* runs synchronously when jdtls's reply is dispatched.
SendRefresh = Callable[[str, Callable[[], None]], Awaitable[bool]]

STOP_CLEAN = "clean"
STOP_NO_CANDIDATES_NONE = "no-candidates(none)"
STOP_NO_CANDIDATES_MARKERS = "no-candidates(markers-pending)"
STOP_NO_CANDIDATES_JAR = "no-candidates(owner-has-jar)"
STOP_BUDGET = "budget"
STOP_ROUNDS = "rounds"
STOP_WALL_CLOCK = "wall-clock"
STOP_BUSY = "jdtls-busy"
STOP_NO_FRESH = "no-fresh-diagnostics"
STOP_REFRESHES = "refresh-cap"
STOP_REASONS = (
    STOP_CLEAN,
    STOP_NO_CANDIDATES_NONE,
    STOP_NO_CANDIDATES_MARKERS,
    STOP_NO_CANDIDATES_JAR,
    STOP_BUDGET,
    STOP_ROUNDS,
    STOP_WALL_CLOCK,
    STOP_BUSY,
    STOP_NO_FRESH,
    STOP_REFRESHES,
)
#: Session limits: once hit, later demand stops at once with the same reason.
_TERMINAL = frozenset({STOP_BUDGET, STOP_ROUNDS, STOP_WALL_CLOCK, STOP_REFRESHES})
#: Stops before any import that un-arm the phase's modules (the next demand tries again).
_RETRYABLE = frozenset({STOP_BUSY, STOP_NO_FRESH})
_WARN_STOPS = frozenset({STOP_BUSY, STOP_NO_FRESH})
_STOP_HINTS = {
    STOP_NO_CANDIDATES_JAR: "an installed jar may be stale; see the README (troubleshooting)",
    STOP_BUDGET: f"raise {ENV_BUDGET} (max {MAX_BUDGET}) or open a file in the missing module's group",
    STOP_ROUNDS: f"raise {ENV_ROUNDS} (max {MAX_ROUNDS})",
    STOP_BUSY: "jdtls stayed busy; errors may clear later",
    STOP_NO_FRESH: "jdtls did not re-validate the open file",
    STOP_REFRESHES: "the session's diagnostics-refresh cap is spent; restart the server to try again",
}

# --- Demand classification (#110-class errors) -------------------------------------------

#: jdtls problem ids (Eclipse ``IProblem``) of #110-class errors: ImportNotFound, UndefinedType,
#: UndefinedMethod, UndefinedField, UndefinedName, IsClassPathCorrect.
_DEMAND_PROBLEM_IDS = frozenset({"268435846", "16777218", "67108964", "33554502", "570425394", "16777540"})
#: Never demand: m2e lifecycle / build-path markers and jdtls's "non-project file".
_NOT_DEMAND_IDS = frozenset({"0", "16", "964"})
_DEMAND_SUBSTRINGS = (
    "cannot be resolved",
    "is undefined for the type",
    "indirectly referenced from required",
    "must override or implement a supertype method",
)


def is_demand_diagnostic(diag: object) -> bool:
    """True for an Error diagnostic that an unimported in-repo dependency can cause."""
    if not isinstance(diag, dict) or diag.get("severity") != 1:
        return False
    code = str(diag.get("code", ""))
    if code in _NOT_DEMAND_IDS:
        return False
    if code in _DEMAND_PROBLEM_IDS:
        return True
    message = diag.get("message")
    if not isinstance(message, str):
        return False
    message = message[:MAX_MESSAGE_CHARS]
    if any(s in message for s in _DEMAND_SUBSTRINGS):
        return True
    return "The hierarchy of the type" in message and "is inconsistent" in message


def has_demand(diagnostics: object) -> bool:
    """Whether a jdtls publish for a ``.java`` file holds #110-class errors (bounded scan)."""
    if not isinstance(diagnostics, list):
        return False
    return any(is_demand_diagnostic(d) for d in diagnostics[:MAX_DIAGNOSTICS_SCANNED])


# --- Knobs -----------------------------------------------------------------------------------


def _parse_int(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_knob(env_name: str, default: int, hard_max: int, raw: str) -> int:
    value = _parse_int(raw)
    if value is None:
        logger.warning("jdtls: invalid %s %r, using default %d", env_name, raw, default)
        return default
    if value > hard_max:
        logger.warning("jdtls: %s %d is above the maximum, using %d", env_name, value, hard_max)
        return hard_max
    return max(value, 0)


def _config_knob(config_key: str, env_name: str, default: int, raw: object) -> int:
    value = _parse_int(raw)
    if value is None:
        # Repository-controlled: cap what reaches the log.
        logger.warning("jdtls: invalid jdtls.%s %s, using default %d", config_key, repr(raw)[:80], default)
        return default
    if value > default:
        logger.warning(
            "jdtls: jdtls.%s %d ignored: repository config can only lower it (default %d); set %s to raise it",
            config_key,
            value,
            default,
            env_name,
        )
        return default
    return max(value, 0)


def _resolve_knob(
    env_name: str,
    config_key: str,
    default: int,
    hard_max: int,
    config: Mapping[str, Any] | None,
    environ: Mapping[str, str] | None,
) -> int:
    """Repo config may only lower *default*; the environment may set anything up to *hard_max*."""
    env = environ if environ is not None else os.environ
    raw_env = env.get(env_name)
    if raw_env:
        return _env_knob(env_name, default, hard_max, raw_env)
    jdtls_cfg = (config or {}).get("jdtls")
    raw_cfg = jdtls_cfg.get(config_key) if isinstance(jdtls_cfg, Mapping) else None
    return default if raw_cfg is None else _config_knob(config_key, env_name, default, raw_cfg)


def resolve_budget(config: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Session budget of dependency-module folders (0 disables the feature).

    ``{"jdtls": {"dependencyModules": N}}`` in ``.java-functional-lsp.json`` can only lower the
    default; ``JAVA_FUNCTIONAL_LSP_DEPENDENCY_MODULES`` wins and may raise it up to ``MAX_BUDGET``.
    """
    return _resolve_knob(ENV_BUDGET, "dependencyModules", DEFAULT_BUDGET, MAX_BUDGET, config, environ)


def resolve_rounds(config: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Rounds per session: ``jdtls.dependencyRounds`` can only lower it, ``JAVA_FUNCTIONAL_LSP_DEPENDENCY_ROUNDS``
    may raise it up to ``MAX_ROUNDS``."""
    return _resolve_knob(ENV_ROUNDS, "dependencyRounds", DEFAULT_ROUNDS, MAX_ROUNDS, config, environ)


# --- Paths -----------------------------------------------------------------------------------


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


def _real(path: Path) -> Path:
    """*path* with symlinks resolved, or unchanged when it cannot be resolved.

    Registry entries come from the reactor index, which is always resolved; folders the
    proxy imported are spelled as the client sent them. Both sides are compared resolved,
    so a client root reached through a symlink still matches (#110).
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return path.name


def _nearest_pom_dir(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / "pom.xml").is_file():
            return parent
    return None


def _is_test_source(path: Path) -> bool:
    parts = path.parts
    return any(parts[i] == "src" and parts[i + 1] == "test" for i in range(len(parts) - 1))


# --- Build-idle ------------------------------------------------------------------------------


def _is_indexing_task(task: object) -> bool:
    if not isinstance(task, str):
        return False
    lowered = task.lower()
    return lowered.startswith("searching") or "index" in lowered


class BuildIdle:
    """jdtls build-idle detector fed by ``language/progressReport`` (``progressReportProvider``).

    Idle = no open (non-indexing) progress id and no non-indexing progress event for *quiet*
    seconds (counted from *since* too, when given). "Searching..."/indexing tasks never block
    idle. One timer: every relevant event re-arms the same ``call_later`` handle.
    """

    #: Bound on open progress ids (the oldest is dropped; any open id keeps jdtls busy).
    MAX_OPEN = 256

    def __init__(self, quiet: float = IDLE_QUIET_SEC, clock: Callable[[], float] = time.monotonic) -> None:
        self.quiet = quiet
        self._clock = clock
        self._open: dict[str, str] = {}
        self._last = clock()
        self._timer: asyncio.TimerHandle | None = None
        self._changed: asyncio.Event | None = None

    @property
    def open_tasks(self) -> int:
        return len(self._open)

    def note_progress(self, params: object) -> None:
        if not isinstance(params, dict):
            return
        task_id = params.get("id")
        if not isinstance(task_id, str):
            return
        complete = bool(params.get("complete"))
        if _is_indexing_task(params.get("task")):
            self._open.pop(task_id, None)
            return
        self._last = self._clock()
        if complete:
            self._open.pop(task_id, None)
        else:
            if task_id not in self._open and len(self._open) >= self.MAX_OPEN:
                self._open.pop(next(iter(self._open)))
            self._open[task_id] = str(params.get("task"))[:100]
        self._arm()

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = loop.call_later(self.quiet, self._fire)
        self._wake()

    def _fire(self) -> None:
        self._timer = None
        self._wake()

    def _wake(self) -> None:
        if self._changed is not None:
            self._changed.set()

    def is_idle(self, since: float | None = None) -> bool:
        ref = self._last if since is None else max(self._last, since)
        return not self._open and self._clock() - ref >= self.quiet

    async def wait(self, since: float | None, timeout: float) -> bool:
        """True once idle (see class doc), False when *timeout* passes first."""
        deadline = self._clock() + timeout
        if self._changed is None:
            self._changed = asyncio.Event()
        while not self.is_idle(since):
            now = self._clock()
            if now >= deadline:
                return False
            step = deadline - now
            if not self._open:  # otherwise only a progress event (which wakes us) can end the wait
                ref = self._last if since is None else max(self._last, since)
                step = min(step, self.quiet - (now - ref))
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=max(step, 0.001))
            except asyncio.TimeoutError:
                pass
        return True

    def reset(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._open.clear()
        self._last = self._clock()


# --- Round controller ------------------------------------------------------------------------


@dataclass
class Limits:
    budget: int = DEFAULT_BUDGET
    rounds: int = DEFAULT_ROUNDS
    per_round: int = PER_ROUND
    wall_clock: float = WALL_CLOCK_SEC
    refreshes_per_round: int = REFRESHES_PER_ROUND
    #: None: ``refreshes_per_round * (rounds + 1) * REFRESH_RETRY_FACTOR``.
    refreshes_per_session: int | None = None
    hub_fan_in: int = HUB_FAN_IN

    @property
    def refresh_cap(self) -> int:
        if self.refreshes_per_session is not None:
            return self.refreshes_per_session
        return self.refreshes_per_round * (self.rounds + 1) * REFRESH_RETRY_FACTOR


@dataclass
class Timing:
    idle_quiet: float = IDLE_QUIET_SEC
    round_timeout: float = ROUND_TIMEOUT_SEC
    busy_wait_budget: float = BUSY_WAIT_BUDGET_SEC
    busy_wait_retries: int = BUSY_WAIT_RETRIES
    refresh_backoff: float = REFRESH_BACKOFF_SEC
    marker_wait: float = MARKER_WAIT_SEC


@dataclass(frozen=True)
class _Fresh:
    demand: bool
    digest: str | None


@dataclass
class _Session:
    rounds: int = 0
    refreshes: int = 0
    #: Import time of finished phases (round-1 import to STOP, minus refreshes), all re-arms.
    import_time: float = 0.0
    #: The current phase: when its round-1 import was sent, and refresh time since then.
    phase_start: float | None = None
    phase_refresh: float = 0.0
    #: Time spent in refreshes, whole session (reported; not import time).
    refresh_time: float = 0.0
    terminal: str | None = None
    last_stop: str | None = None
    stops: int = 0
    record: list[dict[str, str]] = field(default_factory=list)


class DependencyModules:
    """Session registry and demand-driven importer of dependency-module folders."""

    def __init__(
        self,
        send_folders: SendFolders,
        covered_roots: Callable[[], Iterable[Path]],
        reactor_root_for: Callable[[Path], Path],
        to_uri: Callable[[Path], str],
        *,
        send_refresh: SendRefresh | None = None,
        open_uris: Callable[[], Iterable[str]] = lambda: (),
        uri_key: Callable[[str], str] = lambda uri: uri,
        doc_digest: Callable[[str], str | None] = lambda _uri: None,
        module_of: Callable[[Path], Path | None] = _nearest_pom_dir,
        notify: Callable[[str], None] = lambda _msg: None,
        limits: Limits | None = None,
        timing: Timing | None = None,
        index_builder: Callable[[Path], ReactorIndex] = build_reactor_index,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send_folders = send_folders
        self._covered_roots = covered_roots
        self._reactor_root_for = reactor_root_for
        self._to_uri = to_uri
        self._send_refresh = send_refresh
        self._open_uris = open_uris
        self._uri_key = uri_key
        self._doc_digest = doc_digest
        self._module_of = module_of
        self._notify = notify
        self._index_builder = index_builder
        self._clock = clock
        self.limits = limits or Limits()
        self.timing = timing or Timing()
        self.idle = BuildIdle(self.timing.idle_quiet, clock)
        self._indexes: dict[Path, asyncio.Task[ReactorIndex | None]] = {}
        self._roots: dict[Path, Path] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        #: GA → module dir, folders currently added by this class.
        self.registry: dict[str, Path] = {}
        #: Folders removed because a group expansion covered them; never re-added.
        self._retired: set[Path] = set()
        #: Resolved module dir → latest m2e "Missing artifact" markers on its pom.
        self._markers: dict[Path, frozenset[Marker]] = {}
        #: Resolved module dir → number of pom publishes seen.
        self._pom_publishes: dict[Path, int] = {}
        #: Modules that had demand this session (a new one re-arms the controller).
        self._armed: set[Path] = set()
        self._pending_modules: list[Path] = []
        #: uri_key → the latest (not necessarily fresh) demand classification of an open file.
        self._latest: dict[str, bool] = {}
        #: Open refresh windows: uri_key → last publish in the window (None until one arrives).
        self._window: dict[str, _Fresh | None] = {}
        self._closed: dict[str, _Fresh | None] = {}
        self._window_uri: dict[str, str] = {}
        self._runner: asyncio.Task[None] | None = None
        self._record_path: Path | None = None
        self._session = _Session()

    # --- public state ---

    @property
    def budget(self) -> int:
        return self.limits.budget

    @budget.setter
    def budget(self, value: int) -> None:
        self.limits.budget = value

    @property
    def enabled(self) -> bool:
        return self.limits.budget > 0

    @property
    def used(self) -> int:
        return len(self._session.record)

    @property
    def rounds(self) -> int:
        return self._session.rounds

    @property
    def last_stop(self) -> str | None:
        return self._session.last_stop

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def folders(self) -> list[Path]:
        return list(self.registry.values())

    # --- inputs from the proxy ---

    def begin_session(self, record_path: Path | None) -> None:
        """jdtls (re)started on a data dir: the record of imported modules starts empty.

        jdtls 1.61 does not keep folders added through didChangeWorkspaceFolders across a
        restart (verified: only the rootUri project is listed again), so a previous record is
        only reported, never counted.
        """
        self._record_path = record_path
        if record_path is None:
            return
        try:
            previous = json.loads(record_path.read_text()).get("modules", [])
            if previous:
                logger.info(
                    "jdtls: previous session imported %d dependency module(s); jdtls does not keep them, "
                    "starting from 0",
                    len(previous),
                )
        except (OSError, ValueError, AttributeError):
            pass
        self._save_record()

    def note_pom(self, pom_path: Path, markers: Iterable[Marker]) -> None:
        """Every pom.xml publish (changed or not): the latest markers, and one more arrival."""
        module = _real(pom_path.parent)
        self._pom_publishes[module] = self._pom_publishes.get(module, 0) + 1
        found = frozenset(markers)
        if found:
            self._markers[module] = found
        else:
            self._markers.pop(module, None)

    def note_publish(self, uri: str, diagnostics: object) -> None:
        """A jdtls publish for a ``.java`` file."""
        if not self.enabled or not isinstance(uri, str):
            return
        key = self._uri_key(uri)
        in_window = key in self._window
        # jdtls publishes for every file of the workspace: classify only open (or refreshed) ones.
        if not in_window and key not in self._open_keys():
            return
        demand = has_demand(diagnostics)
        if in_window:
            # Digest of the document now, looked up under the client's spelling of the URI.
            self._window[key] = _Fresh(demand, self._safe_digest(self._window_uri.get(key, uri)))
        if key not in self._open_keys():
            return
        self._latest[key] = demand
        if not demand:
            return
        path = path_from_uri(uri)
        module = self._module_of(path) if path is not None else None
        if module is None:
            return
        module = _real(module)
        if module in self._armed:
            return
        self._armed.add(module)
        self._pending_modules.append(module)
        if not self._kick():  # nothing scheduled (no running loop): do not keep it armed
            self._armed.discard(module)
            self._pending_modules.remove(module)

    # --- helpers ---

    def _safe_digest(self, uri: str) -> str | None:
        try:
            return self._doc_digest(uri)
        except Exception:
            return None

    def _open_list(self) -> list[str]:
        try:
            return [u for u in self._open_uris() if isinstance(u, str) and u.endswith(".java")]
        except Exception:
            return []

    def _open_keys(self) -> set[str]:
        return {self._uri_key(u) for u in self._open_list()}

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        try:
            task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no running loop (sync unit test): nothing to schedule
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _kick(self) -> bool:
        """Make sure a runner will pick up ``_pending_modules``; False when none can be scheduled."""
        if self.running:
            return True
        self._runner = self._spawn(self._run())
        return self._runner is not None

    async def _index_for(self, module_dir: Path) -> ReactorIndex | None:
        """Reactor index for *module_dir*, built once per reactor root (single flight)."""
        root = self._root_of(module_dir)
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
        module_dir = _real(module_dir)
        if any(_is_under(module_dir, folder) for folder in self.registry.values()):
            return True
        return any(_is_under(module_dir, _real(folder)) for folder in self._covered_roots())

    def _save_record(self) -> None:
        if self._record_path is None:
            return
        tmp: str | None = None
        try:
            # A fresh, exclusively created temp file (never a predictable, followable name).
            with tempfile.NamedTemporaryFile(
                "w", dir=self._record_path.parent, prefix=self._record_path.name + ".", suffix=".tmp", delete=False
            ) as f:
                tmp = f.name
                f.write(json.dumps({"schema": 1, "modules": self._session.record}))
            os.replace(tmp, self._record_path)
            tmp = None
        except OSError as e:
            logger.debug("jdtls: could not write the dependency-module record: %s", e)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _demand_files(self, modules: Iterable[Path] | None) -> list[str]:
        """Open files with (latest) demand, most recently opened first, at most the per-round cap."""
        wanted = set(modules) if modules is not None else None
        result: list[str] = []
        for uri in reversed(self._open_list()):
            key = self._uri_key(uri)
            if not self._latest.get(key) or any(self._uri_key(r) == key for r in result):
                continue
            if wanted is not None:
                path = path_from_uri(uri)
                module = self._module_of(path) if path is not None else None
                if module is None or _real(module) not in wanted:
                    continue
            result.append(uri)
            if len(result) >= self.limits.refreshes_per_round:
                break
        return result

    # --- refresh (the fresh-demand gate) ---

    async def _refresh(self, uris: list[str]) -> dict[str, tuple[str, bool]] | None:
        """Refresh *uris* one at a time. uri_key → (uri, fresh demand), or None when jdtls did not
        re-validate a file (after one retry) or the session's refresh cap is reached (then the
        session is terminal: ``STOP_REFRESHES``)."""
        result: dict[str, tuple[str, bool]] = {}
        if self._send_refresh is None:
            return None
        for uri in uris:
            fresh = await self._refresh_one(uri)
            if fresh is None:
                return None
            result[self._uri_key(uri)] = (uri, fresh)
        return result

    async def _refresh_one(self, uri: str) -> bool | None:
        assert self._send_refresh is not None
        key = self._uri_key(uri)
        for attempt in range(2):
            if self._session.refreshes >= self.limits.refresh_cap:
                logger.info("jdtls: dependency refresh cap (%d per session) reached", self.limits.refresh_cap)
                self._session.terminal = STOP_REFRESHES
                return None
            self._session.refreshes += 1
            self._window[key] = None
            self._window_uri[key] = uri
            self._closed.pop(key, None)

            def close(key: str = key) -> None:
                if key in self._window:
                    self._closed[key] = self._window.pop(key)

            started = self._clock()
            try:
                answered = await self._send_refresh(uri, close)
            finally:
                self._window.pop(key, None)
                self._window_uri.pop(key, None)
                took = self._clock() - started
                self._session.refresh_time += took
                if self._session.phase_start is not None:
                    self._session.phase_refresh += took
            seen = self._closed.pop(key, None)
            if answered and seen is not None and seen.digest is not None and seen.digest == self._safe_digest(uri):
                self._latest[key] = seen.demand
                return seen.demand
            if attempt == 0:
                logger.debug("jdtls: refresh gave no fresh diagnostics (answered=%s); retrying", answered)
                await asyncio.sleep(self.timing.refresh_backoff)
        return None

    # --- the round loop ---

    async def _run(self) -> None:
        # A module armed while a phase runs is only queued (the runner exists): this loop
        # gives it its own phase after the current one.
        while self._pending_modules:
            new = self._take_phase()
            try:
                reason = await self._process(new)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("jdtls: dependency-module import failed", exc_info=True)
                self._close_phase()
                self._armed.difference_update(new)  # the next demand publish tries again
                continue
            imported = self._session.phase_start is not None
            self._close_phase()
            if reason in _RETRYABLE and not imported:
                self._armed.difference_update(new)
            self._stop(reason)

    def _take_phase(self) -> list[Path]:
        """The pending modules of one reactor root (the first pending one's); the rest stay queued."""
        first = self._pending_modules[0]
        try:
            root = self._root_of(first)
            phase = [m for m in self._pending_modules if self._root_of(m) == root]
        except Exception:
            logger.debug("jdtls: reactor root lookup failed", exc_info=True)
            phase = [first]
        self._pending_modules = [m for m in self._pending_modules if m not in phase]
        return phase

    def _root_of(self, module_dir: Path) -> Path:
        root = self._roots.get(module_dir)
        if root is None:
            root = self._reactor_root_for(module_dir)
            self._roots[module_dir] = root
        return root

    async def _wait_idle(self, since: float | None) -> str | None:
        """Wait for build-idle, retrying within a busy budget so a build that outlasts one round
        timeout is not a dead end (#DEV-v0.14.2, large module groups on products).

        Each attempt waits up to ``round_timeout`` for :meth:`BuildIdle.wait`. Between attempts
        this re-checks the session's existing limits (budget/rounds/wall-clock): if one has
        already been reached, that is latched onto ``self._session.terminal`` (mirroring
        :meth:`_select`) and returned immediately, so the caller reports and stops on the real
        reason instead of a generic busy timeout, and later demand for the same module short-
        circuits via the ``terminal`` check at the top of :meth:`_process` rather than retrying
        forever. Only once the busy budget (``timing.busy_wait_budget``) or the retry count is
        exhausted does this give up on idle itself, returning the final ``jdtls-busy``. Returns
        ``None`` when idle was reached.
        """
        deadline = self._clock() + self.timing.busy_wait_budget
        attempt = 0
        while True:
            if await self.idle.wait(since, self.timing.round_timeout):
                return None
            attempt += 1
            # A terminal session limit outranks an ordinary busy timeout, even on what would
            # otherwise be the final attempt: it must latch and report the real reason (not a
            # retryable jdtls-busy) so a re-arm does not busy-wait through the same limit again.
            limit = self._limit_reason()
            if limit is not None:
                self._session.terminal = limit
                return limit
            if attempt >= self.timing.busy_wait_retries or self._clock() >= deadline:
                return STOP_BUSY
            logger.info(
                "jdtls: still busy after %.0fs, waiting for build-idle (attempt %d/%d, up to %.0fs more)",
                attempt * self.timing.round_timeout,
                attempt,
                self.timing.busy_wait_retries,
                max(deadline - self._clock(), 0.0),
            )

    async def _process(self, new_modules: list[Path]) -> str:
        """Probe the newly armed modules (one reactor root), then run rounds until a STOP reason."""
        if self._session.terminal is not None:
            return self._session.terminal
        armed_at = self._clock()
        idle_stop = await self._wait_idle(None)
        if idle_stop is not None:
            return idle_stop
        fresh = await self._refresh(self._demand_files(new_modules))
        if fresh is None:
            return self._session.terminal or STOP_NO_FRESH
        demand = [uri for uri, d in fresh.values() if d]
        waiting: dict[Path, int] = dict.fromkeys(new_modules, 0)
        wait_start = armed_at
        while demand:
            selected = await self._select(demand, waiting, wait_start)
            if isinstance(selected, str):
                return selected
            outcome = await self._round(demand, *selected)
            if isinstance(outcome, str):
                return outcome
            demand, waiting, wait_start = outcome
        return STOP_CLEAN

    async def _select(
        self, demand: list[str], waiting: dict[Path, int], wait_start: float
    ) -> str | tuple[list[tuple[str, Path]], int, ReactorIndex | None]:
        """This round's candidates (after the marker wait), or the STOP reason."""
        pending = await self._wait_markers(waiting, wait_start)
        candidates, frontier, index = await self._candidates(demand)
        if not candidates and frontier and not pending:
            # Fresh demand, a non-empty frontier, no marker yet: one more quiet window.
            await asyncio.sleep(self.timing.idle_quiet)
            candidates, frontier, index = await self._candidates(demand)
        if not candidates:
            if not frontier:
                return STOP_NO_CANDIDATES_NONE
            return STOP_NO_CANDIDATES_MARKERS if pending else STOP_NO_CANDIDATES_JAR
        limit = self._limit_reason()
        if limit is not None:
            self._session.terminal = limit
            return limit
        return candidates, frontier, index

    async def _round(
        self, demand: list[str], candidates: list[tuple[str, Path]], frontier: int, index: ReactorIndex | None
    ) -> str | tuple[list[str], dict[Path, int], float]:
        """Import one batch, wait for build-idle, refresh: the next round's inputs, or the STOP reason."""
        batch = candidates[: min(self.limits.per_round, self.limits.budget - self.used)]
        waiting = {target: self._pom_publishes.get(target, 0) for _, target in batch}
        await self._import(batch)
        self._session.rounds += 1
        sent_at = self._clock()
        if self._session.phase_start is None:
            self._session.phase_start = sent_at

        def log(refresh: str, files: list[str]) -> None:
            self._log_round(files, frontier, candidates, batch, index, self._clock() - sent_at, refresh)

        idle_stop = await self._wait_idle(sent_at)
        if idle_stop is not None:
            log(f"skipped ({idle_stop})", demand)
            return idle_stop
        # The files with demand in this round (already most recent first), still open: an
        # unsolicited publish in between never decides the round.
        open_keys = self._open_keys()
        files = [uri for uri in demand if self._uri_key(uri) in open_keys][: self.limits.refreshes_per_round]
        fresh = await self._refresh(files)
        if fresh is None:
            log("failed", demand)
            return self._session.terminal or STOP_NO_FRESH
        still = [uri for uri, d in fresh.values() if d]
        log("errors" if still else "clean", still)
        return still, waiting, sent_at

    def _limit_reason(self) -> str | None:
        if self.used >= self.limits.budget:
            return STOP_BUDGET
        if self._session.rounds >= self.limits.rounds:
            return STOP_ROUNDS
        # Import time summed over phases (round-1 import to STOP, minus refreshes); see _import_time.
        if self._session.rounds and self._import_time() >= self.limits.wall_clock:
            return STOP_WALL_CLOCK
        return None

    async def _wait_markers(self, waiting: dict[Path, int], since: float) -> bool:
        """Wait until every module in *waiting* had a pom publish after its recorded count, or
        ``marker_wait`` seconds after *since*. True when some are still pending."""

        def pending() -> bool:
            return any(self._pom_publishes.get(m, 0) <= seen for m, seen in waiting.items())

        while pending():
            remaining = since + self.timing.marker_wait - self._clock()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, 0.25))
        return False

    async def _candidates(self, demand_uris: list[str]) -> tuple[list[tuple[str, Path]], int, ReactorIndex | None]:
        """(ordered candidates as (GA, dir), frontier size, the index) for the demand files."""
        demand_modules: dict[Path, bool] = {}
        for uri in demand_uris:
            path = path_from_uri(uri)
            if path is None:
                continue
            module = self._module_of(path)
            if module is not None:
                module = _real(module)
                demand_modules[module] = demand_modules.get(module, False) or _is_test_source(path)
        if not demand_modules:
            return [], 0, None
        # One phase is one reactor root (_take_phase), so the first module's index covers all.
        index = await self._index_for(next(iter(demand_modules)))
        if index is None:
            return [], 0, None
        frontier: list[tuple[str, bool]] = []
        seen: set[tuple[str, bool]] = set()
        sources: list[tuple[Path, bool]] = [*demand_modules.items(), *((d, False) for d in self.registry.values())]
        for module_dir, test in sources:
            for dep in index.edges(module_dir, test=test):
                key = (dep.ga, dep.test_jar)
                if key in seen:
                    continue
                seen.add(key)
                target = index.get(dep.ga)
                if target is None or dep.ga in self.registry or target in self._retired or self._is_covered(target):
                    continue
                frontier.append(key)
        markers = set().union(*self._markers.values()) if self._markers else set()
        chosen: list[tuple[str, Path]] = []
        for ga, test_jar in frontier:
            target = index.get(ga)
            if target is not None and Marker(ga, test_jar) in markers and all(ga != g for g, _ in chosen):
                chosen.append((ga, target))
        chosen.sort(key=lambda c: index.is_hub(c[0], self.limits.hub_fan_in))  # stable: hubs last
        return chosen, len(frontier), index

    async def _import(self, batch: list[tuple[str, Path]]) -> None:
        added: list[Folder] = []
        for ga, target in batch:
            self.registry[ga] = target
            self._session.record.append({"ga": ga, "path": str(target)})
            added.append({"uri": self._to_uri(target), "name": target.name})
        self._save_record()
        if added:
            await self._send_folders(added, [])

    def _log_round(
        self,
        demand: list[str],
        frontier: int,
        candidates: list[tuple[str, Path]],
        batch: list[tuple[str, Path]],
        index: ReactorIndex | None,
        elapsed: float,
        refresh: str,
    ) -> None:
        root = index.root if index is not None else Path("/")
        shown = ", ".join(f"{ga} ({_display(target, root)})" for ga, target in batch)
        logger.info(
            "jdtls: dependency round %d: imported %d dependency module(s) (%d/%d used): %s -> build idle "
            "(round took %.1fs) -> refresh %s, demand left %d file(s) (frontier %d, candidates %d, deferred %d)",
            self._session.rounds,
            len(batch),
            self.used,
            self.limits.budget,
            shown,
            elapsed,
            refresh,
            len(demand),
            frontier,
            len(candidates),
            len(candidates) - len(batch),
        )

    def _import_time(self) -> float:
        """Session import time: finished phases plus the current one (idle gaps between phases excluded)."""
        session = self._session
        current = 0.0
        if session.phase_start is not None:
            current = max(self._clock() - session.phase_start - session.phase_refresh, 0.0)
        return session.import_time + current

    def _close_phase(self) -> None:
        session = self._session
        session.import_time = self._import_time()
        session.phase_start = None
        session.phase_refresh = 0.0

    def _stop(self, reason: str) -> None:
        session = self._session
        # A repeated session limit, or the same jdtls-busy / no-fresh warning again: log at DEBUG only.
        repeated = reason == session.last_stop and (reason in _TERMINAL or reason in _WARN_STOPS)
        session.last_stop = reason
        session.stops += 1
        import_sec = self._import_time()
        text = (
            f"dependency-module import stopped ({reason}): rounds {session.rounds}/{self.limits.rounds}, "
            f"imported {self.used}/{self.limits.budget}, import time {import_sec:.0f}s, "
            f"refreshes {session.refreshes} ({session.refresh_time:.0f}s)"
        )
        hint = _STOP_HINTS.get(reason)
        if hint:
            text += f"; {hint}"
        level = logging.WARNING if reason in _WARN_STOPS else logging.INFO
        logger.log(logging.DEBUG if repeated else level, "jdtls: %s", text)
        if not repeated:
            try:
                self._notify(f"java-functional-lsp: {text}")
            except Exception:
                logger.debug("jdtls: could not notify the client", exc_info=True)

    def log_summary(self) -> None:
        """Session-end summary (jdtls stopping)."""
        session = self._session
        if not session.rounds and not session.stops:
            return
        logger.info(
            "jdtls: dependency-module session: rounds %d, imported %d/%d, refreshes %d (%.1fs), stops %d, last stop %s",
            session.rounds,
            self.used,
            self.limits.budget,
            session.refreshes,
            session.refresh_time,
            session.stops,
            session.last_stop or "-",
        )

    # --- group expansion / lifecycle ---

    def take_covered_by(self, folder: Path) -> list[Folder]:
        """A group folder is being added: retire the dependency folders it covers.

        Returns them as ``removed`` entries for the same didChangeWorkspaceFolders event.
        """
        folder = _real(folder)
        removed: list[Folder] = []
        for ga, target in list(self.registry.items()):
            if _is_under(target, folder):
                del self.registry[ga]
                self._retired.add(target)
                removed.append({"uri": self._to_uri(target), "name": target.name})
        if removed:
            logger.info("jdtls: group %s covers %d dependency module folder(s)", folder.name, len(removed))
        return removed

    def reset(self) -> None:
        """Forget the session (jdtls stopped)."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._runner = None
        self._indexes.clear()
        self._roots.clear()
        self.registry.clear()
        self._retired.clear()
        self._markers.clear()
        self._pom_publishes.clear()
        self._armed.clear()
        self._pending_modules.clear()
        self._latest.clear()
        self._window.clear()
        self._closed.clear()
        self._window_uri.clear()
        self.idle.reset()
        self._session = _Session()

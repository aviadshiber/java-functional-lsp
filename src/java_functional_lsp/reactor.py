"""Maven reactor index and m2e "Missing artifact" parsing (#110).

Pure functions, no jdtls. ``build_reactor_index`` maps ``groupId:artifactId`` to the
module directory of every module reachable from a reactor root through ``<modules>``
(all profiles unioned), and records each module's declared ``<dependencies>`` and its
parent. ``ReactorIndex.edges`` turns those into the in-repo dependency edges of a module,
including the ones it inherits from in-repo parent poms. The proxy intersects the edges
with m2e's pom.xml diagnostic ``Missing artifact g:a:type[:classifier]:version`` to
decide which in-repo modules to import as workspace folders.

The poms come from the user's checkout, so parsing is defensive: files over
``MAX_POM_BYTES`` or containing a ``<!DOCTYPE``/``<!ENTITY`` declaration are
rejected before ElementTree sees them (no entity expansion): a byte scan catches the
ASCII-compatible spellings, and an expat pass that refuses any doctype or entity
declaration catches the rest (e.g. a UTF-16 pom). Symlinked poms are skipped,
every module must resolve inside the reactor root, and the walk is bounded by a
pom count and a wall-clock budget. Coordinates outside Maven's usual character set
are ignored, so nothing from a pom reaches the log unfiltered.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

logger = logging.getLogger(__name__)

MAX_POM_BYTES = 1024 * 1024
MAX_POMS = 5000
MAX_SECONDS = 10.0

_FORBIDDEN_MARKUP = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_COORDINATE = r"[A-Za-z0-9_.\-]+"
_COORDINATE_RE = re.compile(_COORDINATE)
#: m2e: "[<prefix> / ]Missing artifact g:a:type[:classifier]:version". ``${revision}`` is
#: already interpolated.
_MISSING_ARTIFACT_RE = re.compile(rf"(?:^|/\s*)Missing artifact ({_COORDINATE}):({_COORDINATE}):(\S+)")
#: Scopes whose dependencies are on a module's main classpath.
MAIN_SCOPES = frozenset({"compile", "provided", "runtime", "system"})
#: Bound on the in-repo parent chain walked for inherited dependencies.
MAX_PARENT_DEPTH = 20
#: Bound on the dependencies kept per pom (a hostile pom cannot grow the index unboundedly).
MAX_DEPENDENCIES_PER_POM = 2000
_OWN_GROUP_PLACEHOLDERS = frozenset({"${project.groupId}", "${pom.groupId}", "${groupId}"})
_PARENT_GROUP_PLACEHOLDERS = frozenset({"${project.parent.groupId}", "${parent.groupId}"})


@dataclass(frozen=True)
class Dependency:
    """One declared ``<dependency>``: its GA, scope, and whether it is a test-jar."""

    ga: str
    scope: str = "compile"
    test_jar: bool = False


@dataclass(frozen=True)
class Marker:
    """One m2e "Missing artifact" marker: the GA and whether it names a test-jar."""

    ga: str
    test_jar: bool = False


class _ForbiddenDeclarationError(Exception):
    pass


def _forbid(*_args: object) -> None:
    raise _ForbiddenDeclarationError


def _declares_dtd(data: bytes) -> bool:
    """True when expat meets a doctype or entity declaration in *data*, in any encoding.

    Only declarations are rejected here; a malformed document is left to ElementTree.
    """
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    try:
        parser.Parse(data, True)
    except _ForbiddenDeclarationError:
        return True
    except (expat.ExpatError, ValueError):  # ValueError: an encoding expat cannot decode
        return False
    return False


@dataclass
class ReactorIndex:
    """``groupId:artifactId`` → module directory for one reactor root."""

    root: Path
    modules: dict[str, Path] = field(default_factory=dict)
    #: GAs declared by more than one module directory (never resolved).
    ambiguous: set[str] = field(default_factory=set)
    #: GAs of aggregator poms (with ``<modules>``). Never resolved: importing one would pull
    #: in its whole subtree (a group, or the entire reactor) as a single folder.
    aggregators: set[str] = field(default_factory=set)
    poms_read: int = 0
    #: True when the walk stopped early at the pom-count or time bound.
    truncated: bool = False
    #: Module directory → its declared ``<dependencies>`` (``dependencyManagement`` ignored).
    dependencies: dict[Path, tuple[Dependency, ...]] = field(default_factory=dict)
    #: Module directory → the GA of its ``<parent>``.
    parent_of: dict[Path, str] = field(default_factory=dict)
    #: Unambiguous GA → directory of every indexed pom, aggregators included (parent lookups).
    poms: dict[str, Path] = field(default_factory=dict)
    #: GA → number of indexed modules declaring it on their main classpath (hub detection).
    fan_in: dict[str, int] = field(default_factory=dict)

    def get(self, ga: str) -> Path | None:
        return self.modules.get(ga)

    def edges(self, module_dir: Path, *, test: bool = False) -> list[Dependency]:
        """In-repo-resolvable dependency edges of *module_dir*, in declaration order.

        Main-classpath scopes of the module and of every in-repo parent pom (nearest first;
        the chain is looked up by GA inside this index only, with a visited set and a depth
        bound). With *test*, test-scope dependencies are included too. Deduplicated by
        (GA, test-jar); GAs that are not importable modules of this index are dropped.
        """
        result: list[Dependency] = []
        seen: set[tuple[str, bool]] = set()
        current: Path | None = module_dir
        visited: set[Path] = set()
        depth = 0
        while current is not None and current not in visited and depth <= MAX_PARENT_DEPTH:
            visited.add(current)
            for dep in self.dependencies.get(current, ()):
                if dep.scope not in MAIN_SCOPES and not (test and dep.scope == "test"):
                    continue
                key = (dep.ga, dep.test_jar)
                if key in seen or dep.ga not in self.modules:
                    continue
                seen.add(key)
                result.append(dep)
            parent_ga = self.parent_of.get(current)
            current = self.poms.get(parent_ga) if parent_ga else None
            depth += 1
        return result

    def is_hub(self, ga: str, threshold: int) -> bool:
        return self.fan_in.get(ga, 0) >= threshold


@dataclass(frozen=True)
class _Pom:
    group_id: str | None
    artifact_id: str | None
    modules: tuple[str, ...]
    parent_ga: str | None = None
    dependencies: tuple[Dependency, ...] = ()


def _local(tag: object) -> str:
    """Tag name without its ``{namespace}``."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for c in elem:
        if _local(c.tag) == name:
            return c
    return None


def _children(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [c for c in elem if _local(c.tag) == name]


def _text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    value = elem.text.strip()
    return value or None


def _read_pom_bytes(pom: Path) -> bytes | None:
    """Raw bytes of *pom*, or None when it is a symlink, too large, or declares a DOCTYPE/ENTITY."""
    try:
        if pom.is_symlink() or not pom.is_file() or pom.stat().st_size > MAX_POM_BYTES:
            return None
        data = pom.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_POM_BYTES:
        return None
    if _FORBIDDEN_MARKUP.search(data) or _declares_dtd(data):
        logger.info("reactor: skipping %s (DOCTYPE/ENTITY declarations are not allowed)", pom.name)
        return None
    return data


def _coordinate(value: str | None) -> str | None:
    return value if value and _COORDINATE_RE.fullmatch(value) else None


def _parse_dependencies(
    project: ET.Element, group_id: str | None, parent_group_id: str | None
) -> tuple[Dependency, ...]:
    """Project-level ``<dependencies>`` (not ``dependencyManagement``, not profiles)."""
    result: list[Dependency] = []
    for dep in _children(_child(project, "dependencies"), "dependency")[:MAX_DEPENDENCIES_PER_POM]:
        raw_group = _text(_child(dep, "groupId"))
        if raw_group in _OWN_GROUP_PLACEHOLDERS:
            raw_group = group_id
        elif raw_group in _PARENT_GROUP_PLACEHOLDERS:
            raw_group = parent_group_id
        g = _coordinate(raw_group)
        a = _coordinate(_text(_child(dep, "artifactId")))
        if g is None or a is None:
            continue
        scope = (_text(_child(dep, "scope")) or "compile").lower()
        dep_type = _text(_child(dep, "type")) or "jar"
        classifier = _text(_child(dep, "classifier"))
        result.append(Dependency(f"{g}:{a}", scope, dep_type == "test-jar" or classifier == "tests"))
    return tuple(result)


def parse_pom(pom: Path) -> _Pom | None:
    """Parse the coordinates, parent, ``<dependencies>`` and ``<modules>`` (all profiles) of *pom*.

    None if rejected or invalid.
    """
    data = _read_pom_bytes(pom)
    if data is None:
        return None
    try:
        project = ET.fromstring(data)
    except (ET.ParseError, ValueError):  # ValueError: e.g. encoding="UTF-16-BE"
        return None
    if _local(project.tag) != "project":
        return None
    parent = _child(project, "parent")
    parent_group = _text(_child(parent, "groupId")) if parent is not None else None
    parent_artifact = _text(_child(parent, "artifactId")) if parent is not None else None
    group_id = _text(_child(project, "groupId")) or parent_group
    artifact_id = _text(_child(project, "artifactId"))
    pg, pa = _coordinate(parent_group), _coordinate(parent_artifact)
    parent_ga = f"{pg}:{pa}" if pg and pa else None
    module_lists = [_child(project, "modules")]
    for profile in _children(_child(project, "profiles"), "profile"):
        module_lists.append(_child(profile, "modules"))
    modules: list[str] = []
    for modules_elem in module_lists:
        for m in _children(modules_elem, "module"):
            value = _text(m)
            if value and value not in modules:
                modules.append(value)
    return _Pom(group_id, artifact_id, tuple(modules), parent_ga, _parse_dependencies(project, group_id, parent_group))


def _module_pom(base: Path, entry: str) -> Path:
    """``<module>`` is a directory (holding pom.xml) or, rarely, a path to a pom file."""
    candidate = base / entry
    return candidate if entry.endswith(".xml") else candidate / "pom.xml"


def _contained_dir(pom: Path, real_root: Path) -> Path | None:
    """Resolved directory of *pom* if it is inside *real_root* and *pom* is not a symlink."""
    try:
        if pom.is_symlink():
            return None
        module_dir = pom.parent.resolve()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop on older Pythons
        return None
    return module_dir if module_dir.is_relative_to(real_root) else None


def _resolve_found(index: ReactorIndex, found: dict[str, set[Path]]) -> None:
    """Keep GAs declared by exactly one non-aggregator directory; log the ambiguous ones once."""
    for ga, dirs in found.items():
        if len(dirs) == 1:
            index.poms[ga] = next(iter(dirs))
        if ga in index.aggregators:
            continue
        if len(dirs) == 1:
            index.modules[ga] = next(iter(dirs))
        else:
            index.ambiguous.add(ga)
            logger.info("reactor: ignoring %s, declared by %d module directories", ga, len(dirs))


def build_reactor_index(
    root: Path,
    *,
    max_poms: int = MAX_POMS,
    max_seconds: float = MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ReactorIndex:
    """Index the modules reachable from ``root/pom.xml`` through ``<modules>``.

    Breadth-first; each pom is read at most once. A module whose resolved directory
    is outside the resolved root, or whose pom is a symlink, is skipped. Coordinates
    with characters outside ``[A-Za-z0-9_.-]`` (such as ``${...}`` placeholders) are
    skipped. A GA declared by two directories is dropped entirely and logged once.
    Blocking: run it in an executor.
    """
    try:
        real_root = root.resolve()
    except (OSError, RuntimeError):
        return ReactorIndex(root)
    index = ReactorIndex(real_root)
    deadline = clock() + max_seconds
    seen: set[Path] = set()
    found: dict[str, set[Path]] = {}
    queue: deque[Path] = deque([real_root / "pom.xml"])
    while queue:
        if index.poms_read >= max_poms or clock() > deadline:
            index.truncated = True
            logger.warning(
                "reactor: index of %s stopped early after %d poms (bounds: %d poms, %.0fs)",
                real_root.name,
                index.poms_read,
                max_poms,
                max_seconds,
            )
            break
        pom = queue.popleft()
        module_dir = _contained_dir(pom, real_root)
        if module_dir is None or module_dir in seen:
            continue
        seen.add(module_dir)
        parsed = parse_pom(module_dir / pom.name)
        index.poms_read += 1
        if parsed is None:
            continue
        g, a = parsed.group_id, parsed.artifact_id
        if g and a and _COORDINATE_RE.fullmatch(g) and _COORDINATE_RE.fullmatch(a):
            found.setdefault(f"{g}:{a}", set()).add(module_dir)
            if parsed.modules:
                index.aggregators.add(f"{g}:{a}")
        if parsed.dependencies:
            index.dependencies[module_dir] = parsed.dependencies
            for dep in {d.ga for d in parsed.dependencies if d.scope in MAIN_SCOPES}:
                index.fan_in[dep] = index.fan_in.get(dep, 0) + 1
        if parsed.parent_ga:
            index.parent_of[module_dir] = parsed.parent_ga
        for entry in parsed.modules:
            if "${" in entry:
                continue
            queue.append(_module_pom(module_dir, entry))
    _resolve_found(index, found)
    return index


def find_reactor_root(module_dir: Path, boundary: Path | None = None) -> Path:
    """Topmost ancestor of *module_dir* reachable through consecutive pom.xml directories.

    Never goes above *boundary* (e.g. the repository's ``.git`` directory) when given.
    """
    top = module_dir
    while top.parent != top and (top.parent / "pom.xml").is_file():
        if boundary is not None and top == boundary:
            break
        top = top.parent
    return top


def parse_missing_markers(messages: tuple[str, ...] | list[str]) -> list[Marker]:
    """Every m2e "Missing artifact" message as a ``Marker``, in order, deduped.

    ``g:a:type:version`` or ``g:a:type:classifier:version``; a ``test-jar`` type or a
    ``tests`` classifier marks a test-jar.
    """
    result: list[Marker] = []
    for message in messages:
        match = _MISSING_ARTIFACT_RE.search(message)
        if not match:
            continue
        # "<type>:<version>" or "<type>:<classifier>:<version>"; tolerate a truncated message
        # ("g:a:jar"): a pom diagnostic must never raise inside the jdtls reader loop.
        parts = match.group(3).split(":")
        dep_type = parts[0]
        classifier = parts[1] if len(parts) > 2 else None  # noqa: PLR2004 (type:classifier:version)
        marker = Marker(f"{match.group(1)}:{match.group(2)}", dep_type == "test-jar" or classifier == "tests")
        if marker not in result:
            result.append(marker)
    return result


def parse_missing_artifacts(messages: tuple[str, ...] | list[str]) -> list[str]:
    """Return the ``groupId:artifactId`` of every m2e "Missing artifact" message, in order, deduped."""
    result: list[str] = []
    for marker in parse_missing_markers(messages):
        if marker.ga not in result:
            result.append(marker.ga)
    return result

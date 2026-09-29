"""Maven reactor index and m2e "Missing artifact" parsing (#110).

Pure functions, no jdtls. ``build_reactor_index`` maps ``groupId:artifactId`` to the
module directory of every module reachable from a reactor root through ``<modules>``
(all profiles unioned). The proxy uses it to turn m2e's pom.xml diagnostic
``Missing artifact g:a:type:version`` into "import that in-repo module as a
workspace folder".

The poms come from the user's checkout, so parsing is defensive: files over
``MAX_POM_BYTES`` or containing a ``<!DOCTYPE``/``<!ENTITY`` declaration are
rejected before XML parsing (no entity expansion), symlinked poms are skipped,
every module must resolve inside the reactor root, and the walk is bounded by a
pom count and a wall-clock budget.
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

logger = logging.getLogger(__name__)

MAX_POM_BYTES = 1024 * 1024
MAX_POMS = 5000
MAX_SECONDS = 10.0

_FORBIDDEN_MARKUP = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
#: m2e: "[<prefix> / ]Missing artifact g:a:type[:classifier]:version". ``${revision}`` is
#: already interpolated. Only groupId and artifactId are kept.
_MISSING_ARTIFACT_RE = re.compile(r"(?:^|/\s*)Missing artifact ([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):\S+")


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

    def get(self, ga: str) -> Path | None:
        return self.modules.get(ga)


@dataclass(frozen=True)
class _Pom:
    group_id: str | None
    artifact_id: str | None
    modules: tuple[str, ...]


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
    if _FORBIDDEN_MARKUP.search(data):
        logger.info("reactor: skipping %s (DOCTYPE/ENTITY declarations are not allowed)", pom.name)
        return None
    return data


def parse_pom(pom: Path) -> _Pom | None:
    """Parse the coordinates and ``<modules>`` (all profiles) of *pom*; None if rejected or invalid."""
    data = _read_pom_bytes(pom)
    if data is None:
        return None
    try:
        project = ET.fromstring(data)
    except ET.ParseError:
        return None
    if _local(project.tag) != "project":
        return None
    parent = _child(project, "parent")
    group_id = _text(_child(project, "groupId")) or _text(_child(parent, "groupId") if parent is not None else None)
    artifact_id = _text(_child(project, "artifactId"))
    module_lists = [_child(project, "modules")]
    for profile in _children(_child(project, "profiles"), "profile"):
        module_lists.append(_child(profile, "modules"))
    modules: list[str] = []
    for modules_elem in module_lists:
        for m in _children(modules_elem, "module"):
            value = _text(m)
            if value and value not in modules:
                modules.append(value)
    return _Pom(group_id, artifact_id, tuple(modules))


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
    containing ``${`` are skipped. A GA declared by two directories is dropped
    entirely and logged once. Blocking: run it in an executor.
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
        if g and a and "${" not in g and "${" not in a:
            found.setdefault(f"{g}:{a}", set()).add(module_dir)
            if parsed.modules:
                index.aggregators.add(f"{g}:{a}")
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


def parse_missing_artifacts(messages: tuple[str, ...] | list[str]) -> list[str]:
    """Return the ``groupId:artifactId`` of every m2e "Missing artifact" message, in order, deduped."""
    result: list[str] = []
    for message in messages:
        match = _MISSING_ARTIFACT_RE.search(message)
        if match:
            ga = f"{match.group(1)}:{match.group(2)}"
            if ga not in result:
                result.append(ga)
    return result

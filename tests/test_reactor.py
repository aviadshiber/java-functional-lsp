"""Tests for the Maven reactor index and the m2e "Missing artifact" parser (#110 part B)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from java_functional_lsp.reactor import (
    MAX_POM_BYTES,
    build_reactor_index,
    find_reactor_root,
    parse_missing_artifacts,
    parse_pom,
)

_NS = ' xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(
    artifact: str,
    *,
    group: str | None = None,
    parent_group: str | None = "com.example",
    modules: tuple[str, ...] = (),
    profile_modules: tuple[str, ...] = (),
    ns: bool = True,
) -> str:
    parts = [f"<project{_NS if ns else ''}>", "<modelVersion>4.0.0</modelVersion>"]
    if parent_group:
        parts.append(f"<parent><groupId>{parent_group}</groupId><artifactId>p</artifactId></parent>")
    if group:
        parts.append(f"<groupId>{group}</groupId>")
    parts.append(f"<artifactId>{artifact}</artifactId>")
    if modules:
        parts.append("<modules>" + "".join(f"<module>{m}</module>" for m in modules) + "</modules>")
    if profile_modules:
        parts.append(
            "<profiles><profile><id>jars</id><modules>"
            + "".join(f"<module>{m}</module>" for m in profile_modules)
            + "</modules></profile></profiles>"
        )
    parts.append("</project>")
    return "\n".join(parts)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel / "pom.xml" if rel else root / "pom.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.parent


@pytest.fixture
def reactor(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    _write(root, "", _pom("root", group="com.example", parent_group=None, modules=("groupA", "groupB")))
    _write(root, "groupA", _pom("groupA", modules=("common",)))
    _write(root, "groupA/common", _pom("common"))
    _write(root, "groupB", _pom("groupB", modules=("it",), ns=False))
    _write(root, "groupB/it", _pom("it", ns=False))
    return root


class TestIndex:
    def test_maps_reachable_modules_with_inherited_group(self, reactor: Path) -> None:
        index = build_reactor_index(reactor)
        real = reactor.resolve()
        assert index.get("com.example:common") == real / "groupA" / "common"
        assert index.get("com.example:it") == real / "groupB" / "it"  # namespace-less pom
        assert index.poms_read == 5
        assert not index.truncated

    def test_aggregators_are_never_resolved(self, reactor: Path) -> None:
        index = build_reactor_index(reactor)
        assert index.get("com.example:root") is None
        assert index.get("com.example:groupA") is None
        assert index.aggregators == {"com.example:root", "com.example:groupA", "com.example:groupB"}

    def test_own_group_id_wins_over_parent(self, reactor: Path) -> None:
        _write(reactor, "groupA/common", _pom("common", group="org.other"))
        index = build_reactor_index(reactor)
        assert index.get("org.other:common") is not None
        assert index.get("com.example:common") is None

    def test_unreachable_pom_is_not_indexed(self, reactor: Path) -> None:
        _write(reactor, "groupA/orphan", _pom("orphan"))
        assert build_reactor_index(reactor).get("com.example:orphan") is None

    def test_profile_modules_are_unioned(self, reactor: Path) -> None:
        _write(reactor, "groupA", _pom("groupA", modules=("common",), profile_modules=("extra",)))
        _write(reactor, "groupA/extra", _pom("extra"))
        assert build_reactor_index(reactor).get("com.example:extra") is not None

    def test_module_entry_may_name_a_pom_file(self, reactor: Path) -> None:
        _write(reactor, "groupA", _pom("groupA", modules=("common", "alt/pom.xml")))
        _write(reactor, "groupA/alt", _pom("alt"))
        assert build_reactor_index(reactor).get("com.example:alt") is not None

    @pytest.mark.parametrize(
        "declaration",
        [
            '<!DOCTYPE project [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>',
            '<!doctype project SYSTEM "file:///etc/passwd">',
            '<!ENTITY x SYSTEM "file:///etc/passwd">',
        ],
    )
    def test_doctype_or_entity_pom_is_rejected(
        self, reactor: Path, declaration: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        malicious = _pom("groupA", modules=("common",)).replace("<project", declaration + "\n<project", 1)
        _write(reactor, "groupA", malicious)
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.reactor"):
            index = build_reactor_index(reactor)
        assert index.poms_read == 4  # groupA read and rejected, common never reached
        assert index.get("com.example:common") is None  # its modules are not followed either
        assert index.get("com.example:it") is not None
        # The guard itself rejected it, not an incidental XML parse error.
        assert "DOCTYPE/ENTITY declarations are not allowed" in caplog.text

    @pytest.mark.parametrize(
        ("codec", "bom"),
        [("utf-16", b""), ("utf-16-be", b"\xfe\xff"), ("utf-16-le", b"")],  # "utf-16" writes its own BOM
    )
    def test_doctype_in_non_ascii_encoding_is_rejected(
        self, reactor: Path, codec: str, bom: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The byte regex cannot see "<!DOCTYPE" in UTF-16; without the parser-level guard the
        # internal entity expands and the pom parses as groupA.
        text = _pom("groupA", modules=("common",)).replace(
            "<artifactId>groupA</artifactId>", "<artifactId>&a;</artifactId>", 1
        )
        body = '<?xml version="1.0" encoding="UTF-16"?>\n<!DOCTYPE project [<!ENTITY a "groupA">]>\n' + text
        data = bom + body.encode(codec)
        pom = reactor / "groupA" / "pom.xml"
        pom.write_bytes(data)
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.reactor"):
            assert parse_pom(pom) is None
            index = build_reactor_index(reactor)
        assert index.get("com.example:common") is None
        assert index.get("com.example:it") is not None
        assert "DOCTYPE/ENTITY declarations are not allowed" in caplog.text

    def test_pom_in_an_encoding_expat_rejects_does_not_abort_the_index(self, reactor: Path) -> None:
        text = '<?xml version="1.0" encoding="UTF-16-BE"?>\n' + _pom("groupA", modules=("common",))
        (reactor / "groupA" / "pom.xml").write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))
        index = build_reactor_index(reactor)  # expat raises ValueError for this declaration
        assert index.get("com.example:common") is None
        assert index.get("com.example:it") is not None

    def test_utf16_pom_without_declarations_still_parses(self, reactor: Path) -> None:
        text = '<?xml version="1.0" encoding="UTF-16"?>\n' + _pom("groupA", modules=("common",))
        (reactor / "groupA" / "pom.xml").write_bytes(text.encode("utf-16"))
        assert build_reactor_index(reactor).get("com.example:common") is not None

    @pytest.mark.parametrize("artifact", ["com\nmon", "com mon", "jdtls: importing"])
    def test_coordinates_outside_the_maven_charset_are_skipped(
        self, reactor: Path, artifact: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Declared by two directories, so it would be logged as ambiguous if it were kept.
        _write(reactor, "groupA/common", _pom(artifact))
        _write(reactor, "groupB", _pom("groupB", modules=("it", "copy")))
        _write(reactor, "groupB/copy", _pom(artifact))
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.reactor"):
            index = build_reactor_index(reactor)
        assert not [ga for ga in [*index.modules, *index.ambiguous] if "mon" in ga or "importing" in ga]
        assert index.get("com.example:it") is not None
        assert "ignoring" not in caplog.text

    def test_oversized_pom_is_rejected(self, reactor: Path) -> None:
        big = _pom("groupA", modules=("common",)).replace("</project>", "<!--" + "x" * MAX_POM_BYTES + "--></project>")
        _write(reactor, "groupA", big)
        assert build_reactor_index(reactor).get("com.example:common") is None

    def test_malformed_pom_is_tolerated(self, reactor: Path) -> None:
        _write(reactor, "groupA/common", "<project><artifactId>common</artifactId>")
        index = build_reactor_index(reactor)
        assert index.get("com.example:common") is None
        assert index.get("com.example:it") is not None

    def test_placeholder_coordinates_are_skipped(self, reactor: Path) -> None:
        _write(reactor, "groupA/common", _pom("common-${suffix}"))
        assert not [ga for ga in build_reactor_index(reactor).modules if "common" in ga]

    def test_duplicate_ga_is_dropped_and_logged_once(self, reactor: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write(reactor, "groupB", _pom("groupB", modules=("it", "copy")))
        _write(reactor, "groupB/copy", _pom("common"))
        with caplog.at_level(logging.INFO, logger="java_functional_lsp.reactor"):
            index = build_reactor_index(reactor)
        assert index.get("com.example:common") is None
        assert "com.example:common" in index.ambiguous
        assert sum("com.example:common" in r.getMessage() for r in caplog.records) == 1

    def test_module_outside_root_is_skipped(self, reactor: Path, tmp_path: Path) -> None:
        _write(tmp_path, "outside", _pom("outside"))
        _write(reactor, "groupA", _pom("groupA", modules=("common", "../../outside")))
        assert build_reactor_index(reactor).get("com.example:outside") is None

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_symlinked_dir_pointing_outside_is_skipped(self, reactor: Path, tmp_path: Path) -> None:
        target = _write(tmp_path, "elsewhere", _pom("elsewhere"))
        (reactor / "groupA" / "link").symlink_to(target, target_is_directory=True)
        _write(reactor, "groupA", _pom("groupA", modules=("common", "link")))
        assert build_reactor_index(reactor).get("com.example:elsewhere") is None

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_symlinked_pom_is_skipped(self, reactor: Path) -> None:
        real = _write(reactor, "groupA/real", _pom("viaLink"))
        (reactor / "groupA" / "lnk").mkdir()
        (reactor / "groupA" / "lnk" / "pom.xml").symlink_to(real / "pom.xml")
        _write(reactor, "groupA", _pom("groupA", modules=("common", "lnk")))
        assert build_reactor_index(reactor).get("com.example:viaLink") is None

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_symlink_loop_terminates(self, reactor: Path) -> None:
        (reactor / "groupA" / "common" / "loop").symlink_to(reactor / "groupA", target_is_directory=True)
        _write(reactor, "groupA/common", _pom("common", modules=("loop",)))
        index = build_reactor_index(reactor)
        assert index.poms_read == 5  # groupA is not read a second time through the link
        assert index.get("com.example:it") is not None

    def test_pom_count_bound(self, reactor: Path) -> None:
        index = build_reactor_index(reactor, max_poms=2)
        assert index.truncated
        assert index.poms_read == 2

    def test_time_bound(self, reactor: Path) -> None:
        ticks = iter([0.0, 0.0, 0.0, 99.0, 99.0, 99.0, 99.0, 99.0])
        index = build_reactor_index(reactor, max_seconds=10.0, clock=lambda: next(ticks))
        assert index.truncated
        assert index.poms_read == 2

    def test_missing_root_pom_gives_empty_index(self, tmp_path: Path) -> None:
        assert build_reactor_index(tmp_path).modules == {}

    def test_parse_pom_rejects_non_project_root(self, tmp_path: Path) -> None:
        pom = tmp_path / "pom.xml"
        pom.write_text("<settings/>")
        assert parse_pom(pom) is None


class TestReactorRoot:
    def test_topmost_consecutive_pom(self, reactor: Path) -> None:
        assert find_reactor_root(reactor / "groupB" / "it") == reactor

    def test_stops_at_boundary(self, reactor: Path) -> None:
        assert find_reactor_root(reactor / "groupB" / "it", boundary=reactor / "groupB") == reactor / "groupB"


class TestMissingArtifactParser:
    @pytest.mark.parametrize(
        "message",
        [
            "Missing artifact com.example:common:jar:X-DEFAULT",
            "Offline / Missing artifact com.example:common:jar:X-DEFAULT",
            "Offline /Missing artifact com.example:common:jar:X-DEFAULT",
            "Missing artifact com.example:common:test-jar:tests:X-DEFAULT",
        ],
    )
    def test_extracts_group_and_artifact(self, message: str) -> None:
        assert parse_missing_artifacts([message]) == ["com.example:common"]

    @pytest.mark.parametrize(
        "message",
        [
            "Project build error: Non-resolvable parent POM",
            "Missing artifact com.example",
            "The container 'Maven Dependencies' references non existing library",
            "see Missing artifact com.example:common:jar:1",  # not at the start or after a prefix
        ],
    )
    def test_ignores_other_messages(self, message: str) -> None:
        assert parse_missing_artifacts([message]) == []

    def test_dedupes_in_order(self) -> None:
        messages = (
            "Missing artifact a.b:x:jar:1",
            "Missing artifact a.b:y:jar:1",
            "Missing artifact a.b:x:test-jar:tests:1",
        )
        assert parse_missing_artifacts(messages) == ["a.b:x", "a.b:y"]


# --- dependency edges (v0.14.1) ----------------------------------------------------------------


def _deps_pom(artifact: str, parent: str | None, deps: str, *, modules: tuple[str, ...] = ()) -> str:
    parent_xml = f"<parent><groupId>g</groupId><artifactId>{parent}</artifactId></parent>" if parent else ""
    mods = "<modules>" + "".join(f"<module>{m}</module>" for m in modules) + "</modules>" if modules else ""
    return f"<project{_NS}>{parent_xml}<groupId>g</groupId><artifactId>{artifact}</artifactId>{deps}{mods}</project>"


def _dep(artifact: str, extra: str = "", group: str = "g") -> str:
    return f"<dependency><groupId>{group}</groupId><artifactId>{artifact}</artifactId>{extra}</dependency>"


@pytest.fixture
def chain(tmp_path: Path) -> Path:
    """root -> {grpA (declares owner, managed-only x) -> mid, grpB -> app, grpC -> {owner, deeper, x, tlib}}."""
    root = tmp_path / "chain"
    _write(root, "", _deps_pom("root", None, "", modules=("grpA", "grpB", "grpC")))
    _write(
        root,
        "grpA",
        _deps_pom(
            "grpA",
            "root",
            f"<dependencies>{_dep('owner')}</dependencies>"
            f"<dependencyManagement><dependencies>{_dep('x')}</dependencies></dependencyManagement>",
            modules=("mid",),
        ),
    )
    _write(root, "grpA/mid", _deps_pom("mid", "grpA", ""))
    _write(root, "grpB", _deps_pom("grpB", "root", "", modules=("app",)))
    app_deps = (
        _dep("mid")
        + _dep("x", "<scope>test</scope>")
        + _dep("tlib", "<type>test-jar</type><scope>test</scope>")
        + _dep("tlib", "<classifier>tests</classifier>")
        + _dep("deeper", "<scope>runtime</scope>", group="${project.groupId}")
        + _dep("owner", "<scope>import</scope>")
        + _dep("junit", "<scope>test</scope>", group="org.junit")
        + _dep("${weird}")
    )
    _write(root, "grpB/app", _deps_pom("app", "grpB", f"<dependencies>{app_deps}</dependencies>"))
    _write(root, "grpC", _deps_pom("grpC", "root", "", modules=("owner", "deeper", "x", "tlib")))
    _write(root, "grpC/owner", _deps_pom("owner", "grpC", f"<dependencies>{_dep('deeper')}</dependencies>"))
    for leaf in ("deeper", "x", "tlib"):
        _write(root, f"grpC/{leaf}", _deps_pom(leaf, "grpC", ""))
    return root


class TestEdges:
    def test_parent_chain_is_inherited(self, chain: Path) -> None:
        index = build_reactor_index(chain)
        mid = index.poms["g:mid"]
        assert index.parent_of[mid] == "g:grpA"
        # mid declares nothing itself; owner comes from its parent grpA. dependencyManagement is ignored.
        assert [d.ga for d in index.edges(mid)] == ["g:owner"]
        owner = index.poms["g:owner"]
        assert [d.ga for d in index.edges(owner)] == ["g:deeper"]

    def test_scopes_placeholders_and_test_jars(self, chain: Path) -> None:
        index = build_reactor_index(chain)
        app = index.poms["g:app"]
        # Main classpath: compile + runtime (the ${project.groupId} placeholder resolves); the
        # import-scope, external and unparseable entries are dropped; tests classifier is a test-jar.
        main = index.edges(app)
        assert [(d.ga, d.scope, d.test_jar) for d in main] == [
            ("g:mid", "compile", False),
            ("g:tlib", "compile", True),
            ("g:deeper", "runtime", False),
        ]
        with_tests = index.edges(app, test=True)
        assert [(d.ga, d.scope, d.test_jar) for d in with_tests] == [
            ("g:mid", "compile", False),
            ("g:x", "test", False),
            ("g:tlib", "test", True),
            ("g:deeper", "runtime", False),
        ]

    def test_fan_in_counts_main_scope_declarations(self, chain: Path) -> None:
        index = build_reactor_index(chain)
        assert index.fan_in["g:deeper"] == 2  # app (runtime) and owner
        assert "g:x" not in index.fan_in  # test scope / managed only
        assert index.is_hub("g:deeper", 2)
        assert not index.is_hub("g:deeper", 3)

    def test_parent_cycle_and_unknown_parent_terminate(self, tmp_path: Path) -> None:
        root = tmp_path / "cyc"
        _write(root, "", _deps_pom("root", "b", "", modules=("a", "b")))
        _write(root, "a", _deps_pom("a", "b", f"<dependencies>{_dep('b')}</dependencies>"))
        _write(root, "b", _deps_pom("b", "a", f"<dependencies>{_dep('a')}</dependencies>"))
        index = build_reactor_index(root)
        a = index.poms["g:a"]
        assert [d.ga for d in index.edges(a)] == ["g:b", "g:a"]
        assert index.edges(tmp_path / "unknown") == []


class TestMissingMarkers:
    def test_type_and_classifier_are_kept(self) -> None:
        from java_functional_lsp.reactor import Marker, parse_missing_markers

        assert parse_missing_markers(
            [
                "Offline / Missing artifact com.example:common:jar:X-DEFAULT",
                "Missing artifact com.example:base:jar:tests:X-DEFAULT",
                "Missing artifact com.example:base:test-jar:X-DEFAULT",
                "Missing artifact com.example:common:jar:X-DEFAULT",
                "Missing artifact com.example:native:jar:linux-x86_64:1.0",
                "Non-resolvable parent POM",
            ]
        ) == [
            Marker("com.example:common"),
            Marker("com.example:base", test_jar=True),
            Marker("com.example:native"),
        ]

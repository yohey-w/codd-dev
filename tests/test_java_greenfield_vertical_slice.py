"""Greenfield ② vertical slice for Java, mirroring the C# increment (commit 4fa7983,
``tests/test_csharp_greenfield_vertical_slice.py``).

RED-BEFORE-GREEN. Before this increment ``resolve_layout_profile('java')`` returned
``None`` (no legacy builder) so every greenfield deep gate (coverage-execution-campaign,
import-coherence, scaffold) was a strict NO-OP for Java. Commit 2736959 pins the SAME
OPT-IN, language-free synthesis C# uses onto ``java.yaml`` (``greenfield_synthesis:
true``) — this module is the vertical-slice regression test that increment shipped
WITHOUT (a gap found by inspection: ``tests/languages/test_profile_java.py`` only
covers the raw YAML shape, nothing about the synthesizer/gate/scaffold machinery this
opt-in actually activates).

Java exercises the SAME language-free machinery as C#, through two DIFFERENT data
forks:

  * ``package_root.kind: none`` (flat) — UNLIKE C#'s nested ``path_package``
    (``src/{package_name}``), Java's ``package_root`` collapses to its
    ``source_root`` (``src/main/java``, Maven's standard layout). There is no
    lib-dir-vs-source-root routing fork to pin (FORK #1 in the C# file) because
    there is only one dir.
  * NO ``naming.package_case`` declaration — Java stays the historical ``lower``
    default (like Python/TS/Go); it does NOT opt into C#'s case-PRESERVING
    ``pascal`` (FORK C in the C# file).
  * The scaffold writes ONE file (``pom.xml``, generic-template adapter) instead of
    C#'s three (lib .csproj / test .csproj / .sln). Java has no C#-style physical
    lib/test PROJECT split to structurally forbid a leaked test dependency; Maven's
    OWN ``<scope>test</scope>`` is the purity mechanism instead (a test-scoped
    dependency never reaches the compile/runtime classpath or the packaged jar) —
    see ``test_java_pom_test_dependency_is_scoped_test_no_leak_into_compile_runtime``,
    the Java analogue of the C# file's
    ``test_csharp_library_deliverable_is_pure_no_test_packages``.

Go (whose ``scaffold.adapter`` is generic-template too but which does NOT opt in)
MUST stay a strict NO-OP — the SAME control the C# file asserts is replicated here
(verified independently below, not assumed: ``go.yaml`` declares no
``greenfield_synthesis`` key as of this writing).

These are unit/integration tests; a real ``codd greenfield --language java`` AI run is
a SEPARATE next step (the campaign is exercised only up to gate-APPLICABILITY here —
see the NOTE on ``test_java_coverage_gate_applies_not_noop`` for a wrinkle found while
writing this file: ``run_verify_campaign``'s post-execution check requires the report
path to be a FILE, but Maven Surefire's real convention writes
``target/surefire-reports`` as a DIRECTORY of one ``TEST-<class>.xml`` per test class —
an end-to-end ``run_verify_campaign`` execution test is deliberately NOT included here
pending that reconciliation; it is out of scope for a test-only change).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from codd.coverage_execution_coherence import (
    coherence_gate_applies,
    resolve_runner_report_adapter,
    supported_runner_report_formats,
)
from codd.implement_oracle import resolve_implement_oracle
from codd.import_coherence import check_import_coherence
from codd.languages.adapters.runner_report import SurefireXmlReportAdapter
from codd.project_types import (
    LayoutProfile,
    resolve_layout_profile,
    scaffold_layout,
    synthesize_implement_oracle_spec,
)
from codd.vb_marker_authenticity import JavaTestBlockProfile

_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


# ── (1) generic LayoutProfile synthesizer ───────────────────────────────────────


def test_java_synthesizes_layout_profile() -> None:
    """resolve_layout_profile('java') synthesizes a profile (was None → NO-OP).

    Fields follow java.yaml's declared layout: source_root/test_root are Maven's
    standard ``src/main/java`` / ``src/test/java`` (NOT the bare ``src`` C# uses);
    package_root EQUALS source_root because java.yaml declares ``package_root.kind:
    none`` (FLAT — unlike C#'s nested ``path_package`` ``src/{package_name}``);
    requires_*_init=False and a non-"package_absolute" policy (so the Python
    import-coherence checks stay NO-OPs, same anti-false-RED reasoning as C#); the
    implement-oracle + verify-campaign are SET (not None)."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None, "java layout profile must be synthesized, not None"
    assert profile.language == "java"
    assert profile.source_root == "src/main/java"
    assert profile.test_root == "src/test/java"
    # kind: none → FLAT: package_root == source_root, unlike C#'s nested path_package.
    assert profile.package_root == "src/main/java"
    assert profile.package_name == "todo_cli"
    assert profile.requires_package_init is False
    assert profile.requires_test_init is False
    assert profile.test_import_policy != "package_absolute"
    assert profile.install_mode == "none"
    # runner is derived from toolchain.package_manager.id (maven), not a language literal.
    assert profile.runner == "maven"
    # the implement-oracle + verify-campaign are wired (not the silent NO-OP None).
    assert profile.implement_oracle is not None
    assert profile.verify_campaign is not None


def test_java_synthesis_is_alias_resolvable() -> None:
    """The opt-in resolves through the registry alias set too (``jvm``), and is
    case-insensitive (``matches()`` lower-cases both sides)."""
    for name in ("jvm", "JVM", "JAVA", "Java"):
        assert resolve_layout_profile(language=name, project_name="x") is not None, name


def test_java_profile_has_no_pascal_casing_defaults_to_lower() -> None:
    """java.yaml declares NO ``naming.package_case`` block (unlike csharp.yaml's
    ``pascal``) — the casing discipline defaults to ``lower``, matching Python/TS/Go.
    A project name is FORCE-LOWERED end to end; there is no nested cased dir for it
    to leak into (package_root stays the flat ``src/main/java`` regardless of the
    project name's casing) — the C# PascalCase-preservation fork does not apply here."""
    from codd.languages import default_registry

    assert default_registry.resolve("java").package_case == "lower"
    profile = resolve_layout_profile(language="java", project_name="TextKit")
    assert profile is not None
    assert profile.package_name == "textkit"  # NOT TextKit — no case preservation for java
    assert profile.package_root == "src/main/java"  # flat: unaffected by project-name casing


# ── (2) oracle non-regression (must NOT become None) ────────────────────────────


def test_java_implement_oracle_not_none_no_regression(tmp_path: Path) -> None:
    """resolve_implement_oracle('java') stays non-None after the layout profile exists.

    Before, java reached the oracle via ``_resolve_registry_oracle`` (legacy profile
    None). Now it reaches it via the synthesized profile's ``implement_oracle``. Both
    MUST yield the SAME composite spec (the shared-helper drift guard) — command
    sentinel ``java-composite`` (``{lang_id}-{kind}``, mirrors ``csharp-composite``)."""
    resolved = resolve_implement_oracle(tmp_path, language="java", project_name="todo")
    assert resolved is not None, "the java implement-oracle must not regress to None"
    _profile, spec = resolved
    assert spec.kind == "composite"
    assert spec.command == "java-composite"


def test_java_shared_oracle_spec_helper_matches_registry_path() -> None:
    """The shared helper produces the SAME spec the registry path used (drift guard)."""
    from codd.languages import default_registry

    lang_profile = default_registry.resolve("java")
    spec = synthesize_implement_oracle_spec(lang_profile)
    assert spec is not None
    assert (spec.kind, spec.command) == ("composite", "java-composite")
    assert spec.scope.require_source_root is True
    assert spec.scope.require_test_root is False
    assert spec.requires_node_install is False


# ── (3) surefire-xml runner-report adapter resolution (pin) ─────────────────────


def test_surefire_xml_runner_report_adapter_resolves() -> None:
    assert isinstance(resolve_runner_report_adapter("surefire-xml"), SurefireXmlReportAdapter)
    assert "surefire-xml" in set(supported_runner_report_formats())


# ── coverage-execution-coherence gate APPLIES for java (NO-OP脱却) ───────────────


def test_java_coverage_gate_applies_not_noop() -> None:
    """The coverage-execution coherence gate is APPLICABLE for the synthesized java
    profile (it was a strict NO-OP before — no campaign → not applicable), and its
    runner-report adapter resolves to the surefire-xml parser.

    NOTE (scope): this pins gate APPLICABILITY only. ``coherence_gate_applies`` checks
    that a campaign is DECLARED and EVERY report format HAS a registered adapter — it
    does not run the campaign. Java is now a ONE-step/TWO-report campaign (Surefire +
    Failsafe from a single ``mvn verify`` invocation, both ``surefire-xml`` — the
    multi-report verify campaigns generalization, 2026-07-02), so it resolves via
    ``steps``/``resolved_steps()`` rather than the legacy flat ``report_format`` /
    ``command_argv`` / ``report_relpath`` fields (those stay ``None`` for a
    steps-based campaign — see ``VerifyCampaignSpec``); ``runner_report_adapter()``
    still resolves to ONE adapter because both reports share the same format."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None
    assert coherence_gate_applies(profile) is True
    assert isinstance(profile.runner_report_adapter(), SurefireXmlReportAdapter)
    campaign = profile.verify_campaign
    assert campaign is not None
    steps = campaign.resolved_steps()
    assert len(steps) == 1
    assert steps[0].command_argv == ("mvn", "-q", "verify")
    reports_by_path = {r.relpath: r for r in steps[0].reports}
    assert reports_by_path["target/surefire-reports"].format == "surefire-xml"
    assert reports_by_path["target/surefire-reports"].optional is False
    assert reports_by_path["target/failsafe-reports"].format == "surefire-xml"
    assert reports_by_path["target/failsafe-reports"].optional is True


# ── lock = honest NO-OP (data-driven) ───────────────────────────────────────────


def test_java_toolchain_dependencies_is_none() -> None:
    """Java declares no ``dependency_integrity_files`` entry (Maven resolves straight
    from pom.xml, no lockfile) → toolchain_dependencies None — the SAME honest NO-OP
    C# gets. Data-driven (lock absence), not a language-name branch; see
    test_csharp_greenfield_vertical_slice.py's
    ``test_synthesize_toolchain_dependencies_lock_branch_is_data_driven`` for the
    proof that a lock-DECLARING stack would synthesize a non-None profile."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None
    assert profile.toolchain_dependencies is None


# ── (4) generic-template scaffolder → single-pom Maven model ────────────────────


def test_java_scaffolds_pom_with_defaults_substituted(tmp_path: Path, monkeypatch) -> None:
    """scaffold_layout writes the SINGLE ``pom.xml`` at the repo root from java.yaml's
    template: java.yaml's ``package_root.kind: none`` means there is no C#-style
    nested lib/test project split — create-only / idempotent / non-clobber,
    substituting {package_name} + scaffold.defaults (group_id, java_version,
    junit_jupiter_version, surefire_plugin_version, compiler_plugin_version,
    build_helper_plugin_version). Also asserts the Failsafe/build-helper/compiler
    wiring (added alongside the Surefire/Failsafe coverage-execution-coherence fix)
    is present, since every generated Java project's pom depends on it to make
    ``tests/e2e/java`` a real, Failsafe-run second test source root."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None

    # Pin the host-toolchain probe to fail-open (None) so the DECLARED
    # java_version default (21) flows through host-independently — the clamp
    # behavior itself is pinned by test_layout_profile.py::TestHostVersionClamps.
    import codd.project_types as pt

    monkeypatch.setattr(pt, "_probe_host_toolchain_version", lambda argv, pattern: None)

    result = scaffold_layout(tmp_path, profile)
    pom = tmp_path / "pom.xml"
    assert pom.is_file()
    assert {"pom.xml"} <= set(result.created)

    # scaffold.defaults + the resolved package_name substituted; no UNSUBSTITUTED
    # template var leaked.
    body = pom.read_text(encoding="utf-8")
    assert "<artifactId>todo_cli</artifactId>" in body
    assert "<groupId>org.example</groupId>" in body
    assert "<maven.compiler.release>21</maven.compiler.release>" in body
    assert "<version>5.11.0</version>" in body  # junit_jupiter_version default
    assert "<version>3.2.5</version>" in body  # surefire_plugin_version default
    for var in (
        "{package_name}", "{group_id}", "{java_version}",
        "{junit_jupiter_version}", "{surefire_plugin_version}",
        "{compiler_plugin_version}", "{build_helper_plugin_version}",
    ):
        assert var not in body, var

    # Failsafe + build-helper + explicit compiler-plugin version, all present.
    assert "maven-compiler-plugin" in body
    assert "<version>3.13.0</version>" in body  # compiler_plugin_version default
    assert "build-helper-maven-plugin" in body
    assert "<version>3.6.0</version>" in body  # build_helper_plugin_version default
    assert "<source>tests/e2e/java</source>" in body
    assert "maven-failsafe-plugin" in body
    assert "<goal>integration-test</goal>" in body

    # idempotent: a second call creates nothing, skips the existing file.
    result2 = scaffold_layout(tmp_path, profile)
    assert result2.created == ()
    assert "pom.xml" in result2.skipped

    # non-clobber: an authored file is left byte-for-byte.
    pom.write_text("AUTHORED", encoding="utf-8")
    scaffold_layout(tmp_path, profile)
    assert pom.read_text(encoding="utf-8") == "AUTHORED"


def test_java_pom_test_dependency_is_scoped_test_no_leak_into_compile_runtime(
    tmp_path: Path,
) -> None:
    """ANTI-FALSE-GREEN (the load-bearing point, Java's analogue of the C# file's
    ``test_csharp_library_deliverable_is_pure_no_test_packages``): Java has NO
    lib/test PROJECT split (``package_root.kind: none``, one ``pom.xml``) — so
    purity is enforced by MAVEN'S OWN ``<scope>test</scope>`` mechanism instead of
    physical file separation (per java.yaml's ``scaffold.templates`` comment: a
    test-scoped dependency is excluded from the compile/runtime classpath of
    consumers AND from the packaged jar by Maven's own dependency resolution).
    junit-jupiter MUST be the ONLY dependency declared, AND it MUST carry
    ``<scope>test</scope>`` — a regression that drops the scope (or adds a second,
    unscoped dependency) would leak a test-only library into the shipped runtime
    artifact, the exact single-project FALSE GREEN Option C forbids structurally
    for C#. This pins the equivalent STRUCTURAL purity for Java's flat single-pom
    model."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None
    scaffold_layout(tmp_path, profile)

    pom_text = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    assert pom_text.count("<dependency>") == 1, "unexpected extra dependency: possible scope leak"

    root = ET.fromstring(pom_text)
    deps = root.findall(".//m:dependencies/m:dependency", _POM_NS)
    assert len(deps) == 1, "exactly one dependency must be declared (the test-scoped junit one)"
    dep = deps[0]
    assert dep.findtext("m:groupId", namespaces=_POM_NS) == "org.junit.jupiter"
    assert dep.findtext("m:artifactId", namespaces=_POM_NS) == "junit-jupiter"
    assert dep.findtext("m:scope", namespaces=_POM_NS) == "test", (
        "junit-jupiter must be <scope>test</scope> — an unscoped/compile-scoped test "
        "dependency would leak into the shipped runtime artifact (FALSE GREEN)."
    )


# ── (5) import-coherence does NOT false-RED for java ─────────────────────────────


def test_java_import_coherence_no_false_red(tmp_path: Path) -> None:
    """A realistic Java/Maven project passes import-coherence (the Python missing-
    __init__ / bare-basename / manifest checks must be strict NO-OPs for a
    kind:none layout, exactly like C#'s kind:path_package)."""
    (tmp_path / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (tmp_path / "src" / "test" / "java" / "com" / "example").mkdir(parents=True)
    (tmp_path / "src" / "main" / "java" / "com" / "example" / "Calculator.java").write_text(
        "package com.example;\n"
        "public class Calculator { public int add(int a, int b) { return a + b; } }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "test" / "java" / "com" / "example" / "CalculatorTest.java").write_text(
        "package com.example;\n"
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
        "class CalculatorTest { @Test void adds() { assertEquals(3, new Calculator().add(1, 2)); } }\n",
        encoding="utf-8",
    )
    (tmp_path / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"></project>\n', encoding="utf-8"
    )
    result = check_import_coherence(tmp_path, language="java", project_name="todo-cli")
    assert result.passed is True, [f.kind for f in result.findings]
    assert result.findings == []


# ── (6) harness_owned_scaffold_paths includes pom.xml ────────────────────────────


def test_java_harness_owned_scaffold_paths_includes_pom() -> None:
    """The orphan-gate / write-fence must recognise ``pom.xml`` as harness-owned —
    never an unowned orphan, never reverted by the scoped-rerun write-fence. Java's
    flat model owns exactly one scaffold file (unlike C#'s three: lib csproj, test
    csproj, .sln)."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None
    owned = profile.harness_owned_scaffold_paths()
    assert "pom.xml" in owned


# ── (7) test_block_profile resolves to JavaTestBlockProfile ─────────────────────


def test_java_test_block_profile_resolves_to_java_parser() -> None:
    """LayoutProfile.test_block_profile() resolves to JavaTestBlockProfile for a
    synthesized java layout profile (adapter id ``java-junit-semantics`` → the
    parser-table entry wired into project_types.py alongside this morning's opt-in).
    Without this wiring the VB authenticity gate would silently degrade to its
    language-agnostic stage-1 (orphan-marker) check only for every Java project —
    the exact silent-degrade ``tests/test_java_test_block_profile.py`` warns about
    in its module docstring (it exercises the parser only via a hand-rolled stub,
    never through this real synthesized-profile path)."""
    profile = resolve_layout_profile(language="java", project_name="todo-cli")
    assert profile is not None
    assert isinstance(profile.test_block_profile(), JavaTestBlockProfile)


# ── (8) Go exclusion: opt-out stays a strict NO-OP ──────────────────────────────


def test_go_excluded_resolve_layout_profile_still_none() -> None:
    """Go does NOT opt in → resolve_layout_profile('go') stays None (no synthesis).

    The SAME control language the C# vertical slice uses
    (``test_csharp_greenfield_vertical_slice.py::test_go_excluded_resolve_layout_
    profile_still_none``), replicated here so this file's regression pin does not
    depend on the C# file continuing to exist. Verified independently (not
    assumed): ``go.yaml`` declares no ``greenfield_synthesis`` key as of this
    writing, and case-insensitive aliasing (``GO``) is checked too."""
    assert resolve_layout_profile(language="go", project_name="m") is None
    assert resolve_layout_profile(language="golang", project_name="m") is None
    assert resolve_layout_profile(language="GO", project_name="m") is None


def test_go_excluded_scaffold_is_noop(tmp_path: Path) -> None:
    """A Go LayoutProfile scaffolds NOTHING (its scaffold.adapter is generic-template
    too, but Go has no opt-in key) — the same non-regression control the C# file
    pins (``test_unknown_stack_is_noop`` lineage)."""
    go_profile = LayoutProfile(
        language="go",
        package_name="x",
        source_root="src",
        package_root="src/x",
        test_root="tests",
    )
    result = scaffold_layout(tmp_path, go_profile)
    assert result.created == ()
    assert list(tmp_path.iterdir()) == []

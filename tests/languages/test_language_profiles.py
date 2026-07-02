"""Tests for the Phase-1 language-generality subsystem (additive).

Covers:
* load + resolve all 3 bundled profiles by id AND by alias;
* the Go-no-``src/`` invariant (no source_set / test_set / scaffold / artifact
  / forbidden-prefix path may sit under ``src/``), with go.mod at module_root ".";
* Python/TS DO retain their ``src/`` layout;
* the model round-trips (loader produces the expected dataclass shapes);
* ``resolve()`` raises on an unknown language.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages import (
    AdapterRegistry,
    CommandSpec,
    LanguageProfile,
    LanguageRegistry,
    PackageRoot,
    SourceSet,
    TestSet,
    UnknownLanguageError,
    load_language_profile,
)
from codd.languages.registry import PROFILES_DIR


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> LanguageRegistry:
    return LanguageRegistry()


def _contains_src(text: str) -> bool:
    """True if a path string is rooted at / passes through a ``src/`` segment."""
    return text == "src" or text.startswith("src/") or "/src/" in text


def _all_layout_paths(profile: LanguageProfile) -> list[str]:
    paths: list[str] = []
    for ss in profile.layout.source_sets:
        paths.append(ss.root)
        paths.extend(ss.file_globs)
    for ts in profile.layout.test_sets:
        paths.append(ts.root)
        paths.extend(ts.file_globs)
    if profile.layout.package_root.path:
        paths.append(profile.layout.package_root.path)
    if profile.scaffold:
        paths.extend(profile.scaffold.owned_files)
    if profile.artifacts:
        paths.extend(profile.artifacts.harness_owned)
    return paths


# ---------------------------------------------------------------------------
# discovery / resolution
# ---------------------------------------------------------------------------


def test_all_three_profiles_discovered(registry: LanguageRegistry) -> None:
    ids = set(registry.ids())
    assert {"python", "typescript", "go"} <= ids


@pytest.mark.parametrize("lang_id", ["python", "typescript", "go"])
def test_resolve_by_id(registry: LanguageRegistry, lang_id: str) -> None:
    profile = registry.resolve(lang_id)
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == lang_id


@pytest.mark.parametrize(
    "alias, expected_id",
    [
        ("golang", "go"),
        ("GoLang", "go"),  # case-insensitive
        ("py", "python"),
        ("python3", "python"),
        ("node", "typescript"),
        ("ts", "typescript"),
        ("  Node  ", "typescript"),  # surrounding whitespace tolerated
        # NOTE: "javascript" is intentionally NOT parametrized here anymore.
        # javascript.yaml (id: javascript) now claims that exact string via the
        # registry's id fast-path — it resolves to its OWN profile, not
        # typescript's, even though typescript.yaml's ``aliases:`` list still
        # textually contains "javascript" (an intentionally-left, now-shadowed,
        # harmless entry — see tests/languages/test_profile_javascript.py's
        # ``test_javascript_id_shadows_the_typescript_alias_entry``). "ts"/
        # "node" above are UNCHANGED, since neither is a distinct profile id.
    ],
)
def test_resolve_by_alias(
    registry: LanguageRegistry, alias: str, expected_id: str
) -> None:
    profile = registry.resolve(alias)
    assert profile.identity.id == expected_id


def test_resolve_unknown_raises(registry: LanguageRegistry) -> None:
    with pytest.raises(UnknownLanguageError) as excinfo:
        registry.resolve("rust")
    # error should list the known languages, to be actionable
    assert "go" in excinfo.value.known
    assert "python" in excinfo.value.known


def test_resolve_none_raises(registry: LanguageRegistry) -> None:
    with pytest.raises(UnknownLanguageError):
        registry.resolve(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Go: the no-src/ invariant (the load-bearing point of the redesign)
# ---------------------------------------------------------------------------


def test_go_manifest_is_go_mod_at_module_root(registry: LanguageRegistry) -> None:
    go = registry.resolve("go")
    assert go.layout.module_root == "."
    assert go.toolchain is not None
    assert go.toolchain.manifest.path == "go.mod"
    assert go.toolchain.manifest.format == "go.mod"
    assert go.toolchain.manifest.required is True


def test_go_has_no_src_anywhere(registry: LanguageRegistry) -> None:
    """No source_set / test_set / scaffold / artifact path may contain src/."""
    go = registry.resolve("go")

    offenders = [p for p in _all_layout_paths(go) if _contains_src(p)]
    assert offenders == [], f"Go profile leaked src/ paths: {offenders}"

    # explicit per-set checks for a clear failure message
    for ss in go.layout.source_sets:
        assert not _contains_src(ss.root), f"source_set {ss.id} rooted under src/"
        for g in ss.file_globs:
            assert not _contains_src(g), f"source_set {ss.id} glob under src/: {g}"
    for ts in go.layout.test_sets:
        assert not _contains_src(ts.root), f"test_set {ts.id} rooted under src/"
        for g in ts.file_globs:
            assert not _contains_src(g), f"test_set {ts.id} glob under src/: {g}"

    # forbidden_generated_prefixes (preserved under .extra) must include src/
    forbidden = (
        go.extra.get("path_rules", {}).get("forbidden_generated_prefixes", [])
    )
    assert "src/" in list(forbidden)


def test_go_source_and_test_sets_shape(registry: LanguageRegistry) -> None:
    go = registry.resolve("go")

    src_by_id = {s.id: s for s in go.layout.source_sets}
    assert set(src_by_id) == {"commands", "internal"}
    assert src_by_id["commands"].root == "cmd"
    assert src_by_id["commands"].file_globs == ("cmd/**/*.go",)
    assert src_by_id["internal"].root == "internal"
    assert src_by_id["internal"].file_globs == ("internal/**/*.go",)

    test_by_id = {t.id: t for t in go.layout.test_sets}
    assert set(test_by_id) == {"colocated", "e2e"}
    assert test_by_id["colocated"].root == "."
    assert test_by_id["colocated"].file_globs == ("**/*_test.go",)
    assert test_by_id["colocated"].colocated is True
    assert test_by_id["e2e"].root == "tests"
    assert test_by_id["e2e"].optional is True


def test_go_package_root_is_none(registry: LanguageRegistry) -> None:
    go = registry.resolve("go")
    assert isinstance(go.layout.package_root, PackageRoot)
    assert go.layout.package_root.kind == "none"
    assert go.layout.package_root.path is None


def test_go_is_strict(registry: LanguageRegistry) -> None:
    go = registry.resolve("go")
    assert go.identity.strictness == "strict"
    assert "golang" in go.identity.aliases


def test_go_gosum_is_checksum_not_lock(registry: LanguageRegistry) -> None:
    """go.sum must be a checksum/dependency-integrity file, NOT a lock file."""
    go = registry.resolve("go")
    assert go.toolchain is not None
    integrity = {f.path: f for f in go.toolchain.dependency_integrity_files}
    assert "go.sum" in integrity
    assert integrity["go.sum"].kind == "checksum"
    assert integrity["go.sum"].required is False


def test_go_commands_use_go_tool(registry: LanguageRegistry) -> None:
    go = registry.resolve("go")
    for name in ("build", "typecheck", "vet", "verify"):
        cmd = go.commands[name]
        assert isinstance(cmd, CommandSpec)
        assert cmd.argv[0] in ("go", "gofmt")
    # verify emits a go-test-json report
    assert go.commands["verify"].argv[:3] == ("go", "test", "-json")
    assert go.commands["verify"].report is not None
    assert go.commands["verify"].report.adapter == "go-test-json"


# ---------------------------------------------------------------------------
# Python / TypeScript: DO retain src/
# ---------------------------------------------------------------------------


def test_python_retains_src_layout(registry: LanguageRegistry) -> None:
    py = registry.resolve("python")
    assert py.identity.strictness == "legacy_compatible"

    src_by_id = {s.id: s for s in py.layout.source_sets}
    assert "package" in src_by_id
    assert src_by_id["package"].root == "src/{package_name}"  # placeholder kept literal
    assert any(_contains_src(g) for g in src_by_id["package"].file_globs)

    assert py.layout.package_root.kind == "named_package"
    assert py.layout.package_root.path == "src/{package_name}"

    test_by_id = {t.id: t for t in py.layout.test_sets}
    assert test_by_id["tests"].root == "tests"

    assert py.toolchain is not None
    assert py.toolchain.manifest.path == "pyproject.toml"


def test_typescript_retains_src_layout(registry: LanguageRegistry) -> None:
    ts = registry.resolve("typescript")
    assert ts.identity.strictness == "legacy_compatible"

    src_by_id = {s.id: s for s in ts.layout.source_sets}
    assert src_by_id["src"].root == "src"
    assert src_by_id["src"].file_globs == ("src/**/*.ts",)

    assert ts.layout.package_root.kind == "path_root"
    assert ts.layout.package_root.path == "src"

    test_by_id = {t.id: t for t in ts.layout.test_sets}
    assert test_by_id["tests"].file_globs == (
        "tests/**/*.test.ts",
        "tests/**/*.spec.ts",
    )

    assert ts.toolchain is not None
    assert ts.toolchain.manifest.path == "package.json"
    lock = {f.path: f for f in ts.toolchain.dependency_integrity_files}
    assert "package-lock.json" in lock
    assert lock["package-lock.json"].kind == "lock"


# ---------------------------------------------------------------------------
# model round-trip / placeholder preservation
# ---------------------------------------------------------------------------


def test_loader_produces_expected_dataclass_shapes() -> None:
    profile = load_language_profile(PROFILES_DIR / "go.yaml")
    assert isinstance(profile, LanguageProfile)
    assert all(isinstance(s, SourceSet) for s in profile.layout.source_sets)
    assert all(isinstance(t, TestSet) for t in profile.layout.test_sets)
    assert all(isinstance(c, CommandSpec) for c in profile.commands.values())
    # there is NO single source_root attribute on the model — source is a SET
    assert not hasattr(profile.layout, "source_root")
    assert isinstance(profile.layout.source_sets, tuple)


def test_profile_is_immutable() -> None:
    profile = load_language_profile(PROFILES_DIR / "python.yaml")
    with pytest.raises(Exception):
        profile.layout.source_sets = ()  # type: ignore[misc]


def test_placeholders_left_literal() -> None:
    """{package_name} etc. must NOT be substituted in Phase 1."""
    py = load_language_profile(PROFILES_DIR / "python.yaml")
    assert "{package_name}" in py.layout.package_root.path
    pkg = {s.id: s for s in py.layout.source_sets}["package"]
    assert "{package_name}" in pkg.root

    go = load_language_profile(PROFILES_DIR / "go.yaml")
    # module_path placeholder preserved in imports data / scaffold templates
    assert go.scaffold is not None
    template_blob = "".join(
        str(t.get("content_template", "")) for t in go.scaffold.templates
    )
    assert "{module_path}" in template_blob


def test_raw_and_extra_preserve_unmodeled_sections() -> None:
    go = load_language_profile(PROFILES_DIR / "go.yaml")
    # path_rules is still not modeled -> kept in .extra.
    assert "path_rules" in go.extra
    # implement_oracle is now a first-class field (Contract Kernel §1), so it is
    # NO LONGER left in .extra; it parses into the modeled spec instead.
    assert "implement_oracle" not in go.extra
    assert go.implement_oracle is not None
    assert go.implement_oracle.kind == "composite"
    # .raw is the whole document (the raw block is still present there).
    assert go.raw["id"] == "go"
    assert go.raw["implement_oracle"]["kind"] == "composite"


# ---------------------------------------------------------------------------
# AdapterRegistry stub
# ---------------------------------------------------------------------------


def test_adapter_registry_register_and_require() -> None:
    reg = AdapterRegistry()
    sentinel = object()
    reg.register("test_semantics", "go-test-semantics", sentinel)
    assert reg.require("test_semantics", "go-test-semantics") is sentinel
    assert ("test_semantics", "go-test-semantics") in reg


def test_adapter_registry_require_missing_raises() -> None:
    reg = AdapterRegistry()
    with pytest.raises(KeyError):
        reg.require("runner_report", "nonexistent")


# ---------------------------------------------------------------------------
# bundled-files sanity: every shipped profile loads
# ---------------------------------------------------------------------------


def test_every_bundled_profile_loads() -> None:
    yaml_files = sorted(Path(PROFILES_DIR).glob("*.yaml"))
    assert len(yaml_files) >= 3
    for yf in yaml_files:
        profile = load_language_profile(yf)
        assert profile.identity.id
        assert profile.layout.source_sets  # every profile declares >=1 source set


# ---------------------------------------------------------------------------
# tests.framework — test-authoring ground truth for generation prompts
# (2026-07-03: TS ExprCalc greenfield dogfood — the AI wrote tests against
# Node's built-in ``node:test`` because nothing declared the project's ACTUAL
# test runner; the TS profile's own scaffolded ``commands.verify`` always
# runs Vitest, which does not collect ``node:test``-style files. See
# ``TestFrameworkSpec`` in codd/languages/profile.py for the full incident.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang_id, expected_name",
    [
        ("python", "pytest"),
        ("typescript", "Vitest"),
        ("javascript", "Vitest"),
        ("go", "Go testing (standard library)"),
        ("java", "JUnit 5 (Jupiter)"),
        ("csharp", "xUnit.net"),
        ("cpp", "GoogleTest"),
    ],
)
def test_every_bundled_profile_declares_test_framework(
    registry: LanguageRegistry, lang_id: str, expected_name: str
) -> None:
    """Every one of the 7 bundled profiles declares its ACTUALLY-executed test
    framework (grounded in each profile's own ``commands.verify``/``scaffold``,
    never a menu of merely-possible libraries)."""
    profile = registry.resolve(lang_id)
    assert profile.tests is not None
    assert profile.tests.framework is not None
    assert profile.tests.framework.name == expected_name
    assert profile.tests.framework.example.strip()


def test_test_framework_missing_name_raises() -> None:
    from codd.languages.loader import LanguageProfileError, _parse_test_framework

    with pytest.raises(LanguageProfileError):
        _parse_test_framework({"example": "some example"})


def test_test_framework_missing_example_raises() -> None:
    from codd.languages.loader import LanguageProfileError, _parse_test_framework

    with pytest.raises(LanguageProfileError):
        _parse_test_framework({"name": "Vitest"})


def test_test_framework_absent_is_none() -> None:
    from codd.languages.loader import _parse_test_framework

    assert _parse_test_framework(None) is None


def test_profile_without_declared_framework_has_none() -> None:
    """A profile that hasn't declared ``tests.framework`` yet must expose
    ``None`` — never a fabricated guess — so callers degrade gracefully."""
    from codd.languages.profile import TestsSpec

    bare = TestsSpec(semantics_adapter="whatever")
    assert bare.framework is None

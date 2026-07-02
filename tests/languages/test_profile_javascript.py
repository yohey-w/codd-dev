"""Tests for the JavaScript language profile (javascript.yaml), modeled on
``tests/languages/test_profile_cpp.py``.

Asserts the profile loads, is auto-discovered + resolvable by id, carries the
load-bearing shape (plain src/ + tests/ layout, no ``.ts``/``.tsx`` anywhere),
declares a ``kind=adapter`` implement-oracle (``javascript-composite`` — plain
JS has no compiler/type-checker, so this is an in-process ``node --check`` +
first-party import/export resolver composite, NOT a tsc-shaped command/
composite; see ``codd/languages/adapters/oracle_javascript.py``), and wires a
vitest verify campaign whose report adapter is ``vitest-json`` (the SAME
adapter TypeScript uses). It does NOT assert the language CONTRACT is
complete — ``vitest-json`` is registered centrally by the parent, not in this
task.

Also asserts the registry-shadowing design this profile depends on: adding
``id: javascript`` here must NOT require any edit to typescript.yaml. The
existing ``ts``/``js``/``node`` aliases must keep resolving to the canonical
TypeScript profile exactly as before (only the exact string ``"javascript"``
moves, via the registry's id fast-path taking precedence over the alias scan).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.coverage_execution_coherence import coherence_gate_applies
from codd.languages import (
    CommandSpec,
    LanguageProfile,
    LanguageRegistry,
    PackageRoot,
    SourceSet,
    TestSet,
    load_language_profile,
)
from codd.languages.registry import PROFILES_DIR
from codd.project_types import resolve_layout_profile, scaffold_layout
from codd.vb_marker_authenticity import TypeScriptTestBlockProfile


@pytest.fixture(scope="module")
def registry() -> LanguageRegistry:
    return LanguageRegistry()


# ── discovery / resolution ────────────────────────────────────────────────────


def test_javascript_profile_is_discovered(registry: LanguageRegistry) -> None:
    assert "javascript" in set(registry.ids())


def test_javascript_resolves_by_id(registry: LanguageRegistry) -> None:
    profile = registry.resolve("javascript")
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == "javascript"


def test_javascript_declares_no_aliases(registry: LanguageRegistry) -> None:
    """This profile deliberately claims ONLY the exact id string.

    Declaring ``js``/``ts`` here too would create a genuine cross-profile alias
    collision with typescript.yaml (both would then claim the same alias, and
    the winner would depend on profiles-directory filename sort order — a
    latent, fragile ambiguity). The registry has no cross-profile alias-
    uniqueness check, so staying alias-free is what keeps this addition clean.
    """
    js = registry.resolve("javascript")
    assert js.identity.aliases == ()


@pytest.mark.parametrize("still_typescript", ["ts", "js", "node", "TypeScript", "  Node  "])
def test_typescript_aliases_are_unaffected_by_the_new_profile(
    registry: LanguageRegistry, still_typescript: str
) -> None:
    """The historical TS alias surface (``ts``/``js``/``node``) is UNCHANGED.

    Only the exact string ``"javascript"`` moves to the new profile (via the
    registry's id-fast-path); every other alias keeps resolving to the
    canonical TypeScript profile exactly as before this profile was added.
    """
    assert registry.resolve(still_typescript).id == "typescript"


def test_javascript_id_shadows_the_typescript_alias_entry(registry: LanguageRegistry) -> None:
    """``"javascript"`` now resolves to THIS profile, not typescript.yaml.

    typescript.yaml's own ``aliases:`` list still textually contains
    ``"javascript"`` (intentionally left untouched — out of scope for this
    change), but it is now unreachable for that one string: the registry's
    ``resolve()`` checks the exact-id dict FIRST, so a distinct profile
    claiming ``javascript`` as its ``id`` always wins over another profile's
    alias of the same name.
    """
    js = registry.resolve("javascript")
    ts = registry.resolve("typescript")
    assert js.identity.id == "javascript"
    assert ts.identity.id == "typescript"
    assert js is not ts
    assert js.raw != ts.raw
    # Both now declare a REAL implement-oracle, but distinct ones: TS's is a
    # tsc-shaped kind=command (a static type-checker); JS's is a kind=adapter
    # in-process composite (node --check + import/export resolution — no type
    # information exists for JS, so it never claims to be tsc-equivalent).
    assert js.implement_oracle is not None
    assert ts.implement_oracle is not None
    assert js.implement_oracle.kind == "adapter"
    assert js.implement_oracle.adapter == "javascript-composite"
    assert ts.implement_oracle.kind == "command"
    assert ts.implement_oracle.adapter == "typescript-tsc"


# ── identity / strictness / extensions ────────────────────────────────────────


def test_javascript_is_strict(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")
    assert js.identity.strictness == "strict"


def test_javascript_file_extensions_have_no_typescript_extensions(
    registry: LanguageRegistry,
) -> None:
    js = registry.resolve("javascript")
    exts = set(js.identity.file_extensions)
    assert {".js", ".jsx"} <= exts, exts
    assert ".ts" not in exts and ".tsx" not in exts, exts


# ── layout: plain src/ + tests/, no .ts anywhere ──────────────────────────────


def test_javascript_module_root_and_manifest(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")
    assert js.layout.module_root == "."
    assert js.toolchain is not None
    assert js.toolchain.manifest.path == "package.json"
    assert js.toolchain.manifest.format == "package.json"
    assert js.toolchain.manifest.required is True


def test_javascript_source_and_test_sets_shape(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")

    src_by_id = {s.id: s for s in js.layout.source_sets}
    assert set(src_by_id) == {"src"}
    assert src_by_id["src"].root == "src"
    for glob in src_by_id["src"].file_globs:
        assert ".ts" not in glob, glob

    test_by_id = {t.id: t for t in js.layout.test_sets}
    assert set(test_by_id) == {"tests"}
    assert test_by_id["tests"].root == "tests"
    assert "tests/**/*.test.js" in test_by_id["tests"].file_globs
    assert "tests/**/*.spec.js" in test_by_id["tests"].file_globs
    for glob in test_by_id["tests"].file_globs:
        assert ".ts" not in glob, glob


def test_javascript_package_root_is_flat_path_root(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")
    assert isinstance(js.layout.package_root, PackageRoot)
    assert js.layout.package_root.kind == "path_root"
    assert js.layout.package_root.path == "src"


# ── commands + the in-process (not tsc-shaped) implement-oracle ──────────────


def test_javascript_verify_command_uses_vitest_without_tsc(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")
    argv = js.commands["verify"].argv
    assert argv[0] == "npx"
    assert "vitest" in argv
    assert "tsc" not in argv
    assert not any("tsc" in str(a) for a in argv)


def test_javascript_implement_oracle_is_an_in_process_composite_not_tsc(
    registry: LanguageRegistry,
) -> None:
    """Plain JavaScript has no compiler/type-checker, so its oracle is NOT a
    tsc-shaped ``kind=command``/``composite`` — it is the SAME in-process
    ``kind=adapter`` shape Python's composite uses (node --check + a
    first-party import/export resolver; see
    codd/languages/adapters/oracle_javascript.py). This closes the
    "declared but UNSUPPORTED" gate RED a fully-absent implement_oracle used
    to leave open — every other anti-false-green surface (lock-freshness,
    coverage-execution-coherence, VB marker-authenticity) still applies too
    (see the tests below), this is a THIRD, independent one.
    """
    js = registry.resolve("javascript")
    assert js.implement_oracle is not None
    assert js.implement_oracle.kind == "adapter"
    assert js.implement_oracle.adapter == "javascript-composite"
    assert js.implement_oracle.command is None
    assert js.implement_oracle.steps == ()


def test_javascript_verify_report_adapter_is_vitest_json(registry: LanguageRegistry) -> None:
    js = registry.resolve("javascript")
    assert js.commands["verify"].report is not None
    assert js.commands["verify"].report.adapter == "vitest-json"
    assert js.commands["verify"].report.format == "vitest-json"
    assert js.verify is not None
    assert js.verify.report is not None
    assert js.verify.report.adapter == "vitest-json"
    assert js.tests is not None
    assert js.tests.runner_report_adapter == "vitest-json"
    assert js.tests.semantics_adapter == "typescript-test-semantics"


# ── model round-trip via the loader ───────────────────────────────────────────


def test_javascript_loader_produces_expected_dataclass_shapes() -> None:
    profile = load_language_profile(PROFILES_DIR / "javascript.yaml")
    assert isinstance(profile, LanguageProfile)
    assert all(isinstance(s, SourceSet) for s in profile.layout.source_sets)
    assert all(isinstance(t, TestSet) for t in profile.layout.test_sets)
    assert all(isinstance(c, CommandSpec) for c in profile.commands.values())
    assert not hasattr(profile.layout, "source_root")


def test_javascript_raw_id_and_no_legacy_bridge() -> None:
    js = load_language_profile(PROFILES_DIR / "javascript.yaml")
    assert js.raw["id"] == "javascript"
    # Deliberately NOT bridged to the legacy dict-dispatch path (that bridge is
    # typescript.yaml's historical boundary) — javascript.yaml relies solely on
    # the modern greenfield_synthesis opt-in.
    assert "legacy_project_types" not in js.extra


# ── greenfield ② OPT-IN synthesis + anti-false-green gates ────────────────────


def test_javascript_greenfield_synthesis_opted_in() -> None:
    """javascript.yaml declares greenfield_synthesis: true — the SAME data-driven
    opt-in key java.yaml/csharp.yaml/cpp.yaml use; the language-free core
    authorizes the generic LayoutProfile synthesizer + generic-template
    scaffolder by the PRESENCE of this key alone, never a language-name
    branch."""
    js = load_language_profile(PROFILES_DIR / "javascript.yaml")
    assert js.raw.get("greenfield_synthesis") is True


def test_javascript_coherence_gate_applies_after_opt_in() -> None:
    """resolve_layout_profile('javascript') now synthesizes a real LayoutProfile
    (was None before this profile existed — a strict NO-OP, since "javascript"
    used to resolve to typescript.yaml, whose legacy bridge does NOT accept the
    "javascript" name), and the coverage-execution-coherence gate applies."""
    profile = resolve_layout_profile(language="javascript", project_name="todo-cli")
    assert profile is not None
    assert profile.language == "javascript"
    assert coherence_gate_applies(profile) is True


def test_javascript_test_block_profile_reuses_typescript_parser() -> None:
    """LayoutProfile.test_block_profile() resolves to the ALREADY-EXISTING
    TypeScriptTestBlockProfile (codd/vb_marker_authenticity.py) for a
    synthesized javascript layout profile — no new adapter class is needed.
    Verified (by reading the adapter) to be extension-agnostic: its
    ``handles_file`` matches .js/.jsx/.mjs/.cjs (not just .ts/.tsx), and its
    specifier resolver tries the literal given suffix first, so it never
    assumes a .ts sibling for a .js import. Without this wiring the VB
    marker-authenticity gate would silently degrade to its language-agnostic
    stage-1 (orphan-marker) check only, for every JavaScript project."""
    profile = resolve_layout_profile(language="javascript", project_name="todo-cli")
    assert profile is not None
    tb = profile.test_block_profile()
    assert isinstance(tb, TypeScriptTestBlockProfile)
    assert tb.handles_file("tests/foo.test.js")
    assert tb.handles_file("tests/foo.spec.jsx")
    assert not tb.handles_file("tests/foo.py")


def test_javascript_toolchain_dependencies_synthesize_real_lock_freshness() -> None:
    """UNLIKE java/csharp/cpp (which correctly leave dependency_integrity_files
    empty — an honest NO-OP, since none of those ecosystems has a universal
    lock file), javascript.yaml DOES get real (non-NO-OP) manifest<->lock
    coherence: npm's package-lock.json is universal, and the declared
    reconcile/materialize commands are the EXACT SAME proven npm commands
    typescript.yaml's legacy builder already runs in production."""
    profile = resolve_layout_profile(language="javascript", project_name="todo-cli")
    assert profile is not None
    toolchain = profile.toolchain_dependencies
    assert toolchain is not None
    assert toolchain.manifest_filename == "package.json"
    assert toolchain.lock_filenames == ("package-lock.json",)
    assert toolchain.lock_refresh_command == "npm install --package-lock-only"
    assert toolchain.materialize_command == "npm ci"
    assert toolchain.frozen_install_command == "npm ci"


# ── scaffold: a real, minimal, running Node package (no TypeScript) ──────────


def test_javascript_scaffolds_package_json_with_no_typescript(tmp_path: Path) -> None:
    profile = resolve_layout_profile(language="javascript", project_name="todo-cli")
    assert profile is not None

    result = scaffold_layout(tmp_path, profile)
    package_json = tmp_path / "package.json"
    assert package_json.is_file()
    assert "package.json" in result.created

    body = package_json.read_text(encoding="utf-8")

    # (a) no UNSUBSTITUTED template var leaked into the rendered file.
    for var in ("{package_name}", "{vitest_version}"):
        assert var not in body, var

    # (b) valid, parseable JSON — a real, minimal, running Node package.
    payload = json.loads(body)
    assert payload["name"] == "todo_cli"
    assert payload["scripts"]["test"] == "vitest run"
    assert "vitest" in payload["devDependencies"]

    # (c) anti-false-green: NO TypeScript tooling anywhere in the scaffold.
    assert "typescript" not in payload.get("devDependencies", {})
    assert "typescript" not in payload.get("dependencies", {})
    assert not (tmp_path / "tsconfig.json").exists()

    # (d) idempotent: a second call creates nothing, skips the existing file.
    result2 = scaffold_layout(tmp_path, profile)
    assert result2.created == ()
    assert "package.json" in result2.skipped

    # (e) non-clobber: an authored file is left byte-for-byte.
    package_json.write_text("AUTHORED", encoding="utf-8")
    scaffold_layout(tmp_path, profile)
    assert package_json.read_text(encoding="utf-8") == "AUTHORED"


def test_javascript_harness_owned_scaffold_paths_include_manifest_and_lock() -> None:
    profile = resolve_layout_profile(language="javascript", project_name="todo-cli")
    assert profile is not None
    assert profile.harness_owned_scaffold_paths() == ("package.json", "package-lock.json")

"""STEP 0 parity oracle for the Contract Kernel v2.71 de-literalization.

The v2.71 increment removes the language-NAME literal DISPATCH from
``codd/project_types.py`` (``self.language == "python"`` /
``in ("typescript","node")`` / ``in ("go","golang")`` and the
``_LAYOUT_PROFILE_BUILDERS`` / ``_TEST_RUNNER_ENSURERS`` dicts keyed by language
name) so layout + scaffold + test-block decisions become PROFILE-DRIVEN (read
from the Contract Kernel's resolved ``LanguageProfile``) instead of branching on
a language string.

The change is STRUCTURAL, not behavioral: it MUST be byte-identical for the three
supported languages. This module pins the CURRENT resolved values BEFORE the
refactor (it passes GREEN against today's literal-dispatch code) so that after the
de-literalization the same assertions prove byte-identical behavior. It is the
parity oracle the v2.71 exit gate ("behavior-preserving for go/python/typescript")
is verified against.

Anti-false-green: this also pins the CONSERVATIVE degradations — a declared-but-
unknown language has NO layout profile (``None``) and NO scaffolder (a no-op
``ScaffoldResult``), and a directly-constructed ``go`` ``LayoutProfile`` resolves
the Go test-block parser but no scaffold (no Go scaffolder today) — so the
refactor cannot silently turn an unknown/unsupported stack into a wrong-layout
green.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from codd.project_types import (
    LayoutProfile,
    ScaffoldResult,
    ensure_test_runner_config,
    resolve_layout_profile,
    scaffold_layout,
    supported_layout_profile_languages,
    supported_test_runner_languages,
)


def _profile(language: str, name: str = "todo-cli") -> LayoutProfile | None:
    return resolve_layout_profile(
        language=language,
        project_name=name,
        source_dirs=["src/"],
        test_dirs=["tests/"],
    )


# ── byte-identical layout RESOLUTION (the builder outputs) ───────────────────
#
# These are the exact values each ``self.language ==`` builder / dict entry
# produces today. ``node`` is an ALIAS that resolves to the SAME TypeScript
# profile (canonical ``.language == "typescript"`` — the builder normalizes the
# alias to the canonical id), which the de-literalized registry dispatch MUST
# preserve.


class TestLayoutResolutionParity:
    def test_python_layout_triple_and_policy(self):
        p = _profile("python")
        assert p is not None
        assert p.language == "python"
        assert p.package_name == "todo_cli"
        assert p.source_root == "src"
        assert p.package_root == "src/todo_cli"
        assert p.test_root == "tests"
        assert p.runner == "pytest"
        assert p.install_mode == "editable"
        assert p.test_import_policy == "package_absolute"
        assert p.requires_package_init is True
        assert p.requires_test_init is True
        # policy specs the builder attaches (composite oracle; no toolchain/campaign).
        assert p.implement_oracle is not None
        assert p.implement_oracle.kind == "composite"
        assert p.implement_oracle.command == "python-composite"
        assert p.toolchain_dependencies is None
        assert p.verify_campaign is None

    def test_typescript_layout_triple_and_policy(self):
        p = _profile("typescript")
        assert p is not None
        assert p.language == "typescript"
        assert p.source_root == "src"
        assert p.package_root == "src"
        assert p.test_root == "tests"
        assert p.runner == "vitest"
        assert p.install_mode == "node"
        assert p.test_import_policy == "relative"
        assert p.requires_package_init is False
        assert p.requires_test_init is False
        assert p.implement_oracle is not None
        assert p.implement_oracle.kind == "compiler"
        assert p.implement_oracle.command == "npx --no-install tsc --noEmit"
        # the TS toolchain profile + the vitest verify campaign are attached.
        assert p.toolchain_dependencies is not None
        dep_names = {d.name for d in p.toolchain_dependencies.deps}
        assert dep_names == {"vitest", "typescript", "@types/node"}
        assert p.verify_campaign is not None
        assert p.verify_campaign.report_format == "vitest-json"

    def test_node_alias_resolves_canonical_typescript_profile(self):
        # The whole-program contract: ``node`` (and the other TS aliases) MUST
        # resolve to the canonical TypeScript profile, with ``.language`` reported
        # as the canonical ``typescript`` — never the alias. Many callers key the
        # downstream layout/scaffold/runner on ``profile.language``.
        node = _profile("node")
        ts = _profile("typescript")
        assert node is not None and ts is not None
        assert node.language == "typescript"
        assert node.to_dict() == ts.to_dict()

    def test_paths_derive_from_scan_dirs_not_a_language_literal(self):
        p = _profile("python", name="my-app")
        # resolve with non-default dirs to prove the roots come from scan.*_dirs.
        p2 = resolve_layout_profile(
            language="python", project_name="my-app", source_dirs=["lib/"], test_dirs=["spec/"]
        )
        assert p2 is not None
        assert p2.source_root == "lib"
        assert p2.package_root == "lib/my_app"
        assert p2.test_root == "spec"

    def test_unknown_language_has_no_layout_profile(self):
        # Conservative degradation: a declared-but-unknown language resolves to no
        # profile (None) — never a silent wrong-layout default.
        assert _profile("rust") is None
        assert resolve_layout_profile(language=None, project_name="x") is None

    def test_supported_layout_languages_are_exactly_the_legacy_set(self):
        # Byte-identical: the de-literalization MUST NOT widen the accepted set to the
        # full registry alias matrix (the false-green risk GPT flagged — a previously
        # unsupported stack suddenly getting a TS runner). Exactly the legacy dict keys.
        assert supported_layout_profile_languages() == ["node", "python", "typescript"]

    def test_supported_test_runner_languages_are_exactly_the_legacy_set(self):
        assert supported_test_runner_languages() == ["node", "python", "typescript"]

    @pytest.mark.parametrize(
        "runtime_name,supported",
        [
            ("python", True),
            ("py", False),  # registry alias, NOT a legacy-accepted name
            ("python3", False),
            ("typescript", True),
            ("node", True),
            # "javascript" is now its OWN profile (javascript.yaml, id: javascript,
            # greenfield_synthesis: true) — resolve_layout_profile synthesizes a
            # REAL LayoutProfile for it via the generic (non-legacy) synthesizer.
            # It is still NOT in typescript.yaml's legacy_project_types.accepted_names
            # (that historical bridge is untouched), so this True comes ENTIRELY from
            # the opt-in synthesis path, not the legacy dict. "ts"/"js" below stay
            # False: they remain pure registry ALIASES of typescript with no distinct
            # profile of their own and no legacy-bridge acceptance.
            ("javascript", True),
            ("ts", False),
            ("js", False),
            ("go", False),  # Go has no legacy layout builder (contract path owns it)
            ("golang", False),
            ("ruby", False),
        ],
    )
    def test_resolve_layout_profile_legacy_support_matrix(self, runtime_name, supported):
        # ANTI-FALSE-GREEN (GPT-flagged): pin the EXACT accepted-name set so the
        # registry-driven resolution cannot silently start resolving wider aliases
        # (py/ts/js/golang/...) into the legacy builders — which would scaffold +
        # verify a previously-unsupported stack with the wrong policy. ("javascript"
        # above is the one documented exception: it is supported, but via the
        # separate opt-in greenfield_synthesis path, never the legacy dict.)
        profile = resolve_layout_profile(language=runtime_name, project_name="sample")
        assert (profile is not None) is supported


# ── byte-identical harness-owned SCAFFOLD PATHS ──────────────────────────────


class TestScaffoldPathsParity:
    def test_python_scaffold_paths(self):
        p = _profile("python")
        assert p.harness_owned_scaffold_paths() == (
            "pyproject.toml",
            "src/todo_cli/__init__.py",
            "src/todo_cli/__main__.py",
            "tests/__init__.py",
        )

    def test_python_scaffold_paths_derive_from_roots(self):
        p = resolve_layout_profile(
            language="python", project_name="my-app", source_dirs=["lib/"], test_dirs=["spec/"]
        )
        assert p.harness_owned_scaffold_paths() == (
            "pyproject.toml",
            "lib/my_app/__init__.py",
            "lib/my_app/__main__.py",
            "spec/__init__.py",
        )

    def test_typescript_scaffold_paths(self):
        # Order is load-bearing: toolchain manifest/lock first (already literal-
        # free), then the TS scaffold config files.
        p = _profile("typescript")
        assert p.harness_owned_scaffold_paths() == (
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "vitest.config.ts",
        )

    def test_node_alias_scaffold_paths_identical_to_typescript(self):
        assert _profile("node").harness_owned_scaffold_paths() == (
            _profile("typescript").harness_owned_scaffold_paths()
        )

    def test_go_layoutprofile_declares_no_stack_scaffold_paths(self):
        # A directly-constructed Go LayoutProfile (no toolchain) declares nothing
        # stack-specific today; the de-literalization must keep it empty (not
        # accidentally inherit python/ts paths).
        p = LayoutProfile(
            language="go", package_name="x", source_root="src", package_root="src/x", test_root="tests"
        )
        assert p.harness_owned_scaffold_paths() == ()


# ── byte-identical TEST-BLOCK profile dispatch ───────────────────────────────


class TestTestBlockProfileParity:
    @pytest.mark.parametrize(
        "language,expected",
        [
            ("python", "PythonTestBlockProfile"),
            ("typescript", "TypeScriptTestBlockProfile"),
            ("node", "TypeScriptTestBlockProfile"),
            ("javascript", "TypeScriptTestBlockProfile"),
            ("go", "GoTestBlockProfile"),
            ("golang", "GoTestBlockProfile"),
        ],
    )
    def test_test_block_profile_by_language(self, language, expected):
        # Build a LayoutProfile directly so aliases that have no layout builder
        # (javascript / golang) are still exercised — the test-block dispatch
        # historically accepts a wider alias set than the layout builders.
        p = LayoutProfile(
            language=language,
            package_name="x",
            source_root="src",
            package_root="src/x",
            test_root="tests",
        )
        tb = p.test_block_profile()
        assert tb is not None
        assert type(tb).__name__ == expected

    def test_unknown_language_has_no_test_block_profile(self):
        p = LayoutProfile(
            language="rust", package_name="x", source_root="src", package_root="src/x", test_root="tests"
        )
        assert p.test_block_profile() is None

    @pytest.mark.parametrize("alias,expected", [("py", "PythonTestBlockProfile"),
                                                ("python3", "PythonTestBlockProfile"),
                                                ("ts", "TypeScriptTestBlockProfile"),
                                                ("js", "TypeScriptTestBlockProfile")])
    def test_test_block_edge_alias_resolves_correct_parser_anti_false_green(self, alias, expected):
        """Documented BENIGN widening (Contract Kernel v2.71): the de-literalized
        ``test_block_profile`` now resolves the CORRECT parser for the wider registry
        aliases ``py``/``python3``/``ts``/``js`` (the legacy ``self.language ==``
        branch returned ``None`` for these — an arbitrary historical gap, since
        ``javascript`` *did* map to the TS parser). This is anti-false-green-POSITIVE:
        it can only make the VB authenticity gate MORE active (it never returns the
        WRONG-language parser and never silences a gate), and it is unreachable in
        production (a builder always emits a canonical ``.language``). Pinned so the
        change is intentional + a future regression to ``None`` is caught."""
        p = LayoutProfile(
            language=alias, package_name="x", source_root="src", package_root="src/x", test_root="tests"
        )
        tb = p.test_block_profile()
        assert tb is not None and type(tb).__name__ == expected


# ── byte-identical SCAFFOLD execution (which scaffolder runs + what it writes) ─


class TestScaffoldExecutionParity:
    def test_python_scaffold_creates_package_topology(self, tmp_path):
        p = _profile("python")
        r = scaffold_layout(tmp_path, p)
        assert r.language == "python"
        assert sorted(r.created) == [
            "pyproject.toml",
            "src/todo_cli/__init__.py",
            "src/todo_cli/__main__.py",
            "tests/__init__.py",
        ]

    def test_typescript_scaffold_creates_config(self, tmp_path):
        p = _profile("typescript")
        r = scaffold_layout(tmp_path, p)
        assert r.language == "typescript"
        assert sorted(r.created) == ["package.json", "tsconfig.json", "vitest.config.ts"]

    def test_node_alias_scaffold_runs_typescript_scaffolder(self, tmp_path):
        p = _profile("node")
        r = scaffold_layout(tmp_path, p)
        # canonical language id is reported (typescript), TS files created.
        assert r.language == "typescript"
        assert sorted(r.created) == ["package.json", "tsconfig.json", "vitest.config.ts"]

    def test_unknown_stack_scaffold_is_noop(self, tmp_path):
        p = LayoutProfile(
            language="go", package_name="x", source_root="src", package_root="src/x", test_root="tests"
        )
        r = scaffold_layout(tmp_path, p)
        assert isinstance(r, ScaffoldResult)
        assert r.created == ()
        assert list(tmp_path.iterdir()) == []


# ── byte-identical test-runner ENSURER dispatch ──────────────────────────────


class TestEnsureTestRunnerParity:
    def test_python_ensurer(self, tmp_path):
        r = ensure_test_runner_config(
            tmp_path, language="python", project_name="todo-cli", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert r.language == "python"
        assert r.action == "created"

    def test_typescript_ensurer(self, tmp_path):
        r = ensure_test_runner_config(
            tmp_path, language="typescript", project_name="todo-cli", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert r.language == "typescript"
        assert r.action == "created"

    def test_node_alias_ensurer_runs_typescript(self, tmp_path):
        r = ensure_test_runner_config(
            tmp_path, language="node", project_name="todo-cli", source_dirs=["src/"], test_dirs=["tests/"]
        )
        # canonical id reported.
        assert r.language == "typescript"
        assert r.action == "created"

    def test_unknown_stack_ensurer_is_unsupported_noop(self, tmp_path):
        r = ensure_test_runner_config(
            tmp_path, language="rust", project_name="x", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert r.action == "unsupported"


# ── legacy-bridge realizer-id coherence (anti-silent-degradation) ────────────
#
# The de-literalized dispatch routes on the ``legacy_project_types.<realizer>`` ids
# DECLARED in the language YAMLs, mapped to functions by string CONSTANTS in
# project_types.py. A typo on either side would SILENTLY degrade a stack to
# "unsupported" (no builder/scaffolder/ensurer engages) — a quiet false-negative.
# These bind the two so a mismatch fails loudly.


class TestLegacyBridgeRealizerCoherence:
    def _bridge(self, language: str):
        from codd.languages.registry import default_registry

        return dict(default_registry.resolve(language).extra["legacy_project_types"])

    def test_python_bridge_realizers_are_registered(self):
        from codd import project_types as pt

        b = self._bridge("python")
        assert b["layout_builder"] in pt._LAYOUT_BUILDERS_BY_REALIZER
        assert b["test_runner_ensurer"] in pt._TEST_RUNNER_ENSURERS_BY_REALIZER
        assert b["scaffolder"] == pt._SCAFFOLDER_PY_SRC_PACKAGE

    def test_typescript_bridge_realizers_are_registered(self):
        from codd import project_types as pt

        b = self._bridge("typescript")
        assert b["layout_builder"] in pt._LAYOUT_BUILDERS_BY_REALIZER
        assert b["test_runner_ensurer"] in pt._TEST_RUNNER_ENSURERS_BY_REALIZER
        assert b["scaffolder"] == pt._SCAFFOLDER_TS_NPM

    def test_go_declares_no_legacy_bridge(self):
        # Go's coherence is owned by the contract/implement-oracle path; it must NOT
        # declare a legacy bridge (else it would be routed to a legacy builder).
        from codd.languages.registry import default_registry

        extra = default_registry.resolve("go").extra
        assert "legacy_project_types" not in extra

    def test_every_registered_realizer_is_declared_by_some_profile(self):
        # Reverse drift guard: a realizer registered in core but declared by NO
        # profile is dead code (or a renamed-in-core/not-in-YAML mismatch).
        from codd import project_types as pt
        from codd.languages.registry import default_registry

        declared_layout: set[str] = set()
        declared_ensurer: set[str] = set()
        for profile in default_registry.all_profiles():
            block = profile.extra.get("legacy_project_types")
            if isinstance(block, dict):
                if block.get("layout_builder"):
                    declared_layout.add(str(block["layout_builder"]))
                if block.get("test_runner_ensurer"):
                    declared_ensurer.add(str(block["test_runner_ensurer"]))
        assert set(pt._LAYOUT_BUILDERS_BY_REALIZER) == declared_layout
        assert set(pt._TEST_RUNNER_ENSURERS_BY_REALIZER) == declared_ensurer

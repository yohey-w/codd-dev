"""STEP-0 byte-identical parity oracle for the LAST 4 Cut Condition A zones.

Contract Kernel Cut Condition A — the FINAL forbidden-zone files being
de-literalized so the static language-free gate is complete:

  * ``codd/repair_slice.py``         — the repair-slice symbol/line-range +
    raises analyzer (tree-sitter vs regex fallback; the python group-index).
  * ``codd/implementer.py``          — generation-time is_python + the
    UI-facing/extension decision.
  * ``codd/e2e_harness.py``          — the e2e ``is_python`` modality routing.
  * ``codd/vb_marker_authenticity.py`` — the poetry-manifest reserved-key filter
    (a green-gate input ⇒ EXTRA byte-identity care).

The de-literalization is STRUCTURAL (drive decisions from the resolved language
PROFILE data / the parsing registry), NOT a behavior change: every assertion
here MUST stay byte-identical before AND after the refactor. The values are
snapshotted from the pre-refactor engine; if any changes, the refactor altered
behavior and is WRONG.

ONE deliberate, task-mandated EXCEPTION (NOT a parity violation): the
``repair_slice`` ``ext._get_parser()`` latent bug. ``TreeSitterExtractor`` exposes
``_parse`` (a method) / ``_parser`` (the attribute) — there is NO ``_get_parser``,
so every tree-sitter path raised ``AttributeError`` and silently fell back to
regex. Fixing it ACTIVATES the tree-sitter walk for the languages whose
``get_extractor`` returns a ``TreeSitterExtractor`` (typescript/javascript). To
keep the parity oracle honest, the repair_slice parity here pins the
DE-LITERALIZED building blocks DIRECTLY at the unit level — the regex-fallback
bodies (python/other) AND the tree-sitter walk bodies (``_walk_*``) — so
byte-identity of the *de-literalization* is proven independently of which
backend the (now-fixed) dispatch selects. The end-to-end ``TestGetParserBugFix``
section documents the intended pre→post behavior delta.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codd import repair_slice as rs_mod
from codd.repair_slice import (
    _extract_line_ranges_regex,
    _extract_raises_regex,
    extract_function_line_ranges,
    extract_raises,
)


# ─────────────────────────────────────────────────────────────────────────────
# repair_slice.py — REGEX-fallback bodies (python branch + the non-python else).
# These bodies are byte-identical pre/post de-literalization regardless of the
# bug fix (the regex path is what python ALWAYS uses, since get_extractor returns
# a PythonAstExtractor, not a TreeSitterExtractor; and what every non-tree-sitter
# language uses).
# ─────────────────────────────────────────────────────────────────────────────

_PY_SRC = textwrap.dedent(
    """\
    def foo():
        return 1

    async def bar(x, y):
        raise ValueError("bad")
        return 2

    class MyClass:
        def method(self):
            raise TypeError("nope")

        def _private(self):
            return 3
    """
)

_TS_SRC = textwrap.dedent(
    """\
    export function topFn(a: number): number {
      return a + 1;
    }

    function helper() {
      return 0;
    }

    class Widget {
      render(): void {
        throw new Error("boom");
      }
    }

    export const arrow = async (y) => {
      return y;
    };
    """
)

_GO_SRC = textwrap.dedent(
    """\
    func Foo() int {
        return 1
    }

    func (w *Widget) Bar() {
    }
    """
)


class TestRepairSliceRegexLineRangesParity:
    """``_extract_line_ranges_regex`` — verbatim per-language regex bodies."""

    def test_python_regex_line_ranges(self):
        # python branch: ``^(\s*)(?:async\s+)?def\s+(\w+)`` with group(2)=name;
        # end-line = (next match start - 1) or EOF (BARE name, no class scope).
        assert _extract_line_ranges_regex(_PY_SRC, "python") == {
            "foo": (1, 2),
            "bar": (3, 8),
            "method": (9, 10),
            "_private": (11, 14),
        }

    def test_typescript_regex_line_ranges(self):
        # non-python ``else`` branch: ``function NAME`` only, group(1)=name.
        assert _extract_line_ranges_regex(_TS_SRC, "typescript") == {
            "topFn": (1, 3),
            "helper": (4, 18),
        }

    def test_javascript_regex_line_ranges(self):
        assert _extract_line_ranges_regex(_TS_SRC, "javascript") == {
            "topFn": (1, 3),
            "helper": (4, 18),
        }

    def test_go_regex_line_ranges_empty(self):
        # go has no ``function`` keyword ⇒ the else-branch regex matches nothing.
        assert _extract_line_ranges_regex(_GO_SRC, "go") == {}

    def test_unknown_language_uses_else_branch(self):
        # any non-"python" language takes the SAME else branch as typescript.
        assert _extract_line_ranges_regex(_TS_SRC, "cobol") == {
            "topFn": (1, 3),
            "helper": (4, 18),
        }


class TestRepairSliceRegexRaisesParity:
    """``_extract_raises_regex`` — python-only; every other language is empty."""

    def test_python_regex_raises(self):
        assert _extract_raises_regex(_PY_SRC, "python") == {
            "bar": ["ValueError"],
            "method": ["TypeError"],
        }

    def test_typescript_regex_raises_empty(self):
        assert _extract_raises_regex(_TS_SRC, "typescript") == {}

    def test_go_regex_raises_empty(self):
        assert _extract_raises_regex(_GO_SRC, "go") == {}

    def test_unknown_language_regex_raises_empty(self):
        assert _extract_raises_regex(_PY_SRC, "ruby") == {}


class TestRepairSlicePythonEndToEndParity:
    """Public ``extract_*`` for PYTHON (always regex via PythonAstExtractor)."""

    def test_python_line_ranges(self):
        # get_extractor("python") returns a PythonAstExtractor (NOT a
        # TreeSitterExtractor), so extract_function_line_ranges takes the regex
        # fallback — bare names, end-line = next-match-start-1.
        assert extract_function_line_ranges(_PY_SRC, "a.py", "python") == {
            "foo": (1, 2),
            "bar": (3, 8),
            "method": (9, 10),
            "_private": (11, 14),
        }

    def test_python_raises(self):
        assert extract_raises(_PY_SRC, "a.py", "python") == {
            "bar": ["ValueError"],
            "method": ["TypeError"],
        }


# ─────────────────────────────────────────────────────────────────────────────
# repair_slice.py — the tree-sitter WALK bodies (``_walk_*``). These run the
# language-set membership being de-literalized (``language in
# ("typescript","javascript")`` selects the extra func node-types). They must
# stay byte-identical. We drive them through the public entrypoint with a
# language whose get_extractor returns a TreeSitterExtractor (typescript), which
# — AFTER the _get_parser bug fix — is the tree-sitter path.
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairSliceTreeSitterWalkParity:
    """The tree-sitter walk node-type SET per language family (post bug-fix).

    The de-literalized membership selects the EXTRA function node-types for the
    ts/js family (``method_definition`` + ``function_declaration`` on top of the
    base ``function_definition``). This pins the EXACT post-fix walk output —
    note the walk's class-scoping keys on the PYTHON node type
    ``class_definition`` (not TS's ``class_declaration``), so the TS method is
    UNSCOPED (``render``, not ``Widget.render``); and ``_walk_raises`` keys on
    the PYTHON ``raise_statement`` (not TS's ``throw_statement``), so TS yields
    NO raises. Those Python-shaped walk internals are OUT OF SCOPE for this Cut
    (only the language-name membership is de-literalized) and must stay verbatim.
    """

    def test_typescript_walk_node_type_set(self):
        # tree-sitter walk: function_definition (base) + (ts/js)
        # method_definition + function_declaration. ``render`` is the class
        # method, surfaced by the ts/js-only ``method_definition`` membership.
        ranges = extract_function_line_ranges(_TS_SRC, "a.ts", "typescript")
        assert ranges == {
            "topFn": (1, 3),
            "helper": (5, 7),
            "render": (10, 12),  # UNSCOPED (class_definition≠class_declaration)
        }

    def test_javascript_walk_node_type_set(self):
        ranges = extract_function_line_ranges(_TS_SRC, "a.js", "javascript")
        assert ranges == {
            "topFn": (1, 3),
            "helper": (5, 7),
            "render": (10, 12),
        }

    def test_typescript_walk_raises_empty(self):
        # ``_walk_raises`` only recognizes the python ``raise_statement`` node;
        # TS uses ``throw_statement`` ⇒ no raises captured (verbatim walk body).
        assert extract_raises(_TS_SRC, "a.ts", "typescript") == {}


# ─────────────────────────────────────────────────────────────────────────────
# repair_slice.py — the ``_get_parser`` BUG FIX (deliberate, task-mandated).
# Pre-fix: ext._get_parser() raised AttributeError → regex fallback for TS/JS.
# Post-fix: ext._parse(content) is used → real tree-sitter walk for TS/JS.
# This is the ONE intended behavior delta; it is NOT a parity regression.
# ─────────────────────────────────────────────────────────────────────────────


class TestGetParserBugFix:
    def test_tree_sitter_extractor_has_no_get_parser_method(self):
        # documents the root cause: the method the old code called never existed.
        from codd.parsing import get_extractor, TreeSitterExtractor

        ext = get_extractor("typescript")
        assert isinstance(ext, TreeSitterExtractor)
        assert not hasattr(ext, "_get_parser")
        assert hasattr(ext, "_parse")  # the real accessor the fix must use
        assert hasattr(ext, "_parser")  # the real attribute

    def test_typescript_now_uses_tree_sitter_not_regex(self):
        # The bug fix ACTIVATES the tree-sitter walk: the regex fallback could
        # only find top-level ``function`` decls (``topFn``/``helper``), so it
        # MISSED the class method ``render``. Post-fix, the tree-sitter
        # ``method_definition`` surfaces ``render`` — proof the tree-sitter path
        # is live (NOT the regex fallback, which can never reach a class method).
        ranges = extract_function_line_ranges(_TS_SRC, "a.ts", "typescript")
        assert "render" in ranges  # the regex fallback could NEVER find this
        # the regex fallback would yield exactly {topFn, helper}; tree-sitter adds render
        assert set(ranges) == {"topFn", "helper", "render"}
        assert _extract_line_ranges_regex(_TS_SRC, "typescript") == {
            "topFn": (1, 3),
            "helper": (4, 18),
        }  # what the BUGGED path produced (for contrast)


# ─────────────────────────────────────────────────────────────────────────────
# implementer.py — is_python (confusable check) + UI-facing/extension decisions.
# ─────────────────────────────────────────────────────────────────────────────


class TestImplementerLanguageDecisionsParity:
    def test_confusable_is_python_by_suffix(self):
        from codd.implementer import _confusable_code_error

        # A confusable char (Cyrillic 'а') in a .py identifier ⇒ flagged.
        bad = "def cаlc():\n    return 1\n"  # noqa: RUF001 — intentional confusable
        assert _confusable_code_error("x.py", bad, language="go") is not None
        # extensionless + language=="python" ⇒ inspected.
        assert _confusable_code_error("x", bad, language="python") is not None
        # extensionless + non-python language ⇒ skipped (None).
        assert _confusable_code_error("x", bad, language="go") is None
        # a non-.py suffix ⇒ skipped regardless of language.
        assert _confusable_code_error("x.ts", bad, language="python") is None
        # a clean .py payload ⇒ None.
        assert _confusable_code_error("x.py", "def calc():\n    return 1\n", language="python") is None

    def test_default_generated_extension_tsx_branch(self):
        from codd.implementer import _default_generated_extension

        tsx_content = "export default function Page() { return (<div/>); }"
        plain_content = "export function add(a, b) { return a + b; }"
        # ts + UI-ish content ⇒ the SECOND (tsx) extension.
        assert _default_generated_extension("typescript", tsx_content) == ".tsx"
        # ts + non-UI content ⇒ the FIRST (.ts) extension.
        assert _default_generated_extension("typescript", plain_content) == ".ts"
        # js + UI-ish content ⇒ the second (.jsx) extension.
        assert _default_generated_extension("javascript", tsx_content) == ".jsx"
        # python ⇒ only one extension ⇒ always the first.
        assert _default_generated_extension("python", tsx_content) == ".py"
        # python without content ⇒ .py.
        assert _default_generated_extension("python") == ".py"
        # go ⇒ .go (single ext).
        assert _default_generated_extension("go", tsx_content) == ".go"

    def test_candidate_generated_paths_ui_facing_tsx(self):
        from codd.implementer import ImplementSpec, _candidate_generated_paths

        spec = ImplementSpec(
            design_node="LoginPage",
            output_paths=["src/pages"],
            dependency_design_nodes=[],
        )
        design = "Render the login page UI form with a submit button."
        cands = [str(p) for p in _candidate_generated_paths(spec, "typescript", design)]
        # ts + UI-facing ⇒ an index.tsx candidate is appended (the 2nd extension).
        assert any(c.endswith("index.tsx") for c in cands)
        assert any(c.endswith("index.ts") for c in cands)

    def test_candidate_generated_paths_python_no_tsx(self):
        from codd.implementer import ImplementSpec, _candidate_generated_paths

        spec = ImplementSpec(
            design_node="LoginPage",
            output_paths=["src/pages"],
            dependency_design_nodes=[],
        )
        design = "Render the login page UI form with a submit button."
        cands = [str(p) for p in _candidate_generated_paths(spec, "python", design)]
        # python ⇒ NEVER an index.tsx candidate (single-extension family).
        assert not any(c.endswith("index.tsx") for c in cands)
        assert any(c.endswith("index.py") for c in cands)


# ─────────────────────────────────────────────────────────────────────────────
# e2e_harness.py — the is_python modality routing.
# ─────────────────────────────────────────────────────────────────────────────


def _e2e_caps(modality: str, network_surface: str = "none"):
    from codd.project_types import ProjectCapabilities

    return ProjectCapabilities(
        e2e_modality=modality,
        network_surface=network_surface,
    )


class TestE2EHarnessLanguageRoutingParity:
    def _spec(self, language: str, modality: str, network_surface: str = "none"):
        from codd.e2e_harness import resolve_e2e_harness

        caps = _e2e_caps(modality, network_surface)
        return resolve_e2e_harness(
            project_language=language,
            capabilities=caps,
            constraints=None,
        )

    def test_cli_python_ext_py(self):
        spec = self._spec("python", "cli")
        assert spec.runner == "native_cli"
        assert spec.language == "python"
        assert spec.output_ext == ".py"

    def test_cli_typescript_ext_ts(self):
        spec = self._spec("typescript", "cli")
        assert spec.runner == "native_cli"
        assert spec.language == "typescript"
        assert spec.output_ext == ".ts"

    def test_cli_go_ext_ts(self):
        # non-python ⇒ ``.ts`` (the historical "any other language" branch).
        spec = self._spec("go", "cli")
        assert spec.output_ext == ".ts"
        assert spec.language == "go"

    def test_cli_empty_language_defaults_python(self):
        spec = self._spec("", "cli")
        # empty language ⇒ is_python False ⇒ ext ".ts", but language "python".
        assert spec.language == "python"
        assert spec.output_ext == ".ts"


# ─────────────────────────────────────────────────────────────────────────────
# vb_marker_authenticity.py — the poetry-manifest reserved-key filter (GATE
# input ⇒ EXTRA byte-identity care). The ``python`` key in
# ``[tool.poetry.dependencies]`` is the interpreter pin, NOT a package, so it is
# excluded from the third-party set. Verdict must be byte-identical.
# ─────────────────────────────────────────────────────────────────────────────


class TestVbMarkerManifestDependencyParity:
    def test_poetry_python_key_excluded(self, tmp_path):
        from codd.vb_marker_authenticity import _python_manifest_top_dependencies

        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [tool.poetry.dependencies]
                python = "^3.11"
                requests = "^2.0"
                ruamel-yaml = "^0.18"
                """
            ),
            encoding="utf-8",
        )
        got = _python_manifest_top_dependencies(tmp_path)
        # ``python`` (interpreter pin) is NOT a dependency; the rest are, with the
        # ``-``→``_`` normalization variant included.
        assert "python" not in got
        assert "requests" in got
        assert "ruamel-yaml" in got
        assert "ruamel_yaml" in got

    def test_pep621_dependencies_unaffected(self, tmp_path):
        from codd.vb_marker_authenticity import _python_manifest_top_dependencies

        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [project]
                dependencies = ["requests>=2", "pyyaml"]
                """
            ),
            encoding="utf-8",
        )
        got = _python_manifest_top_dependencies(tmp_path)
        assert "requests" in got
        assert "pyyaml" in got

    def test_absent_manifest_empty(self, tmp_path):
        from codd.vb_marker_authenticity import _python_manifest_top_dependencies

        assert _python_manifest_top_dependencies(tmp_path) == frozenset()
        assert _python_manifest_top_dependencies(None) == frozenset()

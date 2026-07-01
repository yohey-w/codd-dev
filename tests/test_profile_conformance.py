"""Anti-false-green CONFORMANCE contract for language profiles (v3.0).

The v3.0 composable-profile architecture is *curated core + user-extensible*: a
user may register a new ``codd/languages/profiles/<lang>.yaml`` for a language
CoDD does not ship. Extensibility is only SAFE if every registered profile is
provably anti-false-green — the marker-authenticity gate must REJECT a test that
carries a ``codd: covers vb=`` marker yet does not actually prove the behaviour
(no assertion / vacuous constant assertion / skipped), and CREDIT a genuine
covering test.

This module enforces that contract for EVERY profile in the registry:

* ``test_every_registered_profile_has_conformance_fixtures`` fails if a new
  profile ships without fixtures (an unproven profile is an unsafe profile).
* ``test_profile_marker_anti_false_green`` fails the cardinal
  *false-green escape == 0* assertion if any profile's marker gate lets a fake
  through.

The marker gate is pure static analysis, so this runs in CI with no language
toolchain (the toolchain-dependent oracle/coverage probes live under dogfood/).
"""
from __future__ import annotations

import pytest

from codd.languages.registry import default_registry
from codd.project_types import LayoutProfile
from codd.vb_marker_authenticity import build_authenticity_report

_MARKER = "codd: covers vb=VB-01"

# Per-profile seeded fixtures. Each case is a COMPLETE test-file body with the
# marker placed where that language requires it (Go: immediately before the
# func, after the package/import header). A case named ``real_*`` MUST be
# credited; every other case is a FAKE that MUST be rejected.
CONFORMANCE_FIXTURES: dict[str, dict] = {
    "python": {
        "profile": LayoutProfile(
            language="python", package_name="app",
            source_root="src", package_root="src/app", test_root="tests",
        ),
        "filename": "test_x.py",
        "cases": {
            "fake_no_assertion": f"# {_MARKER}\ndef test_x():\n    add(2, 3)\n",
            "fake_constant_only": f"# {_MARKER}\ndef test_x():\n    assert 1 == 1\n",
            "fake_skipped": (
                f"import pytest\n# {_MARKER}\n@pytest.mark.skip\n"
                "def test_x():\n    assert add(2, 3) == 5\n"
            ),
            "real_covering": f"# {_MARKER}\ndef test_x():\n    assert add(2, 3) == 5\n",
        },
    },
    "typescript": {
        "profile": LayoutProfile(
            language="typescript", package_name="app",
            source_root="src", package_root="src", test_root="tests",
        ),
        "filename": "x.test.ts",
        "cases": {
            "fake_no_assertion": f"// {_MARKER}\nit('x', () => {{ add(2, 3); }});\n",
            "fake_constant_only": f"// {_MARKER}\nit('x', () => {{ expect(1).toBe(1); }});\n",
            "fake_skipped": f"// {_MARKER}\nit.skip('x', () => {{ expect(add(2, 3)).toBe(5); }});\n",
            "real_covering": f"// {_MARKER}\nit('x', () => {{ expect(add(2, 3)).toBe(5); }});\n",
        },
    },
    "javascript": {
        # Deliberately byte-identical vitest/jest SYNTAX to the "typescript" fixture
        # above (that fixture already uses zero TS type annotations) — the marker
        # gate's TypeScriptTestBlockProfile is verified extension-agnostic (docstring:
        # "vitest / jest structural adapter (TS + JS + JSX/TSX)"), so the ONLY
        # difference that matters here is the filename (.test.js, not .test.ts) and
        # the resolved profile (language="javascript", no implement_oracle).
        "profile": LayoutProfile(
            language="javascript", package_name="app",
            source_root="src", package_root="src", test_root="tests",
        ),
        "filename": "x.test.js",
        "cases": {
            "fake_no_assertion": f"// {_MARKER}\nit('x', () => {{ add(2, 3); }});\n",
            "fake_constant_only": f"// {_MARKER}\nit('x', () => {{ expect(1).toBe(1); }});\n",
            "fake_skipped": f"// {_MARKER}\nit.skip('x', () => {{ expect(add(2, 3)).toBe(5); }});\n",
            "real_covering": f"// {_MARKER}\nit('x', () => {{ expect(add(2, 3)).toBe(5); }});\n",
        },
    },
    "go": {
        "profile": LayoutProfile(
            language="go", package_name="app",
            source_root=".", package_root=".", test_root="tests",
        ),
        "filename": "x_test.go",
        "cases": {
            "fake_no_assertion": (
                'package x\n\nimport "testing"\n\n'
                f"// {_MARKER}\nfunc TestX(t *testing.T) {{ _ = Add(2, 3) }}\n"
            ),
            "fake_constant_only": (
                'package x\n\nimport "testing"\n\n'
                f'// {_MARKER}\nfunc TestX(t *testing.T) {{ if 1 != 1 {{ t.Fatal("x") }} }}\n'
            ),
            "fake_skipped": (
                'package x\n\nimport "testing"\n\n'
                f'// {_MARKER}\nfunc TestX(t *testing.T) {{ t.Skip(); if Add(2, 3) != 5 {{ t.Fatal("") }} }}\n'
            ),
            "real_covering": (
                'package x\n\nimport "testing"\n\n'
                f'// {_MARKER}\nfunc TestX(t *testing.T) {{ got := Add(2, 3); if got != 5 {{ t.Fatalf("bad %d", got) }} }}\n'
            ),
        },
    },
    "java": {
        "profile": LayoutProfile(
            language="java", package_name="app",
            source_root="src/main/java", package_root="src/main/java", test_root="tests",
        ),
        "filename": "XTest.java",
        "cases": {
            # @Test method, no assertion (calls add but checks nothing) → reject.
            "fake_no_assertion": (
                "import org.junit.jupiter.api.Test;\n\nclass XTest {\n"
                f"    // {_MARKER}\n    @Test\n    void x() {{\n        add(2, 3);\n    }}\n}}\n"
            ),
            # @Test method with a CONSTANT-only assertion → reject (constant_direct).
            "fake_constant_only": (
                "import org.junit.jupiter.api.Test;\n"
                "import static org.junit.jupiter.api.Assertions.assertEquals;\n\nclass XTest {\n"
                f"    // {_MARKER}\n    @Test\n    void x() {{\n        assertEquals(1, 1);\n    }}\n}}\n"
            ),
            # @Disabled @Test → reject (not executable).
            "fake_skipped": (
                "import org.junit.jupiter.api.Test;\n"
                "import org.junit.jupiter.api.Disabled;\n"
                "import static org.junit.jupiter.api.Assertions.assertEquals;\n\nclass XTest {\n"
                f"    // {_MARKER}\n    @Disabled\n    @Test\n    void x() {{\n        assertEquals(5, add(2, 3));\n    }}\n}}\n"
            ),
            # @Test with a real assertion referencing the SUT (add) → credit.
            "real_covering": (
                "import org.junit.jupiter.api.Test;\n"
                "import static org.junit.jupiter.api.Assertions.assertEquals;\n\nclass XTest {\n"
                f"    // {_MARKER}\n    @Test\n    void x() {{\n        assertEquals(5, add(2, 3));\n    }}\n}}\n"
            ),
        },
    },
    "csharp": {
        "profile": LayoutProfile(
            language="csharp", package_name="app",
            source_root="src", package_root="src", test_root="tests",
        ),
        "filename": "XTests.cs",
        "cases": {
            # [Fact] method, no assertion → reject.
            "fake_no_assertion": (
                "using Xunit;\n\npublic class XTests {\n"
                f"    // {_MARKER}\n    [Fact]\n    public void X() {{\n        Add(2, 3);\n    }}\n}}\n"
            ),
            # [Fact] with a CONSTANT-only assertion → reject (constant_direct).
            "fake_constant_only": (
                "using Xunit;\n\npublic class XTests {\n"
                f"    // {_MARKER}\n    [Fact]\n    public void X() {{\n        Assert.True(true);\n    }}\n}}\n"
            ),
            # [Fact(Skip=...)] → reject (not executable).
            "fake_skipped": (
                "using Xunit;\n\npublic class XTests {\n"
                f'    // {_MARKER}\n    [Fact(Skip="wip")]\n    public void X() {{\n        Assert.Equal(5, Add(2, 3));\n    }}\n}}\n'
            ),
            # [Fact] with a real assertion referencing the SUT (Add) → credit.
            "real_covering": (
                "using Xunit;\n\npublic class XTests {\n"
                f"    // {_MARKER}\n    [Fact]\n    public void X() {{\n        Assert.Equal(5, Add(2, 3));\n    }}\n}}\n"
            ),
        },
    },
    "cpp": {
        "profile": LayoutProfile(
            language="cpp", package_name="app",
            source_root="src", package_root="src", test_root="tests",
        ),
        "filename": "x_test.cpp",
        "cases": {
            # gtest TEST, no assertion → reject.
            "fake_no_assertion": (
                "#include <gtest/gtest.h>\n"
                f"// {_MARKER}\nTEST(MySuite, Foo) {{ add(2, 3); }}\n"
            ),
            # gtest TEST with a CONSTANT-only assertion → reject (constant_direct).
            "fake_constant_only": (
                "#include <gtest/gtest.h>\n"
                f"// {_MARKER}\nTEST(MySuite, Foo) {{ EXPECT_TRUE(true); }}\n"
            ),
            # gtest DISABLED_ prefix → reject (not executable).
            "fake_skipped": (
                "#include <gtest/gtest.h>\n"
                f"// {_MARKER}\nTEST(MySuite, DISABLED_Foo) {{ EXPECT_EQ(5, add(2, 3)); }}\n"
            ),
            # gtest TEST with a real assertion referencing the SUT (add) → credit.
            "real_covering": (
                "#include <gtest/gtest.h>\n"
                f"// {_MARKER}\nTEST(MySuite, Foo) {{ EXPECT_EQ(5, add(2, 3)); }}\n"
            ),
        },
    },
}


def _expected_pass(case_name: str) -> bool:
    """A ``real_*`` case must be credited; everything else is a fake to reject."""
    return case_name.startswith("real_")


def _registered_ids() -> list[str]:
    return sorted(default_registry.ids())


def _run_case(tmp_path, language: str, case_name: str) -> bool:
    fx = CONFORMANCE_FIXTURES[language]
    root = tmp_path / f"{language}_{case_name}"
    (root / "docs" / "test").mkdir(parents=True)
    (root / "docs" / "test" / "test_strategy.md").write_text(
        "| VB | D |\n| --- | --- |\n| VB-01 | demo |\n"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / fx["filename"]).write_text(fx["cases"][case_name])
    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=fx["profile"]
    )
    return report.passed


def test_every_registered_profile_has_conformance_fixtures():
    """A registered profile with no anti-false-green fixtures is unproven and
    therefore unsafe. Add fixtures to CONFORMANCE_FIXTURES with each new
    language profile."""
    missing = sorted(set(_registered_ids()) - set(CONFORMANCE_FIXTURES))
    assert not missing, (
        f"registered language profiles with NO anti-false-green conformance "
        f"fixtures: {missing}. Every profile MUST prove the marker gate rejects "
        f"fake (no-assertion / constant-only / skipped) coverage."
    )


@pytest.mark.parametrize("language", _registered_ids())
def test_profile_marker_anti_false_green(language, tmp_path):
    """Cardinal: false-green escape == 0 for every registered profile.

    A FAKE covering test (marker present, behaviour not actually proven) MUST be
    rejected; a real covering test MUST be credited (false-RED control)."""
    fx = CONFORMANCE_FIXTURES.get(language)
    assert fx is not None, f"no conformance fixtures for registered profile {language!r}"

    false_green_escapes = []
    false_reds = []
    for case_name in fx["cases"]:
        passed = _run_case(tmp_path, language, case_name)
        expected = _expected_pass(case_name)
        if passed and not expected:
            false_green_escapes.append(case_name)
        elif not passed and expected:
            false_reds.append(case_name)

    assert not false_green_escapes, (
        f"{language}: FALSE-GREEN ESCAPE — fake coverage credited as real: "
        f"{false_green_escapes} (CARDINAL anti-false-green violation)"
    )
    assert not false_reds, (
        f"{language}: false-RED — genuine covering test rejected: {false_reds}"
    )

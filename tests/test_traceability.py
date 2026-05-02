"""Tests for R5.1 — Test traceability (traceability.py)."""

import textwrap
from pathlib import Path

import pytest

from codd.traceability import TestCoverage, build_test_traceability
from codd.extractor import ModuleInfo, ProjectFacts, Symbol
from codd.parsing import TestInfo


# ── Helper factories ────────────────────────────────────────

def make_symbol(name: str, file: str = "src/mod.py", line: int = 1) -> Symbol:
    return Symbol(name=name, kind="function", file=file, line=line)


def make_facts(*modules: ModuleInfo) -> ProjectFacts:
    facts = ProjectFacts(language="python", source_dirs=["src"])
    for mod in modules:
        facts.modules[mod.name] = mod
    return facts


# ── Unit tests ──────────────────────────────────────────────

def test_test_coverage_dataclass_defaults():
    tc = TestCoverage(module="auth")
    assert tc.module == "auth"
    assert tc.covered_symbols == []
    assert tc.uncovered_symbols == []
    assert tc.coverage_ratio == 0.0
    assert tc.covering_tests == []


def test_build_test_traceability_all_covered(tmp_path):
    """All symbols appear in the test file → 100 % coverage."""
    (tmp_path / "src").mkdir()
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_file = test_dir / "test_auth.py"
    test_file.write_text(textwrap.dedent("""\
        from src.auth import login, logout

        def test_login():
            login("user", "pw")

        def test_logout():
            logout("user")
    """))

    mod = ModuleInfo(name="auth")
    mod.symbols = [make_symbol("login"), make_symbol("logout")]
    mod.test_details = [TestInfo(file_path="tests/test_auth.py")]

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    tc = mod.test_coverage
    assert tc is not None
    assert tc.module == "auth"
    assert set(tc.covered_symbols) == {"login", "logout"}
    assert tc.uncovered_symbols == []
    assert tc.coverage_ratio == 1.0


def test_build_test_traceability_partial_coverage(tmp_path):
    """Only some symbols appear in the test file → partial coverage."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_service.py").write_text("from service import get_user\ndef test_get():\n    get_user(1)\n")

    mod = ModuleInfo(name="service")
    mod.symbols = [make_symbol("get_user"), make_symbol("create_user"), make_symbol("delete_user")]
    mod.test_details = [TestInfo(file_path="tests/test_service.py")]

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    tc = mod.test_coverage
    assert "get_user" in tc.covered_symbols
    assert "create_user" in tc.uncovered_symbols
    assert "delete_user" in tc.uncovered_symbols
    assert round(tc.coverage_ratio, 2) == round(1 / 3, 2)


def test_build_test_traceability_no_test_files(tmp_path):
    """Module with no test files → test_coverage stays None (no symbols covered)."""
    mod = ModuleInfo(name="utils")
    mod.symbols = [make_symbol("helper")]
    mod.test_details = []  # no tests

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    # No test_details → covered set stays empty, but test_coverage is set
    tc = mod.test_coverage
    assert tc is not None
    assert tc.covered_symbols == []
    assert tc.uncovered_symbols == ["helper"]
    assert tc.coverage_ratio == 0.0
    assert tc.covering_tests == []


def test_build_test_traceability_no_symbols(tmp_path):
    """Module with no symbols → skipped entirely, test_coverage not set."""
    mod = ModuleInfo(name="empty")
    mod.symbols = []
    mod.test_details = []

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    # build_test_traceability skips modules with no symbols
    assert mod.test_coverage is None


def test_build_test_traceability_missing_test_file(tmp_path):
    """Non-existent test file path is gracefully skipped."""
    mod = ModuleInfo(name="mymod")
    mod.symbols = [make_symbol("do_thing")]
    mod.test_details = [TestInfo(file_path="tests/test_nonexistent.py")]

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    tc = mod.test_coverage
    assert tc is not None
    assert tc.covered_symbols == []
    assert "do_thing" in tc.uncovered_symbols
    # covering_tests still records the path (it was listed, even if unreadable)
    assert "tests/test_nonexistent.py" in tc.covering_tests


def test_build_test_traceability_multiple_test_files(tmp_path):
    """Two test files together cover all symbols; covering_tests deduplicates."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_a.py").write_text("from mod import alpha\ndef test_a(): alpha()\n")
    (test_dir / "test_b.py").write_text("from mod import beta\ndef test_b(): beta()\n")

    mod = ModuleInfo(name="mod")
    mod.symbols = [make_symbol("alpha"), make_symbol("beta")]
    mod.test_details = [
        TestInfo(file_path="tests/test_a.py"),
        TestInfo(file_path="tests/test_b.py"),
    ]

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    tc = mod.test_coverage
    assert set(tc.covered_symbols) == {"alpha", "beta"}
    assert tc.uncovered_symbols == []
    assert tc.coverage_ratio == 1.0
    assert len(tc.covering_tests) == 2


def test_build_test_traceability_coverage_ratio_rounded(tmp_path):
    """Coverage ratio is rounded to 2 decimal places."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    # Only 1 of 3 symbols appears → ratio = 0.33
    (test_dir / "test_mod.py").write_text("sym_a\n", encoding="utf-8")

    mod = ModuleInfo(name="mod")
    mod.symbols = [make_symbol("sym_a"), make_symbol("sym_b"), make_symbol("sym_c")]
    mod.test_details = [TestInfo(file_path="tests/test_mod.py")]

    facts = make_facts(mod)
    build_test_traceability(facts, tmp_path)

    tc = mod.test_coverage
    assert tc.coverage_ratio == 0.33


def test_build_test_traceability_multiple_modules(tmp_path):
    """build_test_traceability processes all modules independently."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_a.py").write_text("func_a\n", encoding="utf-8")
    (test_dir / "test_b.py").write_text("func_b\n")

    mod_a = ModuleInfo(name="mod_a")
    mod_a.symbols = [make_symbol("func_a")]
    mod_a.test_details = [TestInfo(file_path="tests/test_a.py")]

    mod_b = ModuleInfo(name="mod_b")
    mod_b.symbols = [make_symbol("func_b")]
    mod_b.test_details = [TestInfo(file_path="tests/test_b.py")]

    facts = make_facts(mod_a, mod_b)
    build_test_traceability(facts, tmp_path)

    assert mod_a.test_coverage.coverage_ratio == 1.0
    assert mod_b.test_coverage.coverage_ratio == 1.0

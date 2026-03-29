"""Tests for R7 — TypeScript/JavaScript call graph extraction."""

import textwrap

import pytest

from codd.extractor import CallEdge, Symbol
from codd.parsing import RegexExtractor, TreeSitterExtractor


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tree_sitter_available(language: str = "python") -> bool:
    try:
        return TreeSitterExtractor.is_available(language)
    except Exception:
        return False


def _make_symbols(*names: str, file: str = "test.py") -> list[Symbol]:
    return [Symbol(name=n, kind="function", file=file, line=1) for n in names]


# ── CallEdge dataclass ────────────────────────────────────────────────────────

def test_call_edge_fields():
    """CallEdge must expose caller, callee, call_site, and is_async."""
    edge = CallEdge(caller="module.fn", callee="other.helper", call_site="src/mod.py:42")
    assert edge.caller == "module.fn"
    assert edge.callee == "other.helper"
    assert edge.call_site == "src/mod.py:42"
    assert edge.is_async is False


def test_call_edge_is_async_flag():
    """is_async must be settable to True."""
    edge = CallEdge(caller="a", callee="b", call_site="f.py:1", is_async=True)
    assert edge.is_async is True


# ── RegexExtractor call graph ─────────────────────────────────────────────────

def test_regex_extractor_call_graph_returns_empty_python():
    """RegexExtractor must return [] for call graph on any language."""
    ext = RegexExtractor("python")
    result = ext.extract_call_graph("def foo():\n    bar()\n", "f.py", _make_symbols("bar"))
    assert result == []


def test_regex_extractor_call_graph_returns_empty_typescript():
    ext = RegexExtractor("typescript")
    result = ext.extract_call_graph("function foo() { bar(); }", "f.ts", _make_symbols("bar"))
    assert result == []


def test_regex_extractor_call_graph_returns_empty_javascript():
    ext = RegexExtractor("javascript")
    result = ext.extract_call_graph("function foo() { bar(); }", "f.js", _make_symbols("bar"))
    assert result == []


# ── TreeSitterExtractor — TypeScript/JavaScript (R7 current behavior) ─────────

@pytest.mark.skipif(
    not _tree_sitter_available("typescript"),
    reason="tree-sitter-typescript not installed",
)
def test_tree_sitter_typescript_call_graph_detects_call():
    """TS call graph must detect intra-project calls between known symbols."""
    ext = TreeSitterExtractor("typescript")
    code = textwrap.dedent("""\
        function greet(name: string): string {
            return format(name);
        }
        function format(s: string): string {
            return s.trim();
        }
    """)
    symbols = _make_symbols("greet", "format", file="greet.ts")
    result = ext.extract_call_graph(code, "greet.ts", symbols)
    assert len(result) >= 1
    callees = [e.callee for e in result]
    assert "format" in callees


@pytest.mark.skipif(
    not _tree_sitter_available("javascript"),
    reason="tree-sitter-javascript not installed",
)
def test_tree_sitter_javascript_call_graph_detects_call():
    """JS call graph must detect intra-project calls between known symbols."""
    ext = TreeSitterExtractor("javascript")
    code = textwrap.dedent("""\
        function greet(name) {
            return format(name);
        }
        function format(s) {
            return s.trim();
        }
    """)
    symbols = _make_symbols("greet", "format", file="greet.js")
    result = ext.extract_call_graph(code, "greet.js", symbols)
    assert len(result) >= 1
    callees = [e.callee for e in result]
    assert "format" in callees


# ── TreeSitterExtractor — Python call graph (existing behavior) ───────────────

@pytest.mark.skipif(
    not _tree_sitter_available("python"),
    reason="tree-sitter-python not installed",
)
def test_tree_sitter_python_call_graph_simple():
    """Python call graph must detect a known intra-project call."""
    ext = TreeSitterExtractor("python")
    code = textwrap.dedent("""\
        def helper():
            pass

        def caller():
            helper()
    """)
    symbols = _make_symbols("helper", "caller", file="mod.py")
    edges = ext.extract_call_graph(code, "mod.py", symbols)
    assert len(edges) >= 1
    callees = [e.callee for e in edges]
    assert "helper" in callees


@pytest.mark.skipif(
    not _tree_sitter_available("python"),
    reason="tree-sitter-python not installed",
)
def test_extract_python_call_graph_empty_functions():
    """Functions with no body calls must produce no edges."""
    ext = TreeSitterExtractor("python")
    code = textwrap.dedent("""\
        def foo():
            pass

        def bar():
            pass
    """)
    symbols = _make_symbols("foo", "bar", file="empty.py")
    edges = ext.extract_call_graph(code, "empty.py", symbols)
    assert edges == []

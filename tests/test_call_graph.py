"""Tests for R4.1 — Call graph extraction."""

import textwrap
from pathlib import Path

import pytest

from codd.extractor import CallEdge, ModuleInfo, ProjectFacts, Symbol, _resolve_call_graph


def _tree_sitter_available() -> bool:
    try:
        from codd.parsing import TreeSitterExtractor
        return TreeSitterExtractor.is_available("python")
    except Exception:
        return False


class TestResolveCallGraph:
    def test_resolves_bare_name_to_module(self):
        facts = ProjectFacts(language="python", source_dirs=["src"])
        auth = ModuleInfo(name="auth")
        auth.symbols = [Symbol(name="verify", kind="function", file="auth.py", line=1)]
        facts.modules["auth"] = auth

        api = ModuleInfo(name="api")
        api.symbols = [Symbol(name="handler", kind="function", file="api.py", line=1)]
        api.call_edges = [
            CallEdge(caller="handler", callee="verify", call_site="api.py:5"),
        ]
        facts.modules["api"] = api

        _resolve_call_graph(facts)

        assert api.call_edges[0].callee == "auth.verify"

    def test_self_calls_stay_local(self):
        facts = ProjectFacts(language="python", source_dirs=["src"])
        mod = ModuleInfo(name="service")
        mod.symbols = [
            Symbol(name="process", kind="function", file="s.py", line=1),
            Symbol(name="helper", kind="function", file="s.py", line=5),
        ]
        mod.call_edges = [
            CallEdge(caller="process", callee="self.helper", call_site="s.py:3"),
        ]
        facts.modules["service"] = mod

        _resolve_call_graph(facts)

        # self.helper resolves to 'helper' (same module, not prefixed)
        assert mod.call_edges[0].callee == "helper"

    def test_unknown_callee_unchanged(self):
        facts = ProjectFacts(language="python", source_dirs=["src"])
        mod = ModuleInfo(name="app")
        mod.symbols = [Symbol(name="run", kind="function", file="app.py", line=1)]
        mod.call_edges = [
            CallEdge(caller="run", callee="unknown_func", call_site="app.py:2"),
        ]
        facts.modules["app"] = mod

        _resolve_call_graph(facts)

        assert mod.call_edges[0].callee == "unknown_func"


@pytest.mark.skipif(
    not _tree_sitter_available(),
    reason="tree-sitter not installed",
)
class TestTreeSitterCallGraph:
    def test_extracts_function_calls(self, tmp_path):
        from codd.parsing import TreeSitterExtractor

        code = textwrap.dedent("""\
            def caller():
                result = callee()
                return result

            def callee():
                return 42
        """)
        symbols = [
            Symbol(name="caller", kind="function", file="test.py", line=1),
            Symbol(name="callee", kind="function", file="test.py", line=4),
        ]
        ext = TreeSitterExtractor("python")
        edges = ext.extract_call_graph(code, "test.py", symbols)

        assert len(edges) >= 1
        assert any(e.callee == "callee" and e.caller == "caller" for e in edges)

    def test_skips_builtins(self, tmp_path):
        from codd.parsing import TreeSitterExtractor

        code = textwrap.dedent("""\
            def process():
                items = list(range(10))
                print(len(items))
                return custom_fn(items)

            def custom_fn(data):
                return data
        """)
        symbols = [
            Symbol(name="process", kind="function", file="t.py", line=1),
            Symbol(name="custom_fn", kind="function", file="t.py", line=5),
        ]
        ext = TreeSitterExtractor("python")
        edges = ext.extract_call_graph(code, "t.py", symbols)

        callee_names = [e.callee for e in edges]
        assert "custom_fn" in callee_names
        assert "print" not in callee_names
        assert "len" not in callee_names
        assert "list" not in callee_names
        assert "range" not in callee_names

    def test_async_call_detection(self):
        from codd.parsing import TreeSitterExtractor

        code = textwrap.dedent("""\
            async def main():
                result = await fetch_data()

            async def fetch_data():
                return []
        """)
        symbols = [
            Symbol(name="main", kind="function", file="t.py", line=1),
            Symbol(name="fetch_data", kind="function", file="t.py", line=3),
        ]
        ext = TreeSitterExtractor("python")
        edges = ext.extract_call_graph(code, "t.py", symbols)

        assert len(edges) >= 1
        edge = next(e for e in edges if e.callee == "fetch_data")
        assert edge.is_async is True

    def test_regex_fallback_returns_empty(self):
        from codd.parsing import RegexExtractor

        ext = RegexExtractor("python")
        edges = ext.extract_call_graph("def f(): pass", "t.py", [])
        assert edges == []

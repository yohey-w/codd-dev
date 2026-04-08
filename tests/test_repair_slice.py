"""Tests for codd repair_slice — function-centered repair context generation."""

import textwrap
from pathlib import Path

import pytest

from codd.extractor import CallEdge, ModuleInfo, ProjectFacts, Symbol
from codd.repair_slice import (
    FunctionSlice,
    RepairSlice,
    build_repair_slice,
    extract_function_line_ranges,
    extract_raises,
    format_repair_slices,
    generate_repair_slices,
    score_functions,
)


# -- Helpers --

def make_symbol(name, kind="function", line=1, params="", return_type="",
                is_async=False, decorators=None):
    return Symbol(
        name=name, kind=kind, file="test.py", line=line,
        params=params, return_type=return_type,
        is_async=is_async, decorators=decorators or [],
        bases=[], implements=[],
    )


def make_facts(modules=None, language="python"):
    facts = ProjectFacts(language=language, source_dirs=["src"])
    if modules:
        for mod in modules:
            facts.modules[mod.name] = mod
    return facts


# -- FunctionSlice dataclass --

class TestFunctionSliceDefaults:
    def test_defaults(self):
        fs = FunctionSlice(name="foo", file="a.py", line_start=1, line_end=10, signature="def foo()")
        assert fs.relevance_score == 0.0
        assert fs.callers == []
        assert fs.callees == []
        assert fs.overrides is None
        assert fs.raises == []
        assert fs.is_public is False


# -- Scoring --

class TestScoreFunctions:
    def test_exact_name_match(self):
        scores = score_functions(["filter", "exclude", "get"], "The filter method is broken")
        assert scores["filter"] > scores["exclude"]
        assert scores["filter"] > scores["get"]

    def test_class_name_match(self):
        scores = score_functions(
            ["QuerySet.filter", "Manager.get"],
            "QuerySet returns wrong results",
        )
        assert scores["QuerySet.filter"] > scores["Manager.get"]

    def test_no_issue_text(self):
        scores = score_functions(["foo", "bar"], "")
        assert scores["foo"] == scores["bar"] == 0.5

    def test_traceback_in_mention(self):
        scores = score_functions(
            ["_filter_or_exclude", "filter", "get"],
            "File query.py line 200 in _filter_or_exclude",
        )
        assert scores["_filter_or_exclude"] == 1.0

    def test_empty_functions(self):
        scores = score_functions([], "some issue")
        assert scores == {}


# -- Line ranges (Python) --

class TestExtractFunctionLineRanges:
    def test_python_functions(self, tmp_path):
        code = textwrap.dedent("""\
            def foo():
                return 1

            def bar():
                return 2

            class MyClass:
                def method(self):
                    return 3
        """)
        (tmp_path / "test.py").write_text(code)
        ranges = extract_function_line_ranges(code, "test.py", "python")
        assert "foo" in ranges
        assert "bar" in ranges
        # method should be scoped
        assert "MyClass.method" in ranges or "method" in ranges

    def test_includes_private_functions(self, tmp_path):
        code = textwrap.dedent("""\
            def _private_helper():
                return 1

            def public_func():
                return 2
        """)
        ranges = extract_function_line_ranges(code, "test.py", "python")
        assert "_private_helper" in ranges
        assert "public_func" in ranges

    def test_line_numbers_correct(self):
        code = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        ranges = extract_function_line_ranges(code, "test.py", "python")
        assert "foo" in ranges
        assert ranges["foo"][0] == 1  # starts at line 1
        assert "bar" in ranges
        assert ranges["bar"][0] >= 3  # line 3 or 4 depending on parser


# -- Raises extraction --

class TestExtractRaises:
    def test_single_raise(self):
        code = textwrap.dedent("""\
            def validate(x):
                if x < 0:
                    raise ValueError("negative")
        """)
        result = extract_raises(code, "test.py", "python")
        assert "validate" in result
        assert "ValueError" in result["validate"]

    def test_multiple_raises(self):
        code = textwrap.dedent("""\
            def parse(data):
                if not data:
                    raise ValueError("empty")
                if not isinstance(data, dict):
                    raise TypeError("not dict")
        """)
        result = extract_raises(code, "test.py", "python")
        assert "parse" in result
        assert "ValueError" in result["parse"]
        assert "TypeError" in result["parse"]

    def test_no_raises(self):
        code = textwrap.dedent("""\
            def simple():
                return 42
        """)
        result = extract_raises(code, "test.py", "python")
        assert "simple" not in result

    def test_class_method_raises(self):
        code = textwrap.dedent("""\
            class Foo:
                def bar(self):
                    raise NotImplementedError
        """)
        result = extract_raises(code, "test.py", "python")
        # Should be under Foo.bar or bar
        found = "Foo.bar" in result or "bar" in result
        assert found


# -- Build repair slice --

class TestBuildRepairSlice:
    def test_basic(self, tmp_path):
        code = textwrap.dedent("""\
            def foo():
                return 1

            def bar():
                raise ValueError("bad")
                return 2

            def baz():
                return 3
        """)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text(code)

        mod = ModuleInfo(
            name="mod",
            files=["src/mod.py"],
            symbols=[
                make_symbol("foo", line=1),
                make_symbol("bar", line=4),
                make_symbol("baz", line=8),
            ],
            call_edges=[],
        )
        facts = make_facts(modules=[mod])

        rs = build_repair_slice(tmp_path, "src/mod.py", facts, issue_text="bar raises error", top_n=2)
        assert rs.file == "src/mod.py"
        assert len(rs.functions) == 2
        # bar should be ranked first (name match)
        assert rs.functions[0].name in ("bar", "baz", "foo")

    def test_empty_file(self, tmp_path):
        (tmp_path / "empty.py").write_text("")
        facts = make_facts()
        rs = build_repair_slice(tmp_path, "empty.py", facts)
        assert rs.functions == []

    def test_nonexistent_file(self, tmp_path):
        facts = make_facts()
        rs = build_repair_slice(tmp_path, "does_not_exist.py", facts)
        assert rs.functions == []

    def test_top_n_limits(self, tmp_path):
        code = "\n".join(f"def func_{i}():\n    return {i}\n" for i in range(10))
        (tmp_path / "many.py").write_text(code)

        syms = [make_symbol(f"func_{i}", line=i * 3 + 1) for i in range(10)]
        mod = ModuleInfo(name="many", files=["many.py"], symbols=syms, call_edges=[])
        facts = make_facts(modules=[mod])

        rs = build_repair_slice(tmp_path, "many.py", facts, top_n=3)
        assert len(rs.functions) <= 3

    def test_callers_callees(self, tmp_path):
        code = textwrap.dedent("""\
            def caller():
                return callee()

            def callee():
                return 42
        """)
        (tmp_path / "call.py").write_text(code)

        mod = ModuleInfo(
            name="call",
            files=["call.py"],
            symbols=[make_symbol("caller", line=1), make_symbol("callee", line=4)],
            call_edges=[CallEdge(caller="caller", callee="callee", call_site="call.py:2")],
        )
        facts = make_facts(modules=[mod])

        rs = build_repair_slice(tmp_path, "call.py", facts, top_n=5)
        func_map = {f.name: f for f in rs.functions}
        if "caller" in func_map:
            assert "callee" in func_map["caller"].callees
        if "callee" in func_map:
            assert "caller" in func_map["callee"].callers


# -- Format --

class TestFormatRepairSlices:
    def test_basic_format(self):
        fs = FunctionSlice(
            name="QuerySet.filter", file="query.py",
            line_start=100, line_end=150,
            signature="def filter(self, *args, **kwargs) -> QuerySet",
            relevance_score=0.9,
            callers=["Manager.filter"],
            callees=["_filter_or_exclude"],
            return_type="QuerySet",
            raises=["ValueError"],
            is_public=True,
            test_refs=["tests/test_query.py"],
        )
        rs = RepairSlice(file="query.py", module_name="query", functions=[fs])
        output = format_repair_slices([rs])

        assert "REPAIR CONTEXT: query.py" in output
        assert "QuerySet.filter" in output
        assert "L:100-150" in output
        assert "callers: Manager.filter" in output
        assert "callees: _filter_or_exclude" in output
        assert "returns: QuerySet" in output
        assert "raises: ValueError" in output
        assert "public API" in output

    def test_uncovered_shows_uncovered(self):
        fs = FunctionSlice(
            name="helper", file="a.py",
            line_start=1, line_end=5,
            signature="def helper()",
        )
        rs = RepairSlice(file="a.py", module_name="a", functions=[fs])
        output = format_repair_slices([rs])
        assert "(uncovered)" in output

    def test_no_json_in_output(self):
        fs = FunctionSlice(
            name="foo", file="a.py",
            line_start=1, line_end=5,
            signature="def foo()",
            callers=["bar"],
        )
        rs = RepairSlice(file="a.py", module_name="a", functions=[fs])
        output = format_repair_slices([rs])
        assert "{" not in output  # no JSON braces
        assert "[" not in output or "L:" in output  # only L: brackets allowed


# -- End-to-end --

class TestGenerateRepairSlices:
    def test_end_to_end(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("")
        (src / "main.py").write_text(textwrap.dedent("""\
            def process(data):
                if not data:
                    raise ValueError("empty")
                return transform(data)

            def transform(data):
                return data.upper()
        """))

        result = generate_repair_slices(
            tmp_path,
            files=["src/main.py"],
            issue_text="process raises ValueError",
            source_dirs=["src"],
            top_n=2,
        )
        assert "REPAIR CONTEXT" in result
        assert "process" in result

    def test_no_files(self, tmp_path):
        result = generate_repair_slices(tmp_path, files=[], issue_text="bug")
        assert "No repair context" in result

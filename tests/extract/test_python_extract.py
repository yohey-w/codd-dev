"""Python AST extraction coverage for brownfield projects."""

from pathlib import Path

from codd.extract_ai import pre_scan
from codd.extractor import extract_facts
from codd.parsing import PythonAstExtractor, get_extractor


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_python"


def _sample_facts():
    return extract_facts(FIXTURE_ROOT, "python", ["src"])


def test_get_extractor_uses_stdlib_python_ast():
    assert isinstance(get_extractor("python"), PythonAstExtractor)


def test_python_fixture_discovers_modules_and_counts_files():
    facts = _sample_facts()

    assert facts.language == "python"
    assert "sample" in facts.modules
    assert facts.total_files == 5
    assert facts.modules["sample"].line_count > 0


def test_python_ast_extracts_classes_functions_and_filters_private_functions():
    symbols = {symbol.name: symbol for symbol in _sample_facts().modules["sample"].symbols}

    assert symbols["Service"].kind == "class"
    assert symbols["Worker"].kind == "class"
    assert symbols["helper"].kind == "function"
    assert symbols["run"].kind == "function"
    assert "_private_helper" not in symbols


def test_python_ast_extracts_signature_decorators_and_bases():
    symbols = {symbol.name: symbol for symbol in _sample_facts().modules["sample"].symbols}

    assert symbols["Service"].bases == ["BaseService"]
    assert symbols["Inner"].bases == ["Model"]
    run = symbols["run"]
    assert "value: str" in run.params
    assert "limit: int = 3" in run.params
    assert run.return_type == "dict[str, int]"
    assert run.decorators == ['router.get("/items")']
    assert run.is_async is True


def test_python_ast_extracts_internal_and_external_imports():
    module = _sample_facts().modules["sample"]

    assert "sample" in module.internal_imports
    assert "requests" in module.external_imports
    assert "json" not in module.external_imports
    assert "os" not in module.external_imports


def test_python_ast_records_module_docstring():
    module = _sample_facts().modules["sample"]

    assert module.patterns["module_docstring"] == "Sample package entry points."


def test_python_ast_extracts_call_graph_edges():
    module = _sample_facts().modules["sample"]
    edges = {(edge.caller, edge.callee) for edge in module.call_edges}

    assert ("Service.run", "helper") in edges
    assert ("Worker.process", "helper") in edges


def test_python_syntax_error_falls_back_to_raw_module_facts(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    facts = extract_facts(tmp_path, "python", ["src"])

    assert "broken" in facts.modules
    assert facts.modules["broken"].line_count == 2
    assert facts.modules["broken"].symbols == []
    assert facts.modules["broken"].internal_imports == {}
    assert facts.modules["broken"].external_imports == set()


def test_extract_ai_pre_scan_includes_python_source_files():
    scan = pre_scan(FIXTURE_ROOT)

    assert "src/sample/main.py" in scan.source_files
    assert "src/sample/helpers.py" in scan.source_files
    assert "src/sample/worker.py" in scan.source_files


def test_extract_ai_pre_scan_preserves_invalid_python_raw_text_and_excludes_tests():
    scan = pre_scan(FIXTURE_ROOT)

    assert "def unfinished(" in scan.source_files["src/sample/broken.py"]
    assert "tests/test_sample.py" not in scan.source_files
    assert scan.test_files == ["tests/test_sample.py"]

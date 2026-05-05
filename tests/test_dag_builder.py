import json
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.dag import DAG
from codd.dag.builder import build_dag, write_dag_mermaid
from codd.dag.extractor import extract_design_doc_metadata, extract_imports


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.ts", "src/**/*.tsx", "app/**/*.ts", "app/**/*.tsx"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def test_build_dag_returns_dag_object(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    dag = build_dag(tmp_path, _settings())

    assert isinstance(dag, DAG)


def test_build_dag_design_docs_extracted(tmp_path):
    _write(tmp_path / "docs" / "design" / "api_design.md", "---\ntitle: API\n---\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["docs/design/api_design.md"].kind == "design_doc"


def test_build_dag_depends_on_edge(tmp_path):
    _write(tmp_path / "docs" / "design" / "system_design.md", "# System\n")
    _write(tmp_path / "docs" / "design" / "api_design.md", "---\ndepends_on:\n  - system_design.md\n---\n")

    dag = build_dag(tmp_path, _settings())

    assert any(
        edge.from_id == "docs/design/api_design.md"
        and edge.to_id == "docs/design/system_design.md"
        and edge.kind == "depends_on"
        for edge in dag.edges
    )


def test_build_dag_impl_files_extracted(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")
    _write(tmp_path / "app" / "page.tsx", "export default function Page() { return null; }\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["src/index.ts"].kind == "impl_file"
    assert dag.nodes["app/page.tsx"].kind == "impl_file"


def test_build_dag_import_edges(tmp_path):
    _write(tmp_path / "src" / "a.ts", 'import { b } from "./b";\n')
    _write(tmp_path / "src" / "b.ts", "export const b = 1;\n")

    dag = build_dag(tmp_path, _settings())

    assert any(edge.from_id == "src/a.ts" and edge.to_id == "src/b.ts" and edge.kind == "imports" for edge in dag.edges)


def test_build_dag_plan_tasks_extracted(tmp_path):
    _write(tmp_path / "src" / "feature.ts", "export const feature = true;\n")
    _write(tmp_path / "docs" / "design" / "implementation_plan.md", "## 1-1 Build feature\noutputs:\n  - src/feature.ts\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["implementation_plan.md#1-1"].kind == "plan_task"


def test_build_dag_plan_task_produces_edge(tmp_path):
    _write(tmp_path / "src" / "feature.ts", "export const feature = true;\n")
    _write(tmp_path / "docs" / "design" / "implementation_plan.md", "## 1-1 Build feature\noutputs:\n  - src/feature.ts\n")

    dag = build_dag(tmp_path, _settings())

    assert any(
        edge.from_id == "implementation_plan.md#1-1"
        and edge.to_id == "src/feature.ts"
        and edge.kind == "produces"
        for edge in dag.edges
    )


def test_build_dag_expected_nodes(tmp_path):
    _write(tmp_path / "src" / "header.ts", "export const Header = () => null;\n")
    _write(
        tmp_path / "project_lexicon.yaml",
        "required_artifacts:\n"
        "  - id: component:Header\n"
        "    title: Header\n"
        "    scope: UI shell\n"
        "    source: ai_derived\n"
        "    path: src/header.ts\n",
    )

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["lexicon:component:Header"].kind == "expected"
    assert any(
        edge.from_id == "lexicon:component:Header" and edge.to_id == "src/header.ts" and edge.kind == "represents"
        for edge in dag.edges
    )


def test_build_dag_no_design_docs_graceful(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    dag = build_dag(tmp_path, _settings())

    assert "src/index.ts" in dag.nodes


def test_build_dag_no_impl_files_graceful(tmp_path):
    _write(tmp_path / "docs" / "design" / "api_design.md", "# API\n")

    dag = build_dag(tmp_path, _settings())

    assert "docs/design/api_design.md" in dag.nodes


def test_build_dag_json_output(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    build_dag(tmp_path, _settings())

    assert (tmp_path / ".codd" / "dag.json").is_file()


def test_build_dag_json_schema(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    build_dag(tmp_path, _settings())
    payload = json.loads((tmp_path / ".codd" / "dag.json").read_text(encoding="utf-8"))

    assert {"version", "built_at", "project_root", "nodes", "edges", "cycles"} <= payload.keys()


def test_build_dag_mermaid_output(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")
    dag = build_dag(tmp_path, _settings())

    write_dag_mermaid(dag, tmp_path / ".codd" / "dag.mmd")

    assert "flowchart TD" in (tmp_path / ".codd" / "dag.mmd").read_text(encoding="utf-8")


def test_build_dag_cache_hit(tmp_path):
    cache_path = tmp_path / ".codd" / "dag.json"
    _write(cache_path, "cached\n")

    result = CliRunner().invoke(main, ["dag", "build", "--path", str(tmp_path), "--cache"])

    assert result.exit_code == 0
    assert "Using cached DAG" in result.output
    assert cache_path.read_text(encoding="utf-8") == "cached\n"


def test_build_dag_osato_lms_scale(tmp_path):
    for index in range(101):
        _write(tmp_path / "src" / f"file_{index}.ts", "export const value = 1;\n")

    dag = build_dag(tmp_path, _settings())

    assert len(dag.nodes) >= 101


def test_dag_cli_build_command(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    result = CliRunner().invoke(main, ["dag", "build", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".codd" / "dag.json").is_file()


def test_dag_cli_build_json_format(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    result = CliRunner().invoke(main, ["dag", "build", "--path", str(tmp_path), "--format", "json", "--output", "dag-out.json"])

    assert result.exit_code == 0
    assert json.loads((tmp_path / "dag-out.json").read_text(encoding="utf-8"))["version"] == "1"


def test_dag_cli_build_mermaid_format(tmp_path):
    _write(tmp_path / "src" / "index.ts", "export const ok = true;\n")

    result = CliRunner().invoke(
        main,
        ["dag", "build", "--path", str(tmp_path), "--format", "mermaid", "--output", "dag.mmd"],
    )

    assert result.exit_code == 0
    assert "flowchart TD" in (tmp_path / "dag.mmd").read_text(encoding="utf-8")


def test_extractor_adapter_imports(tmp_path):
    source = _write(
        tmp_path / "src" / "index.ts",
        "import x from './x';\n"
        "export { y } from './y';\n"
        "const z = require('./z');\n"
        "await import('./lazy');\n",
    )

    assert extract_imports(source) == ["./x", "./y", "./z", "./lazy"]


def test_extractor_adapter_design_doc_metadata(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "api.md", "---\ndepends_on:\n  - system.md\n---\n# API\n")

    metadata = extract_design_doc_metadata(doc)

    assert metadata["depends_on"] == ["system.md"]
    assert metadata["body"].strip() == "# API"


def test_build_dag_cycle_detection(tmp_path):
    _write(tmp_path / "docs" / "design" / "a.md", "---\ndepends_on:\n  - b.md\n---\n")
    _write(tmp_path / "docs" / "design" / "b.md", "---\ndepends_on:\n  - a.md\n---\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.detect_cycles() == [["docs/design/a.md", "docs/design/b.md"]]


def test_build_dag_no_cycle(tmp_path):
    _write(tmp_path / "docs" / "design" / "a.md", "---\ndepends_on:\n  - b.md\n---\n")
    _write(tmp_path / "docs" / "design" / "b.md", "# B\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.detect_cycles() == []

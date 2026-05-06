from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.dag import builder as builder_module
from codd.dag.builder import build_dag, dag_to_dict
from codd.dag.checks.implementation_coverage import ImplementationCoverageCheck
from codd.dag.extractor import extract_design_doc_metadata
from codd.dag.runner import run_checks
from codd.llm.design_doc_extractor import (
    DESIGN_DOC_EXTRACTORS,
    DesignDocExtractor,
    ExpectedEdge,
    ExpectedExtraction,
    ExpectedNode,
    SubprocessAiCommandDesignDocExtractor,
    build_project_structure_summary,
    expected_extraction_cache_path,
    extract_expected_artifacts_for_file,
    load_cached_expected_extraction,
    register_design_doc_extractor,
    save_expected_extraction,
)


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.py"],
        "test_file_patterns": ["tests/**/*.py"],
    }
    settings.update(overrides)
    return settings


def _extraction(path_hint: str = "src/service.py") -> ExpectedExtraction:
    return ExpectedExtraction(
        expected_nodes=[
            ExpectedNode(
                kind="impl_file",
                path_hint=path_hint,
                rationale="required by design",
                source_design_section="Overview",
            )
        ],
        expected_edges=[],
        source_design_doc="docs/design/spec.md",
        provider_id="test-provider",
        generated_at="2026-05-06T00:00:00Z",
    )


class StaticExtractor(DesignDocExtractor):
    provider_name = "static"

    def __init__(self, extraction: ExpectedExtraction):
        self.extraction = extraction
        self.calls = 0

    def extract_expected_artifacts(self, design_doc: Node, project_context: dict) -> ExpectedExtraction:
        self.calls += 1
        return self.extraction


class FailingExtractor(DesignDocExtractor):
    provider_name = "failing"

    def extract_expected_artifacts(self, design_doc: Node, project_context: dict) -> ExpectedExtraction:
        raise AssertionError("cache should have been used")


def test_expected_node_to_dict_roundtrip():
    node = ExpectedNode(
        kind="impl_file",
        path_hint="src/service.py",
        rationale="needed",
        source_design_section="Scope",
        required_capabilities=["persistence"],
    )

    assert ExpectedNode.from_dict(node.to_dict()) == node


def test_expected_node_defaults_required_capabilities():
    node = ExpectedNode.from_dict(
        {
            "kind": "unknown",
            "path_hint": "src/service.py",
            "rationale": "needed",
            "source_design_section": "Scope",
        }
    )

    assert node.kind == "impl_file"
    assert node.required_capabilities == []


def test_expected_edge_to_dict_roundtrip():
    edge = ExpectedEdge(
        from_path_hint="src/service.py",
        to_path_hint="tests/test_service.py",
        kind="tested_by",
        rationale="covered by tests",
        attributes={"source": "design"},
    )

    assert ExpectedEdge.from_dict(edge.to_dict()) == edge


def test_expected_extraction_to_dict_roundtrip():
    extraction = _extraction()

    assert ExpectedExtraction.from_dict(extraction.to_dict()) == extraction


def test_expected_extraction_from_nested_payload():
    payload = {"expected_extraction": _extraction().to_dict()}

    assert ExpectedExtraction.from_dict(payload).expected_nodes[0].path_hint == "src/service.py"


def test_register_design_doc_extractor_decorator():
    @register_design_doc_extractor("unit_test_provider")
    class UnitTestProvider(DesignDocExtractor):
        provider_name = "unit_test_provider"

        def extract_expected_artifacts(self, design_doc: Node, project_context: dict) -> ExpectedExtraction:
            return _extraction()

    assert DESIGN_DOC_EXTRACTORS["unit_test_provider"] is UnitTestProvider


def test_builtin_design_doc_extractor_registered():
    assert DESIGN_DOC_EXTRACTORS["subprocess_ai_command"] is SubprocessAiCommandDesignDocExtractor


def test_cache_path_is_under_expected_extractions(tmp_path):
    doc = tmp_path / "docs" / "design" / "spec.md"

    path = expected_extraction_cache_path(tmp_path, doc)

    assert path.parent == tmp_path / ".codd" / "expected_extractions"
    assert path.name == "docs_design_spec.md.yaml"


def test_save_and_load_cache_roundtrip(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    extraction = _extraction()

    save_expected_extraction(tmp_path, doc, extraction, source_sha256="abc")

    assert load_cached_expected_extraction(tmp_path, doc, source_sha256="abc") == extraction


def test_load_cache_missing_returns_none(tmp_path):
    assert load_cached_expected_extraction(tmp_path, tmp_path / "docs" / "design" / "missing.md") is None


def test_stale_cache_ignored_when_hash_differs(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    save_expected_extraction(tmp_path, doc, _extraction(), source_sha256="old")

    assert load_cached_expected_extraction(tmp_path, doc, source_sha256="new") is None


def test_extract_expected_artifacts_for_file_uses_cache_without_extractor(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    extraction = _extraction()
    save_expected_extraction(tmp_path, doc, extraction)

    result = extract_expected_artifacts_for_file(tmp_path / "docs" / "design" / "spec.md", tmp_path, extractor=FailingExtractor())

    assert result == extraction


def test_extract_expected_artifacts_for_file_force_refreshes_cache(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    save_expected_extraction(tmp_path, doc, _extraction("src/old.py"))
    extractor = StaticExtractor(_extraction("src/new.py"))

    result = extract_expected_artifacts_for_file(doc, tmp_path, force=True, extractor=extractor)

    assert result.expected_nodes[0].path_hint == "src/new.py"
    assert extractor.calls == 1
    assert load_cached_expected_extraction(tmp_path, doc).expected_nodes[0].path_hint == "src/new.py"


def test_subprocess_extractor_uses_configured_command_and_prompt(tmp_path):
    captured = {}

    def fake_runner(command, **kwargs):
        captured["command"] = command
        captured["prompt"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(_extraction().to_dict()),
            stderr="",
        )

    extractor = SubprocessAiCommandDesignDocExtractor(runner=fake_runner)
    result = extractor.extract_expected_artifacts(
        Node(id="docs/design/spec.md", kind="design_doc", path="docs/design/spec.md"),
        {
            "project_root": tmp_path,
            "config": {"ai_commands": {"design_doc_extract": "custom-ai --json"}},
            "design_doc_body": "Body text",
            "project_structure_summary": "- src/service.py",
        },
    )

    assert captured["command"] == ["custom-ai", "--json"]
    assert "Body text" in captured["prompt"]
    assert "{design_doc_body}" not in captured["prompt"]
    assert result.expected_nodes[0].path_hint == "src/service.py"


def test_subprocess_extractor_accepts_fenced_json(tmp_path):
    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="```json\n" + json.dumps(_extraction().to_dict()) + "\n```",
            stderr="",
        )

    extractor = SubprocessAiCommandDesignDocExtractor(ai_command="ai", runner=fake_runner)

    result = extractor.extract_expected_artifacts(
        Node(id="docs/design/spec.md", kind="design_doc", path="docs/design/spec.md"),
        {"project_root": tmp_path, "design_doc_body": "# Spec", "project_structure_summary": "- src/service.py"},
    )

    assert result.expected_nodes[0].path_hint == "src/service.py"


def test_subprocess_extractor_rejects_invalid_json(tmp_path):
    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    extractor = SubprocessAiCommandDesignDocExtractor(ai_command="ai", runner=fake_runner)

    with pytest.raises(ValueError, match="invalid JSON"):
        extractor.extract_expected_artifacts(
            Node(id="docs/design/spec.md", kind="design_doc", path="docs/design/spec.md"),
            {"project_root": tmp_path, "design_doc_body": "# Spec", "project_structure_summary": "- src/service.py"},
        )


def test_project_structure_summary_lists_files_and_skips_cache_dirs(tmp_path):
    _write(tmp_path / "src" / "service.py", "VALUE = 1\n")
    _write(tmp_path / ".codd" / "secret.txt", "hidden\n")

    summary = build_project_structure_summary(tmp_path)

    assert "- src/service.py" in summary
    assert "secret.txt" not in summary


def test_design_doc_extract_template_has_required_slots():
    template = (Path("codd") / "llm" / "templates" / "design_doc_extract_meta.md").read_text(encoding="utf-8")

    assert "{design_doc_body}" in template
    assert "{project_structure_summary}" in template
    assert "expected_nodes" in template
    assert not any(term in template.lower() for term in ("next", "react", "django", "rails"))


def test_builder_attaches_cached_expected_extraction(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    _write(tmp_path / "src" / "service.py", "VALUE = 1\n")
    save_expected_extraction(tmp_path, doc, _extraction())

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["docs/design/spec.md"].attributes["expected_extraction"]["expected_nodes"][0]["path_hint"] == "src/service.py"


def test_builder_enabled_invokes_extractor_and_adds_expect_edge(tmp_path, monkeypatch):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    _write(tmp_path / "src" / "service.py", "VALUE = 1\n")
    calls = []

    def fake_extract(doc_path, project_root, *, config, force):
        calls.append((doc_path, project_root, force))
        return _extraction()

    monkeypatch.setattr(builder_module, "extract_expected_artifacts_for_file", fake_extract)

    dag = builder_module.build_dag(tmp_path, _settings(design_doc_extraction={"enabled": True}))

    assert calls == [(doc, tmp_path.resolve(), False)]
    assert any(edge.from_id == "docs/design/spec.md" and edge.to_id == "src/service.py" for edge in dag.edges)


def test_dag_json_omits_expected_extraction_attribute(tmp_path):
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            path="docs/design/spec.md",
            attributes={"expected_extraction": _extraction().to_dict(), "keep": True},
        )
    )

    payload = dag_to_dict(dag, tmp_path)

    assert payload["nodes"][0]["attributes"] == {"keep": True}


def test_design_doc_metadata_passthrough_expected_extraction(tmp_path):
    payload = _extraction().to_dict()
    doc = _write(
        tmp_path / "docs" / "design" / "spec.md",
        yaml.safe_dump({"expected_extraction": payload}, explicit_start=True) + "---\n# Spec\n",
    )

    metadata = extract_design_doc_metadata(doc)

    assert metadata["attributes"]["expected_extraction"]["expected_nodes"][0]["path_hint"] == "src/service.py"


def test_implementation_coverage_detects_missing_implementation(tmp_path):
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            attributes={"expected_extraction": _extraction("src/missing.py").to_dict()},
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"
    assert result.block_deploy is False


def test_implementation_coverage_exact_match_passes(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/service.py", kind="impl_file", path="src/service.py"))
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            attributes={"expected_extraction": _extraction("src/service.py").to_dict()},
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_implementation_coverage_glob_match_passes(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/service.py", kind="impl_file", path="src/service.py"))
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            attributes={"expected_extraction": _extraction("src/*.py").to_dict()},
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_implementation_coverage_config_file_existing_passes(tmp_path):
    _write(tmp_path / "codd" / "codd.yaml", "project:\n  name: demo\n")
    extraction = ExpectedExtraction(
        expected_nodes=[
            ExpectedNode(
                kind="config_file",
                path_hint="codd/codd.yaml",
                rationale="needed",
                source_design_section="Config",
            )
        ],
        expected_edges=[],
        source_design_doc="docs/design/spec.md",
    )
    dag = DAG()
    dag.add_node(Node(id="docs/design/spec.md", kind="design_doc", attributes={"expected_extraction": extraction.to_dict()}))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_implementation_coverage_additional_impl_is_amber_warning(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/service.py", kind="impl_file", path="src/service.py"))
    dag.add_node(Node(id="src/helper.py", kind="impl_file", path="src/helper.py"))
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            attributes={"expected_extraction": _extraction("src/service.py").to_dict()},
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.severity == "amber"
    assert result.violations == [{"type": "additional_implementation", "impl_file": "src/helper.py", "severity": "amber"}]


def test_runner_registers_implementation_coverage_check(tmp_path):
    result = run_checks(DAG(), tmp_path, {}, check_names=["implementation_coverage"])

    assert result[0].check_name == "implementation_coverage"


def test_cli_extract_design_force_reextracts(tmp_path, monkeypatch):
    doc = _write(tmp_path / "docs" / "design" / "spec.md", "# Spec\n")
    calls = []

    def fake_extract(doc_path, project_root, *, config, force):
        calls.append((doc_path, project_root, force))
        return _extraction()

    import codd.llm.design_doc_extractor as design_doc_extractor_module

    monkeypatch.setattr(design_doc_extractor_module, "extract_expected_artifacts_for_file", fake_extract)

    result = CliRunner().invoke(
        main,
        ["extract", "design", "--path", str(tmp_path), "--design-doc", str(doc), "--force"],
    )

    assert result.exit_code == 0
    assert calls == [(doc.resolve(), tmp_path.resolve(), True)]
    assert "Extracted expected artifacts: 1 node(s), 0 edge(s)" in result.output

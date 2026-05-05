from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.dag.extractor import scan_capability_evidence


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.py", "src/**/*.ts", "src/**/*.js"],
        "test_file_patterns": ["tests/**/*.py"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def _patterns() -> dict:
    return {
        "runtime_flag_enabled": {
            "matches": [{"regex": r"enableRuntime\(true\)", "languages": ["typescript"]}]
        }
    }


def test_scan_capability_evidence_empty_patterns_returns_empty(tmp_path):
    source = _write(tmp_path / "src" / "service.ts", "enableRuntime(true)\n")

    assert scan_capability_evidence(source, {}) == []


def test_scan_capability_evidence_includes_line_ref_value_and_source(tmp_path):
    source = _write(tmp_path / "src" / "service.ts", "const a = 1;\nenableRuntime(true)\n")

    evidence = scan_capability_evidence(source, _patterns())

    assert evidence == [
        {
            "capability_kind": "runtime_flag_enabled",
            "value": True,
            "line_ref": f"{source.as_posix()}:2",
            "source": "capability_patterns",
        }
    ]


def test_scan_capability_evidence_collects_multiple_patterns_and_matches(tmp_path):
    source = _write(
        tmp_path / "src" / "service.ts",
        "enableRuntime(true)\n"
        "allowBrowserState()\n"
        "enableRuntime(true)\n",
    )
    patterns = {
        **_patterns(),
        "browser_state_allowed": {"matches": [{"regex": r"allowBrowserState\(\)", "languages": ["ts"]}]},
    }

    evidence = scan_capability_evidence(source, patterns)

    assert [item["capability_kind"] for item in evidence] == [
        "runtime_flag_enabled",
        "browser_state_allowed",
        "runtime_flag_enabled",
    ]


def test_scan_capability_evidence_no_match_returns_empty(tmp_path):
    source = _write(tmp_path / "src" / "service.ts", "disableRuntime()\n")

    assert scan_capability_evidence(source, _patterns()) == []


def test_scan_capability_evidence_filters_python_language(tmp_path):
    source = _write(tmp_path / "src" / "service.py", "enable_runtime(True)\n")
    patterns = {"runtime_flag_enabled": {"matches": [{"regex": r"enable_runtime\(True\)", "languages": ["python"]}]}}

    evidence = scan_capability_evidence(source, patterns)

    assert evidence[0]["capability_kind"] == "runtime_flag_enabled"


def test_scan_capability_evidence_language_filter_excludes_other_suffixes(tmp_path):
    source = _write(tmp_path / "src" / "service.py", "enable_runtime(True)\n")
    patterns = {"runtime_flag_enabled": {"matches": [{"regex": r"enable_runtime\(True\)", "languages": ["typescript"]}]}}

    assert scan_capability_evidence(source, patterns) == []


def test_scan_capability_evidence_without_languages_applies_to_any_file_type(tmp_path):
    source = _write(tmp_path / "src" / "worker.js", "enableRuntime(true)\n")
    patterns = {"runtime_flag_enabled": {"matches": [{"regex": r"enableRuntime\(true\)"}]}}

    evidence = scan_capability_evidence(source, patterns)

    assert evidence[0]["line_ref"].endswith("worker.js:1")


def test_scan_capability_evidence_uses_capability_key_as_capability_kind(tmp_path):
    source = _write(tmp_path / "src" / "service.ts", "enableRuntime(true)\n")

    evidence = scan_capability_evidence(source, _patterns())

    assert evidence[0]["capability_kind"] == "runtime_flag_enabled"


def test_build_dag_without_capability_patterns_sets_empty_runtime_evidence(tmp_path):
    _write(tmp_path / "src" / "service.ts", "enableRuntime(true)\n")

    dag = build_dag(tmp_path, _settings(coherence={"capability_patterns": {}}))

    assert dag.nodes["src/service.ts"].attributes["runtime_evidence"] == []


def test_build_dag_registers_runtime_evidence_on_impl_file_attributes(tmp_path):
    _write(tmp_path / "src" / "service.ts", "const a = 1;\nenableRuntime(true)\n")

    dag = build_dag(tmp_path, _settings(coherence={"capability_patterns": _patterns()}))

    assert dag.nodes["src/service.ts"].attributes["runtime_evidence"] == [
        {
            "capability_kind": "runtime_flag_enabled",
            "value": True,
            "line_ref": "src/service.ts:2",
            "source": "capability_patterns",
        }
    ]


def test_build_dag_keeps_multiple_impl_files_independent(tmp_path):
    _write(tmp_path / "src" / "enabled.ts", "enableRuntime(true)\n")
    _write(tmp_path / "src" / "plain.ts", "export const plain = true;\n")

    dag = build_dag(tmp_path, _settings(coherence={"capability_patterns": _patterns()}))

    assert dag.nodes["src/enabled.ts"].attributes["runtime_evidence"][0]["line_ref"] == "src/enabled.ts:1"
    assert dag.nodes["src/plain.ts"].attributes["runtime_evidence"] == []


def test_build_dag_loads_project_declared_capability_patterns_from_codd_yaml(tmp_path):
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump({"coherence": {"capability_patterns": _patterns()}}, sort_keys=False),
    )
    _write(tmp_path / "src" / "service.ts", "enableRuntime(true)\n")

    dag = build_dag(tmp_path)

    assert dag.nodes["src/service.ts"].attributes["runtime_evidence"][0]["capability_kind"] == "runtime_flag_enabled"


def test_build_dag_keeps_existing_impl_file_attributes(tmp_path):
    _write(tmp_path / "src" / "dep.ts", "export const dep = 1;\n")
    _write(tmp_path / "src" / "service.ts", "import { dep } from './dep';\n")

    dag = build_dag(tmp_path, _settings(coherence={"capability_patterns": {}}))
    attributes = dag.nodes["src/service.ts"].attributes

    assert attributes["language"] == "typescript"
    assert attributes["imports"] == ["./dep"]
    assert attributes["runtime_evidence"] == []


def test_codd_core_contains_no_builtin_capability_pattern_literals():
    core_text = "\n".join(
        [
            Path("codd/dag/extractor.py").read_text(encoding="utf-8"),
            Path("codd/dag/builder.py").read_text(encoding="utf-8"),
        ]
    )

    for forbidden in ("NextAuth", "SameSite", "__Secure-", "__Host-"):
        assert forbidden not in core_text

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from codd.dag import DAG, Edge, Node
from codd.dag.builder import build_dag, dag_to_dict


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.ts", "src/**/*.tsx"],
        "test_file_patterns": ["tests/**/*.ts"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def _artifact(**overrides) -> dict:
    artifact = {
        "id": "e2e_login_journey",
        "title": "Login journey",
        "scope": "auth",
        "source": "ai_derived",
        "path": "src/login.ts",
    }
    artifact.update(overrides)
    return artifact


def _write_lexicon(path: Path, artifacts: list[dict]) -> None:
    _write(path / "project_lexicon.yaml", yaml.safe_dump({"required_artifacts": artifacts}, sort_keys=False))


def _write_plan(path: Path, outputs: list[str]) -> None:
    lines = ["## E2E-LOGIN Login journey", "outputs:"]
    lines.extend(f"  - {output}" for output in outputs)
    _write(path / "docs" / "design" / "implementation_plan.md", "\n".join(lines) + "\n")


def _build_project(path: Path, artifact: dict, outputs: list[str] | None = None) -> DAG:
    _write(path / "src" / "login.ts", "export const login = true;\n")
    _write_lexicon(path, [artifact])
    if outputs is not None:
        _write_plan(path, outputs)
    return build_dag(path, _settings())


def _edge(dag: DAG, from_id: str, to_id: str, kind: str = "produces") -> Edge:
    return next(edge for edge in dag.edges if edge.from_id == from_id and edge.to_id == to_id and edge.kind == kind)


def test_lexicon_journey_attribute_passthrough_to_expected_node(tmp_path):
    dag = _build_project(tmp_path, _artifact(journey="login_to_dashboard"))

    assert dag.nodes["lexicon:e2e_login_journey"].attributes["journey"] == "login_to_dashboard"


def test_lexicon_browser_requirements_passthrough_to_expected_node(tmp_path):
    requirements = [{"capability": "cookie_set", "value": True, "rationale": "session persists"}]
    dag = _build_project(tmp_path, _artifact(browser_requirements=requirements))

    assert dag.nodes["lexicon:e2e_login_journey"].attributes["browser_requirements"] == requirements


def test_lexicon_runtime_requirements_passthrough_to_expected_node(tmp_path):
    requirements = [{"capability": "tls_termination", "required": True, "rationale": "secure cookie"}]
    dag = _build_project(tmp_path, _artifact(runtime_requirements=requirements))

    assert dag.nodes["lexicon:e2e_login_journey"].attributes["runtime_requirements"] == requirements


def test_lexicon_unknown_keys_are_not_registered_as_expected_attributes(tmp_path):
    dag = _build_project(tmp_path, _artifact(vendor_extension={"ignored": True}))

    assert "vendor_extension" not in dag.nodes["lexicon:e2e_login_journey"].attributes


def test_plan_task_lexicon_output_creates_produces_edge_to_expected_node(tmp_path):
    dag = _build_project(tmp_path, _artifact(), ["lexicon:e2e_login_journey"])

    edge = _edge(dag, "implementation_plan.md#E2E-LOGIN", "lexicon:e2e_login_journey")
    assert edge.kind == "produces"


def test_plan_task_lexicon_output_copies_journey_to_edge_attributes(tmp_path):
    dag = _build_project(tmp_path, _artifact(journey="login_to_dashboard"), ["lexicon:e2e_login_journey"])

    edge = _edge(dag, "implementation_plan.md#E2E-LOGIN", "lexicon:e2e_login_journey")
    assert edge.attributes == {"journey": "login_to_dashboard"}


def test_unknown_lexicon_output_warns_without_creating_orphan_edge(tmp_path, caplog):
    _write_lexicon(tmp_path, [])
    _write_plan(tmp_path, ["lexicon:missing_journey"])

    with caplog.at_level(logging.WARNING, logger="codd.dag.builder"):
        dag = build_dag(tmp_path, _settings())

    assert "unknown lexicon expected output: lexicon:missing_journey" in caplog.text
    assert all(edge.to_id != "lexicon:missing_journey" for edge in dag.edges)


def test_existing_plan_task_output_without_lexicon_prefix_still_produces_file_edge(tmp_path):
    _write(tmp_path / "src" / "feature.ts", "export const feature = true;\n")
    _write_plan(tmp_path, ["src/feature.ts"])

    dag = build_dag(tmp_path, _settings())

    edge = _edge(dag, "implementation_plan.md#E2E-LOGIN", "src/feature.ts")
    assert edge.attributes is None


def test_lexicon_entry_without_journey_still_gets_produces_edge(tmp_path):
    dag = _build_project(tmp_path, _artifact(), ["lexicon:e2e_login_journey"])

    edge = _edge(dag, "implementation_plan.md#E2E-LOGIN", "lexicon:e2e_login_journey")
    assert edge.attributes is None


def test_edge_attributes_none_serializes_without_attributes_key(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="plan", kind="plan_task"))
    dag.add_node(Node(id="src/feature.ts", kind="impl_file", path="src/feature.ts"))
    dag.add_edge(Edge(from_id="plan", to_id="src/feature.ts", kind="produces", attributes=None))

    payload = dag_to_dict(dag, tmp_path)

    assert "attributes" not in payload["edges"][0]
    json.dumps(payload)


def test_journey_attribute_enables_plan_task_to_expected_traversal(tmp_path):
    dag = _build_project(tmp_path, _artifact(journey="login_to_dashboard"), ["lexicon:e2e_login_journey"])

    journey_edges = [
        edge
        for edge in dag.edges
        if edge.kind == "produces" and (edge.attributes or {}).get("journey") == "login_to_dashboard"
    ]

    assert [(edge.from_id, edge.to_id) for edge in journey_edges] == [
        ("implementation_plan.md#E2E-LOGIN", "lexicon:e2e_login_journey")
    ]


def test_existing_lexicon_entries_without_new_optional_keys_remain_valid(tmp_path):
    _write(tmp_path / "src" / "login.ts", "export const login = true;\n")
    _write(tmp_path / "src" / "logout.ts", "export const logout = true;\n")
    _write_lexicon(
        tmp_path,
        [
            _artifact(id="login_flow", path="src/login.ts"),
            _artifact(id="logout_flow", path="src/logout.ts"),
        ],
    )

    dag = build_dag(tmp_path, _settings())

    assert {"lexicon:login_flow", "lexicon:logout_flow"} <= set(dag.nodes)
    assert "journey" not in dag.nodes["lexicon:login_flow"].attributes

import json
from pathlib import Path

import codd.dag.checks.depends_on_consistency as depends_on_module
from codd.dag import DAG, Edge, Node
from codd.dag.checks import get_registry
from codd.dag.checks.depends_on_consistency import (
    ConsistencyViolation,
    DependsOnConsistencyCheck,
)


def _write_propagation_output(project_root: Path, payload: dict) -> Path:
    output_path = project_root / ".codd" / "propagation_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    return output_path


def _dag_with_depends_on(from_node: str = "docs/design/ux.md", to_node: str = "docs/design/api.md") -> DAG:
    dag = DAG()
    dag.add_node(Node(id=from_node, kind="design_doc", path=from_node))
    dag.add_node(Node(id=to_node, kind="design_doc", path=to_node))
    dag.add_edge(Edge(from_id=from_node, to_id=to_node, kind="depends_on"))
    return dag


def _run(tmp_path: Path, payload: dict, dag: DAG | None = None):
    _write_propagation_output(tmp_path, payload)
    return DependsOnConsistencyCheck().run(dag or _dag_with_depends_on(), tmp_path, {})


def test_depends_on_consistency_registered():
    assert depends_on_module.DependsOnConsistencyCheck is get_registry()["depends_on_consistency"]


def test_no_propagation_output_skip_with_warn(tmp_path):
    result = DependsOnConsistencyCheck().run(_dag_with_depends_on(), tmp_path, {})

    assert result.passed is True
    assert result.skipped is True
    assert result.warnings
    assert "WARN" in result.warnings[0]


def test_consistent_url_pass(tmp_path):
    result = _run(
        tmp_path,
        {
            "values": [
                {"node_id": "docs/design/ux.md", "value_type": "url", "name": "dashboard", "value": "/tenant/dashboard"},
                {"node_id": "docs/design/api.md", "value_type": "url", "name": "dashboard", "value": "/tenant/dashboard"},
            ]
        },
    )

    assert result.passed is True
    assert result.violations == []


def test_inconsistent_url_fail(tmp_path):
    result = _run(
        tmp_path,
        {
            "values": [
                {"node_id": "docs/design/ux.md", "value_type": "url", "name": "dashboard", "value": "/tenant/dashboard"},
                {"node_id": "docs/design/api.md", "value_type": "url", "name": "dashboard", "value": "/tenant-admin"},
            ]
        },
    )

    assert result.passed is False
    assert result.violations == [
        ConsistencyViolation(
            from_node="docs/design/ux.md",
            to_node="docs/design/api.md",
            edge_kind="depends_on",
            value_type="url",
            from_value="/tenant/dashboard",
            to_value="/tenant-admin",
        )
    ]


def test_inconsistent_type_fail(tmp_path):
    result = _run(
        tmp_path,
        {
            "values_by_node": {
                "docs/design/ux.md": {"type": {"User.role": "learner|admin"}},
                "docs/design/api.md": {"type": {"User.role": "student|admin"}},
            }
        },
    )

    assert result.passed is False
    assert result.violations[0].value_type == "type"


def test_multiple_violations_collected(tmp_path):
    result = _run(
        tmp_path,
        {
            "values": [
                {"node_id": "docs/design/ux.md", "value_type": "url", "name": "login", "value": "/login"},
                {"node_id": "docs/design/api.md", "value_type": "url", "name": "login", "value": "/signin"},
                {"node_id": "docs/design/ux.md", "value_type": "constant", "name": "MAX_RETRIES", "value": "3"},
                {"node_id": "docs/design/api.md", "value_type": "constant", "name": "MAX_RETRIES", "value": "5"},
            ]
        },
    )

    assert result.passed is False
    assert [violation.value_type for violation in result.violations] == ["constant", "url"]


def test_empty_dag_pass(tmp_path):
    result = _run(
        tmp_path,
        {
            "values": [
                {"node_id": "docs/design/ux.md", "value_type": "url", "name": "dashboard", "value": "/a"},
                {"node_id": "docs/design/api.md", "value_type": "url", "name": "dashboard", "value": "/b"},
            ]
        },
        DAG(),
    )

    assert result.passed is True
    assert result.violations == []


def test_severity_is_red(tmp_path):
    result = _run(tmp_path, {"values": []})

    assert result.severity == "red"


def test_violation_dataclass_fields():
    violation = ConsistencyViolation(
        from_node="a.md",
        to_node="b.md",
        edge_kind="depends_on",
        value_type="url",
        from_value="/a",
        to_value="/b",
    )

    assert violation.from_node == "a.md"
    assert violation.to_node == "b.md"
    assert violation.edge_kind == "depends_on"
    assert violation.value_type == "url"
    assert violation.from_value == "/a"
    assert violation.to_value == "/b"


def test_passed_flag_true_on_consistent(tmp_path):
    result = _run(
        tmp_path,
        {
            "comparisons": [
                {
                    "from_node": "docs/design/ux.md",
                    "to_node": "docs/design/api.md",
                    "edge_kind": "depends_on",
                    "value_type": "url",
                    "from_value": "/api/users",
                    "to_value": "/api/users",
                }
            ]
        },
    )

    assert result.passed is True


def test_passed_flag_false_on_violation(tmp_path):
    result = _run(
        tmp_path,
        {
            "comparisons": [
                {
                    "from_node": "docs/design/ux.md",
                    "to_node": "docs/design/api.md",
                    "edge_kind": "depends_on",
                    "value_type": "path",
                    "from_value": "/tenant/dashboard",
                    "to_value": "/tenant-admin",
                }
            ]
        },
    )

    assert result.passed is False


def test_propagation_output_consumed_not_duplicated(tmp_path, monkeypatch):
    import codd.propagator as propagator

    def fail_if_called(*args, **kwargs):
        raise AssertionError("depends_on_consistency must consume saved propagation output")

    monkeypatch.setattr(propagator, "run_propagate", fail_if_called)
    result = _run(
        tmp_path,
        {
            "propagations": [
                {
                    "from_node": "docs/design/api.md",
                    "to_node": "docs/design/ux.md",
                    "edge_kind": "depends_on",
                    "values": [
                        {"value_type": "url", "from_value": "/api/users", "to_value": "/api/members"}
                    ],
                }
            ]
        },
    )

    assert result.passed is False
    assert result.violations[0].from_value == "/api/users"

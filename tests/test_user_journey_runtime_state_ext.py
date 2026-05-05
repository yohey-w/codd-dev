from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.deployment import RuntimeStateKind
from codd.deployment.extractor import (
    extract_deployment_docs,
    extract_runtime_states,
    infer_capabilities_provided,
)


def _deploy_yaml(target_type: str, *, healthcheck_url: str | None = None) -> dict:
    target: dict = {"type": target_type}
    if healthcheck_url is not None:
        target["healthcheck"] = {"url": healthcheck_url}
    return {"targets": {"production": target}}


def _write_yaml(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _dag_settings() -> dict:
    return {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.ts"],
        "test_file_patterns": ["tests/**/*.ts"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }


def test_docker_compose_target_infers_container_runtime_and_server_running():
    assert infer_capabilities_provided(_deploy_yaml("docker_compose"), {}) == [
        "container_runtime",
        "server_running",
    ]


def test_https_healthcheck_url_infers_tls_termination():
    capabilities = infer_capabilities_provided(_deploy_yaml("unknown", healthcheck_url="https://example.test/health"), {})

    assert "tls_termination" in capabilities


def test_http_healthcheck_url_does_not_infer_tls_termination():
    capabilities = infer_capabilities_provided(_deploy_yaml("unknown", healthcheck_url="http://example.test/health"), {})

    assert "tls_termination" not in capabilities


def test_vercel_target_infers_tls_termination_and_serverless_runtime():
    assert infer_capabilities_provided(_deploy_yaml("vercel"), {}) == [
        "tls_termination",
        "serverless_runtime",
    ]


def test_unknown_target_type_returns_empty_capabilities():
    assert infer_capabilities_provided(_deploy_yaml("bare_metal"), {}) == []


def test_codd_yaml_runtime_capability_inference_can_override_defaults():
    config = {
        "coherence": {
            "runtime_capability_inference": {
                "inference_rules": [
                    {
                        "target_type": "edge_platform",
                        "capabilities": ["tls_termination", "edge_runtime"],
                    }
                ]
            }
        }
    }

    assert infer_capabilities_provided(_deploy_yaml("edge_platform"), config) == [
        "tls_termination",
        "edge_runtime",
    ]


def test_codd_yaml_runtime_capability_inference_replaces_default_rules():
    config = {
        "coherence": {
            "runtime_capability_inference": {
                "inference_rules": [
                    {
                        "target_type": "edge_platform",
                        "capabilities": ["edge_runtime"],
                    }
                ]
            }
        }
    }

    assert infer_capabilities_provided(_deploy_yaml("docker_compose"), config) == []


def test_runtime_capability_inference_absent_uses_default_yaml_rules():
    assert infer_capabilities_provided(_deploy_yaml("k8s"), {}) == [
        "container_runtime",
        "server_running",
        "orchestrated_containers",
    ]


def test_extract_runtime_states_assigns_capabilities_from_deploy_yaml(tmp_path):
    _write_yaml(
        tmp_path / "deploy.yaml",
        {
            "targets": {"production": {"type": "docker_compose", "steps": [{"name": "start server"}]}},
        },
    )

    states = extract_runtime_states(tmp_path, extract_deployment_docs(tmp_path), [])

    assert states[0].kind is RuntimeStateKind.SERVER_RUNNING
    assert states[0].capabilities_provided == ["container_runtime", "server_running"]


def test_capabilities_provided_is_registered_on_runtime_state_node_attributes(tmp_path):
    _write_yaml(
        tmp_path / "deploy.yaml",
        {
            "targets": {
                "production": {
                    "type": "docker_compose",
                    "steps": [{"name": "start server"}],
                    "healthcheck": {"url": "https://example.test/health"},
                }
            },
        },
    )

    dag = build_dag(tmp_path, _dag_settings())
    node = dag.nodes["runtime:server_running:server"]

    assert node.attributes["capabilities_provided"] == [
        "container_runtime",
        "server_running",
        "tls_termination",
    ]


def test_generality_gate_target_type_names_are_not_hardcoded_in_extractor():
    source = Path("codd/deployment/extractor.py").read_text(encoding="utf-8")

    assert '"docker_compose"' not in source
    assert '"k8s"' not in source
    assert '"vercel"' not in source
    assert '"azure"' not in source


def test_no_new_runtime_state_kind_values_are_added():
    assert {kind.value for kind in RuntimeStateKind} == {
        "db_schema",
        "db_seed",
        "server_running",
        "env_var_set",
        "file_present",
    }

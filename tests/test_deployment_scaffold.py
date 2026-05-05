from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
    DeploymentDocNode,
    RuntimeStateKind,
    RuntimeStateNode,
    VerificationKind,
    VerificationTestNode,
)
from codd.deployment.checks import DEPLOYMENT_CHECKS, register_deployment_check
from codd.deployment.providers import (
    DEPLOY_TARGETS,
    SCHEMA_PROVIDERS,
    VERIFICATION_TEMPLATES,
    DeployTarget,
    SchemaProvider,
    VerificationResult,
    VerificationTemplate,
    register_deploy_target,
    register_schema_provider,
    register_verification_template,
)


def test_deployment_doc_node_serializes_defaults():
    node = DeploymentDocNode(path="DEPLOYMENT.md")

    assert asdict(node) == {
        "path": "DEPLOYMENT.md",
        "sections": [],
        "deploy_target_ref": None,
        "depends_on": [],
    }


def test_runtime_state_node_serializes_enum_and_check_command():
    node = RuntimeStateNode(
        identifier="runtime:db:users_table_seeded",
        kind=RuntimeStateKind.DB_SEED,
        target="users",
        expected_value={"rows": ">=1"},
        actual_check_command="npx prisma db seed",
    )

    payload = asdict(node)
    assert payload["identifier"] == "runtime:db:users_table_seeded"
    assert payload["kind"] is RuntimeStateKind.DB_SEED
    assert payload["target"] == "users"
    assert payload["expected_value"] == {"rows": ">=1"}
    assert payload["actual_check_command"] == "npx prisma db seed"


def test_verification_test_node_serializes_expected_outcome():
    node = VerificationTestNode(
        identifier="verification:smoke:login_endpoint",
        kind=VerificationKind.SMOKE,
        target="/api/auth/login",
        verification_template_ref="curl",
        expected_outcome={"status": 200},
    )

    assert asdict(node) == {
        "identifier": "verification:smoke:login_endpoint",
        "kind": VerificationKind.SMOKE,
        "target": "/api/auth/login",
        "verification_template_ref": "curl",
        "expected_outcome": {"status": 200},
    }


def test_runtime_and_verification_kind_values_are_stable():
    assert RuntimeStateKind.DB_SCHEMA.value == "db_schema"
    assert RuntimeStateKind.DB_SEED.value == "db_seed"
    assert RuntimeStateKind.SERVER_RUNNING.value == "server_running"
    assert RuntimeStateKind.ENV_VAR_SET.value == "env_var_set"
    assert RuntimeStateKind.FILE_PRESENT.value == "file_present"
    assert VerificationKind.SMOKE.value == "smoke"
    assert VerificationKind.HEALTH.value == "health"
    assert VerificationKind.E2E.value == "e2e"
    assert VerificationKind.LOAD.value == "load"


def test_edge_kind_constants_are_stable():
    assert EDGE_REQUIRES_DEPLOYMENT_STEP == "requires_deployment_step"
    assert EDGE_EXECUTES_IN_ORDER == "executes_in_order"
    assert EDGE_PRODUCES_STATE == "produces_state"
    assert EDGE_VERIFIED_BY == "verified_by"


def test_register_deployment_check_adds_class(monkeypatch):
    monkeypatch.setitem(DEPLOYMENT_CHECKS, "existing", object)

    @register_deployment_check("deployment_completeness")
    class DeploymentCompletenessCheck:
        pass

    assert DEPLOYMENT_CHECKS["existing"] is object
    assert DEPLOYMENT_CHECKS["deployment_completeness"] is DeploymentCompletenessCheck


def test_empty_registries_are_initially_graceful():
    assert isinstance(SCHEMA_PROVIDERS, dict)
    assert isinstance(DEPLOY_TARGETS, dict)
    assert isinstance(VERIFICATION_TEMPLATES, dict)


def test_provider_registrars_add_classes_and_result_object():
    @register_schema_provider("dummy_schema")
    class DummySchemaProvider(SchemaProvider):
        def extract_schema(self, project_root):
            return {"root": project_root}

        def detect_seed_files(self, project_root):
            return [project_root / "seed.ts"]

        def detect_migrations(self, project_root):
            return [project_root / "migrations" / "001.sql"]

    @register_deploy_target("dummy_target")
    class DummyDeployTarget(DeployTarget):
        def parse_deploy_yaml(self, deploy_yaml):
            return deploy_yaml.get("steps", [])

        def infer_executes_in_order(self, deployment_doc):
            return deployment_doc.sections

        def get_post_deploy_hooks(self):
            return ["npm run test:smoke"]

    @register_verification_template("dummy_verification")
    class DummyVerificationTemplate(VerificationTemplate):
        def generate_test_command(self, runtime_state, test_kind: str) -> str:
            return f"verify {runtime_state.target} {test_kind}"

        def execute(self, command: str) -> VerificationResult:
            return VerificationResult(command == "pass")

    assert SCHEMA_PROVIDERS["dummy_schema"] is DummySchemaProvider
    assert DEPLOY_TARGETS["dummy_target"] is DummyDeployTarget
    assert VERIFICATION_TEMPLATES["dummy_verification"] is DummyVerificationTemplate
    result = DummyVerificationTemplate().execute("pass")
    assert result.passed is True
    assert result.output == ""
    assert result.duration == 0.0


def test_abstract_provider_base_classes_require_methods():
    with pytest.raises(TypeError):
        SchemaProvider()
    with pytest.raises(TypeError):
        DeployTarget()
    with pytest.raises(TypeError):
        VerificationTemplate()


def test_default_yaml_files_parse_and_expose_expected_defaults():
    defaults_root = Path(__file__).parents[1] / "codd" / "deployment" / "defaults"

    schema = yaml.safe_load((defaults_root / "schema_providers.yaml").read_text(encoding="utf-8"))
    targets = yaml.safe_load((defaults_root / "deploy_targets.yaml").read_text(encoding="utf-8"))
    templates = yaml.safe_load((defaults_root / "verification_templates.yaml").read_text(encoding="utf-8"))

    assert schema["default"] == "prisma"
    assert schema["providers"]["prisma"]["schema_file"] == "prisma/schema.prisma"
    assert targets["default"] == "docker_compose"
    assert targets["targets"]["docker_compose"]["service_name"] == "app"
    assert templates["templates"]["curl"]["retry"] == 3
    assert templates["templates"]["playwright"]["headless"] is True

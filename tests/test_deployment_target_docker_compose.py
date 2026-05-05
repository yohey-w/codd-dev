from __future__ import annotations

from types import SimpleNamespace

import yaml

from codd.deployment import EDGE_EXECUTES_IN_ORDER
from codd.deployment.providers import DEPLOY_TARGETS, DeployTarget
from codd.deployment.providers.target import DockerComposeTarget, DeployStep


def test_register_deploy_target_docker_compose():
    assert DEPLOY_TARGETS["docker_compose"] is DockerComposeTarget


def test_parse_deploy_yaml_returns_deploy_steps():
    deploy_yaml = {
        "targets": {
            "vps": {
                "type": "docker_compose",
                "steps": [
                    {"name": "migrate", "command": "npx prisma migrate deploy", "order": 1},
                    {"name": "seed", "command": "npx prisma db seed", "order": 2},
                ],
            }
        }
    }

    steps = DockerComposeTarget().parse_deploy_yaml(deploy_yaml)

    assert steps == [
        DeployStep(name="migrate", command="npx prisma migrate deploy", order=1),
        DeployStep(name="seed", command="npx prisma db seed", order=2),
    ]


def test_parse_deploy_yaml_without_steps_is_backward_compatible():
    deploy_yaml = {"targets": {"vps": {"type": "docker_compose", "host": "example.test"}}}

    assert DockerComposeTarget().parse_deploy_yaml(deploy_yaml) == []


def test_parse_deploy_yaml_handles_multiple_docker_compose_targets():
    deploy_yaml = {
        "targets": {
            "staging": {
                "type": "docker_compose",
                "steps": [{"name": "build", "command": "docker compose build", "order": 1}],
            },
            "production": {
                "type": "docker_compose",
                "steps": [{"name": "up", "command": "docker compose up -d", "order": 2}],
            },
        }
    }

    steps = DockerComposeTarget().parse_deploy_yaml(deploy_yaml)

    assert steps == [
        DeployStep(name="build", command="docker compose build", order=1),
        DeployStep(name="up", command="docker compose up -d", order=2),
    ]


def test_infer_executes_in_order_maps_migrate_section():
    doc = SimpleNamespace(id="deploy:vps", sections=["## Migrate"])

    assert DockerComposeTarget().infer_executes_in_order(doc) == [
        ("deploy:vps", "prisma/migrate", EDGE_EXECUTES_IN_ORDER, {"order": 1})
    ]


def test_infer_executes_in_order_maps_seed_section():
    doc = SimpleNamespace(id="deploy:vps", sections=["## Seed"])

    assert DockerComposeTarget().infer_executes_in_order(doc) == [
        ("deploy:vps", "prisma/seed.ts", EDGE_EXECUTES_IN_ORDER, {"order": 1})
    ]


def test_infer_executes_in_order_maps_build_section():
    doc = SimpleNamespace(id="deploy:vps", sections=["## Build"])

    assert DockerComposeTarget().infer_executes_in_order(doc) == [
        ("deploy:vps", "Dockerfile", EDGE_EXECUTES_IN_ORDER, {"order": 1})
    ]


def test_infer_executes_in_order_without_sections_returns_empty_list():
    doc = SimpleNamespace(id="deploy:vps")

    assert DockerComposeTarget().infer_executes_in_order(doc) == []


def test_get_post_deploy_hooks_returns_commands():
    deploy_yaml = {
        "targets": {
            "vps": {
                "type": "docker_compose",
                "post_deploy": [
                    "curl -f https://example.test/health",
                    {"command": "npm run test:smoke"},
                ],
            }
        }
    }

    hooks = DockerComposeTarget(deploy_yaml).get_post_deploy_hooks()

    assert hooks == ["curl -f https://example.test/health", "npm run test:smoke"]


def test_get_post_deploy_hooks_without_field_returns_empty_list():
    deploy_yaml = {"targets": {"vps": {"type": "docker_compose"}}}

    assert DockerComposeTarget(deploy_yaml).get_post_deploy_hooks() == []


def test_get_compose_file_detects_production_file(tmp_path):
    compose_file = tmp_path / "docker-compose.production.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    assert DockerComposeTarget().get_compose_file(tmp_path) == compose_file


def test_get_compose_file_returns_none_when_absent(tmp_path):
    assert DockerComposeTarget().get_compose_file(tmp_path) is None


def test_docker_compose_target_satisfies_deploy_target_contract():
    target = DockerComposeTarget()

    assert isinstance(target, DeployTarget)
    assert target.parse_deploy_yaml({}) == []
    assert target.infer_executes_in_order({}) == []
    assert target.get_post_deploy_hooks() == []


def test_reimport_does_not_add_unrelated_deploy_target_registry_keys():
    before_keys = set(DEPLOY_TARGETS)
    __import__("codd.deployment.providers.target.docker_compose")

    assert set(DEPLOY_TARGETS) == before_keys
    assert DEPLOY_TARGETS["docker_compose"] is DockerComposeTarget


def test_osato_lms_deploy_yaml_full_scenario(tmp_path):
    defaults_path = tmp_path / "deploy_targets.yaml"
    defaults_path.write_text(
        yaml.safe_dump(
            {
                "default": "docker_compose",
                "targets": {"docker_compose": {"compose_file": "docker-compose.production.yml"}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.production.yml").write_text("services: {}\n", encoding="utf-8")
    deploy_yaml = {
        "targets": {
            "vps": {
                "type": "docker_compose",
                "steps": [
                    {"name": "migrate", "command": "npx prisma migrate deploy", "order": 1},
                    {"name": "seed", "command": "npx prisma db seed", "order": 2},
                    {"name": "build", "command": "docker compose build", "order": 3},
                ],
                "post_deploy": ["npm run test:smoke"],
            }
        }
    }
    target = DockerComposeTarget(deploy_yaml, defaults_path=defaults_path)

    assert target.parse_deploy_yaml(deploy_yaml) == [
        DeployStep("migrate", "npx prisma migrate deploy", 1),
        DeployStep("seed", "npx prisma db seed", 2),
        DeployStep("build", "docker compose build", 3),
    ]
    assert target.get_post_deploy_hooks() == ["npm run test:smoke"]
    assert target.get_compose_file(tmp_path) == tmp_path / "docker-compose.production.yml"
    assert target.infer_executes_in_order(
        SimpleNamespace(id="deploy:vps", sections=["## Migrate", "## Seed", "## Build"])
    ) == [
        ("deploy:vps", "prisma/migrate", EDGE_EXECUTES_IN_ORDER, {"order": 1}),
        ("deploy:vps", "prisma/seed.ts", EDGE_EXECUTES_IN_ORDER, {"order": 2}),
        ("deploy:vps", "Dockerfile", EDGE_EXECUTES_IN_ORDER, {"order": 3}),
    ]

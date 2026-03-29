"""Tests for infrastructure, build, and test extraction backends."""

import textwrap

from codd.extractor import extract_facts


def _seed_project(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "service.py").write_text(
        textwrap.dedent(
            """\
            def handler() -> str:
                return "ok"
            """
        )
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_service_behavior.py").write_text(
        textwrap.dedent(
            """\
            import pytest

            from service import handler

            @pytest.fixture
            def sample_client():
                return "client"

            def test_handler(sample_client):
                assert handler() == "ok"
            """
        )
    )

    return tmp_path


def test_extracts_docker_compose_and_kubernetes(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "docker-compose.yml").write_text(
        textwrap.dedent(
            """\
            services:
              api:
                image: ghcr.io/example/api:latest
                depends_on:
                  - db
                ports:
                  - "8000:8000"
                volumes:
                  - .:/app
              db:
                image: postgres:16
            """
        )
    )
    (project_root / "k8s.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: api
            spec:
              replicas: 2
              template:
                spec:
                  containers:
                    - name: api
                      image: ghcr.io/example/api:latest
                      ports:
                        - containerPort: 8000
            ---
            apiVersion: v1
            kind: Service
            metadata:
              name: api
            spec:
              type: ClusterIP
              selector:
                app: api
              ports:
                - port: 80
                  targetPort: 8000
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    compose = facts.infra_config["docker-compose.yml"]
    assert compose.format == "docker-compose"
    api_service = next(service for service in compose.services if service["name"] == "api")
    assert api_service["depends_on"] == ["db"]
    assert api_service["ports"] == ["8000:8000"]
    assert api_service["volumes"] == [".:/app"]

    k8s = facts.infra_config["k8s.yaml"]
    assert k8s.format == "kubernetes"
    deployment = next(resource for resource in k8s.resources if resource["kind"] == "Deployment")
    service = next(resource for resource in k8s.resources if resource["kind"] == "Service")
    assert deployment["replicas"] == 2
    assert deployment["containers"][0]["image"] == "ghcr.io/example/api:latest"
    assert service["ports"][0]["targetPort"] == 8000


def test_extracts_terraform_build_deps_and_test_mapping(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "main.tf").write_text(
        textwrap.dedent(
            """\
            resource "aws_s3_bucket" "assets" {
              bucket = "demo-assets"
            }

            module "network" {
              source = "./modules/network"
            }

            variable "aws_region" {
              type = string
            }
            """
        )
    )
    (project_root / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "infra-test"
            dependencies = ["fastapi>=0.110", "pyyaml>=6.0"]

            [project.optional-dependencies]
            dev = ["pytest>=8.0"]

            [project.scripts]
            infra-test = "service:handler"
            """
        )
    )
    (project_root / "package.json").write_text(
        textwrap.dedent(
            """\
            {
              "dependencies": {
                "react": "^19.0.0"
              },
              "devDependencies": {
                "vitest": "^2.0.0"
              },
              "scripts": {
                "build": "vite build"
              }
            }
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    terraform = facts.infra_config["main.tf"]
    assert terraform.format == "terraform"
    assert ("resource", "aws_s3_bucket", "assets") in {
        (resource["kind"], resource.get("type"), resource["name"])
        for resource in terraform.resources
    }
    assert ("module", None, "network") in {
        (resource["kind"], resource.get("type"), resource["name"])
        for resource in terraform.resources
    }
    assert ("variable", None, "aws_region") in {
        (resource["kind"], resource.get("type"), resource["name"])
        for resource in terraform.resources
    }

    assert facts.build_deps is not None
    assert "fastapi>=0.110" in facts.build_deps.runtime
    assert "react" in facts.build_deps.runtime
    assert "pytest>=8.0" in facts.build_deps.dev
    assert "vitest" in facts.build_deps.dev
    assert facts.build_deps.scripts["build"] == "vite build"
    assert facts.build_deps.scripts["infra-test"] == "service:handler"

    module = facts.modules["service"]
    assert "tests/test_service_behavior.py" in module.test_files
    test_info = module.test_details[0]
    assert test_info.source_module == "service"
    assert test_info.test_functions == ["test_handler"]
    assert test_info.fixtures == ["sample_client"]


def test_gracefully_skips_when_optional_files_are_absent(tmp_path):
    project_root = _seed_project(tmp_path)

    facts = extract_facts(project_root, "python", ["src"])

    assert facts.infra_config == {}
    assert facts.build_deps is None
    assert facts.modules["service"].test_files == ["tests/test_service_behavior.py"]

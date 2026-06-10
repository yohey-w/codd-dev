"""Tests for infrastructure, build, and test extraction backends."""

import textwrap

from codd.extractor import extract_facts
from codd.parsing import (
    DockerfileExtractor,
    GitHubActionsExtractor,
    KubernetesExtractor,
    OpsEvidenceExtractor,
    TerraformExtractor,
)


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


# ---------------------------------------------------------------------------
# Expanded Kubernetes kinds (R1)
# ---------------------------------------------------------------------------
def test_kubernetes_extended_kinds_and_workload_internals():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: StatefulSet
        metadata: {name: db, namespace: prod}
        spec:
          replicas: 3
          serviceName: db
          template:
            spec:
              containers:
                - name: db
                  image: postgres:16
                  resources:
                    requests: {cpu: "500m", memory: "512Mi"}
                    limits: {cpu: "1", memory: "1Gi"}
                  livenessProbe: {httpGet: {path: /h, port: 5432}}
                  readinessProbe: {tcpSocket: {port: 5432}}
                  startupProbe: {tcpSocket: {port: 5432}}
        ---
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata: {name: api-hpa}
        spec:
          scaleTargetRef: {kind: Deployment, name: api}
          minReplicas: 2
          maxReplicas: 10
          metrics:
            - type: Resource
              resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
        ---
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata: {name: deny-all}
        spec:
          podSelector: {}
          policyTypes: [Ingress, Egress]
          ingress:
            - from: [{podSelector: {matchLabels: {app: api}}}]
        ---
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata: {name: api-pdb}
        spec: {minAvailable: 2}
        ---
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata: {name: data}
        spec:
          accessModes: [ReadWriteOnce]
          storageClassName: fast
          resources: {requests: {storage: 20Gi}}
        ---
        apiVersion: batch/v1
        kind: CronJob
        metadata: {name: nightly}
        spec:
          schedule: "0 2 * * *"
          jobTemplate:
            spec:
              template:
                spec:
                  containers:
                    - name: job
                      image: job:1
        ---
        apiVersion: apps/v1
        kind: DaemonSet
        metadata: {name: agent}
        spec:
          template:
            spec:
              containers:
                - name: agent
                  image: agent:1
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "k8s.yaml")
    by_kind = {r["kind"]: r for r in info.resources}

    # All new kinds parsed.
    for kind in (
        "StatefulSet",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "PersistentVolumeClaim",
        "CronJob",
        "DaemonSet",
    ):
        assert kind in by_kind, kind

    sset = by_kind["StatefulSet"]
    assert sset["replicas"] == 3
    assert sset["service_name"] == "db"
    assert sset["namespace"] == "prod"
    container = sset["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "500m"
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert container["probes"] == {
        "livenessProbe": True,
        "readinessProbe": True,
        "startupProbe": True,
    }

    hpa = by_kind["HorizontalPodAutoscaler"]
    assert hpa["min_replicas"] == 2
    assert hpa["max_replicas"] == 10
    assert hpa["scale_target"] == {"kind": "Deployment", "name": "api"}
    assert hpa["metrics"][0]["name"] == "cpu"

    np = by_kind["NetworkPolicy"]
    assert np["policy_types"] == ["Ingress", "Egress"]
    assert np["ingress_rules"] == 1
    assert np["egress_rules"] == 0

    assert by_kind["PodDisruptionBudget"]["min_available"] == 2

    pvc = by_kind["PersistentVolumeClaim"]
    assert pvc["access_modes"] == ["ReadWriteOnce"]
    assert pvc["storage"] == "20Gi"
    assert pvc["storage_class"] == "fast"

    assert by_kind["CronJob"]["schedule"] == "0 2 * * *"


def test_kubernetes_extended_kinds_discovered_via_extract_facts(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "hpa.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: autoscaling/v2
            kind: HorizontalPodAutoscaler
            metadata: {name: api-hpa}
            spec: {scaleTargetRef: {kind: Deployment, name: api}, minReplicas: 2, maxReplicas: 6}
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    assert "hpa.yaml" in facts.infra_config
    hpa = facts.infra_config["hpa.yaml"].resources[0]
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    assert hpa["max_replicas"] == 6


# ---------------------------------------------------------------------------
# Terraform NFR-relevant attribute flags (R1)
# ---------------------------------------------------------------------------
def test_terraform_surfaces_nfr_relevant_attribute_flags():
    tf = textwrap.dedent(
        """\
        resource "aws_db_instance" "main" {
          multi_az = true
          backup_retention_period = 7
          deletion_protection = true
          tags = {
            Name = "main"
          }
        }
        resource "aws_s3_bucket" "plain" {
          bucket = "demo"
        }
        """
    )
    info = TerraformExtractor().extract_resources(tf, "main.tf")
    by_name = {r["name"]: r for r in info.resources}

    flags = by_name["main"]["nfr_flags"]
    assert flags["multi_az"] is True
    assert flags["backup_retention_period"] == 7
    assert flags["deletion_protection"] is True
    # Nested block keys must NOT leak into top-level NFR flags.
    assert "Name" not in flags
    # A plain bucket exposes no NFR flags.
    assert "nfr_flags" not in by_name["plain"]


# ---------------------------------------------------------------------------
# GitHub Actions CI/CD (R1)
# ---------------------------------------------------------------------------
def test_github_actions_workflow_parsing():
    workflow = textwrap.dedent(
        """\
        name: ci
        on:
          push:
            branches: [main]
          pull_request:
        jobs:
          build-test:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - name: Run tests
                run: pytest -q
              - name: Build image
                run: docker build -t app .
          deploy:
            runs-on: ubuntu-latest
            environment: production
            steps:
              - name: Deploy
                run: kubectl apply -f k8s.yaml
                env: {TOKEN: "${{ secrets.DEPLOY_TOKEN }}"}
        """
    )
    info = GitHubActionsExtractor().extract_workflow(workflow, ".github/workflows/ci.yml")
    pipeline = info.pipelines[0]

    assert pipeline["name"] == "ci"
    assert set(pipeline["triggers"]) == {"push", "pull_request"}
    assert pipeline["secrets"] == ["DEPLOY_TOKEN"]

    jobs = {j["name"]: j for j in pipeline["jobs"]}
    build_test = jobs["build-test"]
    step_kinds = {kind for step in build_test["steps"] for kind in step["kinds"]}
    assert "test" in step_kinds
    assert "build" in step_kinds

    deploy = jobs["deploy"]
    assert deploy["environment"] == "production"
    assert any("deploy" in step["kinds"] for step in deploy["steps"])


def test_github_actions_tolerates_non_mapping_yaml():
    info = GitHubActionsExtractor().extract_workflow("- just\n- a\n- list\n", "wf.yml")
    assert info.pipelines == []


def test_github_actions_discovered_via_extract_facts(tmp_path):
    project_root = _seed_project(tmp_path)
    workflows = project_root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        textwrap.dedent(
            """\
            name: ci
            on: {push: {}}
            jobs:
              t:
                runs-on: ubuntu-latest
                steps:
                  - run: pytest
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    assert ".github/workflows/ci.yml" in facts.infra_config
    config = facts.infra_config[".github/workflows/ci.yml"]
    assert config.format == "github-actions"
    assert config.pipelines[0]["name"] == "ci"


# ---------------------------------------------------------------------------
# Dockerfile (R1)
# ---------------------------------------------------------------------------
def test_dockerfile_parsing():
    dockerfile = textwrap.dedent(
        """\
        # comment
        FROM node:20 AS builder
        RUN npm ci && npm run build
        FROM nginx:1.27
        EXPOSE 80 443/tcp
        ENTRYPOINT ["/docker-entrypoint.sh"]
        CMD ["nginx", "-g", "daemon off;"]
        """
    )
    info = DockerfileExtractor().extract_dockerfile(dockerfile, "Dockerfile")
    image = info.images[0]

    assert image["base_images"] == ["node:20", "nginx:1.27"]
    assert image["stages"] == ["builder"]
    assert image["ports"] == ["80", "443"]
    assert image["entrypoint"] == '["/docker-entrypoint.sh"]'
    assert image["cmd"] == '["nginx", "-g", "daemon off;"]'


def test_dockerfile_discovered_via_extract_facts(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n")

    facts = extract_facts(project_root, "python", ["src"])

    assert "Dockerfile" in facts.infra_config
    config = facts.infra_config["Dockerfile"]
    assert config.format == "dockerfile"
    assert config.images[0]["ports"] == ["8000"]


# ---------------------------------------------------------------------------
# Ops-evidence recognition (R1)
# ---------------------------------------------------------------------------
def test_ops_evidence_recognizes_prometheus_helm_ansible(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "app.rules.yml").write_text("groups: []\n")
    (project_root / "Chart.yaml").write_text("name: app\nversion: 0.1.0\n")
    playbooks = project_root / "ansible"
    playbooks.mkdir()
    (playbooks / "playbook.yml").write_text("- hosts: all\n")

    extractor = OpsEvidenceExtractor()
    recognized = {
        path.relative_to(project_root).as_posix(): kind
        for path, kind in extractor.detect_ops_files(project_root)
    }

    assert recognized["app.rules.yml"] == "prometheus_rules"
    assert recognized["Chart.yaml"] == "helm_chart"
    assert recognized["ansible/playbook.yml"] == "ansible_playbook"


def test_ops_evidence_surfaced_in_extract_facts(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "prometheus.rules.yml").write_text("groups: []\n")

    facts = extract_facts(project_root, "python", ["src"])

    assert "prometheus.rules.yml" in facts.infra_config
    config = facts.infra_config["prometheus.rules.yml"]
    assert config.format == "ops-evidence"
    assert config.recognized_kind == "prometheus_rules"

"""Tests for infrastructure, build, and test extraction backends."""

import textwrap
from pathlib import Path

import codd.parsing as codd_parsing
from codd.extractor import extract_facts
from codd.parsing import (
    AnsibleExtractor,
    DockerfileExtractor,
    GitHubActionsExtractor,
    KubernetesExtractor,
    OpsEvidenceExtractor,
    PrometheusRulesExtractor,
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
# Terraform: python-hcl2 core promotion — both modes pinned explicitly (H1)
# ---------------------------------------------------------------------------
# A realistic multi-resource fixture with nested blocks and lists. The hcl2
# path must capture the full attribute tree (including nested blocks); the
# regex fallback only captures top-level scalar assignments.
_TF_NESTED_FIXTURE = textwrap.dedent(
    """\
    resource "aws_db_instance" "main" {
      engine                  = "postgres"
      multi_az                = true
      backup_retention_period = 7
      storage_encrypted       = true
      tags = {
        Tier = "data"
      }
    }

    resource "aws_autoscaling_group" "web" {
      min_size         = 2
      max_size         = 8
      desired_capacity = 3

      launch_template {
        id      = "lt-123"
        version = "$Latest"
      }
    }

    resource "aws_instance" "worker" {
      ami           = "ami-123"
      instance_type = "t3.small"

      ebs_block_device {
        device_name = "/dev/sdf"
        encrypted   = true
        volume_size = 100
      }
    }
    """
)


def test_python_hcl2_is_a_core_dependency_not_an_extra():
    """The hcl2 import must be importable by default (promoted from `infra`)."""

    # The module-level import in codd.parsing must have succeeded.
    assert codd_parsing.hcl2 is not None
    assert TerraformExtractor.is_available()

    # And pyproject.toml must declare it in core [project] dependencies so a
    # plain `pip install` brings full-attribute Terraform parsing by default.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    core = [dep for dep in payload["project"]["dependencies"]]
    assert any(dep.startswith("python-hcl2") for dep in core)


def test_terraform_hcl2_mode_captures_nested_blocks_and_nested_nfr_flags():
    """hcl2-present mode: full attribute tree, including nested blocks."""

    assert codd_parsing.hcl2 is not None  # this test pins the hcl2 path
    info = TerraformExtractor().extract_resources(_TF_NESTED_FIXTURE, "main.tf")
    by_name = {r["name"]: r for r in info.resources}

    # Top-level scalars + NFR flags.
    db = by_name["main"]
    assert db["attributes"]["engine"] == "postgres"
    assert db["nfr_flags"]["multi_az"] is True
    assert db["nfr_flags"]["backup_retention_period"] == 7
    assert db["nfr_flags"]["storage_encrypted"] is True
    # Full tree: the nested tags map is captured as a real dict.
    assert db["attributes"]["tags"]["Tier"] == "data"

    asg = by_name["web"]
    assert asg["nfr_flags"]["min_size"] == 2
    assert asg["nfr_flags"]["max_size"] == 8
    assert asg["nfr_flags"]["desired_capacity"] == 3
    # Nested launch_template block is present in the attribute tree.
    assert "launch_template" in asg["attributes"]

    # The decisive richer-than-regex capture: an NFR-relevant attribute that
    # only exists INSIDE a nested block is surfaced into nfr_flags.
    worker = by_name["worker"]
    assert "ebs_block_device" in worker["attributes"]
    assert worker["nfr_flags"]["encrypted"] is True


def test_terraform_regex_fallback_mode_degrades_gracefully(monkeypatch):
    """hcl2-absent mode: top-level scalar flags survive; nested trees do not.

    The fallback is intentionally weaker (graceful degradation), and this test
    pins exactly HOW it is weaker so neither mode can silently change.
    """

    monkeypatch.setattr(codd_parsing, "hcl2", None)
    info = TerraformExtractor().extract_resources(_TF_NESTED_FIXTURE, "main.tf")
    by_name = {r["name"]: r for r in info.resources}

    # Top-level scalar NFR flags still work without python-hcl2.
    db = by_name["main"]
    assert db["nfr_flags"]["multi_az"] is True
    assert db["nfr_flags"]["backup_retention_period"] == 7
    asg = by_name["web"]
    assert asg["nfr_flags"]["min_size"] == 2
    assert asg["nfr_flags"]["max_size"] == 8

    # Degradation: nested blocks are NOT captured in this mode...
    assert "launch_template" not in asg["attributes"]
    worker = by_name["worker"]
    assert "ebs_block_device" not in worker["attributes"]
    # ...so the nested-only NFR flag is missed (hcl2 mode catches it).
    assert "nfr_flags" not in worker


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
    # An EMPTY rules file deep-parses to nothing, so it must fall back to the
    # recognition-only path (presence evidence, not silence).
    project_root = _seed_project(tmp_path)
    (project_root / "prometheus.rules.yml").write_text("groups: []\n")

    facts = extract_facts(project_root, "python", ["src"])

    assert "prometheus.rules.yml" in facts.infra_config
    config = facts.infra_config["prometheus.rules.yml"]
    assert config.format == "ops-evidence"
    assert config.recognized_kind == "prometheus_rules"


# ---------------------------------------------------------------------------
# Ansible deep parse (H1)
# ---------------------------------------------------------------------------
_ANSIBLE_PLAYBOOK = textwrap.dedent(
    """\
    - name: Configure web servers
      hosts: webservers
      become: true
      roles:
        - common
        - role: nginx
      pre_tasks:
        - name: Update apt cache
          ansible.builtin.apt:
            update_cache: yes
      tasks:
        - name: Install nginx
          apt:
            name: nginx
            state: present
        - name: Ensure nginx running
          ansible.builtin.service:
            name: nginx
            state: started
            enabled: true
        - name: Allow http
          ufw:
            rule: allow
            port: "80"
        - name: Nightly cleanup
          cron:
            name: cleanup
            minute: "0"
            hour: "2"
            job: /usr/local/bin/cleanup.sh
        - name: Create deploy user
          user:
            name: deploy
            groups: sudo
        - block:
            - name: Render config
              template:
                src: nginx.conf.j2
                dest: /etc/nginx/nginx.conf
          rescue:
            - name: Report failure
              command: echo failed
      handlers:
        - name: Restart nginx
          service:
            name: nginx
            state: restarted
    """
)


def test_ansible_playbook_deep_parse_plays_tasks_modules_and_handlers():
    info = AnsibleExtractor().extract_ansible(_ANSIBLE_PLAYBOOK, "playbook.yml")

    assert info.format == "ansible"
    play = info.services[0]
    assert play["kind"] == "play"
    assert play["name"] == "Configure web servers"
    assert play["hosts"] == "webservers"
    assert play["become"] is True
    # Both string and dict role declarations are captured.
    assert play["roles"] == ["common", "nginx"]
    assert play["task_count"] == 9

    by_name = {r["name"]: r for r in info.resources}
    # FQCN module names are normalized to the bare module.
    assert by_name["Update apt cache"]["module"] == "apt"
    assert by_name["Ensure nginx running"]["module"] == "service"
    # Cheap scalar args are captured.
    service = by_name["Ensure nginx running"]
    assert service["attributes"]["name"] == "nginx"
    assert service["attributes"]["state"] == "started"
    assert service["attributes"]["enabled"] is True
    assert by_name["Allow http"]["module"] == "ufw"
    assert by_name["Nightly cleanup"]["module"] == "cron"
    assert by_name["Nightly cleanup"]["attributes"]["hour"] == "2"
    assert by_name["Create deploy user"]["module"] == "user"
    # block/rescue sections are recursed into.
    assert by_name["Render config"]["module"] == "template"
    assert by_name["Report failure"]["module"] == "command"
    assert by_name["Report failure"]["attributes"] == {"_raw": "echo failed"}
    # Handlers are captured and tagged distinctly.
    assert by_name["Restart nginx"]["kind"] == "handler"
    assert by_name["Restart nginx"]["module"] == "service"
    # Every task carries its parent play.
    assert by_name["Install nginx"]["parent"] == "Configure web servers"


def test_ansible_role_tasks_main_parsed_with_role_name():
    role_tasks = textwrap.dedent(
        """\
        - name: Install postgres
          apt:
            name: postgresql
            state: present
        - name: Start postgres
          service:
            name: postgresql
            state: started
        """
    )
    info = AnsibleExtractor().extract_ansible(role_tasks, "roles/database/tasks/main.yml")

    assert info.format == "ansible"
    kinds = [(r["kind"], r["name"]) for r in info.resources]
    assert ("role", "database") in kinds
    by_name = {r["name"]: r for r in info.resources if r["kind"] == "task"}
    assert by_name["Install postgres"]["module"] == "apt"
    assert by_name["Install postgres"]["parent"] == "database"
    role_entry = next(r for r in info.resources if r["kind"] == "role")
    assert role_entry["task_count"] == 2


def test_ansible_extractor_never_crashes_on_weird_input():
    extractor = AnsibleExtractor()
    # Malformed YAML.
    assert extractor.extract_ansible("{{ not: yaml", "playbook.yml").resources == []
    # Non-list top level.
    assert extractor.extract_ansible("hosts: all\n", "playbook.yml").resources == []
    # List of non-dicts / plays without hosts / tasks with only directives.
    weird = textwrap.dedent(
        """\
        - "just a string"
        - name: no hosts here
        - hosts: all
          tasks:
            - 42
            - name: directive-only task
              when: ansible_os_family == "Debian"
        """
    )
    info = extractor.extract_ansible(weird, "playbook.yml")
    # The one real play is still surfaced; junk entries are skipped.
    assert len(info.services) == 1
    assert info.services[0]["hosts"] == "all"
    assert info.resources == []


def test_ansible_detection_by_name_site_and_content_sniff(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "playbook.yml").write_text(_ANSIBLE_PLAYBOOK)
    (project_root / "site.yml").write_text("- hosts: all\n  tasks: []\n")
    # Arbitrary name, but the top level is a list of plays with hosts:.
    (project_root / "provision-db.yml").write_text(
        "- hosts: dbservers\n  tasks:\n    - name: x\n      apt: {name: pg}\n"
    )
    # Arbitrary name and NOT a playbook shape -> not detected.
    (project_root / "data.yml").write_text("- 1\n- 2\n")

    detected = {
        path.relative_to(project_root).as_posix()
        for path in AnsibleExtractor().detect_ansible_files(project_root)
    }
    assert "playbook.yml" in detected
    assert "site.yml" in detected
    assert "provision-db.yml" in detected
    assert "data.yml" not in detected


def test_ansible_deep_parse_surfaced_in_extract_facts_with_fallback(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "playbook.yml").write_text(_ANSIBLE_PLAYBOOK)
    role_dir = project_root / "roles" / "web" / "tasks"
    role_dir.mkdir(parents=True)
    (role_dir / "main.yml").write_text(
        "- name: Install nginx\n  apt: {name: nginx}\n"
    )
    # A recognized-but-unparseable playbook must FALL BACK to recognition-only.
    (project_root / "playbook_broken.yml").write_text("{{ definitely not yaml\n")

    facts = extract_facts(project_root, "python", ["src"])

    playbook = facts.infra_config["playbook.yml"]
    assert playbook.format == "ansible"
    assert playbook.services[0]["hosts"] == "webservers"
    assert any(r["module"] == "service" for r in playbook.resources if r["kind"] == "task")

    role = facts.infra_config["roles/web/tasks/main.yml"]
    assert role.format == "ansible"
    assert any(r["kind"] == "role" and r["name"] == "web" for r in role.resources)

    broken = facts.infra_config["playbook_broken.yml"]
    assert broken.format == "ops-evidence"
    assert broken.recognized_kind == "ansible_playbook"


# ---------------------------------------------------------------------------
# Prometheus / Alertmanager deep parse (H1)
# ---------------------------------------------------------------------------
_PROMETHEUS_RULES = textwrap.dedent(
    """\
    groups:
      - name: availability
        interval: 30s
        rules:
          - alert: HighErrorRate
            expr: rate(http_errors_total[5m]) / rate(http_requests_total[5m]) > 0.01
            for: 5m
            labels:
              severity: critical
            annotations:
              summary: Error rate above 1%
              description: 5xx error rate exceeded 1% for 5 minutes.
          - record: job:http_errors:rate5m
            expr: rate(http_errors_total[5m])
    """
)


def test_prometheus_rules_deep_parse_alert_and_recording_rules():
    info = PrometheusRulesExtractor().extract_prometheus(
        _PROMETHEUS_RULES, "monitoring/app.rules.yml"
    )

    assert info.format == "prometheus"
    by_name = {r["name"]: r for r in info.resources}

    alert = by_name["HighErrorRate"]
    assert alert["kind"] == "AlertRule"
    assert "rate(http_errors_total[5m])" in alert["attributes"]["expr"]
    assert alert["attributes"]["for"] == "5m"
    assert alert["attributes"]["severity"] == "critical"
    assert alert["attributes"]["summary"] == "Error rate above 1%"
    assert alert["attributes"]["group"] == "availability"
    assert alert["attributes"]["interval"] == "30s"

    record = by_name["job:http_errors:rate5m"]
    assert record["kind"] == "RecordingRule"
    assert record["attributes"]["expr"] == "rate(http_errors_total[5m])"


def test_prometheus_scrape_config_shallow_jobs_and_target_counts():
    scrape = textwrap.dedent(
        """\
        global:
          scrape_interval: 15s
        scrape_configs:
          - job_name: api
            static_configs:
              - targets: ['api:8000', 'api2:8000']
          - job_name: node
            static_configs:
              - targets: ['node:9100']
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(scrape, "prometheus.yml")

    jobs = {r["name"]: r for r in info.resources if r["kind"] == "ScrapeJob"}
    assert jobs["api"]["attributes"]["targets_count"] == 2
    assert jobs["node"]["attributes"]["targets_count"] == 1


def test_alertmanager_shallow_receivers_and_route_presence():
    am = textwrap.dedent(
        """\
        route:
          receiver: ops-team
          routes:
            - match: {severity: critical}
              receiver: pagerduty
        receivers:
          - name: ops-team
            email_configs: [{to: ops@example.test}]
          - name: pagerduty
            pagerduty_configs: [{service_key: redacted}]
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(am, "alertmanager.yml")

    receivers = [r["name"] for r in info.resources if r["kind"] == "AlertmanagerReceiver"]
    assert receivers == ["ops-team", "pagerduty"]
    route = next(r for r in info.resources if r["kind"] == "AlertmanagerRoute")
    assert route["name"] == "ops-team"
    assert route["attributes"]["child_routes"] == 1


def test_prometheus_extractor_never_crashes_on_weird_input():
    extractor = PrometheusRulesExtractor()
    assert extractor.extract_prometheus("{{ nope", "x.rules.yml").resources == []
    assert extractor.extract_prometheus("- a\n- b\n", "x.rules.yml").resources == []
    # Rules entries that are not dicts / have neither alert nor record.
    weird = "groups:\n  - name: g\n    rules:\n      - 42\n      - {expr: up == 0}\n"
    assert extractor.extract_prometheus(weird, "x.rules.yml").resources == []


def test_prometheus_detection_by_name_and_content_sniff(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "app.rules.yml").write_text(_PROMETHEUS_RULES)
    (project_root / "alertmanager.yml").write_text("receivers: [{name: ops}]\n")
    (project_root / "prometheus.yml").write_text("scrape_configs: []\n")
    # Arbitrary name, but rules-shaped content.
    (project_root / "slo-alerts.yaml").write_text(_PROMETHEUS_RULES)
    (project_root / "unrelated.yml").write_text("foo: bar\n")

    detected = {
        path.relative_to(project_root).as_posix()
        for path in PrometheusRulesExtractor().detect_prometheus_files(project_root)
    }
    assert {"app.rules.yml", "alertmanager.yml", "prometheus.yml", "slo-alerts.yaml"} <= detected
    assert "unrelated.yml" not in detected


def test_prometheus_deep_parse_surfaced_in_extract_facts(tmp_path):
    project_root = _seed_project(tmp_path)
    monitoring = project_root / "monitoring"
    monitoring.mkdir()
    (monitoring / "app.rules.yml").write_text(_PROMETHEUS_RULES)

    facts = extract_facts(project_root, "python", ["src"])

    config = facts.infra_config["monitoring/app.rules.yml"]
    assert config.format == "prometheus"
    assert any(
        r["kind"] == "AlertRule" and r["name"] == "HighErrorRate"
        for r in config.resources
    )
    # Deep parse wins over recognition-only tagging.
    assert config.recognized_kind == ""

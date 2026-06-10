"""Tests for the deterministic IaC-resource -> NFR mapping (codd.iac_nfr)."""

from __future__ import annotations

import textwrap

from codd.iac_nfr import (
    CAT_AVAILABILITY,
    CAT_CAPACITY,
    CAT_DURABILITY,
    CAT_OBSERVABILITY,
    CAT_RELIABILITY,
    CAT_SCALABILITY,
    CAT_SECURITY,
    CAT_TOPOLOGY,
    CATEGORIES,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    KIND_NFR,
    KIND_OPERATIONAL_FACT,
    NfrCandidate,
    derive_iac_nfrs,
)
from codd.parsing import (
    DockerComposeExtractor,
    DockerfileExtractor,
    GitHubActionsExtractor,
    KubernetesExtractor,
    OpsEvidenceExtractor,
    TerraformExtractor,
)


def _by_category(candidates: list[NfrCandidate], category: str) -> list[NfrCandidate]:
    return [c for c in candidates if c.category == category]


def _statements(candidates: list[NfrCandidate]) -> str:
    return "\n".join(c.statement for c in candidates)


# ---------------------------------------------------------------------------
# Kubernetes mappings
# ---------------------------------------------------------------------------
def test_k8s_replicas_map_to_availability_and_scalability_high_confidence():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: api
        spec:
          replicas: 3
          template:
            spec:
              containers:
                - name: api
                  image: app:1
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "k8s/api.yaml")
    candidates = derive_iac_nfrs([info])

    availability = _by_category(candidates, CAT_AVAILABILITY)
    scalability = _by_category(candidates, CAT_SCALABILITY)
    assert availability and scalability
    avail = availability[0]
    assert avail.confidence == CONFIDENCE_HIGH
    assert avail.kind == KIND_NFR
    assert "3 replicas" in avail.statement
    # Provenance carries the file AND the specific resource.
    assert avail.source == "k8s/api.yaml::Deployment::api"
    assert avail.evidence["replicas"] == 3


def test_k8s_single_replica_is_medium_confidence_availability_gap():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: solo
        spec:
          replicas: 1
          template:
            spec:
              containers:
                - name: solo
                  image: app:1
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "solo.yaml")
    candidates = derive_iac_nfrs([info])

    availability = _by_category(candidates, CAT_AVAILABILITY)
    assert len(availability) == 1
    assert availability[0].confidence == CONFIDENCE_MEDIUM
    assert "single point of failure" in availability[0].statement
    # A single replica must NOT claim horizontal scalability.
    assert not _by_category(candidates, CAT_SCALABILITY)


def test_k8s_resources_and_probes_map_to_capacity_and_reliability():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: StatefulSet
        metadata:
          name: db
        spec:
          replicas: 2
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
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "db.yaml")
    candidates = derive_iac_nfrs([info])

    capacity = _by_category(candidates, CAT_CAPACITY)
    reliability = _by_category(candidates, CAT_RELIABILITY)
    assert capacity and reliability
    assert capacity[0].confidence == CONFIDENCE_HIGH
    assert capacity[0].source == "db.yaml::StatefulSet::db::db"
    assert "livenessProbe" in reliability[0].statement
    assert "readinessProbe" in reliability[0].statement
    assert reliability[0].confidence == CONFIDENCE_HIGH


def test_k8s_hpa_maps_to_scalability_and_availability_floor():
    manifest = textwrap.dedent(
        """\
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata:
          name: api-hpa
        spec:
          scaleTargetRef: {kind: Deployment, name: api}
          minReplicas: 2
          maxReplicas: 10
          metrics:
            - type: Resource
              resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "hpa.yaml")
    candidates = derive_iac_nfrs([info])

    scalability = _by_category(candidates, CAT_SCALABILITY)
    availability = _by_category(candidates, CAT_AVAILABILITY)
    assert scalability and availability
    assert "between 2 and 10 replicas" in scalability[0].statement
    assert scalability[0].evidence["max_replicas"] == 10
    # min >= 2 implies an availability floor.
    assert any("floor of 2 replicas" in c.statement for c in availability)


def test_k8s_network_policy_maps_to_security_isolation():
    manifest = textwrap.dedent(
        """\
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata:
          name: deny-all
        spec:
          podSelector: {}
          policyTypes: [Ingress, Egress]
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "np.yaml")
    candidates = derive_iac_nfrs([info])

    security = _by_category(candidates, CAT_SECURITY)
    assert len(security) == 1
    assert security[0].confidence == CONFIDENCE_HIGH
    assert security[0].source == "np.yaml::NetworkPolicy::deny-all"
    assert "network segmentation" in security[0].statement


def test_k8s_pdb_and_pvc_map_to_availability_and_durability():
    manifest = textwrap.dedent(
        """\
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata:
          name: api-pdb
        spec:
          minAvailable: 2
        ---
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: data
        spec:
          accessModes: [ReadWriteOnce]
          resources: {requests: {storage: 20Gi}}
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "stateful.yaml")
    candidates = derive_iac_nfrs([info])

    availability = _by_category(candidates, CAT_AVAILABILITY)
    durability = _by_category(candidates, CAT_DURABILITY)
    assert any("minAvailable=2" in c.statement for c in availability)
    assert durability and durability[0].evidence["storage"] == "20Gi"
    assert durability[0].confidence == CONFIDENCE_HIGH


def test_k8s_cronjob_and_daemonset_are_operational_topology_facts():
    manifest = textwrap.dedent(
        """\
        apiVersion: batch/v1
        kind: CronJob
        metadata:
          name: nightly
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
        metadata:
          name: agent
        spec:
          template:
            spec:
              containers:
                - name: agent
                  image: agent:1
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "batch.yaml")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    assert any("schedule '0 2 * * *'" in c.statement for c in topology)
    assert any("one pod per node" in c.statement for c in topology)
    for cand in topology:
        assert cand.kind == KIND_OPERATIONAL_FACT


def test_k8s_multiple_namespaces_yield_topology_matrix_fact():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api, namespace: prod}
        spec:
          replicas: 2
          template: {spec: {containers: [{name: api, image: a:1}]}}
        ---
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api, namespace: staging}
        spec:
          replicas: 2
          template: {spec: {containers: [{name: api, image: a:1}]}}
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "multi-ns.yaml")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    matrix = [c for c in topology if "namespaces" in c.statement]
    assert len(matrix) == 1
    assert matrix[0].confidence == CONFIDENCE_MEDIUM
    assert matrix[0].evidence["namespaces"] == ["prod", "staging"]


# ---------------------------------------------------------------------------
# Terraform mappings
# ---------------------------------------------------------------------------
def test_terraform_durability_dr_flags_high_confidence():
    tf = textwrap.dedent(
        """\
        resource "aws_db_instance" "main" {
          multi_az = true
          backup_retention_period = 7
          deletion_protection = true
          storage_encrypted = true
        }
        """
    )
    info = TerraformExtractor().extract_resources(tf, "rds.tf")
    candidates = derive_iac_nfrs([info])

    durability = _by_category(candidates, CAT_DURABILITY)
    availability = _by_category(candidates, CAT_AVAILABILITY)
    security = _by_category(candidates, CAT_SECURITY)

    assert any("retains backups for 7" in c.statement for c in durability)
    assert any("deletion protection" in c.statement for c in durability)
    assert any("multi-AZ" in c.statement for c in availability)
    assert any("encrypted at rest" in c.statement for c in security)
    for cand in durability + availability:
        assert cand.confidence == CONFIDENCE_HIGH
        assert cand.source.startswith("rds.tf::aws_db_instance::main")


def test_terraform_autoscaling_maps_to_scalability():
    tf = textwrap.dedent(
        """\
        resource "aws_autoscaling_group" "web" {
          min_size = 2
          max_size = 8
          desired_capacity = 3
        }
        """
    )
    info = TerraformExtractor().extract_resources(tf, "asg.tf")
    candidates = derive_iac_nfrs([info])

    scalability = _by_category(candidates, CAT_SCALABILITY)
    availability = _by_category(candidates, CAT_AVAILABILITY)
    assert scalability  # min_size / max_size
    assert any("autoscaling bounds" in c.statement for c in scalability)
    # desired_capacity >= 2 implies availability.
    assert any("provisions 3 instances" in c.statement for c in availability)


def test_terraform_security_group_type_infers_isolation_medium_confidence():
    tf = textwrap.dedent(
        """\
        resource "aws_security_group" "web" {
          name = "web-sg"
        }
        """
    )
    info = TerraformExtractor().extract_resources(tf, "sg.tf")
    candidates = derive_iac_nfrs([info])

    security = _by_category(candidates, CAT_SECURITY)
    assert len(security) == 1
    assert security[0].confidence == CONFIDENCE_MEDIUM
    assert security[0].source == "sg.tf::aws_security_group::web"


def test_terraform_plain_bucket_yields_no_nfr_candidates():
    tf = textwrap.dedent(
        """\
        resource "aws_s3_bucket" "assets" {
          bucket = "demo-assets"
        }
        """
    )
    info = TerraformExtractor().extract_resources(tf, "s3.tf")
    assert derive_iac_nfrs([info]) == []


# ---------------------------------------------------------------------------
# CI/CD, Dockerfile, docker-compose, ops-evidence
# ---------------------------------------------------------------------------
def test_github_actions_test_and_deploy_steps_map_to_reliability_and_topology():
    workflow = textwrap.dedent(
        """\
        name: ci
        on:
          push:
            branches: [main]
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: Run tests
                run: pytest -q
          deploy:
            runs-on: ubuntu-latest
            environment: production
            steps:
              - name: Deploy
                run: kubectl apply -f k8s.yaml
        """
    )
    info = GitHubActionsExtractor().extract_workflow(workflow, ".github/workflows/ci.yml")
    candidates = derive_iac_nfrs([info])

    reliability = _by_category(candidates, CAT_RELIABILITY)
    topology = _by_category(candidates, CAT_TOPOLOGY)
    assert any("automated test suite" in c.statement for c in reliability)
    assert any("CI/CD pipeline" in c.statement for c in topology)
    deploy_fact = next(c for c in topology if "CI/CD pipeline" in c.statement)
    assert deploy_fact.kind == KIND_OPERATIONAL_FACT
    assert deploy_fact.source == ".github/workflows/ci.yml::ci"


def test_dockerfile_multistage_and_ports_are_topology_facts():
    dockerfile = textwrap.dedent(
        """\
        FROM node:20 AS builder
        RUN npm ci && npm run build
        FROM nginx:1.27
        EXPOSE 8080
        ENTRYPOINT ["/entry.sh"]
        """
    )
    info = DockerfileExtractor().extract_dockerfile(dockerfile, "Dockerfile")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    assert any("multi-stage build" in c.statement for c in topology)
    ports_fact = next(c for c in topology if "exposes port" in c.statement)
    assert ports_fact.evidence["ports"] == ["8080"]
    assert ports_fact.confidence == CONFIDENCE_HIGH


def test_docker_compose_multi_service_and_volumes():
    compose = textwrap.dedent(
        """\
        services:
          api:
            image: app:1
            depends_on: [db]
            volumes:
              - data:/var/lib
          db:
            image: postgres:16
        """
    )
    info = DockerComposeExtractor().extract_services(compose, "docker-compose.yml")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    durability = _by_category(candidates, CAT_DURABILITY)
    assert any("multi-service runtime composition" in c.statement for c in topology)
    assert any("dependency ordering" in c.statement for c in topology)
    assert any("outside the container lifecycle" in c.statement for c in durability)


def test_prometheus_rules_evidence_maps_to_observability_slo():
    info = OpsEvidenceExtractor().build_evidence("prometheus_rules", "monitoring/app.rules.yml")
    candidates = derive_iac_nfrs([info])

    observability = _by_category(candidates, CAT_OBSERVABILITY)
    assert len(observability) == 1
    assert observability[0].confidence == CONFIDENCE_MEDIUM
    assert observability[0].kind == KIND_NFR
    assert "acceptance criteria / SLOs" in observability[0].statement
    assert observability[0].source == "monitoring/app.rules.yml"


def test_helm_and_ansible_evidence_map_to_topology_operational_facts():
    helm = OpsEvidenceExtractor().build_evidence("helm_chart", "chart/Chart.yaml")
    ansible = OpsEvidenceExtractor().build_evidence("ansible_playbook", "playbook.yml")
    candidates = derive_iac_nfrs([helm, ansible])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    assert any("Helm chart" in c.statement for c in topology)
    assert any("Ansible playbook" in c.statement for c in topology)
    for cand in topology:
        assert cand.kind == KIND_OPERATIONAL_FACT


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------
def test_every_candidate_uses_a_known_category_and_confidence():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api}
        spec:
          replicas: 3
          template:
            spec:
              containers:
                - name: api
                  image: app:1
                  resources: {limits: {cpu: "1"}}
                  livenessProbe: {httpGet: {path: /h, port: 80}}
        """
    )
    info = KubernetesExtractor().extract_manifests(manifest, "k8s.yaml")
    candidates = derive_iac_nfrs([info])

    assert candidates
    for cand in candidates:
        assert cand.category in CATEGORIES
        assert cand.confidence in {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}
        assert cand.kind in {KIND_NFR, KIND_OPERATIONAL_FACT}
        assert cand.source.startswith("k8s.yaml")
        assert cand.statement


def test_derive_accepts_mapping_view_and_is_order_stable():
    manifest = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api}
        spec:
          replicas: 2
          template: {spec: {containers: [{name: api, image: a:1}]}}
        """
    )
    info_a = KubernetesExtractor().extract_manifests(manifest, "a.yaml")
    info_b = KubernetesExtractor().extract_manifests(manifest, "b.yaml")
    mapping_view = {"b.yaml": info_b, "a.yaml": info_a}

    candidates = derive_iac_nfrs(mapping_view)
    # Deterministic: sorted by source file, so a.yaml precedes b.yaml.
    sources = [c.source for c in candidates]
    assert sources == sorted(sources)
    assert sources[0].startswith("a.yaml")


def test_empty_infra_config_yields_no_candidates():
    assert derive_iac_nfrs({}) == []
    assert derive_iac_nfrs([]) == []


def test_candidate_to_dict_roundtrips_fields():
    cand = NfrCandidate(
        category=CAT_AVAILABILITY,
        statement="x",
        source="f::r",
        confidence=CONFIDENCE_HIGH,
        evidence={"replicas": 3},
    )
    payload = cand.to_dict()
    assert payload["category"] == CAT_AVAILABILITY
    assert payload["kind"] == KIND_NFR
    assert payload["evidence"] == {"replicas": 3}

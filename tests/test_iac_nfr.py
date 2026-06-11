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
    AnsibleExtractor,
    DockerComposeExtractor,
    DockerfileExtractor,
    GitHubActionsExtractor,
    KubernetesExtractor,
    OpsEvidenceExtractor,
    PrometheusRulesExtractor,
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
# Prometheus deep-parsed mappings (H1): parsed rules upgrade recognition-medium
# to per-rule HIGH-confidence candidates.
# ---------------------------------------------------------------------------
def test_prometheus_alert_rule_maps_to_high_confidence_slo_candidate():
    rules = textwrap.dedent(
        """\
        groups:
          - name: availability
            rules:
              - alert: HighErrorRate
                expr: rate(http_errors_total[5m]) / rate(http_requests_total[5m]) > 0.01
                for: 5m
                labels:
                  severity: critical
                annotations:
                  summary: Error rate above 1%
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(rules, "monitoring/app.rules.yml")
    candidates = derive_iac_nfrs([info])

    observability = _by_category(candidates, CAT_OBSERVABILITY)
    assert len(observability) == 1
    alert = observability[0]
    # Parsed rules are HIGH-confidence NFR candidates (vs recognition MEDIUM).
    assert alert.confidence == CONFIDENCE_HIGH
    assert alert.kind == KIND_NFR
    # The statement carries name + threshold expr + duration + severity and is
    # explicitly phrased as a candidate acceptance criterion.
    assert "HighErrorRate" in alert.statement
    assert "rate(http_errors_total[5m])" in alert.statement
    assert "5m" in alert.statement
    assert "severity critical" in alert.statement
    assert "candidate SLO/acceptance criterion" in alert.statement
    # Provenance stays file::Kind::name.
    assert alert.source == "monitoring/app.rules.yml::AlertRule::HighErrorRate"
    assert alert.evidence["for"] == "5m"
    assert alert.evidence["severity"] == "critical"


def test_prometheus_recording_rule_is_observability_operational_fact():
    rules = textwrap.dedent(
        """\
        groups:
          - name: sli
            rules:
              - record: job:latency:p99
                expr: histogram_quantile(0.99, rate(latency_bucket[5m]))
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(rules, "sli.rules.yml")
    candidates = derive_iac_nfrs([info])

    observability = _by_category(candidates, CAT_OBSERVABILITY)
    assert len(observability) == 1
    record = observability[0]
    assert record.kind == KIND_OPERATIONAL_FACT
    assert record.confidence == CONFIDENCE_HIGH
    assert "job:latency:p99" in record.statement
    assert record.source == "sli.rules.yml::RecordingRule::job:latency:p99"


def test_prometheus_scrape_jobs_map_to_medium_observability_facts():
    scrape = textwrap.dedent(
        """\
        scrape_configs:
          - job_name: api
            static_configs:
              - targets: ['api:8000', 'api2:8000']
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(scrape, "prometheus.yml")
    candidates = derive_iac_nfrs([info])

    observability = _by_category(candidates, CAT_OBSERVABILITY)
    assert len(observability) == 1
    job = observability[0]
    assert job.confidence == CONFIDENCE_MEDIUM
    assert job.kind == KIND_OPERATIONAL_FACT
    assert "job 'api'" in job.statement
    assert job.source == "prometheus.yml::ScrapeJob::api"
    assert job.evidence["targets_count"] == 2


def test_alertmanager_receivers_map_to_observability_routing_fact():
    am = textwrap.dedent(
        """\
        route:
          receiver: ops-team
          routes:
            - match: {severity: critical}
              receiver: pagerduty
        receivers:
          - name: ops-team
          - name: pagerduty
        """
    )
    info = PrometheusRulesExtractor().extract_prometheus(am, "alertmanager.yml")
    candidates = derive_iac_nfrs([info])

    observability = _by_category(candidates, CAT_OBSERVABILITY)
    assert len(observability) == 1
    routing = observability[0]
    assert routing.confidence == CONFIDENCE_MEDIUM
    assert routing.kind == KIND_OPERATIONAL_FACT
    assert "ops-team" in routing.statement and "pagerduty" in routing.statement
    assert routing.evidence["receivers"] == ["ops-team", "pagerduty"]
    assert routing.evidence["default_receiver"] == "ops-team"


# ---------------------------------------------------------------------------
# Ansible deep-parsed mappings (H1)
# ---------------------------------------------------------------------------
_ANSIBLE_PLAYBOOK_FOR_NFR = textwrap.dedent(
    """\
    - name: Configure web servers
      hosts: webservers
      become: true
      roles: [common]
      tasks:
        - name: Install nginx
          apt:
            name: nginx
            state: present
        - name: Ensure nginx running
          service:
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
        - name: Create deploy user
          user:
            name: deploy
    """
)


def test_ansible_service_task_maps_to_high_reliability_operational_fact():
    info = AnsibleExtractor().extract_ansible(_ANSIBLE_PLAYBOOK_FOR_NFR, "playbook.yml")
    candidates = derive_iac_nfrs([info])

    reliability = _by_category(candidates, CAT_RELIABILITY)
    assert len(reliability) == 1
    svc = reliability[0]
    assert svc.confidence == CONFIDENCE_HIGH
    assert svc.kind == KIND_OPERATIONAL_FACT
    assert "service 'nginx'" in svc.statement
    assert "state=started" in svc.statement
    assert "system service" in svc.statement
    assert svc.source == "playbook.yml::task::Ensure nginx running"


def test_ansible_firewall_task_maps_to_high_security_nfr():
    info = AnsibleExtractor().extract_ansible(_ANSIBLE_PLAYBOOK_FOR_NFR, "playbook.yml")
    candidates = derive_iac_nfrs([info])

    security = _by_category(candidates, CAT_SECURITY)
    firewall = [c for c in security if "firewall" in c.statement]
    assert len(firewall) == 1
    assert firewall[0].confidence == CONFIDENCE_HIGH
    # Restricting network access is prescriptive — an NFR, not just a fact.
    assert firewall[0].kind == KIND_NFR
    assert firewall[0].source == "playbook.yml::task::Allow http"

    # User management is security/isolation evidence at MEDIUM.
    accounts = [c for c in security if "user 'deploy'" in c.statement]
    assert len(accounts) == 1
    assert accounts[0].confidence == CONFIDENCE_MEDIUM
    assert accounts[0].kind == KIND_OPERATIONAL_FACT


def test_ansible_package_cron_and_play_map_to_deployment_topology():
    info = AnsibleExtractor().extract_ansible(_ANSIBLE_PLAYBOOK_FOR_NFR, "playbook.yml")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    # Package install -> MEDIUM topology fact.
    package = next(c for c in topology if "package 'nginx'" in c.statement)
    assert package.confidence == CONFIDENCE_MEDIUM
    assert package.kind == KIND_OPERATIONAL_FACT
    # Cron task -> HIGH topology fact carrying the schedule.
    cron = next(c for c in topology if "cron job 'cleanup'" in c.statement)
    assert cron.confidence == CONFIDENCE_HIGH
    assert "hour=2" in cron.statement
    # The play itself -> HIGH topology fact with hosts + roles.
    play = next(c for c in topology if "play 'Configure web servers'" in c.statement)
    assert play.confidence == CONFIDENCE_HIGH
    assert "webservers" in play.statement
    assert play.source == "playbook.yml::play::Configure web servers"
    assert play.evidence["roles"] == ["common"]


def test_ansible_role_maps_to_topology_fact_with_role_provenance():
    role_tasks = textwrap.dedent(
        """\
        - name: Start postgres
          service:
            name: postgresql
            state: started
        """
    )
    info = AnsibleExtractor().extract_ansible(role_tasks, "roles/database/tasks/main.yml")
    candidates = derive_iac_nfrs([info])

    topology = _by_category(candidates, CAT_TOPOLOGY)
    role = next(c for c in topology if "role 'database'" in c.statement)
    assert role.kind == KIND_OPERATIONAL_FACT
    assert role.source == "roles/database/tasks/main.yml::role::database"
    # The role's tasks still map individually (service -> reliability).
    reliability = _by_category(candidates, CAT_RELIABILITY)
    assert any("postgresql" in c.statement for c in reliability)


def test_ansible_unmapped_modules_yield_no_candidates():
    playbook = textwrap.dedent(
        """\
        - hosts: all
          tasks:
            - name: Copy file
              copy:
                src: a
                dest: b
        """
    )
    info = AnsibleExtractor().extract_ansible(playbook, "playbook.yml")
    candidates = derive_iac_nfrs([info])

    # Only the play-level topology fact; the copy task maps to nothing.
    assert len(candidates) == 1
    assert candidates[0].category == CAT_TOPOLOGY


# ---------------------------------------------------------------------------
# Terraform deep-attribute mapping: hcl2 mode vs regex fallback (H1)
# ---------------------------------------------------------------------------
def test_terraform_hcl2_nested_encryption_flag_yields_security_candidate(monkeypatch):
    """The hcl2 path derives NFRs from nested blocks; the fallback cannot."""

    import codd.parsing as codd_parsing

    tf = textwrap.dedent(
        """\
        resource "aws_instance" "worker" {
          ami = "ami-123"

          ebs_block_device {
            device_name = "/dev/sdf"
            encrypted   = true
          }
        }
        """
    )
    extractor = TerraformExtractor()

    assert codd_parsing.hcl2 is not None
    deep = derive_iac_nfrs([extractor.extract_resources(tf, "ec2.tf")])
    security = _by_category(deep, CAT_SECURITY)
    assert any("encrypted at rest" in c.statement for c in security)

    monkeypatch.setattr(codd_parsing, "hcl2", None)
    shallow = derive_iac_nfrs([extractor.extract_resources(tf, "ec2.tf")])
    assert not _by_category(shallow, CAT_SECURITY)


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

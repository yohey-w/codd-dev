"""IaC and ops-evidence extraction (Compose, K8s, Terraform, CI, Ansible, ...)."""

from __future__ import annotations

import io
import os
import re
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import yaml

# ``hcl2`` is owned by the package namespace (``codd.parsing.hcl2``):
# TerraformExtractor reads it through ``_parsing.hcl2`` at call time so that
# callers and tests can monkeypatch ``codd.parsing.hcl2`` (e.g. set it to
# ``None`` to force the regex fallback) exactly as they could when
# ``codd.parsing`` was a single module.
from codd import parsing as _parsing
from codd.parsing._shared import (
    ConfigInfo,
    _IGNORED_DIR_NAMES,
    _iter_project_files,
    _load_structured_document,
    _load_yaml_documents,
    _normalize_environment,
    _normalize_list,
)


class DockerComposeExtractor:
    """Extract docker-compose style service definitions."""

    format = "docker-compose"
    file_names = {
        "compose.yaml",
        "compose.yml",
        "docker-compose.override.yaml",
        "docker-compose.override.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }

    def detect_docker_compose(self, project_root: Path) -> list[Path]:
        return [
            file_path
            for file_path in _iter_project_files(project_root, {".yaml", ".yml"})
            if file_path.name in self.file_names
        ]

    def extract_services(self, content: str, file_path: str) -> ConfigInfo:
        payload = _load_structured_document(content, file_path)
        info = ConfigInfo(format=self.format, file_path=file_path)
        if not isinstance(payload, dict):
            return info

        services = payload.get("services") or {}
        if not isinstance(services, dict):
            return info

        for name, config in services.items():
            if not isinstance(config, dict):
                continue
            depends_on = config.get("depends_on", [])
            if isinstance(depends_on, dict):
                depends_on = list(depends_on.keys())
            info.services.append(
                {
                    "name": str(name),
                    "image": str(config.get("image", "")),
                    "ports": _normalize_list(config.get("ports")),
                    "depends_on": _normalize_list(depends_on),
                    "volumes": _normalize_list(config.get("volumes")),
                    "environment": _normalize_environment(config.get("environment")),
                }
            )

        return info

class KubernetesExtractor:
    """Extract Kubernetes resources from YAML manifests.

    The set of parsed kinds is intentionally broad so that downstream IaC→NFR
    mapping (:mod:`codd.iac_nfr`) can recover availability, scalability,
    capacity, reliability/health, security/isolation and durability/DR facts
    deterministically. ``ConfigInfo.resources`` entries always carry ``kind`` +
    ``name``; kind-specific fields are additive.
    """

    format = "kubernetes"
    # Workload kinds whose pod template carries containers / probes / resources.
    _WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
    supported_kinds = {
        "ConfigMap",
        "CronJob",
        "DaemonSet",
        "Deployment",
        "HorizontalPodAutoscaler",
        "Ingress",
        "Job",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "PodDisruptionBudget",
        "Service",
        "StatefulSet",
    }

    def detect_k8s_manifests(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, {".yaml", ".yml"}):
            docs = _load_yaml_documents(file_path)
            if any(
                isinstance(doc, dict) and doc.get("kind") in self.supported_kinds
                for doc in docs
            ):
                matches.append(file_path)
        return matches

    def extract_manifests(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            return info

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind not in self.supported_kinds:
                continue

            metadata = doc.get("metadata") or {}
            namespace = metadata.get("namespace")
            resource: dict[str, Any] = {
                "kind": kind,
                "name": metadata.get("name", ""),
            }
            if namespace:
                resource["namespace"] = namespace

            if kind in self._WORKLOAD_KINDS:
                self._populate_workload(kind, doc, resource)
            elif kind == "Service":
                spec = doc.get("spec") or {}
                resource["service_type"] = spec.get("type", "ClusterIP")
                resource["selector"] = spec.get("selector") or {}
                resource["ports"] = [
                    {
                        "port": port.get("port"),
                        "targetPort": port.get("targetPort"),
                    }
                    for port in spec.get("ports", []) or []
                    if isinstance(port, dict)
                ]
            elif kind == "Ingress":
                spec = doc.get("spec") or {}
                resource["rules"] = [
                    {
                        "host": rule.get("host"),
                        "paths": [
                            {
                                "path": path_cfg.get("path"),
                                "service": ((path_cfg.get("backend") or {}).get("service") or {}).get("name"),
                            }
                            for path_cfg in ((rule.get("http") or {}).get("paths") or [])
                            if isinstance(path_cfg, dict)
                        ],
                    }
                    for rule in spec.get("rules", []) or []
                    if isinstance(rule, dict)
                ]
            elif kind == "ConfigMap":
                data = doc.get("data") or {}
                resource["data_keys"] = sorted(str(key) for key in data.keys())
            elif kind == "HorizontalPodAutoscaler":
                self._populate_hpa(doc, resource)
            elif kind == "NetworkPolicy":
                self._populate_network_policy(doc, resource)
            elif kind == "PodDisruptionBudget":
                spec = doc.get("spec") or {}
                if "minAvailable" in spec:
                    resource["min_available"] = spec.get("minAvailable")
                if "maxUnavailable" in spec:
                    resource["max_unavailable"] = spec.get("maxUnavailable")
            elif kind == "PersistentVolumeClaim":
                spec = doc.get("spec") or {}
                resource["access_modes"] = _normalize_list(spec.get("accessModes"))
                resource["storage_class"] = spec.get("storageClassName")
                requests = ((spec.get("resources") or {}).get("requests") or {})
                if isinstance(requests, dict):
                    resource["storage"] = requests.get("storage")

            info.resources.append(resource)

        return info

    def _populate_workload(self, kind: str, doc: dict[str, Any], resource: dict[str, Any]) -> None:
        spec = doc.get("spec") or {}
        # CronJob nests the workload spec under spec.jobTemplate.spec; Job/others
        # carry it directly under spec.
        if kind == "CronJob":
            resource["schedule"] = spec.get("schedule")
            job_spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
        else:
            job_spec = spec

        pod_spec = ((job_spec.get("template") or {}).get("spec") or {})

        if kind in {"Deployment", "StatefulSet"}:
            resource["replicas"] = spec.get("replicas", 1)
        if kind == "StatefulSet":
            resource["service_name"] = spec.get("serviceName")
        if kind == "Job":
            if "completions" in job_spec:
                resource["completions"] = job_spec.get("completions")
            if "parallelism" in job_spec:
                resource["parallelism"] = job_spec.get("parallelism")

        resource["containers"] = [
            self._parse_container(container)
            for container in pod_spec.get("containers", []) or []
            if isinstance(container, dict)
        ]

    @staticmethod
    def _parse_container(container: dict[str, Any]) -> dict[str, Any]:
        parsed: dict[str, Any] = {
            "name": container.get("name", ""),
            "image": container.get("image", ""),
            "ports": [
                port.get("containerPort")
                for port in container.get("ports", []) or []
                if isinstance(port, dict) and "containerPort" in port
            ],
        }
        resources = container.get("resources")
        if isinstance(resources, dict):
            requests = resources.get("requests")
            limits = resources.get("limits")
            captured: dict[str, Any] = {}
            if isinstance(requests, dict) and requests:
                captured["requests"] = {str(k): v for k, v in requests.items()}
            if isinstance(limits, dict) and limits:
                captured["limits"] = {str(k): v for k, v in limits.items()}
            if captured:
                parsed["resources"] = captured
        probes: dict[str, bool] = {}
        for probe_key in ("livenessProbe", "readinessProbe", "startupProbe"):
            if isinstance(container.get(probe_key), dict):
                probes[probe_key] = True
        if probes:
            parsed["probes"] = probes
        return parsed

    @staticmethod
    def _populate_hpa(doc: dict[str, Any], resource: dict[str, Any]) -> None:
        spec = doc.get("spec") or {}
        resource["min_replicas"] = spec.get("minReplicas")
        resource["max_replicas"] = spec.get("maxReplicas")
        target = spec.get("scaleTargetRef") or {}
        if isinstance(target, dict) and target:
            resource["scale_target"] = {
                "kind": target.get("kind"),
                "name": target.get("name"),
            }
        metrics: list[dict[str, Any]] = []
        # autoscaling/v2 metrics list. Each entry has a `type` (Resource | Pods |
        # Object | External) and a same-name-lowercased sub-block carrying the
        # metric identity, e.g. {type: Resource, resource: {name: cpu}}.
        for metric in spec.get("metrics", []) or []:
            if not isinstance(metric, dict):
                continue
            metric_type = metric.get("type")
            name = None
            if isinstance(metric_type, str) and metric_type:
                block_key = metric_type[0].lower() + metric_type[1:]
                block = metric.get(block_key)
                if isinstance(block, dict):
                    name = block.get("name")
                    nested_metric = block.get("metric")
                    if name is None and isinstance(nested_metric, dict):
                        name = nested_metric.get("name")
            metrics.append({"type": metric_type, "name": name})
        # autoscaling/v1 single CPU target.
        if "targetCPUUtilizationPercentage" in spec:
            metrics.append(
                {"type": "Resource", "name": "cpu", "target": spec.get("targetCPUUtilizationPercentage")}
            )
        if metrics:
            resource["metrics"] = metrics

    @staticmethod
    def _populate_network_policy(doc: dict[str, Any], resource: dict[str, Any]) -> None:
        spec = doc.get("spec") or {}
        policy_types = _normalize_list(spec.get("policyTypes"))
        ingress = spec.get("ingress")
        egress = spec.get("egress")
        # Infer policy types from declared rule blocks when policyTypes is absent.
        if not policy_types:
            if isinstance(ingress, list):
                policy_types.append("Ingress")
            if isinstance(egress, list):
                policy_types.append("Egress")
        resource["policy_types"] = policy_types
        resource["ingress_rules"] = len(ingress) if isinstance(ingress, list) else 0
        resource["egress_rules"] = len(egress) if isinstance(egress, list) else 0
        pod_selector = spec.get("podSelector")
        if isinstance(pod_selector, dict):
            resource["pod_selector"] = pod_selector

class TerraformExtractor:
    """Extract Terraform resources via python-hcl2 or a regex fallback."""

    format = "terraform"
    _RESOURCE_BLOCK_RE = re.compile(
        r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{',
        re.MULTILINE,
    )
    _NAMED_BLOCK_RE = re.compile(
        r'^\s*(module|variable)\s+"([^"]+)"\s*\{',
        re.MULTILINE,
    )

    @classmethod
    def is_available(cls) -> bool:
        return _parsing.hcl2 is not None or find_spec("hcl2") is not None

    def detect_tf_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".tf"}))

    def extract_resources(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        if _parsing.hcl2 is None:
            return self._extract_resources_regex(content, file_path)

        try:
            parsed = _parsing.hcl2.load(io.StringIO(content))
        except Exception:
            return self._extract_resources_regex(content, file_path)

        for block in parsed.get("resource", []) or []:
            if not isinstance(block, dict):
                continue
            for resource_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    attributes = self._normalize_hcl2(attributes or {})
                    entry: dict[str, Any] = {
                        "kind": "resource",
                        "type": resource_type.strip('"'),
                        "name": name.strip('"'),
                        "attributes": attributes,
                    }
                    flags = self._nfr_flags(attributes)
                    if flags:
                        entry["nfr_flags"] = flags
                    info.resources.append(entry)

        for block in parsed.get("data", []) or []:
            if not isinstance(block, dict):
                continue
            for data_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    info.resources.append(
                        {
                            "kind": "data",
                            "type": data_type.strip('"'),
                            "name": name.strip('"'),
                            "attributes": self._normalize_hcl2(attributes or {}),
                        }
                    )

        for block in parsed.get("module", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "module",
                        "name": name.strip('"'),
                        "attributes": self._normalize_hcl2(attributes or {}),
                    }
                )

        for block in parsed.get("variable", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "variable",
                        "name": name.strip('"'),
                        "attributes": self._normalize_hcl2(attributes or {}),
                    }
                )

        return info

    @classmethod
    def _normalize_hcl2(cls, value: Any) -> Any:
        """Normalize the python-hcl2 attribute tree.

        python-hcl2 >= 5 preserves the surrounding double quotes on string
        literals (round-trip support) and >= 8 injects ``__…__`` metadata keys
        such as ``__is_block__``. Downstream consumers (NFR mapping, restore
        prompts) want plain values, so strip both — recursively, since blocks
        nest arbitrarily.
        """

        if isinstance(value, dict):
            return {
                key: cls._normalize_hcl2(item)
                for key, item in value.items()
                if not (
                    isinstance(key, str) and key.startswith("__") and key.endswith("__")
                )
            }
        if isinstance(value, list):
            return [cls._normalize_hcl2(item) for item in value]
        if (
            isinstance(value, str)
            and len(value) >= 2
            and value.startswith('"')
            and value.endswith('"')
        ):
            return value[1:-1]
        return value

    # NFR-relevant attribute keys. We do not build a full HCL semantic engine;
    # we surface the attributes (already captured) that prescribe a
    # non-functional requirement so downstream mapping can reason about them.
    # Substring match (lowercased) keeps this provider-neutral: e.g.
    # ``backup_retention_period`` (RDS) and ``backup_retention_days`` both match.
    _NFR_FLAG_KEYS: tuple[str, ...] = (
        "replicas",
        "min_size",
        "max_size",
        "desired_capacity",
        "min_capacity",
        "max_capacity",
        "multi_az",
        "availability_zone",
        "backup_retention",
        "deletion_protection",
        "autoscaling",
        "skip_final_snapshot",
        "storage_encrypted",
        "encrypted",
    )

    @classmethod
    def _nfr_flags(cls, attributes: Any) -> dict[str, Any]:
        """Surface NFR-relevant attributes from a Terraform resource block.

        Returns a flat mapping of ``{matched_key: value}`` for any attribute
        whose (lowercased) key contains one of the NFR-relevant tokens. Nested
        blocks (lists/dicts) are walked one level so attributes inside
        ``scaling_config``-style sub-blocks are still surfaced.
        """

        if not isinstance(attributes, dict):
            return {}
        flags: dict[str, Any] = {}

        def _consider(key: str, value: Any) -> None:
            lowered = key.lower()
            if any(token in lowered for token in cls._NFR_FLAG_KEYS):
                # Last write wins; nested keys are namespaced to avoid clobber.
                flags.setdefault(key, value)

        for key, value in attributes.items():
            if not isinstance(key, str):
                continue
            _consider(key, value)
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_key, str):
                        _consider(sub_key, sub_value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for sub_key, sub_value in item.items():
                            if isinstance(sub_key, str):
                                _consider(sub_key, sub_value)
        return flags

    # Simple ``key = value`` assignment (no nested blocks) for the regex fallback.
    _ASSIGN_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$', re.MULTILINE)

    def _extract_resources_regex(self, content: str, file_path: str) -> ConfigInfo:
        """Fallback parser for simple Terraform blocks when python-hcl2 is unavailable.

        Without python-hcl2 we cannot build a full attribute tree, but we still
        surface NFR-relevant top-level ``key = value`` assignments per block so
        the IaC→NFR layer keeps working (durability/DR, scalability flags) even
        in the dependency-free path.
        """
        info = ConfigInfo(format=self.format, file_path=file_path)

        for match in self._RESOURCE_BLOCK_RE.finditer(content):
            kind, block_type, name = match.group(1), match.group(2), match.group(3)
            block_body = self._slice_block_body(content, match.end())
            attributes = self._scalar_assignments(block_body)
            entry: dict[str, Any] = {
                "kind": kind,
                "type": block_type,
                "name": name,
                "attributes": attributes,
            }
            flags = self._nfr_flags(attributes)
            if flags:
                entry["nfr_flags"] = flags
            info.resources.append(entry)

        for kind, name in self._NAMED_BLOCK_RE.findall(content):
            info.resources.append(
                {
                    "kind": kind,
                    "name": name,
                    "attributes": {},
                }
            )

        return info

    @staticmethod
    def _slice_block_body(content: str, start: int) -> str:
        """Return the text of a brace-delimited block beginning just after its ``{``."""

        depth = 1
        for index in range(start, len(content)):
            char = content[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return content[start:index]
        return content[start:]

    @classmethod
    def _scalar_assignments(cls, block_body: str) -> dict[str, Any]:
        """Extract top-level scalar ``key = value`` pairs from a block body."""

        attributes: dict[str, Any] = {}
        depth = 0
        for raw_line in block_body.splitlines():
            line = raw_line.strip()
            # Track nesting so we only read this block's own top-level scalars.
            opens = line.count("{") + line.count("[")
            closes = line.count("}") + line.count("]")
            if depth == 0 and opens == closes:
                match = cls._ASSIGN_RE.match(raw_line)
                if match:
                    key, value = match.group(1), match.group(2)
                    attributes.setdefault(key, cls._coerce_scalar(value))
            depth += opens - closes
            if depth < 0:
                depth = 0
        return attributes

    @staticmethod
    def _coerce_scalar(value: str) -> Any:
        token = value.split("#", 1)[0].strip().rstrip(",").strip()
        if (token.startswith('"') and token.endswith('"')) or (
            token.startswith("'") and token.endswith("'")
        ):
            return token[1:-1]
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            pass
        return token

class GitHubActionsExtractor:
    """Extract CI/CD facts from GitHub Actions workflow files.

    Tolerant of arbitrary YAML: a malformed or non-mapping workflow yields an
    empty :class:`ConfigInfo` rather than raising. Each workflow becomes one
    entry in ``ConfigInfo.pipelines`` carrying its triggers, jobs, classified
    steps (build/test/deploy), and any secret/env references — the raw material
    for recovering deployment topology and CI gates as operational facts.
    """

    format = "github-actions"
    _WORKFLOW_DIR = Path(".github") / "workflows"
    _BUILD_TOKENS = ("build", "compile", "package", "bundle", "docker build")
    _TEST_TOKENS = ("test", "pytest", "jest", "vitest", "lint", "check", "coverage")
    _DEPLOY_TOKENS = (
        "deploy",
        "release",
        "publish",
        "rollout",
        "terraform apply",
        "kubectl apply",
        "helm upgrade",
        "push",
    )
    _SECRET_RE = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")

    def detect_workflow_files(self, project_root: Path) -> list[Path]:
        workflow_dir = Path(project_root) / self._WORKFLOW_DIR
        if not workflow_dir.is_dir():
            return []
        matches: list[Path] = []
        for file_path in sorted(workflow_dir.iterdir()):
            if file_path.is_file() and file_path.suffix.lower() in {".yml", ".yaml"}:
                matches.append(file_path)
        return matches

    def extract_workflow(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        payload = _load_structured_document(content, file_path)
        if not isinstance(payload, dict):
            return info

        pipeline: dict[str, Any] = {
            "name": str(payload.get("name") or Path(file_path).stem),
            "triggers": self._extract_triggers(payload),
            "jobs": [],
            "secrets": sorted(set(self._SECRET_RE.findall(content))),
        }

        jobs = payload.get("jobs")
        if isinstance(jobs, dict):
            for job_name, job_cfg in jobs.items():
                if not isinstance(job_cfg, dict):
                    continue
                pipeline["jobs"].append(self._extract_job(str(job_name), job_cfg))

        info.pipelines.append(pipeline)
        return info

    def _extract_triggers(self, payload: dict[str, Any]) -> list[str]:
        # PyYAML parses the bareword ``on:`` key as the boolean True.
        on = payload.get("on", payload.get(True))
        if isinstance(on, str):
            return [on]
        if isinstance(on, list):
            return [str(item) for item in on]
        if isinstance(on, dict):
            return [str(key) for key in on.keys()]
        return []

    def _extract_job(self, job_name: str, job_cfg: dict[str, Any]) -> dict[str, Any]:
        steps_out: list[dict[str, Any]] = []
        env_keys: set[str] = set()

        job_env = job_cfg.get("env")
        if isinstance(job_env, dict):
            env_keys.update(str(k) for k in job_env.keys())

        for step in job_cfg.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            label = str(step.get("name") or step.get("uses") or step.get("run") or "")
            run = str(step.get("run") or "")
            uses = str(step.get("uses") or "")
            haystack = f"{label}\n{run}\n{uses}".lower()
            step_env = step.get("env")
            if isinstance(step_env, dict):
                env_keys.update(str(k) for k in step_env.keys())
            steps_out.append(
                {
                    "name": label,
                    "uses": uses or None,
                    "kinds": self._classify_step(haystack),
                }
            )

        return {
            "name": job_name,
            "runs_on": job_cfg.get("runs-on"),
            "environment": self._job_environment(job_cfg),
            "steps": steps_out,
            "env": sorted(env_keys),
        }

    @staticmethod
    def _job_environment(job_cfg: dict[str, Any]) -> Any:
        environment = job_cfg.get("environment")
        if isinstance(environment, dict):
            return environment.get("name")
        return environment

    @classmethod
    def _classify_step(cls, haystack: str) -> list[str]:
        kinds: list[str] = []
        if any(token in haystack for token in cls._DEPLOY_TOKENS):
            kinds.append("deploy")
        if any(token in haystack for token in cls._BUILD_TOKENS):
            kinds.append("build")
        if any(token in haystack for token in cls._TEST_TOKENS):
            kinds.append("test")
        return kinds

class DockerfileExtractor:
    """Light parser for Dockerfile base images, ports, entrypoint, and stages."""

    format = "dockerfile"
    _FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
    _EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.IGNORECASE)
    _ENTRYPOINT_RE = re.compile(r"^\s*ENTRYPOINT\s+(.+)$", re.IGNORECASE)
    _CMD_RE = re.compile(r"^\s*CMD\s+(.+)$", re.IGNORECASE)

    def detect_dockerfiles(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for root, dirs, files in os.walk(project_root):
            dirs[:] = [
                directory
                for directory in dirs
                if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
            ]
            for filename in files:
                if filename == "Dockerfile" or filename.startswith("Dockerfile."):
                    matches.append(Path(root) / filename)
        return matches

    def extract_dockerfile(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        image: dict[str, Any] = {
            "base_images": [],
            "stages": [],
            "ports": [],
            "entrypoint": None,
            "cmd": None,
        }

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            from_match = self._FROM_RE.match(line)
            if from_match:
                base, stage = from_match.group(1), from_match.group(2)
                image["base_images"].append(base)
                if stage:
                    image["stages"].append(stage)
                continue
            expose_match = self._EXPOSE_RE.match(line)
            if expose_match:
                for token in expose_match.group(1).split():
                    port = token.split("/", 1)[0]
                    if port:
                        image["ports"].append(port)
                continue
            entry_match = self._ENTRYPOINT_RE.match(line)
            if entry_match:
                image["entrypoint"] = entry_match.group(1).strip()
                continue
            cmd_match = self._CMD_RE.match(line)
            if cmd_match:
                image["cmd"] = cmd_match.group(1).strip()

        if any(image[key] for key in ("base_images", "ports", "entrypoint", "cmd")):
            info.images.append(image)
        return info

class OpsEvidenceExtractor:
    """Recognition-only discovery of ops/observability/config-management files.

    This is the FALLBACK layer: Ansible and Prometheus/Alertmanager files are
    deep-parsed first (:class:`AnsibleExtractor`,
    :class:`PrometheusRulesExtractor`); any such file that yields no structured
    facts — plus kinds with no deep parser (Helm Chart.yaml, role
    defaults/vars) — still surfaces its PRESENCE as evidence (observability/SLO
    and deployment-topology sources) so the IaC→NFR layer records it with
    MEDIUM confidence rather than dropping it.
    """

    format = "ops-evidence"

    # (recognized_kind, predicate) — predicate takes a lowercased file name and
    # POSIX relative path. Ordered; first match wins.
    @staticmethod
    def _classify(name: str, rel_posix: str) -> str | None:
        lname = name.lower()
        lpath = rel_posix.lower()
        if lname.startswith("alertmanager"):
            return "alertmanager_config"
        if lname.endswith(".rules.yml") or lname.endswith(".rules.yaml"):
            return "prometheus_rules"
        if lname.startswith("prometheus") and lname.endswith((".yml", ".yaml")):
            return "prometheus_config"
        if lname == "chart.yaml":
            return "helm_chart"
        if lname.startswith("playbook") and lname.endswith((".yml", ".yaml")):
            return "ansible_playbook"
        if "/roles/" in f"/{lpath}" or lpath.startswith("roles/"):
            # Ansible role task/handler files live under roles/<name>/tasks etc.
            if lname in {"main.yml", "main.yaml"}:
                return "ansible_role"
        return None

    def detect_ops_files(self, project_root: Path) -> list[tuple[Path, str]]:
        root = Path(project_root)
        matches: list[tuple[Path, str]] = []
        for file_path in _iter_project_files(root, {".yaml", ".yml"}):
            rel = file_path.relative_to(root).as_posix()
            recognized = self._classify(file_path.name, rel)
            if recognized:
                matches.append((file_path, recognized))
        return matches

    def build_evidence(self, recognized_kind: str, file_path: str) -> ConfigInfo:
        return ConfigInfo(
            format=self.format,
            file_path=file_path,
            recognized_kind=recognized_kind,
        )

class AnsibleExtractor:
    """Deep-parse Ansible playbooks and roles into structured facts.

    Upgrades the previous recognition-only treatment: plays become
    ``ConfigInfo.services`` entries (kind ``"play"``: name, hosts, become,
    roles, task count) and every task/handler becomes a ``ConfigInfo.resources``
    entry (kind ``"task"``/``"handler"``: name, module, cheap scalar args,
    parent play/role). Role task files (``roles/<name>/tasks|handlers/main.yml``)
    are parsed the same way with the role name captured.

    Tolerant by construction: malformed YAML, non-list payloads, or weird task
    shapes yield an empty/partial :class:`ConfigInfo` — never an exception. A
    file that produces nothing falls back to the recognition-only path
    (:class:`OpsEvidenceExtractor`), preserving the old behavior.
    """

    format = "ansible"

    _PLAYBOOK_NAMES = {"site.yml", "site.yaml"}
    # Task keys that are Ansible directives, not modules. The module is the
    # first key NOT in this set (dict order is preserved by PyYAML).
    _RESERVED_TASK_KEYS = {
        "any_errors_fatal",
        "args",
        "async",
        "become",
        "become_method",
        "become_user",
        "changed_when",
        "check_mode",
        "collections",
        "connection",
        "delay",
        "delegate_facts",
        "delegate_to",
        "diff",
        "environment",
        "failed_when",
        "ignore_errors",
        "ignore_unreachable",
        "listen",
        "local_action",
        "loop",
        "loop_control",
        "module_defaults",
        "name",
        "no_log",
        "notify",
        "poll",
        "register",
        "retries",
        "run_once",
        "tags",
        "throttle",
        "timeout",
        "until",
        "vars",
        "when",
    }
    _BLOCK_KEYS = ("block", "rescue", "always")

    def detect_ansible_files(self, project_root: Path) -> list[Path]:
        root = Path(project_root)
        matches: list[Path] = []
        for file_path in _iter_project_files(root, {".yaml", ".yml"}):
            rel = file_path.relative_to(root).as_posix()
            lname = file_path.name.lower()
            if lname.startswith("playbook") or lname in self._PLAYBOOK_NAMES:
                matches.append(file_path)
                continue
            if self._role_context(rel) is not None:
                matches.append(file_path)
                continue
            # Content sniff: a YAML whose top level is a list of plays
            # (dicts carrying ``hosts:``) is a playbook regardless of name.
            try:
                payload = yaml.safe_load(
                    file_path.read_text(encoding="utf-8", errors="ignore")
                )
            except Exception:
                continue
            if self._looks_like_playbook(payload):
                matches.append(file_path)
        return matches

    @staticmethod
    def _looks_like_playbook(payload: Any) -> bool:
        return isinstance(payload, list) and any(
            isinstance(item, dict) and "hosts" in item for item in payload
        )

    @staticmethod
    def _role_context(rel_posix: str) -> tuple[str, str] | None:
        """Return ``(role_name, section)`` for ``roles/<n>/tasks|handlers/main.yml``."""

        parts = rel_posix.lower().split("/")
        if len(parts) < 4 or parts[-1] not in {"main.yml", "main.yaml"}:
            return None
        section = parts[-2]
        if section not in {"tasks", "handlers"}:
            return None
        if parts[-4] != "roles":
            return None
        # Preserve the original (non-lowercased) role name.
        return rel_posix.split("/")[-3], section

    def extract_ansible(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        try:
            payload = yaml.safe_load(content)
        except Exception:
            return info

        role = self._role_context(Path(file_path).as_posix())
        if role is not None:
            role_name, section = role
            if isinstance(payload, list):
                tasks = self._parse_tasks(
                    payload, parent=role_name, handler=(section == "handlers")
                )
                if tasks:
                    info.resources.append(
                        {"kind": "role", "name": role_name, "task_count": len(tasks)}
                    )
                    info.resources.extend(tasks)
            return info

        if not isinstance(payload, list):
            return info

        for play in payload:
            if not isinstance(play, dict) or "hosts" not in play:
                continue
            self._parse_play(play, info)
        return info

    def _parse_play(self, play: dict[str, Any], info: ConfigInfo) -> None:
        hosts = play.get("hosts")
        name = str(play.get("name") or hosts or "")
        roles: list[str] = []
        for role in play.get("roles") or []:
            if isinstance(role, str):
                roles.append(role)
            elif isinstance(role, dict):
                label = role.get("role") or role.get("name")
                if label:
                    roles.append(str(label))

        tasks: list[dict[str, Any]] = []
        for section, is_handler in (
            ("pre_tasks", False),
            ("tasks", False),
            ("post_tasks", False),
            ("handlers", True),
        ):
            tasks.extend(
                self._parse_tasks(play.get(section), parent=name, handler=is_handler)
            )

        info.services.append(
            {
                "kind": "play",
                "name": name,
                "hosts": str(hosts) if hosts is not None else "",
                "become": bool(play.get("become")),
                "roles": roles,
                "task_count": len(tasks),
            }
        )
        info.resources.extend(tasks)

    def _parse_tasks(
        self, tasks: Any, parent: str, handler: bool
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(tasks, list):
            return out
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if any(key in task for key in self._BLOCK_KEYS):
                for key in self._BLOCK_KEYS:
                    out.extend(self._parse_tasks(task.get(key), parent, handler))
                continue
            module_raw = next(
                (
                    key
                    for key in task
                    if isinstance(key, str) and key not in self._RESERVED_TASK_KEYS
                ),
                None,
            )
            if module_raw is None:
                continue
            # Normalize FQCN (``ansible.builtin.service`` -> ``service``).
            module = module_raw.rsplit(".", 1)[-1]
            args = task.get(module_raw)
            attributes: dict[str, Any] = {}
            if isinstance(args, dict):
                attributes = {
                    str(key): value
                    for key, value in args.items()
                    if isinstance(value, (str, int, float, bool))
                }
            elif isinstance(args, str):
                attributes = {"_raw": args}
            out.append(
                {
                    "kind": "handler" if handler else "task",
                    "name": str(task.get("name") or module),
                    "module": module,
                    "attributes": attributes,
                    "parent": parent,
                }
            )
        return out

class PrometheusRulesExtractor:
    """Deep-parse Prometheus rule files, scrape config, and Alertmanager config.

    Alerting rules are SLO/acceptance-criteria gold: each ``alert`` carries a
    threshold expression, a sustain duration (``for``), and a severity — i.e. a
    machine-readable candidate acceptance criterion. This extractor upgrades
    the previous recognition-only treatment into structured
    ``ConfigInfo.resources`` entries:

    * ``AlertRule`` / ``RecordingRule`` — name + attributes (expr, for,
      severity, labels, summary/description annotations, group, interval).
    * ``ScrapeJob`` — job name + shallow target count (``prometheus.yml`` with
      ``scrape_configs:``).
    * ``AlertmanagerReceiver`` / ``AlertmanagerRoute`` — names only, shallow.

    Tolerant: malformed YAML or unexpected shapes yield an empty/partial
    :class:`ConfigInfo`; such files fall back to the recognition-only path.
    """

    format = "prometheus"

    def detect_prometheus_files(self, project_root: Path) -> list[Path]:
        root = Path(project_root)
        matches: list[Path] = []
        for file_path in _iter_project_files(root, {".yaml", ".yml"}):
            lname = file_path.name.lower()
            if (
                lname.endswith((".rules.yml", ".rules.yaml"))
                or lname.startswith("prometheus")
                or lname.startswith("alertmanager")
            ):
                matches.append(file_path)
                continue
            # Content sniff: ``groups:`` whose entries carry ``rules:`` is a
            # Prometheus rule file regardless of name.
            try:
                payload = yaml.safe_load(
                    file_path.read_text(encoding="utf-8", errors="ignore")
                )
            except Exception:
                continue
            if self._looks_like_rules(payload):
                matches.append(file_path)
        return matches

    @staticmethod
    def _looks_like_rules(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        groups = payload.get("groups")
        return isinstance(groups, list) and any(
            isinstance(group, dict) and isinstance(group.get("rules"), list)
            for group in groups
        )

    def extract_prometheus(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        try:
            payload = yaml.safe_load(content)
        except Exception:
            return info
        if not isinstance(payload, dict):
            return info

        self._extract_rule_groups(payload, info)
        self._extract_scrape_configs(payload, info)
        self._extract_alertmanager(payload, info)
        return info

    def _extract_rule_groups(self, payload: dict[str, Any], info: ConfigInfo) -> None:
        for group in payload.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "")
            interval = group.get("interval")
            for rule in group.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                entry = self._rule_entry(rule, group_name, interval)
                if entry is not None:
                    info.resources.append(entry)

    @staticmethod
    def _rule_entry(
        rule: dict[str, Any], group_name: str, interval: Any
    ) -> dict[str, Any] | None:
        alert = rule.get("alert")
        record = rule.get("record")
        if alert is None and record is None:
            return None
        labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}
        annotations = (
            rule.get("annotations") if isinstance(rule.get("annotations"), dict) else {}
        )
        attributes: dict[str, Any] = {
            "expr": str(rule.get("expr") or ""),
            "group": group_name,
        }
        if interval is not None:
            attributes["interval"] = str(interval)
        if alert is not None:
            if rule.get("for") is not None:
                attributes["for"] = str(rule.get("for"))
            severity = labels.get("severity")
            if severity is not None:
                attributes["severity"] = str(severity)
            if labels:
                attributes["labels"] = {str(k): str(v) for k, v in labels.items()}
            for key in ("summary", "description"):
                if annotations.get(key) is not None:
                    attributes[key] = str(annotations[key])
            return {"kind": "AlertRule", "name": str(alert), "attributes": attributes}
        return {"kind": "RecordingRule", "name": str(record), "attributes": attributes}

    @staticmethod
    def _extract_scrape_configs(payload: dict[str, Any], info: ConfigInfo) -> None:
        for scrape in payload.get("scrape_configs") or []:
            if not isinstance(scrape, dict):
                continue
            job_name = scrape.get("job_name")
            if job_name is None:
                continue
            targets = 0
            for static in scrape.get("static_configs") or []:
                if isinstance(static, dict) and isinstance(static.get("targets"), list):
                    targets += len(static["targets"])
            info.resources.append(
                {
                    "kind": "ScrapeJob",
                    "name": str(job_name),
                    "attributes": {"targets_count": targets},
                }
            )

    @staticmethod
    def _extract_alertmanager(payload: dict[str, Any], info: ConfigInfo) -> None:
        # Shallow by design: receiver names + route-tree presence only.
        receivers = payload.get("receivers")
        route = payload.get("route")
        if not isinstance(receivers, list) and not isinstance(route, dict):
            return
        for receiver in receivers or []:
            if isinstance(receiver, dict) and receiver.get("name") is not None:
                info.resources.append(
                    {
                        "kind": "AlertmanagerReceiver",
                        "name": str(receiver["name"]),
                        "attributes": {},
                    }
                )
        if isinstance(route, dict):
            child_routes = route.get("routes")
            info.resources.append(
                {
                    "kind": "AlertmanagerRoute",
                    "name": str(route.get("receiver") or "default"),
                    "attributes": {
                        "child_routes": len(child_routes)
                        if isinstance(child_routes, list)
                        else 0,
                    },
                }
            )

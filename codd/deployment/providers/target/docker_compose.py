"""Docker Compose deployment verification provider."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from codd.deployment import EDGE_EXECUTES_IN_ORDER
from codd.deployment.providers import DeployTarget, register_deploy_target


@dataclass(frozen=True)
class DeployStep:
    name: str
    command: str
    order: int


@register_deploy_target("docker_compose")
class DockerComposeTarget(DeployTarget):
    """Parse Docker Compose deployment metadata for the C6 DAG."""

    def __init__(
        self,
        deploy_yaml: dict[str, Any] | None = None,
        *,
        target_name: str | None = None,
        defaults_path: Path | None = None,
    ) -> None:
        self.deploy_yaml = deploy_yaml or {}
        self.target_name = target_name
        self.defaults_path = defaults_path or (
            Path(__file__).resolve().parents[2] / "defaults" / "deploy_targets.yaml"
        )

    def parse_deploy_yaml(self, deploy_yaml: dict) -> list[DeployStep]:
        """
        Convert ``targets.<name>.steps`` entries into ordered deployment steps.

        Missing ``steps`` is treated as the legacy deploy.yaml format and returns
        no steps instead of blocking existing projects.
        """
        self.deploy_yaml = deploy_yaml or {}

        steps: list[DeployStep] = []
        for _target_name, target_config in self._iter_target_configs(self.deploy_yaml):
            for index, raw_step in enumerate(target_config.get("steps", []) or [], start=1):
                step = self._coerce_step(raw_step, index)
                if step is not None:
                    steps.append(step)
        return steps

    def infer_executes_in_order(self, deployment_doc) -> list[tuple]:
        """Infer ordered execution edges from deployment document sections."""
        sections = self._read_field(deployment_doc, "sections", []) or []
        if not sections:
            return []

        doc_id = self._deployment_doc_id(deployment_doc)
        edges: list[tuple] = []
        for section in sections:
            impl_file = self._section_to_impl_file(str(section))
            if impl_file is None:
                continue
            edges.append(
                (
                    doc_id,
                    impl_file,
                    EDGE_EXECUTES_IN_ORDER,
                    {"order": len(edges) + 1},
                )
            )
        return edges

    def get_post_deploy_hooks(self) -> list[str]:
        """Return post-deploy verification commands from the configured deploy.yaml."""
        hooks: list[str] = []
        for _target_name, target_config in self._iter_target_configs(self.deploy_yaml):
            hooks.extend(self._coerce_commands(target_config.get("post_deploy")))
        return hooks

    def get_compose_file(self, project_root: Path) -> Path | None:
        """Locate the Docker Compose file, honoring deployment defaults first."""
        root = Path(project_root)
        for candidate in self._compose_file_candidates():
            path = root / candidate
            if path.is_file():
                return path
        return None

    def _iter_target_configs(self, deploy_yaml: dict[str, Any]) -> Iterable[tuple[str | None, dict]]:
        targets = deploy_yaml.get("targets")
        if isinstance(targets, dict):
            for name, config in targets.items():
                if self.target_name is not None and name != self.target_name:
                    continue
                if not isinstance(config, dict):
                    continue
                target_type = config.get("type")
                if target_type not in (None, "docker_compose"):
                    continue
                yield str(name), config
            return

        if "steps" in deploy_yaml or "post_deploy" in deploy_yaml:
            yield self.target_name, deploy_yaml

    @staticmethod
    def _coerce_step(raw_step: Any, index: int) -> DeployStep | None:
        if isinstance(raw_step, str):
            return DeployStep(name=f"step_{index}", command=raw_step, order=index)
        if not isinstance(raw_step, dict):
            return None

        name = str(raw_step.get("name") or f"step_{index}")
        command = str(raw_step.get("command") or "")
        order = DockerComposeTarget._coerce_order(raw_step.get("order"), index)
        return DeployStep(name=name, command=command, order=order)

    @staticmethod
    def _coerce_order(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _coerce_commands(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            command = value.get("command")
            return [str(command)] if command else []
        if isinstance(value, list):
            commands: list[str] = []
            for item in value:
                commands.extend(DockerComposeTarget._coerce_commands(item))
            return commands
        return []

    @staticmethod
    def _read_field(obj: Any, field_name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(field_name, default)
        return getattr(obj, field_name, default)

    @classmethod
    def _deployment_doc_id(cls, deployment_doc: Any) -> str:
        return str(
            cls._read_field(
                deployment_doc,
                "id",
                cls._read_field(deployment_doc, "path", "deployment_doc"),
            )
        )

    @staticmethod
    def _section_to_impl_file(section: str) -> str | None:
        normalized = section.strip().lstrip("#").strip().lower()
        if "migrate" in normalized or "migration" in normalized:
            return "prisma/migrate"
        if "seed" in normalized:
            return "prisma/seed.ts"
        if "build" in normalized:
            return "Dockerfile"
        return None

    def _compose_file_candidates(self) -> list[str]:
        configured = self._configured_compose_file()
        candidates = [
            configured,
            "docker-compose.production.yml",
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        ]

        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _configured_compose_file(self) -> str | None:
        if not self.defaults_path.is_file():
            return None

        defaults = yaml.safe_load(self.defaults_path.read_text(encoding="utf-8")) or {}
        docker_compose = defaults.get("targets", {}).get("docker_compose", {})
        compose_file = docker_compose.get("compose_file")
        return str(compose_file) if compose_file else None

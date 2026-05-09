"""Derive V-model implementation tasks from design documents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, ClassVar, Literal, Mapping

import yaml

from codd.dag import Node
from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand


VModelLayer = Literal["requirement", "basic", "detailed"]
VALID_V_MODEL_LAYERS: set[str] = {"requirement", "basic", "detailed"}
VALID_TEST_KINDS: set[str] = {"unit", "integration", "e2e"}
DEFAULT_PROVIDER_NAME = "subprocess_ai_command"
DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("templates") / "plan_derive_meta.md"

LOGGER = logging.getLogger(__name__)
PLAN_DERIVERS: dict[str, type["PlanDeriver"]] = {}
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


@dataclass
class DerivedTask:
    id: str
    title: str
    description: str
    source_design_doc: str
    v_model_layer: VModelLayer
    expected_outputs: list[str] = field(default_factory=list)
    test_kinds: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    provider_id: str = ""
    generated_at: str = ""
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DerivedTask":
        layer = _layer_value(payload.get("v_model_layer"))
        if layer is None:
            raise ValueError("v_model_layer is required")
        item_id = _required_text(payload, "id")
        if not _SNAKE_CASE_RE.match(item_id):
            raise ValueError(f"task id must be snake_case: {item_id}")
        return cls(
            id=item_id,
            title=_required_text(payload, "title"),
            description=_required_text(payload, "description"),
            source_design_doc=_required_text(payload, "source_design_doc"),
            v_model_layer=layer,
            expected_outputs=_string_list(payload.get("expected_outputs")),
            test_kinds=_test_kind_list(payload.get("test_kinds")),
            dependencies=_string_list(payload.get("dependencies")),
            provider_id=str(payload.get("provider_id") or ""),
            generated_at=str(payload.get("generated_at") or ""),
            approved=bool(payload.get("approved", False)),
        )


@dataclass(frozen=True)
class DerivedTaskCacheRecord:
    provider_id: str
    cache_key: str
    design_doc_sha: str
    prompt_template_sha: str
    generated_at: str
    design_docs: list[str]
    tasks: list[DerivedTask]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "cache_key": self.cache_key,
            "design_doc_sha": self.design_doc_sha,
            "prompt_template_sha": self.prompt_template_sha,
            "generated_at": self.generated_at,
            "design_docs": list(self.design_docs),
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DerivedTaskCacheRecord":
        return cls(
            provider_id=str(payload["provider_id"]),
            cache_key=str(payload["cache_key"]),
            design_doc_sha=str(payload["design_doc_sha"]),
            prompt_template_sha=str(payload["prompt_template_sha"]),
            generated_at=str(payload["generated_at"]),
            design_docs=_string_list(payload.get("design_docs")),
            tasks=[DerivedTask.from_dict(item) for item in _mapping_list(payload.get("tasks"))],
        )


class PlanDeriver(ABC):
    provider_name: ClassVar[str]

    @abstractmethod
    def derive_tasks(
        self,
        design_docs: list[Node],
        v_model_layer: VModelLayer,
        project_context: dict,
    ) -> list[DerivedTask]:
        ...


def register_plan_deriver(name: str):
    def decorator(cls: type[PlanDeriver]):
        PLAN_DERIVERS[name] = cls
        cls.provider_name = name
        return cls

    return decorator


@register_plan_deriver(DEFAULT_PROVIDER_NAME)
class SubprocessAiCommandPlanDeriver(PlanDeriver):
    provider_name = DEFAULT_PROVIDER_NAME

    def __init__(
        self,
        ai_command: Any | None = None,
        *,
        provider_id: str | None = None,
        template_path: Path | str | None = None,
        model: str | None = None,
    ) -> None:
        self.ai_command = ai_command
        self._provider_id = provider_id
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
        self.model = model

    def derive_tasks(
        self,
        design_docs: list[Node],
        v_model_layer: VModelLayer,
        project_context: dict,
    ) -> list[DerivedTask]:
        if not design_docs:
            return []

        layer = _require_layer(v_model_layer)
        bundle = design_doc_bundle(design_docs, project_context)
        provider_id = self._resolve_provider_id(project_context)
        template_text = self.template_path.read_text(encoding="utf-8")
        prompt_template_sha = sha256_text(template_text)
        design_doc_sha = sha256_text(bundle)
        cache_key = derived_task_cache_key(
            design_doc_sha=design_doc_sha,
            provider_id=provider_id,
            prompt_template_sha=prompt_template_sha,
        )
        cache_path = derived_task_cache_path(design_docs, project_context)
        use_cache = bool(project_context.get("use_cache", True)) and not bool(project_context.get("force", False))
        write_cache = bool(project_context.get("write_cache", True)) and not bool(project_context.get("dry_run", False))

        if use_cache:
            cached = read_derived_task_cache(cache_path)
            if cached is not None and cached.cache_key == cache_key:
                return cached.tasks

        prompt = template_text.replace("{design_doc_bundle}", bundle)
        prompt = prompt.replace("{v_model_layer}", layer)
        prompt = prompt.replace("{project_context}", json.dumps(project_context.get("project_context", {}), sort_keys=True))

        try:
            raw_output = self._invoke(prompt)
        except AiCommandError as exc:
            LOGGER.warning("Plan derivation command failed: %s", exc)
            return []

        generated_at = utc_timestamp()
        tasks = parse_derived_tasks(
            raw_output,
            provider_id=provider_id,
            generated_at=generated_at,
            default_source_design_doc=_default_source_design_doc(design_docs),
            default_v_model_layer=layer,
        )
        tasks = apply_declarative_v_model_layers(tasks, design_docs)

        if write_cache:
            write_derived_task_cache(
                cache_path,
                DerivedTaskCacheRecord(
                    provider_id=provider_id,
                    cache_key=cache_key,
                    design_doc_sha=design_doc_sha,
                    prompt_template_sha=prompt_template_sha,
                    generated_at=generated_at,
                    design_docs=[node.path or node.id for node in design_docs],
                    tasks=tasks,
                ),
            )
        return tasks

    def _invoke(self, prompt: str) -> str:
        command = self.ai_command or SubprocessAiCommand()
        if hasattr(command, "invoke"):
            try:
                return command.invoke(prompt, model=self.model)
            except TypeError:
                return command.invoke(prompt)
        if callable(command):
            return command(prompt)
        raise TypeError("ai_command must be callable or expose invoke()")

    def _resolve_provider_id(self, project_context: Mapping[str, Any]) -> str:
        if self._provider_id:
            return self._provider_id
        command = self.ai_command
        if command is not None and hasattr(command, "provider_id"):
            provider_id = command.provider_id
            if callable(provider_id):
                try:
                    return str(provider_id(model=self.model))
                except TypeError:
                    return str(provider_id())
        context_provider = project_context.get("provider_id")
        return str(context_provider or self.provider_name)


def parse_derived_tasks(
    raw_output: str,
    *,
    provider_id: str,
    generated_at: str,
    default_source_design_doc: str = "",
    default_v_model_layer: VModelLayer = "detailed",
) -> list[DerivedTask]:
    try:
        payload = json.loads(strip_json_fence(raw_output))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Skipping plan derivation output: invalid JSON: %s", exc)
        return []

    entries = _task_entries(payload)
    if entries is None:
        LOGGER.warning("Skipping plan derivation output: expected a task array")
        return []

    parsed: list[DerivedTask] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            LOGGER.warning("Skipping derived task at index %s: entry must be an object", index)
            continue
        payload_entry = dict(entry)
        payload_entry.setdefault("source_design_doc", default_source_design_doc)
        payload_entry.setdefault("v_model_layer", default_v_model_layer)
        payload_entry.setdefault("provider_id", provider_id)
        payload_entry.setdefault("generated_at", generated_at)
        payload_entry.setdefault("approved", False)
        try:
            parsed.append(DerivedTask.from_dict(payload_entry))
        except ValueError as exc:
            LOGGER.warning("Skipping derived task at index %s: %s", index, exc)
    return parsed


def apply_declarative_v_model_layers(tasks: list[DerivedTask], design_docs: list[Node]) -> list[DerivedTask]:
    layers_by_doc = {
        node.path or node.id: layer
        for node in design_docs
        if (layer := declarative_v_model_layer(node)) is not None
    }
    node_id_layers = {
        str((node.attributes or {}).get("node_id")): layer
        for node in design_docs
        if (node.attributes or {}).get("node_id") and (layer := declarative_v_model_layer(node)) is not None
    }
    if not layers_by_doc and not node_id_layers:
        return tasks
    overridden: list[DerivedTask] = []
    for task in tasks:
        layer = layers_by_doc.get(task.source_design_doc) or node_id_layers.get(task.source_design_doc)
        if layer is None:
            overridden.append(task)
            continue
        data = task.to_dict()
        data["v_model_layer"] = layer
        overridden.append(DerivedTask.from_dict(data))
    return overridden


def declarative_v_model_layer(node: Node) -> VModelLayer | None:
    attributes = node.attributes or {}
    direct = _layer_value(attributes.get("v_model_layer"))
    if direct is not None:
        return direct
    frontmatter = attributes.get("frontmatter")
    if not isinstance(frontmatter, Mapping):
        return None
    codd_meta = frontmatter.get("codd")
    if isinstance(codd_meta, Mapping):
        nested = _layer_value(codd_meta.get("v_model_layer"))
        if nested is not None:
            return nested
    return _layer_value(frontmatter.get("v_model_layer"))


def design_doc_bundle(design_docs: list[Node], project_context: Mapping[str, Any]) -> str:
    project_root = Path(str(project_context.get("project_root") or Path.cwd())).resolve()
    parts: list[str] = []
    for node in design_docs:
        content = _node_content(node, project_root)
        layer = declarative_v_model_layer(node)
        header = {
            "id": node.id,
            "path": node.path,
            "v_model_layer": layer,
        }
        parts.append(
            "DESIGN_DOC\n"
            f"{json.dumps(header, sort_keys=True)}\n"
            "CONTENT\n"
            f"{content.strip()}\n"
        )
    return "\n---\n".join(parts)


def derived_task_cache_key(
    *,
    design_doc_sha: str,
    provider_id: str,
    prompt_template_sha: str,
) -> str:
    return sha256_text(f"{design_doc_sha}\0{provider_id}\0{prompt_template_sha}")


def derived_task_cache_path(design_docs: list[Node], project_context: Mapping[str, Any]) -> Path:
    project_root = Path(str(project_context.get("project_root") or Path.cwd())).resolve()
    cache_dir = Path(str(project_context.get("cache_dir") or project_root / ".codd" / "derived_tasks"))
    if not cache_dir.is_absolute():
        cache_dir = project_root / cache_dir
    return cache_dir / f"{_design_doc_path_safe(design_docs)}.yaml"


def read_derived_task_cache(path: Path) -> DerivedTaskCacheRecord | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, Mapping):
            return None
        return DerivedTaskCacheRecord.from_dict(payload)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None


def write_derived_task_cache(path: Path, record: DerivedTaskCacheRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(record.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def approve_cached_tasks(path: Path, *, task_id: str | None = None, approve_all: bool = False) -> int:
    record = read_derived_task_cache(path)
    if record is None:
        raise FileNotFoundError(f"derived task cache not found: {path}")
    changed = 0
    approved_tasks: list[DerivedTask] = []
    for task in record.tasks:
        should_approve = approve_all or task.id == task_id
        if should_approve and not task.approved:
            changed += 1
            data = task.to_dict()
            data["approved"] = True
            approved_tasks.append(DerivedTask.from_dict(data))
        else:
            approved_tasks.append(task)
    if not approve_all and task_id and not any(task.id == task_id for task in record.tasks):
        raise ValueError(f"derived task not found: {task_id}")
    write_derived_task_cache(
        path,
        DerivedTaskCacheRecord(
            provider_id=record.provider_id,
            cache_key=record.cache_key,
            design_doc_sha=record.design_doc_sha,
            prompt_template_sha=record.prompt_template_sha,
            generated_at=record.generated_at,
            design_docs=record.design_docs,
            tasks=approved_tasks,
        ),
    )
    return changed


def iter_derived_task_records(project_root: Path, design_doc: str | None = None) -> list[tuple[Path, DerivedTaskCacheRecord]]:
    cache_dir = project_root / ".codd" / "derived_tasks"
    records: list[tuple[Path, DerivedTaskCacheRecord]] = []
    if not cache_dir.is_dir():
        return records
    normalized_doc = _normalize_doc_path(design_doc) if design_doc else None
    for path in sorted(cache_dir.glob("*.yaml")):
        record = read_derived_task_cache(path)
        if record is None:
            continue
        if normalized_doc and normalized_doc not in {_normalize_doc_path(item) for item in record.design_docs}:
            continue
        records.append((path, record))
    return records


def find_derived_task_cache(project_root: Path, design_doc: str) -> Path:
    normalized = _normalize_doc_path(design_doc)
    for path, record in iter_derived_task_records(project_root):
        if normalized in {_normalize_doc_path(item) for item in record.design_docs}:
            return path
    return project_root / ".codd" / "derived_tasks" / f"{_slug_text(normalized)}.yaml"


def approved_tasks_markdown(tasks: list[DerivedTask]) -> str:
    lines: list[str] = []
    for task in tasks:
        if not task.approved:
            continue
        lines.append(f"## {task.id} {task.title}")
        lines.append("")
        lines.append(task.description)
        lines.append("")
        if task.expected_outputs:
            lines.append("Outputs:")
            lines.extend(f"- {item}" for item in task.expected_outputs)
            lines.append("")
        if task.test_kinds:
            lines.append(f"Tests: {', '.join(task.test_kinds)}")
            lines.append("")
        if task.dependencies:
            lines.append(f"Depends on: {', '.join(task.dependencies)}")
            lines.append("")
    return "\n".join(lines).strip()


def merge_approved_tasks_into_plan(project_root: Path, tasks: list[DerivedTask]) -> int:
    """Append approved derived tasks to the project's requirements file.

    cmd_444 v2.11.0: ``codd implement`` no longer reads
    ``implementation_plan.md``. Derived tasks now live alongside the
    requirements they enrich, under the same ``## Derived Tasks`` heading,
    so a future ``codd implement --design <path>`` can pick the design
    node directly from the surrounding section.
    """

    markdown = approved_tasks_markdown(tasks)
    if not markdown:
        return 0
    plan_path = project_root / "docs" / "requirements" / "requirements.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    existing = plan_path.read_text(encoding="utf-8") if plan_path.exists() else "# Requirements\n"
    marker = "\n\n## Derived Tasks\n\n"
    if marker.strip() not in existing:
        existing = existing.rstrip() + marker
    existing = existing.rstrip() + "\n\n" + markdown + "\n"
    plan_path.write_text(existing, encoding="utf-8")
    return len([task for task in tasks if task.approved])


def strip_json_fence(raw_output: str) -> str:
    text = raw_output.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _node_content(node: Node, project_root: Path) -> str:
    attributes = node.attributes or {}
    for key in ("content", "body", "text"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value
    candidate = Path(node.path or node.id)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8", errors="ignore")
    return ""


def _default_source_design_doc(design_docs: list[Node]) -> str:
    first = design_docs[0]
    return first.path or first.id


def _task_entries(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    for key in ("tasks", "derived_tasks"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _test_kind_list(value: Any) -> list[str]:
    kinds = _string_list(value)
    invalid = [item for item in kinds if item not in VALID_TEST_KINDS]
    if invalid:
        raise ValueError(f"test_kinds contains unsupported value(s): {', '.join(invalid)}")
    return kinds


def _layer_value(value: Any) -> VModelLayer | None:
    text = str(value or "").strip()
    if text not in VALID_V_MODEL_LAYERS:
        return None
    return text  # type: ignore[return-value]


def _require_layer(value: Any) -> VModelLayer:
    layer = _layer_value(value)
    if layer is None:
        raise ValueError(f"invalid v_model_layer: {value}")
    return layer


def _design_doc_path_safe(design_docs: list[Node]) -> str:
    if len(design_docs) == 1:
        return _slug_text(design_docs[0].path or design_docs[0].id)
    joined = "\0".join(sorted(node.path or node.id for node in design_docs))
    return f"bundle_{sha256_text(joined)[:16]}"


def _slug_text(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return slug or "design_doc"


def _normalize_doc_path(value: str | None) -> str:
    if not value:
        return ""
    return Path(value).as_posix().lstrip("./")


__all__ = [
    "DEFAULT_PROVIDER_NAME",
    "PLAN_DERIVERS",
    "DerivedTask",
    "DerivedTaskCacheRecord",
    "PlanDeriver",
    "SubprocessAiCommandPlanDeriver",
    "apply_declarative_v_model_layers",
    "approve_cached_tasks",
    "declarative_v_model_layer",
    "derived_task_cache_key",
    "derived_task_cache_path",
    "design_doc_bundle",
    "find_derived_task_cache",
    "iter_derived_task_records",
    "merge_approved_tasks_into_plan",
    "parse_derived_tasks",
    "read_derived_task_cache",
    "register_plan_deriver",
    "write_derived_task_cache",
]

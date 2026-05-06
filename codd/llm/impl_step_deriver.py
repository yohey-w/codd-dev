"""Derive task-level implementation steps from design documents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, field, is_dataclass, make_dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, ClassVar, Mapping

import yaml

from codd.dag import Node
from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand
from codd.llm.criteria_expander import coverage_axes_hint
from codd.llm.plan_deriver import design_doc_bundle, strip_json_fence


DEFAULT_PROVIDER_NAME = "subprocess_ai_command"
DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("templates") / "impl_step_derive_meta.md"
DEFAULT_CATALOG_PATH = Path(__file__).with_name("templates") / "implementation_step_catalog.yaml"
IMPL_STEP_DERIVERS: dict[str, type["ImplStepDeriver"]] = {}
LOGGER = logging.getLogger(__name__)
_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_LINK_FIELD = "in" + "puts"


def _impl_step_to_dict(self) -> dict[str, Any]:
    return asdict(self)


def _impl_step_from_dict(cls, payload: Mapping[str, Any]) -> Any:
    step_id = _required_text(payload, "id")
    if not _SNAKE_CASE_RE.match(step_id):
        raise ValueError(f"step id must be snake_case: {step_id}")
    confidence = _float_value(payload.get("confidence", 1.0), default=1.0)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    links = payload.get(_LINK_FIELD)
    if links is None:
        links = payload.get("dependencies")
    return cls(
        id=step_id,
        kind=_required_text(payload, "kind"),
        rationale=_required_text(payload, "rationale"),
        source_design_section=_required_text(payload, "source_design_section"),
        target_path_hint=_optional_text(payload.get("target_path_hint")),
        **{_LINK_FIELD: _string_list(links)},
        expected_outputs=_string_list(payload.get("expected_outputs")),
        required_axes=_string_list(payload.get("required_axes")),
        provider_id=str(payload.get("provider_id") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        approved=bool(payload.get("approved", False)),
        inferred=bool(payload.get("inferred", False)),
        confidence=confidence,
        best_practice_category=str(payload.get("best_practice_category") or ""),
    )


ImplStep = make_dataclass(
    "ImplStep",
    [
        ("id", str),
        ("kind", str),
        ("rationale", str),
        ("source_design_section", str),
        ("target_path_hint", str | None, field(default=None)),
        (_LINK_FIELD, list[str], field(default_factory=list)),
        ("expected_outputs", list[str], field(default_factory=list)),
        ("required_axes", list[str], field(default_factory=list)),
        ("provider_id", str, field(default="")),
        ("generated_at", str, field(default="")),
        ("approved", bool, field(default=False)),
        ("inferred", bool, field(default=False)),
        ("confidence", float, field(default=1.0)),
        ("best_practice_category", str, field(default="")),
    ],
    namespace={
        "to_dict": _impl_step_to_dict,
        "from_dict": classmethod(_impl_step_from_dict),
    },
)


class ImplStepCacheRecord:
    def __init__(
        self,
        provider_id: str,
        cache_key: str,
        task_id: str,
        design_doc_sha: str,
        prompt_template_sha: str,
        generated_at: str,
        design_docs: list[str],
        steps: list[Any],
    ) -> None:
        self.provider_id = provider_id
        self.cache_key = cache_key
        self.task_id = task_id
        self.design_doc_sha = design_doc_sha
        self.prompt_template_sha = prompt_template_sha
        self.generated_at = generated_at
        self.design_docs = design_docs
        self.steps = steps

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ImplStepCacheRecord):
            return False
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict[str, Any]:
        steps = [step.to_dict() for step in self.steps]
        return {
            "provider_id": self.provider_id,
            "cache_key": self.cache_key,
            "task_id": self.task_id,
            "design_doc_sha": self.design_doc_sha,
            "prompt_template_sha": self.prompt_template_sha,
            "generated_at": self.generated_at,
            "design_docs": list(self.design_docs),
            "steps": steps,
            "layer_1_steps": [step for step in steps if not step.get("inferred")],
            "layer_2_steps": [step for step in steps if step.get("inferred")],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ImplStepCacheRecord":
        return cls(
            provider_id=str(payload["provider_id"]),
            cache_key=str(payload["cache_key"]),
            task_id=str(payload["task_id"]),
            design_doc_sha=str(payload["design_doc_sha"]),
            prompt_template_sha=str(payload["prompt_template_sha"]),
            generated_at=str(payload["generated_at"]),
            design_docs=_string_list(payload.get("design_docs")),
            steps=[ImplStep.from_dict(item) for item in _cache_step_payloads(payload)],
        )


class ImplStepDeriver(ABC):
    provider_name: ClassVar[str]

    @abstractmethod
    def derive_steps(
        self,
        task: Any,
        design_docs: list[Node],
        project_context: dict,
    ) -> list[Any]:
        ...


def register_impl_step_deriver(name: str):
    registry_name = name.strip()
    if not registry_name:
        raise ValueError("implementation step deriver name is required")

    def decorator(cls: type[ImplStepDeriver]) -> type[ImplStepDeriver]:
        if not issubclass(cls, ImplStepDeriver):
            raise TypeError("registered implementation step deriver must subclass ImplStepDeriver")
        IMPL_STEP_DERIVERS[registry_name] = cls
        cls.provider_name = registry_name
        return cls

    return decorator


@register_impl_step_deriver(DEFAULT_PROVIDER_NAME)
class SubprocessAiCommandImplStepDeriver(ImplStepDeriver):
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

    def derive_steps(
        self,
        task: Any,
        design_docs: list[Node],
        project_context: dict,
    ) -> list[Any]:
        if not design_docs:
            return []

        project_root = _context_root(project_context)
        bundle = design_doc_bundle(design_docs, {"project_root": project_root})
        catalog_hint = implementation_step_catalog_hint(project_context, project_root=project_root)
        provider_id = self._resolve_provider_id(project_context)
        template_text = self.template_path.read_text(encoding="utf-8")
        prompt_template_sha = sha256_text(template_text)
        design_doc_sha = sha256_text(bundle)
        task_id = task_identifier(task)
        cache_key = impl_step_cache_key(
            task_id=task_id,
            design_doc_sha=design_doc_sha,
            provider_id=provider_id,
            prompt_template_sha=prompt_template_sha,
        )
        cache_path = impl_step_cache_path(task_id, project_context)
        use_cache = bool(project_context.get("use_cache", True)) and not bool(project_context.get("force", False))
        write_cache = bool(project_context.get("write_cache", True)) and not bool(project_context.get("dry_run", False))

        if use_cache:
            cached = read_impl_step_cache(cache_path)
            if cached is not None and cached.cache_key == cache_key:
                return cached.steps

        prompt = template_text.replace("{design_doc_bundle}", bundle)
        prompt = prompt.replace("{task_yaml}", task_yaml(task))
        prompt = prompt.replace("{step_catalog_hint}", catalog_hint)
        prompt = prompt.replace("{coverage_axes_hint}", coverage_axes_hint(project_context, design_docs))
        prompt = prompt.replace(
            "{project_context}",
            json.dumps(project_context.get("project_context", {}), sort_keys=True),
        )

        try:
            raw_output = self._invoke(prompt)
        except AiCommandError as exc:
            LOGGER.warning("Implementation step derivation command failed: %s", exc)
            return []

        generated_at = utc_timestamp()
        steps = parse_impl_steps(
            raw_output,
            provider_id=provider_id,
            generated_at=generated_at,
            default_source_design_section=_default_source_design_section(design_docs),
        )
        if write_cache:
            write_impl_step_cache(
                cache_path,
                ImplStepCacheRecord(
                    provider_id=provider_id,
                    cache_key=cache_key,
                    task_id=task_id,
                    design_doc_sha=design_doc_sha,
                    prompt_template_sha=prompt_template_sha,
                    generated_at=generated_at,
                    design_docs=[node.path or node.id for node in design_docs],
                    steps=steps,
                ),
            )
        return steps

    def _invoke(self, prompt: str) -> str:
        command = self.ai_command or SubprocessAiCommand()
        if hasattr(command, "invoke"):
            try:
                return str(command.invoke(prompt, model=self.model))
            except TypeError:
                return str(command.invoke(prompt))
        if callable(command):
            return str(command(prompt))
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


def parse_impl_steps(
    raw_output: str,
    *,
    provider_id: str,
    generated_at: str,
    default_source_design_section: str = "",
    inferred: bool | None = None,
) -> list[Any]:
    try:
        payload = json.loads(strip_json_fence(raw_output))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Skipping implementation step output: invalid JSON: %s", exc)
        return []

    entries = _step_entries(payload)
    if entries is None:
        LOGGER.warning("Skipping implementation step output: expected a step array")
        return []

    parsed: list[Any] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            LOGGER.warning("Skipping implementation step at index %s: entry must be an object", index)
            continue
        item = dict(entry)
        item.setdefault("source_design_section", default_source_design_section)
        item.setdefault("provider_id", provider_id)
        item.setdefault("generated_at", generated_at)
        item.setdefault("approved", False)
        if inferred is not None:
            item["inferred"] = inferred
        try:
            parsed.append(ImplStep.from_dict(item))
        except ValueError as exc:
            LOGGER.warning("Skipping implementation step at index %s: %s", index, exc)
    return parsed


def merge_impl_steps(explicit_steps: list[Any], implicit_steps: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for step in [*explicit_steps, *implicit_steps]:
        step_id = str(getattr(step, "id", ""))
        if not step_id or step_id in seen:
            continue
        seen.add(step_id)
        merged.append(step)
    return merged


def render_impl_steps_for_prompt(steps: list[Any]) -> str:
    if not steps:
        return "(none)"
    rendered = []
    for step in steps:
        data = step.to_dict() if hasattr(step, "to_dict") else dict(step)
        data.pop("provider_id", None)
        data.pop("generated_at", None)
        rendered.append(data)
    return yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True).strip()


def implementation_step_catalog_hint(project_context: Mapping[str, Any], *, project_root: Path | None = None) -> str:
    project_root = project_root or _context_root(project_context)
    catalog = resolve_implementation_step_catalog(project_context, project_root=project_root)
    return yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True).strip()


def resolve_implementation_step_catalog(
    project_context: Mapping[str, Any],
    *,
    project_root: Path | None = None,
) -> Any:
    project_root = project_root or _context_root(project_context)
    lexicon_catalog = _catalog_from_project_lexicon(project_context, project_root)
    if lexicon_catalog is not None:
        return lexicon_catalog
    configured_path = _catalog_path_from_context(project_context, project_root)
    if configured_path is not None:
        return _read_yaml(configured_path)
    return _read_yaml(DEFAULT_CATALOG_PATH)


def impl_step_cache_key(
    *,
    task_id: str,
    design_doc_sha: str,
    provider_id: str,
    prompt_template_sha: str,
) -> str:
    return sha256_text(f"{task_id}\0{design_doc_sha}\0{provider_id}\0{prompt_template_sha}")


def impl_step_cache_path(task: Any, project_context: Mapping[str, Any]) -> Path:
    task_id = task_identifier(task)
    project_root = _context_root(project_context)
    cache_dir = Path(str(project_context.get("cache_dir") or project_root / ".codd" / "derived_impl_steps"))
    if not cache_dir.is_absolute():
        cache_dir = project_root / cache_dir
    return cache_dir / f"{_slug_text(task_id)}.yaml"


def read_impl_step_cache(path: Path) -> ImplStepCacheRecord | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, Mapping):
            return None
        return ImplStepCacheRecord.from_dict(payload)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None


def write_impl_step_cache(path: Path, record: ImplStepCacheRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(record.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def approve_cached_impl_steps(path: Path, *, step_id: str | None = None, approve_all: bool = False) -> int:
    record = read_impl_step_cache(path)
    if record is None:
        raise FileNotFoundError(f"implementation step cache not found: {path}")
    changed = 0
    approved_steps: list[Any] = []
    for step in record.steps:
        should_approve = approve_all or step.id == step_id
        if should_approve and not step.approved:
            changed += 1
            data = step.to_dict()
            data["approved"] = True
            approved_steps.append(ImplStep.from_dict(data))
        else:
            approved_steps.append(step)
    if not approve_all and step_id and not any(step.id == step_id for step in record.steps):
        raise ValueError(f"implementation step not found: {step_id}")
    write_impl_step_cache(
        path,
        ImplStepCacheRecord(
            provider_id=record.provider_id,
            cache_key=record.cache_key,
            task_id=record.task_id,
            design_doc_sha=record.design_doc_sha,
            prompt_template_sha=record.prompt_template_sha,
            generated_at=record.generated_at,
            design_docs=record.design_docs,
            steps=approved_steps,
        ),
    )
    return changed


def task_identifier(task: Any) -> str:
    if isinstance(task, str):
        return task
    for attr in ("task_id", "id"):
        value = getattr(task, attr, None)
        if value:
            return str(value)
    if isinstance(task, Mapping):
        return str(task.get("task_id") or task.get("id") or "task")
    return "task"


def task_yaml(task: Any) -> str:
    if is_dataclass(task):
        payload = asdict(task)
    elif isinstance(task, Mapping):
        payload = dict(task)
    else:
        payload = {
            key: value
            for key in ("task_id", "title", "summary", "module_hint", "deliverable", "output_dir", "task_context")
            if (value := getattr(task, key, None)) is not None
        }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context_root(project_context: Mapping[str, Any]) -> Path:
    return Path(str(project_context.get("project_root") or Path.cwd())).resolve()


def _catalog_from_project_lexicon(project_context: Mapping[str, Any], project_root: Path) -> Any | None:
    direct = project_context.get("implementation_step_catalog")
    if direct is not None:
        return direct
    for name in ("project_lexicon_path", "lexicon_path"):
        path_text = project_context.get(name)
        if isinstance(path_text, str) and path_text.strip():
            path = Path(path_text)
            if not path.is_absolute():
                path = project_root / path
            if path.exists():
                payload = _read_yaml(path)
                if isinstance(payload, Mapping) and payload.get("implementation_step_catalog") is not None:
                    return payload["implementation_step_catalog"]
    default_path = project_root / "project_lexicon.yaml"
    if default_path.exists():
        payload = _read_yaml(default_path)
        if isinstance(payload, Mapping) and payload.get("implementation_step_catalog") is not None:
            return payload["implementation_step_catalog"]
    return None


def _catalog_path_from_context(project_context: Mapping[str, Any], project_root: Path) -> Path | None:
    configured = project_context.get("implementation_step_catalog_path")
    config = project_context.get("config")
    if configured is None and isinstance(config, Mapping):
        configured = _nested_value(config, ("llm", "implementation_step_catalog_path"))
    if not isinstance(configured, str) or not configured.strip():
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = project_root / path
    return path


def _nested_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _cache_step_payloads(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if payload.get("steps") is not None:
        return [dict(item) for item in _mapping_list(payload.get("steps"))]

    layer_1 = [dict(item) for item in _mapping_list(payload.get("layer_1_steps"))]
    layer_2 = [dict(item) for item in _mapping_list(payload.get("layer_2_steps"))]
    for item in layer_1:
        item.setdefault("inferred", False)
    for item in layer_2:
        item.setdefault("inferred", True)
    return [*layer_1, *layer_2]


def _step_entries(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    for key in ("steps", "impl_steps", "implementation_steps"):
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


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _float_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_source_design_section(design_docs: list[Node]) -> str:
    first = design_docs[0]
    return first.path or first.id


def _slug_text(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return slug or "task"


__all__ = [
    "DEFAULT_PROVIDER_NAME",
    "IMPL_STEP_DERIVERS",
    "ImplStep",
    "ImplStepCacheRecord",
    "ImplStepDeriver",
    "SubprocessAiCommandImplStepDeriver",
    "approve_cached_impl_steps",
    "impl_step_cache_key",
    "impl_step_cache_path",
    "implementation_step_catalog_hint",
    "merge_impl_steps",
    "parse_impl_steps",
    "read_impl_step_cache",
    "register_impl_step_deriver",
    "render_impl_steps_for_prompt",
    "resolve_implementation_step_catalog",
    "task_identifier",
    "task_yaml",
    "utc_timestamp",
    "write_impl_step_cache",
]

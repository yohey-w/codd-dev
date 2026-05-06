"""Dynamic completion criteria expansion helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, ClassVar, Literal, Mapping

import yaml

from codd.config import load_project_config
from codd.dag import Node
from codd.deployment.providers.ai_command import SubprocessAiCommand


CriteriaSource = Literal["static", "expected_node", "expected_edge", "user_journey", "v_model"]
CriteriaSeverity = Literal["critical", "high", "medium", "info"]

ALLOWED_SOURCES: set[str] = {"static", "expected_node", "expected_edge", "user_journey", "v_model"}
ALLOWED_SEVERITIES: set[str] = {"critical", "high", "medium", "info"}
DEFAULT_TEMPLATE = Path(__file__).with_name("templates") / "criteria_expand_meta.md"


@dataclass(frozen=True)
class CriteriaItem:
    id: str
    text: str
    source: CriteriaSource
    source_ref: str
    severity: CriteriaSeverity

    def __post_init__(self) -> None:
        _require_text(self.id, "id")
        _require_text(self.text, "text")
        _require_text(self.source_ref, "source_ref")
        if self.source not in ALLOWED_SOURCES:
            raise ValueError(f"invalid criteria source: {self.source}")
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"invalid criteria severity: {self.severity}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CriteriaItem":
        return cls(
            id=str(payload.get("id") or "").strip(),
            text=str(payload.get("text") or payload.get("description") or "").strip(),
            source=_literal_source(str(payload.get("source") or "").strip()),
            source_ref=str(payload.get("source_ref") or payload.get("source") or "").strip(),
            severity=_literal_severity(str(payload.get("severity") or "medium").strip()),
        )


@dataclass(frozen=True)
class ExpandedCriteria:
    task_id: str
    static_items: list[CriteriaItem]
    dynamic_items: list[CriteriaItem]
    coverage_summary: dict[str, Any] = field(default_factory=dict)
    provider_id: str = ""
    generated_at: str = ""
    input_sha256: str = ""

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        _ensure_unique_ids([*self.static_items, *self.dynamic_items])

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "static_items": [item.to_dict() for item in self.static_items],
            "dynamic_items": [item.to_dict() for item in self.dynamic_items],
            "coverage_summary": dict(self.coverage_summary),
            "provider_id": self.provider_id,
            "generated_at": self.generated_at,
            "input_sha256": self.input_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExpandedCriteria":
        return cls(
            task_id=str(payload.get("task_id") or "").strip(),
            static_items=[CriteriaItem.from_dict(item) for item in _mappings(payload.get("static_items", []))],
            dynamic_items=[CriteriaItem.from_dict(item) for item in _mappings(payload.get("dynamic_items", []))],
            coverage_summary=_mapping_dict(payload.get("coverage_summary")),
            provider_id=str(payload.get("provider_id") or ""),
            generated_at=str(payload.get("generated_at") or ""),
            input_sha256=str(payload.get("input_sha256") or ""),
        )


@dataclass(frozen=True)
class TaskCriteriaSource:
    task_id: str
    static_criteria: list[str]
    path: Path | None = None


class CriteriaExpander(ABC):
    provider_name: ClassVar[str]

    @abstractmethod
    def expand(
        self,
        task_id: str,
        static_criteria: list[str],
        design_docs: list[Node],
        expected_extractions: list[dict[str, Any]],
        project_context: dict[str, Any],
    ) -> ExpandedCriteria:
        """Expand static checklist text into full criteria."""


CRITERIA_EXPANDERS: dict[str, type[CriteriaExpander]] = {}


def register_criteria_expander(name: str):
    registry_name = name.strip()
    if not registry_name:
        raise ValueError("criteria expander name is required")

    def decorator(cls: type[CriteriaExpander]) -> type[CriteriaExpander]:
        if not issubclass(cls, CriteriaExpander):
            raise TypeError("registered criteria expander must subclass CriteriaExpander")
        CRITERIA_EXPANDERS[registry_name] = cls
        return cls

    return decorator


@register_criteria_expander("subprocess_ai_command")
class SubprocessAiCommandCriteriaExpander(CriteriaExpander):
    provider_name = "subprocess_ai_command"

    def __init__(
        self,
        ai_command: Any | None = None,
        provider_id: str | None = None,
        project_root: Path | str | None = None,
        cache_dir: Path | str | None = None,
        model: str | None = None,
        use_cache: bool = True,
        template_path: Path | str | None = None,
    ) -> None:
        self.ai_command = ai_command
        self._provider_id = provider_id
        self.project_root = Path(project_root) if project_root is not None else None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.model = model
        self.use_cache = use_cache
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE

    def expand(
        self,
        task_id: str,
        static_criteria: list[str],
        design_docs: list[Node],
        expected_extractions: list[dict[str, Any]],
        project_context: dict[str, Any],
    ) -> ExpandedCriteria:
        project_root = _context_root(project_context, self.project_root)
        config = _load_config(project_root)
        model = str(project_context.get("model") or self.model or "") or None
        input_sha = expansion_input_sha256(task_id, design_docs, expected_extractions, project_context)
        cache_path = expanded_criteria_cache_path(project_root, task_id, self.cache_dir)
        use_cache = bool(project_context.get("use_cache", self.use_cache))
        if use_cache:
            cached = read_expanded_criteria_cache(cache_path, input_sha)
            if cached is not None:
                return cached

        adapter = self._adapter(project_root, config)
        provider_id = str(project_context.get("provider_id") or self._resolve_provider_id(adapter, model))
        prompt = build_criteria_expand_prompt(
            task_id=task_id,
            static_criteria=static_criteria,
            design_docs=design_docs,
            expected_extractions=expected_extractions,
            project_context=project_context,
            template_path=self.template_path,
        )
        raw_output = self._invoke(adapter, prompt, model)
        dynamic_items, coverage_summary = parse_dynamic_items(raw_output)
        result = ExpandedCriteria(
            task_id=task_id,
            static_items=static_criteria_items(static_criteria),
            dynamic_items=dynamic_items,
            coverage_summary=coverage_summary,
            provider_id=provider_id,
            generated_at=_utc_now(),
            input_sha256=input_sha,
        )
        write_expanded_criteria(cache_path, result)
        return result

    def _adapter(self, project_root: Path, config: Mapping[str, Any] | None) -> Any:
        if self.ai_command is None:
            return SubprocessAiCommand(
                command=_criteria_expand_command(config),
                project_root=project_root,
                config=config,
            )
        if isinstance(self.ai_command, str):
            return SubprocessAiCommand(
                command=self.ai_command,
                project_root=project_root,
                config=config,
            )
        return self.ai_command

    def _invoke(self, adapter: Any, prompt: str, model: str | None) -> str:
        if hasattr(adapter, "invoke"):
            try:
                return str(adapter.invoke(prompt, model=model))
            except TypeError:
                return str(adapter.invoke(prompt))
        if callable(adapter):
            return str(adapter(prompt))
        raise TypeError("ai_command must be callable or expose invoke()")

    def _resolve_provider_id(self, adapter: Any, model: str | None) -> str:
        if self._provider_id:
            return self._provider_id
        provider_id = getattr(adapter, "provider_id", None)
        if callable(provider_id):
            try:
                return str(provider_id(model=model))
            except TypeError:
                return str(provider_id())
        return self.provider_name


def static_criteria_items(static_criteria: list[str]) -> list[CriteriaItem]:
    return [
        CriteriaItem(
            id=f"static_{index:03d}",
            text=text.strip(),
            source="static",
            source_ref=f"completion_criteria[{index - 1}]",
            severity="critical",
        )
        for index, text in enumerate(static_criteria, start=1)
        if text.strip()
    ]


def build_criteria_expand_prompt(
    *,
    task_id: str,
    static_criteria: list[str],
    design_docs: list[Node],
    expected_extractions: list[dict[str, Any]],
    project_context: Mapping[str, Any] | None = None,
    template_path: Path | str = DEFAULT_TEMPLATE,
) -> str:
    context = project_context or {}
    template = Path(template_path).read_text(encoding="utf-8")
    replacements = {
        "{task_id}": task_id,
        "{static_criteria_json}": _stable_json(static_criteria),
        "{design_doc_bundle}": _stable_json(_design_doc_records(design_docs, context)),
        "{expected_extraction_json}": _stable_json(expected_extractions),
        "{project_context_json}": _stable_json(_prompt_context(context)),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def parse_dynamic_items(raw_output: str) -> tuple[list[CriteriaItem], dict[str, Any]]:
    payload = json.loads(_strip_json_fence(raw_output))
    if isinstance(payload, Mapping) and isinstance(payload.get("expanded_criteria"), Mapping):
        payload = payload["expanded_criteria"]
    if isinstance(payload, list):
        entries: Any = payload
        coverage_summary: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        entries = payload.get("dynamic_items", [])
        coverage_summary = _mapping_dict(payload.get("coverage_summary"))
    else:
        raise ValueError("criteria expansion output must be a JSON object or list")

    dynamic_items: list[CriteriaItem] = []
    for item in _mappings(entries):
        parsed = CriteriaItem.from_dict(item)
        if parsed.source == "static":
            raise ValueError("dynamic_items must not contain source=static")
        dynamic_items.append(parsed)
    _ensure_unique_ids(dynamic_items)
    return dynamic_items, coverage_summary


def expansion_input_sha256(
    task_id: str,
    design_docs: list[Node],
    expected_extractions: list[dict[str, Any]],
    project_context: Mapping[str, Any] | None = None,
) -> str:
    context = project_context or {}
    payload = {
        "task_id": task_id,
        "design_docs": _design_doc_records(design_docs, context),
        "expected_extractions": expected_extractions,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def expanded_criteria_cache_path(
    project_root: Path | str,
    task_id: str,
    cache_dir: Path | str | None = None,
) -> Path:
    base_dir = Path(cache_dir) if cache_dir is not None else Path(project_root) / ".codd" / "expanded_criteria"
    return base_dir / f"{_safe_name(task_id)}.yaml"


def read_expanded_criteria(path: Path | str) -> ExpandedCriteria:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError("expanded criteria cache must contain a mapping")
    return ExpandedCriteria.from_dict(payload)


def read_expanded_criteria_cache(path: Path | str, input_sha256: str) -> ExpandedCriteria | None:
    try:
        cached = read_expanded_criteria(path)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None
    if cached.input_sha256 != input_sha256:
        return None
    return cached


def write_expanded_criteria(path: Path | str, criteria: ExpandedCriteria) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(criteria.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_task_criteria(project_root: Path | str, task: str) -> TaskCriteriaSource:
    task_path = find_task_yaml(project_root, task)
    if task_path is None:
        return TaskCriteriaSource(task_id=_task_id_from_text(task), static_criteria=[], path=None)

    payload = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"task YAML must contain a mapping: {task_path}")
    return TaskCriteriaSource(
        task_id=str(payload.get("task_id") or payload.get("id") or _task_id_from_text(task)).strip(),
        static_criteria=_extract_static_criteria(payload),
        path=task_path,
    )


def find_task_yaml(project_root: Path | str, task: str) -> Path | None:
    raw = Path(task).expanduser()
    if raw.is_file():
        return raw.resolve()

    roots = _unique_paths([Path(project_root).resolve(), Path.cwd().resolve()])
    names = _task_file_names(task)
    dirs = (Path("."), Path(".codd/tasks"), Path("codd/tasks"), Path("queue/tasks"), Path("tasks"))

    for root in roots:
        for directory in dirs:
            for name in names:
                candidate = root / directory / name
                if candidate.is_file():
                    return candidate.resolve()

    for root in roots:
        for directory in dirs[1:]:
            base = root / directory
            if not base.is_dir():
                continue
            for candidate in sorted([*base.glob("*.yaml"), *base.glob("*.yml")]):
                try:
                    payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError):
                    continue
                if isinstance(payload, Mapping) and str(payload.get("task_id") or payload.get("id") or "") == task:
                    return candidate.resolve()
    return None


def load_design_docs(project_root: Path | str, paths: list[Path | str] | tuple[Path | str, ...]) -> list[Node]:
    root = Path(project_root).resolve()
    doc_paths = [Path(path).expanduser() for path in paths]
    if not doc_paths:
        doc_paths = sorted((root / "docs" / "design").glob("**/*.md"))

    nodes: list[Node] = []
    for path in doc_paths:
        resolved = path if path.is_absolute() else root / path
        if not resolved.is_file():
            continue
        try:
            node_path = resolved.relative_to(root).as_posix()
        except ValueError:
            node_path = resolved.as_posix()
        nodes.append(
            Node(
                id=node_path,
                kind="design_doc",
                path=node_path,
                attributes={"content": resolved.read_text(encoding="utf-8")},
            )
        )
    return nodes


def load_expected_extractions(paths: list[Path | str] | tuple[Path | str, ...]) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for path_value in paths:
        path = Path(path_value)
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, Mapping) and isinstance(payload.get("expected_extractions"), list):
            loaded.extend(_mappings(payload["expected_extractions"]))
            continue
        if isinstance(payload, Mapping) and (
            "expected_nodes" in payload or "expected_edges" in payload or "source_design_doc" in payload
        ):
            loaded.append(dict(payload))
            continue
        if isinstance(payload, list):
            loaded.extend(_mappings(payload))
            continue
        raise ValueError(f"expected extraction file has unsupported shape: {path}")
    return loaded


def evaluate_expanded_criteria(criteria: ExpandedCriteria) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for item in [*criteria.static_items, *criteria.dynamic_items]:
        failures = _criteria_item_failures(item)
        details.append(
            {
                **item.to_dict(),
                "status": "FAIL" if failures else "PASS",
                "failures": failures,
            }
        )

    pass_count = sum(1 for detail in details if detail["status"] == "PASS")
    fail_count = len(details) - pass_count
    return {
        "task_id": criteria.task_id,
        "provider_id": criteria.provider_id,
        "generated_at": criteria.generated_at,
        "input_sha256": criteria.input_sha256,
        "static_count": len(criteria.static_items),
        "dynamic_count": len(criteria.dynamic_items),
        "total": len(details),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "items": details,
    }


def _criteria_item_failures(item: CriteriaItem) -> list[str]:
    failures: list[str] = []
    for key, value in item.to_dict().items():
        if not str(value).strip():
            failures.append(f"{key} is required")
    if item.source not in ALLOWED_SOURCES:
        failures.append("source is invalid")
    if item.severity not in ALLOWED_SEVERITIES:
        failures.append("severity is invalid")
    return failures


def _extract_static_criteria(payload: Mapping[str, Any]) -> list[str]:
    for key in ("completion_criteria", "acceptance_criteria", "criteria"):
        if key in payload:
            return _criteria_lines(payload[key])
    description = payload.get("description") or payload.get("purpose") or ""
    if isinstance(description, str):
        parsed = _criteria_from_description(description)
        if parsed:
            return parsed
    return []


def _criteria_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return _list_lines(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, Mapping):
        return [str(item).strip() for item in value.values() if str(item).strip()]
    return []


def _criteria_from_description(description: str) -> list[str]:
    active = False
    collected: list[str] = []
    for line in description.splitlines():
        if re.search(r"(completion|acceptance|完了条件)", line, re.IGNORECASE):
            active = True
            continue
        if active and re.match(r"^\s*#{1,6}\s+", line):
            break
        if active:
            collected.extend(_markdown_bullet_lines(line))
    if collected:
        return collected
    return [item for line in description.splitlines() for item in _checkbox_lines(line)]


def _list_lines(text: str) -> list[str]:
    values = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$", line)
        if match:
            values.append(match.group("text").strip())
        elif line.strip():
            values.append(line.strip())
    return values


def _markdown_bullet_lines(text: str) -> list[str]:
    match = re.match(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(?P<text>.+?)\s*$", text)
    return [match.group("text").strip()] if match else []


def _checkbox_lines(text: str) -> list[str]:
    match = re.match(r"^\s*[-*]\s*\[[ xX]\]\s*(?P<text>.+?)\s*$", text)
    return [match.group("text").strip()] if match else []


def _strip_json_fence(raw_output: str) -> str:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _design_doc_records(design_docs: list[Node], context: Mapping[str, Any]) -> list[dict[str, Any]]:
    project_root = _context_root(context, None)
    records: list[dict[str, Any]] = []
    for node in design_docs:
        records.append(
            {
                "id": node.id,
                "kind": node.kind,
                "path": node.path,
                "content": _node_content(node, project_root),
                "attributes": {key: value for key, value in node.attributes.items() if key != "content"},
            }
        )
    return records


def _node_content(node: Node, project_root: Path) -> str:
    content = node.attributes.get("content")
    if content is not None:
        return str(content)
    if not node.path:
        return ""
    candidate = Path(node.path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return ""


def _prompt_context(context: Mapping[str, Any]) -> dict[str, Any]:
    allowed_keys = ("task_path", "project_name", "language", "model")
    return {key: context[key] for key in allowed_keys if key in context}


def _load_config(project_root: Path) -> Mapping[str, Any] | None:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return None


def _criteria_expand_command(config: Mapping[str, Any] | None) -> str | None:
    ai_commands = config.get("ai_commands") if isinstance(config, Mapping) else None
    if isinstance(ai_commands, Mapping):
        command = ai_commands.get("criteria_expand")
        if isinstance(command, str) and command.strip():
            return command
    return None


def _context_root(context: Mapping[str, Any] | None, fallback: Path | None) -> Path:
    if context and context.get("project_root"):
        return Path(str(context["project_root"])).resolve()
    if fallback is not None:
        return fallback.resolve()
    return Path.cwd().resolve()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "task"


def _task_file_names(task: str) -> list[str]:
    raw = Path(task).name
    names = [raw]
    if not raw.endswith((".yaml", ".yml")):
        names.extend([f"{raw}.yaml", f"{raw}.yml"])
    return list(dict.fromkeys(names))


def _task_id_from_text(task: str) -> str:
    raw = Path(task).stem if task.endswith((".yaml", ".yml")) else task
    return raw.strip() or "task"


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _literal_source(value: str) -> CriteriaSource:
    if value not in ALLOWED_SOURCES:
        raise ValueError(f"invalid criteria source: {value}")
    return value  # type: ignore[return-value]


def _literal_severity(value: str) -> CriteriaSeverity:
    if value not in ALLOWED_SEVERITIES:
        raise ValueError(f"invalid criteria severity: {value}")
    return value  # type: ignore[return-value]


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("expected a list of mappings")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("expected a list of mappings")
        result.append(item)
    return result


def _mapping_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _ensure_unique_ids(items: list[CriteriaItem]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"duplicate criteria id: {item.id}")
        seen.add(item.id)


def _require_text(value: str, field_name: str) -> None:
    if not str(value).strip():
        raise ValueError(f"{field_name} is required")


def _stable_json(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, sort_keys=True, indent=2)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CRITERIA_EXPANDERS",
    "CriteriaExpander",
    "CriteriaItem",
    "ExpandedCriteria",
    "SubprocessAiCommandCriteriaExpander",
    "TaskCriteriaSource",
    "build_criteria_expand_prompt",
    "evaluate_expanded_criteria",
    "expanded_criteria_cache_path",
    "expansion_input_sha256",
    "find_task_yaml",
    "load_design_docs",
    "load_expected_extractions",
    "load_task_criteria",
    "parse_dynamic_items",
    "read_expanded_criteria",
    "read_expanded_criteria_cache",
    "register_criteria_expander",
    "static_criteria_items",
    "write_expanded_criteria",
]

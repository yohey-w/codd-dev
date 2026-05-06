"""Design-document expected artifact extraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, ClassVar, Literal, Mapping

import yaml

from codd.dag import Node
from codd.dag.extractor import extract_design_doc_metadata


ExpectedNodeKind = Literal["impl_file", "test_file", "config_file"]
DEFAULT_AI_COMMAND = "ai"
DEFAULT_MAX_TREE_ENTRIES = 240
SKIPPED_TREE_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".codd",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


@dataclass
class ExpectedNode:
    kind: ExpectedNodeKind
    path_hint: str
    rationale: str
    source_design_section: str
    required_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExpectedNode":
        return cls(
            kind=_expected_node_kind(payload.get("kind")),
            path_hint=str(payload.get("path_hint") or "").strip(),
            rationale=str(payload.get("rationale") or "").strip(),
            source_design_section=str(payload.get("source_design_section") or "").strip(),
            required_capabilities=[str(item) for item in _as_list(payload.get("required_capabilities"))],
        )


@dataclass
class ExpectedEdge:
    from_path_hint: str
    to_path_hint: str
    kind: str
    rationale: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExpectedEdge":
        attributes = payload.get("attributes")
        return cls(
            from_path_hint=str(payload.get("from_path_hint") or "").strip(),
            to_path_hint=str(payload.get("to_path_hint") or "").strip(),
            kind=str(payload.get("kind") or "depends_on").strip() or "depends_on",
            rationale=str(payload.get("rationale") or "").strip(),
            attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
        )


@dataclass
class ExpectedExtraction:
    expected_nodes: list[ExpectedNode]
    expected_edges: list[ExpectedEdge]
    source_design_doc: str
    provider_id: str = ""
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_nodes": [node.to_dict() for node in self.expected_nodes],
            "expected_edges": [edge.to_dict() for edge in self.expected_edges],
            "source_design_doc": self.source_design_doc,
            "provider_id": self.provider_id,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExpectedExtraction":
        source = payload.get("expected_extraction")
        if isinstance(source, Mapping):
            payload = source
        return cls(
            expected_nodes=[
                ExpectedNode.from_dict(item)
                for item in _mapping_items(payload.get("expected_nodes"))
            ],
            expected_edges=[
                ExpectedEdge.from_dict(item)
                for item in _mapping_items(payload.get("expected_edges"))
            ],
            source_design_doc=str(payload.get("source_design_doc") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            generated_at=str(payload.get("generated_at") or ""),
        )


class DesignDocExtractor(ABC):
    provider_name: ClassVar[str]

    @abstractmethod
    def extract_expected_artifacts(
        self,
        design_doc: Node,
        project_context: dict[str, Any],
    ) -> ExpectedExtraction:
        """Return expected artifact hints for one design document."""


DESIGN_DOC_EXTRACTORS: dict[str, type[DesignDocExtractor]] = {}


def register_design_doc_extractor(name: str):
    def decorator(cls):
        DESIGN_DOC_EXTRACTORS[name] = cls
        return cls

    return decorator


@register_design_doc_extractor("subprocess_ai_command")
class SubprocessAiCommandDesignDocExtractor(DesignDocExtractor):
    provider_name = "subprocess_ai_command"
    template_path = Path(__file__).with_name("templates") / "design_doc_extract_meta.md"

    def __init__(
        self,
        ai_command: str | None = None,
        *,
        runner=subprocess.run,
        timeout_seconds: float | None = None,
        template_path: str | Path | None = None,
    ) -> None:
        self.ai_command = ai_command
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.template_path = Path(template_path) if template_path is not None else self.template_path

    def extract_expected_artifacts(
        self,
        design_doc: Node,
        project_context: dict[str, Any],
    ) -> ExpectedExtraction:
        config = _mapping(project_context.get("config"))
        project_root = _optional_path(project_context.get("project_root"))
        prompt = self._build_prompt(design_doc, project_context)
        command = _resolve_ai_command(config, self.ai_command)

        try:
            completed = self.runner(
                shlex.split(command),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=_resolve_timeout(config, self.timeout_seconds),
                check=False,
                cwd=str(project_root) if project_root is not None else None,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"AI command not found: {shlex.split(command)[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("AI command timed out") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise ValueError(f"AI command failed: {detail}")
        if not completed.stdout.strip():
            raise ValueError("AI command returned empty output")

        extraction = ExpectedExtraction.from_dict(_json_payload(completed.stdout))
        if not extraction.source_design_doc:
            extraction.source_design_doc = str(design_doc.path or design_doc.id)
        if not extraction.provider_id:
            extraction.provider_id = self._provider_id(command)
        if not extraction.generated_at:
            extraction.generated_at = _utc_now()
        return extraction

    def _build_prompt(self, design_doc: Node, project_context: dict[str, Any]) -> str:
        body = str(project_context.get("design_doc_body") or "")
        if not body:
            body = _read_design_body(design_doc, _optional_path(project_context.get("project_root")))
        tree_summary = str(project_context.get("project_structure_summary") or "")
        if not tree_summary:
            project_root = _optional_path(project_context.get("project_root"))
            tree_summary = build_project_structure_summary(project_root) if project_root is not None else ""
        template = self.template_path.read_text(encoding="utf-8")
        return (
            template.replace("{design_doc_body}", body.strip())
            .replace("{project_structure_summary}", tree_summary.strip())
            .strip()
            + "\n"
        )

    def _provider_id(self, command: str) -> str:
        digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        return f"{self.provider_name}:{digest[:12]}"


def extract_expected_artifacts_for_file(
    design_doc_path: Path | str,
    project_root: Path | str,
    *,
    config: Mapping[str, Any] | None = None,
    force: bool = False,
    extractor: DesignDocExtractor | None = None,
) -> ExpectedExtraction:
    root = Path(project_root).resolve()
    doc_path = Path(design_doc_path).resolve()
    source_hash = _sha256_file(doc_path)

    if not force:
        cached = load_cached_expected_extraction(root, doc_path, source_sha256=source_hash)
        if cached is not None:
            return cached

    metadata = extract_design_doc_metadata(doc_path)
    node = Node(
        id=_relative_id(doc_path, root),
        kind="design_doc",
        path=_relative_id(doc_path, root),
        attributes={
            "frontmatter": metadata["frontmatter"],
            "depends_on": metadata["depends_on"],
            "node_id": metadata.get("node_id"),
            **(metadata.get("attributes") or {}),
        },
    )
    active_extractor = extractor or SubprocessAiCommandDesignDocExtractor()
    extraction = active_extractor.extract_expected_artifacts(
        node,
        {
            "project_root": root,
            "config": dict(config or {}),
            "design_doc_body": metadata.get("body") or "",
            "project_structure_summary": build_project_structure_summary(root),
        },
    )
    save_expected_extraction(root, doc_path, extraction, source_sha256=source_hash)
    return extraction


def expected_extraction_cache_path(project_root: Path | str, design_doc_path: Path | str) -> Path:
    root = Path(project_root).resolve()
    doc_path = Path(design_doc_path).resolve()
    return root / ".codd" / "expected_extractions" / f"{_path_safe_id(doc_path, root)}.yaml"


def load_cached_expected_extraction(
    project_root: Path | str,
    design_doc_path: Path | str,
    *,
    source_sha256: str | None = None,
) -> ExpectedExtraction | None:
    path = expected_extraction_cache_path(project_root, design_doc_path)
    if not path.is_file():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        return None
    if source_sha256 and payload.get("source_sha256") and payload.get("source_sha256") != source_sha256:
        return None
    return ExpectedExtraction.from_dict(payload)


def save_expected_extraction(
    project_root: Path | str,
    design_doc_path: Path | str,
    extraction: ExpectedExtraction,
    *,
    source_sha256: str | None = None,
) -> Path:
    path = expected_extraction_cache_path(project_root, design_doc_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = extraction.to_dict()
    if source_sha256:
        payload["source_sha256"] = source_sha256
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def build_project_structure_summary(project_root: Path | str, max_entries: int = DEFAULT_MAX_TREE_ENTRIES) -> str:
    root = Path(project_root).resolve()
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(entries) >= max_entries:
            break
        if _skip_tree_path(path, root) or not path.is_file():
            continue
        entries.append(_relative_id(path, root))
    if not entries:
        return "- (no files found)"
    return "\n".join(f"- {entry}" for entry in entries)


def _resolve_ai_command(config: Mapping[str, Any], override: str | None) -> str:
    if override is not None:
        return _non_empty_command(override)
    raw = (
        os.environ.get("CODD_AI_COMMAND")
        or _nested_str(config, ("ai_commands", "design_doc_extract"))
        or _nested_str(config, ("llm", "command"))
        or _mapping(config).get("ai_command")
        or DEFAULT_AI_COMMAND
    )
    return _non_empty_command(raw)


def _resolve_timeout(config: Mapping[str, Any], override: float | None) -> float | None:
    if override is not None:
        return float(override)
    raw = os.environ.get("CODD_AI_TIMEOUT_SECONDS") or _nested_value(config, ("llm", "timeout_seconds"))
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _non_empty_command(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ai_command must be a non-empty string")
    if not shlex.split(value.strip()):
        raise ValueError("ai_command must not be empty")
    return value.strip()


def _json_payload(raw_output: str) -> Mapping[str, Any]:
    text = _strip_json_fence(raw_output)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI command returned invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("AI command must return a JSON object")
    return payload


def _strip_json_fence(raw_output: str) -> str:
    text = raw_output.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _read_design_body(design_doc: Node, project_root: Path | None) -> str:
    if project_root is None or not design_doc.path:
        return ""
    path = (project_root / design_doc.path).resolve()
    if not path.is_file():
        return ""
    return str(extract_design_doc_metadata(path).get("body") or "")


def _path_safe_id(design_doc_path: Path, project_root: Path) -> str:
    relative = _relative_id(design_doc_path, project_root)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", relative).strip("._-")
    return safe or hashlib.sha256(str(design_doc_path).encode("utf-8")).hexdigest()[:16]


def _relative_id(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _skip_tree_path(path: Path, project_root: Path) -> bool:
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIPPED_TREE_NAMES for part in parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_node_kind(value: Any) -> ExpectedNodeKind:
    text = str(value or "").strip()
    if text in {"impl_file", "test_file", "config_file"}:
        return text  # type: ignore[return-value]
    return "impl_file"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _nested_value(config: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _nested_str(config: Mapping[str, Any], path: tuple[str, ...]) -> str | None:
    value = _nested_value(config, path)
    return value if isinstance(value, str) else None


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).resolve()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DESIGN_DOC_EXTRACTORS",
    "DesignDocExtractor",
    "ExpectedEdge",
    "ExpectedExtraction",
    "ExpectedNode",
    "SubprocessAiCommandDesignDocExtractor",
    "build_project_structure_summary",
    "expected_extraction_cache_path",
    "extract_expected_artifacts_for_file",
    "load_cached_expected_extraction",
    "register_design_doc_extractor",
    "save_expected_extraction",
]

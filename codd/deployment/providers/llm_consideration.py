"""LLM-derived consideration provider for deployment verification planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from codd.deployment.providers import VERIFICATION_TEMPLATES
from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand


ApprovalStatus = Literal["pending", "approved", "skipped"]

_ALLOWED_APPROVAL_STATUSES: set[str] = {"pending", "approved", "skipped"}
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class VerificationStrategy:
    """Machine-readable strategy hint generated with a consideration."""

    engine: str
    layer: str = ""
    parallelizable: bool = False
    reason_for_choice: str = ""
    required_capabilities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Consideration:
    """One verification consideration emitted by the provider."""

    id: str
    description: str
    domain_hints: list[str] = field(default_factory=list)
    verification_strategy: VerificationStrategy | None = None
    approval_status: ApprovalStatus = "pending"


@dataclass(frozen=True)
class ConsiderationResult:
    """Provider result with cache identity fields."""

    considerations: list[Consideration]
    provider_id: str
    design_doc_sha: str
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConsiderationResult":
        considerations = [
            _consideration_from_mapping(item)
            for item in _list_of_mappings(payload.get("considerations", []))
        ]
        return cls(
            considerations=considerations,
            provider_id=str(payload["provider_id"]),
            design_doc_sha=str(payload["design_doc_sha"]),
            generated_at=str(payload["generated_at"]),
        )


class LlmConsiderationProvider:
    """Generate considerations from a design document via a subprocess AI command."""

    def __init__(
        self,
        ai_command: Any | None = None,
        provider_id: str | None = None,
        project_root: Path | str | None = None,
        cache_dir: Path | str | None = None,
        model: str | None = None,
        use_cache: bool = True,
    ) -> None:
        self.ai_command = ai_command or SubprocessAiCommand(project_root=project_root)
        self._provider_id = provider_id
        self.project_root = Path(project_root) if project_root is not None else None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.model = model
        self.use_cache = use_cache

    def provide(
        self,
        design_doc_content: str,
        context: Mapping[str, Any] | None = None,
    ) -> ConsiderationResult:
        """Return considerations, using a SHA/provider cache when available."""

        context = context or {}
        design_doc_sha = hashlib.sha256(design_doc_content.encode("utf-8")).hexdigest()
        model = str(context.get("model") or self.model or "") or None
        provider_id = str(context.get("provider_id") or self._resolve_provider_id(model))
        cache_path = self._cache_path(design_doc_sha, provider_id, context)
        should_use_cache = bool(context.get("use_cache", self.use_cache))

        if should_use_cache and cache_path.exists():
            cached = self._read_cache(cache_path)
            if (
                cached is not None
                and cached.design_doc_sha == design_doc_sha
                and cached.provider_id == provider_id
            ):
                return cached

        prompt = _build_prompt(design_doc_content, context)
        try:
            raw_output = self._invoke(prompt, model)
        except AiCommandError:
            return ConsiderationResult(
                considerations=[],
                provider_id=provider_id,
                design_doc_sha=design_doc_sha,
                generated_at=_utc_now(),
            )

        considerations = parse_considerations(raw_output, environ=context.get("env"))
        result = ConsiderationResult(
            considerations=considerations,
            provider_id=provider_id,
            design_doc_sha=design_doc_sha,
            generated_at=_utc_now(),
        )
        if should_use_cache:
            self._write_cache(cache_path, result)
        return result

    def filter_registered_verification_strategies(
        self,
        result: ConsiderationResult,
        registry: Mapping[str, Any] | None = None,
    ) -> ConsiderationResult:
        """Drop considerations whose strategy names an unregistered engine."""

        if registry is None:
            registry = VERIFICATION_TEMPLATES
        kept: list[Consideration] = []
        for consideration in result.considerations:
            strategy = consideration.verification_strategy
            if strategy is not None and strategy.engine and strategy.engine not in registry:
                logging.warning(
                    "Skipping consideration %s: verification engine is not registered: %s",
                    consideration.id,
                    strategy.engine,
                )
                continue
            kept.append(consideration)
        return ConsiderationResult(
            considerations=kept,
            provider_id=result.provider_id,
            design_doc_sha=result.design_doc_sha,
            generated_at=result.generated_at,
        )

    def _invoke(self, prompt: str, model: str | None) -> str:
        if hasattr(self.ai_command, "invoke"):
            try:
                return self.ai_command.invoke(prompt, model=model)
            except TypeError:
                return self.ai_command.invoke(prompt)
        if callable(self.ai_command):
            return self.ai_command(prompt)
        raise TypeError("ai_command must be callable or expose invoke()")

    def _resolve_provider_id(self, model: str | None) -> str:
        if self._provider_id:
            return self._provider_id
        provider_id = getattr(self.ai_command, "provider_id", None)
        if callable(provider_id):
            try:
                return str(provider_id(model=model))
            except TypeError:
                return str(provider_id())
        return "subprocess_ai_command"

    def _cache_path(
        self,
        design_doc_sha: str,
        provider_id: str,
        context: Mapping[str, Any],
    ) -> Path:
        cache_dir = Path(context["cache_dir"]) if "cache_dir" in context else self.cache_dir
        if cache_dir is None:
            project_root = Path(context["project_root"]) if "project_root" in context else self.project_root
            cache_dir = (project_root or Path.cwd()) / ".codd" / "consideration_cache"
        return cache_dir / f"{design_doc_sha}_{_provider_slug(provider_id)}.json"

    def _read_cache(self, path: Path) -> ConsiderationResult | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ConsiderationResult.from_dict(payload)
        except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
            return None

    def _write_cache(self, path: Path, result: ConsiderationResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )


def parse_considerations(
    raw_output: str,
    environ: Mapping[str, str] | None = None,
) -> list[Consideration]:
    """Parse AI JSON output into consideration dataclasses."""

    payload = json.loads(_strip_json_fence(raw_output))
    payload = _resolve_parameter_placeholders(payload, environ or os.environ)
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, Mapping):
        entries = payload.get("considerations", [])
    else:
        raise ValueError("AI output must be a JSON object or list")
    return [_consideration_from_mapping(item) for item in _list_of_mappings(entries)]


def _consideration_from_mapping(payload: Mapping[str, Any]) -> Consideration:
    item_id = str(payload.get("id") or "").strip()
    if not item_id:
        raise ValueError("consideration id is required")
    approval_status = str(payload.get("approval_status") or "pending")
    if approval_status not in _ALLOWED_APPROVAL_STATUSES:
        approval_status = "pending"
    return Consideration(
        id=item_id,
        description=str(payload.get("description") or payload.get("rationale") or item_id),
        domain_hints=_string_list(payload.get("domain_hints")),
        verification_strategy=_strategy_from_mapping(payload.get("verification_strategy")),
        approval_status=approval_status,  # type: ignore[arg-type]
    )


def _strategy_from_mapping(payload: Any) -> VerificationStrategy | None:
    if not isinstance(payload, Mapping):
        return None
    engine = str(payload.get("engine") or "").strip()
    if not engine:
        return None
    return VerificationStrategy(
        engine=engine,
        layer=str(payload.get("layer") or ""),
        parallelizable=bool(payload.get("parallelizable", False)),
        reason_for_choice=str(payload.get("reason_for_choice") or ""),
        required_capabilities=_string_list(payload.get("required_capabilities")),
    )


def _build_prompt(design_doc_content: str, context: Mapping[str, Any]) -> str:
    project_context = context.get("project_context", {})
    return (
        "Read the design document and return valid JSON with a top-level "
        "'considerations' list. Each item must include id, description, "
        "domain_hints, optional verification_strategy, and approval_status.\n\n"
        f"PROJECT CONTEXT:\n{json.dumps(project_context, default=str, sort_keys=True)}\n\n"
        f"DESIGN DOCUMENT:\n{design_doc_content}"
    )


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


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("considerations must be a list")
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _resolve_parameter_placeholders(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda match: environ.get(match.group(1), match.group(0)), value)
    if isinstance(value, list):
        return [_resolve_parameter_placeholders(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_parameter_placeholders(item, environ) for key, item in value.items()}
    return value


def _provider_slug(provider_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", provider_id).strip("._")
    if slug == provider_id and slug:
        return slug
    digest = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'provider'}_{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ApprovalStatus",
    "Consideration",
    "ConsiderationResult",
    "LlmConsiderationProvider",
    "VerificationStrategy",
    "parse_considerations",
]

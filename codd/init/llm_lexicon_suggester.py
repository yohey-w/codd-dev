"""LLM-backed lexicon recommendation from project context."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand
from codd.init.lexicon_suggest import default_lexicon_root
from codd.init.stack_detector import StackDetector


Confidence = Literal["high", "medium", "low"]
_CONFIDENCES: set[str] = {"high", "medium", "low"}
_MAX_DOC_CHARS = 24_000
_MAX_FILE_CHARS = 4_000


class AiCommand(Protocol):
    def invoke(self, prompt: str, model: str | None = None) -> str:
        """Return an AI response for the supplied prompt."""


@dataclass(frozen=True)
class LlmLexiconRecommendation:
    lexicon_id: str
    confidence: Confidence
    reason: str


@dataclass(frozen=True)
class LlmLexiconResult:
    detected_data_types: list[str]
    detected_function_traits: list[str]
    detected_tech_stack: list[str]
    recommendations: list[LlmLexiconRecommendation]


def llm_recommend_lexicons(
    project_root: Path,
    *,
    ai_command: AiCommand | None = None,
) -> LlmLexiconResult:
    """Recommend lexicons via AI, returning an empty result on unusable input."""

    root = Path(project_root)
    context = _collect_project_context(root)
    if not context["requirements"]:
        return _empty_result()

    available = _available_lexicons()
    if not available:
        return _empty_result()

    command = ai_command or SubprocessAiCommand(project_root=root)
    prompt = _build_prompt(context, available)
    try:
        raw_output = command.invoke(prompt)
        payload = json.loads(_extract_json_object(raw_output))
    except (AiCommandError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return _empty_result()
    return _result_from_payload(payload, set(available))


def _collect_project_context(project_root: Path) -> dict[str, Any]:
    requirements = _read_matching_files(
        project_root,
        [
            "requirements.md",
            ".codd/requirements.md",
            "codd/requirements.md",
            "docs/requirements.md",
            "docs/requirements*.md",
            "docs/requirements/**/*.md",
        ],
    )
    designs = _read_matching_files(
        project_root,
        [
            "design/*.md",
            ".codd/design/*.md",
            "codd/design/*.md",
            "docs/design/*.md",
        ],
    )
    detection = StackDetector().detect(project_root)
    return {
        "requirements": requirements,
        "designs": designs,
        "tech_stack_hints": detection.stack_hints,
        "detected_signals": detection.detected_signals,
    }


def _read_matching_files(project_root: Path, patterns: list[str]) -> list[dict[str, str]]:
    seen: set[Path] = set()
    files: list[dict[str, str]] = []
    budget = _MAX_DOC_CHARS
    for pattern in patterns:
        for path in sorted(project_root.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if budget <= 0:
                return files
            text = path.read_text(encoding="utf-8", errors="replace")[: min(_MAX_FILE_CHARS, budget)]
            budget -= len(text)
            files.append({"path": path.relative_to(project_root).as_posix(), "content": text})
    return files


def _available_lexicons() -> dict[str, str]:
    root = default_lexicon_root()
    available: dict[str, str] = {}
    if not root.is_dir():
        return available
    for manifest in sorted(root.glob("*/manifest.yaml")):
        try:
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        lexicon_id = manifest.parent.name
        if not lexicon_id:
            continue
        available[lexicon_id] = str(payload.get("description") or "").strip()
    return available


def _build_prompt(context: dict[str, Any], available_lexicons: dict[str, str]) -> str:
    payload = {
        "available_lexicons": available_lexicons,
        "project_context": context,
        "required_output_schema": {
            "detected_data_types": ["string"],
            "detected_function_traits": ["string"],
            "detected_tech_stack": ["string"],
            "recommendations": [
                {
                    "lexicon_id": "string from available_lexicons keys",
                    "confidence": "high | medium | low",
                    "reason": "short explanation referencing a data type or function trait",
                }
            ],
        },
    }
    return (
        "You are a requirements engineer. Analyze this project's documentation and tech stack.\n"
        "Identify:\n"
        "- Data types handled (personal information / credit card data / medical records / "
        "video content / etc.)\n"
        "- Function traits present (authentication flow / payment processing / public API / "
        "video streaming / etc.)\n"
        "- Tech stack (frameworks, databases, cloud platforms)\n\n"
        "Reasoning rules (apply dynamically):\n"
        "- personal information present -> recommend data_governance_appi_gdpr (or GDPR equivalent)\n"
        "- credit card data present -> recommend compliance_pci_dss_4\n"
        "- medical data present -> recommend compliance_hipaa\n"
        "- authentication flow present -> consider web_authn_webauthn\n"
        "- public REST API present -> recommend api_rest_openapi\n"
        "- etc.\n\n"
        "From the available lexicons, recommend the most relevant ones with lexicon_id, "
        "confidence (high/medium/low), and reason referencing the data type or function trait. "
        "Return JSON only. Do not include Markdown fences or prose.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _extract_json_object(raw_output: str) -> str:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM output did not contain a JSON object")
    return text[start : end + 1]


def _result_from_payload(payload: Any, available_ids: set[str]) -> LlmLexiconResult:
    if not isinstance(payload, dict):
        return _empty_result()

    recommendations: list[LlmLexiconRecommendation] = []
    seen: set[str] = set()
    rows = payload.get("recommendations", [])
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            lexicon_id = str(row.get("lexicon_id") or "").strip()
            confidence = str(row.get("confidence") or "").strip().lower()
            if lexicon_id not in available_ids or confidence not in _CONFIDENCES or lexicon_id in seen:
                continue
            seen.add(lexicon_id)
            recommendations.append(
                LlmLexiconRecommendation(
                    lexicon_id=lexicon_id,
                    confidence=confidence,  # type: ignore[arg-type]
                    reason=str(row.get("reason") or "").strip(),
                )
            )

    return LlmLexiconResult(
        detected_data_types=_string_list(payload.get("detected_data_types")),
        detected_function_traits=_string_list(payload.get("detected_function_traits")),
        detected_tech_stack=_string_list(payload.get("detected_tech_stack")),
        recommendations=recommendations,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _empty_result() -> LlmLexiconResult:
    return LlmLexiconResult(
        detected_data_types=[],
        detected_function_traits=[],
        detected_tech_stack=[],
        recommendations=[],
    )


__all__ = [
    "AiCommand",
    "LlmLexiconRecommendation",
    "LlmLexiconResult",
    "llm_recommend_lexicons",
]

"""Core elicitation engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re
from typing import Any

import yaml

from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.elicit.finding import ElicitResult, Finding
from codd.elicit.persistence import ElicitPersistence


DEFAULT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "elicit_prompt_L0.md"
DEFAULT_MAX_CONTEXT_CHARS = 24000


class ElicitEngine:
    """Build an elicitation prompt, invoke an AI command, and parse findings."""

    def __init__(
        self,
        ai_command: str | Callable[[str], str] | Any | None = None,
        *,
        template_path: Path | str | None = None,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ):
        self.ai_command = ai_command
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
        self.max_context_chars = max_context_chars

    def run(self, project_root: Path, lexicon_config: Any | None = None) -> ElicitResult:
        root = Path(project_root)
        project_scope, project_phase = _project_scope_phase(root)
        prompt = self.build_prompt(root, lexicon_config=lexicon_config)
        raw_output = self.invoke(prompt, root)
        result = self.deserialize_result(raw_output)
        result.findings = _apply_scope_phase(
            result.findings,
            lexicon_config=lexicon_config,
            scope=project_scope,
            phase=project_phase,
        )
        result.findings = ElicitPersistence(root).filter_known(result.findings)
        if not result.findings and result.lexicon_coverage_report:
            non_gap = all(
                str(status).lower() != "gap"
                for status in result.lexicon_coverage_report.values()
            )
            if non_gap:
                result.all_covered = True
        return result

    def build_prompt(self, project_root: Path, lexicon_config: Any | None = None) -> str:
        root = Path(project_root)
        template = self._template_text(lexicon_config)
        replacements = {
            "requirements_content": _collect_requirements(root, self.max_context_chars),
            "design_doc_content": _collect_design_docs(root, self.max_context_chars),
            "project_lexicon": _project_lexicon_text(root, lexicon_config, self.max_context_chars),
            "existing_axes": _existing_axes_text(root),
        }
        return _replace_placeholders(template, replacements)

    def invoke(self, prompt: str, project_root: Path) -> str:
        if callable(self.ai_command) and not hasattr(self.ai_command, "invoke"):
            return str(self.ai_command(prompt))
        if hasattr(self.ai_command, "invoke"):
            return str(self.ai_command.invoke(prompt))
        command = self.ai_command if isinstance(self.ai_command, str) else None
        return SubprocessAiCommand(command=command, project_root=project_root).invoke(prompt)

    def deserialize(self, raw_output: str) -> list[Finding]:
        payload = json.loads(_extract_json_array(raw_output))
        if not isinstance(payload, list):
            raise ValueError("Elicit output must be a JSON array")
        return [Finding.from_dict(item) for item in payload]

    def deserialize_result(self, raw_output: str) -> ElicitResult:
        payload_text = _extract_json_payload(raw_output)
        payload = json.loads(payload_text)
        return ElicitResult.from_payload(payload)

    def _template_text(self, lexicon_config: Any | None) -> str:
        extension = _string_attr(lexicon_config, "prompt_extension_content")
        if extension:
            return extension
        return self.template_path.read_text(encoding="utf-8")


def _collect_requirements(project_root: Path, max_chars: int) -> str:
    paths = _document_paths(
        project_root,
        explicit_names=("requirements.md", "REQUIREMENTS.md"),
        directory_names=("requirements",),
    )
    return _read_documents(paths, project_root, max_chars)


def _collect_design_docs(project_root: Path, max_chars: int) -> str:
    paths = _document_paths(
        project_root,
        explicit_names=("design.md", "DESIGN.md"),
        directory_names=("design", "architecture"),
    )
    return _read_documents(paths, project_root, max_chars)


def _document_paths(
    project_root: Path,
    *,
    explicit_names: tuple[str, ...],
    directory_names: tuple[str, ...],
) -> list[Path]:
    paths: list[Path] = []
    for name in explicit_names:
        candidate = project_root / name
        if candidate.is_file():
            paths.append(candidate)
    docs_dir = project_root / "docs"
    for directory_name in directory_names:
        directory = docs_dir / directory_name
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.md")))
    return _unique_paths(paths)


def _read_documents(paths: list[Path], project_root: Path, max_chars: int) -> str:
    if not paths:
        return "(none provided)"
    chunks: list[str] = []
    remaining = max_chars
    for path in paths:
        if remaining <= 0:
            break
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        rel_path = _relative_path(path, project_root)
        chunk = f"### {rel_path}\n{text}\n"
        chunks.append(chunk[:remaining])
        remaining -= len(chunks[-1])
    return "\n".join(chunks) if chunks else "(none provided)"


def _project_lexicon_text(project_root: Path, lexicon_config: Any | None, max_chars: int) -> str:
    chunks: list[str] = []
    for name in ("project_lexicon.yaml", "project_lexicon.yml"):
        path = project_root / name
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace").strip())
            break
    lexicon_name = _string_attr(lexicon_config, "lexicon_name")
    recommended = getattr(lexicon_config, "recommended_kinds", None)
    if lexicon_name:
        chunks.append(f"loaded_lexicon: {lexicon_name}")
    if isinstance(recommended, list) and recommended:
        chunks.append(yaml.safe_dump({"recommended_kinds": recommended}, sort_keys=False).strip())
    text = "\n\n".join(chunk for chunk in chunks if chunk)
    return text[:max_chars] if text else "(none provided)"


def _existing_axes_text(project_root: Path) -> str:
    config = _load_optional_codd_config(project_root)
    values: dict[str, Any] = {}
    for key in ("coverage_axes", "axes"):
        if key in config:
            values[key] = config[key]
    coverage = config.get("coverage")
    if isinstance(coverage, Mapping):
        for key in ("axes", "required_axes"):
            if key in coverage:
                values[f"coverage.{key}"] = coverage[key]
    if not values:
        return "(none provided)"
    return yaml.safe_dump(values, sort_keys=False, allow_unicode=True).strip()


def _project_scope_phase(project_root: Path) -> tuple[str, str]:
    for name in ("project_lexicon.yaml", "project_lexicon.yml"):
        path = project_root / name
        if not path.is_file():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, Mapping):
            return "full", "production"
        return (
            str(payload.get("scope") or "full"),
            str(payload.get("phase") or "production"),
        )
    return "full", "production"


def _apply_scope_phase(
    findings: list[Finding],
    *,
    lexicon_config: Any | None,
    scope: str,
    phase: str,
) -> list[Finding]:
    axis_concerns = _axis_concern_map(lexicon_config)
    severity_rules = _mapping_attr(lexicon_config, "severity_rules")
    filtered: list[Finding] = []
    for finding in findings:
        axis = _finding_axis(finding)
        concern = axis_concerns.get(axis) if axis is not None else None
        if not _scope_allows_concern(scope, concern):
            continue
        _apply_phase_severity(
            finding,
            axis=axis,
            concern=concern,
            phase=phase,
            scope=scope,
            severity_rules=severity_rules,
        )
        filtered.append(finding)
    return filtered


def _axis_concern_map(lexicon_config: Any | None) -> dict[str, str]:
    concerns: dict[str, str] = {}
    for axis in _list_attr(lexicon_config, "coverage_axes"):
        if not isinstance(axis, Mapping):
            continue
        axis_type = axis.get("axis_type")
        concern = axis.get("concern")
        if isinstance(axis_type, str) and isinstance(concern, str):
            concerns[axis_type] = concern.strip().lower()
    return concerns


def _finding_axis(finding: Finding) -> str | None:
    for key in ("dimension", "axis", "axis_type"):
        value = finding.details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _scope_allows_concern(scope: str, concern: str | None) -> bool:
    if concern is None:
        return True
    scope_value = scope.strip().lower()
    concern_value = concern.strip().lower()
    if scope_value == "system_implementation":
        return concern_value in {"system", "both"}
    if scope_value == "business_only":
        return concern_value in {"business", "both"}
    return True


def _apply_phase_severity(
    finding: Finding,
    *,
    axis: str | None,
    concern: str | None,
    phase: str,
    scope: str,
    severity_rules: Mapping[str, Any],
) -> None:
    phase_value = phase.strip().lower()
    concern_value = (concern or "").strip().lower()
    context = {
        "axis": axis or "",
        "dimension": axis or "",
        "concern": concern_value,
        "phase": phase_value,
        "scope": scope.strip().lower(),
        "severity": finding.severity,
    }
    if _apply_matching_severity_rule(finding, context, severity_rules):
        return
    if phase_value == "mvp" and concern_value == "business" and finding.severity != "info":
        finding.severity = "info"


def _apply_matching_severity_rule(
    finding: Finding,
    context: Mapping[str, str],
    severity_rules: Mapping[str, Any],
) -> bool:
    rules = severity_rules.get("rules", [])
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        when = rule.get("when")
        severity = rule.get("severity")
        if not isinstance(when, str) or not isinstance(severity, str):
            continue
        if _condition_matches(when, context):
            _set_severity(finding, severity)
            return True
    return False


def _condition_matches(condition: str, context: Mapping[str, str]) -> bool:
    parts = [
        part.strip()
        for part in re.split(r"\bAND\b", condition, flags=re.IGNORECASE)
        if part.strip()
    ]
    if not parts:
        return False
    for part in parts:
        if "=" not in part:
            return False
        key_text, expected_text = part.split("=", 1)
        key = key_text.strip().lower()
        expected = expected_text.strip().lower()
        if context.get(key, "").lower() != expected:
            return False
    return True


def _set_severity(finding: Finding, severity: str) -> None:
    cleaned = severity.strip().lower()
    if cleaned in {"critical", "high", "medium", "info"}:
        finding.severity = cleaned  # type: ignore[assignment]


def _load_optional_codd_config(project_root: Path) -> dict[str, Any]:
    for dirname in ("codd", ".codd"):
        path = project_root / dirname / "codd.yaml"
        if not path.is_file():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _replace_placeholders(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _extract_json_array(raw_output: str) -> str:
    text = raw_output.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
    if text.startswith("[") and text.endswith("]"):
        return text
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Elicit output did not contain a JSON array")
    return text[start : end + 1]


def _extract_json_payload(raw_output: str) -> str:
    text = raw_output.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
    if text.startswith("[") and text.endswith("]"):
        return text
    if text.startswith("{") and text.endswith("}"):
        return text
    obj_start = text.find("{")
    arr_start = text.find("[")
    if obj_start != -1 and (arr_start == -1 or obj_start < arr_start):
        obj_end = text.rfind("}")
        if obj_end != -1 and obj_end > obj_start:
            return text[obj_start : obj_end + 1]
    if arr_start != -1:
        arr_end = text.rfind("]")
        if arr_end != -1 and arr_end > arr_start:
            return text[arr_start : arr_end + 1]
    raise ValueError("Elicit output did not contain a JSON array or object")


def _string_attr(value: Any, name: str) -> str | None:
    candidate = getattr(value, name, None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    if isinstance(value, Mapping):
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _mapping_attr(value: Any, name: str) -> Mapping[str, Any]:
    candidate = getattr(value, name, None)
    if isinstance(candidate, Mapping):
        return candidate
    if isinstance(value, Mapping):
        candidate = value.get(name)
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _list_attr(value: Any, name: str) -> list[Any]:
    candidate = getattr(value, name, None)
    if isinstance(candidate, list):
        return candidate
    if isinstance(value, Mapping):
        candidate = value.get(name)
        if isinstance(candidate, list):
            return candidate
    return []


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["DEFAULT_TEMPLATE_PATH", "ElicitEngine", "ElicitResult"]

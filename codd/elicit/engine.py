"""Core elicitation engine."""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.elicit.finding import ElicitResult, Finding, FindingDimension, FindingType
from codd.elicit.persistence import ElicitPersistence


DEFAULT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "elicit_prompt_L0.md"
DEFAULT_MAX_CONTEXT_CHARS = 24000

# Sentinel commands that bypass AI invocation and return an empty,
# well-formed elicit payload. Useful for AI-free integration tests and for
# structural-findings-only runs in CI where AI cost matters.
_MOCK_AI_COMMAND_SENTINELS = frozenset({"true", ":", "none", "mock"})
_MOCK_AI_OUTPUT = '{"findings": [], "lexicon_coverage_report": {}}'


def _is_mock_ai_command(ai_command: Any) -> bool:
    if not isinstance(ai_command, str):
        return False
    stripped = ai_command.strip().lower()
    return stripped in _MOCK_AI_COMMAND_SENTINELS


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
        lexicon_configs = _as_lexicon_configs(lexicon_config)
        if len(lexicon_configs) > 1:
            return self._run_many(root, lexicon_configs, scope=project_scope, phase=project_phase)
        config = lexicon_configs[0] if lexicon_configs else None
        return self._run_one(root, config, scope=project_scope, phase=project_phase)

    def _run_one(
        self,
        root: Path,
        lexicon_config: Any | None,
        *,
        scope: str,
        phase: str,
    ) -> ElicitResult:
        prompt = self.build_prompt(root, lexicon_config=lexicon_config)
        raw_output = self.invoke(prompt, root)
        result = self.deserialize_result(raw_output)
        result.findings = _apply_scope_phase(
            result.findings,
            lexicon_config=lexicon_config,
            scope=scope,
            phase=phase,
        )
        if _lexicon_has_actor_axis(lexicon_config):
            actors = _actor_names_from_result(result)
            actors.extend(_design_doc_actor_names(root))
            result.findings.extend(
                self._check_journey_coverage(
                    _dedupe_strings(actors),
                    _design_doc_journey_map(root),
                )
            )
        _attach_lexicon_source(result.findings, _string_attr(lexicon_config, "lexicon_name"))
        result.findings = ElicitPersistence(root).filter_known(result.findings)
        if not result.findings and result.lexicon_coverage_report:
            non_gap = all(
                str(status).lower() != "gap"
                for status in result.lexicon_coverage_report.values()
            )
            if non_gap:
                result.all_covered = True
        return result

    def _run_many(
        self,
        root: Path,
        lexicon_configs: list[Any],
        *,
        scope: str,
        phase: str,
    ) -> ElicitResult:
        prepared = _prepare_lexicon_configs(lexicon_configs)
        combined = ElicitResult()
        for config, duplicate_axes in prepared:
            result = self._run_one(root, config, scope=scope, phase=phase)
            if duplicate_axes:
                result.findings = [
                    finding
                    for finding in result.findings
                    if _finding_axis(finding) not in duplicate_axes
                ]
            combined.findings.extend(result.findings)
            for axis, status in result.lexicon_coverage_report.items():
                if axis in combined.lexicon_coverage_report:
                    warnings.warn(
                        f"duplicate lexicon dimension '{axis}' ignored; first lexicon wins",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                combined.lexicon_coverage_report[axis] = status

        if combined.lexicon_coverage_report and not combined.findings:
            combined.all_covered = all(
                str(status).lower() != "gap"
                for status in combined.lexicon_coverage_report.values()
            )
        return combined

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
        if _is_mock_ai_command(self.ai_command):
            return _MOCK_AI_OUTPUT
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
        configs = _as_lexicon_configs(lexicon_config)
        if configs:
            lexicon_config = configs[0]
        extension = _string_attr(lexicon_config, "prompt_extension_content")
        if extension:
            return extension
        return self.template_path.read_text(encoding="utf-8")

    def _check_journey_coverage(
        self,
        actors: list[str],
        journey_map: Mapping[str, Any],
    ) -> list[Finding]:
        findings: list[Finding] = []
        journeys = list(_iter_journey_values(journey_map))
        for actor in actors:
            if any(_journey_references_actor(journey, actor) for journey in journeys):
                continue
            message = f"Actor '{actor}' identified in requirements but no user_journey declared for this actor."
            findings.append(
                Finding(
                    id=f"{FindingType.MISSING_JOURNEY_FOR_ACTOR.value}:{_slug(actor)}",
                    kind=FindingType.MISSING_JOURNEY_FOR_ACTOR.value,
                    severity="amber",
                    name="Missing user journey for actor",
                    question=f"What user_journey should cover actor '{actor}'?",
                    details={
                        "dimension": FindingDimension.PROCESS_USER_JOURNEY.value,
                        "actor": actor,
                        "message": message,
                    },
                    rationale=message,
                )
            )
        return findings


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


def _iter_design_doc_frontmatters(project_root: Path) -> list[dict[str, Any]]:
    frontmatters: list[dict[str, Any]] = []
    for path in _document_paths(
        project_root,
        explicit_names=("design.md", "DESIGN.md"),
        directory_names=("design", "architecture"),
    ):
        frontmatter = _markdown_frontmatter(path)
        if frontmatter:
            frontmatters.append(frontmatter)
    return frontmatters


def _markdown_frontmatter(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) != 3:
        return {}
    payload = yaml.safe_load(parts[1]) or {}
    return payload if isinstance(payload, dict) else {}


def _design_doc_actor_names(project_root: Path) -> list[str]:
    actors: list[str] = []
    for frontmatter in _iter_design_doc_frontmatters(project_root):
        actors.extend(_actor_names_from_mapping(frontmatter))
    return _dedupe_strings(actors)


def _design_doc_journey_map(project_root: Path) -> dict[str, dict[str, Any]]:
    journeys: dict[str, dict[str, Any]] = {}
    for doc_index, frontmatter in enumerate(_iter_design_doc_frontmatters(project_root)):
        entries = frontmatter.get("user_journeys", [])
        if not isinstance(entries, list):
            continue
        for journey_index, entry in enumerate(entries):
            if isinstance(entry, dict):
                key = str(entry.get("name") or f"doc_{doc_index}_journey_{journey_index}")
                journeys[key] = entry
    return journeys


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


def _as_lexicon_configs(lexicon_config: Any | None) -> list[Any]:
    if lexicon_config is None:
        return []
    if isinstance(lexicon_config, (list, tuple)):
        return [item for item in lexicon_config if item is not None]
    return [lexicon_config]


def _lexicon_has_actor_axis(lexicon_config: Any | None) -> bool:
    for axis in _list_attr(lexicon_config, "coverage_axes"):
        if not isinstance(axis, Mapping):
            continue
        text = " ".join(_nested_strings(axis)).lower()
        if any(token in text for token in ("actor", "stakeholder", "role")):
            return True
    extension = _string_attr(lexicon_config, "prompt_extension_content") or ""
    return any(token in extension.lower() for token in ("actor", "stakeholder", "role"))


def _actor_names_from_result(result: ElicitResult) -> list[str]:
    actors: list[str] = []
    actors.extend(_actor_names_from_mapping(result.metadata, actor_dimension=True))
    for finding in result.findings:
        details = finding.details if isinstance(finding.details, dict) else {}
        actor_dimension = _is_actor_dimension(details)
        actors.extend(_actor_names_from_mapping(details, actor_dimension=actor_dimension))
    return _dedupe_strings(actors)


def _actor_names_from_mapping(mapping: Mapping[str, Any], *, actor_dimension: bool = False) -> list[str]:
    keys = {
        "actor",
        "actors",
        "role",
        "roles",
        "stakeholder",
        "stakeholders",
        "stakeholder_roles",
    }
    if actor_dimension:
        keys.update({"value", "values", "item", "items", "candidate", "candidates", "name", "names"})

    actors: list[str] = []
    for key, value in mapping.items():
        if str(key).strip().lower() in keys:
            actors.extend(_actor_names_from_value(value))
    return _dedupe_strings(actors)


def _actor_names_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [actor for item in re.split(r"[,;\n]+", value) if (actor := _clean_actor_name(item))]
    if isinstance(value, Mapping):
        for key in ("name", "id", "label", "role", "actor", "stakeholder"):
            if key in value:
                return _actor_names_from_value(value[key])
        return []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        actors: list[str] = []
        for item in value:
            actors.extend(_actor_names_from_value(item))
        return _dedupe_strings(actors)
    actor = _clean_actor_name(str(value))
    return [actor] if actor else []


def _clean_actor_name(value: str) -> str | None:
    text = value.strip().strip("'\"`")
    if not text or len(text) > 80:
        return None
    lowered = text.lower()
    if lowered in {
        "actor",
        "actors",
        "role",
        "roles",
        "stakeholder",
        "stakeholders",
        "covered",
        "implicit",
        "gap",
    }:
        return None
    return text


def _is_actor_dimension(details: Mapping[str, Any]) -> bool:
    for key in ("dimension", "axis", "axis_type"):
        value = details.get(key)
        if isinstance(value, str) and any(token in value.lower() for token in ("actor", "stakeholder", "role")):
            return True
    return False


def _iter_journey_values(journey_map: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for value in journey_map.values():
        if isinstance(value, dict):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item


def _journey_references_actor(journey: Mapping[str, Any], actor: str) -> bool:
    actor_key = _normalize_actor(actor)
    if not actor_key:
        return False
    journey_actors = _actor_names_from_mapping(journey)
    return actor_key in {_normalize_actor(item) for item in journey_actors}


def _normalize_actor(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "actor"


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _nested_strings(value: Any) -> set[str]:
    strings: set[str] = set()
    if value is None:
        return strings
    if isinstance(value, str):
        strings.add(value)
        return strings
    if isinstance(value, Mapping):
        for key, item in value.items():
            strings.update(_nested_strings(key))
            strings.update(_nested_strings(item))
        return strings
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            strings.update(_nested_strings(item))
        return strings
    strings.add(str(value))
    return strings


def _prepare_lexicon_configs(lexicon_configs: list[Any]) -> list[tuple[Any, set[str]]]:
    prepared: list[tuple[Any, set[str]]] = []
    seen_axes: set[str] = set()
    for config in lexicon_configs:
        kept_axes: list[dict[str, Any]] = []
        duplicate_axes: set[str] = set()
        for axis in _list_attr(config, "coverage_axes"):
            if not isinstance(axis, Mapping):
                continue
            axis_type = axis.get("axis_type")
            if not isinstance(axis_type, str) or not axis_type.strip():
                kept_axes.append(dict(axis))
                continue
            normalized_axis = axis_type.strip()
            if normalized_axis in seen_axes:
                duplicate_axes.add(normalized_axis)
                warnings.warn(
                    f"duplicate lexicon dimension '{normalized_axis}' ignored; first lexicon wins",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            seen_axes.add(normalized_axis)
            kept_axes.append(dict(axis))
        prepared.append((_with_coverage_axes(config, kept_axes), duplicate_axes))
    return prepared


def _with_coverage_axes(config: Any, coverage_axes: list[dict[str, Any]]) -> Any:
    if isinstance(config, Mapping):
        updated = dict(config)
        updated["coverage_axes"] = coverage_axes
        return updated
    try:
        return replace(config, coverage_axes=coverage_axes)
    except TypeError:
        return config


def _attach_lexicon_source(findings: list[Finding], lexicon_name: str | None) -> None:
    if not lexicon_name:
        return
    for finding in findings:
        finding.details["lexicon_source"] = lexicon_name


def _project_lexicon_text(project_root: Path, lexicon_config: Any | None, max_chars: int) -> str:
    chunks: list[str] = []
    for name in ("project_lexicon.yaml", "project_lexicon.yml"):
        path = project_root / name
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace").strip())
            break
    for config in _as_lexicon_configs(lexicon_config):
        lexicon_name = _string_attr(config, "lexicon_name")
        recommended = getattr(config, "recommended_kinds", None)
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
    # cmd_455: default scope = system_implementation matches CoDD's design
    # focus. Projects that want every dimension (including business goals,
    # UAT detail, risk register) opt in via `scope: full` in project_lexicon.yaml.
    from codd.lexicon import DEFAULT_PHASE, DEFAULT_SCOPE

    for name in ("project_lexicon.yaml", "project_lexicon.yml"):
        path = project_root / name
        if not path.is_file():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, Mapping):
            return DEFAULT_SCOPE, DEFAULT_PHASE
        return (
            str(payload.get("scope") or DEFAULT_SCOPE),
            str(payload.get("phase") or DEFAULT_PHASE),
        )
    return DEFAULT_SCOPE, DEFAULT_PHASE


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
    if cleaned in {"critical", "high", "medium", "amber", "info"}:
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

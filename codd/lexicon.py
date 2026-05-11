"""project_lexicon.yaml loader and validator."""

from __future__ import annotations

import warnings
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


SCHEMA_PATH = Path(__file__).parent / "templates" / "lexicon_schema.yaml"
LEXICON_FILENAME = "project_lexicon.yaml"
REQUIRED_ARTIFACT_SOURCES = {"ai_derived", "user_override", "default_template"}
# cmd_455: CoDD's responsibility is system-implementation coherence. Business
# concerns (goal/KPI, acceptance/UAT detail, risk register) belong upstream of
# the DAG and should not surface as findings unless the project opts in
# explicitly with `scope: full` or `scope: business_only`.
DEFAULT_SCOPE = "system_implementation"
DEFAULT_PHASE = "production"
LEGACY_SUGGESTED_LEXICONS_WARNING = (
    "suggested_lexicons is deprecated, renamed to extends; auto-merged"
)


@dataclass
class AskOption:
    id: str
    label: str
    description: str = ""
    cost_effort: Literal["low", "medium", "high"] = "medium"
    pros: str = ""
    cons: str = ""
    recommended: bool = False
    recommendation_rationale: str = ""
    type: str = "option"


@dataclass
class AskItem:
    id: str
    question: str
    type: Literal["select", "multiselect", "free_text"] = "select"
    options: list[AskOption] = field(default_factory=list)
    blocking: bool = False
    status: Literal["ASK", "RECOMMENDED_PROCEEDING", "CONFIRMED", "OVERRIDDEN"] = "ASK"
    recommended_id: str | None = None
    proceeded_with: str | None = None
    answer: str | None = None
    asked_at: str = ""
    answered_at: str = ""


class LexiconError(ValueError):
    """Raised when a project lexicon is malformed."""


class ProjectLexicon:
    """Validated project lexicon data with prompt-friendly accessors."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @property
    def node_vocabulary(self) -> list[dict[str, Any]]:
        return self._data.get("node_vocabulary", [])

    @property
    def naming_conventions(self) -> dict[str, str]:
        return {c["id"]: c["regex"] for c in self._data.get("naming_conventions", [])}

    @property
    def design_principles(self) -> list[str]:
        return self._data.get("design_principles", [])

    @property
    def provenance(self) -> str:
        return self._data.get("provenance", "human")

    @property
    def confidence(self) -> float:
        return float(self._data.get("confidence", 1.0))

    @property
    def failure_modes(self) -> list[dict[str, Any]]:
        return self._data.get("failure_modes", [])

    @property
    def extractor_registry(self) -> dict[str, dict[str, Any]]:
        return self._data.get("extractor_registry", {})

    @property
    def coverage_decisions(self) -> list[AskItem]:
        return [
            ask_item_from_dict(item)
            for item in self._data.get("coverage_decisions", [])
        ]

    @property
    def extends(self) -> list[str]:
        return lexicon_ids_from_entries(self._data.get("extends", []))

    @property
    def required_artifacts(self) -> list[dict[str, Any]]:
        return deepcopy(self._data.get("required_artifacts", []))

    @property
    def scope(self) -> str:
        return str(self._data.get("scope") or DEFAULT_SCOPE)

    @property
    def phase(self) -> str:
        return str(self._data.get("phase") or DEFAULT_PHASE)

    def set_coverage_decisions(self, decisions: list[AskItem]) -> None:
        self._data["coverage_decisions"] = [
            ask_item_to_dict(item)
            for item in decisions
        ]

    def set_required_artifacts(self, artifacts: list[dict[str, Any]]) -> None:
        self._data["required_artifacts"] = deepcopy(artifacts)

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def get_vocabulary_item(self, node_id: str) -> dict[str, Any] | None:
        for item in self.node_vocabulary:
            if item.get("id") == node_id:
                return item
        return None

    def as_context_string(self) -> str:
        """Return a human-readable summary for AI prompt injection."""
        lines = ["## Project Lexicon", ""]
        lines.append("### Node Vocabulary")
        for item in self.node_vocabulary:
            confidence = float(item.get("confidence", 1.0))
            provenance = item.get("provenance", "human")
            warning = " ⚠️ (confidence: low, requires confirmation)" if confidence < 0.6 else ""
            lines.append(f"- **{item['id']}**: {item['description']}{warning}")
            if "naming_convention" in item:
                lines.append(f"  - naming: {item['naming_convention']}")
            if provenance and provenance != "human":
                source = f"  - source: {provenance}"
                if item.get("fetched_at"):
                    source += f" ({item['fetched_at']})"
                lines.append(source)
            if "prefix_rules" in item:
                for rule in item.get("prefix_rules", []):
                    lines.append(f"  - prefix for {rule.get('role', '?')}: {rule.get('prefix', '?')}")
        lines.append("")
        lines.append("### Design Principles")
        for principle in self.design_principles:
            lines.append(f"- {principle}")
        return "\n".join(lines)


def load_lexicon(project_root: str | Path) -> ProjectLexicon | None:
    """Load project_lexicon.yaml from project root. Returns None if not found."""
    path = Path(project_root) / LEXICON_FILENAME
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LexiconError("project_lexicon.yaml must contain a YAML mapping")
    data = normalize_project_lexicon_data(data)
    validate_lexicon(data)
    return ProjectLexicon(data)


def load_project_extends(project_root: str | Path) -> list[str]:
    """Load project-level lexicon IDs from extends, with legacy shim support."""
    path = Path(project_root) / LEXICON_FILENAME
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise LexiconError("project_lexicon.yaml must contain a YAML mapping")
    normalized = normalize_project_lexicon_data(data)
    return lexicon_ids_from_entries(normalized.get("extends", []))


def normalize_project_lexicon_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return lexicon data with legacy suggested_lexicons merged into extends.

    The legacy field remains in the returned mapping for backward compatibility;
    only the canonical runtime list is merged into ``extends``.
    """
    normalized = deepcopy(data)
    current = _optional_lexicon_entries(normalized, "extends")
    if "suggested_lexicons" not in normalized:
        return normalized

    legacy = _optional_lexicon_entries(normalized, "suggested_lexicons")
    warnings.warn(
        LEGACY_SUGGESTED_LEXICONS_WARNING,
        DeprecationWarning,
        stacklevel=2,
    )
    normalized["extends"] = merge_lexicon_entries(current, legacy)
    return normalized


def merge_lexicon_entries(primary: list[Any], secondary: list[Any]) -> list[Any]:
    merged = list(primary)
    known = set(lexicon_ids_from_entries(merged))
    for item in secondary:
        lexicon_id = lexicon_id_from_entry(item)
        if not lexicon_id or lexicon_id in known:
            continue
        merged.append(item)
        known.add(lexicon_id)
    return merged


def lexicon_ids_from_entries(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    seen: set[str] = set()
    ids: list[str] = []
    for item in entries:
        lexicon_id = lexicon_id_from_entry(item)
        if not lexicon_id or lexicon_id in seen:
            continue
        seen.add(lexicon_id)
        ids.append(lexicon_id)
    return ids


def lexicon_id_from_entry(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("id", "name", "lexicon_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item).strip()


def ask_option_from_dict(data: dict[str, Any]) -> AskOption:
    """Build an AskOption from YAML-safe dict data."""
    if not isinstance(data, dict):
        raise LexiconError(f"coverage_decisions option must be a mapping: {data}")
    return AskOption(
        id=str(data.get("id", "")),
        label=str(data.get("label", "")),
        description=str(data.get("description", "")),
        cost_effort=_literal_or_default(
            data.get("cost_effort"),
            {"low", "medium", "high"},
            "medium",
        ),
        pros=str(data.get("pros", "")),
        cons=str(data.get("cons", "")),
        recommended=bool(data.get("recommended", False)),
        recommendation_rationale=str(data.get("recommendation_rationale", "")),
        type=str(data.get("type", "option")),
    )


def ask_item_from_dict(data: dict[str, Any]) -> AskItem:
    """Build an AskItem from YAML-safe dict data."""
    if not isinstance(data, dict):
        raise LexiconError(f"coverage_decisions item must be a mapping: {data}")
    return AskItem(
        id=str(data.get("id", "")),
        question=str(data.get("question", "")),
        type=_literal_or_default(
            data.get("type"),
            {"select", "multiselect", "free_text"},
            "select",
        ),
        options=[
            ask_option_from_dict(option)
            for option in data.get("options", [])
        ],
        blocking=bool(data.get("blocking", False)),
        status=_literal_or_default(
            data.get("status"),
            {"ASK", "RECOMMENDED_PROCEEDING", "CONFIRMED", "OVERRIDDEN"},
            "ASK",
        ),
        recommended_id=_optional_str(data.get("recommended_id")),
        proceeded_with=_optional_str(data.get("proceeded_with")),
        answer=_optional_str(data.get("answer")),
        asked_at=str(data.get("asked_at", "")),
        answered_at=str(data.get("answered_at", "")),
    )


def ask_option_to_dict(option: AskOption) -> dict[str, Any]:
    return asdict(option)


def ask_item_to_dict(item: AskItem) -> dict[str, Any]:
    return {
        **asdict(item),
        "options": [ask_option_to_dict(option) for option in item.options],
    }


def validate_lexicon(data: dict[str, Any]) -> None:
    """Validate lexicon dict against required schema. Raises LexiconError on failure."""
    if not isinstance(data, dict):
        raise LexiconError("project_lexicon.yaml must contain a YAML mapping")

    schema = _load_schema()
    for section in schema.get("required_sections", []):
        if section not in data:
            raise LexiconError(f"Missing required section: '{section}'")

    vocab = data.get("node_vocabulary", [])
    _validate_list_of_mappings(vocab, "node_vocabulary")
    for item in vocab:
        for field in schema["node_vocabulary_item"].get("required_fields", []):
            if field not in item:
                raise LexiconError(f"node_vocabulary item missing required field '{field}': {item}")
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError) as exc:
                raise LexiconError(
                    f"confidence must be numeric, got '{confidence}' for '{item.get('id')}'"
                ) from exc
            if not (0.0 <= confidence_value <= 1.0):
                raise LexiconError(
                    f"confidence must be 0.0-1.0, got {confidence_value} for '{item.get('id')}'"
                )

    conventions = data.get("naming_conventions", [])
    _validate_list_of_mappings(conventions, "naming_conventions")
    for convention in conventions:
        for field in schema["naming_convention_item"].get("required_fields", []):
            if field not in convention:
                raise LexiconError(f"naming_convention item missing required field '{field}': {convention}")

    design_principles = data.get("design_principles", [])
    if not isinstance(design_principles, list):
        raise LexiconError("design_principles must be a list")

    for field_name in ("extends", "suggested_lexicons"):
        if field_name in data and not isinstance(data[field_name], list):
            raise LexiconError(f"{field_name} must be a list")

    failure_modes = data.get("failure_modes", [])
    _validate_list_of_mappings(failure_modes, "failure_modes")

    registry = data.get("extractor_registry", {})
    if not isinstance(registry, dict):
        raise LexiconError("extractor_registry must be a mapping")
    for extractor_id, extractor in registry.items():
        if not isinstance(extractor, dict):
            raise LexiconError(f"extractor_registry item must be a mapping: {extractor_id}")
        for field in schema["extractor_registry_item"].get("required_fields", []):
            if field not in extractor:
                raise LexiconError(
                    f"extractor_registry item '{extractor_id}' missing required field '{field}'"
                )

    decisions = data.get("coverage_decisions", [])
    _validate_list_of_mappings(decisions, "coverage_decisions")
    for decision in decisions:
        for field_name in ("id", "question"):
            if field_name not in decision:
                raise LexiconError(
                    f"coverage_decisions item missing required field '{field_name}': {decision}"
                )
        _validate_list_of_mappings(decision.get("options", []), "coverage_decisions.options")

    required_artifacts = data.get("required_artifacts", [])
    _validate_list_of_mappings(required_artifacts, "required_artifacts")
    artifact_errors: list[str] = []
    for index, artifact in enumerate(required_artifacts):
        artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
        label = f"required_artifacts[{index}]" + (
            f" (id={artifact_id!r})" if artifact_id else ""
        )
        for field_name in ("id", "title", "scope", "source"):
            if field_name not in artifact:
                artifact_errors.append(
                    f"{label} missing required field '{field_name}'"
                )
        if "source" in artifact and artifact["source"] not in REQUIRED_ARTIFACT_SOURCES:
            artifact_errors.append(
                f"{label} source must be one of "
                f"{sorted(REQUIRED_ARTIFACT_SOURCES)}, got {artifact['source']!r}"
            )
        depends_on = artifact.get("depends_on", [])
        if not isinstance(depends_on, list):
            artifact_errors.append(f"{label} depends_on must be a list")
        derived_from = artifact.get("derived_from", [])
        if derived_from is not None and not isinstance(derived_from, list):
            artifact_errors.append(f"{label} derived_from must be a list")
    if artifact_errors:
        if len(artifact_errors) == 1:
            raise LexiconError(artifact_errors[0])
        joined = "\n  - ".join(artifact_errors)
        raise LexiconError(
            f"required_artifacts has {len(artifact_errors)} validation issue(s):\n  - {joined}"
        )

    for field_name in ("scope", "phase"):
        if field_name in data and not isinstance(data[field_name], str):
            raise LexiconError(f"{field_name} must be a string")


def _load_schema() -> dict[str, Any]:
    data = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LexiconError("lexicon_schema.yaml must contain a YAML mapping")
    return data


def _validate_list_of_mappings(value: Any, name: str) -> None:
    if not isinstance(value, list):
        raise LexiconError(f"{name} must be a list")
    for item in value:
        if not isinstance(item, dict):
            raise LexiconError(f"{name} items must be mappings: {item}")


def _optional_lexicon_entries(data: dict[str, Any], field_name: str) -> list[Any]:
    value = data.get(field_name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise LexiconError(f"{field_name} must be a list")
    return list(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _literal_or_default(value: Any, allowed: set[str], default: str) -> Any:
    text = str(value) if value is not None else default
    return text if text in allowed else default

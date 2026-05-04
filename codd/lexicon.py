"""project_lexicon.yaml loader and validator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


SCHEMA_PATH = Path(__file__).parent / "templates" / "lexicon_schema.yaml"
LEXICON_FILENAME = "project_lexicon.yaml"


class LexiconError(Exception):
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
    validate_lexicon(data)
    return ProjectLexicon(data)


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

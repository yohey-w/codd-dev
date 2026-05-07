"""Load elicitation lexicon plug-ins from YAML manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class LexiconLoadError(Exception):
    """Raised when an elicitation lexicon manifest is malformed."""


@dataclass(frozen=True)
class LexiconConfig:
    lexicon_name: str
    prompt_extension_content: str
    recommended_kinds: list[str]


def load_lexicon(lexicon_path: Path) -> LexiconConfig:
    """Load a lexicon directory or manifest file into a prompt-ready config."""
    manifest_path = _find_manifest(Path(lexicon_path))
    manifest_dir = manifest_path.parent
    manifest = _load_yaml_mapping(manifest_path, "manifest")

    lexicon_name = _required_str(manifest, "lexicon_name", manifest_path)
    prompt_extension_path = _resolve_existing_path(
        manifest_dir,
        _required_str(manifest, "prompt_extension", manifest_path),
        "prompt_extension",
    )
    recommended_kinds_path = _resolve_existing_path(
        manifest_dir,
        _required_str(manifest, "recommended_kinds", manifest_path),
        "recommended_kinds",
    )

    prompt_extension_text = prompt_extension_path.read_text(encoding="utf-8")
    metadata, extension_body = _split_frontmatter(prompt_extension_text)
    extends_value = _optional_str(manifest.get("extends")) or _optional_str(
        metadata.get("extends")
    )
    prompt_content = extension_body
    if extends_value:
        base_prompt_path = _resolve_existing_path(
            manifest_dir,
            extends_value,
            "extends",
        )
        base_prompt = base_prompt_path.read_text(encoding="utf-8")
        prompt_content = f"{base_prompt.rstrip()}\n\n{extension_body.lstrip()}"

    recommended_kinds = _load_recommended_kinds(recommended_kinds_path)
    return LexiconConfig(
        lexicon_name=lexicon_name,
        prompt_extension_content=prompt_content,
        recommended_kinds=recommended_kinds,
    )


def _find_manifest(path: Path) -> Path:
    if path.is_dir():
        manifest_path = path / "manifest.yaml"
    else:
        manifest_path = path
    if not manifest_path.exists():
        raise LexiconLoadError(f"lexicon manifest not found: {manifest_path}")
    if not manifest_path.is_file():
        raise LexiconLoadError(f"lexicon manifest is not a file: {manifest_path}")
    return manifest_path


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LexiconLoadError(f"{label} YAML is invalid: {path}") from exc
    if not isinstance(data, dict):
        raise LexiconLoadError(f"{label} YAML must contain a mapping: {path}")
    return data


def _required_str(data: dict[str, Any], key: str, path: Path) -> str:
    value = _optional_str(data.get(key))
    if value is None:
        raise LexiconLoadError(f"manifest missing required string field '{key}': {path}")
    return value


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_existing_path(base_dir: Path, declared_path: str, field_name: str) -> Path:
    path = Path(declared_path)
    candidates: list[Path]
    if path.is_absolute():
        candidates = [path]
    else:
        candidates = []
        for root in (base_dir, *base_dir.parents, Path.cwd()):
            candidate = root / path
            if candidate not in candidates:
                candidates.append(candidate)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise LexiconLoadError(
        f"{field_name} path not found: {declared_path} (searched: {searched})"
    )


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            metadata_text = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            try:
                metadata = yaml.safe_load(metadata_text) or {}
            except yaml.YAMLError as exc:
                raise LexiconLoadError("prompt extension frontmatter is invalid") from exc
            if not isinstance(metadata, dict):
                raise LexiconLoadError("prompt extension frontmatter must be a mapping")
            return metadata, body

    raise LexiconLoadError("prompt extension frontmatter is missing a closing delimiter")


def _load_recommended_kinds(path: Path) -> list[str]:
    data = _load_yaml_mapping(path, "recommended_kinds")
    raw_kinds = data.get("recommended_kinds")
    if not isinstance(raw_kinds, list) or not raw_kinds:
        raise LexiconLoadError("recommended_kinds must be a non-empty list")

    kinds: list[str] = []
    for item in raw_kinds:
        kind = _optional_str(item)
        if kind is None:
            raise LexiconLoadError("recommended_kinds entries must be non-empty strings")
        if kind in kinds:
            raise LexiconLoadError(f"recommended_kinds contains duplicate entry: {kind}")
        kinds.append(kind)
    return kinds

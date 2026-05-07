"""Core elicitation engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re
from typing import Any

import yaml

from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.elicit.finding import Finding
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

    def run(self, project_root: Path, lexicon_config: Any | None = None) -> list[Finding]:
        root = Path(project_root)
        prompt = self.build_prompt(root, lexicon_config=lexicon_config)
        raw_output = self.invoke(prompt, root)
        findings = self.deserialize(raw_output)
        return ElicitPersistence(root).filter_known(findings)

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


def _string_attr(value: Any, name: str) -> str | None:
    candidate = getattr(value, name, None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    if isinstance(value, Mapping):
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


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


__all__ = ["DEFAULT_TEMPLATE_PATH", "ElicitEngine"]

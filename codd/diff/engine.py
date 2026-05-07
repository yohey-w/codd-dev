"""Core engine for comparing extracted project facts with requirements."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from pathlib import Path
import re
from typing import Any

import yaml

from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.elicit.finding import Finding


DEFAULT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "diff_prompt.md"
DEFAULT_MAX_CONTEXT_CHARS = 24000


class DiffEngine:
    """Build a comparison prompt, invoke an AI command, and parse findings."""

    def __init__(
        self,
        llm_client: str | Callable[[str], str] | Any | None,
        project_root: Path,
        lexicon_loader: Any | None = None,
        *,
        template_path: Path | str | None = None,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ):
        self.llm_client = llm_client
        self.project_root = Path(project_root)
        self.lexicon_loader = lexicon_loader
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
        self.max_context_chars = max_context_chars

    def run_diff(
        self,
        extract_input: Path,
        requirements_path: Path,
        ignored_findings: Iterable[str] | None = None,
    ) -> list[Finding]:
        prompt = self.build_prompt(
            extract_input,
            requirements_path,
            ignored_findings=ignored_findings,
        )
        raw_output = self.invoke(prompt)
        ignored_ids = set(_finding_ids(ignored_findings))
        return [
            finding
            for finding in self.deserialize(raw_output)
            if finding.id not in ignored_ids
        ]

    def build_prompt(
        self,
        extract_input: Path,
        requirements_path: Path,
        *,
        ignored_findings: Iterable[str] | None = None,
    ) -> str:
        template = self.template_path.read_text(encoding="utf-8")
        replacements = {
            "extracted_content": self._read_input(extract_input),
            "requirements_content": self._read_input(requirements_path),
            "project_lexicon": self._project_lexicon_text(),
            "ignored_findings": json.dumps(_finding_ids(ignored_findings), ensure_ascii=False),
        }
        return _replace_placeholders(template, replacements)

    def invoke(self, prompt: str) -> str:
        client = self.llm_client
        if callable(client) and not hasattr(client, "invoke") and not hasattr(client, "complete"):
            return str(client(prompt))
        if hasattr(client, "invoke"):
            return str(client.invoke(prompt))
        if hasattr(client, "complete"):
            return str(client.complete(prompt))
        command = client if isinstance(client, str) else None
        return SubprocessAiCommand(command=command, project_root=self.project_root).invoke(prompt)

    def deserialize(self, raw_output: str) -> list[Finding]:
        payload = json.loads(_extract_json_array(raw_output))
        if not isinstance(payload, list):
            raise ValueError("Diff output must be a JSON array")
        return [Finding.from_dict(_finding_payload(item)) for item in payload]

    def _read_input(self, path: Path) -> str:
        resolved = _resolve_path(self.project_root, path)
        text = resolved.read_text(encoding="utf-8", errors="replace").strip()
        return text[: self.max_context_chars] if text else "(empty)"

    def _project_lexicon_text(self) -> str:
        loaded = _loaded_lexicon_text(self.lexicon_loader, self.project_root)
        if loaded:
            return loaded[: self.max_context_chars]
        for name in ("project_lexicon.yaml", "project_lexicon.yml"):
            path = self.project_root / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                return text[: self.max_context_chars] if text else "(empty)"
        return "(none provided)"


def _finding_payload(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Diff finding entries must be mappings")
    payload = dict(item)
    payload.setdefault("source", "extract_brownfield")
    return payload


def _finding_ids(values: Iterable[str] | None) -> list[str]:
    if values is None:
        return []
    return [str(value) for value in values]


def _loaded_lexicon_text(loader: Any | None, project_root: Path) -> str | None:
    if loader is None:
        return None
    value = _invoke_lexicon_loader(loader, project_root)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    chunks: list[str] = []
    name = _string_attr(value, "lexicon_name")
    if name:
        chunks.append(f"loaded_lexicon: {name}")
    recommended = _value_attr(value, "recommended_kinds")
    if isinstance(recommended, list) and recommended:
        chunks.append(yaml.safe_dump({"recommended_kinds": recommended}, sort_keys=False).strip())
    return "\n\n".join(chunk for chunk in chunks if chunk) or None


def _invoke_lexicon_loader(loader: Any, project_root: Path) -> Any:
    if callable(loader):
        return loader(project_root)
    if hasattr(loader, "load"):
        try:
            return loader.load(project_root)
        except TypeError:
            return loader.load()
    return loader


def _value_attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _string_attr(value: Any, name: str) -> str | None:
    candidate = _value_attr(value, name)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None


def _resolve_path(project_root: Path, path: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else project_root / candidate


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
        raise ValueError("Diff output did not contain a JSON array")
    return text[start : end + 1]


__all__ = ["DEFAULT_TEMPLATE_PATH", "DiffEngine"]

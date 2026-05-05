"""Derive required design artifacts from requirements and HITL decisions."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

import yaml

from codd.config import load_project_config
from codd.knowledge_fetcher import KnowledgeFetcher
from codd.lexicon import AskItem


DEFAULTS_DIR = Path(__file__).parent / "required_artifacts" / "defaults"
SUPPORTED_PROJECT_TYPES = ("web", "cli", "mobile", "iot")
VALID_ARTIFACT_SOURCES = {"ai_derived", "user_override", "default_template"}
AI_TIMEOUT_SECONDS = 60


class RequiredArtifactsDeriver:
    """Derive the complete design artifact set required by a project."""

    def __init__(self, project_root: Path, ai_command: str = "claude --print"):
        self.project_root = Path(project_root)
        self.ai_command = ai_command
        self.project_config = self._load_project_config()
        self.config = self._load_config()
        self.fetcher = KnowledgeFetcher(self.project_root)
        self.project_type = self._detect_project_type()

    def derive(
        self,
        requirement_docs: list[str],
        coverage_decisions: list[AskItem] | None = None,
    ) -> list[dict[str, Any]]:
        """Return required artifact entries derived from requirements and decisions."""
        req_text = self._read_requirement_docs(requirement_docs)
        defaults = self._load_defaults(self.project_type)
        prompt = self._build_ai_prompt(
            req_text=req_text,
            project_type=self.project_type,
            decisions_summary=self._summarize_decisions(coverage_decisions or []),
            defaults=defaults,
        )
        return self._parse_ai_response(self._call_ai(prompt))

    def _build_ai_prompt(
        self,
        req_text: str,
        project_type: str,
        decisions_summary: str,
        defaults: list[dict[str, Any]],
    ) -> str:
        """Build the AI prompt used for artifact derivation."""
        defaults_yaml = yaml.safe_dump(defaults, sort_keys=False, allow_unicode=True)
        lines = [
            "You are deriving the complete set of design artifacts needed to implement a project.",
            f"Project type: {project_type}",
            "",
            "Requirements:",
            req_text[:24000].rstrip(),
            "",
            "User decisions from requirement completeness audit:",
            decisions_summary or "(none)",
            "",
            "Default candidate artifacts for this project type:",
            defaults_yaml.rstrip(),
            "",
            "Task:",
            "- Analyze the requirements and user decisions.",
            "- Produce the complete, minimal, non-overlapping list of design artifacts required before implementation.",
            "- Use the defaults as candidates, but include or omit conditional artifacts based on the requirements.",
            "- Include always_required defaults unless the requirements make them irrelevant.",
            "- Add project-specific artifacts only when the requirements need them.",
            "- Preserve dependency order through depends_on.",
            "- Every artifact must include id, title, depends_on, scope, rationale, and source.",
            "- source must be one of: ai_derived, user_override, default_template.",
            "- When a user decision materially influenced an artifact, include derived_from with the ASK item id(s).",
            "- Output JSON only. Do not wrap in Markdown fences.",
            "",
            "JSON schema:",
            "{",
            '  "required_artifacts": [',
            "    {",
            '      "id": "category:name",',
            '      "title": "Artifact Title",',
            '      "depends_on": ["category:dependency"],',
            '      "scope": "What this artifact must cover",',
            '      "rationale": "Why this artifact is required for these requirements",',
            '      "source": "ai_derived",',
            '      "derived_from": ["ask_item_id"]',
            "    }",
            "  ]",
            "}",
        ]
        return "\n".join(lines).rstrip() + "\n"

    def _call_ai(self, prompt: str) -> str:
        """Invoke the configured AI command with the prompt on stdin."""
        command = shlex.split(self.ai_command.strip())
        if not command:
            raise ValueError("ai_command must not be empty")

        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=int(self.config.get("ai_timeout_seconds", AI_TIMEOUT_SECONDS)),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"AI command not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("AI command timed out") from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise ValueError(f"AI command failed: {detail}")
        if not result.stdout.strip():
            raise ValueError("AI command returned empty output")
        return result.stdout

    def _parse_ai_response(self, response: str) -> list[dict[str, Any]]:
        """Parse and validate the AI JSON response."""
        payload = _parse_json_payload(response)
        if not isinstance(payload, dict):
            raise ValueError("AI response must be a JSON object")

        raw_artifacts = payload.get("required_artifacts")
        if not isinstance(raw_artifacts, list):
            raise ValueError("AI response must contain required_artifacts list")

        artifacts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in raw_artifacts:
            artifact = _normalize_artifact(raw)
            artifact_id = artifact["id"]
            if artifact_id in seen_ids:
                raise ValueError(f"duplicate required artifact id: {artifact_id}")
            seen_ids.add(artifact_id)
            artifacts.append(artifact)
        return artifacts

    def _load_defaults(self, project_type: str) -> list[dict[str, Any]]:
        """Load project-type defaults, allowing codd.yaml overrides."""
        override_artifacts = self.config.get("default_artifacts")
        if isinstance(override_artifacts, list):
            return [
                _normalize_default_artifact(item)
                for item in override_artifacts
                if isinstance(item, dict)
            ]

        defaults_path = DEFAULTS_DIR / f"{project_type}.yaml"
        if not defaults_path.exists():
            if project_type == "custom":
                return []
            defaults_path = DEFAULTS_DIR / "web.yaml"

        payload = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
        artifacts = payload.get("default_artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError(f"{defaults_path} default_artifacts must be a list")
        return [
            _normalize_default_artifact(item)
            for item in artifacts
            if isinstance(item, dict)
        ]

    def _load_project_config(self) -> dict[str, Any]:
        try:
            return load_project_config(self.project_root)
        except (FileNotFoundError, ValueError):
            return {}

    def _load_config(self) -> dict[str, Any]:
        section = self.project_config.get("required_artifacts", {})
        return section if isinstance(section, dict) else {}

    def _detect_project_type(self) -> str:
        project = self.project_config.get("project", {})
        configured = ""
        if isinstance(project, dict):
            configured = str(project.get("type") or "").lower()
        configured = str(self.config.get("project_type") or configured).lower()
        if configured == "custom" or configured in SUPPORTED_PROJECT_TYPES:
            return configured

        detected = self.fetcher.detect_project_type().lower()
        return detected if detected in SUPPORTED_PROJECT_TYPES else "web"

    def _read_requirement_docs(self, requirement_docs: list[str]) -> str:
        paths = [
            _resolve_project_path(self.project_root, doc_path)
            for doc_path in requirement_docs
        ] or self._discover_requirement_docs()
        if not paths:
            raise FileNotFoundError("No requirement documents found")

        contents: list[str] = []
        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"Requirement document not found: {path}")
            contents.append(path.read_text(encoding="utf-8", errors="ignore"))
        return "\n\n".join(contents)

    def _discover_requirement_docs(self) -> list[Path]:
        configured_paths = self.config.get("requirement_docs")
        if isinstance(configured_paths, list):
            return [
                _resolve_project_path(self.project_root, str(doc_path))
                for doc_path in configured_paths
                if str(doc_path).strip()
            ]

        candidates: list[Path] = []
        req_dir = self.project_root / "docs" / "requirements"
        if req_dir.exists():
            candidates.extend(sorted(req_dir.rglob("*.md")))
        for filename in ("docs/requirements.md", "REQUIREMENTS.md", "requirements.md"):
            path = self.project_root / filename
            if path.exists():
                candidates.append(path)
        return _unique_paths(candidates)

    def _summarize_decisions(self, decisions: list[AskItem]) -> str:
        lines: list[str] = []
        for item in decisions:
            chosen = item.answer or item.proceeded_with or item.recommended_id or ""
            label = _option_label(item, chosen)
            suffix = f" ({label})" if label and label != chosen else ""
            chosen_text = f"{chosen}{suffix}" if chosen else "(not decided)"
            lines.append(
                f"- {item.id}: {item.question} "
                f"[status={item.status}, decided={chosen_text}]"
            )
        return "\n".join(lines)


def _parse_json_payload(output: str) -> Any:
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
        if not match:
            raise ValueError("AI response is not valid JSON")
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError as exc:
            raise ValueError("AI response is not valid JSON") from exc


def _normalize_artifact(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"required artifact entries must be mappings: {raw}")
    artifact = deepcopy(raw)
    for field_name in ("id", "title", "scope", "rationale", "source"):
        if not isinstance(artifact.get(field_name), str) or not artifact[field_name].strip():
            raise ValueError(f"required artifact missing non-empty {field_name}: {raw}")

    artifact["id"] = artifact["id"].strip()
    artifact["title"] = artifact["title"].strip()
    artifact["scope"] = artifact["scope"].strip()
    artifact["rationale"] = artifact["rationale"].strip()
    artifact["source"] = artifact["source"].strip()
    if artifact["source"] not in VALID_ARTIFACT_SOURCES:
        raise ValueError(f"invalid required artifact source: {artifact['source']}")
    artifact["depends_on"] = _string_list(artifact.get("depends_on", []), "depends_on")
    if "derived_from" in artifact:
        artifact["derived_from"] = _string_list(artifact.get("derived_from", []), "derived_from")
    return artifact


def _normalize_default_artifact(raw: dict[str, Any]) -> dict[str, Any]:
    artifact = deepcopy(raw)
    for field_name in ("id", "title", "scope"):
        if not isinstance(artifact.get(field_name), str) or not artifact[field_name].strip():
            raise ValueError(f"default artifact missing non-empty {field_name}: {raw}")
    artifact["depends_on"] = _string_list(artifact.get("depends_on", []), "depends_on")
    if "condition" in artifact:
        artifact["condition"] = str(artifact["condition"])
    artifact["always_required"] = bool(artifact.get("always_required", False))
    return artifact


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [str(item) for item in value if str(item).strip()]


def _resolve_project_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _option_label(item: AskItem, option_id: str) -> str:
    for option in item.options:
        if option.id == option_id:
            return option.label
    return ""

"""Derive required design artifacts from requirements and HITL decisions."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

import yaml

from codd.ai_invoke import resolve_ai_command
from codd.claude_cli import with_default_claude_permission_bypass
from codd.config import load_project_config
from codd.defaults import AI_TIMEOUT_SECONDS as _DEFAULT_AI_TIMEOUT_SECONDS
from codd.knowledge_fetcher import KnowledgeFetcher
from codd.lexicon import LEXICON_FILENAME, AskItem, ProjectLexicon, validate_lexicon
from codd.path_safety import resolve_project_path
from codd.project_types import (
    CUSTOM_PROJECT_TYPE,
    GENERIC_PROJECT_TYPE,
    resolve_project_type,
    supported_project_types,
)
from codd.requirement_appendix import PROVISIONAL_DECISIONS_PATH, read_requirement_appendix
from codd.required_artifact_requirements import number_requirements, read_requirement_snapshot


DEFAULTS_DIR = Path(__file__).parent / "required_artifacts" / "defaults"
VALID_ARTIFACT_SOURCES = {"ai_derived", "user_override", "default_template"}
_ARTIFACT_TEXT_FIELDS = ("id", "title", "scope", "rationale", "source")
_ACCOUNTED_ATTEMPTS = 3
# Sourced from codd.defaults; see feedback_codd_default_values_policy.
AI_TIMEOUT_SECONDS = int(_DEFAULT_AI_TIMEOUT_SECONDS)


def artifact_fingerprint(artifacts: list[dict[str, Any]]) -> str:
    return sha256(yaml.safe_dump(artifacts, sort_keys=True).encode("utf-8")).hexdigest()


def lexicon_data_from_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{LEXICON_FILENAME} must contain a YAML mapping")
    for field in ("node_vocabulary", "naming_conventions", "design_principles", "coverage_decisions"):
        data.setdefault(field, [])
    return data


def requirement_docs_from_lexicon(data: dict[str, Any]) -> list[str]:
    value = data.get("requirement_docs_path") or data.get("requirement_doc_paths") or data.get("requirement_docs")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def accounted_lexicon_path(project_root: Path) -> Path:
    path = resolve_project_path(project_root, LEXICON_FILENAME)
    if path is None:
        raise ValueError(f"{LEXICON_FILENAME} resolves outside the project")
    return path


def plan_accounted_artifacts(
    project_root: Path, ai_cmd: str | None, rederive: bool = False, *,
    warn: Callable[[str], None] = print, select_if_unaccounted_empty: bool = False,
) -> dict[str, Any]:
    """Reuse a saved selection or atomically persist the accounted derivation."""
    path = accounted_lexicon_path(project_root)
    original = _optional_file_bytes(path)
    data = lexicon_data_from_yaml((original or b"").decode("utf-8"))
    lexicon = ProjectLexicon(data)
    if "required_artifacts" in data and not rederive:
        validate_lexicon(data)
        artifacts = lexicon.required_artifacts
        report = {"required_artifacts": artifacts, "saved": True}
        decisions = data.get("required_artifact_decisions")
        if isinstance(decisions, dict) and decisions.get("artifacts_sha256") == artifact_fingerprint(artifacts):
            report = {**decisions, **report}
            del report["artifacts_sha256"]
            return report
        if artifacts or not select_if_unaccounted_empty:
            return report

    command = resolve_ai_command(load_project_config(project_root), ai_cmd, command_name="plan_derive")
    deriver = RequiredArtifactsDeriver(project_root, ai_command=command, warn=warn)
    report, snapshot = deriver._derive_accounted(
        requirement_docs_from_lexicon(data), lexicon.coverage_decisions,
    )
    artifacts = report["required_artifacts"]
    fingerprint = artifact_fingerprint(artifacts)
    lexicon.set_required_artifacts(artifacts)
    output = lexicon.as_dict()
    output["required_artifact_decisions"] = {
        "artifacts_sha256": fingerprint,
        **{key: value for key, value in report.items() if key != "required_artifacts"},
    }
    output["required_artifact_snapshot"] = {"artifacts_sha256": fingerprint, **snapshot}
    validate_lexicon(output)
    _write_accounted_lexicon(path, original, yaml.safe_dump(output, sort_keys=False, allow_unicode=True))
    return {**report, "saved": False}


def _optional_file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_accounted_lexicon(
    path: Path, original: bytes | None, content: str, *, operation: str = "derivation",
) -> None:
    """Replace the lexicon atomically if it still matches the bytes we read."""
    from stat import S_IMODE
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            lock = path.with_name(f".{path.name}.derive.lock")
            try:
                lock.mkdir()
            except FileExistsError as exc:
                raise ValueError(f"Another derivation is writing {LEXICON_FILENAME}. Retry the command.") from exc
            try:
                if _optional_file_bytes(path) != original:
                    raise ValueError(f"{LEXICON_FILENAME} changed during {operation}. Retry the command.")
                if original is not None:
                    temp_path.chmod(S_IMODE(path.stat().st_mode))
                temp_path.replace(path)
            finally:
                lock.rmdir()
        finally:
            temp_path.unlink(missing_ok=True)


class RequiredArtifactsDeriver:
    """Derive the complete design artifact set required by a project."""

    def __init__(
        self, project_root: Path, ai_command: str = "claude --print", *, warn: Callable[[str], None] = print,
    ):
        self.project_root = Path(project_root)
        self.ai_command = ai_command
        self.project_config = self._load_project_config()
        self.config = self._load_config()
        self.fetcher = KnowledgeFetcher(self.project_root)
        self.project_type = self._detect_project_type(warn)

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

    def derive_accounted(
        self,
        requirement_docs: list[str],
        coverage_decisions: list[AskItem] | None = None,
    ) -> dict[str, Any]:
        """Derive a validated selection and account for all candidates and requirements."""
        report, _ = self._derive_accounted(requirement_docs, coverage_decisions)
        return report

    def _derive_accounted(
        self,
        requirement_docs: list[str],
        coverage_decisions: list[AskItem] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        root = self.project_root.resolve()
        documents = []
        hashes = {}
        for path in _unique_paths(self._accounted_requirement_paths(requirement_docs)):
            resolved = path.resolve()
            try:
                text, digest = read_requirement_snapshot(resolved)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"Requirement document not found: {path}") from exc
            rel = resolved.relative_to(root).as_posix()
            documents.append((rel, text))
            hashes[rel] = digest
        items, requirements = number_requirements(documents)
        defaults = self._load_defaults(self.project_type, strict=True)
        candidates: dict[str, dict[str, Any]] = {}
        for artifact in defaults:
            artifact_id = artifact["id"].strip()
            if artifact_id in candidates:
                raise ValueError(f"duplicate default artifact id: {artifact_id}")
            candidates[artifact_id] = {**artifact, "id": artifact_id}
        prompt = _accounted_prompt(
            self.project_type, requirements, list(candidates.values()),
            self._summarize_decisions(coverage_decisions or []),
        )
        feedback = ""
        for _ in range(_ACCOUNTED_ATTEMPTS):
            response = self._call_ai(prompt + feedback, allow_empty=True)
            try:
                report = _validate_accounted_response(response, candidates, items)
                break
            except ValueError as exc:
                violations = str(exc)
                feedback = (
                    "\nThe previous response failed validation. Correct every issue below "
                    "and return the complete JSON object again.\n" + violations + "\n"
                )
        else:
            raise ValueError(
                f"Artifact derivation failed after {_ACCOUNTED_ATTEMPTS} attempts:\n{violations}"
            )
        selected = {row["id"] for row in report["required_artifacts"]}
        return report, {
            "requirements_sha256": hashes,
            "candidates": {
                artifact_id: candidate for artifact_id, candidate in candidates.items()
                if artifact_id in selected
            },
        }

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

    def _call_ai(self, prompt: str, *, allow_empty: bool = False) -> str:
        """Invoke the configured AI command with the prompt on stdin."""
        command = with_default_claude_permission_bypass(shlex.split(self.ai_command.strip()))
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
        if not allow_empty and not result.stdout.strip():
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

    def _load_defaults(self, project_type: str, *, strict: bool = False) -> list[dict[str, Any]]:
        """Load project-type defaults, allowing codd.yaml overrides."""
        override_artifacts = self.config.get("default_artifacts")
        if isinstance(override_artifacts, list):
            return _normalize_defaults(override_artifacts, strict=strict)
        if strict and "default_artifacts" in self.config:
            raise ValueError("required_artifacts.default_artifacts must be a list")

        if project_type == CUSTOM_PROJECT_TYPE:
            return []
        defaults_path = DEFAULTS_DIR / f"{project_type}.yaml"
        if not defaults_path.exists():
            # No silent web fallback: use the conservative generic baseline.
            defaults_path = DEFAULTS_DIR / f"{GENERIC_PROJECT_TYPE}.yaml"

        payload = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
        artifacts = payload.get("default_artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError(f"{defaults_path} default_artifacts must be a list")
        return _normalize_defaults(artifacts, strict=strict)

    def _load_project_config(self) -> dict[str, Any]:
        try:
            return load_project_config(self.project_root)
        except (FileNotFoundError, ValueError):
            return {}

    def _load_config(self) -> dict[str, Any]:
        section = self.project_config.get("required_artifacts", {})
        return section if isinstance(section, dict) else {}

    def _detect_project_type(self, warn: Callable[[str], None] = print) -> str:
        project = self.project_config.get("project", {})
        configured = ""
        if isinstance(project, dict):
            configured = str(project.get("type") or "").lower()
        configured = str(self.config.get("project_type") or configured).lower()

        known = set(supported_project_types(self.project_root))
        if configured == CUSTOM_PROJECT_TYPE or configured in known:
            return configured

        detected = self.fetcher.detect_project_type().lower()
        resolved, reason = resolve_project_type(
            configured or None, detected or None, self.project_root
        )
        if configured and resolved == GENERIC_PROJECT_TYPE:
            # Unknown configured type: warn, then use the generic baseline (NOT web).
            warn(f"warning: {reason}")
        return resolved

    def _read_requirement_docs(self, requirement_docs: list[str]) -> str:
        contents: list[str] = []
        for path in self._requirement_paths(requirement_docs):
            if not path.exists():
                raise FileNotFoundError(f"Requirement document not found: {path}")
            contents.append(path.read_text(encoding="utf-8", errors="ignore"))
        return "\n\n".join(contents)

    def _requirement_paths(self, requirement_docs: list[str]) -> list[Path]:
        # Jail every user-supplied requirement_doc path: an absolute/``../`` value
        # (or in-root symlink) resolving outside the project root is excluded so
        # its contents never enter the requirement evidence text.
        explicit = [
            safe
            for doc_path in requirement_docs
            if (safe := resolve_project_path(self.project_root, doc_path)) is not None
        ]
        paths = explicit or self._discover_requirement_docs()
        if not paths:
            raise FileNotFoundError("No requirement documents found")

        if read_requirement_appendix(self.project_root) is not None:
            appendix = self.project_root / PROVISIONAL_DECISIONS_PATH
            if appendix.resolve() not in {path.resolve() for path in paths}:
                paths.append(appendix)
        return paths

    def _accounted_requirement_paths(self, requirement_docs: list[str]) -> list[Path]:
        if requirement_docs or isinstance(self.config.get("requirement_docs"), list):
            return self._requirement_paths(requirement_docs)

        # Include requirement documents from the same configured scan as plan --init.
        from codd.planner import _load_requirement_documents

        root = self.project_root.resolve()
        paths = self._discover_requirement_docs()
        paths.extend(root / doc.path for doc in _load_requirement_documents(root, self.project_config))
        if not paths:
            raise FileNotFoundError("No requirement documents found")
        return paths

    def _discover_requirement_docs(self) -> list[Path]:
        configured_paths = self.config.get("requirement_docs")
        if isinstance(configured_paths, list):
            # Jail each configured path; out-of-root entries are dropped.
            return [
                safe
                for doc_path in configured_paths
                if str(doc_path).strip()
                and (safe := resolve_project_path(self.project_root, str(doc_path)))
                is not None
            ]

        candidates: list[Path] = []
        req_dir = self.project_root / "docs" / "requirements"
        if req_dir.exists():
            # rglob follows symlinks: jail each match so an in-root *.md symlinked
            # outside the root is not surfaced as a requirement doc.
            candidates.extend(
                path
                for path in sorted(req_dir.rglob("*.md"))
                if resolve_project_path(self.project_root, path) is not None
            )
        for filename in ("docs/requirements.md", "REQUIREMENTS.md", "requirements.md"):
            path = self.project_root / filename
            if resolve_project_path(self.project_root, path) is not None and path.exists():
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


def _accounted_prompt(
    project_type: str,
    requirements: str,
    defaults: list[dict[str, Any]],
    decisions: str,
) -> str:
    return f"""Derive the complete, minimal, non-overlapping set of design artifacts for this project.
Project type: {project_type}

Requirements (R markers start items; other lines continue the item or provide context):
{requirements}

User decisions from requirement completeness audit:
{decisions or '(none)'}

Default candidate artifacts for this project type:
{yaml.safe_dump(defaults, sort_keys=False, allow_unicode=True).rstrip()}

Rules:
- List each candidate ID exactly once, in required_artifacts or omitted_artifacts.
- Select every always_required candidate. Use the candidate conditions for the other selections.
- Add a new artifact ID only if the requirements need a separate artifact beyond the candidates.
- Every selected artifact, including additions, needs a rationale and at least one requirement_ids reference to an actual R marker that supports it.
- Every R marker must be referenced by at least one selected artifact or listed once in unassigned_requirements with a rationale. Use unassigned only where no design artifact is needed.
- Every depends_on ID must also be selected. Do not create dependency cycles.
- Use source ai_derived, user_override or default_template. Include derived_from with ASK item IDs when user decisions materially influenced the artifact.
- Write every rationale, and the scope of each selected artifact, in the language of the requirement documents.
- Return only a JSON object with these three lists. Use empty lists where applicable.
{{
  "required_artifacts": [{{"id": "category:name", "title": "Artifact title", "scope": "What it covers", "rationale": "Why it is needed", "source": "ai_derived", "depends_on": [], "requirement_ids": ["R1"]}}],
  "omitted_artifacts": [{{"id": "category:optional", "rationale": "Why it is unnecessary"}}],
  "unassigned_requirements": [{{"id": "R2", "rationale": "Why it needs no artifact"}}]
}}
"""


def _validate_accounted_response(
    response: str,
    candidates: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collect all report violations before accepting a selection."""
    if not response.strip():
        raise ValueError("AI response is empty; expected a JSON object")
    # Accept one surrounding Markdown fence, as the legacy derivation does, so a
    # correct selection is not sent back for a retry over its wrapper alone.
    fenced = re.fullmatch(r"\s*```[A-Za-z]*[ \t]*\n(.*?)\n?```\s*", response, flags=re.DOTALL)
    if fenced:
        response = fenced.group(1)
    try:
        payload = json.loads(response, object_pairs_hook=_unique_json_object, parse_constant=_invalid_json_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI response must be a JSON object")

    errors: list[str] = []
    selected = _decision_rows(payload, "required_artifacts", errors)
    omitted = _decision_rows(payload, "omitted_artifacts", errors)
    unassigned = _decision_rows(payload, "unassigned_requirements", errors)
    selected_ids = {artifact_id for artifact_id, _ in selected}
    omitted_ids = {artifact_id for artifact_id, _ in omitted}
    unassigned_ids = {item_id for item_id, _ in unassigned}
    known_items = {item["id"] for item in items}

    for artifact_id, candidate in candidates.items():
        if artifact_id in selected_ids and artifact_id in omitted_ids:
            errors.append(f"candidate {artifact_id!r} is both selected and omitted (duplicate id)")
        if candidate["always_required"] and artifact_id not in selected_ids:
            errors.append(f"always_required candidate {artifact_id!r} must be selected")
        elif artifact_id not in selected_ids and artifact_id not in omitted_ids:
            errors.append(f"candidate {artifact_id!r} is missing from both selection lists")

    omissions = []
    for artifact_id, row in omitted:
        label = f"omitted candidate {artifact_id!r}"
        rationale = _decision_text(row, "rationale", label, errors)
        if artifact_id not in candidates:
            errors.append(f"{label} is not in the candidate list")
        else:
            omissions.append({"id": artifact_id, "title": candidates[artifact_id]["title"], "rationale": rationale})

    artifacts = []
    evidence: dict[str, list[str]] = {}
    dependencies: dict[str, list[str]] = {}
    assigned_ids: set[str] = set()
    for artifact_id, row in selected:
        label = f"required artifact {artifact_id!r}"
        artifact = {key: value for key, value in row.items() if key != "requirement_ids"}
        for field in _ARTIFACT_TEXT_FIELDS:
            artifact[field] = _decision_text(row, field, label, errors)
        if artifact["source"] and artifact["source"] not in VALID_ARTIFACT_SOURCES:
            errors.append(f"{label} has invalid source {artifact['source']!r}")
        artifact["depends_on"] = _decision_ids(row, "depends_on", label, errors)
        if "derived_from" in row:
            artifact["derived_from"] = _decision_ids(row, "derived_from", label, errors)
        for dependency in artifact["depends_on"]:
            if dependency not in selected_ids:
                errors.append(f"{label} depends_on {dependency!r}, which is not selected")
        dependencies[artifact_id] = [dep for dep in artifact["depends_on"] if dep in selected_ids]
        artifacts.append(artifact)

        refs = _decision_ids(row, "requirement_ids", label, errors, nonempty=True)
        evidence[artifact_id] = refs
        for ref in refs:
            if ref not in known_items:
                errors.append(f"{label} cites unknown requirement {ref!r} in requirement_ids")
            else:
                assigned_ids.add(ref)

    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as exc:
        errors.append("depends_on cycle: " + " -> ".join(exc.args[1]))

    unassigned_items = []
    for item_id, row in unassigned:
        label = f"unassigned requirement {item_id!r}"
        rationale = _decision_text(row, "rationale", label, errors)
        if item_id not in known_items:
            errors.append(f"{label} is an unknown requirement")
        elif item_id in assigned_ids:
            errors.append(f"{label} is already assigned to a selected artifact")
        unassigned_items.append({"id": item_id, "rationale": rationale})

    accounted_ids = assigned_ids | unassigned_ids
    for item in items:
        if item["id"] not in accounted_ids:
            errors.append(
                f"Unaccounted requirement {item['id']} ({item['path']}:{item['start_line']}): "
                f"{item['text'][:200]!r}"
            )
    if errors:
        raise ValueError("\n".join(f"- {error}" for error in errors))

    return {
        "required_artifacts": artifacts,
        "artifact_evidence": evidence,
        "added_artifact_ids": [artifact_id for artifact_id, _ in selected if artifact_id not in candidates],
        "omitted_artifacts": omissions,
        "requirement_items": items,
        "unassigned_requirements": unassigned_items,
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"AI response repeats JSON key {key!r}")
        output[key] = value
    return output


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"AI response is not valid JSON: invalid constant {value}")


def _decision_rows(
    payload: dict[str, Any], field: str, errors: list[str],
) -> list[tuple[str, dict[str, Any]]]:
    raw = payload.get(field)
    if not isinstance(raw, list):
        errors.append(f"{field} must be a list")
        return []
    rows = []
    seen = set()
    for index, row in enumerate(raw):
        label = f"{field}[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        row_id = _decision_text(row, "id", label, errors)
        if row_id:
            if row_id in seen:
                errors.append(f"{label} has duplicate id {row_id!r}")
            seen.add(row_id)
            rows.append((row_id, row))
    return rows


def _decision_text(row: dict[str, Any], field: str, label: str, errors: list[str]) -> str:
    value = row.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    errors.append(f"{label} needs a non-empty {field} string")
    return ""


def _decision_ids(
    row: dict[str, Any], field: str, label: str, errors: list[str], *, nonempty: bool = False,
) -> list[str]:
    raw = row.get(field)
    if not isinstance(raw, list) or (nonempty and not raw):
        qualifier = "a non-empty" if nonempty else "a"
        errors.append(f"{label} needs {qualifier} {field} list")
        return []
    ids = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label} {field}[{index}] must be a non-empty ID string")
        else:
            ids.append(item.strip())
    return ids


def _normalize_defaults(artifacts: list, *, strict: bool) -> list[dict[str, Any]]:
    defaults = []
    for index, item in enumerate(artifacts):
        if isinstance(item, dict):
            defaults.append(_normalize_default_artifact(item))
        elif strict:
            raise ValueError(f"default_artifacts[{index}] must be a mapping")
    return defaults


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
    for field_name in _ARTIFACT_TEXT_FIELDS:
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

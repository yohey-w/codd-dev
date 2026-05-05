"""Requirement completeness auditing before design artifact derivation."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

import yaml

from codd.ask_user_question_adapter import send_ask_items
from codd.config import load_project_config
from codd.hitl_session import HitlSession
from codd.knowledge_fetcher import KnowledgeFetcher
from codd.lexicon import AskItem, AskOption, LEXICON_FILENAME


DEFAULTS_DIR = Path(__file__).parent / "requirement_completeness" / "defaults"
SUPPORTED_PROJECT_TYPES = ("web", "cli", "mobile", "iot")
AI_TIMEOUT_SECONDS = 300


class RequirementCompletenessAuditor:
    """Generate ASK items for missing requirement details needed by design derivation."""

    def __init__(self, project_root: Path, ai_command: str = "claude --print"):
        self.project_root = Path(project_root)
        self.ai_command = ai_command
        self.project_config = self._load_project_config()
        self.config = self._load_config()
        self.fetcher = KnowledgeFetcher(self.project_root)
        self.project_type = self._detect_project_type()
        self.session = HitlSession()

    def audit(self, requirement_docs: list[str]) -> HitlSession:
        """Audit requirement docs, dispatch ASK items, and persist decisions."""
        req_text = self._read_requirement_docs(requirement_docs)
        for item in self._generate_missing_items(req_text, self.project_type):
            if self._check_item_in_requirements(item, req_text):
                continue
            options = self._generate_ask_options(item, self.project_type)
            ask_item = AskItem(
                id=str(item["id"]),
                question=str(item["question"]),
                type=_ask_type(item.get("type")),
                options=options,
                blocking=bool(item.get("blocking", False)),
                recommended_id=_recommended_option_id(options),
            )
            self.session.add_ask(ask_item)

        if self._hitl_mode() == "cooperative":
            self.session.proceed_with_recommended()

        lexicon_path = self.project_root / LEXICON_FILENAME
        send_ask_items(
            self.session.ask_items,
            channels=list(self.config.get("channels", ["askuserquestion", "ntfy", "lexicon"])),
            ntfy_topic=str(self.config.get("ntfy_topic") or ""),
            lexicon_path=lexicon_path,
        )
        return self.session

    def _generate_missing_items(self, req_text: str, project_type: str) -> list[dict[str, Any]]:
        """Generate required information items from defaults plus optional AI metadata."""
        default_items = self._load_default_questions(project_type)
        if not self.config.get("ai_missing_items", False):
            return default_items

        prompt = "\n".join(
            [
                "Return JSON array of requirement information items still needed.",
                "Each item must include id, question, type, blocking, aliases, search_query.",
                f"Project type: {project_type}",
                "--- Requirements ---",
                req_text[:12000],
            ]
        )
        ai_items = self._invoke_ai_json(prompt)
        if not isinstance(ai_items, list):
            return default_items
        return _merge_items_by_id(default_items, [_coerce_item(item) for item in ai_items])

    def _check_item_in_requirements(self, item: dict[str, Any], req_text: str) -> bool:
        """Check whether an information item is already covered by requirements text."""
        if not req_text.strip():
            return False

        lowered = req_text.lower()
        candidates = _requirement_match_terms(item)
        if any(candidate in lowered for candidate in candidates):
            return True

        if not self.config.get("semantic_check", False):
            return False
        prompt = "\n".join(
            [
                "Answer only JSON true or false.",
                f"Question: {item.get('question', '')}",
                "--- Requirements ---",
                req_text[:12000],
            ]
        )
        return self._invoke_ai_json(prompt) is True

    def _generate_ask_options(self, item: dict[str, Any], project_type: str) -> list[AskOption]:
        """Generate ASK options from project defaults, optional search, and optional AI."""
        knowledge_summary = ""
        search_query = str(item.get("search_query") or "").strip()
        if search_query:
            knowledge_summary = self.fetcher.fetch(search_query).result

        options = [_coerce_option(option) for option in item.get("options", [])]
        if options:
            return _ensure_one_recommended(options)

        if self.config.get("ai_generate_options", False):
            prompt = "\n".join(
                [
                    "Return JSON array of 3-5 selectable options.",
                    "Each option must include id, label, description, cost_effort, recommended.",
                    f"Project type: {project_type}",
                    f"Question: {item.get('question', '')}",
                    "--- Knowledge ---",
                    knowledge_summary[:4000],
                ]
            )
            ai_options = self._invoke_ai_json(prompt)
            if isinstance(ai_options, list):
                options = [_coerce_option(option) for option in ai_options]
                if options:
                    return _ensure_one_recommended(options)

        return _fallback_options()

    def _load_project_config(self) -> dict[str, Any]:
        try:
            return load_project_config(self.project_root)
        except (FileNotFoundError, ValueError):
            return {}

    def _load_config(self) -> dict[str, Any]:
        section = self.project_config.get("requirement_completeness", {})
        return section if isinstance(section, dict) else {}

    def _detect_project_type(self) -> str:
        configured = str(self.config.get("project_type") or "").lower()
        if configured in SUPPORTED_PROJECT_TYPES:
            return configured
        detected = self.fetcher.detect_project_type().lower()
        return detected if detected in SUPPORTED_PROJECT_TYPES else "web"

    def _read_requirement_docs(self, requirement_docs: list[str]) -> str:
        doc_paths = [
            _resolve_project_path(self.project_root, doc_path)
            for doc_path in requirement_docs
        ] or self._discover_requirement_docs()
        contents: list[str] = []
        for path in doc_paths:
            if not path.exists():
                raise FileNotFoundError(f"Requirement document not found: {path}")
            contents.append(path.read_text(encoding="utf-8", errors="ignore"))
        return "\n\n".join(contents)

    def _discover_requirement_docs(self) -> list[Path]:
        candidates: list[Path] = []
        req_dir = self.project_root / "docs" / "requirements"
        if req_dir.exists():
            candidates.extend(sorted(req_dir.rglob("*.md")))
        for filename in ("docs/requirements.md", "REQUIREMENTS.md", "requirements.md"):
            path = self.project_root / filename
            if path.exists():
                candidates.append(path)
        return _unique_paths(candidates)

    def _load_default_questions(self, project_type: str) -> list[dict[str, Any]]:
        defaults_path = DEFAULTS_DIR / f"{project_type}.yaml"
        if not defaults_path.exists():
            defaults_path = DEFAULTS_DIR / "web.yaml"
        payload = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
        questions = payload.get("default_questions", [])
        default_items = [_coerce_item(question) for question in questions if isinstance(question, dict)]
        overrides = [
            _coerce_item(question)
            for question in self.config.get("questions", [])
            if isinstance(question, dict)
        ]
        return _merge_items_by_id(default_items, overrides)

    def _hitl_mode(self) -> str:
        hitl = self.project_config.get("hitl", {})
        if isinstance(hitl, dict):
            mode = str(hitl.get("mode") or "").lower()
            if mode in {"cooperative", "blocking"}:
                return mode
        mode = str(self.config.get("hitl_mode") or "").lower()
        return mode if mode in {"cooperative", "blocking"} else "cooperative"

    def _invoke_ai_json(self, prompt: str) -> Any:
        command = self.ai_command.strip()
        if not command:
            return None
        try:
            result = subprocess.run(
                shlex.split(command),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=int(self.config.get("ai_timeout_seconds", AI_TIMEOUT_SECONDS)),
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return None
        if result.returncode != 0:
            return None
        return _parse_json_payload(result.stdout)


def _resolve_project_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def _ask_type(value: Any) -> str:
    text = str(value or "select")
    return text if text in {"select", "multiselect", "free_text"} else "select"


def _coerce_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    data = dict(item)
    data["id"] = str(data.get("id") or "question")
    data["question"] = str(data.get("question") or data["id"])
    data["type"] = _ask_type(data.get("type"))
    data["blocking"] = bool(data.get("blocking", False))
    data["aliases"] = [str(alias) for alias in data.get("aliases", []) if alias]
    data["options"] = [
        option
        for option in data.get("options", [])
        if isinstance(option, dict)
    ]
    return data


def _coerce_option(option: Any) -> AskOption:
    if not isinstance(option, dict):
        option = {}
    return AskOption(
        id=str(option.get("id") or "option"),
        label=str(option.get("label") or option.get("id") or "Option"),
        description=str(option.get("description", "")),
        cost_effort=_cost_effort(option.get("cost_effort")),
        pros=str(option.get("pros", "")),
        cons=str(option.get("cons", "")),
        recommended=bool(option.get("recommended", False)),
        recommendation_rationale=str(option.get("recommendation_rationale", "")),
        type=str(option.get("type", "option")),
    )


def _cost_effort(value: Any) -> str:
    text = str(value or "medium")
    return text if text in {"low", "medium", "high"} else "medium"


def _ensure_one_recommended(options: list[AskOption]) -> list[AskOption]:
    if not options:
        return options
    if not any(option.recommended for option in options):
        options[0].recommended = True
    first_recommended_seen = False
    for option in options:
        if option.recommended and not first_recommended_seen:
            first_recommended_seen = True
        elif option.recommended:
            option.recommended = False
    return options


def _recommended_option_id(options: list[AskOption]) -> str | None:
    for option in options:
        if option.recommended:
            return option.id
    return options[0].id if options else None


def _fallback_options() -> list[AskOption]:
    return [
        AskOption(
            id="recommended_baseline",
            label="Recommended baseline",
            cost_effort="medium",
            recommended=True,
            recommendation_rationale="Best default when requirements are silent.",
        ),
        AskOption(id="minimal_scope", label="Minimal scope", cost_effort="low"),
        AskOption(id="custom", label="Other / custom", type="free_text"),
    ]


def _merge_items_by_id(
    defaults: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in [*defaults, *overrides]:
        item_id = str(item.get("id", ""))
        if not item_id:
            continue
        if item_id not in merged:
            order.append(item_id)
            merged[item_id] = dict(item)
        else:
            merged[item_id] = {**merged[item_id], **item}
    return [merged[item_id] for item_id in order]


def _requirement_match_terms(item: dict[str, Any]) -> list[str]:
    raw_terms = [
        str(item.get("id", "")).replace("_", " "),
        str(item.get("question", "")),
        *[str(alias) for alias in item.get("aliases", [])],
    ]
    terms: list[str] = []
    for term in raw_terms:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", term.lower()).strip()
        if len(cleaned) >= 3:
            terms.append(cleaned)
        for token in cleaned.split():
            if len(token) >= 4 and token not in {"what", "which", "should", "required"}:
                terms.append(token)
    return sorted(set(terms), key=len, reverse=True)


def _parse_json_payload(output: str) -> Any:
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                return None
    return None


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique

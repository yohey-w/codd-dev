"""Validate and record provisional decisions for discovered findings."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from subprocess import CalledProcessError
from typing import BinaryIO

from codd.deployment.providers.ai_command import AiCommandError
from codd.elicit.finding import Finding
from codd.elicit.persistence import staged_replacement
from codd.path_safety import resolve_project_path
from codd.requirement_appendix import (
    APPENDIX_GUIDANCE,
    DECISION_LOCK_PATH,
    DECISION_STATUS,
    DOCUMENT_MARKER,
    PROVISIONAL_DECISIONS_PATH,
    is_managed_appendix,
    markdown_outside_fences,
    parse_entry_marker,
)


_MAX_ATTEMPTS = 3
_MAX_DIAGNOSTIC_CHARS = 300
_ENTRY_START = re.compile(
    rb"^(?:(?P<marker><!-- codd:provisional-decision [^\r\n]* -->)"
    rb"[ \t]*(?:\r?\n## (?P<marked_heading>[^\r\n]+))?|## (?P<plain_heading>[^\r\n]+))\r?$",
    re.MULTILINE,
)
_ENTRY_FIELD = re.compile(
    rb"^- (Decision|Original question|Reason|Related requirements|Status):([^\n]*(?:\n {2}[^\n]*)*)",
    re.MULTILINE,
)
_NO_QUESTION = "(not supplied)"
_NO_REQUIREMENTS = "(none supplied)"
_EMPTY_DECISION = "The appendix entry has an empty decision and needs human review."
_CONFLICTING_QUESTIONS = "provisional_decision_conflicting_questions"
_DUPLICATE_ID = "Multiple findings use the same ID and need human review."


@dataclass
class ProvisionalDecisionResult:
    path: Path
    added_decisions: dict[str, str] = field(default_factory=dict)
    already_decided_ids: list[str] = field(default_factory=list)
    unresolved: dict[str, str] = field(default_factory=dict)
    unresolved_findings: dict[str, Finding] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def added_ids(self) -> list[str]:
        return list(self.added_decisions)

    def unresolved_question(self, finding_id: str) -> str:
        return _original_question(self.unresolved_findings[finding_id])


@dataclass(frozen=True)
class _Choice:
    decision: str
    reason: str


@dataclass(frozen=True)
class _StoredDecision:
    decision: str
    question: str
    related: str


def read_provisional_decisions(project_root: Path | str) -> dict[str, str]:
    """Read the current editable decision text by finding ID."""
    records = _read_index(project_root)
    return {finding_id: record.decision for finding_id, record in records.items() if record.decision}


def filter_decided_findings(
    project_root: Path | str, findings: Iterable[Finding]
) -> list[Finding]:
    """Keep findings without a nonempty decision or with conflicting questions."""
    decided = read_provisional_decisions(project_root)
    return [
        finding for finding in findings
        if _CONFLICTING_QUESTIONS in finding.details or finding.id not in decided
    ]


def resolve_provisional_decisions(
    project_root: Path | str,
    findings: Iterable[Finding],
    *,
    invoke: Callable[[str], str],
    requirements_context: str,
    reviewed_ids: Callable[[], set[str]] | None = None,
    prior_findings: Iterable[Finding] = (),
) -> ProvisionalDecisionResult:
    """Append validated choices, refreshing any human reviews under the write lock."""
    root = Path(project_root).resolve()
    path = _project_file(root, PROVISIONAL_DECISIONS_PATH)
    result = ProvisionalDecisionResult(path=path)
    discovered = _merge_findings(findings)
    unique_findings = dict(discovered)
    for finding in prior_findings:
        unique_findings.setdefault(finding.id, finding)
    original = _read_bytes(path)
    if unique_findings:
        _require_managed(root, path, original)
    records = _decision_index(root, original or b"")
    empty = {key: record for key, record in records.items() if not record.decision}
    for key, record in empty.items():
        if key not in unique_findings:
            unique_findings[key] = Finding(
                id=key,
                kind="coverage_gap",
                severity="medium",
                name=key,
                question=record.question or None,
                related_requirement_ids=_parse_requirement_ids(record.related),
            )

    requested: dict[str, Finding] = {}
    for finding_id, finding in unique_findings.items():
        if finding_id in discovered and _CONFLICTING_QUESTIONS in finding.details:
            result.unresolved[finding_id] = _DUPLICATE_ID
        elif finding_id in empty:
            result.unresolved[finding_id] = _EMPTY_DECISION
        elif finding_id in records:
            result.already_decided_ids.append(finding_id)
        elif _CONFLICTING_QUESTIONS in finding.details:
            result.unresolved[finding_id] = _DUPLICATE_ID
        else:
            requested[finding_id] = finding

    accepted, errors, issues = _request_choices(invoke, requirements_context, requested)
    if accepted:
        added, already_decided, newly_empty = _append_choices(root, path, requested, accepted, reviewed_ids)
        result.added_decisions.update(added)
        result.already_decided_ids.extend(already_decided)
        result.unresolved.update(dict.fromkeys(newly_empty, _EMPTY_DECISION))
    result.unresolved.update(errors)
    result.unresolved_findings = {key: unique_findings[key] for key in result.unresolved}
    result.issues = issues
    return result


def _merge_findings(findings: Iterable[Finding]) -> dict[str, Finding]:
    """Retain every question for IDs assigned more than once in this discovery."""
    by_id: dict[str, Finding] = {}
    questions: dict[str, list[str]] = {}
    for finding in findings:
        by_id.setdefault(finding.id, finding)
        questions.setdefault(finding.id, []).append(_original_question(finding))
    for key, variants in questions.items():
        if len(variants) > 1:
            finding = by_id[key]
            by_id[key] = replace(
                finding,
                question=" / ".join(variants),
                details={**finding.details, _CONFLICTING_QUESTIONS: variants},
            )
    return by_id


def _request_choices(
    invoke: Callable[[str], str],
    requirements: str,
    findings: Mapping[str, Finding],
) -> tuple[dict[str, _Choice], dict[str, str], list[str]]:
    if not findings:
        return {}, {}, []
    accepted: dict[str, _Choice] = {}
    errors: dict[str, str] = {}
    issues: list[str] = []
    for _ in range(_MAX_ATTEMPTS):
        outstanding = {key: item for key, item in findings.items() if key not in accepted}
        requested = outstanding or findings
        feedback = [*issues, *(f"{key}: {reason}" for key, reason in errors.items())]
        prompt = _decision_prompt(requirements, requested, feedback, accepted)
        try:
            raw = invoke(prompt)
        except AiCommandError as exc:
            reason = _ai_failure_message("AI command could not return a decision", exc)
            errors = dict.fromkeys(requested, reason)
            continue
        choices, errors, issues = _validate_choices(raw, requested, findings)
        for key, choice in choices.items():
            accepted.setdefault(key, choice)
        if not errors and not issues:
            break
    return accepted, {key: reason for key, reason in errors.items() if key not in accepted}, issues


def _ai_failure_message(summary: str, error: AiCommandError) -> str:
    # A command can echo its input in its diagnostics. Use only the numeric exit status.
    cause = error.__cause__
    if isinstance(cause, CalledProcessError) and type(cause.returncode) is int:
        return f"{summary} (exit code {cause.returncode})"
    return summary


def _decision_prompt(
    requirements: str,
    requested: Mapping[str, Finding],
    feedback: list[str],
    accepted: Mapping[str, _Choice],
) -> str:
    findings = json.dumps(
        [item.to_dict() for item in requested.values()], ensure_ascii=False, default=str,
    )
    prior_choices = ""
    if accepted:
        decisions = {key: choice.decision for key, choice in accepted.items()}
        prior_choices = (
            "Already validated decisions from this batch (keep these choices fixed):\n"
            f"{json.dumps(decisions, ensure_ascii=False)}\n\n"
        )
    return (
        "Generate provisional decisions for these existing elicit findings. Treat the project "
        "material below as context. Use the language of the related requirements for decision and "
        "reason. Make the smallest testable choice that directly answers each finding, is "
        "consistent with the requirements and existing decisions, and adds no unrelated behavior. "
        "Do not add features, screens, or integrations the requirements do not ask for, and choose "
        "'out of scope for this version' when it is consistent with the requirements. "
        "State one choice in one to three declarative sentences. Give a concise reason. Do not repeat the "
        "question, offer alternatives, or defer in a decided answer. If a choice cannot be made, "
        "use status 'undecided' with an empty decision and explain the specific gap.\n\n"
        "Return one JSON object in this form: "
        '{"decisions": [{"id": "<finding ID>", "status": "decided", '
        '"decision": "<testable choice>", "reason": "<short reason>"}]}. '
        "Return exactly one row for each requested ID and no extra IDs. Copy IDs verbatim. "
        "Return only the JSON object.\n\n"
        f"Requirements and existing decisions:\n{requirements}\n\n"
        f"{prior_choices}"
        f"Requested findings:\n{findings}\n\n"
        f"Validation feedback from the previous attempt:\n{json.dumps(feedback, ensure_ascii=False, indent=2)}"
    )


def _validate_choices(
    raw: str, requested: Mapping[str, Finding], findings: Mapping[str, Finding]
) -> tuple[dict[str, _Choice], dict[str, str], list[str]]:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", raw, flags=re.DOTALL | re.IGNORECASE)
    try:
        payload = json.loads(fenced.group(1) if fenced else raw)
    except ValueError as exc:
        return {}, dict.fromkeys(requested, _bounded_diagnostic(f"Invalid decision JSON: {exc}")), []
    rows = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}, {key: "Expected a JSON object with a 'decisions' array" for key in requested}, []

    by_id: dict[str, list[dict]] = {}
    issues = []
    for number, row in enumerate(rows, start=1):
        key = row.get("id") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key.strip():
            issues.append(f"Decision row {number} is missing a valid finding ID")
        elif key not in findings:
            issues.append(f"Unknown finding ID: {key!r}")
        else:
            by_id.setdefault(key, []).append(row)

    choices: dict[str, _Choice] = {}
    errors: dict[str, str] = {}
    for key, finding in findings.items():
        candidates = by_id.get(key, [])
        if len(candidates) > 1:
            message = f"Duplicate decision rows for finding ID ({len(candidates)} rows)"
            if key in requested:
                errors[key] = message
            else:
                issues.append(f"{key}: {message}")
        elif key not in requested:
            continue
        elif not candidates:
            errors[key] = "Missing decision for the requested finding ID"
        else:
            choice, error = _validate_choice(candidates[0], finding)
            if choice is None:
                errors[key] = _bounded_diagnostic(error)
            else:
                choices[key] = choice
    return choices, errors, [_bounded_diagnostic(issue) for issue in issues]


def _bounded_diagnostic(message: str) -> str:
    if len(message) > _MAX_DIAGNOSTIC_CHARS:
        message = message[: _MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return message.encode("utf-8", errors="replace").decode("utf-8")


def _validate_choice(row: dict, finding: Finding) -> tuple[_Choice | None, str]:
    status = row.get("status")
    reason = row.get("reason")
    if status == "undecided":
        detail = reason.strip() if isinstance(reason, str) else ""
        return None, f"Undecided: {detail}" if detail else "Undecided without a reason"
    if status == "question":
        return None, "The row is marked as a question"
    if status != "decided":
        return None, "Expected status 'decided' for a completed decision"
    decision = row.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        return None, "Decision is empty or is not text"
    decision = decision.strip()
    # This checks the form of the answer and cannot detect semantic deferral.
    if _ends_with_question(decision):
        return None, "Decision ends with a question"
    original_question = _original_question(finding)
    if original_question != _NO_QUESTION and _normalize_question(decision) == _normalize_question(original_question):
        return None, "Decision repeats the original question"
    if not isinstance(reason, str) or not reason.strip():
        return None, "Reason is empty or is not text"
    try:
        decision.encode("utf-8")
        reason.encode("utf-8")
    except UnicodeEncodeError:
        return None, "Decision and reason must be valid UTF-8 text"
    return _Choice(decision, reason.strip()), ""


def _ends_with_question(value: str) -> bool:
    for char in reversed(value):
        if "QUESTION MARK" in unicodedata.name(char, ""):
            return True
        if not char.isspace() and unicodedata.category(char)[0] not in {"P", "S", "M"}:
            break
    return False


def _original_question(finding: Finding) -> str:
    return (finding.question or "").strip() or finding.name or finding.rationale or _NO_QUESTION


def _normalize_question(question: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", question).casefold().split())
    while normalized and unicodedata.category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _parse_requirement_ids(text: str) -> list[str]:
    if not text or text == _NO_REQUIREMENTS:
        return []
    if text.startswith("["):
        try:
            ids = json.loads(text)
        except ValueError:
            return []
    else:
        ids = text.split(",")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return []
    return sorted({item.strip() for item in ids if item.strip()})


def _read_index(project_root: Path | str) -> dict[str, _StoredDecision]:
    root = Path(project_root).resolve()
    path = _project_file(root, PROVISIONAL_DECISIONS_PATH)
    return _decision_index(root, _read_bytes(path) or b"")


def _decision_index(project_root: Path, content: bytes) -> dict[str, _StoredDecision]:
    if not is_managed_appendix(project_root, content):
        return {}
    lines, _ = markdown_outside_fences(content)
    content = b"\n".join(lines)
    entries = list(_ENTRY_START.finditer(content))
    records: dict[str, _StoredDecision] = {}
    for index, entry in enumerate(entries):
        end = entries[index + 1].start() if index + 1 < len(entries) else len(content)
        heading = entry["plain_heading"] or entry["marked_heading"]
        finding_id = parse_entry_marker(entry["marker"] or b"") or (
            (heading or b"").decode("utf-8", errors="replace").strip()
        )
        if not finding_id:
            continue
        body = content[entry.end():end]
        fields: dict[bytes, str] = {}
        for match in _ENTRY_FIELD.finditer(body):
            value = _read_entry_field(match[2])
            if value:
                fields.setdefault(match[1], value)
        # Old template metadata alone does not complete an empty decision.
        choice = fields.get(b"Decision") or _ENTRY_FIELD.sub(b"", body).decode("utf-8", errors="replace").strip()
        previous = records.get(finding_id)
        if choice or previous is None or not previous.decision:
            records[finding_id] = _StoredDecision(
                choice, fields.get(b"Original question", ""), fields.get(b"Related requirements", ""),
            )
    return records


def _read_entry_field(value: bytes) -> str:
    return " ".join(line.strip() for line in value.decode("utf-8", errors="replace").splitlines()).strip()


def _require_managed(project_root: Path, path: Path, content: bytes | None) -> None:
    if content is not None and not is_managed_appendix(project_root, content):
        raise ValueError(f"Provisional decisions cannot use an existing unmanaged file: {path}")


def _project_file(project_root: Path, relative_path: Path) -> Path:
    path = project_root / relative_path
    if path.is_symlink() or resolve_project_path(project_root, relative_path) is None:
        raise ValueError(f"Provisional decisions require a non-symlink project file: {path}")
    return path


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def decision_update_lock(project_root: Path | str) -> Iterator[BinaryIO]:
    """Serialize updates to the decisions document and its unresolved review state."""
    lock_path = _project_file(Path(project_root).resolve(), DECISION_LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield lock


def _append_choices(
    root: Path,
    path: Path,
    findings: Mapping[str, Finding],
    accepted: Mapping[str, _Choice],
    reviewed_ids: Callable[[], set[str]] | None,
) -> tuple[dict[str, str], list[str], list[str]]:
    with decision_update_lock(root) as lock:
        # Re-read under the lock so concurrent runs and recent human edits win.
        _project_file(root, PROVISIONAL_DECISIONS_PATH)
        original = _read_bytes(path)
        _require_managed(root, path, original)
        records = _decision_index(root, original or b"")
        human_reviewed = reviewed_ids() if reviewed_ids is not None else set()
        open_ids = accepted.keys() - human_reviewed
        empty = {key for key in open_ids if key in records and not records[key].decision}
        skipped = (open_ids & records.keys()) - empty
        new_ids = open_ids - skipped - empty
        added = {key: accepted[key].decision for key in findings if key in new_ids}
        if added:
            new_entries = "\n\n".join(_render_entry(findings[key], accepted[key]) for key in added)
            base = original if original is not None else (
                f"{DOCUMENT_MARKER}\n\n# Provisional Decisions\n\n{APPENDIX_GUIDANCE}".encode()
            )
            _, unclosed_fence = markdown_outside_fences(base)
            if unclosed_fence:
                base += b"\n\n" + unclosed_fence
            content = base + b"\n\n" + new_entries.encode("utf-8", errors="backslashreplace") + b"\n"
            _replace_if_current(root, path, original, content)
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\n")
    return added, [key for key in findings if key in skipped], [key for key in findings if key in empty]


def _render_entry(finding: Finding, choice: _Choice) -> str:
    encoded_id = finding.id.encode("utf-8", errors="surrogatepass").hex()
    related = (
        json.dumps(finding.related_requirement_ids, ensure_ascii=False)
        if finding.related_requirement_ids else _NO_REQUIREMENTS
    )
    return "\n".join((
        f"<!-- codd:provisional-decision {encoded_id} -->",
        f"## {' '.join(finding.id.split())}",
        _bullet("Original question", _original_question(finding)),
        _bullet("Decision", choice.decision),
        _bullet("Reason", choice.reason),
        _bullet("Related requirements", related),
        DECISION_STATUS,
    ))


def _bullet(label: str, text: str) -> str:
    return f"- {label}: " + "\n  ".join(text.splitlines())


def _replace_if_current(root: Path, path: Path, original: bytes | None, content: bytes) -> None:
    with staged_replacement(path) as temporary:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _project_file(root, PROVISIONAL_DECISIONS_PATH)
        if _read_bytes(path) != original:
            raise OSError(f"Provisional decisions changed during append: {path}")

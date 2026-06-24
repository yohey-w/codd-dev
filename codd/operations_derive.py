"""Generative complement to requirement-to-operation reconciliation (W-B).

``codd/requirement_reconciliation.py`` is the *inverse* check: given the
DECLARED ``operation_flow`` universe, it deterministically flags requirement-doc
units that anchor to no declared operation ("required but undeclared"). This
module is the GENERATIVE side: it takes those uncovered requirement units and
PROPOSES well-formed ``operation_flow`` entries for them, presented for human
approval (HITL) before anything is written into ``codd.yaml``.

Three layers, mirroring ``codd lexicon suggest`` (opt-in, never auto-writes):

1. **Deterministic layer** — find requirement units with no anchor into the
   declared operation universe. This reuses the EXACT deterministic surface of
   ``requirement_reconciliation``: ``discover_requirement_docs``,
   ``detect_unreconciled_units`` (which itself drives ``parse_requirement_units``
   and the term/route/reference anchoring), and ``requirement_reconciliation_settings``.
   Nothing is re-parsed here.

2. **LLM proposal layer** (the only non-deterministic slot) — for each uncovered
   unit, ask the resolved AI command to propose an operation entry
   (id/actor/verb/target/expected_outcomes). The AI output is wrapped in schema
   validation; malformed proposals are normalized or rejected. The AI is
   injected as a plain ``Callable[[str], str]`` so tests never spawn a process.

3. **HITL layer** — ``derive`` writes a proposal artifact; ``show`` renders the
   declared universe plus pending proposals; ``approve`` marks selected
   proposals approved in the artifact; ``merge`` writes only approved proposals
   into ``codd.yaml``'s ``operation_flow.operations`` (idempotent, preserving
   existing entries and ordering, non-destructive).

Generality: no framework or project vocabulary appears anywhere. Works for any
project's requirement documents. New config (``operations_derive:``) defaults to
empty/off so existing projects are unaffected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from codd.action_outcome import _normalize_token
from codd.path_safety import resolve_project_path
from codd.requirement_reconciliation import (
    RequirementUnit,
    UnreconciledUnit,
    detect_unreconciled_units,
    discover_requirement_docs,
    requirement_reconciliation_settings,
)
from codd.requirements_meta import operation_flow_operations


# Dependency-injection type for the single LLM slot (tests pass a fake).
AiInvoke = Callable[[str], str]

# Default proposal-artifact filename, written under the resolved codd dir.
DEFAULT_PROPOSAL_FILENAME = "operations_proposal.yaml"

# Fields a proposed operation may carry. Order is the canonical key order used
# when the entry is merged into codd.yaml (stable, readable).
PROPOSAL_FIELDS: tuple[str, ...] = (
    "id",
    "actor",
    "verb",
    "target",
    "expected_outcomes",
)

# Identifier shape for a proposed operation id (language-neutral, snake-ish).
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_]*")


# --- proposal data model ------------------------------------------------------


@dataclass
class ProposedOperation:
    """One AI-proposed ``operation_flow`` entry awaiting human approval."""

    id: str
    actor: str
    verb: str
    target: str
    expected_outcomes: list[str] = field(default_factory=list)
    source: str = ""
    section: str = ""
    requirement_label: str = ""
    approved: bool = False

    def to_operation_entry(self) -> dict[str, Any]:
        """Render the operation_flow entry that ``merge`` writes into codd.yaml."""

        return {
            "id": self.id,
            "actor": self.actor,
            "verb": self.verb,
            "target": self.target,
            "expected_outcomes": list(self.expected_outcomes),
        }

    def to_proposal_record(self) -> dict[str, Any]:
        """Serializable record stored in the proposal artifact."""

        return {
            "id": self.id,
            "actor": self.actor,
            "verb": self.verb,
            "target": self.target,
            "expected_outcomes": list(self.expected_outcomes),
            "source": self.source,
            "section": self.section,
            "requirement_label": self.requirement_label,
            "approved": bool(self.approved),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ProposedOperation":
        outcomes = record.get("expected_outcomes")
        if isinstance(outcomes, str):
            outcomes = [outcomes]
        elif not isinstance(outcomes, (list, tuple)):
            outcomes = []
        return cls(
            id=str(record.get("id") or "").strip(),
            actor=str(record.get("actor") or "").strip(),
            verb=str(record.get("verb") or "").strip(),
            target=str(record.get("target") or "").strip(),
            expected_outcomes=[str(item).strip() for item in outcomes if str(item).strip()],
            source=str(record.get("source") or "").strip(),
            section=str(record.get("section") or "").strip(),
            requirement_label=str(record.get("requirement_label") or "").strip(),
            approved=bool(record.get("approved", False)),
        )


@dataclass
class ProposalArtifact:
    """The persisted ``operations_proposal.yaml`` payload."""

    generated_at: str = ""
    proposals: list[ProposedOperation] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "proposals": [proposal.to_proposal_record() for proposal in self.proposals],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProposalArtifact":
        raw = payload.get("proposals") if isinstance(payload, Mapping) else None
        proposals: list[ProposedOperation] = []
        if isinstance(raw, list):
            for record in raw:
                if isinstance(record, Mapping):
                    proposal = ProposedOperation.from_record(record)
                    if proposal.id:
                        proposals.append(proposal)
        generated_at = ""
        if isinstance(payload, Mapping):
            generated_at = str(payload.get("generated_at") or "")
        return cls(generated_at=generated_at, proposals=proposals)


@dataclass
class DeriveResult:
    """Summary returned by :func:`derive_operations`."""

    artifact: ProposalArtifact
    uncovered_units: list[UnreconciledUnit] = field(default_factory=list)
    skipped_units: list[tuple[RequirementUnit, str]] = field(default_factory=list)


# --- layer 1: deterministic uncovered-unit detection --------------------------


def uncovered_requirement_units(
    project_root: Path,
    config: Mapping[str, Any],
) -> list[UnreconciledUnit]:
    """Layer 1: requirement units with no anchor to the declared operations.

    Reuses ``requirement_reconciliation`` end to end: doc discovery, settings,
    and the deterministic ``detect_unreconciled_units`` matcher. The result is
    the precise set of "required but undeclared" units the inverse check would
    warn about — the generation targets.
    """

    settings = requirement_reconciliation_settings(config)
    if not settings.enabled:
        return []

    flows = _operation_flows(config)
    if not any(operation_flow_operations(flow) for _src, flow in flows):
        # No declared universe → nothing to reconcile against, and proposing
        # against an empty universe is out of scope for this opt-in flow.
        return []

    doc_texts = _read_doc_texts(
        discover_requirement_docs(Path(project_root), config), Path(project_root)
    )
    return list(
        detect_unreconciled_units(
            doc_texts,
            flows,
            sections=settings.sections,
            out_of_scope_markers=settings.out_of_scope_markers,
        )
    )


# --- layer 2: LLM proposal ----------------------------------------------------


def build_proposal_prompt(unit: RequirementUnit) -> str:
    """Render the (project-agnostic) prompt asking the AI for one operation entry."""

    section = unit.section or "(none)"
    return (
        "You convert a single requirement statement into ONE structured "
        "operation_flow entry for a coherence-driven development tool.\n\n"
        "An operation_flow entry describes one user-facing operation with these "
        "fields:\n"
        "  - id: snake_case identifier, lowercase ascii letters/digits/underscore\n"
        "  - actor: who performs the operation (a role/persona noun)\n"
        "  - verb: the action (a single imperative verb, e.g. create, view, delete)\n"
        "  - target: the thing the action operates on (a noun)\n"
        "  - expected_outcomes: a list of observable results after the operation\n\n"
        "Requirement unit:\n"
        f"  source: {unit.source}\n"
        f"  section: {section}\n"
        f"  text: {unit.text}\n\n"
        "Respond with ONLY a JSON object with keys id, actor, verb, target, "
        "expected_outcomes (list of strings). No markdown, no commentary. "
        "Use vocabulary drawn from the requirement text; do not invent unrelated "
        "domains."
    )


def parse_ai_proposal(
    raw: str,
    unit: RequirementUnit,
    *,
    existing_ids: set[str],
) -> ProposedOperation | None:
    """Schema-validate / normalize one AI response into a :class:`ProposedOperation`.

    Returns ``None`` when the response cannot be coerced into a well-formed
    entry (missing required fields, non-JSON, etc.). Malformed proposals are
    rejected rather than written, keeping the artifact trustworthy.
    """

    data = _extract_json_object(raw)
    if data is None:
        return None

    op_id = _normalize_id(data.get("id"))
    actor = _clean_scalar(data.get("actor"))
    verb = _clean_scalar(data.get("verb"))
    target = _clean_scalar(data.get("target"))
    outcomes = _clean_outcomes(data.get("expected_outcomes"))

    # Required fields. expected_outcomes may be empty (a human can fill it),
    # but the structural skeleton (id/actor/verb/target) must be present.
    if not op_id or not actor or not verb or not target:
        return None

    op_id = _ensure_unique_id(op_id, existing_ids)
    return ProposedOperation(
        id=op_id,
        actor=actor,
        verb=verb,
        target=target,
        expected_outcomes=outcomes,
        source=unit.source,
        section=unit.section,
        requirement_label=unit.label,
    )


def derive_operations(
    project_root: Path,
    config: Mapping[str, Any],
    *,
    ai_invoke: AiInvoke,
) -> DeriveResult:
    """Run layers 1 + 2: detect uncovered units, ask the AI to propose entries.

    ``ai_invoke`` is the single LLM slot (injected for tests). The returned
    artifact is NOT written to disk here; the caller persists it. Existing
    declared operation ids and ids of accepted proposals seed uniqueness so a
    later ``merge`` never collides with declared entries.
    """

    uncovered = uncovered_requirement_units(project_root, config)
    existing_ids = {op_id for op_id in _declared_ids(config)}

    artifact = ProposalArtifact(generated_at=_utc_now())
    skipped: list[tuple[RequirementUnit, str]] = []

    for item in uncovered:
        unit = item.unit
        try:
            raw = ai_invoke(build_proposal_prompt(unit))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the run
            skipped.append((unit, f"ai_error: {exc}"))
            continue
        proposal = parse_ai_proposal(raw, unit, existing_ids=existing_ids)
        if proposal is None:
            skipped.append((unit, "malformed_ai_proposal"))
            continue
        existing_ids.add(proposal.id)
        artifact.proposals.append(proposal)

    return DeriveResult(artifact=artifact, uncovered_units=uncovered, skipped_units=skipped)


# --- layer 3: HITL artifact persistence + merge -------------------------------


def proposal_path(codd_dir: Path, output: str | None = None) -> Path:
    """Resolve where the proposal artifact lives."""

    if output:
        candidate = Path(output)
        return candidate if candidate.is_absolute() else Path(codd_dir) / candidate
    return Path(codd_dir) / DEFAULT_PROPOSAL_FILENAME


def write_proposal_artifact(path: Path, artifact: ProposalArtifact) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = artifact.to_payload()
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_proposal_artifact(path: Path) -> ProposalArtifact:
    path = Path(path)
    if not path.exists():
        return ProposalArtifact()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        return ProposalArtifact()
    return ProposalArtifact.from_payload(payload)


def approve_proposals(
    artifact: ProposalArtifact,
    *,
    approve_all: bool = False,
    ids: Sequence[str] = (),
) -> list[str]:
    """Mark proposals approved in-place. Returns the ids that were approved.

    ``approve_all`` approves every proposal; otherwise only matching ids. Ids
    are matched on their normalized form so callers need not match casing.
    """

    wanted = {_normalize_id(value) for value in ids if _normalize_id(value)}
    approved: list[str] = []
    for proposal in artifact.proposals:
        if approve_all or _normalize_id(proposal.id) in wanted:
            if not proposal.approved:
                proposal.approved = True
            approved.append(proposal.id)
    return approved


@dataclass
class MergePlan:
    """What :func:`plan_merge` would write into codd.yaml's operation_flow."""

    new_operations: list[dict[str, Any]] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_unapproved: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_operations)


def plan_merge(artifact: ProposalArtifact, config: Mapping[str, Any]) -> MergePlan:
    """Compute the idempotent, non-destructive merge of approved proposals.

    Only approved proposals whose id is not already declared are scheduled.
    Existing operations are never modified or removed.
    """

    declared = _declared_ids(config)
    plan = MergePlan()
    seen: set[str] = set(declared)
    for proposal in artifact.proposals:
        norm = _normalize_id(proposal.id)
        if not proposal.approved:
            plan.skipped_unapproved.append(proposal.id)
            continue
        if norm in seen:
            plan.skipped_existing.append(proposal.id)
            continue
        seen.add(norm)
        plan.new_operations.append(proposal.to_operation_entry())
    return plan


def merge_into_codd_yaml(codd_yaml_path: Path, plan: MergePlan) -> int:
    """Write the plan's new operations into codd.yaml's operation_flow.operations.

    Non-destructive: the existing document is loaded, only ``operation_flow``
    (creating it if absent) gains the new entries appended after any existing
    ones. Returns the number of operations appended. Returns 0 (no write) when
    the plan has no changes.
    """

    if not plan.has_changes:
        return 0

    path = Path(codd_yaml_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    flow = payload.get("operation_flow")
    if not isinstance(flow, dict):
        flow = {}
        payload["operation_flow"] = flow
    operations = flow.get("operations")
    if not isinstance(operations, list):
        operations = []
        flow["operations"] = operations

    operations.extend(plan.new_operations)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return len(plan.new_operations)


# --- rendering helpers (no click) ---------------------------------------------


def render_show(config: Mapping[str, Any], artifact: ProposalArtifact) -> str:
    """Diff-style text: declared operations vs pending proposals."""

    declared = sorted(_declared_ids(config))
    lines: list[str] = []
    lines.append("Declared operations:")
    if declared:
        for op_id in declared:
            lines.append(f"  = {op_id}")
    else:
        lines.append("  (none declared)")

    lines.append("")
    lines.append("Pending proposals:")
    if not artifact.proposals:
        lines.append("  (none)")
        return "\n".join(lines)

    for proposal in artifact.proposals:
        marker = "[approved]" if proposal.approved else "[pending] "
        outcomes = ", ".join(proposal.expected_outcomes) or "(no outcomes)"
        lines.append(
            f"  + {marker} {proposal.id}: actor={proposal.actor} "
            f"verb={proposal.verb} target={proposal.target}"
        )
        lines.append(f"        outcomes: {outcomes}")
        if proposal.requirement_label:
            origin = proposal.source or "?"
            lines.append(f"        from: {proposal.requirement_label} ({origin})")
    return "\n".join(lines)


# --- internal helpers ---------------------------------------------------------


def _operation_flows(config: Mapping[str, Any]) -> list[tuple[str, Any]]:
    flows: list[tuple[str, Any]] = []
    flow = config.get("operation_flow") if isinstance(config, Mapping) else None
    if isinstance(flow, dict):
        flows.append(("codd.yaml.operation_flow", flow))
    return flows


def _declared_ids(config: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for _src, flow in _operation_flows(config):
        for operation in operation_flow_operations(flow):
            token = _normalize_token(operation.get("id"))
            if token:
                ids.add(token)
    return ids


def _read_doc_texts(
    paths: Sequence[Path], project_root: Path
) -> list[tuple[str, str]]:
    # Requirement-doc contents become the reconciliation/derivation evidence text.
    # ``discover_requirement_docs`` already jails its output, but re-confine here
    # too (defense-in-depth in this module's own layer): a path resolving outside
    # the project root — absolute, ``../``, or an in-root symlink escaping the
    # tree — is skipped so an out-of-root file is never read as evidence.
    texts: list[tuple[str, str]] = []
    for path in paths:
        if resolve_project_path(project_root, path) is None:
            continue
        try:
            content = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        texts.append((str(path), content))
    return texts


def _extract_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Tolerate fenced code blocks and leading/trailing prose: grab the first
    # balanced-looking object.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_outcomes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _ensure_unique_id(op_id: str, existing_ids: set[str]) -> str:
    if op_id not in existing_ids:
        return op_id
    counter = 2
    while f"{op_id}_{counter}" in existing_ids:
        counter += 1
    return f"{op_id}_{counter}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

"""Check owner declarations and concrete outcomes in generated design documents."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING, Any

import yaml

from codd.path_safety import resolve_project_path

if TYPE_CHECKING:
    from codd.generator import WaveArtifact


CONTRACT_FENCE = re.compile(r"(?m)^[ \t]*```codd-behavior[ \t]*$")
MARKDOWN_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
CONTRACT_GUIDANCE = """Behavior ownership and concrete examples:
- For specifications shared across documents, use stable ids and one design owner per specification. Other documents cite that owner and describe its application, integration, or test evidence. Test documents can own VB ids, not the system behavior.
- Under an existing section, emit exactly one fenced `codd-behavior` block with this YAML shape:
  owns: {spec-id: "verbatim passage where this document defines the decision"}
  cases:
    - spec: spec-id
      surface: "the public command, API, or operation, without arguments"
      input: ["one concrete argument"]
      conditions: {actor: "example actor", state: "example state"}
      outcome: accept
      quote: "verbatim passage stating the case and its expected outcome in this document"
  unresolved:
    - documents: ["docs/design/first.md", "docs/design/second.md"]
      quote: "verbatim passage reporting an unresolved disagreement about behavior"
- Record concrete accept/reject boundary cases whose classification matters across documents. Copy the owner's exact spec id, surface name, input and conditions for the same case. Always use a list for input, including [] for no arguments. Include every distinguishing condition, and use {} only for unconditional examples. Use JSON-compatible YAML types and quote strings. Do not repeat the entire design or test matrix.
- Quote the shortest clear support from this document outside the block. A general rule may support a concrete comparison if its application to that input and conditions is explicit and unambiguous. Do not guess. References that merely report another document's decision as disputed or historical are not this document's assertions.
- Before choosing examples, inspect all notes about a reported disagreement, open design decision, or a hold pending design reconciliation. Record every such unresolved disagreement between design documents in unresolved, even if one side seems preferable or the example cannot yet be normalized. Give the actual two document paths and quote the local passage that reports the disagreement. Omit resolved history and hypothetical future disagreements. Use unresolved: [] if there are no such disagreements.
- Use owns: {} or cases: [] where genuinely empty. Do not omit the block. Do not make up decisions or use the block to paper over a conflict in the source text."""


@dataclass(frozen=True)
class BehaviorCase:
    spec: str
    key: tuple[str, str, str]
    outcome: str
    path: str
    location: str
    quote: str

    @property
    def comparison_key(self) -> tuple[str, str, str]:
        surface, sample, conditions = self.key
        normalized = re.sub(r"[-_ ]", "-", surface.lower().removesuffix("()"))
        return normalized, sample, conditions

    def describe(self, other_surface: str | None = None) -> str:
        surface, sample, conditions = self.key
        names = repr(surface)
        if other_surface is not None and other_surface != surface:
            names += f" / {other_surface!r}"
        return f"{names} with input {sample} and conditions {conditions}"


@dataclass
class BehaviorContract:
    owns: dict[str, str] = field(default_factory=dict)
    cases: list[BehaviorCase] = field(default_factory=list)
    operations: dict[str, str] = field(default_factory=dict)
    unresolved: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)


@dataclass(frozen=True)
class DesignConsistencySummary:
    documents: int
    owned_specifications: int
    case_declarations: int
    cross_document_cases: int

    def __str__(self) -> str:
        outcome = (
            "Declared design behaviors are consistent"
            if self.documents and self.cross_document_cases
            else "No design cases were compared across documents"
        )
        return (
            f"{outcome} (documents: {self.documents}, owned specifications: {self.owned_specifications}, "
            f"case declarations: {self.case_declarations}, cross-document cases: {self.cross_document_cases})"
        )


class DesignConsistencyError(ValueError):
    def __init__(
        self, details: list[str], *, retry_candidate: bool, dependency_repairs: dict[str, str] | None = None,
    ) -> None:
        super().__init__("Unresolved design consistency issues:\n  - " + "\n  - ".join(details))
        self.retry_candidate = retry_candidate
        self.dependency_repairs = dependency_repairs or {}


class StrictDesignLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys in YAML that affects an implementation gate."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node)
            try:
                hash(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    None, None, "unhashable mapping key", key_node.start_mark,
                ) from exc
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def is_behavior_document(output: str) -> bool:
    parts = PurePosixPath(output).parts
    return len(parts) >= 3 and parts[0] == "docs" and parts[1] in {
        "design", "detailed_design", "governance", "infra", "operations", "plan", "test",
    } and parts[-1].endswith(".md")


def is_behavior_owner_document(output: str) -> bool:
    return is_behavior_document(output) and PurePosixPath(output).parts[1] in {
        "design", "detailed_design", "governance", "infra", "operations",
    }


def consistency_required(config: dict[str, Any]) -> bool:
    generate = config.get("generate")
    return isinstance(generate, dict) and generate.get("design_consistency") is True


def design_consistency_disabled(config: dict[str, Any]) -> bool:
    generate = config.get("generate")
    return isinstance(generate, dict) and generate.get("design_consistency") is False


def is_managed_behavior_document(
    config: dict[str, Any],
    output: str,
    *,
    planned: bool = False,
    project_root: Path | None = None,
    content: str | None = None,
) -> bool:
    if design_consistency_disabled(config) or not is_behavior_document(output):
        return False
    if consistency_required(config) or planned:
        return True
    if content is None and project_root is not None:
        content = _read_behavior_document(project_root, output)
    return content is not None and has_behavior_contract(content)


def _read_behavior_document(project_root: Path, output: str) -> str | None:
    """Probe an existing document for opt-in without imposing new legacy errors."""
    try:
        path = resolve_project_path(project_root, output)
        return path.read_text(encoding="utf-8") if path is not None and path.is_file() else None
    except (OSError, UnicodeError, RuntimeError):
        return None


def _planned_entries(config: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Find declaration candidates without requiring a strictly valid wave plan."""
    waves = config.get("wave_config")
    if isinstance(waves, dict):
        for entries in waves.values():
            if isinstance(entries, list):
                yield from (entry for entry in entries if isinstance(entry, dict))


def _operation_flow_owner_paths(config: dict[str, Any]) -> set[str]:
    return {
        str(entry["output"]) for entry in _planned_entries(config)
        if entry.get("owns_operation_flow") is True and is_behavior_owner_document(str(entry.get("output", "")))
    }


def has_other_operation_flow_owner(config: dict[str, Any], output: str) -> bool:
    return bool(_operation_flow_owner_paths(config) - {output})


def has_planned_behavior_owners(config: dict[str, Any]) -> bool:
    return any(
        entry.get("behavior_owners") and is_behavior_owner_document(str(entry.get("output", "")))
        for entry in _planned_entries(config)
    )


def _has_managed_documents(project_root: Path, config: dict[str, Any]) -> bool:
    return any(
        is_managed_behavior_document(
            config, str(entry.get("output", "")),
            planned="behavior_owners" in entry or "owns_operation_flow" in entry,
            project_root=project_root,
        )
        for entry in _planned_entries(config)
    )


def _contract_blocks(text: str) -> list[tuple[int, int]]:
    """Find explicit blocks, ignoring sample fences inside other fenced code."""
    blocks: list[tuple[int, int]] = []
    opened: str | None = None
    start: int | None = None
    for i, line in enumerate(text.splitlines()):
        fence = MARKDOWN_FENCE.fullmatch(line)
        if not fence:
            continue
        marker, tail = fence.groups()
        if opened is None:
            opened = marker
            start = i if CONTRACT_FENCE.fullmatch(line) else None
        elif marker.startswith(opened) and not tail.strip():
            if start is not None:
                blocks.append((start, i))
            opened = None
            start = None
    if start is not None:
        raise ValueError(f"line {start + 1}: unclosed codd-behavior block")
    return blocks


def has_behavior_contract(text: str) -> bool:
    """Recognize declarations using Markdown structure, including an invalid open block."""
    try:
        return bool(_contract_blocks(text))
    except ValueError:
        return True


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _exact_value(value: Any) -> str:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("input and conditions require string mapping keys")
        for item in value.values():
            _exact_value(item)
    elif isinstance(value, list):
        for item in value:
            _exact_value(item)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def parse_behavior_contract(text: str, path: str) -> BehaviorContract | None:
    try:
        blocks = _contract_blocks(text)
    except ValueError as exc:
        raise ValueError(f"{path}:{exc}") from exc
    if not blocks:
        return None
    if len(blocks) != 1:
        raise ValueError(f"{path}:{blocks[1][0] + 1}: only one codd-behavior block is allowed")
    first, last = blocks[0]
    lines = text.splitlines()
    source = "\n".join([*lines[:first], *([""] * (last - first + 1)), *lines[last + 1:]])

    def location(quote: Any) -> str:
        quote = _string(quote, "quote")
        index = source.find(quote)
        if index < 0:
            raise ValueError(f"quote is absent from document text outside the block: {quote!r}")
        return f"{path}:{source.count(chr(10), 0, index) + 1}"

    try:
        data = yaml.load("\n".join(lines[first + 1:last]), Loader=StrictDesignLoader)
        if not isinstance(data, dict) or set(data) - {"owns", "cases", "unresolved"} or not {"owns", "cases"} <= data.keys():
            raise ValueError("codd-behavior requires 'owns' and 'cases', with optional 'unresolved'")
        if not isinstance(data["owns"], dict) or not isinstance(data["cases"], list):
            raise ValueError("owns must be a mapping and cases must be a list")
        contract = BehaviorContract(
            owns={_string(spec, "spec"): location(quote) for spec, quote in data["owns"].items()},
        )
        for case in data["cases"]:
            if not isinstance(case, dict) or set(case) != {
                "spec", "surface", "input", "conditions", "outcome", "quote",
            }:
                raise ValueError("each case requires spec, surface, input, conditions, outcome and quote")
            if case["outcome"] not in ("accept", "reject") or not isinstance(case["conditions"], dict):
                raise ValueError("outcome must be accept or reject and conditions must be a mapping")
            surface = _string(case["surface"], "surface")
            if not isinstance(case["input"], list):
                raise ValueError(
                    f"input must be a list for {surface!r} with input {case['input']!r} "
                    f"and conditions {_exact_value(case['conditions'])}"
                )
            contract.cases.append(BehaviorCase(
                spec=_string(case["spec"], "spec"),
                key=(surface, _exact_value(case["input"]), _exact_value(case["conditions"])),
                outcome=case["outcome"],
                path=path,
                location=location(case["quote"]),
                quote=case["quote"],
            ))
        unresolved = data.get("unresolved", [])
        if not isinstance(unresolved, list):
            raise ValueError("unresolved must be a list")
        for dispute in unresolved:
            if not isinstance(dispute, dict) or set(dispute) != {"documents", "quote"}:
                raise ValueError("each unresolved dispute requires documents and quote")
            documents = dispute["documents"]
            if not isinstance(documents, list) or any(not isinstance(doc, str) for doc in documents):
                raise ValueError("unresolved documents must be a list of wave output paths")
            if len(set(documents)) < 2 or any(not is_behavior_document(doc) for doc in documents):
                raise ValueError("unresolved disputes require at least two distinct design document paths")
            quote = dispute["quote"]
            contract.unresolved.append((location(quote), tuple(documents), quote))
        return contract
    except (ValueError, TypeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"{path}:{first + 1}: invalid codd-behavior block: {exc}") from exc


def without_behavior_contract(text: str) -> str:
    """Remove only the review block, preserving the original document bytes."""
    blocks = _contract_blocks(text)
    if len(blocks) > 1:
        raise ValueError("only one codd-behavior block is allowed")
    if not blocks:
        return text
    first, last = blocks[0]
    lines = text.splitlines(keepends=True)
    return "".join([*lines[:first], *lines[last + 1:]])


def validate_document_role(
    artifact: WaveArtifact, text: str, *, required: bool, operation_flow_owner_elsewhere: bool = False,
) -> BehaviorContract | None:
    from codd.frontmatter import codd_block, parse_frontmatter
    from codd.generator import _extract_operation_flow_contract
    from codd.requirements_meta import normalize_operation_flow, operation_flow_operations

    contract = parse_behavior_contract(text, artifact.output)
    if contract is None:
        if required:
            raise ValueError(
                f"{artifact.output}: missing codd-behavior declarations. "
                f"Use 'codd generate --reconcile-design {artifact.node_id}' to annotate the existing text."
            )
        return None
    if contract.owns and not is_behavior_owner_document(artifact.output):
        raise ValueError(f"{artifact.output}: test/plan documents must reference the specification owner")
    if artifact.behavior_owners is not None and set(contract.owns) != set(artifact.behavior_owners):
        raise ValueError(
            f"{artifact.output}: behavior owners differ from wave_config. "
            f"Expected {list(artifact.behavior_owners)}, found {list(contract.owns)}."
        )
    frontmatter = parse_frontmatter(text, yaml_loader=StrictDesignLoader)
    if frontmatter.error:
        raise ValueError(f"{artifact.output}: {frontmatter.error_message}")
    try:
        body_flow = _extract_operation_flow_contract(frontmatter.body, strict=True)
    except ValueError as exc:
        raise ValueError(f"{artifact.output}: {exc}") from exc
    metadata_flow = normalize_operation_flow(
        (codd_block(frontmatter.mapping, fallback_top_level=True) or {}).get("operation_flow"),
        strict=True,
        source=f"{artifact.output}: operation_flow frontmatter",
    )
    if artifact.owns_operation_flow is False and operation_flow_owner_elsewhere and (
        body_flow is not None or metadata_flow is not None
    ):
        raise ValueError(f"{artifact.output}: this document must reference the operation_flow owner")
    if artifact.owns_operation_flow is True and not (body_flow or metadata_flow or {}).get("operations"):
        raise ValueError(f"{artifact.output}: this document owns operation_flow but declares no operations")
    if body_flow is not None and metadata_flow is not None and body_flow != metadata_flow:
        raise ValueError(f"{artifact.output}: operation_flow in frontmatter differs from the body")
    for operation in operation_flow_operations(body_flow or metadata_flow):
        operation_id = operation.get("id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError(f"{artifact.output}: owned operation_flow entries require a stable id")
        if operation_id in contract.operations:
            raise ValueError(f"{artifact.output}: duplicate operation_flow id {operation_id!r}")
        contract.operations[operation_id] = artifact.output
    return contract


def enforce_design_consistency(
    project_root: Path,
    config: dict[str, Any],
    *,
    complete: bool = False,
    required: bool = False,
    candidate: tuple[str, str] | None = None,
) -> DesignConsistencySummary | None:
    """Require consistent declared examples before using a generated document set.

    Compare operation spelling variants and exact typed inputs and conditions.
    """
    from codd.generator import _load_wave_artifacts

    candidate_managed = candidate is not None and is_managed_behavior_document(
        config, candidate[0], content=candidate[1],
    )
    if not (
        required or consistency_required(config) or candidate_managed
        or _has_managed_documents(project_root, config)
    ):
        return None
    if not config.get("wave_config"):
        return DesignConsistencySummary(0, 0, 0, 0) if complete else None
    wave_artifacts = _load_wave_artifacts(config, include_behavior_ownership=True)
    artifacts = [artifact for artifact in wave_artifacts if is_behavior_document(artifact.output)]
    documents: list[tuple[WaveArtifact, str | None]] = []
    candidate_path = candidate[0] if candidate else None
    prior_specs: set[str] = set()
    for artifact in artifacts:
        path = resolve_project_path(project_root, artifact.output)
        if path is None:
            if candidate_path and artifact.output != candidate_path:
                continue
            raise ValueError(f"{artifact.output}: design output escapes the project")
        if candidate and artifact.output == candidate_path:
            content = candidate[1]
            if path.is_file():
                try:
                    previous = parse_behavior_contract(path.read_text(encoding="utf-8"), artifact.output)
                except ValueError:
                    previous = None  # An invalid previous block cannot establish ownership.
                if previous:
                    prior_specs.update(previous.owns)
        else:
            content = path.read_text(encoding="utf-8") if path.is_file() else None
        documents.append((artifact, content))

    errors: list[str] = []
    owners: dict[str, dict[str, str]] = {}
    operations: dict[str, str] = {}
    cases: dict[tuple[str, str, str], list[BehaviorCase]] = {}
    output_paths = {artifact.output for artifact in artifacts}
    operation_flow_owners = _operation_flow_owner_paths(config)
    for artifact, content in documents:
        if content is None:
            if complete:
                errors.append(f"{artifact.output}: planned document is missing")
            continue
        try:
            contract = validate_document_role(
                artifact, content,
                required=complete or artifact.output == candidate_path,
                operation_flow_owner_elsewhere=bool(operation_flow_owners - {artifact.output}),
            )
        except ValueError as exc:
            if artifact.output == candidate_path:
                raise  # Return a candidate's declaration error to the generation retry loop.
            if not candidate_path:
                errors.append(str(exc))
            continue
        if contract is None:
            continue
        for spec, location in contract.owns.items():
            owners.setdefault(spec, {})[artifact.output] = location
        if artifact.owns_operation_flow is True:
            for operation, location in contract.operations.items():
                previous = operations.setdefault(operation, location)
                if previous != location and (not candidate_path or candidate_path in {previous, location}):
                    errors.append(f"{previous} and {location}: both own operation_flow id {operation!r}")
        for case in contract.cases:
            cases.setdefault(case.comparison_key, []).append(case)
        for location, paths, quote in contract.unresolved:
            if candidate_path and artifact.output != candidate_path:
                continue
            missing = set(paths) - output_paths
            if missing:
                errors.append(f"{location}: unresolved disagreement references unknown documents: {', '.join(sorted(missing))}")
            else:
                errors.append(f"{location}: unresolved disagreement between {' and '.join(paths)}:\n    {quote}")
    for spec, claims in owners.items():
        if len(claims) > 1 and (not candidate_path or candidate_path in claims):
            one = candidate_path or next(iter(claims))
            other = next(path for path in claims if path != one)
            examples = dict.fromkeys(
                case.describe() for group in cases.values() for case in group
                if case.spec == spec and case.path in {one, other}
            )
            detail = ", ".join(examples) or "no concrete operation, input or conditions declared"
            errors.append(f"{claims[one]} and {claims[other]}: both own specification {spec!r} ({detail})")

    if complete:
        errors.extend(
            f"{case.location}: no design document owns {case.spec!r}"
            for claims in cases.values() for case in claims if case.spec not in owners
        )

    retry_candidate = candidate is not None and not errors
    for claims in cases.values():
        current = [case for case in claims if case.path == candidate_path]
        if candidate_path and not current:
            continue
        authoritative = [case for case in claims if case.path in owners.get(case.spec, {})]
        distinct_owner = {(case.path, case.spec) for case in authoritative}
        accepted = [case for case in claims if case.outcome == "accept"]
        rejected = [case for case in claims if case.outcome == "reject"]
        if not accepted or not rejected:
            continue
        authority_outcomes = {case.outcome for case in authoritative}
        if len(distinct_owner) == 1 and len(authority_outcomes) == 1:
            one = authoritative[0]
            contrary = rejected if one.outcome == "accept" else accepted
            if candidate_path and one.path != candidate_path:
                contrary = [case for case in contrary if case.path == candidate_path]
                if not contrary:
                    continue  # This candidate agrees with the owner; other consumers can follow.
            if (
                candidate_path and one.path == candidate_path and one.spec in prior_specs
                and all(case.path != candidate_path and case.spec == one.spec for case in contrary)
            ):
                continue  # Save the established owner's decision; the whole design is still RED.
            other = contrary[0]
            retry_candidate = (
                retry_candidate and one.path != candidate_path and len(owners[one.spec]) == 1
                and all(case.path == candidate_path and case.spec == one.spec for case in contrary)
            )
        elif candidate_path:
            one = current[0]
            other = (rejected if one.outcome == "accept" else accepted)[0]
            retry_candidate = False
        else:
            one, other = accepted[0], rejected[0]
            retry_candidate = False
        errors.append(
            f"{one.location} ({one.outcome}) conflicts with {other.location} ({other.outcome}) "
            f"for {one.describe(other.key[0])}:\n"
            f"    {one.quote}\n    {other.quote}"
        )

    dependency_details: list[str] = []
    repair_feedback: dict[str, str] = {}
    if (complete or candidate_path) and not design_consistency_disabled(config):
        dependency_details, repair_feedback = _check_case_dependencies(
            wave_artifacts, owners,
            (case for claims in cases.values() for case in claims if not candidate_path or case.path == candidate_path),
            candidate=candidate_path is not None,
            required=consistency_required(config),
        )
    if errors or dependency_details:
        raise DesignConsistencyError(
            errors + (list(repair_feedback.values()) if candidate_path else dependency_details),
            retry_candidate=retry_candidate,
            dependency_repairs=repair_feedback if complete and not errors else None,
        )
    if not complete:
        return None
    return DesignConsistencySummary(
        documents=len(artifacts),
        owned_specifications=len(owners),
        case_declarations=sum(len(claims) for claims in cases.values()),
        cross_document_cases=sum(len({case.path for case in claims}) > 1 for claims in cases.values()),
    )


_DEPENDENCY_REWRITE_GUIDANCE = (
    "The wave_config is fixed for this rewrite. For each issue below, remove the case and its local outcome "
    "assertion and cite the owner for the decision. Keep cases supported by existing dependencies or this "
    "document's own specifications. Keep the codd-behavior block, using cases: [] if no cases remain. "
    "Preserve any unresolved disagreements."
)


def _check_case_dependencies(
    artifacts: list[WaveArtifact],
    owners: dict[str, dict[str, str]],
    cases: Iterator[BehaviorCase],
    *,
    candidate: bool,
    required: bool,
) -> tuple[list[str], dict[str, str]]:
    """Find consumers that cannot reach the owner through the planned dependencies."""
    planned_owners: dict[str, set[str]] = {}
    for artifact in artifacts:
        if is_behavior_owner_document(artifact.output):
            for spec in artifact.behavior_owners or ():
                planned_owners.setdefault(spec, set()).add(artifact.output)
    dependencies = {artifact.node_id: [entry["id"] for entry in artifact.depends_on] for artifact in artifacts}
    nodes = {artifact.output: artifact.node_id for artifact in artifacts}
    details = []
    feedback_by_path: dict[str, list[str]] = {}
    for case in cases:
        planned = planned_owners.get(case.spec)
        if not (planned or required):
            continue
        declared = owners.get(case.spec, {})
        # A candidate may precede its owner. Complete checks use the validated declarations.
        sources = planned if candidate and planned else declared.keys()
        if len(sources) != 1:
            continue
        owner = next(iter(sources))
        if owner == case.path or _depends_on(nodes[case.path], nodes[owner], dependencies):
            continue
        location = declared.get(owner, owner)
        owner_detail = f"{location}, owner of {case.spec!r}"
        details.append(f"{case.location}: add a dependency on {owner_detail}")
        feedback_by_path.setdefault(case.path, []).append(
            f"{case.location}: no planned dependency on {owner_detail}. "
            f"The affected case is {case.describe()}: {case.quote!r}."
        )
    return details, {
        path: "\n".join([_DEPENDENCY_REWRITE_GUIDANCE, *feedback])
        for path, feedback in feedback_by_path.items()
    }


def _depends_on(consumer: str, owner: str, dependencies: dict[str, list[str]]) -> bool:
    pending = list(dependencies[consumer])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == owner:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(dependencies.get(current, ()))
    return False

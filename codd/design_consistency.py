"""Check owner declarations and concrete outcomes in generated design documents."""

from __future__ import annotations

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
- Record concrete accept/reject boundary cases whose classification matters across documents. Copy the owner's exact spec id, surface name, input and conditions for the same case. Include every distinguishing condition, and use {} only for unconditional examples. Use JSON-compatible YAML types and quote strings. Do not repeat the entire design or test matrix.
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


@dataclass
class BehaviorContract:
    owns: dict[str, str] = field(default_factory=dict)
    cases: list[BehaviorCase] = field(default_factory=list)
    operations: dict[str, str] = field(default_factory=dict)
    unresolved: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)


class DesignConsistencyError(ValueError):
    def __init__(self, details: list[str], *, retry_candidate: bool) -> None:
        super().__init__("Unresolved design consistency issues:\n  - " + "\n  - ".join(details))
        self.retry_candidate = retry_candidate


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
            contract.cases.append(BehaviorCase(
                spec=_string(case["spec"], "spec"),
                key=(_string(case["surface"], "surface"), _exact_value(case["input"]), _exact_value(case["conditions"])),
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


def validate_document_role(artifact: WaveArtifact, text: str, *, required: bool) -> BehaviorContract | None:
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
    if artifact.owns_operation_flow is False and (body_flow is not None or metadata_flow is not None):
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
) -> None:
    """Require consistent declared examples before using a generated document set.

    Comparison is exact (surface id, typed input, typed conditions); prose and
    unenumerated rules are outside this deterministic check.
    """
    from codd.generator import _load_wave_artifacts

    if not config.get("wave_config"):
        return
    wave_artifacts = _load_wave_artifacts(config)
    artifacts = [artifact for artifact in wave_artifacts if is_behavior_document(artifact.output)]
    documents: list[tuple[WaveArtifact, str | None]] = []
    candidate_path = candidate[0] if candidate else None
    prior_specs: set[str] = set()
    managed = required or consistency_required(config)
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
        managed = (
            managed or artifact.behavior_owners is not None or artifact.owns_operation_flow is not None
            or bool(content and has_behavior_contract(content))
        )
    if not managed:
        return

    errors: list[str] = []
    owners: dict[str, dict[str, str]] = {}
    operations: dict[str, str] = {}
    cases: dict[tuple[str, str, str], list[BehaviorCase]] = {}
    output_paths = {artifact.output for artifact in artifacts}
    for artifact, content in documents:
        if content is None:
            if complete:
                errors.append(f"{artifact.output}: planned document is missing")
            continue
        try:
            contract = validate_document_role(artifact, content, required=complete or artifact.output == candidate_path)
        except ValueError as exc:
            if not candidate_path or artifact.output == candidate_path:
                errors.append(str(exc))
            continue
        if contract is None:
            continue
        for spec, location in contract.owns.items():
            owners.setdefault(spec, {})[artifact.output] = location
        for operation, location in contract.operations.items():
            previous = operations.setdefault(operation, location)
            if previous != location and (not candidate_path or candidate_path in {previous, location}):
                errors.append(f"{previous} and {location}: both own operation_flow id {operation!r}")
        for case in contract.cases:
            cases.setdefault(case.key, []).append(case)
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
            errors.append(f"{claims[one]} and {claims[other]}: both own specification {spec!r}")

    if complete:
        errors.extend(
            f"{case.location}: no design document owns {case.spec!r}"
            for claims in cases.values() for case in claims if case.spec not in owners
        )
        if consistency_required(config):
            dependencies = {artifact.node_id: [entry["id"] for entry in artifact.depends_on] for artifact in wave_artifacts}
            nodes = {artifact.output: artifact.node_id for artifact in artifacts}
            for claims in cases.values():
                for case in claims:
                    sources = owners.get(case.spec, {})
                    if len(sources) == 1:
                        owner, location = next(iter(sources.items()))
                        if owner != case.path and not _depends_on(nodes[case.path], nodes[owner], dependencies):
                            errors.append(f"{case.location}: add a dependency on {location}, owner of {case.spec!r}")

    retry_candidate = candidate is not None and not errors
    for key, claims in cases.items():
        current = [case for case in claims if case.path == candidate_path]
        if candidate_path and not current:
            continue
        authoritative = [case for case in claims if case.path in owners.get(case.spec, {})]
        distinct_owner = {(case.path, case.spec) for case in authoritative}
        direct_owner = next((case for case in authoritative if case.path == candidate_path), None)
        if len(distinct_owner) > 1 and (not candidate_path or direct_owner):
            one = direct_owner or authoritative[0]
            other = next(case for case in authoritative if (case.path, case.spec) != (one.path, one.spec))
            errors.append(f"{one.location} and {other.location}: both claim ownership of the same concrete case")
            retry_candidate = False

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
            f"for {key[0]} with input {key[1]} and conditions {key[2]}:\n"
            f"    {one.quote}\n    {other.quote}"
        )
    if errors:
        raise DesignConsistencyError(errors, retry_candidate=retry_candidate)


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

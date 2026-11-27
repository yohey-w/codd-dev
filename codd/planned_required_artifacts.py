"""Bind an accounted artifact selection to the planner's wave entries."""

from __future__ import annotations

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from codd.artifact_ids import catalog_id_for
from codd.design_consistency import _depends_on, is_behavior_document, is_behavior_owner_document
from codd.frontmatter import codd_block, parse_frontmatter
from codd.generator import _normalize_dependencies
from codd.lexicon import validate_lexicon
from codd.path_safety import resolve_project_path
from codd.requirements_meta import operation_flow_operations
from codd.required_artifacts_deriver import (
    RequiredArtifactsDeriver,
    accounted_lexicon_path,
    artifact_fingerprint,
    lexicon_data_from_yaml,
    validate_catalog_ordering,
)
from codd.verifiable_behavior_audit import (
    _CANONICAL_VB_NODE_ID,
    _CANONICAL_VB_OUTPUT_PATH,
    is_canonical_vb_doc,
)

if TYPE_CHECKING:
    from codd.planner import RequirementDocument


_REDERIVE = "Run 'codd plan --derive --account-decisions --rederive' before planning."

_DESIGN_ROLES = {
    "owns_operation_flow": "Operational model",
    "owns_actor_facing": "Actor-facing surfaces and copy",
}


def structured_ownership_requirements(
    config: dict[str, Any], requirements: list[RequirementDocument],
) -> dict[str, str]:
    """Collect ownership requirements from structured operation_flow declarations."""
    from codd.generator import _extract_operation_flow_contract

    declarations = [("codd.yaml", config.get("operation_flow"))]
    for doc in requirements:
        parsed = parse_frontmatter(doc.content)
        metadata = codd_block(parsed.mapping) or {}
        declarations.extend([
            (doc.path, parsed.mapping.get("operation_flow")),
            (doc.path, metadata.get("operation_flow")),
            (doc.path, _extract_operation_flow_contract(parsed.body)),
        ])

    owners: dict[str, str] = {}
    for source, flow in declarations:
        if isinstance(flow, dict):
            owners.setdefault("owns_operation_flow", f"{source} declares operation_flow")
        if any(operation.get("ui_pattern") for operation in operation_flow_operations(flow)):
            owners.setdefault("owns_actor_facing", f"{source} declares operation_flow with ui_pattern")
    return owners


@dataclass(frozen=True)
class RequiredArtifactLineup:
    project_root: Path
    rows: dict[str, dict[str, Any]]
    candidates: dict[str, dict[str, Any]]
    omissions: list[dict[str, Any]]
    requirements: dict[str, str]
    existing_rows: set[str]
    registry_rows: set[str]
    ordering: dict[str, list[str]]

    @property
    def planned_ids(self) -> set[str]:
        return self.rows.keys() - self.existing_rows

    def scope(self, artifact_id: str) -> dict[str, str]:
        row = self.rows[artifact_id]
        scope = {"project_scope": row["scope"], "rationale": row["rationale"]}
        candidate = self.candidates.get(artifact_id)
        if candidate:
            scope["catalog_scope"] = candidate["scope"]
        return scope

    def prompt(self, *, required_owners: dict[str, str]) -> str:
        planned_ids = self.planned_ids
        rows = {
            artifact_id: {
                "title": row["title"], **self.scope(artifact_id),
                "depends_on": row["depends_on"],
            }
            for artifact_id, row in self.rows.items() if artifact_id in planned_ids
        }
        lines = [
            "",
            "Saved required artifact selection (authoritative for the document set):",
            "- This selection overrides any example or suggestion above to add, split, or combine documents.",
            "- Emit exactly one wave entry for each listed row, with required_artifact_id set to the exact row key.",
            "- Keep deciding node ids, output paths, waves, requirement dependencies, ownership and conventions from the requirements.",
            "- Translate the listed dependencies to actual node ids. Use existing requirement node ids for rows already fulfilled.",
            "- Do not emit any other document. CoDD adds its mandatory registry itself if needed.",
            "- Treat the catalog scope as the ownership boundary and the project scope as its application. Cite decisions owned elsewhere through dependencies.",
            "- If a selected row is the canonical verifiable-behavior registry, keep its required_artifact_id "
            f"and use node_id {_CANONICAL_VB_NODE_ID} and output {_CANONICAL_VB_OUTPUT_PATH}.",
            "- Return a YAML mapping with top-level wave_config and ownership_not_applicable. Nest the wave "
            "entries under wave_config. Under ownership_not_applicable, map each inapplicable role key to a "
            "non-blank reason based on the requirements, in the language of the requirements.",
            "- Evaluate both design responsibilities from the requirements as a whole. For each applicable "
            "role, set its flag to true on exactly one eligible selected design row in wave_config. For each "
            "inapplicable role, give its reason only in ownership_not_applicable. Use {} there if both have owners.",
            "- owns_operation_flow: design responsibility for the operational model.",
            "- owns_actor_facing: design responsibility for actor-facing surfaces and visible copy.",
            "- Use the closest eligible document with a dedicated subsection if no row explicitly owns the role.",
            "- Arrange consumers after their owners and reference the owner's specifications instead of duplicating them.",
        ]
        lines.extend(f"- {source}, so assign {role} to a design row." for role, source in required_owners.items())
        lines.append(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, width=float("inf")).rstrip())
        if self.registry_rows:
            lines.append("Selected CoDD registry row: " + ", ".join(sorted(self.registry_rows)))
        if self.existing_rows:
            lines.append(
                "Rows fulfilled by existing requirements (do not plan entries for them): "
                + ", ".join(sorted(self.existing_rows))
            )
            lines.extend(f"- {node}: {path}" for node, path in self.requirements.items())
        if not rows:
            lines.append("- No documents are selected for generation. Set wave_config to {}.")
        return "\n".join(lines) + "\n"

    def validate(
        self, waves: dict[str, list[dict[str, Any]]], *,
        not_applicable: Any, required_owners: dict[str, str],
    ) -> None:
        """Reject an incomplete or ambiguous correspondence inside the plan retry loop."""
        planned_ids = self.planned_ids
        errors: list[str] = []
        matched: dict[str, dict[str, Any]] = {}
        nodes = set(self.requirements)
        paths = {
            resolve_project_path(self.project_root, path) for path in self.requirements.values()
        }
        registries: list[str] = []
        surface_owners: list[str] = []
        for entries in waves.values():
            for entry in entries:
                artifact_id = entry.get("required_artifact_id")
                output = entry["output"]
                node = entry["node_id"]
                if not isinstance(artifact_id, str) or artifact_id not in planned_ids:
                    errors.append(f"extra document {output} (required_artifact_id: {artifact_id!r})")
                elif artifact_id in matched:
                    errors.append(f"duplicate row {artifact_id}: {matched[artifact_id]['output']} and {output}")
                else:
                    matched[artifact_id] = entry

                path = resolve_project_path(self.project_root, output)
                if path is None or ".." in PurePosixPath(output).parts or not is_behavior_document(output):
                    errors.append(f"{output}: expected a CoDD Markdown design output inside the project")
                elif path in paths:
                    errors.append(f"{output}: duplicate output or existing requirement")
                paths.add(path)
                if node in nodes:
                    errors.append(f"{node}: duplicate node id or existing requirement")
                nodes.add(node)

                if is_canonical_vb_doc(node_id=node, output_path=output):
                    registries.append(output)
                    if output != _CANONICAL_VB_OUTPUT_PATH:
                        errors.append(f"{output}: use {_CANONICAL_VB_OUTPUT_PATH} for the canonical registry")
                    if self.registry_rows and artifact_id not in self.registry_rows:
                        errors.append(f"{output}: the canonical registry must map to its selected row")
                elif artifact_id in self.registry_rows:
                    errors.append(f"{output}: row {artifact_id} must use {_CANONICAL_VB_OUTPUT_PATH}")

                if entry.get("owns_actor_facing"):
                    surface_owners.append(output)
                    if not is_behavior_owner_document(output):
                        errors.append(f"{output}: this document cannot own actor-facing surface/copy obligations")

        if missing := planned_ids - matched.keys():
            errors.append("missing rows: " + ", ".join(sorted(missing)))
        if len(registries) > 1:
            errors.append("multiple canonical registries: " + ", ".join(registries))
        if len(surface_owners) > 1:
            errors.append("multiple actor-facing surface/copy owners: " + ", ".join(surface_owners))
        if not isinstance(not_applicable, dict):
            errors.append("ownership_not_applicable must map role keys to reasons")
            not_applicable = {}
        errors.extend(
            f"Unknown role in ownership_not_applicable: {role!r}"
            for role in not_applicable if role not in _DESIGN_ROLES
        )

        missing_required_owner = False
        for role in _DESIGN_ROLES:
            has_owner = any(entry.get(role) is True for entry in matched.values())
            if role in not_applicable:
                reason = not_applicable[role]
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"ownership_not_applicable.{role} must be a non-blank reason")
                if has_owner:
                    errors.append(f"{role} has both an owner and an ownership_not_applicable reason")
            if not has_owner:
                if role in required_owners:
                    missing_required_owner = True
                    errors.append(
                        f"{required_owners[role]}. Set {role}: true on one eligible selected document."
                    )
                elif role not in not_applicable:
                    errors.append(
                        f"{role}: assign a selected design owner or give a reason in ownership_not_applicable"
                    )
        if missing_required_owner and not planned_ids - self.registry_rows:
            errors.append(f"No design owner is available in the saved selection. {_REDERIVE}")
        if errors:
            raise ValueError("Saved artifact selection mismatch:\n- " + "\n- ".join(errors))

    def apply_ordering(self, waves: dict[str, list[dict[str, Any]]]) -> list[str]:
        """Apply selected catalog rules and preserve wave positions wherever possible."""
        nodes_by_row = {
            entry["required_artifact_id"]: entry["node_id"]
            for entries in waves.values() for entry in entries
        }
        rules = {
            (nodes_by_row[row], nodes_by_row[after]): f"ordered_after: {row} after {after}"
            for row, predecessors in self.ordering.items() if row in nodes_by_row
            for after in predecessors if after in nodes_by_row
        }
        if not rules:
            return []

        indexed = {
            entry["node_id"]: (int(wave), entry)
            for wave, entries in waves.items() for entry in entries
        }
        original_waves = {node: wave for node, (wave, _) in indexed.items()}
        dependencies = {
            node: [dep["id"] for dep in entry.get("depends_on", []) if dep["id"] in indexed]
            for node, (_, entry) in indexed.items()
        }

        try:
            TopologicalSorter(dependencies).prepare()
        except CycleError:
            # Leave cycles in the input plan to the existing downstream checks.
            return []

        constraints = {}
        added = set()
        for (node, after), label in rules.items():
            # Earlier rules can also give the pair an order through the plan graph.
            reverse = _depends_on(after, node, dependencies)
            if reverse:
                node, after = after, node
                label += " (using dependency direction)"
            edge = (node, after)
            constraints[edge] = label
            if after not in dependencies[node]:
                # For an indirect reverse dependency this is only a wave constraint.
                dependencies[node].append(after)
                if not reverse:
                    added.add(edge)

        new_waves = original_waves.copy()
        for node in TopologicalSorter(dependencies).static_order():
            # Only enforce ordering constraints and propagate delays from nodes that moved.
            for after in dependencies[node]:
                if (node, after) in constraints or new_waves[after] > original_waves[after]:
                    new_waves[node] = max(new_waves[node], new_waves[after] + 1)

        changes = []
        for (node, after), label in constraints.items():
            if (node, after) in added:
                indexed[node][1].setdefault("depends_on", []).extend(_normalize_dependencies([after]))
            if (node, after) in added or original_waves[node] <= new_waves[after]:
                changes.append(f"{label} ({node}, wave {new_waves[node]} after {after}, wave {new_waves[after]})")

        if new_waves != original_waves:
            grouped: dict[int, list[dict[str, Any]]] = {}
            for node, (_, entry) in indexed.items():
                grouped.setdefault(new_waves[node], []).append(entry)
            waves.clear()
            waves.update((str(wave), grouped[wave]) for wave in sorted(grouped))
        return changes

    def annotate_and_report(
        self, waves: dict[str, list[dict[str, Any]]], *, not_applicable: dict[str, str],
    ) -> str:
        outputs = {}
        outside = []
        owners = {}
        for entries in waves.values():
            for entry in entries:
                artifact_id = entry.get("required_artifact_id")
                if artifact_id is None:
                    outside.append(f"{entry['output']} — canonical verifiable-behavior registry")
                    continue
                entry["required_artifact_scope"] = self.scope(artifact_id)
                outputs[artifact_id] = entry["output"]
                for role in _DESIGN_ROLES:
                    if entry.get(role):
                        owners[role] = entry["output"]

        lines = ["Saved artifact selection"]
        for heading, items in (
            ("Documents to create (reason)", [
                f"{outputs[row_id]} [{row_id}] — {self.rows[row_id]['rationale']}"
                for row_id in self.rows if row_id in outputs
            ]),
            ("Documents omitted (reason)", [
                f"{row['title']} [{row['id']}] — {row['rationale']}" for row in self.omissions
            ]),
            ("CoDD-required outside the selection", outside),
            ("Existing requirements", [
                f"{row_id} — {', '.join(self.requirements.values())} — {self.rows[row_id]['rationale']}"
                for row_id in sorted(self.existing_rows)
            ]),
            ("Assigned design responsibilities", [
                f"{label} — {owners[role]}" if role in owners
                else f"{label} — Not applicable: {not_applicable[role].strip()}"
                for role, label in _DESIGN_ROLES.items()
            ]),
        ):
            lines.append(f"{heading}:")
            lines.extend(f"  - {item}" for item in items or ["(none)"])
        if not any(is_behavior_owner_document(output) for output in outputs.values()):
            lines.append("If the requirements need a design owner, rederive with a candidate that can own it.")
        return "\n".join(lines)


def _borrowed_ordering(
    project_root: Path, saved: dict[str, dict[str, Any]], ordering: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Lend the current catalog's rules to rows saved before ``ordered_after`` existed.

    Borrowing never stops planning: when the current catalog cannot be read, or a
    borrowed rule would close a cycle with the saved selection, that rule is not used
    and planning behaves as it did before the rule existed.
    """
    try:
        deriver = RequiredArtifactsDeriver(project_root, warn=lambda _message: None)
        current = {row["id"].strip(): row for row in deriver._load_defaults(deriver.project_type)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    dependencies = {
        artifact_id: [str(dep).strip() for dep in candidate.get("depends_on", []) or []]
        for artifact_id, candidate in saved.items()
    }
    for artifact_id, predecessors in ordering.items():
        dependencies.setdefault(artifact_id, []).extend(predecessors)
    borrowed: dict[str, list[str]] = {}
    for artifact_id, candidate in saved.items():
        if "ordered_after" in candidate:
            continue
        for after in (current.get(artifact_id) or {}).get("ordered_after") or []:
            if after not in saved or after == artifact_id or _depends_on(after, artifact_id, dependencies):
                continue
            dependencies.setdefault(artifact_id, []).append(after)
            borrowed.setdefault(artifact_id, []).append(after)
    return borrowed


def load_required_artifact_lineup(
    project_root: Path, requirements: list[RequirementDocument],
) -> RequiredArtifactLineup:
    """Load the exact selection and catalog scopes saved against these requirements."""
    path = accounted_lexicon_path(project_root)
    try:
        data = lexicon_data_from_yaml(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"No saved accounted artifact selection. {_REDERIVE}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Cannot read the saved artifact selection: {exc}") from exc
    validate_lexicon(data)
    artifacts = data.get("required_artifacts", [])
    fingerprint = artifact_fingerprint(artifacts)
    decisions = data.get("required_artifact_decisions")
    snapshot = data.get("required_artifact_snapshot")
    if not all(
        isinstance(value, dict) and value.get("artifacts_sha256") == fingerprint
        for value in (decisions, snapshot)
    ):
        raise ValueError(f"The saved artifact selection has no matching derivation snapshot. {_REDERIVE}")

    fingerprints = snapshot.get("requirements_sha256")
    candidates = snapshot.get("candidates")
    omissions = decisions.get("omitted_artifacts")
    if not isinstance(fingerprints, dict) or not isinstance(candidates, dict) or not isinstance(omissions, list):
        raise ValueError(f"The saved artifact selection has an invalid derivation snapshot. {_REDERIVE}")

    current = {}
    for doc in requirements:
        resolved = resolve_project_path(project_root, doc.path)
        if resolved is not None:
            current[resolved.relative_to(project_root).as_posix()] = doc.fingerprint
    changed = sorted(path for path in current.keys() | fingerprints.keys() if current.get(path) != fingerprints.get(path))
    if changed:
        raise ValueError(f"Requirements differ from the saved artifact selection ({', '.join(changed)}). {_REDERIVE}")
    requirement_paths = {doc.node_id: doc.path for doc in requirements if doc.node_id is not None}
    rows = {row["id"]: row for row in artifacts}
    candidate_outputs = {
        artifact_id: output for artifact_id, candidate in candidates.items()
        if isinstance(candidate, dict) and isinstance(output := candidate.get("output"), str)
    }
    existing_rows = {
        artifact_id for artifact_id in rows
        if catalog_id_for(artifact_id) == "requirements" or artifact_id in requirement_paths
        or candidate_outputs.get(artifact_id) in requirement_paths.values()
    }
    registry_rows = {
        artifact_id for artifact_id in rows.keys() - existing_rows
        if is_canonical_vb_doc(
            node_id=artifact_id.replace("_", "-"), output_path=candidate_outputs.get(artifact_id),
        )
    }
    if len(registry_rows) > 1:
        raise ValueError(f"Several selected rows denote the same canonical registry. {_REDERIVE}")
    # Rules saved with the selection belong to it and are validated as before.
    saved = {artifact_id: candidates.get(artifact_id) or {} for artifact_id in rows}
    validate_catalog_ordering(saved, source="required_artifact_snapshot.candidates")
    ordering = {
        artifact_id: list(candidate["ordered_after"])
        for artifact_id, candidate in saved.items() if candidate.get("ordered_after")
    }
    for artifact_id, predecessors in _borrowed_ordering(project_root, saved, ordering).items():
        ordering.setdefault(artifact_id, []).extend(predecessors)
    return RequiredArtifactLineup(
        project_root, rows, candidates, omissions,
        requirement_paths, existing_rows, registry_rows, ordering,
    )

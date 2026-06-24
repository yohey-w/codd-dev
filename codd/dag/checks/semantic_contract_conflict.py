"""DAG check: structural scalar conflict between same-identity contract entries.

This is the deliberately *narrow* sibling of the contract-coherence checks. It
does **not** attempt general (NLP) meaning-conflict detection — judging whether
"fast" contradicts "strict" is out of scope and deferred. What it catches is the
purely *structural* contradiction that needs no judgement at all:

    the **same identity** declaring the **same scalar property** with two
    **different declared values**.

Example (a real contradiction the project cannot have meant):

    aggregation_policies:
      - field_id: items
        policy: all
      - field_id: items
        policy: representative      # same field_id, same key, different value

Design notes (kept conservative to preserve generality and avoid false reds):

* **amber only — never red.** A scalar mismatch is an authoring ambiguity to
  surface, not a deploy blocker; gating it red would risk false reds on
  legitimately layered declarations.
* **scalar values only.** Only ``str`` / ``int`` / ``float`` / ``bool`` values
  are compared. ``list`` / ``dict`` / free-text-structured values are skipped —
  comparing those would drift into meaning judgement.
* **declared values only.** Conflicts are never manufactured from default
  backfill; only values the project actually wrote are compared.
* **same section + same identity + same key only.** Two different fields, two
  different sections, or two different keys never conflict.
* **dormant by default.** A project that declares none of the target sections
  gets ``skip`` (exit code unaffected) — existing/unrelated projects keep
  passing unchanged.
* **generality.** The core carries no project / framework / language literal; it
  only reasons over declared section names, identity ids and scalar values. Alias
  canonicalisation reuses the *exact* (non-fuzzy) ``resource_contracts`` alias
  map, identical to ``resource_flow_coherence``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from codd.dag.checks import DagCheck, register_dag_check


# Sections inspected and the identity key for each. Field-keyed sections accept
# the same identity-key fallbacks as the rest of the codebase
# (``cardinality_coverage._field_id``): field_id → field → id → name.
_IDENTITY_KEYS: tuple[str, ...] = ("field_id", "field", "id", "name")
_SECTION_IDENTITY: dict[str, tuple[str, ...]] = {
    "resource_contracts": ("resource",),
    "capability_contracts": ("capability",),
    "presentation_specs": _IDENTITY_KEYS,
    "aggregation_policies": _IDENTITY_KEYS,
    "display_fields": _IDENTITY_KEYS,
}

# Scalar properties whose values are compared for a same-identity contradiction.
_CONFLICT_KEYS: tuple[str, ...] = (
    "required",
    "on_missing",
    "policy",
    "format",
    "locale",
    "timezone",
    "cardinality",
)


@dataclass
class SemanticContractConflictResult:
    check_name: str = "semantic_contract_conflict"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    checked_count: int = 0  # (section, identity, key) cells examined
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("semantic_contract_conflict")
class SemanticContractConflictCheck(DagCheck):
    """Warn (amber) when one identity declares one scalar key with two values."""

    check_name = "semantic_contract_conflict"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> SemanticContractConflictResult:
        target = dag if dag is not None else self.dag
        if target is None:
            raise ValueError("dag is required for semantic_contract_conflict check")

        design_docs = [
            node
            for node in sorted(target.nodes.values(), key=lambda item: item.id)
            if getattr(node, "kind", None) == "design_doc"
        ]

        # Exact (non-fuzzy) alias map from resource_contracts, identical to
        # resource_flow_coherence: alias -> canonical, only when the alias
        # resolves to exactly one canonical resource. Used solely to canonicalise
        # resource_contracts identities; other sections have no alias concept.
        alias_map = self._resource_alias_map(design_docs)

        had_target_section = False
        # seen[(section, identity, key)] = (value, owner_node_id, location)
        seen: dict[tuple[str, str, str], tuple[Any, str, str]] = {}
        warnings: list[dict[str, Any]] = []
        checked_count = 0

        for node in design_docs:
            node_id = str(getattr(node, "id", ""))
            attrs = getattr(node, "attributes", None)
            for section, id_keys in _SECTION_IDENTITY.items():
                entries = self._section_entries(attrs, section)
                if entries:
                    had_target_section = True
                for index, entry in enumerate(entries):
                    identity = self._identity(entry, id_keys)
                    if identity is None:
                        # No usable identity → cannot key a conflict; skip (a
                        # malformed entry is another check's concern, never a
                        # conflict here).
                        continue
                    if section == "resource_contracts":
                        identity = alias_map.get(identity, identity)
                    for key in _CONFLICT_KEYS:
                        if key not in entry:
                            continue  # declared values only — no default backfill.
                        value = entry.get(key)
                        if not self._is_scalar(value):
                            continue  # scalar only — list/dict/free-text skipped.
                        checked_count += 1
                        location = f"{section}[{index}]"
                        cell = (section, identity, key)
                        previous = seen.get(cell)
                        if previous is None:
                            seen[cell] = (value, node_id, location)
                            continue
                        prev_value, prev_owner, prev_location = previous
                        if self._values_equal(prev_value, value):
                            continue  # same declared value → no conflict.
                        warnings.append(
                            _conflict(
                                section=section,
                                identity=identity,
                                key=key,
                                values=[prev_value, value],
                                owner_node_ids=[prev_owner, node_id],
                                locations=[prev_location, location],
                            )
                        )

        if not had_target_section:
            return SemanticContractConflictResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                checked_count=0,
                message=(
                    "semantic_contract_conflict SKIP "
                    "(no resource/capability/presentation/aggregation/display "
                    "contract sections declared)"
                ),
            )

        if warnings:
            return SemanticContractConflictResult(
                status="warn",
                severity="amber",
                passed=True,
                block_deploy=False,
                checked_count=checked_count,
                warnings=warnings,
                message=(
                    f"semantic_contract_conflict found {len(warnings)} "
                    f"same-identity scalar contradiction(s) "
                    f"({checked_count} scalar cell(s) checked)"
                ),
            )

        return SemanticContractConflictResult(
            status="pass",
            severity="amber",
            passed=True,
            block_deploy=False,
            checked_count=checked_count,
            message=(
                f"semantic_contract_conflict PASS "
                f"({checked_count} scalar cell(s) checked, no contradictions)"
            ),
        )

    # ------------------------------------------------------------------ helpers
    def _resource_alias_map(self, design_docs: list[Any]) -> dict[str, str]:
        """Build the exact alias→canonical map from ``resource_contracts``.

        Mirrors ``resource_flow_coherence``: an alias is resolved only when it
        targets exactly one canonical resource; a colliding alias is left
        unresolved (no silent collapse). Exact string match only — no fuzzy or
        case-insensitive normalisation.
        """

        alias_targets: dict[str, set[str]] = {}
        for node in design_docs:
            attrs = getattr(node, "attributes", None)
            for entry in self._section_entries(attrs, "resource_contracts"):
                resource = self._str(entry.get("resource"))
                if not resource:
                    continue
                for alias in self._as_list(entry.get("aliases")):
                    alias_s = self._str(alias)
                    if alias_s:
                        alias_targets.setdefault(alias_s, set()).add(resource)
        return {
            alias: next(iter(targets))
            for alias, targets in alias_targets.items()
            if len(targets) == 1
        }

    @classmethod
    def _section_entries(cls, attrs: Any, section: str) -> list[Mapping[str, Any]]:
        """Collect ``section`` entries from a node's attributes.

        Reads the top-level attribute plus the nested ``frontmatter`` and
        ``frontmatter.codd`` locations, mirroring how other field-keyed checks
        (e.g. ``cardinality_coverage._aggregation_policy_entries``) locate the
        same data regardless of where the builder stashed it.
        """

        if not isinstance(attrs, Mapping):
            return []
        values: list[Any] = [attrs.get(section)]
        frontmatter = attrs.get("frontmatter")
        if isinstance(frontmatter, Mapping):
            values.append(frontmatter.get(section))
            codd_meta = frontmatter.get("codd")
            if isinstance(codd_meta, Mapping):
                values.append(codd_meta.get(section))
        entries: list[Mapping[str, Any]] = []
        for value in values:
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, Mapping))
        return entries

    @classmethod
    def _identity(cls, entry: Mapping[str, Any], id_keys: tuple[str, ...]) -> str | None:
        for key in id_keys:
            value = cls._str(entry.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        # bool is intentionally included (it is a scalar property value, e.g.
        # ``required: true``). None is not a declared scalar value to compare.
        return isinstance(value, (str, int, float, bool))

    @staticmethod
    def _values_equal(left: Any, right: Any) -> bool:
        # Compare scalars exactly. Guard against Python's ``1 == True`` /
        # ``0 == False`` coercion so an int and a bool are never treated as the
        # same declared value (and a str "true" stays distinct from bool True).
        if type(left) is not type(right):
            if isinstance(left, bool) or isinstance(right, bool):
                return False
        return left == right

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _str(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            # A boolean is not an identity token; treat as absent for id lookup.
            return None
        text = str(value).strip()
        return text or None


def _conflict(
    *,
    section: str,
    identity: str,
    key: str,
    values: list[Any],
    owner_node_ids: list[str],
    locations: list[str],
) -> dict[str, Any]:
    return {
        "type": "scalar_contract_conflict",
        "severity": "amber",
        "section": section,
        "identity": identity,
        "key": key,
        "values": values,
        "owner_node_ids": owner_node_ids,
        "locations": locations,
        "message": (
            f"{section} identity '{identity}' declares '{key}' with conflicting "
            f"scalar values {values!r} across {locations}."
        ),
        "remediation": (
            f"Reconcile '{key}' for '{identity}' in {section} so it has a single "
            "declared value, or split it into distinct identities if the two "
            "values describe different things."
        ),
    }


__all__ = ["SemanticContractConflictCheck", "SemanticContractConflictResult"]

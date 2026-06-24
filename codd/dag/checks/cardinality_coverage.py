"""DAG check: cardinality coverage for one-to-many (1:N) relations.

A 1:N relation (one parent owns many children) raises a verification question
the rest of the harness cannot answer on its own: when a test asserts the
*parent*, does it also cover the *children*, and how many? The honest default is
that we **do not know** the member universe — a 1:N detector hit only proves a
relation exists, never that "all" members must be asserted. So this check is
deliberately conservative:

* **default amber.** Detecting a 1:N relation while verification exists, with no
  explicit cardinality policy, is surfaced as amber (visibility), never red.
* **red only on a logically-derivable miss.** The single red path requires the
  design doc to *explicitly* declare ``cardinality_assertion.policy: all`` AND a
  non-empty ``member_signals`` list, of which at least one signal is provably not
  asserted by any test. Missing-ness is then a logical deduction from the
  project's own declaration, not a guess about the member universe.
* **never infer the member universe.** ``at_least_one`` passes as soon as a
  single declared signal is asserted. ``representative`` passes (with a summary
  noting the limitation). A heuristic relation hit alone is amber at most.
* **dormant by default.** No 1:N relation detected ⇒ skip (checked_count=0).
* **generality.** The core carries no project/framework/language literal; it
  only reads the relations the detector found and the policies the project itself
  declared.

API mirrors ``ui_coherence.py`` / ``extraction_diagnostics.py`` (DagCheck +
``@register_dag_check`` + a result dataclass exposing
check_name/severity/status/passed/block_deploy/skipped/checked_count/warnings).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.dag.checks._one_to_many_detection import detect_one_to_many_relations


_VERIFICATION_KINDS = {"test_file", "verification_test"}
_ASSERTION_ATTR_KEYS = (
    "assertions",
    "asserted_capabilities",
    "browser_assertions",
    "expected_outcome",
)
_POLICY_ALL = "all"
_POLICY_AT_LEAST_ONE = "at_least_one"
_POLICY_REPRESENTATIVE = "representative"
_KNOWN_POLICIES = {_POLICY_ALL, _POLICY_AT_LEAST_ONE, _POLICY_REPRESENTATIVE}


@dataclass
class CardinalityCoverageResult:
    check_name: str = "cardinality_coverage"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    # one-to-many relations actually examined; 0 on a pass/skip = nothing verified
    checked_count: int = 0
    one_to_many_relations_total: int = 0
    summaries: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("cardinality_coverage")
class CardinalityCoverageCheck(DagCheck):
    """Verify 1:N member coverage only when the project declares the policy."""

    check_name = "cardinality_coverage"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> CardinalityCoverageResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for cardinality_coverage check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings

        root = self.project_root
        # Thread the flattened DAG settings so a configured ``dag.lexicon_file``
        # (canonical to the builder) is honored; otherwise a custom lexicon path
        # is silently missed and this check goes dormant (SKIP, checked_count=0).
        relations = detect_one_to_many_relations(target_dag, root, settings=self.settings)

        if not relations:
            # Dormant: no 1:N shape means there is nothing to reason about.
            return CardinalityCoverageResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                checked_count=0,
                one_to_many_relations_total=0,
                message="cardinality_coverage SKIP (no one-to-many relation detected)",
            )

        verification_exists = _has_verification(target_dag)
        asserted_signals = _asserted_signals(target_dag, root)
        assertions = _cardinality_assertions(target_dag)

        warnings: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        red_violations: list[dict[str, Any]] = []

        # Bind each assertion to the detected relation it actually describes. An
        # assertion is evaluated ONLY against a relation it matches: a policy for
        # an unrelated field can neither block a different relation (false-red)
        # nor suppress that relation's own amber (false-green). Matching is exact
        # on normalized identity (never substring/pluralization) so a loose bind
        # cannot re-introduce a false-red.
        #
        # Dedup is by FULL assertion identity (field_id + policy + member_signals
        # + design_doc), not field_id alone. A field_id-only key let a softer
        # policy that happened to come first (e.g. ``representative``) mask a
        # later stricter ``all`` on the *same* field, swallowing a real red
        # (false-green). Keying on the whole declaration evaluates every distinct
        # assertion exactly once while still collapsing an identical assertion
        # that matches several relations to a single evaluation (no fan-out).
        evaluated_identities: set[tuple] = set()
        for relation in relations:
            matched = [a for a in assertions if _assertion_matches_relation(a, relation)]
            if not matched:
                # No policy describes THIS relation. amber visibility — but only
                # when verification exists (otherwise a separate "no tests"
                # concern owns it; we never invent a member universe).
                if verification_exists:
                    warnings.append(_unspecified_policy_warning(relation))
                continue
            for assertion in matched:
                identity = _assertion_identity(assertion)
                if identity in evaluated_identities:
                    continue
                evaluated_identities.add(identity)
                self._evaluate_assertion(
                    assertion, asserted_signals, summaries, warnings, red_violations
                )

        if not assertions:
            # 1:N relation(s) detected, no explicit policy anywhere.
            message = (
                f"cardinality_coverage examined {len(relations)} one-to-many "
                f"relation(s); no cardinality_assertion policy declared"
                + ("" if verification_exists else " (no verification present)")
            )
            return CardinalityCoverageResult(
                status="warn" if warnings else "pass",
                severity="amber",
                passed=True,
                block_deploy=False,
                checked_count=len(relations),
                one_to_many_relations_total=len(relations),
                summaries=summaries,
                warnings=warnings,
                message=message,
            )

        if red_violations:
            return CardinalityCoverageResult(
                status="fail",
                severity="red",
                passed=False,
                block_deploy=True,
                checked_count=len(relations),
                one_to_many_relations_total=len(relations),
                summaries=summaries,
                warnings=red_violations + warnings,
                message=(
                    f"cardinality_coverage found {len(red_violations)} field(s) with "
                    f"policy=all whose declared member signals are not all asserted "
                    f"({len(relations)} one-to-many relation(s) examined)"
                ),
            )

        return CardinalityCoverageResult(
            status="warn" if warnings else "pass",
            severity="amber",
            passed=True,
            block_deploy=False,
            checked_count=len(relations),
            one_to_many_relations_total=len(relations),
            summaries=summaries,
            warnings=warnings,
            message=(
                f"cardinality_coverage examined {len(relations)} one-to-many "
                f"relation(s); {len(evaluated_identities)} cardinality_assertion(s) "
                f"evaluated (matched to a detected relation)"
            ),
        )

    @staticmethod
    def _evaluate_assertion(
        assertion: dict[str, Any],
        asserted_signals: set[str],
        summaries: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        red_violations: list[dict[str, Any]],
    ) -> None:
        """Evaluate one relation-matched assertion, appending to result buckets.

        Only ever called for an assertion already bound to a detected 1:N
        relation, so a red here is a logical miss against the project's own
        declaration — never a guess about an unrelated field.
        """

        # Normalize so "All"/"ALL" behave like "all" — a case-sensitive compare
        # would let a capitalized policy bypass the red path (a false-green).
        policy = str(assertion["policy"] or "").strip().lower()
        member_signals = assertion["member_signals"]
        summary = {
            "field_id": assertion["field_id"],
            "policy": policy,
            "member_signals_total": len(member_signals),
        }

        if policy == _POLICY_ALL:
            if not member_signals:
                # policy=all with no members: cannot verify "all" — amber,
                # never red (no logical miss is derivable).
                summary["status"] = "unverifiable_all"
                warnings.append(_unverifiable_all_warning(assertion["field_id"]))
                summaries.append(summary)
                return
            missing = [s for s in member_signals if not _signal_asserted(s, asserted_signals)]
            summary["asserted"] = len(member_signals) - len(missing)
            if missing:
                # The only red path: project declared every member must be
                # asserted; at least one provably is not.
                summary["status"] = "incomplete_all"
                summary["missing_signals"] = missing
                red_violations.append(_missing_member_violation(assertion["field_id"], missing))
            else:
                summary["status"] = "complete_all"
            summaries.append(summary)
            return

        if policy == _POLICY_AT_LEAST_ONE:
            # Passes as soon as one declared member is asserted; we never
            # require the full (unknown) universe.
            if not member_signals:
                summary["status"] = "at_least_one_no_members"
                warnings.append(_at_least_one_no_members_warning(assertion["field_id"]))
            elif any(_signal_asserted(s, asserted_signals) for s in member_signals):
                summary["status"] = "satisfied_at_least_one"
            else:
                summary["status"] = "at_least_one_unsatisfied"
                warnings.append(_at_least_one_unsatisfied_warning(assertion["field_id"], member_signals))
            summaries.append(summary)
            return

        if policy == _POLICY_REPRESENTATIVE:
            # Representative coverage is an explicit acceptance of partial
            # coverage: pass, but record the limitation in the summary.
            summary["status"] = "representative"
            summary["limitation"] = (
                "representative coverage only — non-representative members are "
                "intentionally not asserted"
            )
            summaries.append(summary)
            return

        # Unknown policy value: treat as unspecified — amber, never red.
        summary["status"] = "unknown_policy"
        warnings.append(_unknown_policy_warning(assertion["field_id"], policy))
        summaries.append(summary)


def _assertion_matches_relation(
    assertion: Mapping[str, Any], relation: Mapping[str, Any]
) -> bool:
    """Return True iff this assertion describes this detected 1:N relation.

    Binding is by *identity*, exact on normalized form — never substring or
    pluralization (both are generality traps and a loose bind would re-introduce
    a false-red). An assertion is bound when either:

    * its ``field_id`` normalizes to the relation's child or parent, or
    * any ``member_signal``'s entity prefix (the token before ``:``) normalizes to
      the relation's child or parent.

    The member-signal prefix is the precise, convention-driven key (signals read
    ``<child>:<token>``); ``field_id`` equality is a secondary key for projects
    that name the policy after the relation entity. A relation whose identity
    appears nowhere in the assertion stays unbound (amber), which is the safe,
    anti-false-red outcome.
    """

    targets = {_norm(str(relation.get("child") or "")), _norm(str(relation.get("parent") or ""))}
    targets.discard("")
    if not targets:
        return False

    if _norm(str(assertion.get("field_id") or "")) in targets:
        return True
    for signal in assertion.get("member_signals") or []:
        prefix = str(signal).split(":", 1)[0]
        if _norm(prefix) in targets:
            return True
    return False


def _norm(value: str) -> str:
    """Normalize an identity token: lowercase, strip non-alphanumerics.

    Mirrors ``_one_to_many_detection._norm`` so relation identities compare on the
    same footing the detector deduped them with.
    """

    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _assertion_identity(assertion: Mapping[str, Any]) -> tuple:
    """A hashable identity for one assertion: its full declaration content.

    Two assertions share an identity only when field_id, policy, member_signals
    (order-sensitive — the declaration's own order) AND originating design_doc all
    match. This collapses a single assertion seen across multiple matched
    relations to one evaluation (no fan-out duplicate red), while keeping two
    *different* policies on the same field_id distinct so a softer one cannot mask
    a stricter one's red.
    """

    return (
        str(assertion.get("field_id") or ""),
        str(assertion.get("policy") or ""),
        tuple(assertion.get("member_signals") or []),
        str(assertion.get("design_doc") or ""),
    )


def _cardinality_assertions(dag: DAG) -> list[dict[str, Any]]:
    """Collect explicit ``cardinality_assertion`` declarations from design docs.

    Reads ``aggregation_policies[]`` entries (top-level attribute and nested
    ``frontmatter``) and keeps only those carrying an explicit
    ``cardinality_assertion`` block. The member universe is *only* ever what the
    project lists in ``member_signals`` — it is never inferred.
    """

    assertions: list[dict[str, Any]] = []
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind != "design_doc":
            continue
        attributes = node.attributes if isinstance(node.attributes, Mapping) else {}
        for policy_entry in _aggregation_policy_entries(attributes):
            assertion = policy_entry.get("cardinality_assertion")
            if not isinstance(assertion, Mapping):
                continue
            policy = assertion.get("policy")
            if not isinstance(policy, str) or not policy.strip():
                continue
            assertions.append(
                {
                    "field_id": _field_id(policy_entry),
                    "policy": policy.strip(),
                    "member_signals": _string_list(assertion.get("member_signals")),
                    "design_doc": str(node.id),
                }
            )
    return assertions


def _aggregation_policy_entries(attributes: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Any] = [attributes.get("aggregation_policies")]
    frontmatter = attributes.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        values.append(frontmatter.get("aggregation_policies"))
        codd_meta = frontmatter.get("codd")
        if isinstance(codd_meta, Mapping):
            values.append(codd_meta.get("aggregation_policies"))
    entries: list[Mapping[str, Any]] = []
    for value in values:
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, Mapping))
    return entries


def _has_verification(dag: DAG) -> bool:
    return any(node.kind in _VERIFICATION_KINDS for node in dag.nodes.values())


def _asserted_signals(dag: DAG, project_root: Path | None) -> set[str]:
    """Return the set of signals any verification node asserts.

    Two evidence forms, mirroring user_journey_coherence: explicit assertion
    attributes, and presence of the signal token in the verification source
    text. Source presence is intentionally a *weak* signal — it only ever
    contributes to a *pass* (the absence of all evidence is what produces red),
    so it can never manufacture a false red.
    """

    signals: set[str] = set()
    source_texts: list[str] = []
    for node in dag.nodes.values():
        if node.kind not in _VERIFICATION_KINDS:
            continue
        attributes = node.attributes if isinstance(node.attributes, Mapping) else {}
        for key in _ASSERTION_ATTR_KEYS:
            signals.update(_nested_strings(attributes.get(key)))
        text = _verification_source_text(node, project_root)
        if text:
            source_texts.append(text)
    # Store source texts under a sentinel so _signal_asserted can substring-match
    # without re-reading files per signal.
    if source_texts:
        signals.add(_SOURCE_TEXT_BLOB_PREFIX + "\n".join(source_texts))
    return signals


_SOURCE_TEXT_BLOB_PREFIX = "\x00source_blob\x00"


def _signal_asserted(signal: str, asserted_signals: set[str]) -> bool:
    if signal in asserted_signals:
        return True
    for entry in asserted_signals:
        if entry.startswith(_SOURCE_TEXT_BLOB_PREFIX) and signal:
            # Search only the content AFTER the sentinel prefix, so a signal that is
            # itself a substring of the prefix (e.g. "source", "blob") cannot
            # spuriously register as asserted — that would be a false-green.
            if signal in entry[len(_SOURCE_TEXT_BLOB_PREFIX):]:
                return True
    return False


def _verification_source_text(node: Node, project_root: Path | None) -> str:
    if project_root is None:
        return ""
    source = _verification_source(node)
    if not source:
        return ""
    root = Path(project_root).resolve()
    path = (root / source).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _verification_source(node: Node) -> str | None:
    attributes = node.attributes if isinstance(node.attributes, Mapping) else {}
    outcome = attributes.get("expected_outcome")
    if isinstance(outcome, Mapping) and isinstance(outcome.get("source"), str):
        return outcome["source"]
    return node.path


def _field_id(entry: Mapping[str, Any]) -> str:
    for key in ("field_id", "field", "id", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _nested_strings(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, Mapping):
        out: set[str] = set()
        for item in value.values():
            out |= _nested_strings(item)
        return out
    if isinstance(value, (list, tuple, set)):
        out = set()
        for item in value:
            out |= _nested_strings(item)
        return out
    text = str(value).strip()
    return {text} if text else set()


def _unspecified_policy_warning(relation: Mapping[str, Any]) -> dict[str, Any]:
    parent = str(relation.get("parent") or "")
    child = str(relation.get("child") or "")
    return {
        "type": "cardinality_policy_unspecified",
        "parent": parent,
        "child": child,
        "evidence": str(relation.get("evidence") or "one-to-many relation detected"),
        "severity": "amber",
        "block_deploy": False,
        "suggestion": (
            f"one-to-many relation {parent} -> {child} has no cardinality_assertion; "
            "declare aggregation_policies[].cardinality_assertion "
            "(policy: all | at_least_one | representative) to make coverage intent explicit"
        ),
    }


def _unverifiable_all_warning(field_id: str) -> dict[str, Any]:
    return {
        "type": "cardinality_unverifiable_all",
        "field_id": field_id,
        "severity": "amber",
        "block_deploy": False,
        "suggestion": (
            f"field '{field_id}' declares policy=all but lists no member_signals; "
            "'all' cannot be verified without the member set — list member_signals "
            "or use policy=representative"
        ),
    }


def _at_least_one_no_members_warning(field_id: str) -> dict[str, Any]:
    return {
        "type": "cardinality_at_least_one_no_members",
        "field_id": field_id,
        "severity": "amber",
        "block_deploy": False,
        "suggestion": (
            f"field '{field_id}' declares policy=at_least_one but lists no "
            "member_signals to assert"
        ),
    }


def _at_least_one_unsatisfied_warning(field_id: str, member_signals: list[str]) -> dict[str, Any]:
    return {
        "type": "cardinality_at_least_one_unsatisfied",
        "field_id": field_id,
        "member_signals": list(member_signals),
        "severity": "amber",
        "block_deploy": False,
        "suggestion": (
            f"field '{field_id}' declares policy=at_least_one but none of its "
            "declared member_signals are asserted by any test"
        ),
    }


def _unknown_policy_warning(field_id: str, policy: str) -> dict[str, Any]:
    return {
        "type": "cardinality_unknown_policy",
        "field_id": field_id,
        "policy": policy,
        "severity": "amber",
        "block_deploy": False,
        "suggestion": (
            f"field '{field_id}' declares an unrecognised cardinality policy "
            f"'{policy}'; expected one of: {', '.join(sorted(_KNOWN_POLICIES))}"
        ),
    }


def _missing_member_violation(field_id: str, missing: list[str]) -> dict[str, Any]:
    return {
        "type": "cardinality_members_not_all_asserted",
        "field_id": field_id,
        "missing_signals": list(missing),
        "severity": "red",
        "block_deploy": True,
        "suggestion": (
            f"field '{field_id}' declares policy=all with member_signals "
            f"{missing} that are not asserted by any test; assert them or relax "
            "the policy (at_least_one / representative)"
        ),
    }


__all__ = ["CardinalityCoverageCheck", "CardinalityCoverageResult"]

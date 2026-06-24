"""C8 implementation coverage check."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
from typing import Any

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.llm.design_doc_extractor import ExpectedExtraction, ExpectedNode


DEFAULT_PATH_PREFIX_TOLERANT = ("src/", "lib/", "app/")
_BRACKET_SEGMENT_RE = re.compile(r"\[[^\]]+\]")
# ``kind="common"`` is overloaded: design docs opt in via frontmatter, but
# implementation/test files matched by ``common_node_patterns`` are also
# reclassified to ``kind="common"`` (for the transitive-closure exemption).
# ``.md`` is the codebase-wide doc discriminator (same principle as
# dependency_freshness): non-markdown common nodes are code artifacts and
# must remain inside the implementation matching pool, otherwise real
# implementations are misreported as missing_implementation.
_DOC_SUFFIX = ".md"


@dataclass
class ImplementationCoverageResult:
    check_name: str = "implementation_coverage"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True
    coverage_summaries: list[dict[str, Any]] = field(default_factory=list)
    # Expected artifacts actually evaluated across all design docs. When no design
    # doc declares an expected_extraction the loop never runs and the check passes
    # having verified nothing; checked_count==0 lets the materiality overlay flag
    # that as a vacuous pass rather than a verified clean run.
    checked_count: int = 0


@register_dag_check("implementation_coverage")
class ImplementationCoverageCheck(DagCheck):
    """Compare design-derived expected artifacts with the project DAG."""

    check_name = "implementation_coverage"
    severity = "red"
    block_deploy = False

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ImplementationCoverageResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for implementation_coverage check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = self.project_root or Path.cwd()

        violations: list[dict[str, Any]] = []
        coverage_summaries: list[dict[str, Any]] = []
        expected_impl_nodes: list[ExpectedNode] = []
        checked_count = 0
        for design_doc in _design_doc_nodes(target_dag):
            expected = _expected_extraction(design_doc)
            if expected is None:
                continue
            # Per-design-doc coverage accounting feeds the amber-only
            # diagnostics below. ``doc_has_red`` mirrors the existing red
            # emission exactly, so the cross-artifact amber can defer to the
            # red instead of duplicating it.
            by_kind: dict[str, dict[str, int]] = {}
            expected_total = 0
            matched_total = 0
            doc_has_red = False
            for expected_node in expected.expected_nodes:
                if expected_node.kind == "impl_file":
                    expected_impl_nodes.append(expected_node)
                matched = _matches_any_artifact(target_dag, expected_node, root, self.settings)
                expected_total += 1
                bucket = by_kind.setdefault(expected_node.kind, {"expected": 0, "matched": 0})
                bucket["expected"] += 1
                if matched:
                    matched_total += 1
                    bucket["matched"] += 1
                    continue
                doc_has_red = True
                violations.append(
                    {
                        "type": "missing_implementation",
                        "design_doc": design_doc.id,
                        "expected_kind": expected_node.kind,
                        "path_hint": expected_node.path_hint,
                        "rationale": expected_node.rationale,
                        "source_design_section": expected_node.source_design_section,
                        "severity": "red",
                    }
                )

            checked_count += expected_total
            coverage_summaries.append(
                {
                    "design_doc": design_doc.id,
                    "expected_total": expected_total,
                    "matched_total": matched_total,
                    "missing_total": expected_total - matched_total,
                    "by_kind": by_kind,
                }
            )
            violations.extend(
                _coverage_shape_violations(design_doc.id, by_kind, expected_total, matched_total, doc_has_red)
            )

        if expected_impl_nodes:
            # Historical behavior preserved on purpose: a glob path_hint that
            # matches any project file suppresses the additional_implementation
            # pass entirely (the FS lookup never referenced the candidate
            # node). Changing this would surface new amber findings in
            # existing projects, so the quirk stays until explicitly revised.
            fs_claimed = any(
                _fs_glob_match(_normalize_hint(expected.path_hint), root)
                for expected in expected_impl_nodes
            )
            for impl_node in _nodes_by_kind(target_dag, "impl_file"):
                if fs_claimed or any(
                    _hint_matches_node(expected.path_hint, impl_node, self.settings)
                    for expected in expected_impl_nodes
                ):
                    continue
                violations.append(
                    {
                        "type": "additional_implementation",
                        "impl_file": impl_node.id,
                        "severity": "amber",
                    }
                )

        red_count = sum(1 for item in violations if item.get("severity") == "red")
        amber_count = sum(1 for item in violations if item.get("severity") == "amber")
        additional_count = sum(1 for item in violations if item.get("type") == "additional_implementation")
        status = "fail" if red_count else ("warn" if amber_count else "pass")
        severity = "red" if red_count else ("amber" if amber_count else "info")
        if red_count:
            message = f"C8 implementation_coverage found {red_count} missing expected artifact(s)"
        elif amber_count == additional_count and additional_count:
            message = f"C8 implementation_coverage found {amber_count} additional implementation artifact(s)"
        elif amber_count:
            message = f"C8 implementation_coverage found {amber_count} coverage warning(s)"
        else:
            message = "C8 implementation_coverage PASS"

        return ImplementationCoverageResult(
            severity=severity,
            status=status,
            message=message,
            block_deploy=self.block_deploy,
            violations=violations,
            passed=red_count == 0,
            coverage_summaries=coverage_summaries,
            checked_count=checked_count,
        )


def _coverage_shape_violations(
    design_doc_id: str,
    by_kind: dict[str, dict[str, int]],
    expected_total: int,
    matched_total: int,
    doc_has_red: bool,
) -> list[dict[str, Any]]:
    """Amber-only diagnostics derived from one design doc's coverage shape.

    These are summaries on top of the existing red ``missing_implementation``
    pass — they never change the red verdict and never re-report a missing
    artifact that the red already owns.
    """

    diagnostics: list[dict[str, Any]] = []

    impl_expected = by_kind.get("impl_file", {}).get("expected", 0)
    test_expected = by_kind.get("test_file", {}).get("expected", 0)
    # Only flag the missing-test dimension for docs that already declare a
    # multi-artifact shape (``expected_total > 1``). A lone implementation
    # artifact with no declared test is far too common to be a coherence
    # signal, and flagging it would flood every existing project with amber.
    if expected_total > 1 and impl_expected > 0 and test_expected == 0:
        # An implementation artifact (plus at least one more artifact) is
        # declared but no test artifact kind is — the coverage shape is
        # incomplete by construction, independent of whether the declared
        # artifacts exist on disk.
        diagnostics.append(
            {
                "type": "coverage_shape_incomplete",
                "design_doc": design_doc_id,
                "by_kind": dict(by_kind),
                "severity": "amber",
            }
        )

    # Cross-artifact partial coverage is a doc-level shape signal. Any missing
    # required artifact is already a red, so when this doc has a red we defer
    # to it and stay silent to avoid double-reporting the same gap. Because the
    # red emission uses the same matcher, a count-based partial always coincides
    # with a red today; this branch is therefore a deliberate forward-compatible
    # guard (e.g. for a future soft/optional-artifact state where a gap is not
    # red). The visible partial-coverage signal lives in ``coverage_summaries``.
    if expected_total > 1 and 0 < matched_total < expected_total and not doc_has_red:
        diagnostics.append(
            {
                "type": "cross_artifact_partial_coverage",
                "design_doc": design_doc_id,
                "expected_total": expected_total,
                "matched_total": matched_total,
                "by_kind": dict(by_kind),
                "severity": "amber",
            }
        )

    return diagnostics


def _design_doc_nodes(dag: DAG) -> list[Node]:
    return _nodes_by_kind(dag, "design_doc")


def _nodes_by_kind(dag: DAG, kind: str) -> list[Node]:
    return [node for node in sorted(dag.nodes.values(), key=lambda item: item.id) if node.kind == kind]


def _expected_extraction(design_doc: Node) -> ExpectedExtraction | None:
    payload = design_doc.attributes.get("expected_extraction")
    if isinstance(payload, ExpectedExtraction):
        return payload
    if isinstance(payload, dict):
        return ExpectedExtraction.from_dict(payload)
    return None


def _matches_any_artifact(
    dag: DAG,
    expected_node: ExpectedNode,
    project_root: Path,
    settings: dict[str, Any] | None = None,
) -> bool:
    if expected_node.kind == "config_file":
        # Route through the root-jail (resolve + in-root check) so a hint naming
        # an in-root symlink whose target escapes the project root cannot
        # false-green via a raw ``project_root / hint`` stat.
        resolved = _resolve_in_root(expected_node.path_hint, project_root)
        if resolved is not None:
            try:
                if resolved.is_file():
                    return True
            except OSError:
                pass
    if expected_node.kind == "impl_file":
        return _matches_any_impl(dag, expected_node.path_hint, project_root, settings)
    for candidate in _candidate_nodes(dag, expected_node.kind):
        if _hint_matches_node(expected_node.path_hint, candidate, settings):
            return True
    return _fs_fallback_match(expected_node.path_hint, project_root)


def _candidate_nodes(dag: DAG, expected_kind: str) -> list[Node]:
    if expected_kind == "test_file":
        return _nodes_by_kind(dag, "test_file") + _code_common_nodes(dag)
    if expected_kind == "config_file":
        return [
            node
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
            if node.kind in {"config_file", "deployment_doc", "impl_file"}
        ] + _code_common_nodes(dag)
    return _impl_candidate_nodes(dag)


def _impl_candidate_nodes(dag: DAG) -> list[Node]:
    return _nodes_by_kind(dag, "impl_file") + _code_common_nodes(dag)


def _code_common_nodes(dag: DAG) -> list[Node]:
    """Code-side common nodes (non-markdown paths; see ``_DOC_SUFFIX`` note)."""
    return [
        node
        for node in _nodes_by_kind(dag, "common")
        if not _normalize_hint(node.path or node.id or "").lower().endswith(_DOC_SUFFIX)
    ]


def _matches_any_impl(
    dag: DAG,
    path_hint: str,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    hint = _normalize_hint(path_hint)
    if not hint:
        return False
    candidates = [candidate for node in _impl_candidate_nodes(dag) for candidate in _node_path_candidates(node)]

    if any(_exact_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_glob_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_bracket_path_match(hint, candidate) for candidate in candidates):
        return True
    if any(_match_with_src_prefix_tolerance(hint, candidate, settings) for candidate in candidates):
        return True
    if any(_soft_path_match(hint, candidate) for candidate in candidates):
        return True

    return _fs_fallback_match(path_hint, project_root)


def _hint_matches_node(
    path_hint: str,
    node: Node,
    settings: dict[str, Any] | None = None,
) -> bool:
    hint = _normalize_hint(path_hint)
    if not hint:
        return False
    candidates = _node_path_candidates(node)
    for candidate in candidates:
        if _exact_path_match(hint, candidate):
            return True
        if _glob_path_match(hint, candidate):
            return True
        if _bracket_path_match(hint, candidate):
            return True
        if _match_with_src_prefix_tolerance(hint, candidate, settings):
            return True
        if _soft_path_match(hint, candidate):
            return True
    return False


def _fs_fallback_match(raw_hint: str, project_root: Path | None) -> bool:
    """Match a hint against the file system when no DAG node satisfies it.

    Glob hints are matched with fnmatch; the literal-bracket-escaped form of
    the pattern is tried as well so bracketed directory names are not misread
    as character classes. Literal hints are resolved directly: an existing
    file satisfies the hint (the artifact exists even when the scanner did not
    register a node for it).

    Root-jail: the literal lookup resolves the hint against ``project_root`` and
    only consults the file system when the result stays inside the project root
    (mirrors ``stale_evidence._resolve_source``). A parent-traversal or absolute
    hint that escapes the root must not satisfy an expected artifact with a file
    that lives outside the project — that would be a false-green. The glob branch
    is already root-confined because ``_project_files`` only enumerates files
    under the root, so it is left unchanged.

    The *raw* hint is taken here (not the ``_normalize_hint`` form). Globbing and
    bracket escaping run on the normalized hint as before, but the literal lookup
    forwards the raw hint to ``_resolve_in_root`` so the leading-slash distinction
    (absolute vs root-relative) survives the jail. See ``_resolve_in_root``.
    """
    if project_root is None:
        return False
    normalized = _normalize_hint(raw_hint)
    if not normalized:
        return False
    if any(char in normalized for char in "*?[]"):
        files = _project_files(project_root)
        for pattern in (normalized, _escape_literal_brackets(normalized)):
            if fnmatch.filter(files, pattern):
                return True
    resolved = _resolve_in_root(raw_hint, project_root)
    if resolved is None:
        return False
    try:
        return resolved.is_file()
    except OSError:
        return False


def _resolve_in_root(raw_hint: str, project_root: Path) -> Path | None:
    """Resolve ``raw_hint`` under ``project_root``, returning it only when the
    resolved path stays inside the root. Parent-traversal hints, and absolute
    hints that point outside the root, resolve to ``None`` (no match).

    The *raw* hint is taken on purpose (not the ``_normalize_hint`` form): that
    normalization strips the leading ``/`` before the jail can see it, which both
    lets an FS-absolute hint (``/src/service.py``) collapse onto the in-root
    relative file ``src/service.py`` (false-green) and turns a genuine in-root
    absolute hint into a root-appended relative path that escapes the jail
    (false-red). To tell the two apart the leading-slash distinction must survive
    until here.

    Absolute hints are accepted only when they live under ``project_root`` (then
    converted to a project-relative lookup); an absolute hint resolving outside
    the root is rejected even if its target exists on the real filesystem. An
    "absolute-looking but root-relative" hint such as ``/src/...`` denotes the
    filesystem root, so it is treated as absolute and only matches if that
    absolute location happens to fall inside the project root (safe side: it
    will not).
    """
    cleaned = str(raw_hint or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return None

    root_resolved = project_root.resolve()
    pure = PurePosixPath(cleaned)
    try:
        if pure.is_absolute():
            # Absolute hint: only accept when it is genuinely inside the root.
            absolute = Path(cleaned).resolve()
            absolute.relative_to(root_resolved)
            resolved = absolute
        else:
            resolved = (project_root / cleaned).resolve()
            resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return resolved


def _fs_glob_match(hint: str, project_root: Path | None) -> bool:
    """Glob-only FS lookup, kept identical to the historical fallback (raw
    fnmatch, no literal-bracket escaping) so the additional_implementation
    pass keeps its exact pre-existing behavior."""
    if project_root is None or not hint:
        return False
    if not any(char in hint for char in "*?[]"):
        return False
    return any((project_root / match).is_file() for match in fnmatch.filter(_project_files(project_root), hint))


def _node_path_candidates(node: Node) -> list[str]:
    candidates: list[str] = []
    for value in (node.path, node.id):
        candidate = _normalize_hint(value or "")
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _exact_path_match(hint: str, candidate: str) -> bool:
    return hint == candidate or hint == Path(candidate).name


def _glob_path_match(hint: str, candidate: str) -> bool:
    return fnmatch.fnmatchcase(candidate, hint) or fnmatch.fnmatchcase(candidate, _escape_literal_brackets(hint))


def _bracket_path_match(hint: str, candidate: str) -> bool:
    normalized_hint = _normalize_bracket_segments(hint)
    normalized_candidate = _normalize_bracket_segments(candidate)
    if normalized_hint == hint and normalized_candidate == candidate:
        return False
    if normalized_hint == normalized_candidate:
        return True
    return _glob_path_match(normalized_hint, normalized_candidate)


def _normalize_bracket_segments(path: str) -> str:
    return _BRACKET_SEGMENT_RE.sub("*", path)


def _match_with_src_prefix_tolerance(
    path_hint: str,
    impl_path: str,
    settings: dict[str, Any] | None = None,
) -> bool:
    prefixes = _path_prefix_tolerant(settings)
    if not prefixes:
        return False

    hint = _normalize_hint(path_hint)
    candidate = _normalize_hint(impl_path)
    for hint_variant in _prefix_variants(hint, prefixes):
        for candidate_variant in _prefix_variants(candidate, prefixes):
            if hint_variant == candidate_variant:
                return True
            if _glob_path_match(hint_variant, candidate_variant):
                return True
            if _bracket_path_match(hint_variant, candidate_variant):
                return True
    return False


def _path_prefix_tolerant(settings: dict[str, Any] | None = None) -> list[str]:
    coherence = settings.get("coherence", {}) if isinstance(settings, dict) else {}
    configured = coherence.get("path_prefix_tolerant") if isinstance(coherence, dict) else None
    if configured is None:
        return list(DEFAULT_PATH_PREFIX_TOLERANT)

    prefixes: list[str] = []
    values = [configured] if isinstance(configured, str) else configured
    if not isinstance(values, (list, tuple, set)):
        return list(DEFAULT_PATH_PREFIX_TOLERANT)
    for value in values:
        if not isinstance(value, str):
            continue
        prefix = _normalize_hint(value)
        if not prefix:
            continue
        if not prefix.endswith("/"):
            prefix = f"{prefix}/"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _prefix_variants(path: str, prefixes: list[str]) -> list[str]:
    variants = [path]
    for prefix in prefixes:
        if path.startswith(prefix):
            variants.append(path.removeprefix(prefix))
        else:
            variants.append(f"{prefix}{path}")
    deduped: list[str] = []
    for variant in variants:
        if variant and variant not in deduped:
            deduped.append(variant)
    return deduped


def _escape_literal_brackets(pattern: str) -> str:
    escaped: list[str] = []
    for char in pattern:
        if char == "[":
            escaped.append("[[]")
        elif char == "]":
            escaped.append("[]]")
        else:
            escaped.append(char)
    return "".join(escaped)


def _soft_path_match(hint: str, candidate: str) -> bool:
    if any(char in hint for char in "*?[]"):
        return False
    hint_lower = hint.lower()
    candidate_lower = candidate.lower()
    if hint_lower in candidate_lower:
        return True
    return Path(hint_lower).stem == Path(candidate_lower).stem


def _normalize_hint(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


@lru_cache(maxsize=16)
def _project_files(project_root: Path) -> list[str]:
    excluded_parts = {
        ".codd",
        ".git",
        ".next",
        "__pycache__",
        "dist",
        "node_modules",
    }
    root_resolved = project_root.resolve()
    files: list[str] = []
    for path in project_root.rglob("*"):
        relative = path.relative_to(project_root)
        if excluded_parts.intersection(relative.parts):
            continue
        if not path.is_file():
            continue
        # Symlink-aware jail: ``rglob`` enumerates an in-root symlink entry even
        # when its target escapes the root. Resolve each candidate and keep it
        # only when the resolved target stays inside the project root, so glob
        # matching (``_fs_glob_match`` / ``_fs_fallback_match``) cannot be
        # satisfied by an off-root file reached through an in-root symlink.
        try:
            path.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        files.append(relative.as_posix())
    return files


__all__ = ["ImplementationCoverageCheck", "ImplementationCoverageResult"]

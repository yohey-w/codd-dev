"""Amber UI coherence check for one-to-many relations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping

from codd.dag import DAG, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.dag.checks._one_to_many_detection import detect_one_to_many_relations
from codd.path_safety import PathEscapeError, resolve_project_path
from codd.requirements_meta import operation_flow_operations


DETAIL_PATH_RE = re.compile(r"/[A-Za-z0-9_.-]+/(?:\[id\]|:id|\{id\}|<id>)/[A-Za-z0-9_.-]+", re.IGNORECASE)
MASTER_DETAIL_RE = re.compile(r"master[\s_.-]?detail|list[\s_.-]?detail", re.IGNORECASE)
DRILLDOWN_RE = re.compile(r"drill[\s_.-]?down|ドリルダウン", re.IGNORECASE)
DETAIL_SCREEN_RE = re.compile(r"詳細.*画面|親.*画面.*子.*画面")
FRONTMATTER_RE = re.compile(
    r"\A(?:\ufeff)?---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
ATOMIC_MARKDOWN_RE = re.compile(r"^(?:\||#{1,6}\s|[-+*]\s|\d+[.)]\s|>)")
FENCE_RE = re.compile(r"^(?:```|~~~)")
SUPPRESS_PATTERNS = {"single_form", "inline_edit"}
MASTER_DETAIL_PATTERNS = {"master_detail", "list_detail", "drilldown"}


@dataclass
class UiCoherenceResult:
    check_name: str = "ui_coherence_for_one_to_many"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    checked_count: int = 0  # one-to-many relations actually verified; 0 ⇒ skip (no applicable input)
    one_to_many_relations_total: int = 0
    relations_with_master_detail_ui: int = 0
    relations_missing_master_detail: list[dict[str, Any]] = field(default_factory=list)
    ignored_relations: int = 0
    suppressed_relations: int = 0
    warnings: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("ui_coherence_for_one_to_many")
class UiCoherenceCheck(DagCheck):
    """Warn when one-to-many data shape lacks declared master-detail UI."""

    check_name = "ui_coherence_for_one_to_many"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> UiCoherenceResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for ui_coherence check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings

        root = self.project_root
        config = codd_config if codd_config is not None else self.settings
        # Pass the flattened DAG settings (which carry the canonical
        # ``lexicon_file``), so a configured custom lexicon path is honored.
        #
        # A configured ``dag.lexicon_file`` that escapes the project root raises
        # PathEscapeError. That is NOT a skip: the canonical 1:N evidence source
        # is out-of-root and unreadable, so surface an honest red/error finding
        # rather than crashing the run or silently passing (a false-green).
        try:
            relations = detect_one_to_many_relations(target_dag, root, settings=self.settings)
        except PathEscapeError as exc:
            return _lexicon_escape_error_result(exc)
        if not relations:
            # No one-to-many data shape exists, so there is no master-detail UI
            # obligation to verify. Report skip (not a clean PASS) so a verify
            # summary does not read "ran and verified" when nothing was applicable.
            return UiCoherenceResult(
                status="skip",
                skipped=True,
                message="ui_coherence_for_one_to_many skipped: no one-to-many relations detected",
                checked_count=0,
                one_to_many_relations_total=0,
                passed=True,
            )
        missing: list[dict[str, Any]] = []
        covered = 0
        ignored = 0
        suppressed = 0

        for relation in relations:
            if _ignored_by_config(relation, config):
                ignored += 1
                continue
            if _operation_flow_pattern(target_dag, relation, config) in SUPPRESS_PATTERNS:
                suppressed += 1
                continue
            if _operation_flow_pattern(target_dag, relation, config) in MASTER_DETAIL_PATTERNS:
                covered += 1
                continue
            if _has_master_detail_ui(target_dag, relation, root):
                covered += 1
                continue
            missing.append(_missing_relation_entry(relation))

        warning_lines = [_warning_line(item) for item in missing]
        violations = [
            {
                **item,
                "type": "missing_master_detail_ui_for_one_to_many",
                "severity": "amber",
                "block_deploy": False,
            }
            for item in missing
        ]
        message = (
            f"ui_coherence_for_one_to_many checked {len(relations)} one-to-many relation(s); "
            f"{len(missing)} missing master-detail UI hint(s)"
        )
        return UiCoherenceResult(
            status="warn" if missing else "pass",
            message=message,
            checked_count=len(relations),
            one_to_many_relations_total=len(relations),
            relations_with_master_detail_ui=covered,
            relations_missing_master_detail=missing,
            ignored_relations=ignored,
            suppressed_relations=suppressed,
            warnings=warning_lines,
            violations=violations,
            passed=True,
        )


def _lexicon_escape_error_result(exc: PathEscapeError) -> UiCoherenceResult:
    """Red/error result for a configured ``dag.lexicon_file`` that escapes root.

    The configured lexicon is the canonical source of the one-to-many relations
    this check reasons about; when it points outside the project tree the check
    cannot hold. Fail-closed (red, block_deploy) rather than skip, because a
    silent skip would let the gate "pass" on metadata it never read (a
    false-green). Distinct from the legacy default filenames simply being absent,
    which stays a legitimate skip.
    """

    violation = {
        "type": "ui_coherence_lexicon_file_out_of_root",
        "lexicon_file": exc.path,
        "severity": "red",
        "block_deploy": True,
        "suggestion": (
            "configured dag.lexicon_file resolves outside the project root, so "
            "ui_coherence_for_one_to_many cannot hold (its one-to-many evidence "
            "source is unreadable); point lexicon_file at an in-root file or remove it"
        ),
    }
    return UiCoherenceResult(
        status="fail",
        severity="red",
        passed=False,
        block_deploy=True,
        skipped=False,
        checked_count=0,
        one_to_many_relations_total=0,
        warnings=[
            "configured dag.lexicon_file out-of-root, ui_coherence_for_one_to_many "
            f"cannot hold ({exc})"
        ],
        violations=[violation],
        message=(
            "ui_coherence_for_one_to_many ERROR: configured dag.lexicon_file "
            f"out-of-root, check cannot hold ({exc})"
        ),
    )


def _missing_relation_entry(relation: Mapping[str, Any]) -> dict[str, Any]:
    parent = str(relation.get("parent") or "")
    child = str(relation.get("child") or "")
    return {
        "parent": parent,
        "child": child,
        "evidence": str(relation.get("evidence") or "one-to-many relation detected"),
        "ui_check": f"no master-detail or drilldown UI evidence found for {parent} -> {child}",
        "suggestion": "Add operation_flow with ui_pattern=master_detail OR add a detail page to ux_design.md",
    }


def _warning_line(item: Mapping[str, Any]) -> str:
    return (
        f"{item.get('parent')} -> {item.get('child')}: "
        "one-to-many relation has no master-detail UI evidence"
    )


def _ignored_by_config(relation: Mapping[str, Any], config: Mapping[str, Any] | None) -> bool:
    ui_config = config.get("ui_coherence") if isinstance(config, Mapping) else None
    if not isinstance(ui_config, Mapping):
        return False
    ignored = ui_config.get("ignore_relations")
    if not isinstance(ignored, list):
        return False
    return any(_same_pair(relation, item) for item in ignored if isinstance(item, Mapping))


def _operation_flow_pattern(
    dag: DAG,
    relation: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    for operation in _operation_flow_entries(dag, config):
        if _operation_matches_relation(operation, relation):
            return str(operation.get("ui_pattern") or "").strip()
    return ""


def _operation_flow_entries(dag: DAG, config: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(config, Mapping):
        entries.extend(operation_flow_operations(config.get("operation_flow")))
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        attributes = node.attributes if isinstance(node.attributes, Mapping) else {}
        entries.extend(operation_flow_operations(attributes.get("operation_flow")))
        frontmatter = attributes.get("frontmatter")
        if isinstance(frontmatter, Mapping):
            entries.extend(operation_flow_operations(frontmatter.get("operation_flow")))
            codd_meta = frontmatter.get("codd")
            if isinstance(codd_meta, Mapping):
                entries.extend(operation_flow_operations(codd_meta.get("operation_flow")))
    return entries


def _operation_matches_relation(operation: Mapping[str, Any], relation: Mapping[str, Any]) -> bool:
    parent = str(operation.get("parent") or "").strip()
    target = str(operation.get("target") or operation.get("child") or "").strip()
    return bool(parent and target and _same_term(parent, relation.get("parent")) and _same_term(target, relation.get("child")))


def _has_master_detail_ui(dag: DAG, relation: Mapping[str, Any], project_root: Path | None) -> bool:
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind != "design_doc":
            continue
        if not _looks_like_ui_design_node(node):
            continue
        text = _node_text(node, project_root)
        if _text_has_relation_ui(text, relation):
            return True
    return False


def _looks_like_ui_design_node(node: Node) -> bool:
    marker = f"{node.id} {node.path or ''}".lower()
    return any(token in marker for token in ("ux", "ui", "api", "design", "screen", "page"))


def _node_text(node: Node, project_root: Path | None) -> str:
    attributes = node.attributes if isinstance(node.attributes, Mapping) else {}
    parts: list[str] = []
    content = attributes.get("content")
    if content is not None:
        parts.append(_without_frontmatter(str(content)))
    if node.path and project_root is not None:
        # ``node.path`` is user-controllable design-doc DAG data. An out-of-root
        # absolute path, a ``../`` traversal, or an in-root symlink that escapes the
        # tree could make an off-root file's master-detail wording PASS-credit a
        # one-to-many relation — a path-escape false-green. ``resolve_project_path``
        # returns ``None`` for any escaped path, so it is simply not read (the
        # relation stays uncredited). In-root files are resolved and read as before.
        resolved = resolve_project_path(project_root, node.path)
        if resolved is not None and resolved.is_file():
            try:
                parts.append(_without_frontmatter(resolved.read_text(encoding="utf-8")))
            except OSError:
                pass
    return "\n\n".join(parts)


def _text_has_relation_ui(text: str, relation: Mapping[str, Any]) -> bool:
    if not text.strip():
        return False
    parent = str(relation.get("parent") or "")
    child = str(relation.get("child") or "")
    for block in _prose_evidence_blocks(text):
        if not _has_distinct_relation_mentions(block, parent, child):
            continue
        if DETAIL_PATH_RE.search(block):
            return True
        if MASTER_DETAIL_RE.search(block) or DRILLDOWN_RE.search(block) or DETAIL_SCREEN_RE.search(block):
            return True
    return False


def _without_frontmatter(text: str) -> str:
    """Keep structured metadata out of the unstructured prose heuristic."""

    return FRONTMATTER_RE.sub("", text, count=1)


def _prose_evidence_blocks(text: str) -> list[str]:
    """Split prose at Markdown boundaries that carry independent claims."""

    blocks: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    for raw_line in _without_frontmatter(text).splitlines():
        line = raw_line.strip()
        if FENCE_RE.match(line):
            flush_paragraph()
            in_fence = not in_fence
            continue
        if not line:
            flush_paragraph()
            continue
        if in_fence or ATOMIC_MARKDOWN_RE.match(line):
            flush_paragraph()
            blocks.append(line)
            continue
        paragraph.append(line)
    flush_paragraph()
    return blocks


def _has_distinct_relation_mentions(text: str, parent: str, child: str) -> bool:
    """Require separate evidence for both ends of a relation.

    Normalized substring matching remains tolerant of separators and casing, but
    occurrences wholly inside the other endpoint (``group`` in
    ``group_entry``) cannot count as separate mentions, even when the longer
    endpoint appears more than once.
    """

    parent_spans = _term_spans(text, parent)
    child_spans = _term_spans(text, child)
    if _norm(parent) != _norm(child):
        original_parent_spans = parent_spans
        original_child_spans = child_spans
        parent_spans = _spans_not_contained_by(parent_spans, original_child_spans)
        child_spans = _spans_not_contained_by(child_spans, original_parent_spans)
    return any(
        parent_end <= child_start or child_end <= parent_start
        for parent_start, parent_end in parent_spans
        for child_start, child_end in child_spans
    )


def _spans_not_contained_by(
    spans: list[tuple[int, int]],
    containers: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    return [
        (start, end)
        for start, end in spans
        if not any(container_start <= start and end <= container_end for container_start, container_end in containers)
    ]


def _term_spans(text: str, term: str) -> list[tuple[int, int]]:
    normalized_text = _norm(text)
    normalized_term = _norm(str(term or ""))
    if not normalized_term:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = normalized_text.find(normalized_term, start)
        if index < 0:
            return spans
        spans.append((index, index + len(normalized_term)))
        start = index + 1


def _same_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _same_term(left.get("parent"), right.get("parent")) and _same_term(left.get("child"), right.get("child"))


def _same_term(left: Any, right: Any) -> bool:
    return _norm(str(left or "")) == _norm(str(right or ""))


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9ぁ-んァ-ン一-龥]+", "", value.lower())


__all__ = ["UiCoherenceCheck", "UiCoherenceResult"]

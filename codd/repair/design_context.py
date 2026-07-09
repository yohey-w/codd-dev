"""Design-doc closure resolution + terminal-reason classification for repair.

Two consumers, one shared (language-blind) core:

* :mod:`codd.repair.llm_repair_engine` injects the failing nodes' design-doc
  bodies — resolved over the transitive ``depends_on`` closure — into the
  analyze / propose prompt, so a repair aligns the IMPLEMENTATION toward the
  canonical design pins and producer declarations, and never rewrites a test to
  pass. (PART A of the v3.22.0 Increment 3 repair-honesty work.)
* :mod:`codd.repair.loop` labels an already-RED terminal
  ``TEST_CONTRACT_OVERREACH`` when the failing assertion's surface tokens are
  PROVABLY absent from the design closure + producer files — a deterministic
  containment check that never creates a green path or changes patch scope.
  (PART B.)

Language-blindness: every selector here keys on CoDD *node kind* / *edge kind*
(data emitted by the DAG builder), never on a programming-language literal. The
containment check is token-based over the design + producer TEXT, so it says
nothing about which language produced that text. This keeps the shared-core
``language ==`` ratchet (``tests/test_language_free_core_ratchet.py``) green.
"""

from __future__ import annotations

from collections import deque
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from codd.path_safety import resolve_project_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from codd.dag import DAG, Node
    from codd.repair.schema import VerificationFailureReport


#: Cap on total bytes of design-doc closure prose folded into a repair prompt.
#: Mirrors ``codd.implementer.DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT`` (20000):
#: the full design prose is the load-bearing signal, but a pathological doc set
#: must never blow the prompt budget.
REPAIR_DESIGN_CONTEXT_PROMPT_LIMIT = 20000

#: Same cap, applied independently to the producer-artifact corpus that feeds
#: ONLY the deterministic containment check (never rendered into a prompt).
PRODUCER_FILES_CORPUS_LIMIT = 20000

#: Default terminal reason when repair exhausts with nothing more to fix.
DEFAULT_TERMINAL_REASON = "ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING"
#: Machine-readable terminal reason: the failing assertion demands a contract
#: absent from the canonical design closure + producer files — i.e. generation
#: overreach, not an implementation bug. A LABEL only; never a green path.
TEST_CONTRACT_OVERREACH_REASON = "TEST_CONTRACT_OVERREACH"

#: CoDD node kinds that carry canonical design prose (data, not a language).
DESIGN_DOC_KINDS = frozenset({"design_doc", "common"})
#: Edge kinds connecting a design doc to another design doc it depends on.
DEPENDS_ON_EDGE_KINDS = frozenset({"depends_on"})
#: Edge kinds connecting a design doc to a source artifact it produces.
PRODUCES_EDGE_KINDS = frozenset({"expects", "represents"})

#: Prompt rule text (PART A). Verbatim per the Increment-3 spec.
DESIGN_CONTEXT_RULE = (
    "The tests are IMMUTABLE. The design documents' pinned surface and the "
    "producer artifacts' declarations are CANONICAL. Align the IMPLEMENTATION "
    "toward them — never rewrite a test to pass."
)

# Surface-token extraction: property/field names and discriminator strings the
# typechecker cannot see are asserted via string-keyed access / ``in`` checks /
# ``toHaveProperty``-style matchers / attribute access — i.e. they appear as
# QUOTED literals, subscript keys, or dotted attribute names in the assertion
# text. We harvest exactly those shapes, then keep the identifier tokens.
_QUOTED_RE = re.compile(r"""['"`]([^'"`]+)['"`]""")
_SUBSCRIPT_RE = re.compile(r"""\[\s*['"`]([^'"`]+)['"`]\s*\]""")
_ATTRIBUTE_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Assertion-framework / test-harness noise that is never a real surface token.
#: Kept small and language-neutral; anything not filtered here is simply matched
#: against the corpus, and a present token only ever SUPPRESSES the label (safe).
_ASSERTION_STOPWORDS = frozenset(
    {
        "assert",
        "assertion",
        "assertionerror",
        "assertequal",
        "asserttrue",
        "assertfalse",
        "error",
        "expect",
        "expected",
        "actual",
        "value",
        "values",
        "true",
        "false",
        "none",
        "null",
        "nil",
        "and",
        "not",
        "the",
        "keyerror",
        "attributeerror",
        "typeerror",
        "valueerror",
        "indexerror",
        "tohaveproperty",
        "tobe",
        "toequal",
        "tocontain",
        "tomatch",
        "test",
        "tests",
        "failed",
        "failure",
        "self",
        "return",
    }
)
#: Minimum identifier length that can count as a meaningful surface token.
_MIN_TOKEN_LEN = 3


def _node(dag: "DAG | None", node_id: str) -> "Node | None":
    return dag.nodes.get(node_id) if dag is not None else None


def _node_path(node: "Node | None") -> str | None:
    if node is None:
        return None
    raw = getattr(node, "path", None)
    if not raw and isinstance(getattr(node, "attributes", None), dict):
        raw = node.attributes.get("path")
    return str(raw) if raw else None


def _is_design_doc(node: "Node | None") -> bool:
    return node is not None and getattr(node, "kind", "") in DESIGN_DOC_KINDS


def _owning_design_docs(dag: "DAG | None", node_id: str) -> list[str]:
    """Design-doc node ids that OWN ``node_id``.

    The node itself when it is a design doc, plus every design doc with a
    producing edge (``expects`` / ``represents``) pointing at it. Deterministic
    insertion order; no language literal.
    """
    node = _node(dag, node_id)
    if _is_design_doc(node):
        return [node_id]
    owners: list[str] = []
    seen: set[str] = set()
    if dag is not None:
        for edge in dag.edges:
            if edge.to_id != node_id or edge.kind not in PRODUCES_EDGE_KINDS:
                continue
            if edge.from_id in seen:
                continue
            if _is_design_doc(_node(dag, edge.from_id)):
                seen.add(edge.from_id)
                owners.append(edge.from_id)
    return owners


def _depends_on_targets(dag: "DAG | None", node_id: str) -> list[str]:
    """Direct ``depends_on`` targets of ``node_id`` (frontmatter data + edges)."""
    targets: list[str] = []
    node = _node(dag, node_id)
    if node is not None and isinstance(getattr(node, "attributes", None), dict):
        for dep in node.attributes.get("depends_on", []) or []:
            dep_id = dep.get("id") if isinstance(dep, dict) else None
            if isinstance(dep_id, str) and dep_id:
                targets.append(dep_id)
    if dag is not None:
        for edge in dag.edges:
            if edge.from_id == node_id and edge.kind in DEPENDS_ON_EDGE_KINDS:
                targets.append(edge.to_id)
    return targets


def design_closure_node_ids(dag: "DAG | None", seed_ids: Iterable[str]) -> list[str]:
    """Transitive ``depends_on`` closure of the design docs owning ``seed_ids``.

    BFS mirroring the implementer's dependency-document walk
    (``_collect_dependency_documents``): resolve each seed to its owning design
    doc(s), then follow ``depends_on`` edges/frontmatter transitively. Returns
    the design-doc node ids in deterministic BFS order. Language-blind.
    """
    if dag is None:
        return []
    start: list[str] = []
    seen_seed: set[str] = set()
    for sid in seed_ids:
        for owner in _owning_design_docs(dag, sid):
            if owner not in seen_seed:
                seen_seed.add(owner)
                start.append(owner)

    ordered: list[str] = []
    seen: set[str] = set()
    queue: deque[str] = deque(start)
    while queue:
        nid = queue.popleft()
        if nid in seen:
            continue
        seen.add(nid)
        if _is_design_doc(_node(dag, nid)):
            ordered.append(nid)
        for target in _depends_on_targets(dag, nid):
            if target not in seen:
                queue.append(target)
    return ordered


def _read_body(project_root: Path | str | None, node: "Node | None") -> str | None:
    if project_root is None or node is None:
        return None
    raw = _node_path(node)
    if not raw:
        return None
    resolved = resolve_project_path(project_root, raw)
    if resolved is None or not resolved.is_file():
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def design_closure_documents(
    dag: "DAG | None",
    seed_ids: Iterable[str],
    project_root: Path | str | None,
    *,
    budget: int = REPAIR_DESIGN_CONTEXT_PROMPT_LIMIT,
) -> list[tuple[str, str]]:
    """``[(path, body)]`` for design docs in the closure, budget-capped.

    Best-effort and read-only: a missing / unreadable / out-of-root doc is
    skipped rather than failing the whole build.
    """
    docs: list[tuple[str, str]] = []
    if project_root is None:
        return docs
    remaining = int(budget)
    for nid in design_closure_node_ids(dag, seed_ids):
        if remaining <= 0:
            break
        node = _node(dag, nid)
        body = _read_body(project_root, node)
        if not body:
            continue
        truncated = body[:remaining]
        remaining -= len(truncated)
        docs.append((_node_path(node) or nid, truncated))
    return docs


def producer_files_text(
    dag: "DAG | None",
    design_node_ids: Iterable[str],
    project_root: Path | str | None,
    *,
    budget: int = PRODUCER_FILES_CORPUS_LIMIT,
) -> str:
    """Concatenated bodies of the source artifacts the closure design docs
    produce (``expects`` / ``represents`` targets that are not themselves design
    docs). Budget-capped; read-only; language-blind."""
    if dag is None or project_root is None:
        return ""
    design_set = set(design_node_ids)
    remaining = int(budget)
    seen: set[str] = set()
    parts: list[str] = []
    for edge in dag.edges:
        if remaining <= 0:
            break
        if edge.from_id not in design_set or edge.kind not in PRODUCES_EDGE_KINDS:
            continue
        target = edge.to_id
        if target in seen:
            continue
        seen.add(target)
        node = _node(dag, target)
        if node is None or _is_design_doc(node):
            continue
        body = _read_body(project_root, node)
        if not body:
            continue
        truncated = body[:remaining]
        remaining -= len(truncated)
        parts.append(truncated)
    return "\n".join(parts)


def render_design_context(
    dag: "DAG | None",
    seed_ids: Iterable[str],
    project_root: Path | str | None,
    *,
    budget: int = REPAIR_DESIGN_CONTEXT_PROMPT_LIMIT,
) -> str:
    """Prompt-ready design context for the failing nodes (PART A).

    Empty string when nothing resolves (no design docs, no project root, or the
    docs are unreadable) — so the prompt is unchanged for failures that do not
    map to a design closure.
    """
    docs = design_closure_documents(dag, seed_ids, project_root, budget=budget)
    if not docs:
        return ""
    sections = [DESIGN_CONTEXT_RULE]
    for path, body in docs:
        sections.append(
            f"--- BEGIN DESIGN DOC {path} ---\n{body.rstrip()}\n--- END DESIGN DOC {path} ---"
        )
    return "\n\n".join(sections)


def surface_tokens(violations: Iterable[Any]) -> set[str]:
    """Identifier-shaped surface tokens from the failing assertions.

    Harvests quoted literals, subscript keys, and dotted attribute names from
    each violation's ``error_messages`` (the type-invisible shape an assertion
    binds to), keeps the identifier tokens, and drops assertion-framework noise
    and sub-``_MIN_TOKEN_LEN`` fragments.
    """
    raw: set[str] = set()
    for violation in violations:
        for message in getattr(violation, "error_messages", None) or []:
            text = str(message)
            for match in _QUOTED_RE.findall(text):
                raw.update(_IDENTIFIER_RE.findall(match))
            for match in _SUBSCRIPT_RE.findall(text):
                raw.update(_IDENTIFIER_RE.findall(match))
            raw.update(_ATTRIBUTE_RE.findall(text))
    return {
        token
        for token in raw
        if len(token) >= _MIN_TOKEN_LEN and token.lower() not in _ASSERTION_STOPWORDS
    }


def _violation_node_ids(violations: Iterable[Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for violation in violations:
        for node_id in getattr(violation, "failed_nodes", None) or []:
            text = str(node_id)
            if text and text not in seen:
                seen.add(text)
                ids.append(text)
    return ids


def classify_terminal_reason(
    violations: list[Any],
    dag: "DAG | None",
    project_root: Path | str | None,
    *,
    default: str = DEFAULT_TERMINAL_REASON,
) -> str:
    """Deterministic terminal-reason label for an already-RED repair terminal.

    Returns :data:`TEST_CONTRACT_OVERREACH_REASON` only when the failing
    assertions' surface tokens are PROVABLY absent from a NON-EMPTY design
    closure + producer corpus. Every "unknown" branch — no violations, no DAG,
    no project root, no resolvable design corpus, no surface tokens — falls back
    to ``default`` (``provably-absent -> assert; unknown -> never assert``).

    This is a LABEL only: it never edits a test, never changes patch scope, and
    is only ever consulted where the loop was already going to terminate RED.
    """
    if not violations or dag is None or project_root is None:
        return default

    seed_ids = _violation_node_ids(violations)
    design_docs = design_closure_documents(dag, seed_ids, project_root)
    if not design_docs:
        # No canonical design corpus resolved -> absence is unprovable.
        return default

    closure_ids = design_closure_node_ids(dag, seed_ids)
    corpus = "\n".join(body for _, body in design_docs)
    producer = producer_files_text(dag, closure_ids, project_root)
    if producer:
        corpus = f"{corpus}\n{producer}"
    corpus_lower = corpus.lower()
    if not corpus_lower.strip():
        return default

    tokens = surface_tokens(violations)
    if not tokens:
        # No surface tokens to test -> absence is unprovable.
        return default

    if all(token.lower() not in corpus_lower for token in tokens):
        return TEST_CONTRACT_OVERREACH_REASON
    return default


__all__ = [
    "DEFAULT_TERMINAL_REASON",
    "DESIGN_CONTEXT_RULE",
    "PRODUCER_FILES_CORPUS_LIMIT",
    "REPAIR_DESIGN_CONTEXT_PROMPT_LIMIT",
    "TEST_CONTRACT_OVERREACH_REASON",
    "classify_terminal_reason",
    "design_closure_documents",
    "design_closure_node_ids",
    "producer_files_text",
    "render_design_context",
    "surface_tokens",
]

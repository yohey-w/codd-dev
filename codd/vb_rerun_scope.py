"""Targeted-test-rerun scope for the VB coverage gate.

When the implement-stage VB coverage gate finds uncovered verifiable behaviors,
it re-runs implementation with gap feedback (the same gate→feedback→rerun shape
the native oracle uses). But — unlike the oracle, which legitimately rewrites
the broken-edge source/test files — the VB gate's responsibility is "does a
verification-test CLAIM exist for every behavior?". Its rerun must therefore:

* target the TEST task(s) that should own the uncovered behaviors, NOT every
  source task (re-running source codegen to satisfy a *coverage* gap is wrong:
  it risks rewriting working production code to chase a test claim); and
* be write-fenced to TEST files + test helpers ONLY — a new test that fails
  because of a real production defect is the verify/repair stage's job, not the
  VB gate's. The VB gate never edits source.

This module derives that scope. It reuses the oracle's
:class:`~codd.implement_oracle_scope.OracleRerunScope` shape (``task_ids`` +
write-fence ``allowed_paths``) so the existing fenced-rerun dispatch
(``GreenfieldPipeline._rerun_tasks_with_feedback``) drives it unchanged.

GENERALITY: scope derivation is path/intent-based (a task is a "test task" when
its design node is itself a test-type document, or its declared outputs land
under the configured test dirs, or its output filenames look like tests). No
language-specific logic. A project with no resolvable test task degrades to a
broad rerun (every task), exactly as the oracle does — never a no-op.

NOTE: ``task.test_kinds`` is deliberately NOT consulted here, even though it
looks like an obvious signal. Per ``ImplementTaskRef.test_kinds`` (see
``codd/greenfield/pipeline.py``), it is V-model coverage-layer metadata
("this task's deliverable is verified at the unit/e2e layer") that a derived
SOURCE task carries just as often as a derived TEST task — it is explicitly
documented elsewhere in this codebase as "coverage-level metadata, not a
deliverable contract" for exactly this reason. Treating a non-empty
``test_kinds`` as "this task IS a test task" pulled plain source tasks (e.g. a
derived ``implement_<module>`` task) into the VB gate's test-only rerun scope
and write-fence, contradicting this module's own "never rewrite production
source" guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

from codd.implement_oracle_scope import SCOPE_BROAD, OracleRerunScope

#: Rung label recorded on the scope. The TARGETED rung is a non-broad scope (so
#: the write-fence dispatch fences it); the BROAD fallback MUST reuse the oracle's
#: canonical ``SCOPE_BROAD`` so ``OracleRerunScope.is_broad()`` returns True and
#: the existing ``_rerun_tasks_with_feedback`` dispatch runs it unfenced (a broad
#: rerun legitimately regenerates everything; an empty allow-set must NOT be
#: mistaken for a fence that reverts the whole tree).
SCOPE_VB_TARGETED = "vb_targeted"
SCOPE_VB_BROAD = SCOPE_BROAD

_TEST_FILENAME_RE = re.compile(r"(^test_|_test|\.spec\.|\.test\.|\.e2e\.|\.e2e-spec\.|\.cy\.)", re.IGNORECASE)

#: Doc-shaped extensions: a declared output ending in one of these is a DOCUMENT
#: (e.g. the canonical VB registry ``docs/test/test_strategy.md``), never an
#: authored TEST file. It matters here because such a doc's filename is itself
#: test-shaped (``test_strategy.md`` matches ``^test_``), so ``_is_test_output``
#: alone would misread the doc-only registry task as one that authors a covering
#: test — which is exactly the inert-rerun trap this module must avoid.
_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip("/")


def _test_dir_prefixes(config: dict[str, Any] | None) -> list[str]:
    raw = ((config or {}).get("scan") or {}).get("test_dirs")
    dirs = [str(item) for item in raw] if isinstance(raw, list) and raw else ["tests/"]
    return [_norm(item) for item in dirs if _norm(item)]


def _is_test_output(path: str, *, test_prefixes: Sequence[str]) -> bool:
    rel = _norm(path)
    if not rel:
        return False
    for prefix in test_prefixes:
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    name = PurePosixPath(rel).name
    return bool(_TEST_FILENAME_RE.search(name))


def _authors_test_artifact_path(path: str, *, test_prefixes: Sequence[str]) -> bool:
    """Whether a declared output is an authored TEST artifact (not a document).

    A test-shaped path whose extension is a DOCUMENT extension (the canonical VB
    registry ``docs/test/test_strategy.md``) authors no covering test — its
    implement is a deterministic no-op, so a rerun targeting it is inert. A test
    DIRECTORY (no extension) under a test root, or a real test FILE, does author
    tests.
    """
    rel = _norm(path)
    if not rel:
        return False
    if PurePosixPath(rel).suffix.lower() in _DOC_EXTENSIONS:
        return False
    return _is_test_output(rel, test_prefixes=test_prefixes)


def _task_authors_test_artifact(
    task: Any,
    *,
    config: dict[str, Any] | None,
    path_resolver: Any,
    test_prefixes: Sequence[str],
) -> bool:
    """Whether a test task's OWN declared outputs include an authored test artifact.

    Distinguishes a genuine test-authoring task from a doc-only "test task" (the
    canonical registry task, classified a test task by its ``docs/test/`` node but
    whose only output is the registry document). The latter must never be the sole
    rerun target — its rerun authors nothing.
    """
    candidates = list(getattr(task, "expected_outputs", ()) or [])
    candidates.extend(_resolve_paths(task, config, path_resolver))
    return any(_authors_test_artifact_path(path, test_prefixes=test_prefixes) for path in candidates)


def task_is_test_task(
    task: Any,
    *,
    config: dict[str, Any] | None,
    resolved_output_paths: Iterable[str] | None = None,
) -> bool:
    """Whether an implement task's OWN deliverable is a TEST artifact.

    True when the task's design node is a test-type node (``test:`` id or under
    ``docs/test/``), or any of its declared/resolved output paths land under a
    test dir (or look like a test file). Mirrors
    :func:`codd.verifiable_behavior_audit.is_test_related_implement` but
    operates on an :class:`~codd.greenfield.pipeline.ImplementTaskRef` — and,
    like that sibling, deliberately does NOT consult ``task.test_kinds`` (see
    the module docstring: it is V-model coverage-layer metadata populated on
    source tasks and test tasks alike, not a deliverable-kind signal).
    """

    design_node = str(getattr(task, "design_node", "") or "")
    normalized = design_node.strip().replace("\\", "/")
    if normalized.startswith("test:"):
        return True
    parts = PurePosixPath(normalized).parts
    if len(parts) >= 3 and parts[0] == "docs" and parts[1] == "test":
        return True

    test_prefixes = _test_dir_prefixes(config)
    candidates = list(resolved_output_paths or []) or list(getattr(task, "output_paths", ()) or [])
    for candidate in candidates:
        if _is_test_output(candidate, test_prefixes=test_prefixes):
            return True
    # Also treat declared expected_outputs that look test-y as a signal.
    for candidate in getattr(task, "expected_outputs", ()) or ():
        if _is_test_output(candidate, test_prefixes=test_prefixes):
            return True
    return False


@dataclass(frozen=True)
class _TaskView:
    """Minimal projection of an ImplementTaskRef for scope derivation."""

    task_id: str
    design_node: str
    output_paths: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    test_kinds: tuple[str, ...]


def _resolve_paths(task: Any, config: dict[str, Any] | None, path_resolver: Any) -> list[str]:
    declared = list(getattr(task, "output_paths", ()) or [])
    if declared:
        return declared
    if path_resolver is not None:
        try:
            return list(path_resolver(config or {}, task) or [])
        except Exception:  # noqa: BLE001 — a task whose paths fail just contributes none.
            return []
    return []


def derive_vb_rerun_scope(
    uncovered_source_docs: Iterable[str],
    tasks: Sequence[Any],
    *,
    config: dict[str, Any] | None,
    path_resolver: Any = None,
    test_helper_dirs: Iterable[str] | None = None,
) -> OracleRerunScope:
    """Derive the test-task scope to re-run for the uncovered VBs.

    ``uncovered_source_docs`` are the ``source_doc`` display paths of the
    uncovered VB rows (their declaring test docs). ``path_resolver`` is the
    pipeline's ``_output_paths_for_task`` (config → declared output paths) so a
    task with no inline ``output_paths`` still resolves its outputs.

    Selection (in order of preference):

    1. Test tasks whose design node corresponds to an uncovered VB's source doc
       (a test doc → its implementing test task), grouped together.
    2. If none match by doc, ALL test tasks (the build's test tasks own the test
       suite collectively — batch the gap to them rather than re-running source).
    3. If there are no recognizable test tasks at all, fall back to a BROAD scope
       (every task) — never a no-op; the bounded retry still applies.

    The write-fence ``allowed_paths`` are the selected test tasks' resolved
    output paths UNION the configured test dirs UNION any test-helper dirs — i.e.
    TEST surface only. Source files are intentionally excluded so a VB rerun can
    never rewrite production code (anti-false-green: the gate proves test claims,
    it does not fix implementations).
    """

    config = config or {}
    docs = {_norm(doc) for doc in uncovered_source_docs if _norm(doc)}
    test_prefixes = _test_dir_prefixes(config)

    test_tasks: list[Any] = []
    for task in tasks:
        resolved = _resolve_paths(task, config, path_resolver)
        if task_is_test_task(task, config=config, resolved_output_paths=resolved):
            test_tasks.append(task)

    if not test_tasks:
        # No test task to target — broad fallback (the oracle's empty-allow shape).
        return OracleRerunScope(
            rung=SCOPE_VB_BROAD,
            task_ids=tuple(getattr(t, "task_id", "") for t in tasks),
            allowed_paths=(),  # empty ⇒ no fence (broad regenerates everything)
            detail="VB rerun: no recognizable test task — broad fallback",
        )

    # Stage 1: test tasks tied to an uncovered VB's source doc.
    def _task_matches_doc(task: Any) -> bool:
        if not docs:
            return False
        node = _norm(str(getattr(task, "design_node", "") or ""))
        # The test doc path or node tail matching the design node / its outputs.
        node_tail = PurePosixPath(node).name if node else ""
        for doc in docs:
            doc_tail = PurePosixPath(doc).name
            if node and (node == doc or node.endswith("/" + doc) or doc.endswith(node)):
                return True
            if node_tail and doc_tail and node_tail.split(".")[0] in doc:
                return True
        return False

    # A test task AUTHORS a test artifact unless its only outputs are documents
    # (the canonical registry task): dropping the doc-only task keeps a rerun from
    # resolving ONLY to a no-authored-artifact target (an inert rerun that authors
    # nothing — the 2026-07 S3 StockRoom-mini burn, where every residual VB shared
    # the registry doc, so stage-1 matched only ``document_test_strategy``).
    authoring = [
        task
        for task in test_tasks
        if _task_authors_test_artifact(
            task, config=config, path_resolver=path_resolver, test_prefixes=test_prefixes
        )
    ]
    authoring_ids = {id(task) for task in authoring}

    targeted = [task for task in test_tasks if _task_matches_doc(task)]
    targeted_authoring = [task for task in targeted if id(task) in authoring_ids]

    rung = SCOPE_VB_TARGETED
    if targeted_authoring:
        selected = targeted_authoring
        detail = f"VB rerun: {len(selected)} authoring test task(s) targeted by uncovered VB source doc(s)"
    elif authoring:
        # Stage-1 matched only no-authored-artifact task(s) (the doc-only registry
        # task) or nothing by doc — fall through to the test tasks that actually
        # author test files so the residual VBs get authored (was: inert rerun).
        selected = authoring
        detail = f"VB rerun: all {len(selected)} authoring test task(s) (doc match inert/absent) — batched"
    elif targeted:
        selected = targeted  # only non-authoring test task(s) exist — preserve prior scope
        detail = f"VB rerun: {len(selected)} test task(s) targeted by uncovered VB source doc(s)"
    else:
        selected = test_tasks
        detail = f"VB rerun: all {len(selected)} test task(s) (no per-doc match) — batched"

    allowed: list[str] = []

    def _add_allowed(path: str) -> None:
        norm = _norm(path)
        if norm and norm not in allowed:
            allowed.append(norm)

    for task in selected:
        for path in _resolve_paths(task, config, path_resolver):
            _add_allowed(path)
    # Always permit the configured test dirs + helper dirs (the test surface).
    for prefix in test_prefixes:
        _add_allowed(prefix)
    for helper in test_helper_dirs or ():
        _add_allowed(helper)

    return OracleRerunScope(
        rung=rung,
        task_ids=tuple(getattr(t, "task_id", "") for t in selected),
        allowed_paths=tuple(allowed),
        detail=detail,
    )

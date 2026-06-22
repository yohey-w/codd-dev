"""Tests for the field-less / expected-bridge generalization of the impact planner.

These cover the coverage/precision generalization (GPT-5.5 design,
``codd_coverage_design_gpt_20260622.md``):

* CASE B — a FIELD-LESS styling change (button gradient) where the LLM
  obligations are abstract (``theme.update`` / ``ui.display``) and the real
  files (a stylesheet + a button component) are reached via the EXPECTED-BRIDGE
  plus concrete anchors (``button`` / ``gradient`` / ``primary`` / a hex).
* GUARD (a) — exact ``expects`` targets present but NO concrete anchor ⇒ NOT
  ``complete`` (the bridge is gated on ``anchors.specific`` being non-empty).
* GUARD (b) — a ``concrete_write`` obligation (api surface + create verb) is
  NOT satisfied by the abstract bridge alone (needs the create literal).
* GUARD (c) — a too-broad expected envelope (> cardinality cap exact targets)
  ⇒ ``ambiguous``.
* GUARD (d) — bridge-capacity: a single expected file as the sole bridge for
  multiple abstract obligations ⇒ ``ambiguous``.

Plus direct unit tests of the changed internal signatures
(``_score_and_accept`` takes an ``AnchorPolicy``; ``_covers_direct`` /
``_covers_via_expected_bridge`` take a ``CoverageContext``).

All fixtures are tmp-local: no network, no AI, no live commands. Concrete
framework/domain names appear only in the *fixtures* (allowed); the planner
core stays name-free.
"""

from __future__ import annotations

from pathlib import Path

from codd.dag import DAG, Edge, Node
from codd.fix.impact_planner import (
    AnchorPolicy,
    AnchorSets,
    CoverageContext,
    ExpectedEnvelope,
    ImpactObligation,
    ImpactEvidence,
    ImplCandidate,
    _covers_direct,
    _covers_via_expected_bridge,
    _score_and_accept,
    resolve_impact_plan,
)
from codd.fix.design_updater import DesignUpdate
from codd.fix.phenomenon_parser import PhenomenonAnalysis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_file(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


_CSS = "src/styles/globals.css"
_BTN = "src/components/button.tsx"
_DESIGN = "docs/design/ux_design.md"


def _styling_dag_with_expects(tmp_path: Path, *, targets: list[str]) -> DAG:
    """A design node with EXACT ``expects`` edges to the given impl targets."""
    dag = DAG()
    _make_file(
        tmp_path,
        _DESIGN,
        "---\ncodd:\n  id: ux_design\n---\n# UX\nprimary button gradient\n",
    )
    dag.add_node(
        Node(id=_DESIGN, kind="design_doc", path=_DESIGN, attributes={"frontmatter": {}})
    )
    for rel in targets:
        dag.add_node(Node(id=rel, kind="impl_file", path=rel))
        dag.add_edge(Edge(from_id=_DESIGN, to_id=rel, kind="expects"))
    return dag


def _make_styling_files(root: Path) -> None:
    _make_file(
        root,
        _CSS,
        ":root {\n  --color-primary-gradient: "
        "linear-gradient(90deg, #1d4ed8, #1e3a8a);\n}\n",
    )
    _make_file(
        root,
        _BTN,
        "export function PrimaryButton() {\n"
        "  // primary button uses a gradient background\n"
        "  return null;\n}\n",
    )
    # An unrelated component in the SAME directory must NOT be pulled in.
    _make_file(
        root, "src/components/footer.tsx", "export function Footer() { return null; }\n"
    )


def _abstract_styling_analysis() -> PhenomenonAnalysis:
    """Field-less: NO data fields; obligations are abstract facet labels."""
    return PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["button", "gradient"],
        entities=["button"],
        fields=[],
        operations=["update", "display"],
        surfaces=["ui", "theme"],
        obligations=[
            {
                "id": "theme.update",
                "description": "update the theme gradient",
                "terms": ["theme", "update"],
            },
            {
                "id": "ui.display",
                "description": "display the button",
                "terms": ["ui", "display"],
            },
        ],
    )


def _styling_design_update() -> DesignUpdate:
    return DesignUpdate(
        target_path=Path(_DESIGN),
        original_content="# UX\n",
        proposed_content="# UX\nprimary button gradient #1d4ed8\n",
        diff="+primary button gradient #1d4ed8\n+--color-primary-gradient",
        changed=True,
    )


# ---------------------------------------------------------------------------
# CASE B — field-less styling via expected-bridge + anchors
# ---------------------------------------------------------------------------


def test_case_b_fieldless_styling_resolves_both_files_via_expected_bridge(tmp_path):
    dag = _styling_dag_with_expects(tmp_path, targets=[_CSS, _BTN])
    _make_styling_files(tmp_path)

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="make the primary button use a blue gradient (#1d4ed8) background",
        analysis=_abstract_styling_analysis(),
        design_updates=[_styling_design_update()],
    )

    assert plan.status == "complete", (
        f"status={plan.status!r}, unresolved={plan.unresolved_obligations!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )
    # EXACTLY the stylesheet + the button component; the unrelated sibling out.
    assert set(plan.impl_paths) == {_CSS, _BTN}, plan.impl_paths
    assert "src/components/footer.tsx" not in plan.impl_paths
    assert plan.unresolved_obligations == []
    # Both abstract obligations are covered, and they reach the real files.
    assert "theme.update" in plan.covered_obligations
    assert "ui.display" in plan.covered_obligations
    assert _CSS in plan.covered_obligations["theme.update"]
    assert _BTN in plan.covered_obligations["ui.display"]


def test_case_b_works_with_empty_llm_decomposition(tmp_path):
    """Model-agnostic: with NO LLM obligations, the exact expects still yield
    expected-target obligations and (given an anchor) resolve complete."""
    dag = _styling_dag_with_expects(tmp_path, targets=[_CSS, _BTN])
    _make_styling_files(tmp_path)

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["button", "gradient"],
        entities=["button"],
        fields=[],
        operations=[],
        surfaces=[],
        obligations=[],  # empty decomposition
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="primary button gradient #1d4ed8",
        analysis=analysis,
        design_updates=[_styling_design_update()],
    )

    assert plan.status == "complete", (
        f"status={plan.status!r}, diagnostics={plan.diagnostics!r}"
    )
    assert set(plan.impl_paths) == {_CSS, _BTN}
    # Expected-target obligations exist even without an LLM decomposition.
    assert any(o.id.startswith("expected.") for o in plan.obligations)


# ---------------------------------------------------------------------------
# GUARD (a) — expects targets but NO concrete anchor => not complete
# ---------------------------------------------------------------------------


def test_guard_expects_only_without_anchor_is_not_complete(tmp_path):
    """The bridge is gated on a concrete anchor existing somewhere. With expects
    targets but ZERO specific anchors (generic facet words only), the abstract
    obligations CANNOT be bridged => status must NOT be complete."""
    dag = _styling_dag_with_expects(tmp_path, targets=[_CSS, _BTN])
    # Files contain only generic facet words — no button/gradient/hex anchor.
    _make_file(tmp_path, _CSS, ":root { /* update theme */ }\n")
    _make_file(
        tmp_path, _BTN, "export function X() { /* ui display update */ return null; }\n"
    )

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=[],
        entities=[],
        fields=[],
        operations=["update", "display"],
        surfaces=["ui", "theme"],
        obligations=[
            {"id": "theme.update", "description": "update theme", "terms": ["theme", "update"]},
            {"id": "ui.display", "description": "display ui", "terms": ["ui", "display"]},
        ],
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        # Phenomenon text carries no identifier-ish anchor either.
        phenomenon_text="update the ui theme",
        analysis=analysis,
        # No design diff with added identifiers.
        design_updates=[],
    )

    assert plan.status != "complete", (
        f"expects-only-without-anchor must not be complete; "
        f"status={plan.status!r}, covered={plan.covered_obligations!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )
    assert plan.status in {"incomplete", "ambiguous"}
    # The abstract facet obligations were NOT bridge-satisfied.
    assert "theme.update" in plan.unresolved_obligations
    assert "ui.display" in plan.unresolved_obligations


# ---------------------------------------------------------------------------
# GUARD (b) — concrete_write obligation not satisfiable by bridge alone
# ---------------------------------------------------------------------------


def test_guard_concrete_write_not_satisfied_by_bridge_alone(tmp_path):
    """An api.create obligation (api surface + create verb) is a concrete write:
    even with the file as an exact expects target and a concrete anchor present,
    the bridge must NOT cover it — the create literal is required."""
    api_rel = "src/app/api/widgets/route.ts"
    dag = _styling_dag_with_expects(tmp_path, targets=[api_rel])
    # The expected target carries the anchor but NOT the create literal.
    _make_file(
        tmp_path,
        api_rel,
        "export async function GET() { /* widget gradient read */ }\n",
    )

    analysis = PhenomenonAnalysis(
        intent="new_feature",
        subject_terms=["widget", "gradient"],
        entities=["widget"],
        fields=[],
        operations=["create"],
        surfaces=["api"],
        obligations=[
            {
                "id": "api.create",
                "description": "persist a new widget via the API",
                "terms": ["api", "create"],
            }
        ],
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="add a widget with a gradient via the api",
        analysis=analysis,
        design_updates=[
            DesignUpdate(
                target_path=Path(_DESIGN),
                original_content="# UX\n",
                proposed_content="# UX\nwidget gradient\n",
                diff="+widget gradient",
                changed=True,
            )
        ],
    )

    # api.create is concrete_write — bridge cannot cover it without the literal.
    assert plan.status != "complete", (
        f"concrete_write must not be bridge-covered; status={plan.status!r}, "
        f"covered={plan.covered_obligations!r}, diagnostics={plan.diagnostics!r}"
    )
    create_oblig = next(o for o in plan.obligations if o.id == "api.create")
    # It is classified as a concrete write (api surface + create verb), which is
    # what makes the bridge route refuse it regardless of allow_expected_bridge.
    assert create_oblig.concrete_write is True
    assert "api.create" not in plan.covered_obligations
    assert "api.create" in plan.unresolved_obligations


def test_guard_concrete_write_satisfied_when_literal_present(tmp_path):
    """Bracket for GUARD (b): the SAME api.create flips to covered once the
    create literal is present on the api surface (direct coverage)."""
    api_rel = "src/app/api/widgets/route.ts"
    dag = _styling_dag_with_expects(tmp_path, targets=[api_rel])
    # Now the file carries the create verb literal + the api surface + anchor.
    _make_file(
        tmp_path,
        api_rel,
        "export async function POST() { /* api create widget gradient */ }\n",
    )

    analysis = PhenomenonAnalysis(
        intent="new_feature",
        subject_terms=["widget", "gradient"],
        entities=["widget"],
        fields=[],
        operations=["create"],
        surfaces=["api"],
        obligations=[
            {
                "id": "api.create",
                "description": "persist a new widget via the API",
                "terms": ["api", "create"],
            }
        ],
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="add a widget with a gradient via the api create",
        analysis=analysis,
        design_updates=[
            DesignUpdate(
                target_path=Path(_DESIGN),
                original_content="# UX\n",
                proposed_content="# UX\nwidget gradient\n",
                diff="+widget gradient",
                changed=True,
            )
        ],
    )

    assert "api.create" in plan.covered_obligations, (
        f"api.create should be directly covered (literal present); "
        f"status={plan.status!r}, diagnostics={plan.diagnostics!r}"
    )
    assert api_rel in plan.covered_obligations["api.create"]


# ---------------------------------------------------------------------------
# GUARD (c) — too-broad expected envelope => ambiguous
# ---------------------------------------------------------------------------


def test_guard_too_broad_expected_envelope_is_ambiguous(tmp_path):
    """An expects envelope naming more exact targets than the cardinality cap is
    refused as ambiguous (a sprawling 'expects everything' must not be silently
    bridge-covered to complete)."""
    targets = [f"src/components/comp_{i}.tsx" for i in range(8)]
    dag = _styling_dag_with_expects(tmp_path, targets=targets)
    for rel in targets:
        _make_file(tmp_path, rel, "// button gradient primary component\n")

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="button gradient primary #1d4ed8 across components",
        analysis=_abstract_styling_analysis(),
        design_updates=[_styling_design_update()],
        # Cap below the number of exact targets => envelope is too broad.
        max_impl_candidates=4,
    )

    assert plan.status == "ambiguous", (
        f"too-broad expected envelope must be ambiguous; status={plan.status!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )


# ---------------------------------------------------------------------------
# GUARD (d) — bridge-capacity: one file as sole bridge for many obligations
# ---------------------------------------------------------------------------


def test_guard_bridge_capacity_one_file_many_abstract_obligations(tmp_path):
    """A single expected file that is the SOLE bridge for multiple abstract
    obligations trips the capacity guard => ambiguous. (One file silently
    'satisfying' several distinct facets with no direct evidence is exactly the
    over-cover the guard exists to refuse.)"""
    only = "src/styles/globals.css"
    dag = _styling_dag_with_expects(tmp_path, targets=[only])
    # One stylesheet, carrying an anchor so the bridge is even eligible.
    _make_file(
        tmp_path,
        only,
        ":root { --color-primary-gradient: linear-gradient(#1d4ed8); }\n",
    )

    # Three abstract obligations, all bridge-coverable, all reaching the one file.
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["gradient"],
        entities=[],
        fields=[],
        operations=["update", "display", "change"],
        surfaces=["theme", "ui", "style"],
        obligations=[
            {"id": "theme.update", "description": "update theme", "terms": ["theme", "update"]},
            {"id": "ui.display", "description": "display ui", "terms": ["ui", "display"]},
            {"id": "style.change", "description": "change style", "terms": ["style", "change"]},
        ],
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="change the primary gradient #1d4ed8",
        analysis=analysis,
        design_updates=[
            DesignUpdate(
                target_path=Path(_DESIGN),
                original_content="# UX\n",
                proposed_content="# UX\ngradient #1d4ed8\n",
                diff="+gradient #1d4ed8\n+--color-primary-gradient",
                changed=True,
            )
        ],
    )

    assert plan.status == "ambiguous", (
        f"one file as sole bridge for many obligations must be ambiguous; "
        f"status={plan.status!r}, covered={plan.covered_obligations!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )
    assert any("capacity" in d for d in plan.diagnostics), plan.diagnostics


# ---------------------------------------------------------------------------
# Changed internal signatures — direct unit tests
# ---------------------------------------------------------------------------


def _anchor_ctx(specific: set[str], expected_path: str, design_id: str) -> CoverageContext:
    env = ExpectedEnvelope(
        design_node_id=design_id,
        paths=frozenset({expected_path}),
        exact=True,
        source="expects",
        too_broad=False,
    )
    return CoverageContext(
        # In these unit tests the anchors stand in for phenomenon/diff signals,
        # so they are non-target (they authorize the bridge).
        anchors=AnchorSets(specific=set(specific), specific_nontarget=set(specific)),
        expected_by_design={design_id: env},
        expected_by_path={expected_path: {design_id}},
    )


def test_score_and_accept_takes_anchor_policy_hard_source_admits():
    cand = ImplCandidate(path="a.css")
    cand.evidences.append(
        ImpactEvidence(source="expects", detail="d", weight=1.0, category="expected")
    )
    _score_and_accept(
        cand,
        min_score=0.55,
        min_independent_sources=2,
        anchor_policy=AnchorPolicy(field_terms_present=True, specific_terms=frozenset()),
    )
    assert cand.accepted is True  # hard source admits regardless of anchors


def test_score_and_accept_rejects_soft_without_specific_anchor():
    cand = ImplCandidate(path="a.tsx")
    # Two independent path sources but only generic (surface) categories.
    cand.evidences.append(
        ImpactEvidence(source="path_segment", detail="ui", weight=0.35, category="surface")
    )
    cand.evidences.append(
        ImpactEvidence(source="path_basename", detail="theme", weight=0.25, category="surface")
    )
    _score_and_accept(
        cand,
        min_score=0.55,
        min_independent_sources=2,
        anchor_policy=AnchorPolicy(
            field_terms_present=True, specific_terms=frozenset({"gradient"})
        ),
    )
    assert cand.accepted is False
    assert "anchor" in cand.reject_reason


def test_covers_direct_requires_discriminator_and_handles_concrete_write():
    ctx = _anchor_ctx({"gradient"}, "a.tsx", "d")
    # Candidate with an anchor discriminator + the create literal + api surface.
    cand = ImplCandidate(path="a.tsx")
    cand.evidences.append(
        ImpactEvidence(source="content_token", detail="gradient", weight=0.12, category="anchor")
    )
    cand.evidences.append(
        ImpactEvidence(source="content_token", detail="api", weight=0.12, category="surface")
    )
    cand.evidences.append(
        ImpactEvidence(source="content_token", detail="create", weight=0.12, category="operation")
    )
    concrete = ImpactObligation(
        id="api.create",
        description="",
        required_surface=["api"],
        required_operation=["create"],
        concrete_write=True,
    )
    assert _covers_direct(cand, concrete, ctx) is True

    # Remove the create literal: concrete write no longer directly covered.
    cand2 = ImplCandidate(path="a.tsx")
    cand2.evidences.append(
        ImpactEvidence(source="content_token", detail="gradient", weight=0.12, category="anchor")
    )
    cand2.evidences.append(
        ImpactEvidence(source="content_token", detail="api", weight=0.12, category="surface")
    )
    assert _covers_direct(cand2, concrete, ctx) is False


def test_covers_via_expected_bridge_requires_anchor_and_exact_target():
    ctx = _anchor_ctx({"gradient"}, "a.tsx", "d")
    cand = ImplCandidate(path="a.tsx")
    cand.evidences.append(
        ImpactEvidence(source="expects", detail="d", weight=1.0, category="expected")
    )
    abstract = ImpactObligation(
        id="theme.update",
        description="",
        required_surface=[],
        required_operation=[],
        allow_expected_bridge=True,
        concrete_write=False,
    )
    assert _covers_via_expected_bridge(cand, abstract, ctx) is True

    # No specific anchor => bridge refuses.
    ctx_no_anchor = _anchor_ctx(set(), "a.tsx", "d")
    assert _covers_via_expected_bridge(cand, abstract, ctx_no_anchor) is False

    # concrete_write obligation => bridge refuses even with anchor + exact target.
    concrete = ImpactObligation(
        id="api.create",
        description="",
        required_operation=["create"],
        allow_expected_bridge=False,
        concrete_write=True,
    )
    assert _covers_via_expected_bridge(cand, concrete, ctx) is False

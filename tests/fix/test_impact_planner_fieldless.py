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
    _bridge_capacity_ok,
    _content_certificate_label,
    _covers_direct,
    _covers_via_expected_bridge,
    _literal_strength,
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


def test_styling_file_with_rare_literal_anchor_resolves_complete_via_direct(tmp_path):
    """A content-only styling file bearing a rare/literal anchor (a unique hex)
    is DIRECTLY anchor-discovered and covers the abstract styling facets — no
    expects bridge needed. This is the field-less success case: the discriminator
    is a specific anchor in the body, not a declared data field. (Previously such
    a file could only be reached via the expects bridge; now its own content
    certificate makes it a first-class direct target.)"""
    only = "src/styles/globals.css"
    dag = _styling_dag_with_expects(tmp_path, targets=[only])
    # One stylesheet whose body carries a unique hex literal (the change value).
    _make_file(
        tmp_path,
        only,
        ":root { --color-primary-gradient: linear-gradient(#1d4ed8); }\n",
    )

    # Three abstract styling facets — all covered directly by the one file's
    # anchor discriminator (no surface literal required for abstract facets).
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

    assert plan.status == "complete", (
        f"a rare-literal-anchored styling file must resolve complete via direct "
        f"coverage; status={plan.status!r}, covered={plan.covered_obligations!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )
    assert only in plan.impl_paths, plan.impl_paths
    # Coverage is DIRECT, not bridge — no capacity refusal.
    assert not any("capacity" in d for d in plan.diagnostics), plan.diagnostics


def test_bridge_capacity_guard_refuses_one_file_sole_bridge_for_many():
    """Unit: the capacity guard refuses when one path is the sole bridge for
    more than one obligation (anti-false-green over-cover)."""
    # One file is the sole bridge for two distinct obligations => refuse.
    assert _bridge_capacity_ok({"o1": ["a.tsx"], "o2": ["a.tsx"]}) is False
    # One file bridging a single obligation is fine.
    assert _bridge_capacity_ok({"o1": ["a.tsx"]}) is True
    # Multiply-bridged obligations (>1 path each) don't pin any single file.
    assert _bridge_capacity_ok({"o1": ["a.tsx", "b.tsx"], "o2": ["a.tsx", "c.tsx"]}) is True
    # Distinct sole bridges for distinct obligations are fine.
    assert _bridge_capacity_ok({"o1": ["a.tsx"], "o2": ["b.tsx"]}) is True


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


def test_covers_via_expected_bridge_requires_candidate_alignment_and_exact_target():
    ctx = _anchor_ctx({"gradient"}, "a.tsx", "d")
    abstract = ImpactObligation(
        id="theme.update",
        description="",
        required_surface=[],
        required_operation=[],
        allow_expected_bridge=True,
        concrete_write=False,
    )

    # An exact expects target that ALSO carries its own non-target anchor
    # ("gradient") aligns the bridge => covered.
    cand = ImplCandidate(path="a.tsx")
    cand.evidences.append(
        ImpactEvidence(source="expects", detail="d", weight=1.0, category="expected")
    )
    cand.evidences.append(
        ImpactEvidence(source="content_token", detail="gradient", weight=0.12, category="anchor")
    )
    assert _covers_via_expected_bridge(cand, abstract, ctx) is True

    # A BARE expects target (no concrete signal of its own) must NOT bridge —
    # a stale/imprecise expects edge cannot fake semantic coverage
    # (anti-false-green: this is the candidate-alignment requirement).
    bare = ImplCandidate(path="a.tsx")
    bare.evidences.append(
        ImpactEvidence(source="expects", detail="d", weight=1.0, category="expected")
    )
    assert _covers_via_expected_bridge(bare, abstract, ctx) is False

    # No specific anchor in context => even an anchor-bearing candidate cannot
    # align (its detail is not a recognized non-target anchor).
    ctx_no_anchor = _anchor_ctx(set(), "a.tsx", "d")
    assert _covers_via_expected_bridge(cand, abstract, ctx_no_anchor) is False

    # concrete_write obligation => bridge refuses even when aligned.
    concrete = ImpactObligation(
        id="api.create",
        description="",
        required_operation=["create"],
        allow_expected_bridge=False,
        concrete_write=True,
    )
    assert _covers_via_expected_bridge(cand, concrete, ctx) is False


# ---------------------------------------------------------------------------
# Content-certificate route (field-less discriminator) + selection separation
# ---------------------------------------------------------------------------


def _cert_anchors(specific_nontarget: set[str], content_df: dict[str, int]) -> AnchorSets:
    return AnchorSets(
        specific=set(specific_nontarget),
        specific_nontarget=set(specific_nontarget),
        content_df=dict(content_df),
        df=dict(content_df),
    )


def test_literal_strength_separates_values_from_plain_words():
    # A hex value is strongly literal; a kebab construct is moderately literal;
    # a bare alphabetic word is weak. No project/framework names involved.
    assert _literal_strength("0f9ed3") >= 4
    assert 2 <= _literal_strength("linear-gradient") < 4
    assert _literal_strength("button") < 2
    assert _literal_strength("primary") < 2


def test_content_certificate_unique_literal_route():
    # A single value-like literal unique to one body certifies (route A).
    anchors = _cert_anchors({"0f9ed3", "linear-gradient"}, {"0f9ed3": 1, "linear-gradient": 2})
    assert _content_certificate_label({"0f9ed3"}, anchors) == ("content_unique_anchor", "0f9ed3")


def test_content_certificate_rare_cluster_route():
    # Two rare identifier-shaped anchors co-occurring certify (route B), even
    # though neither is a value literal (this is the styling-construct case).
    anchors = _cert_anchors(
        {"linear-gradient", "background-image"},
        {"linear-gradient": 2, "background-image": 2},
    )
    result = _content_certificate_label({"linear-gradient", "background-image"}, anchors)
    assert result is not None and result[0] == "content_anchor_cluster"


def test_content_certificate_rejects_single_rare_nonliteral():
    # A LONE rare non-value token must NOT certify (a stray rare word must not
    # admit a file) — route A needs literal strength, route B needs a cluster.
    anchors = _cert_anchors({"linear-gradient"}, {"linear-gradient": 2})
    assert _content_certificate_label({"linear-gradient"}, anchors) is None


def test_content_certificate_rejects_high_frequency_anchor():
    # A high-content-frequency token (appears in many bodies) never certifies —
    # this is the guard against the historical broad over-match.
    anchors = _cert_anchors({"0f9ed3"}, {"0f9ed3": 9})
    assert _content_certificate_label({"0f9ed3"}, anchors) is None


def test_content_certificate_requires_specific_nontarget_membership():
    # A token that is not a recognized specific non-target anchor cannot certify
    # even if rare (it is not a change signal, just noise).
    anchors = _cert_anchors(set(), {"0f9ed3": 1})
    assert _content_certificate_label({"0f9ed3"}, anchors) is None


def test_score_and_accept_content_only_file_via_certificate():
    # A content-only file (no path hit, no cross-category pair) is accepted via
    # the certificate route WITHOUT the independent-2-sources rule.
    cand = ImplCandidate(path="src/app/globals.css")
    cand.evidences.append(
        ImpactEvidence(source="content_token", detail="0f9ed3", weight=0.12, category="anchor")
    )
    cand.evidences.append(
        ImpactEvidence(
            source="content_unique_anchor", detail="0f9ed3", weight=0.56, category="anchor"
        )
    )
    _score_and_accept(
        cand,
        min_score=0.55,
        min_independent_sources=2,
        anchor_policy=AnchorPolicy(
            field_terms_present=True, specific_terms=frozenset({"0f9ed3"})
        ),
    )
    assert cand.accepted is True

    # A content-only file with only a lone content token (no certificate) is
    # still rejected — the certificate route does NOT loosen the ordinary rule.
    bare = ImplCandidate(path="src/app/other.css")
    bare.evidences.append(
        ImpactEvidence(source="content_token", detail="primary", weight=0.12, category="anchor")
    )
    _score_and_accept(
        bare,
        min_score=0.55,
        min_independent_sources=2,
        anchor_policy=AnchorPolicy(field_terms_present=True, specific_terms=frozenset()),
    )
    assert bare.accepted is False


def test_stale_expects_dropped_anchor_discovered_target_selected(tmp_path):
    """A stale/imprecise ``expects`` edge (target with no concrete anchor of its
    own) is NOT selected as a write target; the real anchor-discovered file (a
    rare hex literal in its body) IS — and the plan still completes. The dropped
    target is surfaced as a diagnostic (anti-false-green visibility)."""
    layout = "src/app/(dashboard)/layout.tsx"
    css = "src/app/globals.css"
    # The design expects ONLY the layout file — the imprecise prior.
    dag = _styling_dag_with_expects(tmp_path, targets=[layout])
    # The expected target carries NO change anchor (just unrelated structure).
    _make_file(
        tmp_path, layout, "export default function Layout() { return null; }\n"
    )
    # The real target: a content-only stylesheet bearing the unique hex literal.
    _make_file(
        tmp_path,
        css,
        ":root { --primary: 221 83% 53%; "
        "background-image: linear-gradient(#0f9ed3, #0b71b9); }\n",
    )

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["gradient"],
        entities=[],
        fields=[],
        operations=["update", "display"],
        surfaces=["theme", "ui"],
        obligations=[
            {"id": "theme.update", "description": "update theme", "terms": ["theme", "update"]},
            {"id": "ui.display", "description": "display ui", "terms": ["ui", "display"]},
        ],
    )
    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="change the primary button to a gradient #0f9ed3 to #0b71b9",
        analysis=analysis,
        design_updates=[
            DesignUpdate(
                target_path=Path(_DESIGN),
                original_content="# UX\n",
                proposed_content="# UX\ngradient #0f9ed3 #0b71b9\n",
                diff="+gradient #0f9ed3 #0b71b9\n+primary-gradient",
                changed=True,
            )
        ],
    )
    assert plan.status == "complete", (plan.status, plan.diagnostics)
    assert css in plan.impl_paths, plan.impl_paths
    # The stale expects target must NOT be a write target.
    assert layout not in plan.impl_paths, plan.impl_paths
    assert any("ignored expected target" in d for d in plan.diagnostics), plan.diagnostics


def test_pure_abstract_facets_without_certificate_refuse(tmp_path):
    """A purely-abstract-facet change (no concrete write, no concrete surface-
    reach) whose candidate is matched only by a GENERIC discriminator — no
    rare/literal content certificate — must REFUSE (ambiguous), not fake green.
    This is the anti-false-green guard for styling/theme changes that the LLM
    decomposed with common words (so unrelated files could otherwise 'cover' the
    abstract facets)."""
    f = "src/components/panel.tsx"
    dag = _styling_dag_with_expects(tmp_path, targets=[])  # design node, no expects
    _make_file(
        tmp_path,
        f,
        "export function Panel() {\n"
        "  return <div className=\"panel label\">x</div>;\n}\n",
    )
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["panel"],
        entities=["panel"],
        fields=["label"],
        operations=["update", "display"],
        surfaces=["ui"],
        obligations=[
            {"id": "ui.update", "description": "", "terms": ["ui", "update"]},
            {"id": "ui.display", "description": "", "terms": ["ui", "display"]},
        ],
    )
    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=[_DESIGN],
        phenomenon_text="update the panel label",
        analysis=analysis,
        design_updates=[],
    )
    assert plan.status == "ambiguous", (plan.status, plan.diagnostics)
    assert any("abstract facet" in d for d in plan.diagnostics), plan.diagnostics

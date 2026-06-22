"""Unit tests for codd.fix.impact_planner (Stage-4 impact resolution).

Implements the in-scope tests from the GPT-5.5 root-cause design (section 6.1):

* test 2 (CORE GOLDEN) — recover EXACTLY the 4 video-lesson-body impl files
  from a coarse "courses" module + exclude the unrelated file; status complete.
* test 3 — no over-propagation of unrelated course files.
* test 4 — a missing surface leaves an obligation unresolved; status is not
  ``complete`` and the plan is NOT applied.

Everything is validated purely with tmp fixtures + the planner API: no network,
no AI, no live commands. Concrete framework/domain names appear only in the
*fixtures* (allowed); the planner core stays name-free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from codd.dag import DAG, Node
from codd.fix.impact_planner import resolve_impact_plan
from codd.fix.phenomenon_fixer import run_phenomenon_fix
from codd.fix.phenomenon_parser import PhenomenonAnalysis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_file(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _dag_with_coarse_module(tmp_path: Path) -> DAG:
    """A design doc linked only at a coarse 'courses' module granularity.

    Mirrors the brownfield failure: the design node declares NO ``expects``
    edges to the impl files; its frontmatter module ('courses') does not even
    name the lesson/video surfaces the feature actually spans.
    """
    dag = DAG()
    rel = "docs/design/courses.md"
    _make_file(
        tmp_path,
        rel,
        "---\ncodd:\n  id: courses\nmodules: [courses]\n---\n# Courses\n",
    )
    dag.add_node(
        Node(
            id=rel,
            kind="design_doc",
            path=rel,
            attributes={"frontmatter": {"modules": ["courses"]}},
        )
    )
    return dag


def _video_lesson_analysis() -> PhenomenonAnalysis:
    return PhenomenonAnalysis(
        intent="new_feature",
        subject_terms=["動画", "レッスン", "本文"],
        entities=["lesson", "video"],
        fields=["content_body", "contentBody"],
        operations=["create", "update", "display", "input"],
        surfaces=["api", "learner", "admin"],
    )


_FOUR_REQUIRED = {
    "src/app/api/v1/lessons/route.ts",
    "src/app/api/v1/lessons/[lessonId]/route.ts",
    "src/app/(dashboard)/learner/courses/[id]/lessons/[lessonId]/page.tsx",
    "src/components/features/admin-course-workbench.tsx",
}


def _make_video_lesson_files(root: Path) -> None:
    _make_file(
        root,
        "src/app/api/v1/lessons/route.ts",
        "export async function POST() { /* video lesson contentBody create */ }",
    )
    _make_file(
        root,
        "src/app/api/v1/lessons/[lessonId]/route.ts",
        "export async function PATCH() { /* video lesson contentBody update */ }",
    )
    _make_file(
        root,
        "src/app/(dashboard)/learner/courses/[id]/lessons/[lessonId]/page.tsx",
        "// learner page: render video then content_body below the video",
    )
    _make_file(
        root,
        "src/components/features/admin-course-workbench.tsx",
        "// admin form: edit lesson video contentBody input field",
    )


# ---------------------------------------------------------------------------
# test 2 — CORE GOLDEN
# ---------------------------------------------------------------------------


def test_impact_plan_recovers_video_lesson_body_targets_from_coarse_module(tmp_path):
    dag = _dag_with_coarse_module(tmp_path)
    _make_video_lesson_files(tmp_path)
    # An unrelated file under the same coarse "course" concept must NOT be hit.
    _make_file(
        tmp_path,
        "src/components/unrelated-sidebar.tsx",
        "// course navigation sidebar",
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=["docs/design/courses.md"],
        phenomenon_text="動画レッスンで、動画の下に補足テキスト(本文)を表示したい",
        analysis=_video_lesson_analysis(),
    )

    assert set(plan.impl_paths) == _FOUR_REQUIRED, (
        f"expected exactly the 4 video-lesson-body files, got {plan.impl_paths!r}; "
        f"diagnostics={plan.diagnostics!r}"
    )
    assert plan.status == "complete", (
        f"status={plan.status!r}, unresolved={plan.unresolved_obligations!r}, "
        f"diagnostics={plan.diagnostics!r}"
    )
    assert "src/components/unrelated-sidebar.tsx" not in plan.impl_paths
    # Every derived obligation is covered by at least one accepted file.
    assert plan.unresolved_obligations == []
    assert plan.covered_obligations


# ---------------------------------------------------------------------------
# test 3 — no over-propagation
# ---------------------------------------------------------------------------


def test_impact_plan_does_not_include_unrelated_course_files(tmp_path):
    dag = _dag_with_coarse_module(tmp_path)
    _make_video_lesson_files(tmp_path)
    # Sibling "course" files that share the coarse module but NOT the feature.
    _make_file(
        tmp_path,
        "src/app/api/v1/courses/route.ts",
        "export async function GET() { /* course title description list */ }",
    )
    _make_file(tmp_path, "src/components/course-card.tsx", "// course card display")

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=["docs/design/courses.md"],
        phenomenon_text="動画レッスンに本文を表示",
        analysis=_video_lesson_analysis(),
    )

    assert "src/app/api/v1/courses/route.ts" not in plan.impl_paths
    assert "src/components/course-card.tsx" not in plan.impl_paths
    # The real feature files are still recovered (no recall regression).
    assert set(plan.impl_paths) == _FOUR_REQUIRED


# ---------------------------------------------------------------------------
# test 4 — obligation missing => not complete, not applied
# ---------------------------------------------------------------------------


def test_impact_plan_incomplete_when_a_surface_is_missing(tmp_path):
    dag = _dag_with_coarse_module(tmp_path)
    # Only 3 of 4 surfaces exist — the admin input surface is absent.
    _make_file(
        tmp_path,
        "src/app/api/v1/lessons/route.ts",
        "export async function POST() { /* video lesson contentBody create */ }",
    )
    _make_file(
        tmp_path,
        "src/app/api/v1/lessons/[lessonId]/route.ts",
        "export async function PATCH() { /* video lesson contentBody update */ }",
    )
    _make_file(
        tmp_path,
        "src/app/(dashboard)/learner/courses/[id]/lessons/[lessonId]/page.tsx",
        "// learner page: render video then content_body below the video",
    )

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=["docs/design/courses.md"],
        phenomenon_text="動画レッスンに本文",
        analysis=_video_lesson_analysis(),
    )

    # Must NOT be a (false) green: the admin surface obligation is unresolved.
    assert plan.status in {"incomplete", "ambiguous"}
    assert plan.status != "complete"
    # The deterministic baseline derives a per-surface obligation; the admin
    # surface one is the missing facet.
    assert "surface.admin" in plan.unresolved_obligations, (
        f"unresolved={plan.unresolved_obligations!r}, "
        f"obligations={[o.id for o in plan.obligations]!r}"
    )
    # The admin impl file is genuinely absent from the plan.
    assert "src/components/features/admin-course-workbench.tsx" not in plan.impl_paths


def test_impact_plan_complete_status_only_when_all_covered(tmp_path):
    """Sanity bracket around test 4: adding the missing surface flips to
    complete (so the incompleteness in test 4 is the missing file, not a bug)."""
    dag = _dag_with_coarse_module(tmp_path)
    _make_video_lesson_files(tmp_path)

    plan = resolve_impact_plan(
        dag=dag,
        project_root=tmp_path,
        design_node_ids=["docs/design/courses.md"],
        phenomenon_text="動画レッスンに本文",
        analysis=_video_lesson_analysis(),
    )
    assert plan.status == "complete"
    assert plan.unresolved_obligations == []


# ---------------------------------------------------------------------------
# Stage-4 wiring (phenomenon_fixer) — anti-false-green fail-fast
# ---------------------------------------------------------------------------


def _scripted_ai(responses: list[str]) -> Callable[[str], str]:
    it = iter(responses)

    def invoke(_prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            return "{}"

    return invoke


def _design_doc(body_extra: str = "") -> str:
    return (
        "---\n"
        "title: Courses\n"
        "description: course and lesson content\n"
        "user_journeys:\n"
        "  - id: u1\n"
        "    description: author edits a lesson\n"
        "acceptance_criteria:\n"
        "  - id: c1\n"
        "    description: lesson body persists\n"
        "codd:\n"
        "  node_id: courses\n"
        "  band: green\n"
        "---\n"
        "# Courses\n\n"
        f"course design body. {body_extra}\n"
    )


def _write_brownfield_project(tmp_path: Path, *, include_admin: bool) -> Path:
    codd_dir = tmp_path / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        "scan:\n  source_dirs: []\n"
        "dag:\n"
        "  design_doc_patterns:\n"
        "    - 'design/**/*.md'\n"
        "  impl_file_patterns:\n"
        "    - 'src/**/*.ts'\n"
        "    - 'src/**/*.tsx'\n"
        "  test_file_patterns: []\n"
        "  scan_exclude_patterns:\n"
        "    - '.codd/**'\n"
        "    - 'tests/**'\n",
        encoding="utf-8",
    )
    _make_file(tmp_path, "design/courses.md", _design_doc())
    _make_file(
        tmp_path,
        "src/app/api/v1/lessons/route.ts",
        "export async function POST() { /* video lesson contentBody create */ }",
    )
    _make_file(
        tmp_path,
        "src/app/api/v1/lessons/[lessonId]/route.ts",
        "export async function PATCH() { /* video lesson contentBody update */ }",
    )
    _make_file(
        tmp_path,
        "src/app/(dashboard)/learner/courses/[id]/lessons/[lessonId]/page.tsx",
        "// learner page: render video then content_body below the video",
    )
    if include_admin:
        _make_file(
            tmp_path,
            "src/components/features/admin-course-workbench.tsx",
            "// admin form: edit lesson video contentBody input field",
        )
    return tmp_path


def _decomposed_parser_response() -> str:
    return json.dumps(
        {
            "intent": "new_feature",
            "subject_terms": ["lesson", "video", "body"],
            "lexicon_hits": [],
            "ambiguity_score": 0.05,
            "acceptance_signal": "body shows under the video",
            "entities": ["lesson", "video"],
            "fields": ["content_body", "contentBody"],
            "operations": ["create", "update", "display", "input"],
            "surfaces": ["api", "learner", "admin"],
        }
    )


def test_stage4_dry_run_aborts_when_a_surface_is_missing(tmp_path):
    """Dry-run with a multi-surface analysis but a missing surface file must be
    flagged incomplete and aborted — not previewed as if it were complete."""
    project = _write_brownfield_project(tmp_path, include_admin=False)
    updated = _design_doc("Added: lessons may carry a body shown under the video.")
    ai = _scripted_ai(
        [
            _decomposed_parser_response(),  # parser
            "{}",                            # candidate_selector tier2
            updated,                         # design_updater
            json.dumps({"risky": False, "categories": [], "summary": ""}),  # risk
        ]
    )

    result = run_phenomenon_fix(
        project,
        "lessonに本文(body)をvideoの下に表示したい",
        ai_invoke=ai,
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=True,
    )

    assert result.aborted
    assert "incomplete" in result.abort_reason
    assert "surface.admin" in result.unresolved_obligations
    # The design doc was never written (dry run) and no impl files were applied.
    assert not result.applied_paths


def test_stage4_real_run_aborts_partial_without_applying(tmp_path):
    """The real run must FAIL-FAST on an incomplete impact plan and never reach
    the LLM patch slot — proving no partial (false-green) apply."""
    project = _write_brownfield_project(tmp_path, include_admin=False)
    updated = _design_doc("Added: lessons may carry a body shown under the video.")

    patch_calls = {"count": 0}

    base = _scripted_ai(
        [
            _decomposed_parser_response(),  # parser
            "{}",                            # candidate_selector tier2
            updated,                         # design_updater
            json.dumps({"risky": False, "categories": [], "summary": ""}),  # risk
        ]
    )

    def ai(prompt: str) -> str:
        # Any call after the scripted 4 would be the impl-patch slot.
        if "FIX:" in prompt or "fenced" in prompt.lower():
            patch_calls["count"] += 1
        return base(prompt)

    result = run_phenomenon_fix(
        project,
        "lessonに本文(body)をvideoの下に表示したい",
        ai_invoke=ai,
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=False,
    )

    assert result.aborted, result.abort_reason
    assert "surface.admin" in result.unresolved_obligations
    # Stage 4 aborted before the propagation patch loop ran.
    assert result.propagation is None
    assert patch_calls["count"] == 0


def test_stage4_complete_plan_drives_propagation_with_all_surfaces(tmp_path):
    """When all surfaces are present the plan is complete: the design doc
    applies, Stage 4 runs propagation, and the write allowlist is the planner's
    full impl set (NOT the legacy 0–1 forward-edge result)."""
    project = _write_brownfield_project(tmp_path, include_admin=True)
    updated = _design_doc("Added: lessons may carry a body shown under the video.")
    ai = _scripted_ai(
        [
            _decomposed_parser_response(),  # parser
            "{}",                            # candidate_selector tier2
            updated,                         # design_updater
            json.dumps({"risky": False, "categories": [], "summary": ""}),  # risk
            # impl-patch slot: no fenced blocks => nothing applied, gate skips.
            "no changes needed",
        ]
    )

    result = run_phenomenon_fix(
        project,
        "lessonに本文(body)をvideoの下に表示したい",
        ai_invoke=ai,
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=False,
    )

    assert not result.aborted, result.abort_reason
    assert result.impact_plan is not None
    assert result.impact_plan.status == "complete"
    assert result.unresolved_obligations == []
    assert "design/courses.md" in result.applied_paths
    # Stage 4 ran with the planner's full 4-surface impl set as the allowlist.
    assert result.propagation is not None
    assert set(result.affected_impl_paths) == _FOUR_REQUIRED

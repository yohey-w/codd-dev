from __future__ import annotations

from pathlib import Path
import re

import yaml

import codd.dag.checks.implementation_coverage as implementation_module
from codd.config import load_project_config
from codd.dag import DAG, Node
from codd.dag.builder import load_dag_settings
from codd.dag.checks.implementation_coverage import (
    ImplementationCoverageCheck,
    _match_with_src_prefix_tolerance,
    _matches_any_impl,
    _normalize_bracket_segments,
    _path_prefix_tolerant,
)
from codd.llm.design_doc_extractor import ExpectedExtraction, ExpectedNode


def _dag_with_impls(*paths: str) -> DAG:
    dag = DAG()
    for path in paths:
        dag.add_node(Node(id=path, kind="impl_file", path=path))
    return dag


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def test_exact_match_keeps_existing_behavior():
    dag = _dag_with_impls("src/auth/login.ts")

    assert _matches_any_impl(dag, "src/auth/login.ts") is True


def test_glob_match_keeps_existing_behavior():
    dag = _dag_with_impls("src/auth/login.ts")

    assert _matches_any_impl(dag, "src/*/login.ts") is True


def test_soft_path_match_keeps_nested_module_hint_behavior():
    dag = _dag_with_impls("src/lib/auth/providers/credentials.ts")

    assert _matches_any_impl(dag, "providers/credentials.ts") is True


def test_normalize_bracket_segments_single_segment():
    assert _normalize_bracket_segments("app/items/[id]/handler.ts") == "app/items/*/handler.ts"


def test_normalize_bracket_segments_multiple_segments():
    assert _normalize_bracket_segments("app/[tenant]/items/[id]/handler.ts") == "app/*/items/*/handler.ts"


def test_normalize_bracket_segments_without_brackets_is_noop():
    assert _normalize_bracket_segments("app/items/static/handler.ts") == "app/items/static/handler.ts"


def test_bracket_normalization_matches_different_segment_names():
    dag = _dag_with_impls("app/items/[itemId]/handler.ts")

    assert _matches_any_impl(dag, "app/items/[id]/handler.ts") is True


def test_bracket_normalization_matches_multiple_segments():
    dag = _dag_with_impls("app/[tenant]/items/[itemId]/handler.ts")

    assert _matches_any_impl(dag, "app/[org]/items/[id]/handler.ts") is True


def test_bracket_normalization_allows_glob_hint():
    dag = _dag_with_impls("app/items/[itemId]/handler.ts")

    assert _matches_any_impl(dag, "app/items/*/handler.ts") is True


def test_default_prefix_tolerance_list_is_available():
    assert _path_prefix_tolerant({}) == ["src/", "lib/", "app/"]


def test_prefix_tolerance_matches_when_hint_has_prefix():
    assert _match_with_src_prefix_tolerance("src/services/billing.ts", "services/billing.ts", {}) is True


def test_prefix_tolerance_matches_when_impl_has_prefix():
    dag = _dag_with_impls("src/services/billing.ts")

    assert _matches_any_impl(
        dag,
        "services/billing.ts",
        settings={"coherence": {"path_prefix_tolerant": ["src/"]}},
    ) is True


def test_prefix_tolerance_matches_custom_project_prefix():
    dag = _dag_with_impls("packages/app/services/billing.ts")

    assert _matches_any_impl(
        dag,
        "services/billing.ts",
        settings={"coherence": {"path_prefix_tolerant": ["packages/app"]}},
    ) is True


def test_prefix_tolerance_empty_override_disables_prefix_match():
    assert (
        _match_with_src_prefix_tolerance(
            "services/billing.ts",
            "src/services/billing.ts",
            {"coherence": {"path_prefix_tolerant": []}},
        )
        is False
    )


def test_codd_yaml_prefix_tolerance_project_override_wins(tmp_path):
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump({"coherence": {"path_prefix_tolerant": ["custom/"]}}, sort_keys=False),
    )

    config = load_project_config(tmp_path)
    settings = load_dag_settings(tmp_path)

    assert config["coherence"]["path_prefix_tolerant"] == ["custom/"]
    assert settings["coherence"]["path_prefix_tolerant"] == ["custom/"]


def test_codd_yaml_prefix_tolerance_can_be_empty(tmp_path):
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump({"coherence": {"path_prefix_tolerant": []}}, sort_keys=False),
    )

    assert load_project_config(tmp_path)["coherence"]["path_prefix_tolerant"] == []


def test_exact_match_short_circuits_later_stages(monkeypatch):
    dag = _dag_with_impls("src/auth/login.ts")

    def fail_stage(*args, **kwargs):
        raise AssertionError("later stage should not run")

    monkeypatch.setattr(implementation_module, "_glob_path_match", fail_stage)
    monkeypatch.setattr(implementation_module, "_bracket_path_match", fail_stage)
    monkeypatch.setattr(implementation_module, "_match_with_src_prefix_tolerance", fail_stage)

    assert implementation_module._matches_any_impl(dag, "src/auth/login.ts") is True


def test_example_lms_bracket_and_prefix_case_passes_c8(tmp_path):
    dag = _dag_with_impls("src/app/api/v1/courses/[id]/route.ts")
    dag.add_node(
        Node(
            id="docs/design/api.md",
            kind="design_doc",
            path="docs/design/api.md",
            attributes={
                "expected_extraction": ExpectedExtraction(
                    expected_nodes=[
                        ExpectedNode(
                            kind="impl_file",
                            path_hint="app/api/v1/courses/[courseId]/route.ts",
                            rationale="course detail endpoint",
                            source_design_section="S02",
                        )
                    ],
                    expected_edges=[],
                    source_design_doc="docs/design/api.md",
                )
            },
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {"coherence": {"path_prefix_tolerant": ["src/"]}})

    assert result.passed is True
    assert result.violations == []


def test_core_source_has_no_framework_specific_keywords():
    source = Path("codd/dag/checks/implementation_coverage.py").read_text(encoding="utf-8").lower()

    for forbidden in ("page.tsx", "route.ts", "layout.tsx", "react", "django", "rails"):
        assert forbidden not in source
    assert re.search(r"next\.?js", source) is None


# --- common node matching (common_node_patterns reclassification) ------------


def _expected_doc(kind: str, path_hint: str) -> Node:
    return Node(
        id="docs/design/spec.md",
        kind="design_doc",
        path="docs/design/spec.md",
        attributes={
            "expected_extraction": ExpectedExtraction(
                expected_nodes=[
                    ExpectedNode(
                        kind=kind,
                        path_hint=path_hint,
                        rationale="expected artifact",
                        source_design_section="S01",
                    )
                ],
                expected_edges=[],
                source_design_doc="docs/design/spec.md",
            )
        },
    )


def test_common_code_node_satisfies_expected_impl(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/lib/shared/util.ts", kind="common", path="src/lib/shared/util.ts"))
    dag.add_node(_expected_doc("impl_file", "src/lib/shared/util.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_common_markdown_node_does_not_satisfy_expected_impl(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/shared/notes.md", kind="common", path="docs/shared/notes.md"))
    dag.add_node(_expected_doc("impl_file", "docs/shared/notes.md"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"


def test_common_code_node_satisfies_expected_test_file(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="tests/shared/check_util.py", kind="common", path="tests/shared/check_util.py"))
    dag.add_node(_expected_doc("test_file", "tests/shared/check_util.py"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_matches_any_impl_accepts_common_code_node():
    dag = DAG()
    dag.add_node(Node(id="src/lib/shared/util.ts", kind="common", path="src/lib/shared/util.ts"))

    assert _matches_any_impl(dag, "src/lib/shared/util.ts") is True


# --- file-system fallback matching -------------------------------------------


def test_literal_hint_matches_existing_file_on_disk(tmp_path):
    _write(tmp_path / "src" / "extra" / "helper.ts", "export {}\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/extra/helper.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_literal_hint_missing_from_disk_and_dag_stays_red(tmp_path):
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/extra/missing.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"


def test_bracketed_literal_path_matches_existing_file_on_disk(tmp_path):
    _write(tmp_path / "src" / "items" / "[...itemPath]" / "handler.ts", "export {}\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/items/[...itemPath]/handler.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_glob_hint_with_bracketed_segment_matches_on_disk(tmp_path):
    _write(tmp_path / "src" / "items" / "[...itemPath]" / "handler.ts", "export {}\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/items/[...itemPath]/*.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_fs_fallback_applies_to_expected_test_file_without_candidates(tmp_path):
    _write(tmp_path / "tests" / "extra" / "check_helper.py", "def test_x():\n    pass\n")
    dag = DAG()
    dag.add_node(_expected_doc("test_file", "tests/extra/check_helper.py"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_glob_fs_match_keeps_historical_additional_suppression(tmp_path):
    """Behavior invariance: a glob hint matching any project file has always
    suppressed the additional_implementation pass. The literal-hint FS
    matching added for missing_implementation must not change that."""

    _write(tmp_path / "src" / "extra" / "helper.ts", "export {}\n")
    dag = _dag_with_impls("src/unrelated/orphan.ts")
    dag.add_node(_expected_doc("impl_file", "src/extra/*.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True  # expected artifact found on disk
    assert result.violations == []  # historical suppression preserved


def test_literal_hints_do_not_suppress_additional_implementation(tmp_path):
    """Behavior invariance: literal hints never reached the FS fallback in the
    additional_implementation pass, so unclaimed nodes stay reported."""

    _write(tmp_path / "src" / "extra" / "helper.ts", "export {}\n")
    dag = _dag_with_impls("src/extra/helper.ts", "src/unrelated/orphan.ts")
    dag.add_node(_expected_doc("impl_file", "src/extra/helper.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    additional = [item for item in result.violations if item["type"] == "additional_implementation"]
    assert [item["impl_file"] for item in additional] == ["src/unrelated/orphan.ts"]


# --- coverage summaries + cross-artifact diagnostics (amber only) -------------


def _multi_kind_doc(*nodes: tuple[str, str], doc_id: str = "docs/design/spec.md") -> Node:
    """Build a design_doc whose expected_extraction declares several nodes.

    Each ``nodes`` entry is ``(kind, path_hint)``. Mirrors ``_expected_doc``
    but supports the multi-artifact shape the coverage summary describes.
    """

    return Node(
        id=doc_id,
        kind="design_doc",
        path=doc_id,
        attributes={
            "expected_extraction": ExpectedExtraction(
                expected_nodes=[
                    ExpectedNode(
                        kind=kind,
                        path_hint=path_hint,
                        rationale="expected artifact",
                        source_design_section="S01",
                    )
                    for kind, path_hint in nodes
                ],
                expected_edges=[],
                source_design_doc=doc_id,
            )
        },
    )


def _summary_for(result, design_doc: str):
    for summary in result.coverage_summaries:
        if summary["design_doc"] == design_doc:
            return summary
    return None


def test_coverage_summary_full_impl_and_test_present_passes(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(Node(id="tests/auth/login.test.ts", kind="test_file", path="tests/auth/login.test.ts"))
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("test_file", "tests/auth/login.test.ts"),
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []
    summary = _summary_for(result, "docs/design/spec.md")
    assert summary is not None
    assert summary["expected_total"] == 2
    assert summary["matched_total"] == 2
    assert summary["by_kind"]["impl_file"] == {"expected": 1, "matched": 1}
    assert summary["by_kind"]["test_file"] == {"expected": 1, "matched": 1}


def test_no_design_doc_expected_extraction_pass_is_vacuous(tmp_path):
    # No design doc declares an expected_extraction, so the coverage loop never
    # runs and the check passes having verified nothing. checked_count==0 lets the
    # materiality overlay flag the vacuous pass instead of a verified clean run.
    from codd.dag.materiality import is_vacuous_pass

    result = ImplementationCoverageCheck().run(DAG(), tmp_path, {})

    assert result.passed is True
    assert result.checked_count == 0
    assert is_vacuous_pass(result) is True


def test_full_coverage_pass_is_not_vacuous(tmp_path):
    # A real verification evaluates each expected artifact, so checked_count is
    # non-zero and the pass is materially distinct from the empty (vacuous) case.
    from codd.dag.materiality import is_vacuous_pass

    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(Node(id="tests/auth/login.test.ts", kind="test_file", path="tests/auth/login.test.ts"))
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("test_file", "tests/auth/login.test.ts"),
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.checked_count == 2
    assert is_vacuous_pass(result) is False


def test_missing_test_keeps_existing_red_and_emits_summary(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("test_file", "tests/auth/login.test.ts"),
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    # Existing red behavior is unchanged: the missing test is still red.
    assert result.passed is False
    missing = [item for item in result.violations if item["type"] == "missing_implementation"]
    assert [item["expected_kind"] for item in missing] == ["test_file"]
    # Summary is still produced alongside the red.
    summary = _summary_for(result, "docs/design/spec.md")
    assert summary is not None
    assert summary["expected_total"] == 2
    assert summary["matched_total"] == 1
    assert summary["missing_total"] == 1
    assert summary["by_kind"]["impl_file"] == {"expected": 1, "matched": 1}
    assert summary["by_kind"]["test_file"] == {"expected": 1, "matched": 0}


def test_partial_coverage_defers_to_red_no_duplicate_amber(tmp_path):
    """Dedup invariant: a partially-covered multi-artifact doc whose gap is a
    missing required artifact must NOT also raise cross_artifact_partial_coverage
    — the missing piece is already owned by the existing red. (Under the current
    missing==red coupling, every count-based partial coincides with a red, so
    the cross-artifact amber defers entirely to the red and stays silent.)"""

    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("test_file", "tests/auth/login.test.ts"),  # missing -> red
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    partial = [item for item in result.violations if item["type"] == "cross_artifact_partial_coverage"]
    assert partial == []  # deferred to the existing missing_implementation red
    # And the summary still records the partial shape for observability.
    summary = _summary_for(result, "docs/design/spec.md")
    assert summary["expected_total"] == 2 and summary["matched_total"] == 1


def test_impl_without_declared_test_kind_emits_shape_incomplete_amber(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(Node(id="src/auth/config.ts", kind="config_file", path="src/auth/config.ts"))
    # A multi-artifact shape (impl + config) but NO test_file kind declared.
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("config_file", "src/auth/config.ts"),
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    # No red: both declared artifacts exist.
    assert result.passed is True
    shape = [item for item in result.violations if item["type"] == "coverage_shape_incomplete"]
    assert len(shape) == 1
    assert shape[0]["severity"] == "amber"
    assert shape[0]["design_doc"] == "docs/design/spec.md"


def test_single_impl_without_test_does_not_emit_amber(tmp_path):
    """Prime-directive guard: a lone impl artifact with no declared test must
    NOT raise any amber (would flood existing projects)."""

    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(_multi_kind_doc(("impl_file", "src/auth/login.ts")))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_shape_incomplete_not_emitted_when_test_kind_declared(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="src/auth/login.ts", kind="impl_file", path="src/auth/login.ts"))
    dag.add_node(Node(id="tests/auth/login.test.ts", kind="test_file", path="tests/auth/login.test.ts"))
    dag.add_node(
        _multi_kind_doc(
            ("impl_file", "src/auth/login.ts"),
            ("test_file", "tests/auth/login.test.ts"),
        )
    )

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    assert [item for item in result.violations if item["type"] == "coverage_shape_incomplete"] == []


def test_glob_quirk_unaffected_by_summary_additions(tmp_path):
    """Historical glob-suppression of additional_implementation stays intact
    even though coverage summaries are now produced for the same doc."""

    _write(tmp_path / "src" / "extra" / "helper.ts", "export {}\n")
    dag = _dag_with_impls("src/unrelated/orphan.ts")
    dag.add_node(_expected_doc("impl_file", "src/extra/*.ts"))

    result = ImplementationCoverageCheck().run(dag, tmp_path, {})

    # Glob hint matched a file on disk -> additional_implementation suppressed.
    assert [item for item in result.violations if item["type"] == "additional_implementation"] == []
    # Summary still generated for the design doc.
    summary = _summary_for(result, "docs/design/spec.md")
    assert summary is not None
    assert summary["expected_total"] == 1


# --- file-system fallback root-jail (path traversal hardening) ----------------


def test_parent_traversal_hint_does_not_match_file_outside_root(tmp_path):
    """Security: a parent-traversal path_hint must NOT be satisfied by a real
    file living outside the project root. Before the root-jail, the literal FS
    fallback resolved ``project_root / '../outside.py'`` and reported a match
    (false-green); the expected artifact must instead stay missing."""

    root = tmp_path / "project"
    root.mkdir()
    # Real file one level above the project root, reachable only via ``..``.
    _write(tmp_path / "outside.py", "print('out')\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "../outside.py"))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"
    assert result.violations[0]["path_hint"] == "../outside.py"


def test_deep_parent_traversal_hint_does_not_match_outside_root(tmp_path):
    """Security: multi-level ``../../`` traversal is likewise jailed out."""

    outer = tmp_path / "outer"
    root = outer / "inner" / "project"
    root.mkdir(parents=True)
    _write(outer / "secret.py", "print('secret')\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "../../secret.py"))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"


def test_in_root_literal_hint_still_matches_after_jail(tmp_path):
    """Regression: a normal in-root literal path_hint that exists on disk must
    keep matching exactly as before the root-jail was added."""

    root = tmp_path / "project"
    _write(root / "src" / "extra" / "helper.ts", "export {}\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/extra/helper.ts"))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is True
    assert result.violations == []


def test_fs_fallback_match_unit_rejects_parent_traversal(tmp_path):
    """Unit-level guard on the helper itself: an out-of-root traversal hint is
    rejected even though the target file exists, while the in-root sibling is
    still accepted."""

    root = tmp_path / "project"
    root.mkdir()
    _write(tmp_path / "outside.py", "print('out')\n")
    _write(root / "inside.py", "print('in')\n")

    assert implementation_module._fs_fallback_match("../outside.py", root) is False
    assert implementation_module._fs_fallback_match("inside.py", root) is True


# --- absolute path_hint handling (root-jail before normalization) -------------


def test_absolute_looking_hint_does_not_false_green_on_relative_file(tmp_path):
    """false-green guard: ``/src/service.py`` is an FS-absolute hint. It must NOT
    be satisfied by the project-relative file ``src/service.py``. Before the fix,
    ``_normalize_hint`` stripped the leading slash *before* the root-jail, so the
    absolute hint silently collapsed onto the in-root file (false-green)."""

    root = tmp_path / "project"
    _write(root / "src" / "service.py", "print('svc')\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "/src/service.py"))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_implementation"
    assert result.violations[0]["path_hint"] == "/src/service.py"


def test_true_in_root_absolute_hint_matches_existing_file(tmp_path):
    """false-red guard: a genuine in-root absolute hint
    (``<project_root>/src/service.py``) must match the file it points at. Before
    the fix, ``_normalize_hint`` stripped the leading slash, turning the absolute
    path into ``<project_root>/src/service.py`` *appended* under the root, which
    escaped the jail and failed (false-red)."""

    root = tmp_path / "project"
    _write(root / "src" / "service.py", "print('svc')\n")
    absolute_hint = (root / "src" / "service.py").as_posix()
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", absolute_hint))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is True
    assert result.violations == []


def test_in_root_relative_hint_still_matches_after_absolute_fix(tmp_path):
    """Regression: a plain in-root relative hint keeps matching after the
    absolute-path handling is added."""

    root = tmp_path / "project"
    _write(root / "src" / "service.py", "print('svc')\n")
    dag = DAG()
    dag.add_node(_expected_doc("impl_file", "src/service.py"))

    result = ImplementationCoverageCheck().run(dag, root, {})

    assert result.passed is True
    assert result.violations == []


def test_fs_fallback_match_unit_handles_absolute_hints(tmp_path):
    """Unit-level guard: the helper rejects an FS-absolute hint that resolves
    outside the root, accepts a true in-root absolute hint, and keeps the
    relative in-root and parent-traversal behavior intact."""

    root = tmp_path / "project"
    _write(root / "src" / "service.py", "print('svc')\n")
    absolute_in_root = (root / "src" / "service.py").as_posix()

    # FS-absolute hint pointing outside the root -> reject (no false-green).
    assert implementation_module._fs_fallback_match("/src/service.py", root) is False
    # Genuine in-root absolute hint -> accept (no false-red).
    assert implementation_module._fs_fallback_match(absolute_in_root, root) is True
    # Relative in-root hint -> accept (regression).
    assert implementation_module._fs_fallback_match("src/service.py", root) is True
    # Parent traversal -> reject (regression).
    assert implementation_module._fs_fallback_match("../service.py", root) is False


def test_absolute_hint_outside_root_rejected_even_when_target_exists(tmp_path):
    """false-green guard: an absolute hint whose target genuinely exists on the
    real filesystem outside the project root must still be rejected."""

    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    _write(outside / "service.py", "print('out')\n")
    absolute_outside = (outside / "service.py").as_posix()

    assert implementation_module._fs_fallback_match(absolute_outside, root) is False

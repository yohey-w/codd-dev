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


def test_osato_lms_bracket_and_prefix_case_passes_c8(tmp_path):
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

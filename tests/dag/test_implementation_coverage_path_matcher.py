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

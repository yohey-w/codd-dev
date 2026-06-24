from __future__ import annotations

import json
import re
from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.dag import DAG, Node
from codd.dag.builder import build_dag, dag_to_dict
from codd.dag.checks.environment_coverage import EnvironmentCoverageCheck
from codd.dag.coverage_axes import CoverageAxis, CoverageVariant
from codd.dag.runner import run_checks


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc(frontmatter: dict, body: str = "# Spec\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _axis(criticality: str | None = "critical", *, source: str = "lexicon", owner: str = "project_lexicon.yaml"):
    return CoverageAxis(
        axis_type="runtime_shape",
        rationale="The requirement names shape coverage.",
        variants=[CoverageVariant(id="shape_a", label="Shape A", criticality=criticality)],
        source=source,  # type: ignore[arg-type]
        owner_section=owner,
    )


def _dag_with_axis(axis: CoverageAxis | None = None) -> DAG:
    dag = DAG()
    dag.coverage_axes = [axis or _axis()]
    return dag


def _add_test(dag: DAG, attributes: dict | None = None, *, path: str = "tests/check_shape.py") -> None:
    dag.add_node(Node(id=path, kind="test_file", path=path, attributes=attributes or {}))


def _add_journey(dag: DAG, name: str = "happy_path", doc_id: str = "docs/design/spec.md") -> None:
    dag.add_node(
        Node(
            id=doc_id,
            kind="design_doc",
            path=doc_id,
            attributes={
                "user_journeys": [
                    {
                        "name": name,
                        "criticality": "critical",
                        "steps": [],
                        "required_capabilities": [],
                        "expected_outcome_refs": [],
                    }
                ]
            },
        )
    )


def _run(dag: DAG, root: Path | None = None):
    return EnvironmentCoverageCheck().run(dag, root or Path.cwd(), {})


def test_no_declared_axes_skips_not_vacuous_pass(tmp_path: Path):
    # No coverage axes = no environment matrix to verify. The check must SKIP
    # (verified nothing on purpose), not emit a clean PASS over 0 axes that a
    # verify summary cannot distinguish from a real verification (false-green).
    dag = DAG()

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.severity == "info"
    assert result.block_deploy is True
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0


def test_declared_axis_reports_checked_count(tmp_path: Path):
    # A real run evaluates each axis variant — checked_count is non-zero so the
    # verdict is materially distinct from the vacuous (skip) case.
    result = _run(_dag_with_axis(), tmp_path)

    assert result.checked_count == 1


def test_missing_test_for_critical_variant_is_red(tmp_path: Path):
    result = _run(_dag_with_axis(), tmp_path)

    assert result.passed is False
    assert result.violations[0]["type"] == "missing_test_for_variant"
    assert result.violations[0]["severity"] == "red"


def test_missing_test_for_high_variant_is_red(tmp_path: Path):
    result = _run(_dag_with_axis(_axis("high")), tmp_path)

    assert result.violations[0]["severity"] == "red"


def test_missing_test_for_medium_variant_is_amber(tmp_path: Path):
    result = _run(_dag_with_axis(_axis("medium")), tmp_path)

    assert result.status == "warn"
    assert result.passed is True
    assert result.violations[0]["severity"] == "amber"


def test_missing_test_for_info_variant_is_amber(tmp_path: Path):
    result = _run(_dag_with_axis(_axis("info")), tmp_path)

    assert result.severity == "amber"
    assert result.violations[0]["criticality"] == "info"


def test_variant_without_criticality_reports_unclear_amber(tmp_path: Path):
    result = _run(_dag_with_axis(_axis(None)), tmp_path)

    unclear = [item for item in result.violations if item["type"] == "variant_criticality_unclear"]
    assert unclear[0]["severity"] == "amber"


def test_test_node_axis_attributes_satisfy_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_test(dag, {"coverage_axes": [{"axis_type": "runtime_shape", "variant_id": "shape_a"}]})

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_test_node_variant_key_satisfies_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_test(dag, {"coverage_axis": {"axis_type": "runtime_shape", "variant": "shape_a"}})

    result = _run(dag, tmp_path)

    assert result.passed is True


def test_test_node_variant_list_satisfies_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_test(dag, {"axis_type": "runtime_shape", "variants": ["shape_a"]})

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_test_file_text_satisfies_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_test(dag)
    _write(tmp_path / "tests" / "check_shape.py", "# runtime_shape=shape_a\n")

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_journey_not_executed_under_variant_is_reported(tmp_path: Path):
    dag = _dag_with_axis()
    _add_journey(dag, "happy_path")
    _add_test(dag, {"coverage_axes": [{"axis_type": "runtime_shape", "variant_id": "shape_a"}]})

    result = _run(dag, tmp_path)

    assert any(item["type"] == "journey_not_executed_under_variant" for item in result.violations)


def _add_journey_under_codd(dag: DAG, name: str = "happy_path", doc_id: str = "docs/design/spec.md") -> None:
    dag.add_node(
        Node(
            id=doc_id,
            kind="design_doc",
            path=doc_id,
            attributes={
                "frontmatter": {
                    "codd": {
                        "user_journeys": [
                            {
                                "name": name,
                                "criticality": "critical",
                                "steps": [],
                                "required_capabilities": [],
                                "expected_outcome_refs": [],
                            }
                        ]
                    }
                }
            },
        )
    )


def test_journey_under_frontmatter_codd_is_not_dormant(tmp_path: Path):
    # C9 must read user_journeys nested under the canonical frontmatter.codd location
    # (via the shared collector), not just top-level attrs — else a journey authored
    # there is dormant and journey_not_executed_under_variant never fires (false-green).
    dag = _dag_with_axis()
    _add_journey_under_codd(dag, "happy_path")
    _add_test(dag, {"coverage_axes": [{"axis_type": "runtime_shape", "variant_id": "shape_a"}]})

    result = _run(dag, tmp_path)

    assert any(item["type"] == "journey_not_executed_under_variant" for item in result.violations)


def test_journey_execution_attribute_satisfies_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_journey(dag, "happy_path")
    _add_test(
        dag,
        {
            "coverage_axes": [{"axis_type": "runtime_shape", "variant_id": "shape_a"}],
            "journey": "happy_path",
        },
    )

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_journey_execution_text_satisfies_variant(tmp_path: Path):
    dag = _dag_with_axis()
    _add_journey(dag, "happy_path")
    _add_test(dag)
    _write(tmp_path / "tests" / "check_shape.py", "# runtime_shape=shape_a\n# happy_path\n")

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_lexicon_axis_relates_to_declared_journey(tmp_path: Path):
    dag = _dag_with_axis()
    _add_journey(dag, "happy_path")

    result = _run(dag, tmp_path)

    assert any(item.get("journey") == "happy_path" for item in result.violations)


def test_design_doc_axis_relates_only_to_owner_doc(tmp_path: Path):
    dag = _dag_with_axis(_axis(source="design_doc", owner="docs/design/a.md"))
    _add_journey(dag, "journey_a", "docs/design/a.md")
    _add_journey(dag, "journey_b", "docs/design/b.md")
    _add_test(
        dag,
        {
            "coverage_axes": [{"axis_type": "runtime_shape", "variant_id": "shape_a"}],
            "journey": "journey_b",
        },
    )

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert "journey_a" in journeys
    assert "journey_b" not in journeys


def test_format_report_returns_json(tmp_path: Path):
    result = _run(_dag_with_axis(), tmp_path)

    payload = json.loads(EnvironmentCoverageCheck().format_report(result))

    assert payload["environment_coverage_report"][0]["type"] == "missing_test_for_variant"


def test_run_checks_recognizes_environment_coverage(tmp_path: Path):
    result = run_checks(DAG(), tmp_path, {}, check_names=["environment_coverage"])

    assert result[0].check_name == "environment_coverage"


def test_dag_verify_environment_coverage_check_runs_gracefully(tmp_path: Path):
    # An empty project declares no coverage axes, so the check SKIPs (verified
    # nothing) rather than rendering a clean PASS over zero axes.
    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "environment_coverage"],
    )

    assert result.exit_code == 0
    assert "SKIP  environment_coverage" in result.output


def test_builder_attaches_lexicon_axes(tmp_path: Path):
    _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump(
            {
                "coverage_axes": [
                    {
                        "axis_type": "runtime_shape",
                        "variants": [{"id": "shape_a", "criticality": "critical"}],
                    }
                ]
            },
            sort_keys=False,
        ),
    )

    dag = build_dag(tmp_path, {"lexicon_file": "project_lexicon.yaml"})

    assert dag.coverage_axes[0].axis_type == "runtime_shape"
    assert dag.coverage_axes[0].source == "lexicon"


def test_builder_attaches_design_doc_axes(tmp_path: Path):
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc({"coverage_axes": [{"axis_type": "runtime_shape", "variants": ["shape_a"]}]}),
    )

    dag = build_dag(tmp_path, {"design_doc_patterns": ["docs/design/*.md"]})

    assert dag.coverage_axes[0].source == "design_doc"
    assert dag.coverage_axes[0].owner_section == "docs/design/spec.md"


def test_builder_attaches_design_doc_axes_under_codd_block(tmp_path: Path):
    # Axes authored at the canonical frontmatter.codd position must be attached.
    # Before the central metadata helper they were ignored (C9 dormant), so a
    # codd-nested critical variant with no test silently PASSED (false-green).
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc(
            {
                "codd": {
                    "node_id": "spec",
                    "coverage_axes": [
                        {
                            "axis_type": "runtime_shape",
                            "variants": [{"id": "shape_a", "criticality": "critical"}],
                        }
                    ],
                }
            }
        ),
    )

    dag = build_dag(tmp_path, {"design_doc_patterns": ["docs/design/*.md"]})

    assert [axis.axis_type for axis in dag.coverage_axes] == ["runtime_shape"]
    assert dag.coverage_axes[0].source == "design_doc"

    # And C9 must now actually gate it (critical variant, no covering test → red).
    result = _run(dag, tmp_path)
    assert result.passed is False
    assert any(item["type"] == "missing_test_for_variant" for item in result.violations)


def test_builder_design_doc_axis_under_codd_not_double_counted(tmp_path: Path):
    # A top-level frontmatter axis is lifted into attributes AND kept raw; the
    # builder dedup + helper dedup must keep it to a single axis (no dup variant
    # violations).
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc(
            {
                "coverage_axes": [
                    {
                        "axis_type": "runtime_shape",
                        "variants": [{"id": "shape_a", "criticality": "critical"}],
                    }
                ]
            }
        ),
    )

    dag = build_dag(tmp_path, {"design_doc_patterns": ["docs/design/*.md"]})

    assert len(dag.coverage_axes) == 1
    result = _run(dag, tmp_path)
    missing = [item for item in result.violations if item["type"] == "missing_test_for_variant"]
    assert len(missing) == 1


def test_dag_json_serializes_coverage_axes(tmp_path: Path):
    dag = _dag_with_axis()

    payload = dag_to_dict(dag, tmp_path)

    assert payload["coverage_axes"][0]["axis_type"] == "runtime_shape"


def test_generality_gate_has_zero_hits():
    forbidden = re.compile(
        r"viewport|mobile|desktop|smartphone|iphone|android|responsive|breakpoint|"
        r"chromium|webkit|gecko|375|1920|web app|mobile app|cli|backend|embedded",
        re.IGNORECASE,
    )

    for path in (
        Path("codd/dag/coverage_axes.py"),
        Path("codd/dag/checks/environment_coverage.py"),
    ):
        assert forbidden.search(path.read_text(encoding="utf-8")) is None


def test_result_message_counts_red_and_amber(tmp_path: Path):
    axis = CoverageAxis(
        axis_type="runtime_shape",
        rationale="Mixed variants.",
        variants=[
            CoverageVariant(id="shape_a", label="Shape A", criticality="critical"),
            CoverageVariant(id="shape_b", label="Shape B", criticality="info"),
        ],
        source="lexicon",
    )

    result = _run(_dag_with_axis(axis), tmp_path)

    assert "1 red and 1 amber" in result.message


# --- journey scope (opt-in) -------------------------------------------------


def _scoped_axis(variant_scope=None, axis_scope=None, *, variant_id: str = "shape_a"):
    from codd.dag.coverage_axes import JourneyScope

    def _scope(value):
        if value is None:
            return None
        return JourneyScope.from_value(value)

    return CoverageAxis(
        axis_type="runtime_shape",
        rationale="Scoped coverage.",
        variants=[
            CoverageVariant(
                id=variant_id,
                label=variant_id,
                criticality="critical",
                journey_scope=_scope(variant_scope),
            )
        ],
        source="lexicon",
        owner_section="project_lexicon.yaml",
        journey_scope=_scope(axis_scope),
    )


def _satisfy_variant(dag: DAG, variant_id: str = "shape_a") -> None:
    _add_test(dag, {"coverage_axes": [{"axis_type": "runtime_shape", "variant_id": variant_id}]})


def test_variant_journey_scope_include_limits_cross_product(tmp_path: Path):
    dag = _dag_with_axis(_scoped_axis(variant_scope={"include": ["flow_one"]}))
    _add_journey(dag, "flow_one")
    _add_journey(dag, "flow_two", "docs/design/other.md")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert "flow_one" in journeys
    assert "flow_two" not in journeys


def test_variant_journey_scope_empty_include_drops_all_journeys(tmp_path: Path):
    dag = _dag_with_axis(_scoped_axis(variant_scope={"include": []}))
    _add_journey(dag, "flow_one")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    assert result.violations == []


def test_variant_journey_scope_exclude_drops_named_journey(tmp_path: Path):
    dag = _dag_with_axis(_scoped_axis(variant_scope={"exclude": ["flow_two"]}))
    _add_journey(dag, "flow_one")
    _add_journey(dag, "flow_two", "docs/design/other.md")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert "flow_one" in journeys
    assert "flow_two" not in journeys


def test_axis_journey_scope_applies_to_variants_without_own_scope(tmp_path: Path):
    dag = _dag_with_axis(_scoped_axis(axis_scope={"include": ["flow_one"]}))
    _add_journey(dag, "flow_one")
    _add_journey(dag, "flow_two", "docs/design/other.md")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert journeys == {"flow_one"}


def test_variant_journey_scope_overrides_axis_scope(tmp_path: Path):
    dag = _dag_with_axis(
        _scoped_axis(variant_scope={"include": ["flow_two"]}, axis_scope={"include": ["flow_one"]})
    )
    _add_journey(dag, "flow_one")
    _add_journey(dag, "flow_two", "docs/design/other.md")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert journeys == {"flow_two"}


def test_undeclared_journey_scope_keeps_full_cross_product(tmp_path: Path):
    dag = _dag_with_axis()
    _add_journey(dag, "flow_one")
    _add_journey(dag, "flow_two", "docs/design/other.md")
    _satisfy_variant(dag)

    result = _run(dag, tmp_path)

    journeys = {item.get("journey") for item in result.violations}
    assert journeys == {"flow_one", "flow_two"}


def test_journey_scope_does_not_remove_missing_test_violation(tmp_path: Path):
    dag = _dag_with_axis(_scoped_axis(variant_scope={"include": []}))
    _add_journey(dag, "flow_one")

    result = _run(dag, tmp_path)

    assert [item["type"] for item in result.violations] == ["missing_test_for_variant"]


def test_builder_attaches_lexicon_axis_journey_scope(tmp_path: Path):
    _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump(
            {
                "coverage_axes": [
                    {
                        "axis_type": "runtime_shape",
                        "variants": [
                            {
                                "id": "shape_a",
                                "criticality": "critical",
                                "journey_scope": {"include": ["flow_one"]},
                            }
                        ],
                    }
                ]
            },
            sort_keys=False,
        ),
    )

    dag = build_dag(tmp_path, {"lexicon_file": "project_lexicon.yaml"})

    scope = dag.coverage_axes[0].variants[0].journey_scope
    assert scope is not None
    assert scope.applies_to("flow_one") is True
    assert scope.applies_to("flow_two") is False

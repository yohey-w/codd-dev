"""Tests for codd.restoration_report — the brownfield coverage/limits report.

Covers: locator-shape evidence-source attribution, confidence-band roll-up,
open-question theme grouping, artifact-type coverage vs the capability profile,
maintenance_ready detection, the graceful empty case, and both renderers
(text + json). Fixtures are synthetic restored docs written to a temp project so
the aggregator exercises the same frontmatter parser the DAG scanner uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.restoration_report import (
    BAND_AMBER,
    BAND_GREEN,
    SRC_IAC,
    SRC_RATIONALE,
    SRC_SOURCE,
    SRC_TESTS,
    SRC_UNKNOWN,
    THEME_NFR_PRIORITY,
    THEME_OTHER,
    THEME_RATIONALE,
    THEME_REJECTED,
    THEME_THRESHOLD,
    THEME_UNBUILT,
    build_restoration_report,
    classify_locator,
    classify_open_question_theme,
    render_report_json,
    render_report_text,
)


# ---------------------------------------------------------------------------
# Locator-shape classifier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "locator,expected",
    [
        ("k8s/api.yaml::Deployment::api", SRC_IAC),
        ("infra/db.yaml::HorizontalPodAutoscaler::api-hpa", SRC_IAC),
        ("main.tf::aws_db_instance.primary", SRC_IAC),
        ("Dockerfile", SRC_IAC),
        ("docker-compose.yml::web", SRC_IAC),
        (".github/workflows/test.yml::Tests", SRC_IAC),
        ("kubernetes::namespaces", SRC_IAC),
        ("github-actions::environments", SRC_IAC),
        ("tests/test_auth.py::test_login_succeeds", SRC_TESTS),
        ("tests/test_auth.py::test_login_succeeds[admin]", SRC_TESTS),
        ("test_signup_creates_user", SRC_TESTS),
        ("suite::should_reject_expired_token", SRC_TESTS),
        ("README.md", SRC_RATIONALE),
        ("docs/adr/0001-use-postgres.md", SRC_RATIONALE),
        ("CHANGELOG.md", SRC_RATIONALE),
        ("docs/decisions/0003-caching.md", SRC_RATIONALE),
        ("src/auth/service.py", SRC_SOURCE),
        ("app/models.py::User", SRC_SOURCE),
        ("", SRC_UNKNOWN),
        ("   ", SRC_UNKNOWN),
    ],
)
def test_classify_locator_by_shape(locator: str, expected: str) -> None:
    assert classify_locator(locator) == expected


def test_classify_locator_rationale_beats_path_shape() -> None:
    # A README path that also looks file-ish must classify as rationale.
    assert classify_locator("docs/README.md") == SRC_RATIONALE


# ---------------------------------------------------------------------------
# Open-question theme classifier
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question,why,expected",
    [
        ("Why was Postgres chosen over MySQL?", "alternatives not in code", THEME_REJECTED),
        ("Relative priority of availability vs cost?", "priority weights", THEME_NFR_PRIORITY),
        ("Why is the timeout 30s?", "threshold justification absent", THEME_THRESHOLD),
        ("Any features planned but never built?", "unbuilt intent", THEME_UNBUILT),
        ("What business goal motivated this?", "rationale not in code", THEME_RATIONALE),
        ("Totally generic residue", "", THEME_OTHER),
        ("", "", THEME_OTHER),
    ],
)
def test_classify_open_question_theme(question: str, why: str, expected: str) -> None:
    assert classify_open_question_theme(question, why) == expected


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _write_doc(path: Path, codd_block: dict, body: str = "# Doc\n\nContent.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump({"codd": codd_block}, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")


def _make_project(tmp_path: Path, *, project_type: str | None = None) -> Path:
    """Create a minimal CoDD project with a .codd dir + codd.yaml."""
    root = tmp_path / "proj"
    (root / ".codd").mkdir(parents=True, exist_ok=True)
    config: dict = {"scan": {"doc_dirs": ["docs"]}}
    if project_type is not None:
        config["required_artifacts"] = {"project_type": project_type}
    (root / ".codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root


def _restored_requirements_block() -> dict:
    return {
        "node_id": "req:inferred",
        "type": "requirement",
        "source": "extracted",
        "depends_on": [{"id": "design:extract:system-context", "relation": "derives_from"}],
        "provenance": [
            {
                "statement": "System exposes a login endpoint",
                "evidence": [
                    "tests/test_auth.py::test_login_succeeds",
                    "src/auth/routes.py",
                ],
                "band": "green",
            },
            {
                "statement": "API tolerates single-instance failure (>=3 replicas)",
                "evidence": ["k8s/api.yaml::Deployment::api"],
                "band": "amber",
            },
            {
                "statement": "Changes are gated by CI tests",
                "evidence": [".github/workflows/test.yml::Tests"],
                "band": "green",
            },
        ],
        "confidence_bands": {"green": 2, "amber": 1},
        "open_questions": [
            {
                "question": "Why was Postgres chosen over alternatives?",
                "why_unrecoverable": "rejected alternatives are not encoded in source",
                "needs_human_confirmation": True,
            },
            {
                "question": "What is the relative priority of availability vs cost?",
                "why_unrecoverable": "NFR priority weights are a business decision",
                "needs_human_confirmation": True,
            },
        ],
        "assumptions": [
            {"assumption": "auth module is the only entry point", "basis": "src/auth", "needs_human_confirmation": True},
        ],
    }


def _restored_design_block() -> dict:
    return {
        "node_id": "design:extract:system-context",
        "type": "design",
        "source": "extracted",
        "depends_on": [],
        "provenance": [
            {
                "statement": "Service runs as a long-running HTTP server",
                "evidence": ["Dockerfile", "src/app.py"],
                "band": "green",
            },
        ],
        "open_questions": [
            {
                "question": "Why is the request timeout set to 30s?",
                "why_unrecoverable": "the threshold value's justification is not in code",
                "needs_human_confirmation": True,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Aggregation correctness
# ---------------------------------------------------------------------------
def test_band_and_source_attribution_rollup(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())

    report = build_restoration_report(root)

    assert report.has_restoration is True
    assert report.total_restored_artifacts == 2
    # 3 provenance items in requirements + 1 in design = 4 statements.
    assert report.total_statements == 4
    # green: 2 (req) + 1 (design) = 3; amber: 1.
    assert report.band_counts[BAND_GREEN] == 3
    assert report.band_counts[BAND_AMBER] == 1

    # Evidence-source attribution from locator shapes:
    #   tests: test_login_succeeds (1)
    #   source: src/auth/routes.py, src/app.py (2)
    #   iac: k8s Deployment + CI workflow + Dockerfile (3)
    assert report.source_counts[SRC_TESTS] == 1
    assert report.source_counts[SRC_SOURCE] == 2
    assert report.source_counts[SRC_IAC] == 3


def test_open_question_grouping_by_theme(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())

    report = build_restoration_report(root)

    assert report.open_question_total == 3
    themes = {g.theme: len(g.questions) for g in report.open_question_groups}
    assert themes.get(THEME_REJECTED) == 1
    assert themes.get(THEME_NFR_PRIORITY) == 1
    assert themes.get(THEME_THRESHOLD) == 1
    # Every grouped question retains needs_human_confirmation.
    for group in report.open_question_groups:
        for q in group.questions:
            assert q["needs_human_confirmation"] is True

    assert report.assumption_total == 1


# ---------------------------------------------------------------------------
# Artifact-type coverage vs capability profile
# ---------------------------------------------------------------------------
def test_artifact_type_coverage_web_expects_infra_ops(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())

    report = build_restoration_report(root)
    cov = {c.artifact_id: c for c in report.artifact_type_coverage}

    # requirements + design restored; test_doc missing-but-expected.
    assert cov["requirements"].restored is True
    assert cov["design_spec"].restored is True
    assert cov["test_doc"].restored is False
    assert cov["test_doc"].expected is True

    # web has an http network surface ⇒ infra/ops/NFR are expected-but-missing.
    assert cov["infrastructure_design"].expected is True
    assert cov["infrastructure_design"].restored is False
    assert cov["operations_runbook"].expected is True
    assert "operational surface" in cov["operations_runbook"].note


def test_artifact_type_coverage_cli_does_not_expect_infra_ops(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="cli")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    report = build_restoration_report(root)
    cov = {c.artifact_id: c for c in report.artifact_type_coverage}

    # A CLI has no operational surface ⇒ infra/ops/NFR are NOT expected.
    assert cov["infrastructure_design"].expected is False
    assert cov["operations_runbook"].expected is False
    assert cov["deployment_design"].expected is False
    assert cov["non_functional_requirements"].expected is False
    # ...and are flagged as capability-conditional, not failures.
    assert cov["infrastructure_design"].capability_conditional is True


def test_restored_infra_artifact_detected(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    infra_block = {
        "node_id": "design:infra",
        "type": "design",
        "source": "extracted",
        "depends_on": [],
        "provenance": [
            {"statement": "3 replicas", "evidence": ["k8s/api.yaml::Deployment::api"], "band": "green"},
        ],
    }
    _write_doc(root / "docs" / "infra" / "infrastructure.md", infra_block)

    report = build_restoration_report(root)
    cov = {c.artifact_id: c for c in report.artifact_type_coverage}
    assert cov["infrastructure_design"].restored is True
    assert cov["infrastructure_design"].restored_paths == ["docs/infra/infrastructure.md"]


# ---------------------------------------------------------------------------
# Maintenance readiness detection
# ---------------------------------------------------------------------------
def test_maintenance_ready_when_edges_resolve(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    # design has no deps; requirements depends on the design node (resolvable).
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    report = build_restoration_report(root)
    m = report.maintenance
    assert m.maintenance_ready is True
    assert m.restored_node_count == 2
    assert m.discoverable_by_scanner is True
    assert not m.unresolved_dependencies
    assert "codd dag verify" in m.handoff


def test_maintenance_not_ready_on_unresolved_edge(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    block = _restored_requirements_block()
    block["depends_on"] = [{"id": "design:does-not-exist", "relation": "derives_from"}]
    _write_doc(root / "docs" / "requirements" / "inferred.md", block)

    report = build_restoration_report(root)
    m = report.maintenance
    assert m.maintenance_ready is False
    assert "design:does-not-exist" in m.unresolved_dependencies.get("req:inferred", [])
    assert any("unresolved depends_on" in r for r in m.reasons)


def test_maintenance_not_ready_on_missing_extracted_marker(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    block = _restored_design_block()
    # Carries restoration metadata but is NOT marked source: extracted.
    block.pop("source", None)
    _write_doc(root / "docs" / "design" / "system_context.md", block)

    report = build_restoration_report(root)
    m = report.maintenance
    assert m.maintenance_ready is False
    assert m.nodes_missing_extracted_marker == ["docs/design/system_context.md"]


def test_authored_docs_without_restoration_are_ignored(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    # An authored design doc: no source: extracted, no restoration metadata.
    _write_doc(
        root / "docs" / "design" / "authored.md",
        {"node_id": "design:authored", "type": "design", "depends_on": []},
    )
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    report = build_restoration_report(root)
    # Only the restored doc participates.
    assert report.total_restored_artifacts == 1
    assert report.artifacts[0].node_id == "req:inferred"


# ---------------------------------------------------------------------------
# Graceful empty case
# ---------------------------------------------------------------------------
def test_graceful_empty_case(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    # No restored docs at all.
    report = build_restoration_report(root)

    assert report.has_restoration is False
    assert report.total_restored_artifacts == 0
    assert report.total_statements == 0
    assert report.open_question_total == 0
    assert report.maintenance.maintenance_ready is False
    assert any("No restoration metadata found" in n for n in report.notes)
    assert "No restoration metadata found" in report.summary_line


def test_restored_docs_without_provenance_block(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    # Marked extracted (so it counts as restored) but no provenance metadata.
    _write_doc(
        root / "docs" / "design" / "extracted_only.md",
        {"node_id": "design:extracted-only", "type": "design", "source": "extracted", "depends_on": []},
    )
    report = build_restoration_report(root)
    assert report.has_restoration is True
    assert report.total_restored_artifacts == 1
    assert report.total_statements == 0
    assert any("none carry a codd_restoration" in n for n in report.notes)


# ---------------------------------------------------------------------------
# Rendering (text + json)
# ---------------------------------------------------------------------------
def test_render_json_roundtrips(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())

    report = build_restoration_report(root)
    payload = json.loads(render_report_json(report))

    assert payload["has_restoration"] is True
    assert payload["recovered"]["total_statements"] == 4
    assert payload["recovered"]["band_counts"]["green"] == 3
    assert payload["recovered"]["source_counts"]["iac"] == 3
    assert payload["irrecoverable_in_principle"]["open_question_total"] == 3
    assert isinstance(payload["artifact_type_coverage"], list)
    assert payload["maintenance"]["maintenance_ready"] is True
    assert "summary_line" in payload and payload["summary_line"]


def test_render_text_contains_sections(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    report = build_restoration_report(root)
    text = render_report_text(report)

    assert "RECOVERED" in text
    assert "IRRECOVERABLE IN PRINCIPLE" in text
    assert "COVERAGE BY ARTIFACT TYPE" in text
    assert "MAINTENANCE-LOOP READINESS" in text
    assert "maintenance_ready:" in text
    assert "Recovered" in text  # summary line


def test_render_text_empty_case(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    report = build_restoration_report(root)
    text = render_report_text(report)
    assert "No restoration metadata found" in text


# ---------------------------------------------------------------------------
# Summary line content
# ---------------------------------------------------------------------------
def test_summary_line_reports_percentages_and_ceiling(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())
    _write_doc(root / "docs" / "design" / "system_context.md", _restored_design_block())

    report = build_restoration_report(root)
    line = report.summary_line
    assert "4 provenance-backed statement" in line
    assert "% green" in line and "% amber" in line
    assert "open question" in line
    # The ceiling themes are named in the summary.
    assert "rejected alternatives" in line or "NFR priority weights" in line


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------
def test_cli_restore_report_text(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codd.cli import main

    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    runner = CliRunner()
    result = runner.invoke(main, ["restore", "--report", "--path", str(root)])
    assert result.exit_code == 0, result.output
    assert "RECOVERED" in result.output
    assert "maintenance_ready:" in result.output


def test_cli_restore_report_json(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codd.cli import main

    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    runner = CliRunner()
    result = runner.invoke(main, ["restore", "--report", "--format", "json", "--path", str(root)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["has_restoration"] is True
    assert payload["recovered"]["total_statements"] == 3


def test_cli_restore_requires_wave_without_report(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codd.cli import main

    root = _make_project(tmp_path, project_type="web")
    runner = CliRunner()
    result = runner.invoke(main, ["restore", "--path", str(root)])
    assert result.exit_code != 0
    assert "--wave is required" in result.output


# ---------------------------------------------------------------------------
# H2: candidate_answer leads from git-history testimony
# ---------------------------------------------------------------------------
def _block_with_candidate_answer() -> dict:
    block = _restored_requirements_block()
    block["open_questions"][0]["candidate_answer"] = {
        "text": "Postgres was chosen for transactional billing guarantees.",
        "provenance": "commit:abc1234 (2023-05-01)",
        "corroborated": True,
    }
    return block


def test_candidate_answer_leads_counted_and_passed_through(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _block_with_candidate_answer())

    report = build_restoration_report(root)

    # 2 open questions, 1 of which carries a git-history lead.
    assert report.open_question_total == 2
    assert report.open_questions_with_candidate_answers == 1

    grouped = [q for g in report.open_question_groups for q in g.questions]
    with_lead = [q for q in grouped if q.get("candidate_answer")]
    assert len(with_lead) == 1
    lead = with_lead[0]["candidate_answer"]
    assert lead["provenance"] == "commit:abc1234 (2023-05-01)"
    assert lead["corroborated"] is True
    # A lead is NOT an answer: the question still needs human confirmation.
    assert with_lead[0]["needs_human_confirmation"] is True


def test_candidate_answer_count_in_json_and_text(tmp_path: Path) -> None:
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _block_with_candidate_answer())

    report = build_restoration_report(root)
    payload = json.loads(render_report_json(report))
    assert payload["irrecoverable_in_principle"]["open_questions_with_candidate_answers"] == 1

    text = render_report_text(report)
    assert "1 of them carry a candidate_answer lead from git-history testimony" in text


def test_candidate_answer_absent_degrades_gracefully(tmp_path: Path) -> None:
    """Documents without candidate_answer (all pre-H2 docs) behave exactly as
    before: count is 0, no extra text line, JSON field present at 0."""
    root = _make_project(tmp_path, project_type="web")
    _write_doc(root / "docs" / "requirements" / "inferred.md", _restored_requirements_block())

    report = build_restoration_report(root)
    assert report.open_questions_with_candidate_answers == 0

    payload = json.loads(render_report_json(report))
    assert payload["irrecoverable_in_principle"]["open_questions_with_candidate_answers"] == 0

    text = render_report_text(report)
    assert "candidate_answer lead" not in text

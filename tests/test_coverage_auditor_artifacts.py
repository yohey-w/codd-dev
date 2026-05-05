from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.coverage_auditor import (
    ASK,
    AUTO_ACCEPT,
    AUTO_REJECT,
    ArtifactGap,
    CoverageAuditor,
    GapItem,
)


def _write_config(project: Path, body: str = "{}\n") -> None:
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(body, encoding="utf-8")


def _write_lexicon(project: Path, artifacts: list[dict]) -> None:
    payload = {
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [],
        "required_artifacts": artifacts,
    }
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _artifact(
    artifact_id: str = "design:screen_flow_design",
    *,
    title: str = "Screen Flow Design",
    scope: str = "Navigation transitions",
    source: str = "ai_derived",
) -> dict:
    return {
        "id": artifact_id,
        "title": title,
        "depends_on": [],
        "scope": scope,
        "rationale": "Requirements mention multi-screen navigation.",
        "source": source,
    }


def test_artifact_gap_dataclass_attributes():
    gap = ArtifactGap(
        artifact_id="design:screen_flow_design",
        title="Screen Flow Design",
        severity=ASK,
        rationale="Navigation is required.",
    )

    assert gap.artifact_id == "design:screen_flow_design"
    assert gap.title == "Screen Flow Design"
    assert gap.severity == ASK
    assert gap.rationale == "Navigation is required."
    assert gap.source == "ai_derived"


def test_audit_required_artifacts_all_present_returns_no_gaps(tmp_path):
    design_dir = tmp_path / "docs" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "screen-flow.md").write_text("# Screen Flow\n", encoding="utf-8")

    gaps = CoverageAuditor(tmp_path).audit_required_artifacts([_artifact()], tmp_path)

    assert gaps == []


def test_audit_required_artifacts_missing_returns_ask_gap(tmp_path):
    gaps = CoverageAuditor(tmp_path).audit_required_artifacts([_artifact()], tmp_path)

    assert len(gaps) == 1
    assert gaps[0].artifact_id == "design:screen_flow_design"
    assert gaps[0].severity == ASK


def test_default_template_without_scope_is_auto_reject(tmp_path):
    artifact = _artifact(scope="", source="default_template")

    gaps = CoverageAuditor(tmp_path).audit_required_artifacts([artifact], tmp_path)

    assert gaps[0].severity == AUTO_REJECT


def test_ai_derived_without_scope_is_ask(tmp_path):
    artifact = _artifact(scope="", source="ai_derived")

    gaps = CoverageAuditor(tmp_path).audit_required_artifacts([artifact], tmp_path)

    assert gaps[0].severity == ASK


def test_discover_existing_artifacts_maps_screen_flow_filename(tmp_path):
    design_dir = tmp_path / "docs" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "screen-flow.md").write_text("# Screen Flow\n", encoding="utf-8")

    artifacts = CoverageAuditor(tmp_path)._discover_existing_artifacts(tmp_path)

    assert "design:screen_flow_design" in artifacts


def test_discover_existing_artifacts_uses_codd_yaml_path_override(tmp_path):
    _write_config(tmp_path, "artifact_discovery:\n  paths:\n    - docs/specs\n")
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    (specs_dir / "requirements.md").write_text("# Requirements\n", encoding="utf-8")

    artifacts = CoverageAuditor(tmp_path)._discover_existing_artifacts(tmp_path)

    assert artifacts == {"design:requirements"}


def test_discover_existing_artifacts_missing_directory_is_empty(tmp_path):
    _write_config(tmp_path, "artifact_discovery:\n  paths:\n    - docs/missing\n")

    artifacts = CoverageAuditor(tmp_path)._discover_existing_artifacts(tmp_path)

    assert artifacts == set()


def test_require_audit_cli_outputs_required_artifact_section(tmp_path):
    _write_config(tmp_path)
    req_dir = tmp_path / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "requirements.md").write_text(
        "Users move from login to dashboard to detail pages.",
        encoding="utf-8",
    )
    _write_lexicon(tmp_path, [_artifact()])

    result = CliRunner().invoke(main, ["require", "--path", str(tmp_path), "--audit"])

    assert result.exit_code == 0, result.output
    assert "Required artifacts audit complete:" in result.output
    assert "Missing required artifacts:" in result.output
    report = tmp_path / "docs" / "requirements" / "coverage_audit_report.md"
    text = report.read_text(encoding="utf-8")
    assert "## Required Artifacts Audit" in text
    assert "### Missing required artifacts" in text
    assert "[ASK] design:screen_flow_design" in text


def test_artifact_mapping_is_overridable_from_codd_yaml(tmp_path):
    _write_config(
        tmp_path,
        "artifact_discovery:\n"
        "  paths:\n"
        "    - docs/specs\n"
        "  mappings:\n"
        "    specs/navigation.md: design:screen_flow_design\n",
    )
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    (specs_dir / "navigation.md").write_text("# Navigation\n", encoding="utf-8")

    gaps = CoverageAuditor(tmp_path).audit_required_artifacts([_artifact()], tmp_path)

    assert gaps == []


def test_existing_coverage_auditor_three_class_rule_still_works(tmp_path):
    gap = GapItem(
        id="low_confidence_control",
        label="Low confidence control",
        classification=AUTO_ACCEPT,
        confidence=0.84,
    )
    result = CoverageAuditor(tmp_path).classify_gaps("LMS/EdTech", [])

    assert gap.classification == ASK
    assert result.auto_accept
    assert result.ask
    assert result.auto_reject

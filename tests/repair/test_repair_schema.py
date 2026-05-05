from __future__ import annotations

from dataclasses import asdict

import pytest
import yaml

from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


def _roundtrip_dataclass(instance, cls):
    loaded = yaml.safe_load(yaml.safe_dump(asdict(instance), sort_keys=True))
    return cls(**loaded)


def test_verification_failure_report_yaml_roundtrip():
    report = VerificationFailureReport(
        check_name="node_completeness",
        failed_nodes=["design:auth"],
        error_messages=["missing impl"],
        dag_snapshot={"nodes": [{"id": "design:auth"}], "edges": []},
        timestamp="2026-05-06T00:00:00Z",
    )

    assert _roundtrip_dataclass(report, VerificationFailureReport) == report


def test_root_cause_analysis_yaml_roundtrip():
    rca = RootCauseAnalysis(
        probable_cause="missing implementation file",
        affected_nodes=["design:auth"],
        repair_strategy="unified_diff",
        confidence=0.75,
        analysis_timestamp="2026-05-06T00:00:01Z",
    )

    assert _roundtrip_dataclass(rca, RootCauseAnalysis) == rca


def test_file_patch_yaml_roundtrip():
    patch = FilePatch(
        file_path="src/auth.py",
        patch_mode="full_file_replacement",
        content="print('ok')\n",
    )

    assert _roundtrip_dataclass(patch, FilePatch) == patch


def test_repair_proposal_yaml_roundtrip_rehydrates_nested_patches():
    proposal = RepairProposal(
        patches=[FilePatch("src/auth.py", "unified_diff", "--- a\n+++ b\n")],
        rationale="align implementation with design",
        confidence=0.9,
        proposal_timestamp="2026-05-06T00:00:02Z",
        rca_reference="2026-05-06T00:00:01Z",
    )

    assert _roundtrip_dataclass(proposal, RepairProposal) == proposal


def test_apply_result_yaml_roundtrip():
    result = ApplyResult(
        success=False,
        applied_patches=["src/ok.py"],
        failed_patches=["src/fail.py"],
        error_message="patch rejected",
    )

    assert _roundtrip_dataclass(result, ApplyResult) == result


def test_verification_failure_report_requires_all_fields():
    with pytest.raises(TypeError):
        VerificationFailureReport(check_name="c7", failed_nodes=[], error_messages=[], dag_snapshot={})


def test_file_patch_rejects_unknown_patch_mode():
    with pytest.raises(ValueError, match="patch_mode"):
        FilePatch("src/auth.py", "ast_rewrite", "payload")  # type: ignore[arg-type]


def test_root_cause_analysis_rejects_unknown_repair_strategy():
    with pytest.raises(ValueError, match="patch_mode"):
        RootCauseAnalysis("cause", [], "ast_rewrite", 0.5, "2026-05-06T00:00:00Z")  # type: ignore[arg-type]


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError, match="confidence"):
        RepairProposal([], "too confident", 1.5, "2026-05-06T00:00:00Z", "rca")

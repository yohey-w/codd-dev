"""Tests for codd fixup-drift Phase 1 plumbing."""

from __future__ import annotations

import subprocess

from click.testing import CliRunner

from codd.cli import main
from codd.coherence_engine import DriftEvent
from codd.fixup_drift import _filter_events, _log_hitl, run_fixup_drift
from codd.fixup_drift_strategies import (
    BaseFixStrategy,
    FixProposal,
    get_strategy,
    list_registered_kinds,
    register_strategy,
)


def _event(*, kind="url_drift", severity="red", payload=None) -> DriftEvent:
    return DriftEvent(
        source_artifact="design_doc",
        target_artifact="implementation",
        change_type="modified",
        payload=payload or {"description": "route drift"},
        severity=severity,
        fix_strategy="auto" if severity == "red" else "hitl",
        kind=kind,
    )


@register_strategy
class _TestUrlStrategy(BaseFixStrategy):
    KIND = "url_drift"

    def propose(self, event):
        return [
            FixProposal(
                kind="url_drift",
                file_path="app/routes.py",
                diff="--- a/app/routes.py\n+++ b/app/routes.py\n@@\n-old\n+new\n",
                description=event.payload["description"],
                severity=event.severity,
                can_auto_apply=False,
            )
        ]

    def apply(self, proposal):
        return True


def test_strategy_registry():
    assert "url_drift" in list_registered_kinds()


def test_get_strategy_registered(tmp_path):
    strategy = get_strategy("url_drift", tmp_path)

    assert isinstance(strategy, _TestUrlStrategy)
    assert strategy.project_root == tmp_path


def test_get_strategy_unknown(tmp_path):
    assert get_strategy("unknown_kind", tmp_path) is None


def test_dry_run_returns_proposals_without_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.fixup_drift._collect_events",
        lambda project_root, bus: [_event(payload={"description": "fix route"})],
    )

    result = run_fixup_drift(tmp_path, dry_run=True, severity_filter="red", kind_filter="url_drift")

    assert len(result["proposals"]) == 1
    assert result["proposals"][0].description == "fix route"
    assert result["applied"] == 0
    assert result["hitl_logged"] == 0
    assert not (tmp_path / "docs" / "coherence" / "pending_hitl.md").exists()


def test_filter_events_by_severity():
    filtered = _filter_events(
        [_event(severity="red"), _event(severity="amber")],
        severity_filter="red",
        kind_filter="all",
    )

    assert [event.severity for event in filtered] == ["red"]


def test_filter_events_by_kind_canonicalizes_drift_entries():
    filtered = _filter_events(
        [
            _event(kind="drift", payload={"drift_type": "design-only"}),
            _event(kind="design_token_drift"),
        ],
        severity_filter="all",
        kind_filter="url_drift",
    )

    assert len(filtered) == 1
    assert filtered[0].kind == "url_drift"


def test_cli_help():
    result = CliRunner().invoke(main, ["fixup-drift", "--help"])

    assert result.exit_code == 0
    assert "--apply" in result.output
    assert "--severity" in result.output
    assert "--kind" in result.output


def test_hitl_log(tmp_path):
    proposal = FixProposal(
        kind="url_drift",
        file_path="app/routes.py",
        diff="-old\n+new\n",
        description="needs human review",
        severity="amber",
        can_auto_apply=False,
    )

    _log_hitl(proposal, tmp_path)

    content = (tmp_path / "docs" / "coherence" / "pending_hitl.md").read_text(encoding="utf-8")
    assert "# Pending HITL Fix Proposals" in content
    assert "[url_drift] needs human review" in content
    assert "```diff\n-old\n+new\n```" in content


def test_apply_with_worktree_rolls_back_on_strategy_failure(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    tracked = project / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    @register_strategy
    class _FailingStrategy(BaseFixStrategy):
        KIND = "unit_fail_drift"

        def propose(self, event):
            return [
                FixProposal(
                    kind=self.KIND,
                    file_path="tracked.txt",
                    diff="-original\n+changed\n",
                    description="failing strategy",
                    severity="red",
                    can_auto_apply=True,
                )
            ]

        def apply(self, proposal):
            (self.project_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "codd.fixup_drift._collect_events",
        lambda project_root, bus: [_event(kind="unit_fail_drift")],
    )

    result = run_fixup_drift(project, dry_run=False, severity_filter="red", kind_filter="all")

    assert result["applied"] == 0
    assert result["errors"]
    assert tracked.read_text(encoding="utf-8") == "original\n"

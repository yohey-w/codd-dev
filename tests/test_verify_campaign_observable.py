"""Certification fixtures for ``verify.campaign.observable.v1`` (GPT r2 §3.1).

A profile that DECLARES a verify campaign whose ``report_format`` has no
registered runner-report adapter cannot have its executions observed. Before this
gate, ``coherence_gate_applies`` returned False for that state → a SILENT NO-OP
(the campaign-declaring stack was simply skipped). ``certify_verify_campaign_observable``
turns that into an honest-fail (CampaignError); the greenfield pipeline raises it
as a StageError BEFORE the ``coherence_gate_applies`` short-circuit.

These are the contract's negative fixtures: a declared-but-unreadable campaign
MUST red; a declared-and-readable one (and a no-campaign stack) must pass. The
gate runs NO command — it is a pure observability certification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.coverage_execution_coherence import (
    CampaignError,
    certify_verify_campaign_observable,
    coherence_gate_applies,
)
from codd.greenfield.pipeline import GreenfieldPipeline, StageError
from codd.project_types import (
    LayoutProfile,
    VerifyCampaignSpec,
    resolve_layout_profile,
)


def _profile_with_campaign(report_format: str) -> LayoutProfile:
    """A minimal TS-shaped profile declaring a campaign with ``report_format``."""
    return LayoutProfile(
        language="typescript",
        package_name="tc",
        source_root="src",
        package_root="src",
        test_root="tests",
        verify_campaign=VerifyCampaignSpec(
            command_template="echo nothing",
            report_relpath=".codd/verify/report.json",
            report_format=report_format,
        ),
    )


# ─────────────────────────────────────────────────────────────
# the gate itself (pure)
# ─────────────────────────────────────────────────────────────


def test_declared_campaign_without_adapter_raises():
    """GPT §3.1 negative fixture: a campaign with report_format that has no adapter."""
    profile = _profile_with_campaign("pytest-junit-xml")  # documented, not implemented
    # Precondition: this is EXACTLY the state that today silently no-ops.
    assert profile.runner_report_adapter() is None
    assert coherence_gate_applies(profile) is False
    with pytest.raises(CampaignError) as exc:
        certify_verify_campaign_observable(profile)
    assert "no registered runner-report adapter" in str(exc.value)
    assert "pytest-junit-xml" in str(exc.value)


def test_declared_campaign_with_adapter_ok():
    """A campaign whose report_format HAS an adapter (vitest-json) passes."""
    profile = _profile_with_campaign("vitest-json")
    assert profile.runner_report_adapter() is not None
    # No raise.
    certify_verify_campaign_observable(profile)


def test_no_campaign_is_noop_ok(tmp_path):
    """A profile with NO campaign (Python today) is a legitimate no-op — passes."""
    profile = resolve_layout_profile(
        language="python", project_name="todo", project_root=tmp_path
    )
    assert profile is not None
    assert profile.verify_campaign is None
    # No raise (the gate only fires for a DECLARED-but-unreadable campaign).
    certify_verify_campaign_observable(profile)


def test_totally_unknown_format_raises():
    """An entirely unknown report_format also honest-fails (not just the documented
    extension points)."""
    profile = _profile_with_campaign("totally-unknown-format")
    with pytest.raises(CampaignError):
        certify_verify_campaign_observable(profile)


def test_gate_runs_no_command(tmp_path, monkeypatch):
    """The certification is side-effect-free: it must not spawn a subprocess."""
    import subprocess as _sp

    def _boom(*a, **k):  # pragma: no cover - only hit on a regression
        raise AssertionError("certify_verify_campaign_observable must not run a command")

    monkeypatch.setattr(_sp, "run", _boom)
    # adapterless → raises CampaignError, but via NO subprocess. (pytest-junit-xml
    # is the remaining documented-but-unimplemented format; go-test-json now HAS an
    # adapter, so it is exercised on the with-adapter side below.)
    with pytest.raises(CampaignError):
        certify_verify_campaign_observable(_profile_with_campaign("pytest-junit-xml"))
    # with-adapter → passes, also via no subprocess (vitest-json AND go-test-json).
    certify_verify_campaign_observable(_profile_with_campaign("vitest-json"))
    certify_verify_campaign_observable(_profile_with_campaign("go-test-json"))


# ─────────────────────────────────────────────────────────────
# pipeline wiring: honest-fail BEFORE the coherence_gate_applies short-circuit
# ─────────────────────────────────────────────────────────────


def test_pipeline_honest_fails_on_adapterless_campaign(tmp_path, monkeypatch):
    """The pipeline's coverage-coherence stage raises a StageError (not a silent
    skip) when the resolved profile declares an unreadable campaign."""
    pipeline = GreenfieldPipeline()
    adapterless = _profile_with_campaign("pytest-junit-xml")
    monkeypatch.setattr(
        pipeline, "_resolve_layout_profile", lambda _root: adapterless
    )
    with pytest.raises(StageError) as exc:
        pipeline._enforce_coverage_execution_coherence(tmp_path, {})
    assert "cannot be observed" in str(exc.value)


def test_pipeline_skips_cleanly_for_no_campaign(tmp_path, monkeypatch):
    """A no-campaign profile makes the stage a clean NO-OP (empty detail), not a
    failure — the gate must only fire on a DECLARED-but-unreadable campaign."""
    pipeline = GreenfieldPipeline()
    profile = resolve_layout_profile(
        language="python", project_name="todo", project_root=tmp_path
    )
    monkeypatch.setattr(pipeline, "_resolve_layout_profile", lambda _root: profile)
    detail = pipeline._enforce_coverage_execution_coherence(tmp_path, {})
    assert detail == ""


def test_pipeline_respects_coverage_gate_off(tmp_path, monkeypatch):
    """When the owner turned coverage gating off, the observability gate is skipped
    too (consistent with the existing coverage_gate switch)."""
    pipeline = GreenfieldPipeline()
    adapterless = _profile_with_campaign("pytest-junit-xml")
    monkeypatch.setattr(
        pipeline, "_resolve_layout_profile", lambda _root: adapterless
    )
    # coverage_gate=False → the whole stage is a no-op, even with a bad campaign.
    detail = pipeline._enforce_coverage_execution_coherence(
        tmp_path, {"coverage_gate": False}
    )
    assert detail == ""

"""SSoT default value tests (cmd_466)."""

from __future__ import annotations

from codd.defaults import AI_TIMEOUT_SECONDS


def test_ai_timeout_seconds_is_3600() -> None:
    """Sourced from feedback_codd_default_values_policy (殿 2026-05-11)."""
    assert AI_TIMEOUT_SECONDS == 3600.0


def test_ai_command_default_imports_ssot() -> None:
    from codd.deployment.providers.ai_command import DEFAULT_TIMEOUT_SECONDS

    assert DEFAULT_TIMEOUT_SECONDS == AI_TIMEOUT_SECONDS


def test_required_artifacts_deriver_default_imports_ssot() -> None:
    from codd.required_artifacts_deriver import AI_TIMEOUT_SECONDS as DERIVER_TIMEOUT

    assert DERIVER_TIMEOUT == int(AI_TIMEOUT_SECONDS)


def test_requirement_completeness_auditor_default_imports_ssot() -> None:
    from codd.requirement_completeness_auditor import AI_TIMEOUT_SECONDS as AUDITOR_TIMEOUT

    assert AUDITOR_TIMEOUT == int(AI_TIMEOUT_SECONDS)

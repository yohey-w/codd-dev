"""Regression tests for AI command default timeout.

cmd_466 raised the default to 3600s via the ``codd.defaults`` SSoT; see
``feedback_codd_default_values_policy``. The override paths
(``CODD_AI_TIMEOUT_SECONDS`` env, ``llm.timeout_seconds`` config) remain
unchanged.
"""

from __future__ import annotations

import pytest

from codd.defaults import AI_TIMEOUT_SECONDS
from codd.deployment.providers.ai_command import (
    DEFAULT_TIMEOUT_SECONDS,
    resolve_timeout,
)


def test_default_timeout_seconds_matches_ssot() -> None:
    """cmd_466: ai_command default is sourced from codd.defaults."""
    assert DEFAULT_TIMEOUT_SECONDS == AI_TIMEOUT_SECONDS == 3600.0


def test_resolve_timeout_uses_default_when_no_input() -> None:
    assert resolve_timeout() == DEFAULT_TIMEOUT_SECONDS


def test_resolve_timeout_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODD_AI_TIMEOUT_SECONDS", raising=False)
    assert resolve_timeout(timeout=42.0) == 42.0


def test_resolve_timeout_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODD_AI_TIMEOUT_SECONDS", "60")
    assert resolve_timeout() == 60.0


def test_resolve_timeout_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODD_AI_TIMEOUT_SECONDS", raising=False)
    config = {"llm": {"timeout_seconds": 300}}
    assert resolve_timeout(config) == 300.0


def test_resolve_timeout_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODD_AI_TIMEOUT_SECONDS", "not-a-number")
    assert resolve_timeout() == DEFAULT_TIMEOUT_SECONDS

"""Regression tests for cmd_454: ensure DEFAULT_TIMEOUT_SECONDS = 1800."""

from __future__ import annotations

import pytest

from codd.deployment.providers.ai_command import (
    DEFAULT_TIMEOUT_SECONDS,
    resolve_timeout,
)


def test_default_timeout_seconds_is_1800() -> None:
    """cmd_454 default: 30 minutes accommodates multi-lexicon elicit pipelines."""
    assert DEFAULT_TIMEOUT_SECONDS == 1800.0


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

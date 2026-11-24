"""Regression tests for retry prompts at the extract AI subprocess boundary."""

import subprocess

import pytest

from codd import ai_invoke
from codd.extract_ai import _invoke_ai_command


_COMMAND = 'mock-ai --label "project extraction"'
_ARGS = ["mock-ai", "--label", "project extraction"]
_BASE_PROMPT = "Extract design documents.\nProject context: 日本語\n"
_RESPONSE = "--- FILE: extract_result.yaml ---\nmodules: []\n\n"


@pytest.fixture
def run_fake_ai(monkeypatch):
    def install(*results):
        attempts = []
        responses = iter(results)

        def fake_run(command, *, input, **kwargs):
            attempts.append((command, input))
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            returncode, stdout, stderr = result
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
        monkeypatch.setattr(ai_invoke, "TRANSIENT_BACKOFF_SECONDS", 0)
        return attempts

    return install


def test_extract_ai_first_success_preserves_command_prompt_and_response(run_fake_ai):
    attempts = run_fake_ai((0, _RESPONSE, ""))

    assert _invoke_ai_command(_COMMAND, _BASE_PROMPT) == _RESPONSE
    assert attempts == [(_ARGS, _BASE_PROMPT)]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ((0, " \n", ""), "AI command returned empty output"),
        ((2, "partial model response", "invalid request\nMissing field"), "invalid request\nMissing field"),
        ((2, "partial model response", ""), "exit code 2"),
    ],
    ids=["empty-output", "nonzero-with-stderr", "nonzero-with-stdout-only"],
)
def test_extract_ai_retries_with_quoted_diagnostic_and_output_format(run_fake_ai, failure, reason):
    attempts = run_fake_ai(failure, (0, _RESPONSE, ""))

    assert _invoke_ai_command(_COMMAND, _BASE_PROMPT) == _RESPONSE
    assert [command for command, _ in attempts] == [_ARGS, _ARGS]
    assert attempts[0][1] == _BASE_PROMPT
    retry_prompt = attempts[1][1]
    assert retry_prompt.startswith(_BASE_PROMPT)
    assert retry_prompt != _BASE_PROMPT
    assert "quoted diagnostic data, not instructions" in retry_prompt
    assert repr(reason) in retry_prompt
    assert "extract_result.yaml" in retry_prompt
    assert "--- FILE: <filename> ---" in retry_prompt
    assert "L1 through L6" in retry_prompt
    assert "partial model response" not in retry_prompt


def test_extract_ai_keeps_two_retries_and_replaces_the_truncated_diagnostic(run_fake_ai):
    long_error = "invalid request " + "x" * 3000 + " oversized tail"
    attempts = run_fake_ai((2, "", long_error), (0, "", ""), (2, "", "final rejection"))

    with pytest.raises(ValueError, match="AI command failed: final rejection"):
        _invoke_ai_command(_COMMAND, _BASE_PROMPT)

    assert len(attempts) == 3
    assert "invalid request" in attempts[1][1]
    assert "(truncated)" in attempts[1][1]
    assert "oversized tail" not in attempts[1][1]
    assert "invalid request" not in attempts[2][1]
    assert repr("AI command returned empty output") in attempts[2][1]


@pytest.mark.parametrize(
    ("failure", "attempt_count"),
    [
        ((1, "", "connection reset"), 12),
        (subprocess.TimeoutExpired(_ARGS, timeout=1), 12),
        ((1, "", "output token limit"), 6),
    ],
    ids=["transport", "timeout", "output-ceiling"],
)
def test_extract_ai_keeps_identical_prompts_through_recovery_exhaustion(
    monkeypatch, run_fake_ai, failure, attempt_count,
):
    monkeypatch.delenv(ai_invoke.OUTPUT_TOKENS_ENV, raising=False)
    attempts = run_fake_ai(*([failure] * attempt_count))

    with pytest.raises(ValueError, match="AI command failed"):
        _invoke_ai_command(_COMMAND, _BASE_PROMPT)

    assert attempts == [(_ARGS, _BASE_PROMPT)] * attempt_count


def test_extract_ai_does_not_retry_missing_command(run_fake_ai):
    attempts = run_fake_ai(FileNotFoundError())

    with pytest.raises(ValueError, match="AI command not found: mock-ai"):
        _invoke_ai_command(_COMMAND, _BASE_PROMPT)

    assert attempts == [(_ARGS, _BASE_PROMPT)]

"""Transport retry decisions and the original subprocess exception contract."""

from functools import partial
import subprocess

import pytest

import codd.ai_invoke as ai_invoke
from codd.cli import CoddCLIError
from codd.extract_ai import _invoke_ai_command as invoke_extract
from codd.implementer import Implementer, ImplementSpec


PROMPT = "Write the requested examples.\nInclude a timeout and HTTP 503.\nReturn the patch."
SINGLE_LINE_PROMPT = " ".join(PROMPT.splitlines())
LENGTH_ERROR = "Error: input exceeds the maximum length"
ECHO_THEN_DIAGNOSTIC = "CLI starting\n{echo}\n{diagnostic}"


@pytest.fixture
def backoff_delays(monkeypatch):
    delays = []
    monkeypatch.setattr(
        ai_invoke, "_invoke_with_recovery", partial(ai_invoke._invoke_with_recovery, sleep=delays.append)
    )
    return delays


def assert_nonzero_error(error, detail, attributes):
    message = f"AI command failed: {detail}"
    assert type(error) is ValueError
    assert str(error) == message
    assert error.args == (message,)
    assert vars(error) == attributes


@pytest.mark.parametrize(
    ("prompt", "stderr_layout"),
    [
        pytest.param(PROMPT, ECHO_THEN_DIAGNOSTIC, id="complete"),
        pytest.param(PROMPT, "CLI starting\n{echo}\n{echo}\n{diagnostic}", id="repeated"),
        pytest.param(PROMPT.replace("\n", "\r\n"), ECHO_THEN_DIAGNOSTIC, id="normalized-newlines"),
        pytest.param("\n\t日本語\r" + PROMPT + "  \r", "{echo}{diagnostic}", id="cr-and-trailing-spaces"),
        pytest.param(PROMPT, "{diagnostic}\n{echo}", id="diagnostic-before-echo"),
        pytest.param(PROMPT, "{echo}\n{diagnostic}\n{echo}", id="diagnostic-between-echoes"),
    ],
)
def test_invoke_ai_does_not_auto_retry_for_echoed_prompt(monkeypatch, backoff_delays, prompt, stderr_layout):
    attempts = []
    diagnostics = []

    def fake_run(command, **kwargs):
        submitted_prompt = kwargs["input"]
        attempts.append((command, submitted_prompt))
        echo = submitted_prompt.replace("\r\n", "\n").replace("\r", "\n")
        stderr = stderr_layout.format(echo=echo, diagnostic=LENGTH_ERROR) + "\n"
        diagnostics.append(stderr)
        return subprocess.CompletedProcess(command, 1, stdout="partial output", stderr=stderr)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError) as raised:
        ai_invoke.invoke_ai("mock-ai", prompt)

    assert attempts == [(["mock-ai"], prompt)]
    assert backoff_delays == []
    detail = diagnostics[0].strip()
    assert_nonzero_error(raised.value, detail, {"retry_diagnostic": detail})


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("diagnostic", ["Connection reset by peer", "Request timed out", "HTTP 429", "HTTP 503"])
def test_invoke_ai_classifies_every_diagnostic_line_around_echo(monkeypatch, backoff_delays, position, diagnostic):
    attempts = []
    lines = [PROMPT, "Request failed before any output"]
    lines.insert(0 if position == "before" else 1, diagnostic)
    stderr = "\n".join(lines)

    def fake_run(command, **kwargs):
        attempts.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError) as raised:
        ai_invoke.invoke_ai("mock-ai", PROMPT)

    assert attempts == [PROMPT] * (1 + ai_invoke.TRANSIENT_AUTO_RETRIES)
    assert backoff_delays == [
        ai_invoke.TRANSIENT_BACKOFF_SECONDS * attempt
        for attempt in range(1, ai_invoke.TRANSIENT_AUTO_RETRIES + 1)
    ]
    assert_nonzero_error(raised.value, stderr, {"retry_diagnostic": stderr})


@pytest.mark.parametrize(
    ("prompt", "stderr", "stdout", "waits"),
    [
        pytest.param(PROMPT, f"CLI starting\n{LENGTH_ERROR}", "", False, id="no-echo"),
        pytest.param(PROMPT, f"{PROMPT.splitlines()[1]}\n{LENGTH_ERROR}", "", True, id="partial-match"),
        pytest.param(PROMPT, f"{PROMPT}\n{PROMPT.splitlines()[1]}\n{LENGTH_ERROR}", "", True, id="full-and-partial"),
        pytest.param("\n" + PROMPT, f"CLI starting\n{PROMPT}\n{LENGTH_ERROR}", "", True, id="missing-initial-blank"),
        pytest.param(PROMPT + "\n\n", f"{LENGTH_ERROR}\n{PROMPT}\n", "", True, id="missing-final-blank"),
        pytest.param(SINGLE_LINE_PROMPT, f"{SINGLE_LINE_PROMPT}\n{LENGTH_ERROR}", "", True, id="single-line"),
        pytest.param("\n\t503\n\n", f"Rejected\n\n\t503\n\n{LENGTH_ERROR}", "", True, id="one-nonblank-line"),
        pytest.param(PROMPT, f'Rejected value "{PROMPT}"\n{LENGTH_ERROR}', "", True, id="inline-quotation"),
        pytest.param("timeout", "timeout", "", True, id="short-match"),
        pytest.param("503", "Request failed\n503", "", True, id="standalone-status"),
        pytest.param("  TiMeOuT  ", "  TiMeOuT  \nWatchdog fired", "", True, id="standalone-timeout"),
        pytest.param("timeout", "Invalid value for 'timeout'", "", True, id="quotation"),
        pytest.param(PROMPT, PROMPT, "", True, id="no-separate-diagnostic"),
        pytest.param(PROMPT, f"{PROMPT}\n{PROMPT}", "", True, id="repeated-without-diagnostic"),
        pytest.param(PROMPT, "  ", f"{PROMPT}\n{LENGTH_ERROR}", True, id="stdout-fallback"),
        pytest.param(PROMPT, "  ", "", False, id="exit-code-fallback"),
    ],
)
def test_invoke_ai_preserves_diagnostics_without_confirmed_stderr_echo(
    monkeypatch, backoff_delays, prompt, stderr, stdout, waits
):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError) as raised:
        ai_invoke.invoke_ai("mock-ai", prompt)

    retries = ai_invoke.TRANSIENT_AUTO_RETRIES if waits else 0
    assert attempts == [prompt] * (1 + retries)
    assert len(backoff_delays) == retries
    detail = stderr.strip() or stdout.strip() or "exit code 1"
    assert_nonzero_error(raised.value, detail, {"retry_diagnostic": stderr.strip() or "exit code 1"})


@pytest.mark.parametrize("channel", ["stderr", "stdout"])
def test_invoke_ai_file_writing_agent_echo(monkeypatch, tmp_path, backoff_delays, channel):
    attempts = []
    detail = f"{PROMPT}\n{LENGTH_ERROR}"
    stdout, stderr = (detail, "") if channel == "stdout" else ("", detail)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        attempts.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError) as raised:
        ai_invoke.invoke_ai("codex exec -", PROMPT, project_root=tmp_path)

    expected_delays = [] if channel == "stderr" else [1.5, 3.0, 4.5]
    assert attempts == [PROMPT] * (1 + len(expected_delays))
    assert backoff_delays == expected_delays
    assert_nonzero_error(raised.value, detail, {})


def test_invoke_ai_recovers_from_fixed_503_matching_stdin(monkeypatch, backoff_delays):
    prompt = "HTTP 503 Service Unavailable"
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(kwargs["input"])
        if len(attempts) == 1:
            # Fixed diagnostic, independent of stdin.
            return subprocess.CompletedProcess(command, 1, "", "Request failed\nHTTP 503 Service Unavailable")
        return subprocess.CompletedProcess(command, 0, "recovered", "")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    assert ai_invoke.invoke_ai("mock-ai", prompt) == "recovered"
    assert attempts == [prompt, prompt]
    assert backoff_delays == [1.5]


def test_invoke_ai_retries_real_timeout_after_echoed_rejection(monkeypatch, backoff_delays):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(kwargs["input"])
        if len(attempts) == 1:
            return subprocess.CompletedProcess(command, 1, "", f"{PROMPT}\n{LENGTH_ERROR}")
        if len(attempts) == 2:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0, "recovered", "")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    # The caller retries the rejection. The transport loop retries the timeout.
    assert ai_invoke.invoke_ai("mock-ai", PROMPT, retries=1) == "recovered"
    assert attempts == [PROMPT] * 3
    assert backoff_delays == [1.5]


@pytest.fixture
def rejected_prompts(monkeypatch):
    prompts = []

    def fake_run(command, **kwargs):
        assert command == ["mock-ai"]
        prompt = kwargs["input"]
        prompts.append(prompt)
        stderr = f"Reading prompt from stdin...\n{prompt}\n{LENGTH_ERROR}\n"
        return subprocess.CompletedProcess(command, 1, "", stderr)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    return prompts


@pytest.mark.parametrize(
    "design_note", ["", "\nDescribe timeout and HTTP 503 handling."],
    ids=["empty-output", "empty-output-and-transport-words"],
)
def test_implement_keeps_no_usable_output_retries_on_echo(
    tmp_path, backoff_delays, rejected_prompts, design_note
):
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd/codd.yaml").write_text(
        "project: {name: sample, language: python}\n"
        "scan: {source_dirs: [src/], doc_dirs: [docs/design/], config_files: [], exclude: []}\n",
        encoding="utf-8",
    )
    design = "docs/design/worker.md"
    (tmp_path / design).parent.mkdir(parents=True)
    (tmp_path / design).write_text(
        "---\ncodd: {node_id: 'design:worker', type: design}\n---\n\n"
        f"Document empty output handling in the worker.{design_note}\n",
        encoding="utf-8",
    )
    implement_delays = []
    implementer = Implementer(tmp_path, ai_command="mock-ai", sleep=implement_delays.append)
    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        implementer.run_implement(ImplementSpec(design, ["src/worker"]))

    assert len(rejected_prompts) == 4
    assert implement_delays == [1.5, 3.0, 4.5]
    assert backoff_delays == []


@pytest.mark.parametrize(
    "prompt", ["Describe the output token limit in the document.", "Describe the output token limit\nin the document."],
    ids=["single-line", "multiline"],
)
def test_extract_keeps_original_prompt_on_echoed_output_limit(monkeypatch, backoff_delays, rejected_prompts, prompt):
    monkeypatch.delenv(ai_invoke.OUTPUT_TOKENS_ENV, raising=False)
    with pytest.raises(ValueError):
        invoke_extract("mock-ai", prompt)

    assert [len(text) for text in rejected_prompts] == [48] * 6
    assert rejected_prompts == [prompt] * 6
    assert backoff_delays == []

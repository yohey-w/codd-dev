"""FX1 — markdown-fence contamination fix + write-time syntax gate.

A real-AI greenfield dogfood produced ten Python files containing literal
markdown fences: ``__init__.py`` files whose entire AI-emitted content was an
EMPTY fence pair (```` ```python ````+newline+```` ``` ````) written verbatim
to disk — an immediate SyntaxError that the verify stage then missed.

Two layers pinned here:

1. ``_strip_code_fence`` robustness — empty fence pairs unwrap to an empty
   string (an intentionally empty file), CRLF/trailing-space/no-language
   fences unwrap, leading/trailing ORPHAN fence lines are stripped for
   non-markdown destinations only, and mid-content fences are never touched.
2. The write-time syntax gate (``implement.syntax_gate``, default ON) — a
   payload that does not parse (Python/JSON/YAML/TOML) is NEVER silently
   written; the error feeds one bounded retry and then fails the task naming
   the file(s).
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.ai_patch import parse_fix_blocks
from codd.cli import CoddCLIError
from codd.implementer import (
    DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS,
    ImplementSpec,
    ImplementSyntaxGateError,
    Implementer,
    _describe_nonascii_code_chars,
    _payload_syntax_error,
    _strip_code_fence,
    _syntax_gate_max_attempts,
)


# ---------------------------------------------------------------------------
# helpers (same shape as tests/implement/test_implement_spec.py)
# ---------------------------------------------------------------------------


def _write_doc(project: Path, relative_path: str, *, node_id: str, body: str) -> None:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"{yaml.safe_dump({'codd': {'node_id': node_id, 'type': 'design'}}, sort_keys=False)}"
        "---\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )


def _project(tmp_path: Path, *, language: str = "python", implement_config: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    config: dict = {
        "project": {"name": "demo", "language": language},
        "ai_command": "mock-ai --print",
        "scan": {
            "source_dirs": ["src/"],
            "doc_dirs": ["docs/design/"],
            "config_files": [],
            "exclude": [],
        },
    }
    if implement_config is not None:
        config["implement"] = implement_config
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    _write_doc(
        project,
        "docs/design/auth.md",
        node_id="design:auth",
        body="# Auth Design\n\nBuild an auth service.",
    )
    return project


def _patch_ai_sequence(monkeypatch: pytest.MonkeyPatch, outputs: list[str]) -> list[str]:
    """Scripted AI returning ``outputs`` per call (last output repeats)."""
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        output = outputs[min(len(calls) - 1, len(outputs) - 1)]
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


VALID_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "def build_auth() -> bool:\n"
    "    return True\n"
    "```\n"
)
BROKEN_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "def f(:\n"
    "    return 1\n"
    "```\n"
    "=== FILE: src/auth/helper.py ===\n"
    "```python\n"
    "def ok() -> int:\n"
    "    return 2\n"
    "```\n"
)
# The EXACT live-dogfood shape: a model primed by Japanese context slips a
# full-width period (。, U+3002) into a code position — invalid Python.
NONASCII_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "def build_auth() -> bool:\n"
    "    return True。\n"  # full-width period in code position
    "```\n"
)
# An em-dash (—, U+2014) used where an ASCII operator/punctuation belongs.
EMDASH_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "x = 1 — 2\n"  # em-dash where '-' belongs
    "```\n"
)


# ---------------------------------------------------------------------------
# 1. _strip_code_fence robustness (unit)
# ---------------------------------------------------------------------------


def test_strip_code_fence_empty_pair_returns_empty_string():
    """The EXACT observed artifact: an empty fenced block must unwrap to ''."""
    assert _strip_code_fence("```python\n```") == ""


@pytest.mark.parametrize(
    "raw",
    [
        "```python\r\n```",  # CRLF empty pair
        "```\n```",  # no language
        "```python   \n```",  # trailing spaces after the language tag
        "  ```python\n```  ",  # surrounded by whitespace
        "```python\n\n```",  # blank line between the fences
    ],
)
def test_strip_code_fence_empty_pair_variants(raw: str):
    assert _strip_code_fence(raw) == ""


def test_strip_code_fence_unwraps_language_with_trailing_spaces():
    assert _strip_code_fence("```python   \ncode = 1\n```") == "code = 1"


def test_strip_code_fence_unwraps_without_language():
    assert _strip_code_fence("```\ncode = 1\n```") == "code = 1"


def test_strip_code_fence_unwraps_crlf_line_endings():
    cleaned = _strip_code_fence("```python\r\nline1 = 1\r\nline2 = 2\r\n```")
    assert "```" not in cleaned
    assert "line1 = 1" in cleaned
    assert "line2 = 2" in cleaned


def test_strip_code_fence_strips_leading_orphan_fence():
    """Opening fence without a closing one (half-wrapped payload)."""
    cleaned = _strip_code_fence("```python\ndef ok():\n    return 1")
    assert cleaned == "def ok():\n    return 1"


def test_strip_code_fence_strips_trailing_orphan_fence():
    cleaned = _strip_code_fence("def ok():\n    return 1\n```")
    assert cleaned.strip() == "def ok():\n    return 1"


def test_strip_code_fence_keeps_mid_content_fences():
    """Fence lines in the MIDDLE of content are legitimate — never touched."""
    raw = 'A = 1\n```\nB = 2'
    assert _strip_code_fence(raw) == raw
    inline = 'DOC = "see ```python example```"\nvalue = DOC'
    assert _strip_code_fence(inline) == inline


def test_strip_code_fence_markdown_destination_keeps_orphan_fences():
    """Fences are legitimate markdown content — the destination decides."""
    raw = "# Guide\n\n```python\nprint(1)\n```"
    assert _strip_code_fence(raw, destination="docs/guide/usage.md") == raw
    assert _strip_code_fence(raw, destination="docs/guide/usage.markdown") == raw
    # The same shape destined to a non-markdown file gets the trailing lone
    # fence (its last non-blank line) stripped.
    cleaned = _strip_code_fence(raw, destination="src/guide.py")
    assert cleaned.rstrip().endswith("print(1)")


# ---------------------------------------------------------------------------
# 2. The observed artifact end-to-end: empty fence pair → EMPTY file on disk
# ---------------------------------------------------------------------------


def test_empty_fence_pair_payload_writes_empty_python_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`__init__.py` emitted as ```python\\n``` is written EMPTY (header only)."""
    project = _project(tmp_path)
    _patch_ai_sequence(
        monkeypatch,
        [
            "=== FILE: src/auth/__init__.py ===\n"
            "```python\n"
            "```\n" + VALID_PY_OUTPUT
        ],
    )

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    init_file = project / "src" / "auth" / "__init__.py"
    assert init_file in result.generated_files
    content = init_file.read_text(encoding="utf-8")
    assert "```" not in content  # the literal fences never reach disk
    assert content.startswith("# @generated-by: codd implement")
    # Everything beyond the provenance header is empty, and the file parses.
    body = "\n".join(line for line in content.splitlines() if not line.startswith("#"))
    assert body.strip() == ""
    ast.parse(content)


# ---------------------------------------------------------------------------
# 3. Write-time syntax gate: fail honestly, retry once, never write broken
# ---------------------------------------------------------------------------


def test_broken_python_fails_after_bounded_retry_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [BROKEN_PY_OUTPUT])

    with pytest.raises(CoddCLIError, match=r"src/auth/service\.py"):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    # Bounded retries: the default budget is 3 total AI invocations, then an
    # honest failure.
    assert len(calls) == 3
    # Each retry prompt carries the syntax error as review feedback.
    assert "not valid Python" in calls[1]
    assert "src/auth/service.py" in calls[1]
    assert "not valid Python" in calls[2]
    # The broken content is NOT on disk — and neither is the valid sibling
    # (validate-all-then-write keeps the task output atomic).
    assert not (project / "src" / "auth" / "service.py").exists()
    assert not (project / "src" / "auth" / "helper.py").exists()


def test_broken_python_recovers_on_the_bounded_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [BROKEN_PY_OUTPUT, VALID_PY_OUTPUT])

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(calls) == 2
    service = project / "src" / "auth" / "service.py"
    assert service in result.generated_files
    ast.parse(service.read_text(encoding="utf-8"))


def test_syntax_gate_opt_out_restores_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implement.syntax_gate: false → single attempt, broken content written."""
    project = _project(tmp_path, implement_config={"syntax_gate": False})
    calls = _patch_ai_sequence(monkeypatch, [BROKEN_PY_OUTPUT])

    Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(calls) == 1  # no retry when the gate is off
    assert "def f(:" in (project / "src" / "auth" / "service.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. Non-Python formats: stdlib-checkable ones validated, others skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "broken", "valid"),
    [
        ("settings.json", "{ not json\n", '{"key": "value"}\n'),
        ("config.yaml", "key: [a, b\n", "key:\n  - a\n  - b\n"),
        ("config.yml", "key: [a, b\n", "key: value\n"),
        ("app.toml", "= invalid\n", 'key = "value"\n'),
    ],
)
def test_structured_formats_are_gate_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, broken: str, valid: str
) -> None:
    project = _project(tmp_path)
    block = f"=== FILE: src/auth/{filename} ===\n```\n{broken}```\n"
    calls = _patch_ai_sequence(monkeypatch, [block])

    with pytest.raises(CoddCLIError, match=filename.replace(".", r"\.")):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))
    assert len(calls) == 3  # default bounded budget
    assert not (project / "src" / "auth" / filename).exists()

    # The valid counterpart passes the gate and lands on disk.
    _patch_ai_sequence(monkeypatch, [f"=== FILE: src/auth/{filename} ===\n```\n{valid}```\n"])
    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))
    assert project / "src" / "auth" / filename in result.generated_files


def test_unparseable_formats_are_skipped_by_design() -> None:
    """No stdlib parser for TS/JS/Go/... — verify-stage gate is the backstop."""
    assert _payload_syntax_error("src/app/index.ts", "const x: = broken {{{") is None
    assert _payload_syntax_error("src/app/main.go", "func broken( {") is None
    # The checkable formats DO report errors with the file's format named.
    assert "not valid Python" in _payload_syntax_error("a.py", "def f(:")
    assert "not valid JSON" in _payload_syntax_error("a.json", "{")
    assert "not valid YAML" in _payload_syntax_error("a.yaml", "key: [a,")
    assert "not valid TOML" in _payload_syntax_error("a.toml", "= bad")


# ---------------------------------------------------------------------------
# 5. Markdown destinations end-to-end: fences survive the pipeline
# ---------------------------------------------------------------------------


def test_markdown_file_payload_keeps_fenced_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _patch_ai_sequence(
        monkeypatch,
        [
            "=== FILE: docs/guide/usage.md ===\n"
            "# Usage\n"
            "\n"
            "Example:\n"
            "\n"
            "```python\n"
            "print(1)\n"
            "```\n"
        ],
    )

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["docs/guide"])
    )

    usage = project / "docs" / "guide" / "usage.md"
    assert usage in result.generated_files
    content = usage.read_text(encoding="utf-8")
    assert "```python" in content  # fences are legitimate markdown content
    assert "print(1)" in content


# ---------------------------------------------------------------------------
# 6. ai_patch verdict: the fix-path parser is structurally immune
# ---------------------------------------------------------------------------


def test_ai_patch_empty_fence_block_yields_empty_content_not_fences() -> None:
    """parse_fix_blocks' content group stops at the FIRST closing fence, so
    literal fence lines can never reach disk through the fix path; an empty
    block parses as an (intentionally) empty file."""
    blocks = parse_fix_blocks("```python src/pkg/__init__.py\n```")
    assert blocks == [("src/pkg/__init__.py", "")]


# ---------------------------------------------------------------------------
# 7. Non-ASCII-in-code retry feedback (F-syntax-gate-nonascii-feedback)
#    The gate is RIGHT to reject; the fix is actionable feedback + one more try.
# ---------------------------------------------------------------------------


def test_describe_nonascii_code_chars_reports_codepoint_and_name() -> None:
    """The detector names the char, its U+XXXX codepoint, and (free) its name."""
    described = _describe_nonascii_code_chars("def f():\n    return True。\n", lineno=2)
    assert len(described) == 1
    entry = described[0]
    assert "line 2" in entry
    assert "U+3002" in entry  # full-width period
    assert "IDEOGRAPHIC FULL STOP" in entry  # unicodedata name, when cheap


def test_describe_nonascii_code_chars_flagged_line_first_and_bounded() -> None:
    """Parser-flagged line is reported first; the enumeration is capped."""
    content = "a = '—'\n" + "".join(f"b{i} = '。'\n" for i in range(10))
    described = _describe_nonascii_code_chars(content, lineno=3)
    assert described, "expected at least one offending character"
    assert "line 3" in described[0]  # the parser-flagged line leads
    assert len(described) <= 5  # bounded so the prompt stays small


def test_describe_nonascii_code_chars_empty_when_pure_ascii() -> None:
    assert _describe_nonascii_code_chars("def f():\n    return 1\n", lineno=1) == []


def test_gate_feedback_contains_codepoint_line_and_ascii_directive() -> None:
    """The injected retry feedback must steer the model to ASCII — explicitly."""
    error = ImplementSyntaxGateError(
        [("src/auth/service.py", "not valid Python (line 2: invalid character '。' (U+3002))")],
        payloads={"src/auth/service.py": "def f():\n    return True。\n"},
    )
    feedback = error.feedback_message()
    # File path + the failing line + the offending codepoint.
    assert "src/auth/service.py" in feedback
    assert "line 2" in feedback
    assert "U+3002" in feedback
    # The actionable ASCII directive (generic, language-neutral framing).
    assert "ASCII" in feedback
    assert "code position" in feedback
    assert "literals and comments" in feedback
    # Mentions the canonical typographic offenders with their codepoints.
    assert "U+2014" in feedback  # em dash guidance


def test_gate_feedback_without_payload_still_carries_ascii_directive() -> None:
    """Even with no captured payload, the ASCII directive is always present."""
    error = ImplementSyntaxGateError(
        [("src/auth/service.py", "not valid Python (line 9: invalid syntax)")]
    )
    feedback = error.feedback_message()
    assert "src/auth/service.py" in feedback
    assert "ASCII" in feedback
    assert "U+2014" in feedback


def test_nonascii_in_code_recovers_within_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-dogfood case: model slips 。 twice, then lands a clean file on
    the THIRD attempt — now within the (default 3) budget. Pre-fix (budget 2)
    this exhausted and failed."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(
        monkeypatch,
        [NONASCII_PY_OUTPUT, EMDASH_PY_OUTPUT, VALID_PY_OUTPUT],
    )

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth"])
    )

    assert len(calls) == 3  # recovered on the third (last allowed) attempt
    # The corrective feedback reached the model on each retry, with codepoints.
    assert "U+3002" in calls[1]  # first retry flags the full-width period
    assert "U+2014" in calls[2]  # second retry flags the em-dash
    service = project / "src" / "auth" / "service.py"
    assert service in result.generated_files
    ast.parse(service.read_text(encoding="utf-8"))


def test_permanently_nonascii_file_still_fails_with_nothing_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely unfixable file is NOT written — the gate is not weakened."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [NONASCII_PY_OUTPUT])

    with pytest.raises(CoddCLIError, match=r"src/auth/service\.py"):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(calls) == DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS  # bounded, not unbounded
    assert not (project / "src" / "auth" / "service.py").exists()


def test_syntax_gate_max_attempts_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implement.syntax_gate_max_attempts overrides the default budget."""
    project = _project(
        tmp_path, implement_config={"syntax_gate": True, "syntax_gate_max_attempts": 2}
    )
    calls = _patch_ai_sequence(monkeypatch, [NONASCII_PY_OUTPUT])

    with pytest.raises(CoddCLIError):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(calls) == 2  # honored the configured (lower) budget


def test_syntax_gate_max_attempts_resolver_defaults_and_clamps() -> None:
    """Default is 3; junk / sub-1 values fall back to the default; >=1 honored."""
    assert _syntax_gate_max_attempts({}) == DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS == 3
    assert _syntax_gate_max_attempts({"implement": {"syntax_gate_max_attempts": 5}}) == 5
    assert _syntax_gate_max_attempts({"implement": {"syntax_gate_max_attempts": 1}}) == 1
    # Sub-1 and non-integer fall back to the default (never unbounded, never 0).
    assert _syntax_gate_max_attempts({"implement": {"syntax_gate_max_attempts": 0}}) == 3
    assert _syntax_gate_max_attempts({"implement": {"syntax_gate_max_attempts": -4}}) == 3
    assert _syntax_gate_max_attempts({"implement": {"syntax_gate_max_attempts": "x"}}) == 3

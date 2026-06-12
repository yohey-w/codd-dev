"""F-confusable-passthrough — confusable / non-ASCII-in-code detector (layer 2).

The FX1 write-time syntax gate (``ast.parse``) catches non-ASCII that BREAKS
parsing (a full-width period in a code position). It cannot catch a homoglyph in
a syntactically-VALID position: a Cyrillic ``е`` (U+0435) or ``а`` (U+0430)
substituted for an ASCII letter INSIDE an identifier — ``іd`` looks like ``id``
but is a different name (NameError / silent dict-key mismatch at runtime). The
file PARSES FINE, may even pass tests written with the same wrong char, but is
semantically broken: a false-green. Current-generation models (Sonnet AND Opus)
emit these intermittently — a model-generation-instability robustness gap.

A SEPARATE deterministic detector (``implement.confusable_check``, default ON)
runs alongside the syntax gate in the write-time path. Precision is the whole
game (false positives are the main risk), so it is deliberately narrow:

* It tokenizes (Python: stdlib ``tokenize``) and inspects ONLY code-position
  tokens — NAME/identifier and OP/operator tokens. STRING, COMMENT, and
  f-string text tokens are skipped ENTIRELY, so non-ASCII inside string literals
  and comments (Japanese UI copy, etc.) is NEVER flagged (the critical
  false-positive guard).
* In NAME tokens it flags only MIXED-SCRIPT identifiers (ASCII-Latin mixed with
  Cyrillic/Greek — almost always a homoglyph slip, near-zero false-positive). A
  single-script non-Latin identifier a language genuinely allows is NOT flagged.
* In OP tokens it flags any non-ASCII (full-width punctuation etc.).

On detection the same atomic "nothing written until clean" + bounded retry +
actionable feedback machinery the FX1 path uses kicks in.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import CoddCLIError
from codd.implementer import (
    DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS,
    ImplementSpec,
    ImplementSyntaxGateError,
    Implementer,
    _confusable_check_enabled,
    _confusable_code_error,
    _confusable_findings,
    _confusable_scripts_in_identifier,
)


# ---------------------------------------------------------------------------
# helpers (same shape as tests/test_implement_syntax_gate.py)
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
# A Cyrillic homoglyph (і, U+0456) substituted for the ASCII 'i' inside an
# otherwise-ASCII identifier `id`. PARSES FINE (it is a valid identifier char),
# so the syntax gate passes it; only the confusable detector catches it.
CYRILLIC_ID_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "def build_auth():\n"
    "    іd = 1\n"  # Cyrillic і (U+0456) — looks like 'id'
    "    return іd\n"
    "```\n"
)
# A Cyrillic 'е' (U+0435) inside the name `def` -> the token `dеf` is a NAME
# (NOT the keyword), a mixed-script identifier — again parses, again caught only
# by the confusable detector.
CYRILLIC_DEF_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "dеf = 1\n"  # Cyrillic е (U+0435) in 'def' -> mixed-script NAME, valid Python
    "build_auth = dеf\n"
    "```\n"
)
# The critical false-positive guard: non-ASCII ONLY inside a string literal and
# a comment — both legitimate (Japanese UI copy). Must NOT be flagged.
JAPANESE_LITERAL_PY_OUTPUT = (
    "=== FILE: src/auth/service.py ===\n"
    "```python\n"
    "def build_auth() -> str:\n"
    '    msg = "こんにちは"  # 日本語コメント\n'
    "    return msg\n"
    "```\n"
)


# ---------------------------------------------------------------------------
# 1. Unit: script / mixed-script identifier classification (the precision core)
# ---------------------------------------------------------------------------


def test_mixed_script_latin_plus_cyrillic_is_flagged() -> None:
    """Latin + Cyrillic in one identifier — the high-signal homoglyph pattern."""
    assert _confusable_scripts_in_identifier("іd") == {"CYRILLIC"}  # Cyrillic і
    assert _confusable_scripts_in_identifier("dеf") == {"CYRILLIC"}  # Cyrillic е


def test_mixed_script_latin_plus_greek_is_flagged() -> None:
    assert _confusable_scripts_in_identifier("xα") == {"GREEK"}  # Latin x + Greek α


def test_pure_ascii_identifier_is_not_flagged() -> None:
    assert _confusable_scripts_in_identifier("id") == set()
    assert _confusable_scripts_in_identifier("build_auth") == set()


def test_single_script_non_latin_identifier_is_not_flagged() -> None:
    """A legitimately single-script name (no ASCII-Latin letter) is NOT a slip."""
    assert _confusable_scripts_in_identifier("αβγ") == set()  # pure Greek αβγ
    assert _confusable_scripts_in_identifier("名前") == set()  # pure CJK 名前


# ---------------------------------------------------------------------------
# 2. Unit: _confusable_findings isolates code positions from literals/comments
# ---------------------------------------------------------------------------


def test_findings_flag_mixed_script_identifier_with_codepoint_and_script() -> None:
    findings = _confusable_findings("def f():\n    іd = 1\n")
    assert len(findings) == 1
    entry = findings[0]
    assert "line 2" in entry
    assert "U+0456" in entry  # the Cyrillic codepoint
    assert "CYRILLIC" in entry  # which script
    assert "іd" in entry  # the offending identifier itself


def test_findings_skip_non_ascii_inside_string_literal() -> None:
    """CRITICAL false-positive guard: non-ASCII in a STRING is legitimate."""
    assert _confusable_findings('x = "こんにちは"\n') == []


def test_findings_skip_non_ascii_inside_comment() -> None:
    """CRITICAL false-positive guard: non-ASCII in a COMMENT is legitimate."""
    assert _confusable_findings("x = 1  # 日本語\n") == []


def test_findings_skip_non_ascii_inside_fstring_text() -> None:
    """f-string TEXT is a literal too — Japanese inside an f-string is fine."""
    src = 'name = "x"\nmsg = f"こんにちは {name}"\n'
    assert _confusable_findings(src) == []


def test_findings_empty_for_clean_ascii() -> None:
    assert _confusable_findings("def f():\n    return 1\n") == []


def test_findings_bounded_to_cap() -> None:
    """Many homoglyphs are enumerated but capped so the prompt stays small."""
    src = "".join(f"іd{i} = {i}\n" for i in range(10))  # 10 mixed-script names
    findings = _confusable_findings(src)
    assert findings
    assert len(findings) <= 5


def test_findings_on_untokenizable_input_returns_empty() -> None:
    """Un-parseable input is the syntax gate's job — the detector stays silent."""
    assert _confusable_findings("def f(:\n    іd = 1\n") == []


def test_findings_is_the_python_engine_language_routing_is_the_callers_job() -> None:
    """_confusable_findings always tokenizes as Python — language/suffix routing
    (skipping non-Python files) is _confusable_code_error's responsibility, not
    this engine's. So a snippet that happens to tokenize as Python IS inspected
    here regardless of any other language it might be; the non-Python SKIP is
    asserted at the _confusable_code_error layer instead."""
    # `const іd = 1` tokenizes as Python NAME/NAME/OP/NUMBER; the mixed-script
    # `іd` is therefore flagged by the engine...
    assert _confusable_findings("const іd = 1") != []
    # ...but the same content routed through a .ts payload is skipped:
    assert _confusable_code_error("a.ts", "const іd = 1", language="typescript") is None


# ---------------------------------------------------------------------------
# 3. Unit: _confusable_code_error (payload-level, suffix routing)
# ---------------------------------------------------------------------------


def test_confusable_code_error_flags_python_homoglyph() -> None:
    err = _confusable_code_error("src/a.py", "x = 1\nіd = 2\n", language="python")
    assert err is not None
    assert "confusable" in err
    assert "U+0456" in err


def test_confusable_code_error_clean_python_is_none() -> None:
    assert _confusable_code_error("src/a.py", "x = 1\nid = 2\n", language="python") is None


def test_confusable_code_error_string_comment_literal_is_none() -> None:
    """The false-positive guard at the payload boundary."""
    src = 'def f() -> str:\n    msg = "こんにちは"  # 日本語\n    return msg\n'
    assert _confusable_code_error("src/a.py", src, language="python") is None


def test_confusable_code_error_non_python_suffix_skipped() -> None:
    """Non-Python files are skipped — the syntax/verify gate is the backstop."""
    assert _confusable_code_error("src/a.ts", "const іd = 1", language="typescript") is None
    assert _confusable_code_error("src/a.go", "var іd int", language="go") is None


def test_confusable_code_error_extensionless_python_by_language() -> None:
    """An extensionless payload is treated as Python when project language says so."""
    assert _confusable_code_error("src/main", "іd = 1\n", language="python") is not None
    assert _confusable_code_error("src/main", "іd = 1\n", language="typescript") is None


# ---------------------------------------------------------------------------
# 4. Feedback: the homoglyph directive names file/line/char/identifier/script
# ---------------------------------------------------------------------------


def test_confusable_feedback_names_file_codepoint_script_and_mixed_script_directive() -> None:
    error = ImplementSyntaxGateError(
        [],
        confusable_failures=[
            (
                "src/auth/service.py",
                "contains confusable non-ASCII character(s) in code position: "
                "line 2: identifier 'іd' contains 'і' (U+0456, "
                "CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I; CYRILLIC script)",
            )
        ],
    )
    feedback = error.feedback_message()
    assert "src/auth/service.py" in feedback  # the file
    assert "line 2" in feedback  # the line
    assert "U+0456" in feedback  # the confusable codepoint
    assert "CYRILLIC" in feedback  # which script
    assert "іd" in feedback  # the identifier it appears in
    assert "MIXES SCRIPTS" in feedback  # the mixed-script explanation
    assert "homoglyph" in feedback
    # The actionable directive: ASCII letters for code identifiers, NOT literals.
    assert "ASCII Latin letters for ALL code identifiers" in feedback
    assert "do NOT change text inside string literals or comments" in feedback


def test_all_failures_merges_parse_and_confusable_failures() -> None:
    error = ImplementSyntaxGateError(
        [("a.py", "not valid Python (line 1: invalid syntax)")],
        confusable_failures=[("b.py", "contains confusable non-ASCII character(s) ...")],
    )
    paths = [path for path, _ in error.all_failures]
    assert paths == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# 5. End-to-end: the gate rejects, retries within budget, writes nothing on fail
# ---------------------------------------------------------------------------


def test_homoglyph_identifier_is_flagged_and_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core finding: a parses-fine Cyrillic-homoglyph identifier is rejected
    (the syntax gate would pass it) and NOTHING reaches disk."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [CYRILLIC_ID_PY_OUTPUT])

    with pytest.raises(CoddCLIError, match=r"src/auth/service\.py"):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    # The payload DOES parse — proving the syntax gate alone would have written it.
    body = CYRILLIC_ID_PY_OUTPUT.split("```python\n", 1)[1].split("```", 1)[0]
    ast.parse(body)  # no SyntaxError
    # Bounded budget exhausted (default 3), nothing written.
    assert len(calls) == DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS
    assert not (project / "src" / "auth" / "service.py").exists()
    # Each retry carried the actionable homoglyph feedback.
    assert "U+0456" in calls[1]
    assert "MIXES SCRIPTS" in calls[1]


def test_homoglyph_def_token_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Cyrillic 'е' inside `def` makes `dеf` a mixed-script NAME (valid Python)
    — flagged, not written."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [CYRILLIC_DEF_PY_OUTPUT])

    with pytest.raises(CoddCLIError):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    ast.parse(CYRILLIC_DEF_PY_OUTPUT.split("```python\n", 1)[1].split("```", 1)[0])
    assert len(calls) == DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS
    assert not (project / "src" / "auth" / "service.py").exists()
    assert "U+0435" in calls[1]


def test_homoglyph_recovers_within_bounded_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model slips a homoglyph, then lands a clean ASCII file within the budget."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [CYRILLIC_ID_PY_OUTPUT, VALID_PY_OUTPUT])

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth"])
    )

    assert len(calls) == 2  # recovered on the corrective retry
    assert "U+0456" in calls[1]  # the retry carried the codepoint
    service = project / "src" / "auth" / "service.py"
    assert service in result.generated_files
    content = service.read_text(encoding="utf-8")
    assert content.isascii()  # the landed file is clean
    ast.parse(content)


# ---------------------------------------------------------------------------
# 6. The critical false-positive guard, end-to-end: literals/comments pass
# ---------------------------------------------------------------------------


def test_japanese_string_and_comment_pass_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose ONLY non-ASCII is Japanese UI copy in a string literal and a
    comment is written unchanged on the FIRST attempt — never flagged."""
    project = _project(tmp_path)
    calls = _patch_ai_sequence(monkeypatch, [JAPANESE_LITERAL_PY_OUTPUT])

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth"])
    )

    assert len(calls) == 1  # no retry — accepted immediately
    service = project / "src" / "auth" / "service.py"
    assert service in result.generated_files
    content = service.read_text(encoding="utf-8")
    assert "こんにちは" in content  # the Japanese string survived
    assert "日本語" in content  # the Japanese comment survived
    ast.parse(content)


# ---------------------------------------------------------------------------
# 7. Opt-out: implement.confusable_check: false restores pass-through
# ---------------------------------------------------------------------------


def test_confusable_check_opt_out_writes_homoglyph_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implement.confusable_check: false → the parses-fine homoglyph file is
    written (single attempt), proving the detector is what was rejecting it.
    The syntax gate stays ON and unaffected."""
    project = _project(
        tmp_path, implement_config={"syntax_gate": True, "confusable_check": False}
    )
    calls = _patch_ai_sequence(monkeypatch, [CYRILLIC_ID_PY_OUTPUT])

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth"])
    )

    assert len(calls) == 1  # no confusable retry when the check is off
    service = project / "src" / "auth" / "service.py"
    assert service in result.generated_files
    assert "іd" in service.read_text(encoding="utf-8")  # the homoglyph landed


def test_confusable_check_enabled_resolver() -> None:
    """Default ON; explicit false honored; anything else (incl. missing) ON."""
    assert _confusable_check_enabled({}) is True
    assert _confusable_check_enabled({"implement": {}}) is True
    assert _confusable_check_enabled({"implement": {"confusable_check": True}}) is True
    assert _confusable_check_enabled({"implement": {"confusable_check": False}}) is False


def test_syntax_gate_off_but_confusable_on_still_catches_homoglyph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two checks are independent: with the syntax gate OFF, the confusable
    check still rejects a homoglyph identifier (and drives the bounded retry)."""
    project = _project(
        tmp_path, implement_config={"syntax_gate": False, "confusable_check": True}
    )
    calls = _patch_ai_sequence(monkeypatch, [CYRILLIC_ID_PY_OUTPUT])

    with pytest.raises(CoddCLIError, match=r"src/auth/service\.py"):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(calls) == DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS  # retries still bounded
    assert not (project / "src" / "auth" / "service.py").exists()

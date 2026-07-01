"""F-greenfield-implement-0files-noretry — bounded retry on a TRANSIENT
no-usable-output implement attempt BEFORE the zero-files hard-fail gate.

A greenfield autopilot implement attempt that gets a transient
no-usable-output response (empty output, file-writing-agent "no file changes",
all blocks invalid/out-of-scope, or parsed-but-filtered-to-0) used to hard-fail
the whole multi-hour run with NO retry — even though re-issuing the identical
call succeeds (verified: a sibling run with byte-identical implement code passed
the same task; re-running the failing task twice both succeeded).

The fix routes those three zero-file conditions into a recoverable
``NoUsableGeneratedFiles`` and retries the full prompt->invoke->parse->filter
pass a bounded number of times, composing ADDITIVELY (not multiplicatively) with
the syntax-gate retry budget. The ANTI-FALSE-GREEN invariants are pinned here:
0 files is still failure by default (only explicit ``skip_generation: true`` is
a 0-file success), no scope/path relaxation on retry, the syntax/confusable
gates stay active on every retry, and after the budget is exhausted the SAME
``_zero_generated_files_error`` is raised as a no-retry world.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import CoddCLIError
from codd.implementer import (
    DEFAULT_NO_USABLE_FILE_RETRIES,
    ImplementSpec,
    Implementer,
    _no_usable_file_retries,
)

# ``codd.implementer`` is a thin compatibility package that loads
# ``codd/implementer.py`` under the name ``codd._implementer_legacy`` and copies
# its names into the package namespace. The Implementer methods' module-global
# lookups (e.g. bare ``_write_generated_files``) resolve against the LEGACY
# module's dict, so a ``_write_generated_files`` monkeypatch must target that
# module — not the package copy. Resolve it from where the method actually runs.
import sys as _sys

implementer_legacy = _sys.modules[
    Implementer._run_implementation_generation_attempt.__globals__["__name__"]
]


# ---------------------------------------------------------------------------
# helpers (same project/doc shape as tests/test_implement_syntax_gate.py)
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


def _project(
    tmp_path: Path,
    *,
    language: str = "python",
    implement_config: dict | None = None,
    design_body: str = "# Auth Design\n\nBuild an auth service.",
) -> Path:
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
    _write_doc(project, "docs/design/auth.md", node_id="design:auth", body=design_body)
    return project


def _impl(project: Path) -> Implementer:
    """Implementer with a no-op backoff so retries don't actually sleep."""
    return Implementer(project, sleep=lambda _seconds: None)


def _patch_invoke_sequence(
    monkeypatch: pytest.MonkeyPatch, steps: list[object]
) -> list[str]:
    """Scripted ``generator._invoke_ai_command``.

    Each entry in *steps* is either a ``str`` (returned as raw output) or an
    ``Exception`` instance (raised). The last step repeats if called again.
    Returns the list of prompts seen (one per call), so a test can assert the
    call COUNT and that retries reissue the SAME effective prompt.
    """
    prompts: list[str] = []

    def fake_invoke(ai_command, prompt, *, project_root=None, **kwargs):
        prompts.append(prompt)
        step = steps[min(len(prompts) - 1, len(steps) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(
        implementer_module.generator_module, "_invoke_ai_command", fake_invoke
    )
    return prompts


def _file_block(path: str, body: str = "def ok() -> int:\n    return 1\n") -> str:
    return f"=== FILE: {path} ===\n```python\n{body}```\n"


VALID_OUTPUT = _file_block("src/auth/service.py")
EMPTY_OUTPUT_ERROR = ValueError("AI command returned empty output")
NO_CHANGES_ERROR = ValueError("AI command did not produce any file changes")
NO_READABLE_CHANGES_ERROR = ValueError(
    "AI command did not produce any readable file changes"
)
# A file block whose ONLY path is outside spec.output_paths -> _parse_file_payloads
# raises "all were invalid" (path b). An absolute path additionally exercises the
# traversal/absolute rejection.
OUT_OF_SCOPE_OUTPUT = _file_block("/etc/passwd")
ANOTHER_OUT_OF_SCOPE_OUTPUT = _file_block("src/other/service.py")
# Syntactically invalid Python in an IN-scope path -> ImplementSyntaxGateError.
BROKEN_PY_OUTPUT = _file_block("src/auth/service.py", "def f(:\n    return 1\n")
# No `=== FILE: ... ===` header AND no complete code fence: indistinguishable
# from a garbled/truncated response (e.g. a duplicated tail fragment of a
# DIFFERENT file sliced mid-token — the 2026-06-30 java_v2 greenfield dogfood
# shape, where this exact content class was silently written to disk as a
# bogus repo-root `index.java`). Must retry, never guess it is one real file.
UNHEADERED_UNFENCED_GARBAGE_OUTPUT = (
    "in>\n                <groupId>org.jacoco</groupId>\n            </plugin>\n"
    "        </plugins>\n    </build>\n</project>\n"
)


# ---------------------------------------------------------------------------
# 1-4: each of the three zero-file paths (a)/(b)/(c) retries then succeeds
# ---------------------------------------------------------------------------


def test_transient_empty_then_success_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) empty AI output on attempt 1, valid FILE block on attempt 2."""
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(monkeypatch, [EMPTY_OUTPUT_ERROR, VALID_OUTPUT])

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 2  # retried exactly once
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    assert (project / "src" / "auth" / "service.py").exists()


def test_file_writing_no_changes_then_success_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) file-writing-agent 'no file changes' capture error retries."""
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(monkeypatch, [NO_CHANGES_ERROR, VALID_OUTPUT])

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 2
    assert result.generated_files == [project / "src" / "auth" / "service.py"]


def test_all_blocks_invalid_then_success_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) every block invalid/out-of-scope on attempt 1; valid on attempt 2.

    The invalid path must NEVER be written, on either attempt.
    """
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(monkeypatch, [OUT_OF_SCOPE_OUTPUT, VALID_OUTPUT])

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 2
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    assert not (project / "etc" / "passwd").exists()
    assert not Path("/etc/passwd_codd_should_never_write").exists()


def test_unheadered_unfenced_garbage_then_success_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) response has neither a FILE header nor a complete code fence.

    Regression for the 2026-06-30 java_v2 greenfield dogfood: a garbled
    attempt's raw output (a duplicated, mid-token-sliced tail fragment of a
    DIFFERENT file) had zero `=== FILE: ... ===` matches, and the old fallback
    silently accepted the WHOLE garbled string as "one implicit file",
    writing it to disk as a bogus `index.<ext>` complete with a traceability
    header — indistinguishable from genuine output. It must instead retry,
    and the bogus path must never be written on either attempt.
    """
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(
        monkeypatch, [UNHEADERED_UNFENCED_GARBAGE_OUTPUT, VALID_OUTPUT]
    )

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 2
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    assert not (project / "src" / "auth" / "index.py").exists()
    assert not list(project.glob("index.*"))


def test_filtered_to_zero_then_success_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) _write_generated_files returns [] once, then a valid path."""
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(monkeypatch, [VALID_OUTPUT, VALID_OUTPUT])

    real_write = implementer_legacy._write_generated_files
    write_calls = {"n": 0}

    def fake_write(**kwargs):
        write_calls["n"] += 1
        if write_calls["n"] == 1:
            return []  # parsed but filtered to zero usable files
        return real_write(**kwargs)

    monkeypatch.setattr(implementer_legacy, "_write_generated_files", fake_write)

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 2  # the filtered-to-0 pass was retried
    assert write_calls["n"] == 2
    assert result.generated_files == [project / "src" / "auth" / "service.py"]


# ---------------------------------------------------------------------------
# 5: budget exhausted -> the SAME zero-files hard-fail is preserved
# ---------------------------------------------------------------------------


def test_n_retries_still_zero_hard_fail_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt no-usable -> the SAME zero-generated-files CoddCLIError.

    Not a warning, not a success, not a fallback artifact. And the attempt count
    is bounded at default_retries + 1 (the initial attempt plus the budget).
    """
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(monkeypatch, [EMPTY_OUTPUT_ERROR])

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    # 1 initial + DEFAULT_NO_USABLE_FILE_RETRIES retries, then hard-fail.
    assert len(prompts) == DEFAULT_NO_USABLE_FILE_RETRIES + 1 == 4
    assert not (project / "src" / "auth" / "service.py").exists()


# ---------------------------------------------------------------------------
# 6: skip_generation stays the ONLY 0-file success (no retry attempted)
# ---------------------------------------------------------------------------


def test_skip_generation_still_zero_file_success_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skip_generation:true + empty AI -> accepted [], with NO no-usable retry."""
    project = _project(tmp_path, design_body="skip_generation: true\n")
    prompts = _patch_invoke_sequence(monkeypatch, [EMPTY_OUTPUT_ERROR])

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert result.generated_files == []
    assert len(prompts) == 1  # skip checked BEFORE treating empty as no-usable


# ---------------------------------------------------------------------------
# 7: a retry NEVER relaxes scope/path constraints
# ---------------------------------------------------------------------------


def test_retry_does_not_relax_scope_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out-of-scope file on attempt 1, in-scope on attempt 2.

    The out-of-scope file is absent, the in-scope file present, and the spec's
    output_paths are unchanged across retries (no broadening to coax a file).
    """
    project = _project(tmp_path)
    prompts = _patch_invoke_sequence(
        monkeypatch, [ANOTHER_OUT_OF_SCOPE_OUTPUT, VALID_OUTPUT]
    )
    spec = ImplementSpec("docs/design/auth.md", ["src/auth"])
    original_output_paths = list(spec.output_paths)

    result = _impl(project).run_implement(spec)

    assert len(prompts) == 2
    assert not (project / "src" / "other" / "service.py").exists()
    assert (project / "src" / "auth" / "service.py").exists()
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    # output_paths were never broadened to admit the out-of-scope path.
    assert list(spec.output_paths) == original_output_paths == ["src/auth"]


# ---------------------------------------------------------------------------
# 8: the two retry budgets compose ADDITIVELY (not multiplicatively)
# ---------------------------------------------------------------------------


def test_syntax_gate_budget_composes_additively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mix of NoUsableGeneratedFiles and ImplementSyntaxGateError stays bounded.

    With no_usable budget=2 and syntax budget=2, an interleaving that never
    succeeds must make AT MOST (no_usable_retries + 1) + (syntax_attempts - 1)
    calls — additive, never the 2x2 multiplicative product of nested loops.
    Exhausting the syntax gate raises _syntax_gate_exhausted_error.
    """
    project = _project(
        tmp_path,
        implement_config={
            "syntax_gate": True,
            "syntax_gate_max_attempts": 2,
            "no_usable_file_retries": 2,
        },
    )
    # empty (no-usable #1) -> empty (no-usable #2, budget=2 now spent on the
    # NEXT no-usable) -> broken py (syntax #1) -> broken py (syntax #2 -> exhausts
    # syntax budget=2). If budgets nested/multiplied, the count would balloon.
    prompts = _patch_invoke_sequence(
        monkeypatch,
        [EMPTY_OUTPUT_ERROR, EMPTY_OUTPUT_ERROR, BROKEN_PY_OUTPUT, BROKEN_PY_OUTPUT],
    )

    with pytest.raises(CoddCLIError, match="invalid file"):
        _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    # 2 no-usable attempts (initial + 1 retry within budget=2 ... here both empty
    # are consumed: attempt1 raises, retry#1 -> attempt2 raises, retry#2 used) +
    # 2 syntax attempts. Additive: 2 + 2 = 4. A multiplicative nesting would be
    # >= 2 * 2 = 4 only by accident; assert the EXACT additive count and that the
    # syntax gate (not the no-usable gate) decided the final error.
    assert len(prompts) == 4
    assert not (project / "src" / "auth" / "service.py").exists()


def test_no_usable_gate_decides_error_when_it_exhausts_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausting the no-usable budget raises _zero_generated_files_error.

    Companion to the additive-composition test: when the LAST exhausted gate is
    the no-usable one (a syntax rejection in between does not steal its budget),
    the final error is the zero-files error, and the syntax rejection consumed
    only syntax budget.
    """
    project = _project(
        tmp_path,
        implement_config={
            "syntax_gate": True,
            "syntax_gate_max_attempts": 3,
            "no_usable_file_retries": 1,
        },
    )
    # broken (syntax #1, syntax budget not yet exhausted) -> empty (no-usable
    # initial) -> empty (no-usable retry #1 -> exhausts no_usable budget=1).
    prompts = _patch_invoke_sequence(
        monkeypatch, [BROKEN_PY_OUTPUT, EMPTY_OUTPUT_ERROR, EMPTY_OUTPUT_ERROR]
    )

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    # 1 syntax attempt + 2 no-usable attempts = 3 (additive); the no-usable gate
    # exhausted LAST so it decided the error, and the syntax rejection did not
    # consume any no-usable budget.
    assert len(prompts) == 3


# ---------------------------------------------------------------------------
# 9: syntax/confusable gates still run AFTER a no-usable retry
# ---------------------------------------------------------------------------


def test_syntax_gate_still_runs_after_no_usable_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """empty -> syntactically-invalid Python -> valid Python.

    The no-usable retry must NOT bypass the syntax gate: the invalid 2nd attempt
    is rejected by the gate (not written), and the valid 3rd attempt succeeds.
    """
    project = _project(tmp_path)  # syntax gate ON by default
    prompts = _patch_invoke_sequence(
        monkeypatch, [EMPTY_OUTPUT_ERROR, BROKEN_PY_OUTPUT, VALID_OUTPUT]
    )

    result = _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 3
    # The invalid 2nd attempt was rejected by the syntax gate, not silently
    # written; only the valid 3rd attempt's file lands.
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    written = (project / "src" / "auth" / "service.py").read_text(encoding="utf-8")
    assert "def f(:" not in written  # the broken payload never reached disk
    assert "def ok()" in written


# ---------------------------------------------------------------------------
# config resolver
# ---------------------------------------------------------------------------


def test_no_usable_file_retries_resolver_defaults_and_clamps() -> None:
    """Default 3; junk / sub-0 fall back to default; 0 (opt out) and >=0 honored."""
    assert _no_usable_file_retries({}) == DEFAULT_NO_USABLE_FILE_RETRIES == 3
    assert _no_usable_file_retries({"implement": {"no_usable_file_retries": 5}}) == 5
    # 0 is a VALID value (opt out of no-usable retries) — distinct from junk.
    assert _no_usable_file_retries({"implement": {"no_usable_file_retries": 0}}) == 0
    assert _no_usable_file_retries({"implement": {"no_usable_file_retries": -2}}) == 3
    assert _no_usable_file_retries({"implement": {"no_usable_file_retries": "x"}}) == 3


def test_no_usable_file_retries_zero_means_immediate_hard_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """no_usable_file_retries:0 reproduces the legacy no-retry hard-fail."""
    project = _project(tmp_path, implement_config={"no_usable_file_retries": 0})
    prompts = _patch_invoke_sequence(monkeypatch, [EMPTY_OUTPUT_ERROR])

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        _impl(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert len(prompts) == 1  # zero retries: single attempt then hard-fail

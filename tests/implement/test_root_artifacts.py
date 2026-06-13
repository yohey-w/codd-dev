"""FX2 — repo-root infrastructure allowlist + shared canonical output root.

The 2026-06 real-AI greenfield dogfood produced a FRAGMENTED app: every
derived task defaulted to its own ``src/<task_id>/`` directory (15 disjoint
app copies) and the GitHub Actions workflow landed at
``src/<task>/.github/workflows/ci.yml`` — where CI never runs — turning the
final ``codd check`` ci_health gate red. These tests pin the two structural
fixes:

* ``implement.root_artifact_patterns`` — root-destined artifacts (CI
  workflows, manifests, Dockerfiles, ...) are written at the PROJECT ROOT,
  whether the AI emitted the repo-root path directly or nested it under an
  output path.
* ``_implement_output_paths_for_cli`` — tasks WITHOUT an explicit output-path
  mapping share ONE canonical source root (``implement.output_root`` >
  ``scan.source_dirs[0]`` > ``src``) instead of fragmenting into
  ``src/<task_slug>/``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import _implement_output_paths_for_cli
from codd.implementer import (
    DEFAULT_ROOT_ARTIFACT_PATTERNS,
    ImplementSpec,
    Implementer,
    _matches_root_artifact_pattern,
    _parse_file_payloads,
    _root_artifact_destination,
)


# ═══════════════════════════════════════════════════════════
# Test project scaffolding (self-contained)
# ═══════════════════════════════════════════════════════════

def _project(tmp_path: Path, *, implement_config: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    config: dict = {
        "project": {"name": "demo", "language": "python"},
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
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    doc = project / "docs" / "design" / "auth.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\ncodd:\n  node_id: design:auth\n  type: design\n---\n\n"
        "# Auth Design\n\nBuild an auth service.\n",
        encoding="utf-8",
    )
    return project


def _patch_ai(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[str]:
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


_CI_YAML = "name: ci\non:\n  push:\n  pull_request:\n"


def _block(path: str, body: str, fence: str = "yaml") -> str:
    return f"=== FILE: {path} ===\n```{fence}\n{body}```\n"


_SERVICE_BLOCK = _block("src/auth/service.py", "def service() -> bool:\n    return True\n", "python")


# ═══════════════════════════════════════════════════════════
# Pattern matching (deterministic, no LLM)
# ═══════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        "pyproject.toml",
        "package.json",
        "Dockerfile",
        "Dockerfile.prod",
        "docker-compose.yaml",
        ".gitignore",
        "README.md",
        "Makefile",
        "LICENSE",
    ],
)
def test_default_patterns_match_root_artifacts(path: str) -> None:
    assert _matches_root_artifact_pattern(PurePosixPath(path), DEFAULT_ROOT_ARTIFACT_PATTERNS)


@pytest.mark.parametrize(
    "path",
    [
        "src/service.py",            # ordinary source file
        "docs/README.md",            # README NOT at the root level
        "tools/pyproject.toml",      # manifest nested in another dir
        "githubby/workflows/ci.yml", # prefix must match a whole component
    ],
)
def test_default_patterns_reject_non_root_paths(path: str) -> None:
    assert not _matches_root_artifact_pattern(PurePosixPath(path), DEFAULT_ROOT_ARTIFACT_PATTERNS)


def test_root_destination_direct_and_prefix_stripped() -> None:
    prefixes = [PurePosixPath("src")]
    direct = _root_artifact_destination(
        PurePosixPath(".github/workflows/ci.yml"), prefixes, DEFAULT_ROOT_ARTIFACT_PATTERNS
    )
    assert direct == PurePosixPath(".github/workflows/ci.yml")
    nested = _root_artifact_destination(
        PurePosixPath("src/.github/workflows/ci.yml"), prefixes, DEFAULT_ROOT_ARTIFACT_PATTERNS
    )
    assert nested == PurePosixPath(".github/workflows/ci.yml")
    # deeper nesting is NOT rerooted: only the exact output-prefix tail counts
    assert (
        _root_artifact_destination(
            PurePosixPath("src/app/.github/workflows/ci.yml"), prefixes, DEFAULT_ROOT_ARTIFACT_PATTERNS
        )
        is None
    )
    # ordinary in-scope files are not root artifacts
    assert (
        _root_artifact_destination(PurePosixPath("src/service.py"), prefixes, DEFAULT_ROOT_ARTIFACT_PATTERNS)
        is None
    )


def test_empty_pattern_list_disables_the_allowlist() -> None:
    assert (
        _root_artifact_destination(PurePosixPath(".github/workflows/ci.yml"), [PurePosixPath("src")], [])
        is None
    )


# ═══════════════════════════════════════════════════════════
# _parse_file_payloads — confinement with the allowlist
# ═══════════════════════════════════════════════════════════

def test_parse_accepts_direct_root_artifact_and_reroots_nested() -> None:
    raw = (
        _block(".github/workflows/ci.yml", _CI_YAML)
        + _block("src/auth/.github/workflows/release.yml", _CI_YAML)
        + _SERVICE_BLOCK
    )
    payloads = _parse_file_payloads(
        raw, ["src/auth"], "python", root_artifact_patterns=DEFAULT_ROOT_ARTIFACT_PATTERNS
    )
    assert dict(payloads).keys() == {
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "src/auth/service.py",
    }


def test_parse_without_patterns_drops_root_paths_legacy_behavior() -> None:
    raw = _block(".github/workflows/ci.yml", _CI_YAML) + _SERVICE_BLOCK
    payloads = _parse_file_payloads(raw, ["src/auth"], "python")
    assert [path for path, _ in payloads] == ["src/auth/service.py"]


def test_parse_still_drops_out_of_scope_non_artifact_paths() -> None:
    raw = _block("src/other/service.py", "def bad():\n    return False\n", "python") + _SERVICE_BLOCK
    payloads = _parse_file_payloads(
        raw, ["src/auth"], "python", root_artifact_patterns=DEFAULT_ROOT_ARTIFACT_PATTERNS
    )
    assert [path for path, _ in payloads] == ["src/auth/service.py"]


# ═══════════════════════════════════════════════════════════
# Bare-basename rerooting under a single output dir
#
# The 2026-06-13 cross-CLI greenfield dogfood (codex as SUT): codex honoured the
# stdout file-contract and emitted well-formed ``=== FILE: ===`` blocks, but
# named the files from the design's logical modules (``module:task_model`` →
# ``task_model.py``) WITHOUT the configured ``src/`` source prefix. Every block
# was then skipped as "outside output paths" and the task hard-failed with the
# misleading "produced 0 generated files / add skip_generation" error — genuine
# output silently discarded. A bare basename under a single output dir is
# rerooted to where it belongs; deliberately different paths still drop.
# ═══════════════════════════════════════════════════════════

def test_parse_reroots_bare_basename_under_single_output_dir() -> None:
    """codex's bare-named files (no src/ prefix) are captured, not dropped."""
    raw = (
        _block("task_model.py", "class Task:\n    pass\n", "python")
        + _block("task_store.py", "def load():\n    return []\n", "python")
    )
    payloads = _parse_file_payloads(raw, ["src/"], "python")
    assert [path for path, _ in payloads] == ["src/task_model.py", "src/task_store.py"]


def test_parse_bare_basename_not_rerooted_with_multiple_output_dirs() -> None:
    """Ambiguous target (>1 output prefix) — a bare name stays dropped."""
    raw = _block("foo.py", "x = 1\n", "python")
    with pytest.raises(ValueError, match="outside output paths"):
        _parse_file_payloads(raw, ["src/", "lib/"], "python")


def test_parse_multicomponent_foreign_path_not_rerooted() -> None:
    """A deliberate sibling/foreign dir is NOT relocated; only bare names reroot."""
    raw = (
        _block("tests/test_x.py", "def test_x():\n    assert True\n", "python")
        + _block("main.py", "print('hi')\n", "python")
    )
    # tests/test_x.py is a deliberate directory choice -> dropped;
    # main.py is a bare name -> rerooted under the single src/ prefix.
    payloads = _parse_file_payloads(raw, ["src/"], "python")
    assert [path for path, _ in payloads] == ["src/main.py"]


# ═══════════════════════════════════════════════════════════
# End-to-end through Implementer.run_implement
# ═══════════════════════════════════════════════════════════

def test_run_implement_writes_ci_workflow_at_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dogfood bug: the CI workflow must land where CI actually runs."""
    project = _project(tmp_path)
    _patch_ai(monkeypatch, _block("src/auth/.github/workflows/ci.yml", _CI_YAML) + _SERVICE_BLOCK)

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    workflow = project / ".github" / "workflows" / "ci.yml"
    assert workflow in result.generated_files
    assert yaml.safe_load(workflow.read_text(encoding="utf-8"))  # valid YAML, no comment header
    assert not (project / "src" / "auth" / ".github").exists()
    assert (project / "src" / "auth" / "service.py").is_file()


def test_run_implement_project_pattern_override_replaces_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, implement_config={"root_artifact_patterns": ["tools/**"]})
    _patch_ai(
        monkeypatch,
        _block("tools/lint.cfg", "[lint]\n", "ini")
        + _block(".github/workflows/ci.yml", _CI_YAML)
        + _SERVICE_BLOCK,
    )

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    generated = {path.relative_to(project).as_posix() for path in result.generated_files}
    assert "tools/lint.cfg" in generated           # project pattern matched
    assert ".github/workflows/ci.yml" not in generated  # defaults replaced, not merged
    assert not (project / ".github").exists()


def test_run_implement_overwrites_its_own_root_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-runs converge: unmarkable formats keep normal overwrite semantics."""
    project = _project(tmp_path)
    workflow = project / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: old\non:\n  push:\n", encoding="utf-8")
    _patch_ai(monkeypatch, _block(".github/workflows/ci.yml", _CI_YAML) + _SERVICE_BLOCK)

    Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert "pull_request" in workflow.read_text(encoding="utf-8")


def test_run_implement_never_clobbers_user_authored_markable_root_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A marker-capable root file without @generated-by is user content: kept."""
    project = _project(tmp_path, implement_config={"root_artifact_patterns": ["setup.py"]})
    user_file = project / "setup.py"
    user_file.write_text("from setuptools import setup\n\nsetup(name='hand-written')\n", encoding="utf-8")
    _patch_ai(
        monkeypatch,
        _block("setup.py", "from setuptools import setup\n\nsetup(name='ai')\n", "python") + _SERVICE_BLOCK,
    )

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert "hand-written" in user_file.read_text(encoding="utf-8")
    assert user_file not in result.generated_files
    assert "kept existing root file setup.py" in capsys.readouterr().err


def test_run_implement_overwrites_marked_markable_root_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, implement_config={"root_artifact_patterns": ["setup.py"]})
    generated_before = project / "setup.py"
    generated_before.write_text(
        "# @generated-by: codd implement\n\nfrom setuptools import setup\n\nsetup(name='v1')\n",
        encoding="utf-8",
    )
    _patch_ai(
        monkeypatch,
        _block("setup.py", "from setuptools import setup\n\nsetup(name='v2')\n", "python") + _SERVICE_BLOCK,
    )

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert "v2" in generated_before.read_text(encoding="utf-8")
    assert generated_before in result.generated_files


# ═══════════════════════════════════════════════════════════
# Shared canonical output root (no more src/<task_slug>/)
# ═══════════════════════════════════════════════════════════

def test_unconfigured_task_falls_back_to_scan_source_root() -> None:
    config = {"scan": {"source_dirs": ["src/"]}}
    assert _implement_output_paths_for_cli(config, "implement_cli_add") == ["src/"]
    # every unconfigured task shares the SAME root — no per-task fragmentation
    assert _implement_output_paths_for_cli(config, "implement_cli_done") == ["src/"]


def test_explicit_output_root_wins_over_scan_source_dirs() -> None:
    config = {
        "implement": {"output_root": "app/"},
        "scan": {"source_dirs": ["src/"]},
    }
    assert _implement_output_paths_for_cli(config, "implement_cli_add") == ["app/"]


def test_configured_mapping_still_wins_over_shared_root() -> None:
    config = {
        "implement": {
            "output_root": "app/",
            "default_output_paths": {"docs/design/auth.md": ["src/auth"]},
        },
        "scan": {"source_dirs": ["src/"]},
    }
    assert _implement_output_paths_for_cli(config, "docs/design/auth.md") == ["src/auth"]
    # unmapped tasks still use the shared root
    assert _implement_output_paths_for_cli(config, "another_task") == ["app/"]


def test_bare_config_defaults_to_src() -> None:
    assert _implement_output_paths_for_cli({}, "implement_anything") == ["src"]


def test_defaults_yaml_documents_the_new_implement_keys() -> None:
    defaults = yaml.safe_load(
        (Path(__file__).parents[2] / "codd" / "defaults.yaml").read_text(encoding="utf-8")
    )
    section = defaults["implement"]
    assert section["output_root"] is None
    assert section["root_artifact_patterns"] == list(DEFAULT_ROOT_ARTIFACT_PATTERNS)


def test_implement_prompt_declares_the_root_artifact_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    calls = _patch_ai(monkeypatch, _SERVICE_BLOCK)

    Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    prompt = calls[0]
    assert "REPOSITORY ROOT" in prompt
    assert ".github/workflows/ci.yml" in prompt
    assert re.search(r"stay under one of the output paths", prompt)


# ═══════════════════════════════════════════════════════════
# Greenfield codex path: stdout file-contract + bare basenames
#
# Reproduces the 2026-06-13 cross-CLI greenfield dogfood failure end-to-end
# WITHOUT a live codex: a codex-style ``ai_command`` is routed through the
# file-writing-agent capture, codex "emits" well-formed ``=== FILE: ===`` blocks
# on STDOUT and writes NOTHING to disk, and names the files from the design's
# logical modules WITHOUT the configured ``src/`` prefix. The implement task
# must CAPTURE those files (the stdout-contract fallback) AND reroot the bare
# names under ``src/`` — not hard-fail with "produced 0 generated files".
# ═══════════════════════════════════════════════════════════

def test_run_implement_codex_stdout_contract_bare_basenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codd.ai_invoke as ai_invoke

    project = _project(tmp_path)
    # Configure codex (a file-writing agent) so the implement call routes through
    # invoke_file_writing_agent, exactly like the live greenfield run.
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    config["ai_command"] = "codex exec --model gpt-5.5 --skip-git-repo-check"
    (project / "codd" / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    # The file-writing-agent capture diffs the git tree; give it a real repo.
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)

    # codex honours the stdout file-contract: bare-named blocks (no src/), and
    # writes NOTHING to disk. Mirrors the captured raw output from the dogfood.
    codex_stdout = (
        _block("task_model.py", "class Task:\n    pass\n", "python")
        + _block("task_store.py", "def load():\n    return []\n", "python")
    )
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        # codex call: emit the stdout contract, write nothing to disk.
        if command and "codex" in str(command[0]):
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=codex_stdout, stderr=""
            )
        # everything else (git add/diff/ls-files/reset/checkout) is real.
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/"]))

    generated = {path.relative_to(project).as_posix() for path in result.generated_files}
    assert generated == {"src/task_model.py", "src/task_store.py"}
    assert (project / "src" / "task_model.py").is_file()
    assert (project / "src" / "task_store.py").is_file()
    # codex wrote nothing to disk; the bare names must NOT leak at the repo root.
    assert not (project / "task_model.py").exists()

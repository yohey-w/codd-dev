"""greenfield ci_scaffold stage — author an authentic CI workflow so a freshly
built system is CI-ready and the final ``check`` gate's ci_health requirement is
met honestly.

Surfaced by the C-Go greenfield dogfood: a clean Go build reached green on
verify + Phase-2 mutation probes (false-green escape = 0) but failed the final
``check`` stage on ``ci_workflow_missing`` — greenfield produced a system its own
check stage rejected. The fix (per GPT consult) is a DETERMINISTIC scaffold, not
AI-freeform (which could emit a hollow ``run: true`` that games ci_health) and
not an auto-opt-out (which would silently declare the system needs no CI).

The cardinal property under test: the generated CI runs the project's REAL test
command, so CI authenticity == verify authenticity by construction
(anti-false-green). That command is resolved in two tiers — see
``_default_ci_scaffold_runner``'s docstring: (1) ``detect_test_command``, the
legacy file-heuristic ladder verify's basic runner also uses; (2)
``_ci_scaffold_profile_test_command``, a fallback sourced from the resolved
language profile's ``commands.verify`` for stacks tier 1 has no file heuristic
for at all (Maven/pom.xml, and C#/C++'s equivalents — their real verify proof
already comes from the profile-owned coverage-execution-coherence campaign).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.dag.checks.ci_health import CiConfig, CiHealthCheck
from codd.greenfield.pipeline import (
    STAGES,
    STATUS_SKIPPED,
    GreenfieldPipeline,
    StageError,
    _ci_scaffold_profile_test_command,
    _default_ci_scaffold_runner,
)
from codd.test_detection import detect_test_command


def _go_project(root: Path) -> Path:
    (root / "go.mod").write_text("module cgo-itemapi\n\ngo 1.21\n", encoding="utf-8")
    return root


def _java_project(root: Path) -> Path:
    # No detect_test_command heuristic exists for Maven — pom.xml alone leaves
    # tier 1 empty; this fixture exercises the tier-2 profile fallback.
    (root / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n", encoding="utf-8"
    )
    return root


def _csharp_project(root: Path) -> Path:
    (root / "App.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8"
    )
    return root


def _cpp_project(root: Path) -> Path:
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(x)\n", encoding="utf-8"
    )
    return root


def _write_codd_yaml(root: Path, language: str) -> None:
    # A real greenfield project always has codd.yaml with project.language; the
    # v2.70 contract-driven ci_scaffold reads CI setup steps from that language's
    # profile (not a marker-file table).
    codd_dir = root / "codd"
    codd_dir.mkdir(exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        f"project:\n  name: x\n  language: {language}\n", encoding="utf-8"
    )


def _run_step_commands(workflow_path: Path) -> list[str]:
    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = payload["jobs"]["test"]["steps"]
    return [s["run"] for s in steps if isinstance(s, dict) and "run" in s]


# ── stage wiring ────────────────────────────────────────────


def test_ci_scaffold_runs_after_verify_before_check():
    # It must produce CI BEFORE the check gate inspects it, and after verify
    # (which proves the test command exists).
    assert "ci_scaffold" in STAGES
    assert STAGES.index("verify") < STAGES.index("ci_scaffold") < STAGES.index("check")


# ── authentic generation (anti-false-green core) ────────────


def test_go_project_scaffolds_ci_that_passes_ci_health(tmp_path):
    _go_project(tmp_path)
    detail = _default_ci_scaffold_runner(tmp_path)
    assert "generated" in detail

    result = CiHealthCheck().check(tmp_path, CiConfig())
    assert result.passed is True
    assert result.status == "pass"
    assert result.findings == []
    assert result.workflow_files == [".github/workflows/ci.yml"]


def test_generated_ci_runs_the_real_test_command(tmp_path):
    # CI authenticity == verify authenticity: the workflow's run step is exactly
    # what detect_test_command (verify's source) returns — not a hollow no-op.
    _go_project(tmp_path)
    _default_ci_scaffold_runner(tmp_path)
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    expected = detect_test_command(tmp_path)
    assert expected == "go test ./..."
    assert expected in runs
    # No gamed/hollow command slipped in.
    assert not any(r.strip() in {"true", "echo ok", ":"} for r in runs)


def test_generated_ci_has_required_triggers(tmp_path):
    _go_project(tmp_path)
    _default_ci_scaffold_runner(tmp_path)
    payload = yaml.safe_load((tmp_path / ".github/workflows/ci.yml").read_text())
    # PyYAML quotes the "on" key (YAML 1.1 bool), so it round-trips as a string.
    triggers = payload.get("on") or payload.get(True)
    assert set(triggers) == {"push", "pull_request"}


def test_go_project_gets_setup_go_from_profile(tmp_path):
    # Contract-driven: codd.yaml language=go → the go profile's ci.setup_steps
    # (actions/setup-go) are used. No marker-file table in the pipeline.
    _go_project(tmp_path)
    _write_codd_yaml(tmp_path, "go")
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-go" in text


def test_node_project_gets_setup_node_and_real_test(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name": "x", "scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    _write_codd_yaml(tmp_path, "typescript")  # setup steps come from the profile
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-node" in text
    assert "npm run test" in text  # detect_test_command's node mapping


def test_python_project_gets_setup_python_and_real_test(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_codd_yaml(tmp_path, "python")  # setup steps come from the profile
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-python" in text
    assert "pytest" in text


def test_unprofiled_project_gets_no_setup_steps_but_still_authentic(tmp_path):
    # No codd.yaml / no language profile → no toolchain bootstrap (honest
    # pluggable default), but the workflow still runs the real test command.
    _go_project(tmp_path)  # go.mod → detect_test_command works, but no codd.yaml
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-go" not in text  # no profile resolved → no setup
    assert "go test ./..." in text  # still authentic (real test command)


# ── tier-2 profile fallback (Maven/pom.xml gap — the Java greenfield dogfood) ──
#
# detect_test_command has NO heuristic for pom.xml, *.csproj/*.sln, or
# CMakeLists.txt (confirmed by reading its full body — only pytest/npm/Cargo/
# go.mod/bats/Makefile are covered). Before this fallback, a Java/C#/C++
# greenfield project reached ci_scaffold and hit an honest StageError even
# though verify itself had already run + passed the profile's real command
# (Java: `mvn -q verify`, via the coverage-execution-coherence campaign, NOT
# detect_test_command). These tests exercise the fallback that closes that gap
# by reading the SAME `commands.verify` the campaign runs.


def test_java_maven_project_scaffolds_ci_via_profile_fallback(tmp_path):
    # pom.xml alone is not a detect_test_command signal (rule 3-10 all miss);
    # codd.yaml declaring language=java is what lets the tier-2 fallback fire.
    _java_project(tmp_path)
    _write_codd_yaml(tmp_path, "java")
    detail = _default_ci_scaffold_runner(tmp_path)
    assert "generated" in detail
    assert "mvn -q verify" in detail

    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-java" in text  # profile.ci.setup_steps, unaffected by this fix
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    assert "mvn -q verify" in runs

    result = CiHealthCheck().check(tmp_path, CiConfig())
    assert result.passed is True


def test_csharp_project_scaffolds_ci_via_profile_fallback(tmp_path):
    # *.csproj alone is also not a detect_test_command signal; same fallback,
    # zero csharp-specific code — proves the fix generalizes to a sibling
    # language for free, as the language profile mechanism is generic.
    _csharp_project(tmp_path)
    _write_codd_yaml(tmp_path, "csharp")
    _default_ci_scaffold_runner(tmp_path)
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    # csharp.yaml's argv contains a `;`-laden logger arg — must be shell-quoted
    # (shlex.join), not naively space-joined, or the `;` would split the `run:`
    # step into two shell commands in the authored workflow.
    assert 'dotnet test --logger \'trx;LogFileName=test.trx\' --results-directory TestResults' in runs


def test_cpp_project_scaffolds_ci_via_profile_fallback(tmp_path):
    _cpp_project(tmp_path)
    _write_codd_yaml(tmp_path, "cpp")
    _default_ci_scaffold_runner(tmp_path)
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    assert "ctest --test-dir build --output-junit ctest-junit.xml" in runs


def test_profile_fallback_never_overrides_a_working_heuristic(tmp_path):
    # THE load-bearing safety property: the profile fallback is LOWEST
    # priority, never higher. Go's profile campaign command
    # (`go test -json ./...`) intentionally differs from detect_test_command's
    # legacy heuristic (`go test ./...` — see
    # tests/languages/test_verify_plan.py's documented shadow-mode divergence).
    # A project with BOTH a working heuristic AND a resolvable profile must
    # keep getting the heuristic's answer — switching an already-working,
    # already-tested stack's authored CI command out from under it would be an
    # unrequested, silent behavior change.
    _go_project(tmp_path)
    _write_codd_yaml(tmp_path, "go")
    _default_ci_scaffold_runner(tmp_path)
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    assert "go test ./..." in runs
    assert not any("go test -json" in r for r in runs)


def test_profile_fallback_declines_unresolved_placeholder_argv(tmp_path):
    # TypeScript's profile campaign argv references {test_root}/{report}
    # placeholders this bare fallback does not resolve (that needs the full
    # VerifyCampaignSpec machinery). No package.json/other tier-1 signal here,
    # so this must fail honestly rather than author a workflow with a literal,
    # broken "{test_root}" in its run step.
    _write_codd_yaml(tmp_path, "typescript")
    with pytest.raises(StageError):
        _default_ci_scaffold_runner(tmp_path)
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_ci_scaffold_now_honors_explicit_verify_test_command_config(tmp_path):
    # Adjacent bug fixed alongside the Maven gap: detect_test_command's call
    # inside _default_ci_scaffold_runner previously omitted `config=`, so the
    # error message's own documented remedy ("Declare verify.test_command in
    # codd.yaml") silently did not work. tmp_path has NO detectable signal at
    # all (no pom.xml, no profile) — only the explicit config can save it.
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        "verify:\n  test_command: custom-runner\n", encoding="utf-8"
    )
    detail = _default_ci_scaffold_runner(tmp_path)
    assert "generated" in detail
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    assert "custom-runner" in runs


class TestCiScaffoldProfileTestCommandUnit:
    """Direct unit coverage of the new helper's decline paths."""

    def test_no_codd_yaml_declines(self, tmp_path):
        assert _ci_scaffold_profile_test_command(tmp_path) is None

    def test_unknown_language_declines(self, tmp_path):
        _write_codd_yaml(tmp_path, "cobol-9000")
        assert _ci_scaffold_profile_test_command(tmp_path) is None

    def test_java_resolves_mvn_verify(self, tmp_path):
        _write_codd_yaml(tmp_path, "java")
        assert _ci_scaffold_profile_test_command(tmp_path) == "mvn -q verify"

    def test_placeholder_argv_declines(self, tmp_path):
        _write_codd_yaml(tmp_path, "typescript")
        assert _ci_scaffold_profile_test_command(tmp_path) is None


# ── honest refusals (no false-green, no silent opt-out) ─────


def test_undetectable_test_command_fails_honestly(tmp_path):
    # An unknown project has no authentic test command → refuse to fabricate CI.
    # Honest RED beats a hollow workflow that would falsely satisfy ci_health.
    with pytest.raises(StageError) as excinfo:
        _default_ci_scaffold_runner(tmp_path)
    assert "cannot determine the project's test command" in str(excinfo.value)
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


# ── idempotence / explicit opt-out ──────────────────────────


def test_existing_workflow_left_untouched(tmp_path):
    _go_project(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "release.yml"
    wf.parent.mkdir(parents=True)
    sentinel = "name: release\n'on':\n  push: null\n"
    wf.write_text(sentinel, encoding="utf-8")

    detail = _default_ci_scaffold_runner(tmp_path)
    assert detail.startswith("skipped")
    assert wf.read_text(encoding="utf-8") == sentinel  # untouched
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_opt_out_provider_none_skips_scaffold(tmp_path):
    _go_project(tmp_path)  # would otherwise generate
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text("ci:\n  provider: none\n", encoding="utf-8")

    detail = _default_ci_scaffold_runner(tmp_path)
    assert detail.startswith("skipped")
    assert "ci.provider=none" in detail
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


# ── stage-method status semantics ───────────────────────────


def test_stage_marks_skipped_when_workflow_present(tmp_path):
    _go_project(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("name: ci\n'on':\n  push: null\n", encoding="utf-8")

    pipeline = GreenfieldPipeline()
    record: dict = {"status": "pending", "detail": ""}
    pipeline._stage_ci_scaffold(tmp_path, record, {})
    assert record["status"] == STATUS_SKIPPED
    assert record["detail"].startswith("skipped")


def test_stage_uses_injected_runner(tmp_path):
    calls: list[Path] = []

    def fake(project_root: Path, *, ai_command=None) -> str:
        calls.append(project_root)
        return "generated fake ci"

    pipeline = GreenfieldPipeline(ci_scaffold_runner=fake)
    record: dict = {"status": "pending", "detail": ""}
    pipeline._stage_ci_scaffold(tmp_path, record, {})
    assert calls == [tmp_path]
    assert record["detail"] == "generated fake ci"
    assert record["status"] == "pending"  # not skipped → loop will mark done

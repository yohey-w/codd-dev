"""Contract Kernel Step 6 — the LIVE verify switch (anti-false-green proof).

``VerifyRunner._run_test_command`` is now CONTRACT-FIRST: it resolves the language
contract, builds the verify plan, gates on the runner_report adapter (scoped, not
full ``require_complete()``), and runs the anti-false-green executor — the SINGLE
live point where the Contract Kernel's verify enters production verify. These tests
prove, end-to-end through ``VerifyRunner.run()``:

* the contract path is taken for an adapter-complete strict project, and its verdict
  is FINAL (a contract not-green is a failure even when a legacy ``detect_test_command``
  for the same project WOULD pass — NO post-execution rescue);
* a pre-execution legacy fallback fires (and ``verify`` still works via the legacy
  path) for: no ``project.language``, and a ``legacy_compatible`` profile whose
  runner_report adapter is unregistered;
* a declared-unknown language is RED (a failure), never a fallback;
* seeded mutations (zero tests / nonexistent adapter id / wrong report path) each make
  the contract path RED;
* the contract-not-greener-than-legacy property holds.

Hermeticity: the tests inject a SYNTHETIC ``LanguageRegistry`` (a tmp profile YAML)
plus a fresh ``AdapterRegistry`` carrying a controllable FAKE runner_report adapter,
through ``VerifyRunner``'s keyword-only ``language_registry`` / ``adapter_registry``
seams, and drive the verify command with a tiny ``python -c`` script — so NO real go
toolchain / pytest run is needed and every branch is deterministic. The DAG/structural
checks are stubbed to ``[]`` so ``result.passed`` isolates the test-command verdict.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from codd.languages.adapters.runner_report import RunnerExecution
from codd.languages.registry import AdapterRegistry, LanguageRegistry
from codd.repair import verify_runner as verify_runner_module
from codd.repair.verify_runner import VerificationResult, VerifyRunner


# ── synthetic profiles (driven via a tmp LanguageRegistry) ─────────────────

# A STRICT language whose verify command is a python fixture that writes a report
# file; the fake adapter ("fake") decides what that report MEANS, so each outcome is
# under our control. report_required=True (declares report.path) ⇒ the adapter gate
# and the executor's report-missing/zero/fail branches all engage.
_STRICT_PROFILE = textwrap.dedent(
    """\
    id: ckswitchlang
    display_name: CKSwitchLang
    aliases: [cksw]
    file_extensions: [". cks"]
    strictness: strict
    layout:
      source_sets:
        - id: main
          root: "src"
      test_sets:
        - id: unit
          root: "tests"
      package_root:
        kind: none
    commands:
      verify:
        argv: ["PLACEHOLDER"]
        report:
          path: "report.json"
          format: "fake-json"
          adapter: "fake"
    tests:
      runner_report_adapter: "fake"
    """
)

# Same language but LEGACY_COMPATIBLE — used to prove a missing runner_report adapter
# degrades to the pre-execution legacy fallback (NOT a RED) for legacy_compatible.
_LEGACY_COMPATIBLE_PROFILE = textwrap.dedent(
    """\
    id: ckcompatlang
    display_name: CKCompatLang
    aliases: [ckcompat]
    file_extensions: [".ckc"]
    strictness: legacy_compatible
    layout:
      source_sets:
        - id: main
          root: "src"
      test_sets:
        - id: unit
          root: "tests"
      package_root:
        kind: none
    commands:
      verify:
        argv: ["PLACEHOLDER"]
        report:
          path: "report.json"
          format: "fake-json"
          adapter: "fake"
    tests:
      runner_report_adapter: "fake"
    """
)

# A profile with NO verify command at all → build_verify_plan is None → fallback
# (reason no_verify_plan). It still declares a runner_report adapter elsewhere.
_NO_VERIFY_PROFILE = textwrap.dedent(
    """\
    id: cknoverifylang
    display_name: CKNoVerifyLang
    file_extensions: [".ckn"]
    strictness: legacy_compatible
    layout:
      test_sets:
        - id: unit
          root: "tests"
      package_root:
        kind: none
    commands:
      build:
        argv: ["true"]
    tests:
      runner_report_adapter: "fake"
    """
)


# ── controllable fake runner_report adapter ────────────────────────────────


@dataclass
class _FakeAdapter:
    """Returns a chosen :class:`RunnerExecution` verbatim, or raises on parse.

    ``execution`` is returned from :meth:`parse`; if ``raises`` is set, :meth:`parse`
    raises it instead (REPORT_UNREADABLE branch).
    """

    execution: RunnerExecution | None = None
    raises: BaseException | None = None

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        if self.raises is not None:
            raise self.raises
        assert self.execution is not None
        return self.execution


# Crafted execution shapes (executed_files = passed | failed).
_CLEAN = RunnerExecution(
    executed_passed_files=frozenset({"tests/test_a.py", "tests/test_b.py"}),
    executed_failed_files=frozenset(),
    test_level_available=True,
    total_cases=2,
    passed_cases=2,
)
_ZERO = RunnerExecution(total_cases=0)
_FAILED = RunnerExecution(
    executed_passed_files=frozenset({"tests/test_a.py"}),
    executed_failed_files=frozenset({"tests/test_b.py"}),
    test_level_available=True,
    total_cases=2,
    passed_cases=1,
)


# A verify argv (python interpreter + -c + code) that writes the report and exits 0.
def _argv_write_report_exit0() -> tuple[str, ...]:
    return (sys.executable, "-c", "open('report.json','w').write('{}')")


def _argv_exit0_no_report() -> tuple[str, ...]:
    return (sys.executable, "-c", "import sys; sys.exit(0)")


def _argv_write_report_wrong_path() -> tuple[str, ...]:
    # Writes a report to the WRONG path → the required report.json stays absent.
    return (sys.executable, "-c", "open('elsewhere.json','w').write('{}')")


# ── harness wiring ─────────────────────────────────────────────────────────


def _stub_dag_green(monkeypatch) -> None:
    """Stub the structural DAG pipeline to produce ZERO failures.

    So ``VerificationResult.passed`` reflects ONLY the test-command verdict (the
    install preflight / typecheck are no-ops for a non-node declared language).
    """

    monkeypatch.setattr(verify_runner_module, "reset_dag_cache", lambda project_root: None)
    monkeypatch.setattr(
        verify_runner_module, "load_dag_settings", lambda project_root, settings: dict(settings or {})
    )
    monkeypatch.setattr(verify_runner_module, "build_dag", lambda project_root, settings: verify_runner_module.DAG())
    monkeypatch.setattr(
        verify_runner_module, "run_checks", lambda dag, project_root, settings, check_names=None: []
    )


def _write_project(tmp_path: Path, language: str | None, *, test_command: str | None = None) -> dict:
    """Write a minimal codd.yaml + return the settings dict used to drive the runner."""
    settings: dict = {"project": {"type": "generic"}}
    if language is not None:
        settings["project"]["language"] = language
    if test_command is not None:
        settings.setdefault("verify", {})["test_command"] = test_command
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir(exist_ok=True)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    return settings


def _lang_registry(tmp_path: Path, *profiles: str) -> LanguageRegistry:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    for i, body in enumerate(profiles):
        (profiles_dir / f"prof_{i}.yaml").write_text(body, encoding="utf-8")
    return LanguageRegistry(profiles_dir=profiles_dir)


def _adapter_registry(adapter: object | None, *, ident: str = "fake") -> AdapterRegistry:
    reg = AdapterRegistry()
    if adapter is not None:
        reg.register("runner_report", ident, adapter)
    return reg


def _run(
    monkeypatch,
    tmp_path: Path,
    *,
    language: str | None,
    profile: str | None,
    argv: tuple[str, ...] | None,
    adapter: object | None,
    adapter_ident: str = "fake",
    test_command: str | None = None,
    legacy_detects: str | None = None,
) -> VerificationResult:
    """Build + run a VerifyRunner with a synthetic profile/registry and injected argv.

    ``argv`` is injected into the profile's verify command via the LanguageRegistry by
    monkeypatching ``build_verify_plan`` (a thin shim) — keeps the YAML stable while the
    fixture command varies. ``profile`` None ⇒ no synthetic profile (use bundled).

    ``test_command`` writes an EXPLICIT ``verify.test_command`` into codd.yaml (author
    intent → the explicit-command override branch). ``legacy_detects`` instead patches
    the legacy ladder's ``detect_test_command`` to return that command DETERMINISTICALLY
    (a hermetic stand-in for on-disk detection) — used to prove a fallback path actually
    RUNS a detected command end-to-end WITHOUT tripping the explicit-command override.
    """
    _stub_dag_green(monkeypatch)
    settings = _write_project(tmp_path, language, test_command=test_command)
    lang_registry = _lang_registry(tmp_path, profile) if profile is not None else None
    adapter_registry = _adapter_registry(adapter, ident=adapter_ident)

    if legacy_detects is not None:
        # Patch the legacy resolver the fallback path calls (imported INTO
        # verify_runner_module) so detection is deterministic and tool-free.
        monkeypatch.setattr(
            verify_runner_module, "detect_test_command", lambda root, config=None: legacy_detects
        )

    if argv is not None:
        # Inject the fixture argv into the built plan (the YAML carries PLACEHOLDER).
        import dataclasses

        from codd.languages import verify_plan as _vp_mod

        real_build = _vp_mod.build_verify_plan

        def _patched_build(contract):
            plan = real_build(contract)
            if plan is None:
                return None
            return dataclasses.replace(plan, argv=argv)

        monkeypatch.setattr(verify_runner_module, "build_verify_plan", _patched_build, raising=False)
        # verify_runner imports build_verify_plan lazily from codd.languages.verify_plan;
        # patch THERE so the lazy import inside _run_test_command picks it up.
        monkeypatch.setattr(_vp_mod, "build_verify_plan", _patched_build)

    runner = VerifyRunner(
        tmp_path,
        settings,
        language_registry=lang_registry,
        adapter_registry=adapter_registry,
    )
    return runner.run()


# ── 1. contract path: adapter-complete strict project, clean report → PASS ──


def test_strict_clean_report_takes_contract_path_and_passes(monkeypatch, tmp_path):
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_CLEAN),
    )
    assert res.passed is True
    assert res.verify_path == "contract"
    assert res.fallback_used is False
    assert res.fallback_reason is None
    assert res.tests_executed is True


# ── 2. contract path: zero / failed / missing report → not-green ────────────


def test_strict_zero_tests_is_not_green(monkeypatch, tmp_path):
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_ZERO),
    )
    assert res.passed is False
    assert res.verify_path == "contract"
    assert res.fallback_used is False
    failure = _test_command_failure(res)
    assert failure.details.get("verify_class") == "ZERO_TESTS"


def test_strict_failed_report_is_not_green(monkeypatch, tmp_path):
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_FAILED),
    )
    assert res.passed is False
    assert _test_command_failure(res).details.get("verify_class") == "FAIL"


def test_strict_missing_report_is_not_green(monkeypatch, tmp_path):
    # Command exits 0 but writes NO report → required report absent → REPORT_MISSING.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_exit0_no_report(),
        adapter=_FakeAdapter(_CLEAN),
    )
    assert res.passed is False
    assert res.verify_path == "contract"
    assert _test_command_failure(res).details.get("verify_class") == "REPORT_MISSING"


# ── 3. EXPLICIT author command override pre-empts the contract path ─────────


def test_explicit_test_command_pre_empts_contract(monkeypatch, tmp_path):
    # An adapter-COMPLETE strict project (would otherwise take the contract path) that
    # ALSO sets an explicit verify.test_command → the explicit author command WINS:
    # pre-execution legacy fallback (reason explicit_test_command), running the author's
    # command (a passing python -c), NOT the profile's verify pipeline.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=None,  # the contract plan is never executed (override fired first)
        adapter=_FakeAdapter(_CLEAN),  # adapter present — contract WOULD be available
        test_command=f"{sys.executable} -c \"print('author command ran')\"",
    )
    assert res.verify_path == "legacy_fallback"
    assert res.fallback_used is True
    assert res.fallback_reason == "explicit_test_command"
    assert res.tests_executed is True  # the author's command actually ran
    assert res.passed is True


def test_explicit_command_override_is_language_agnostic_for_fix_test_command(monkeypatch, tmp_path):
    # The override also fires for fix.test_command (test_detection priority 1), proving
    # it keys on "author provided a command", not on a language or the verify section.
    _stub_dag_green(monkeypatch)
    settings: dict = {
        "project": {"type": "generic", "language": "ckswitchlang"},
        "fix": {"test_command": f"{sys.executable} -c \"print('fix cmd ran')\""},
    }
    (tmp_path / "codd").mkdir(exist_ok=True)
    (tmp_path / "codd" / "codd.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    runner = VerifyRunner(
        tmp_path,
        settings,
        language_registry=_lang_registry(tmp_path, _STRICT_PROFILE),
        adapter_registry=_adapter_registry(_FakeAdapter(_CLEAN)),
    )
    res = runner.run()
    assert res.fallback_reason == "explicit_test_command"
    assert res.tests_executed is True
    assert res.passed is True


def test_structural_only_opt_out_pre_empts_contract(monkeypatch, tmp_path):
    # An adapter-complete strict project that declares verify.allow_structural_only:true
    # (no explicit command, no detected command) → pre-execution fallback
    # (reason structural_only); the contract executor is NOT run, and the structural-only
    # result is green (the author opted out of executable verification).
    _stub_dag_green(monkeypatch)
    settings: dict = {
        "project": {"type": "generic", "language": "ckswitchlang"},
        "verify": {"allow_structural_only": True},
    }
    (tmp_path / "codd").mkdir(exist_ok=True)
    (tmp_path / "codd" / "codd.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    runner = VerifyRunner(
        tmp_path,
        settings,
        language_registry=_lang_registry(tmp_path, _STRICT_PROFILE),
        adapter_registry=_adapter_registry(_FakeAdapter(_CLEAN)),
    )
    res = runner.run()
    assert res.verify_path == "legacy_fallback"
    assert res.fallback_used is True
    assert res.fallback_reason == "structural_only"
    assert res.tests_executed is False  # nothing detected; structural-only
    assert res.passed is True  # honesty rule satisfied by the explicit opt-out


# ── 4. legacy fallback: no project.language / no verify plan / missing adapter ─


def test_legacy_compatible_missing_adapter_falls_back_and_verifies(monkeypatch, tmp_path):
    # legacy_compatible profile, runner_report adapter NOT registered, and NO explicit
    # command → PRE-execution fallback (missing_adapter_legacy_compatible). The legacy
    # ladder then runs a DETECTED command (deterministic stand-in), so verify still
    # WORKS via the legacy path.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckcompatlang",
        profile=_LEGACY_COMPATIBLE_PROFILE,
        argv=None,
        adapter=None,  # adapter unavailable → triggers the fallback
        legacy_detects=f"{sys.executable} -c \"print('legacy detected ok')\"",
    )
    assert res.verify_path == "legacy_fallback"
    assert res.fallback_used is True
    assert res.fallback_reason == "missing_adapter_legacy_compatible"
    assert res.tests_executed is True  # the detected legacy command actually ran
    assert res.passed is True  # and passed (legacy path works)


def test_no_language_falls_back(monkeypatch, tmp_path):
    res = _run(
        monkeypatch,
        tmp_path,
        language=None,  # no project.language declared
        profile=None,
        argv=None,
        adapter=None,
        legacy_detects=f"{sys.executable} -c \"print('legacy detected ok')\"",
    )
    assert res.verify_path == "legacy_fallback"
    assert res.fallback_used is True
    assert res.fallback_reason == "no_language"
    assert res.passed is True


def test_no_verify_plan_falls_back(monkeypatch, tmp_path):
    # A declared language whose profile has NO verify command → reason no_verify_plan.
    res = _run(
        monkeypatch,
        tmp_path,
        language="cknoverifylang",
        profile=_NO_VERIFY_PROFILE,
        argv=None,
        adapter=None,
        legacy_detects=f"{sys.executable} -c \"print('legacy detected ok')\"",
    )
    assert res.verify_path == "legacy_fallback"
    assert res.fallback_used is True
    assert res.fallback_reason == "no_verify_plan"
    assert res.passed is True


# ── 5. declared-unknown language → RED, NOT a fallback ──────────────────────


def test_declared_unknown_language_is_red_not_fallback(monkeypatch, tmp_path):
    # project.language names a language with NO profile in the (empty) synthetic
    # registry → UnknownLanguageError → RED. NEVER a fallback.
    empty_registry = LanguageRegistry(profiles_dir=tmp_path / "empty_profiles")
    (tmp_path / "empty_profiles").mkdir()
    _stub_dag_green(monkeypatch)
    settings = _write_project(tmp_path, "nonexistent-language-xyz")
    res = VerifyRunner(
        tmp_path,
        settings,
        language_registry=empty_registry,
        adapter_registry=_adapter_registry(_FakeAdapter(_CLEAN)),
    ).run()
    assert res.passed is False
    assert res.verify_path == "contract"  # a contract resolution was attempted
    assert res.fallback_used is False  # NOT a fallback
    failure = _test_command_failure(res)
    assert failure.details.get("failure_class") == "unknown_declared_language"


# ── 6. fallback-rescue-forbidden (CRITICAL) ────────────────────────────────


def test_contract_failure_is_final_no_legacy_rescue(monkeypatch, tmp_path):
    # A STRICT project (NO explicit author command, so it takes the contract path)
    # whose contract executor returns not-green (verify command writes NO report →
    # REPORT_MISSING). The legacy ladder's detect_test_command WOULD return a PASSING
    # command (patched) — but the contract path NEVER consults the legacy ladder after
    # running. Prove the contract verdict is FINAL: a FAILURE, NO post-execution rescue.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_exit0_no_report(),  # contract → REPORT_MISSING
        adapter=_FakeAdapter(_CLEAN),
        # legacy WOULD pass — but the contract path must ignore it (no rescue).
        legacy_detects=f"{sys.executable} -c \"print('legacy WOULD pass')\"",
    )
    assert res.passed is False  # contract not-green wins
    assert res.verify_path == "contract"  # never flipped to a fallback
    assert res.fallback_used is False
    assert _test_command_failure(res).details.get("verify_class") == "REPORT_MISSING"


# ── 7. seeded mutations (each makes the contract path RED) ──────────────────


def test_seeded_mutation_zero_tests_report(monkeypatch, tmp_path):
    # (i) verify command runs but the report observes zero tests → ZERO_TESTS.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_ZERO),
    )
    assert res.passed is False
    assert _test_command_failure(res).details.get("verify_class") == "ZERO_TESTS"


def test_seeded_mutation_nonexistent_adapter_id_strict_is_red(monkeypatch, tmp_path):
    # (ii) plan.report_adapter points at an id that is NOT registered (strict) → RED,
    # no fallback. We register the fake under a DIFFERENT id than the profile names.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_CLEAN),
        adapter_ident="some-other-id",  # profile names "fake" → unresolved
    )
    assert res.passed is False
    assert res.verify_path == "contract"
    assert res.fallback_used is False
    assert (
        _test_command_failure(res).details.get("failure_class")
        == "missing_runner_report_adapter"
    )


def test_seeded_mutation_wrong_report_path_is_report_missing(monkeypatch, tmp_path):
    # (iii) verify command writes the report to the WRONG path → required report.json
    # absent → REPORT_MISSING.
    res = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_wrong_path(),
        adapter=_FakeAdapter(_CLEAN),
    )
    assert res.passed is False
    assert _test_command_failure(res).details.get("verify_class") == "REPORT_MISSING"


# ── 8. contract-not-greener-than-legacy property ───────────────────────────


def test_contract_not_greener_than_legacy(monkeypatch, tmp_path):
    # For the SAME fixture project (a strict language, NO explicit command, contract
    # path) whose contract executor returns REPORT_MISSING, the legacy ladder WOULD be
    # PASS (its detect_test_command returns a passing command — patched), yet the
    # contract path is NOT-PASS — i.e. whenever legacy is PASS the contract may be
    # stricter (RED) but NEVER laxer. Conversely a clean contract run is PASS.
    not_green = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_exit0_no_report(),
        adapter=_FakeAdapter(_CLEAN),
        legacy_detects=f"{sys.executable} -c \"print('legacy pass')\"",
    )
    assert not_green.passed is False  # contract stricter than the (passing) legacy
    assert not_green.verify_path == "contract"  # legacy never rescued it

    green = _run(
        monkeypatch,
        tmp_path,
        language="ckswitchlang",
        profile=_STRICT_PROFILE,
        argv=_argv_write_report_exit0(),
        adapter=_FakeAdapter(_CLEAN),
    )
    assert green.passed is True  # a genuinely clean contract run is PASS


# ── helpers ─────────────────────────────────────────────────────────────────


def _test_command_failure(res: VerificationResult):
    """Return the single ``test_command`` VerificationFailure on the result."""
    matches = [f for f in res.failures if f.check_name == "test_command"]
    assert matches, f"expected a test_command failure, got {[f.check_name for f in res.failures]}"
    assert len(matches) == 1
    return matches[0]

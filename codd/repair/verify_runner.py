"""Python import based verification runner for repair attempts."""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import subprocess
import time
import warnings

import yaml

from codd.config import find_codd_dir, load_project_config
from codd.dag import DAG, reset_dag_cache
from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.runner import run_checks
from codd.deployment.providers import VERIFICATION_TEMPLATES
from codd.discovery import iter_source_files, scan_exclude_patterns
from codd.project_types import node_install_command
from codd.repair.schema import VerificationFailureReport
from codd.repair.test_failure_attribution import attribute_command_failure
from codd.test_detection import detect_test_command


DEFAULT_CHECKS: tuple[str, ...] = (
    "node_completeness",
    "edge_validity",
    "depends_on_consistency",
    "task_completion",
    "transitive_closure",
    "deployment_completeness",
    "user_journey_coherence",
    "environment_coverage",
)

#: FX3 execution-evidence constants.
#:
#: Source-integrity parse checks are bounded by FILE COUNT (never by language
#: subsetting): a pathological monorepo must not turn a fast deterministic
#: gate into a multi-minute scan.
SOURCE_INTEGRITY_MAX_FILES = 2000

#: Formats with a stdlib parser. Everything else (TypeScript, Go, ...) is
#: skipped on purpose — there is no in-process parser to key on, and shipping
#: per-language toolchains would violate the language-neutrality rule. Their
#: backstop is the executed test/typecheck command.
SOURCE_INTEGRITY_EXTENSIONS: tuple[str, ...] = (".py", ".json", ".yaml", ".yml", ".toml")

#: Bounded wall-clock budget for the detected/configured test + typecheck
#: commands (``verify.test_timeout_seconds``). Mirrors the existing
#: ``verify.verification_timeout`` convention used by verification-test nodes.
DEFAULT_TEST_TIMEOUT_SECONDS = 600.0

#: The honesty rule: a verification that verified nothing must say so.
STRUCTURAL_ONLY_WARNING = (
    "verification executed no tests/typecheck/runtime checks — structural DAG checks only. "
    "This proves document/graph coherence, NOT that the code works. "
    "Set verify.test_command (or add a detectable test setup: pytest config, package.json "
    "test script, Cargo.toml, go.mod, *.bats, Makefile test target), or set "
    "verify.allow_structural_only: true to accept structural-only verification."
)


def _go_aware_env(project_root: Path) -> dict[str, str] | None:
    """Env for running a Go project's verify commands, or ``None`` for non-Go.

    A greenfield Go output dir is frequently NOT a clean git repo (or sits under
    one with dubious ownership), so ``go build`` of a binary — including the
    ``go build`` that SUT-generated tests run as a subprocess during ``go test`` —
    fails with "error obtaining VCS status: exit status 128 / use -buildvcs=false".
    Setting ``-buildvcs=false`` (additive to ``-mod=readonly``, preserving any
    ambient GOFLAGS) makes the generated project build regardless of the harness
    dir's VCS state. Returning ``None`` for non-Go lets the caller inherit the
    ambient env unchanged.
    """
    if not (project_root / "go.mod").exists():
        return None
    env = dict(os.environ)
    flags = env.get("GOFLAGS", "").split()
    for needed in ("-mod=readonly", "-buildvcs=false"):
        if needed not in flags:
            flags.append(needed)
    env["GOFLAGS"] = " ".join(flags).strip()
    return env


@dataclass
class VerificationFailure:
    check_name: str
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    passed: bool
    failures: list[VerificationFailure] = field(default_factory=list)
    check_results: list[Any] = field(default_factory=list)
    runtime_results: list[Any] = field(default_factory=list)
    failure: VerificationFailureReport | None = None
    warnings: list[str] = field(default_factory=list)
    # ── FX3 execution evidence (additive; defaults keep old constructors valid) ──
    #: True when a test command actually ran to a captured exit status.
    tests_executed: bool = False
    #: The resolved test command (explicit config or detect_test_command), if any.
    test_command: str | None = None
    #: One-line human summary of the test run ("no test command detected",
    #: "1 passed in 0.03s", "failed (exit 1)", ...).
    tests_summary: str = ""
    #: True when a configured typecheck command actually ran.
    typecheck_executed: bool = False
    #: One-line status of the deterministic parse check over project sources
    #: ("checked N file(s)", "disabled", "N parse error(s)", "not checked").
    source_integrity: str = "not checked"

    @property
    def executed_anything(self) -> bool:
        """Did this verification EXECUTE anything (vs. only structural checks)?

        The dogfood false-green: structural DAG checks all passed on a project
        of 10 syntactically broken files because no test/typecheck/runtime
        command ever ran and "nothing was executed" silently counted as PASS.
        Callers gate honesty on this property.
        """
        return bool(
            self.tests_executed
            or self.typecheck_executed
            or _any_runtime_executed(self.runtime_results)
        )


@dataclass
class _RuntimeVerificationState:
    identifier: str
    target: str
    project_root: Path
    source: str | None = None
    actual_check_command: str | None = None
    journey: dict[str, Any] | None = None
    steps: list[Any] = field(default_factory=list)
    cdp_browser_config: dict[str, Any] | None = None


class VerifyRunner:
    """Run CoDD verification inside the current Python process."""

    def __init__(
        self,
        project_root: Path,
        codd_yaml: Mapping[str, Any] | None,
        runtime_skip: tuple[str, ...] | list[str] | set[str] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.codd_yaml = dict(codd_yaml or {})
        self.runtime_skip = frozenset(str(item) for item in (runtime_skip or ()))

    def run(self) -> VerificationResult:
        """Reset DAG state, run C1-C7 checks, then run executable verification tests.

        FX3: in addition to the structural DAG checks and verification-test
        nodes, the runner now produces EXECUTION EVIDENCE — it parse-checks
        the project sources (deterministic, no LLM), runs the configured
        typecheck command, and runs the explicit/detected test command. The
        result reports exactly what was executed so a "passed" can never
        again silently mean "nothing ran".
        """

        self.reset_dag_cache()
        if not self._has_codd_yaml():
            return self._error_result("codd_config", f"codd.yaml not found in {self.project_root}")

        try:
            settings = self._load_settings()
            dag_settings = load_dag_settings(self.project_root, settings)
            dag = build_dag(self.project_root, dag_settings)
            check_results = run_checks(dag, self.project_root, dag_settings, check_names=DEFAULT_CHECKS)
            failures = [
                failure
                for result in check_results
                for failure in [self._failure_from_check_result(result)]
                if failure is not None
            ]
            runtime_results = self._run_verification_tests(dag, settings)
            failures.extend(
                failure
                for result in runtime_results
                for failure in [self._failure_from_runtime_result(result)]
                if failure is not None
            )
            # FX3 execution evidence — order: cheap deterministic parse check
            # first (names broken files even when the suite cannot start),
            # then the BLOCKING dependency-install preflight (node stacks),
            # then the configured typecheck, then the test command.
            source_integrity, integrity_failures = self._source_integrity_failures(settings)
            failures.extend(integrity_failures)
            typecheck_executed = False
            tests_executed = False
            test_command = None
            tests_summary = ""
            # #1 — BLOCKING dependency-install preflight. A node/TS build whose
            # deps are not installed cannot be typechecked or tested; an install
            # failure is an honest ``environment_build_error`` (NOT a code-repair
            # target), so we record it and SKIP typecheck/tests (running them
            # would only produce misleading "module not found" noise the engine
            # might thrash on). Non-node stacks: no-op.
            install_failure = self._run_install_preflight(settings)
            if install_failure is not None:
                failures.append(install_failure)
                tests_summary = "skipped (dependency install failed)"
            else:
                typecheck_executed, typecheck_failure = self._run_typecheck_command(settings)
                if typecheck_failure is not None:
                    failures.append(typecheck_failure)
                tests_executed, test_command, tests_summary, test_failure = self._run_test_command(settings)
                if test_failure is not None:
                    failures.append(test_failure)
        except Exception as exc:  # noqa: BLE001 - verification must fail gracefully for repair loop.
            if _is_missing_expected_proof_break(exc):
                return self._warning_result("expected proof break missing; skipped proof-break verification")
            return self._error_result("verification_error", str(exc))

        result = VerificationResult(
            passed=not failures,
            failures=failures,
            check_results=check_results,
            runtime_results=runtime_results,
            failure=self._repair_failure_report(failures, dag),
            tests_executed=tests_executed,
            test_command=test_command,
            tests_summary=tests_summary,
            typecheck_executed=typecheck_executed,
            source_integrity=source_integrity,
        )
        # The honesty rule. Plain `codd verify` stays pass-WITH-WARNING by
        # default because existing brownfield/CI configurations may be
        # deliberately structural-only (doc-coherence gates with the test
        # suite run elsewhere in the pipeline); hard-failing them would break
        # every such pipeline overnight. The greenfield autopilot, by
        # contrast, is certifying a build IT produced with no human in the
        # loop — there it escalates to a stage FAILURE (see
        # greenfield/pipeline.py _default_verify_runner).
        if result.passed and not result.executed_anything and not structural_only_allowed(settings):
            result.warnings.append(STRUCTURAL_ONLY_WARNING)
        return result

    def reset_dag_cache(self) -> None:
        """Clear DAG cache state before rebuilding."""

        reset_dag_cache(self.project_root)

    def _has_codd_yaml(self) -> bool:
        return bool(self.codd_yaml) or find_codd_dir(self.project_root) is not None

    def _load_settings(self) -> dict[str, Any]:
        if self.codd_yaml:
            return dict(self.codd_yaml)
        return load_project_config(self.project_root)

    def _run_verification_tests(self, dag: DAG, settings: dict[str, Any]) -> list[dict[str, Any]]:
        import codd.deployment.providers.verification  # noqa: F401

        results: list[dict[str, Any]] = []
        template_settings = _verification_template_settings(settings)
        per_node_seconds = _verification_per_node_seconds(settings)
        total_seconds = _verification_total_seconds(settings)
        start = time.monotonic()
        for node in sorted(dag.nodes.values(), key=lambda item: item.id):
            if node.kind != "verification_test":
                continue
            if "verification-test" in self.runtime_skip:
                results.append(_skipped_result(node.id, "verification-test"))
                continue
            if total_seconds is not None and time.monotonic() - start > total_seconds:
                results.append(_skipped_result(node.id, "total_timeout_exceeded"))
                continue
            template_ref = str(node.attributes.get("template_ref") or "").strip()
            if not template_ref:
                results.append(_runtime_result(node.id, "", False, "verification template ref is missing"))
                continue
            template_cls = VERIFICATION_TEMPLATES.get(template_ref)
            if template_cls is None:
                results.append(_runtime_result(node.id, template_ref, False, "verification template is not registered"))
                continue

            toolchain_error = _runtime_toolchain_failure(template_ref, self.project_root)
            if toolchain_error:
                results.append(_runtime_result(node.id, template_ref, False, toolchain_error))
                continue

            template_config = template_settings.get(template_ref, {})
            try:
                template = _new_template(template_cls, template_config, per_node_seconds=per_node_seconds)
                state = _runtime_state(node, self.project_root, template_config)
                test_kind = str(node.attributes.get("kind") or "")
                command = template.generate_test_command(state, test_kind)
                # Run the verification command rooted at the PROJECT, not the
                # orchestrator's cwd. A node/TS runner (vitest) keys config and
                # test collection off the process working directory; executing it
                # from the CoDD install tree loads the wrong vitest.config.ts and
                # collects 0 tests (an opaque anti-false-green hard fail). The
                # ``cwd`` threads through every template's ``execute`` so this is
                # correct for vitest/playwright/curl alike. (The pytest evidence
                # path already passes ``cwd`` in ``_run_evidence_command``.)
                result = template.execute(command, cwd=self.project_root)
                passed = bool(getattr(result, "passed", False))
                output = _runtime_output(node.id, passed, getattr(result, "output", "") or "")
                results.append(
                    {
                        "check_name": "verification_test_runtime",
                        "node_id": node.id,
                        "template_ref": template_ref,
                        "command": command,
                        "passed": passed,
                        "skipped": False,
                        "output": output,
                        "duration": getattr(result, "duration", 0.0),
                    }
                )
            except subprocess.TimeoutExpired as exc:
                results.append(_runtime_result(node.id, template_ref, False, _timeout_output(node.id, exc)))
            except Exception as exc:  # noqa: BLE001 - one runtime test failure should not abort all checks.
                results.append(_runtime_result(node.id, template_ref, False, _failure_output(node.id, str(exc))))
        return results

    # ── FX3 execution evidence ──────────────────────────────

    def _source_integrity_failures(self, settings: dict[str, Any]) -> tuple[str, list[VerificationFailure]]:
        """Deterministic parse check of project sources (no LLM, no subprocess).

        Walks ``scan.source_dirs`` through the shared discovery layer and
        parses every file with a stdlib-checkable format (Python/JSON/YAML/
        TOML). One failure per broken file, each naming the file in
        ``details.failed_nodes`` so the repair loop maps it straight to the
        implementation file. This alone would have caught the dogfood
        disaster (10 broken .py files, verify green).
        """
        if _verify_setting(settings, "source_integrity", True) is False:
            return "disabled", []
        failures: list[VerificationFailure] = []
        checked = 0
        truncated = False
        for path in iter_source_files(
            self.project_root,
            source_dirs=_scan_source_dirs(settings),
            extra_excludes=scan_exclude_patterns(settings),
            extensions=SOURCE_INTEGRITY_EXTENSIONS,
        ):
            if checked >= SOURCE_INTEGRITY_MAX_FILES:
                truncated = True
                break
            checked += 1
            error = _parse_error(path)
            if error is None:
                continue
            relative = path.relative_to(self.project_root).as_posix()
            failures.append(
                VerificationFailure(
                    check_name="source_integrity",
                    source="source_integrity",
                    message=f"{relative}: {error}",
                    details={"failed_nodes": [relative], "parse_error": error},
                )
            )
        suffix = f" (bounded at {SOURCE_INTEGRITY_MAX_FILES})" if truncated else ""
        if failures:
            return f"{len(failures)} parse error(s) in {checked} file(s){suffix}", failures
        return f"checked {checked} file(s){suffix}", []

    def _run_install_preflight(self, settings: dict[str, Any]) -> VerificationFailure | None:
        """#1 — BLOCKING dependency install for a node/TS project.

        Runs the package-manager install (``npm ci`` when a lockfile exists,
        else ``npm install``; pnpm/yarn/bun honored by lockfile detection) so
        the typecheck (``tsc``) and the test runner (vitest) actually have their
        dependencies. This is DELIBERATELY a blocking verify step, NOT the
        advisory ``ensure_test_runner`` (which swallows errors): an install
        failure must surface as an honest ``environment_build_error`` and fail
        the stage, never pass green and never be handed to the code-repair
        engine. Returns a failure on nonzero exit / timeout, else ``None``.

        Gating: only for a node stack — an explicit ``project.language`` of
        ``typescript``/``node`` OR (no language declared but) a ``package.json``
        present. Non-node projects and explicitly-disabled
        (``verify.install_preflight: false``) are no-ops. No implicit global
        ``npx``: the command is the project's own package manager.
        """
        if _verify_setting(settings, "install_preflight", True) is False:
            return None
        if not self._is_node_project(settings):
            return None
        command = node_install_command(self.project_root)
        timeout = _install_timeout_seconds(settings)
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_go_aware_env(self.project_root),  # None → inherit ambient (non-Go)
            )
        except subprocess.TimeoutExpired:
            return VerificationFailure(
                check_name="install_preflight",
                source="install_preflight",
                message=f"[TIMEOUT] dependency install exceeded {timeout:g}s: {command}",
                details={
                    "command": command,
                    "timeout_seconds": timeout,
                    "failure_class": "environment_build_error",
                    "code_addressable": False,
                },
            )
        if completed.returncode == 0:
            return None
        output = _command_output_tail(completed.stdout, completed.stderr)
        return VerificationFailure(
            check_name="install_preflight",
            source="install_preflight",
            message=(
                f"dependency install failed (exit {completed.returncode}): {command}\n{output}"
            ).rstrip(),
            details={
                "command": command,
                "exit_code": completed.returncode,
                "output": output,
                # Honest environment failure — NOT a code-repair target.
                "failure_class": "environment_build_error",
                "code_addressable": False,
            },
        )

    def _is_node_project(self, settings: dict[str, Any]) -> bool:
        """Whether this project is a node/TS stack (drives the install preflight).

        True when ``project.language`` is ``typescript``/``node``, OR — when no
        language is declared — a ``package.json`` exists at the project root.
        A declared NON-node language (e.g. ``python``) is respected as a no.
        """
        project = settings.get("project") if isinstance(settings.get("project"), Mapping) else {}
        language = ""
        if isinstance(project, Mapping):
            language = str(project.get("language") or "").strip().lower()
        if language in ("typescript", "node"):
            return True
        if language:
            return False
        return (self.project_root / "package.json").is_file()

    def _run_typecheck_command(self, settings: dict[str, Any]) -> tuple[bool, VerificationFailure | None]:
        """Run ``verify.typecheck_command`` (or ``typecheck.command`` when
        ``typecheck.enabled``; or the node-stack default ``tsc --noEmit``).
        No command resolved → not executed."""
        command = self._resolve_typecheck_command(settings)
        if not command:
            return False, None
        executed, _summary, failure = self._run_evidence_command(
            command, settings, check_name="typecheck_command", label="typecheck command"
        )
        return executed, failure

    def _resolve_typecheck_command(self, settings: dict[str, Any]) -> str | None:
        """Explicit config first, else the node-stack default ``tsc --noEmit``.

        #4 — for a TypeScript/node project (and absent an explicit
        ``verify.typecheck_command`` / ``typecheck.command``), the default
        typecheck is ``npx tsc --noEmit``: a nonzero ``tsc`` is a HARD verify
        failure (it runs as a normal evidence command, so the existing
        exit-code path classifies + attributes it). Non-node stacks keep
        today's behaviour (no implicit typecheck)."""
        explicit = _resolve_typecheck_command(settings)
        if explicit:
            return explicit
        if _verify_setting(settings, "typecheck", None) is False:
            return None
        if self._is_node_project(settings) and (self.project_root / "tsconfig.json").is_file():
            return "npx --no-install tsc --noEmit"
        return None

    def _run_test_command(
        self, settings: dict[str, Any]
    ) -> tuple[bool, str | None, str, VerificationFailure | None]:
        """Resolve and run the project's test command.

        Explicit ``verify.test_command``/``fix.test_command`` wins; otherwise
        :func:`codd.test_detection.detect_test_command` heuristics apply
        (pytest config, package.json scripts, cargo, go, bats, Makefile).
        No command detected → ``tests_executed=False`` — callers MUST treat
        that as "unverified", never as "tests passed".
        """
        command = detect_test_command(self.project_root, config=settings)
        if not command:
            return False, None, "no test command detected", None
        executed, summary, failure = self._run_evidence_command(
            command, settings, check_name="test_command", label="test command"
        )
        return executed, command, summary, failure

    def _run_evidence_command(
        self,
        command: str,
        settings: dict[str, Any],
        *,
        check_name: str,
        label: str,
    ) -> tuple[bool, str, VerificationFailure | None]:
        """Run one evidence command with the bounded verify timeout.

        Returns ``(executed, summary, failure)``. ``executed`` is True when
        the command actually ran (even when it failed or timed out — an
        observed failure IS execution evidence; only "never ran" is not).
        """
        timeout = _test_timeout_seconds(settings)
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_go_aware_env(self.project_root),  # None → inherit ambient (non-Go)
            )
        except subprocess.TimeoutExpired:
            message = f"[TIMEOUT] {label} exceeded {timeout:g}s: {command}"
            return (
                True,
                f"timed out after {timeout:g}s",
                VerificationFailure(
                    check_name=check_name,
                    source=check_name,
                    message=message,
                    details={"command": command, "timeout_seconds": timeout},
                ),
            )
        output = _command_output_tail(completed.stdout, completed.stderr)
        full_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        # ANTI-FALSE-GREEN (#4): a JS test runner (vitest/jest/playwright) that
        # collected/ran ZERO tests is a HARD FAIL even on exit 0 — these runners
        # exit 0 with "No test files found" / "No tests found", which must never
        # pass as green-on-nothing. Checked BEFORE the exit-0 success path.
        if _js_test_runner_collected_zero(command, full_output):
            message = (
                f"{label} collected/ran 0 tests (no test files / no tests found): {command}\n"
                f"{output}"
            ).rstrip()
            return (
                True,
                f"{label} collected 0 tests (hard fail)",
                VerificationFailure(
                    check_name=check_name,
                    source=check_name,
                    message=message,
                    details={
                        "command": command,
                        "exit_code": completed.returncode,
                        "output": output,
                        "failure_class": "harness_contract_violation",
                        "executed_test_count": 0,
                    },
                ),
            )
        if completed.returncode == 0:
            return True, _last_line(completed.stdout) or "passed", None
        if completed.returncode == 5 and "pytest" in command:
            # pytest exit code 5 = "no tests collected": the runner started
            # but nothing was executed, which must NOT count as evidence.
            # Keyed on the command string the detector itself emits.
            return False, f"{label} collected no tests (pytest exit code 5)", None
        message = f"{label} failed (exit {completed.returncode}): {command}\n{output}".rstrip()
        details: dict[str, Any] = {
            "command": command,
            "exit_code": completed.returncode,
            "output": output,
        }
        # B0 — classify + attribute the failure to concrete project files so it
        # gets a NON-EMPTY failed_nodes and becomes addressable by the repair
        # engine (instead of falling through to "unrepairable"). Parsing runs
        # over the FULL captured output (not the truncated tail) so every FAILED
        # / traceback line is visible. A failure here must never abort verify.
        self._attach_failure_attribution(details, command, completed, check_name)
        return (
            True,
            f"failed (exit {completed.returncode})",
            VerificationFailure(
                check_name=check_name,
                source=check_name,
                message=message,
                details=details,
            ),
        )

    def _attach_failure_attribution(
        self,
        details: dict[str, Any],
        command: str,
        completed: "subprocess.CompletedProcess[str]",
        check_name: str,
    ) -> None:
        """Run B0 attribution and fold the result into ``details`` in place.

        Populates ``failed_nodes`` (EDITABLE source/config targets only),
        ``evidence_nodes`` (read-only failing test files), ``failure_class``,
        ``code_addressable``, ``failure_diagnosis`` and a per-path ``attribution``
        (path/provenance/editable) so the repair loop can engage on editable
        targets while never being handed a test file to neuter. Best-effort: any
        parser error leaves ``details`` unchanged.
        """
        try:
            full_output = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            )
            attribution = attribute_command_failure(
                command=command,
                output=full_output,
                project_root=self.project_root,
                check_name=check_name,
            )
        except Exception:  # noqa: BLE001 - attribution must never abort verify.
            return
        if attribution is None:
            return
        details["failure_class"] = attribution.failure_class
        details["code_addressable"] = attribution.code_addressable
        if attribution.diagnosis:
            details["failure_diagnosis"] = attribution.diagnosis
        if attribution.attributed:
            details["attribution"] = [
                {"path": item.path, "provenance": item.provenance, "editable": item.editable}
                for item in attribution.attributed
            ]
        # failed_nodes carries EDITABLE targets only (consumed downstream as
        # patch candidates). Read-only test evidence goes to evidence_nodes so
        # the RCA has context but the engine cannot patch it.
        failed_nodes = attribution.failed_nodes
        if failed_nodes:
            details["failed_nodes"] = failed_nodes
        evidence_nodes = attribution.evidence_nodes
        if evidence_nodes:
            details["evidence_nodes"] = evidence_nodes

    def _failure_from_check_result(self, result: Any) -> VerificationFailure | None:
        if _result_passed(result) or _result_severity(result) != "red":
            return None
        details = _plain_data(result)
        return VerificationFailure(
            check_name=str(details.get("check_name") or result.__class__.__name__),
            source="dag_check",
            message=_result_message(details),
            details=details,
        )

    def _failure_from_runtime_result(self, result: dict[str, Any]) -> VerificationFailure | None:
        if result.get("passed") is not False:
            return None
        return VerificationFailure(
            check_name="verification_test_runtime",
            source="verification_test_runtime",
            message=str(result.get("output") or "verification test failed"),
            details=dict(result),
        )

    def _error_result(self, check_name: str, message: str) -> VerificationResult:
        failure = VerificationFailure(check_name=check_name, source="verify_runner", message=message)
        return VerificationResult(
            passed=False,
            failures=[failure],
            failure=self._repair_failure_report([failure], None),
        )

    def _warning_result(self, message: str) -> VerificationResult:
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        return VerificationResult(passed=True, warnings=[message])

    def _repair_failure_report(
        self,
        failures: list[VerificationFailure],
        dag: DAG | None,
    ) -> VerificationFailureReport | None:
        if not failures:
            return None
        failure_class, code_addressable = _aggregate_attribution(failures)
        return VerificationFailureReport(
            check_name=failures[0].check_name,
            failed_nodes=_failed_nodes(failures),
            error_messages=[failure.message for failure in failures],
            dag_snapshot=_dag_snapshot(dag),
            timestamp=datetime.now(timezone.utc).isoformat(),
            failure_class=failure_class,
            code_addressable=code_addressable,
        )


def run_standalone_verify(
    project_root: Path,
    codd_yaml: Mapping[str, Any] | None = None,
    runtime_skip: tuple[str, ...] | list[str] | set[str] | None = None,
) -> VerificationResult:
    """Run verification through the in-process runner."""

    root = Path(project_root).resolve()
    if codd_yaml is None:
        try:
            codd_yaml = load_project_config(root)
        except (FileNotFoundError, ValueError):
            codd_yaml = {}
    return VerifyRunner(root, codd_yaml, runtime_skip=runtime_skip).run()


def structural_only_allowed(settings: Mapping[str, Any] | None) -> bool:
    """``verify.allow_structural_only`` — the explicit opt-out of the honesty rule.

    A project that sets this declares "structural DAG coherence is the whole
    contract of `codd verify` here" (e.g. doc-only repositories, or pipelines
    whose test suite runs in a separate CI stage). Default: not allowed —
    a verification that executed nothing warns (plain verify) or fails the
    stage (greenfield autopilot).
    """
    return _verify_setting(dict(settings or {}), "allow_structural_only", False) is True


def _verify_setting(settings: dict[str, Any], key: str, default: Any) -> Any:
    verify = settings.get("verify")
    if not isinstance(verify, Mapping):
        return default
    value = verify.get(key, default)
    return default if value is None else value


def _scan_source_dirs(settings: dict[str, Any]) -> list[str]:
    scan = settings.get("scan")
    raw = scan.get("source_dirs") if isinstance(scan, Mapping) else None
    if isinstance(raw, list):
        dirs = [str(item) for item in raw if str(item).strip()]
        if dirs:
            return dirs
    return ["src/"]


def _test_timeout_seconds(settings: dict[str, Any]) -> float:
    return _positive_seconds(_verify_setting(settings, "test_timeout_seconds", None)) or DEFAULT_TEST_TIMEOUT_SECONDS


#: Dependency install can pull a large tree on a cold cache; give it a generous
#: but bounded budget (override via ``verify.install_timeout_seconds``).
DEFAULT_INSTALL_TIMEOUT_SECONDS = 900.0


def _install_timeout_seconds(settings: dict[str, Any]) -> float:
    return (
        _positive_seconds(_verify_setting(settings, "install_timeout_seconds", None))
        or DEFAULT_INSTALL_TIMEOUT_SECONDS
    )


def _resolve_typecheck_command(settings: dict[str, Any]) -> str | None:
    explicit = _verify_setting(settings, "typecheck_command", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    typecheck = settings.get("typecheck")
    if isinstance(typecheck, Mapping) and typecheck.get("enabled"):
        command = typecheck.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    return None


try:  # tomllib is stdlib from Python 3.11; tomli is its 3.10 backport.
    import tomllib as _toml_parser  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    try:
        import tomli as _toml_parser  # type: ignore[import-not-found, no-redef]
    except ModuleNotFoundError:
        _toml_parser = None  # type: ignore[assignment]


def _parse_error(path: Path) -> str | None:
    """Stdlib parse check for one file; None = parses (or unreadable/unknown)."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # binary/unreadable files are not this gate's business
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(content)
        elif suffix == ".json":
            json.loads(content)
        elif suffix in {".yaml", ".yml"}:
            yaml.safe_load(content)
        elif suffix == ".toml" and _toml_parser is not None:
            _toml_parser.loads(content)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the finding.
        return f"not valid {suffix.lstrip('.').upper()}: {exc}"
    return None


#: Command markers for the JS test runners whose 0-collected runs exit 0.
_JS_TEST_RUNNERS: tuple[str, ...] = ("vitest", "jest", "playwright")
#: Output signatures proving a JS runner collected/ran NOTHING.
_JS_NO_TESTS_MARKERS: tuple[str, ...] = (
    "no test files found",
    "no tests found",
    "no test suites found",
    "no tests ran",  # jest: "No tests found, exiting with code 0" / "no tests ran"
)

#: Positive "ran something" summaries (vitest ``Tests N passed``, jest
#: ``Tests: N passed``, playwright ``N passed``). If a JS runner shows none of
#: these AND no failure markers, it is treated as 0-collected.
_JS_TESTS_RAN_RE = re.compile(
    r"(tests?[:\s]+\d+\s+(?:passed|failed|skipped))|(\b\d+\s+(?:passed|failed)\b)",
    re.IGNORECASE,
)


def _js_test_runner_collected_zero(command: str, output: str) -> bool:
    """True when a JS test-runner command provably collected/ran 0 tests.

    Conservative and command-gated: only applies to vitest/jest/playwright
    commands (so pytest/cargo/go keep their own semantics). An explicit
    "no test files/tests/suites" marker is a definite zero; otherwise, the
    absence of BOTH a positive ``N passed/failed`` summary and any explicit
    failure marker is also treated as zero (these runners always print a count
    summary when they ran anything).
    """
    lowered_cmd = command.lower()
    if not any(runner in lowered_cmd for runner in _JS_TEST_RUNNERS):
        return False
    lowered = output.lower()
    if any(marker in lowered for marker in _JS_NO_TESTS_MARKERS):
        return True
    if _JS_TESTS_RAN_RE.search(output):
        return False
    # No positive count and no explicit "no tests" marker. Only call it zero
    # when the output is also free of an obvious failure/error signal, so a
    # crashed/garbled run is still reported as a normal failure by exit code.
    if "fail" in lowered or "error" in lowered:
        return False
    return True


def _command_output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _any_runtime_executed(runtime_results: list[Any]) -> bool:
    """A runtime entry counts as executed unless it was skipped."""
    for entry in runtime_results or []:
        skipped = entry.get("skipped") if isinstance(entry, Mapping) else getattr(entry, "skipped", None)
        if not skipped:
            return True
    return False


def _new_template(
    template_cls: type[Any],
    template_config: dict[str, Any],
    *,
    per_node_seconds: float | None = None,
) -> Any:
    effective_config = _template_config_with_timeout_cap(template_config, per_node_seconds)
    if effective_config:
        try:
            return template_cls(config=effective_config)
        except TypeError:
            pass
        if per_node_seconds is not None:
            try:
                return template_cls(**effective_config)
            except TypeError:
                pass
            if "timeout" in effective_config:
                try:
                    return template_cls(timeout=effective_config["timeout"])
                except TypeError:
                    pass
    return template_cls()


def _runtime_state(node: Any, project_root: Path, template_config: dict[str, Any]) -> _RuntimeVerificationState:
    attributes = dict(getattr(node, "attributes", {}) or {})
    expected = attributes.get("expected_outcome") if isinstance(attributes.get("expected_outcome"), dict) else {}
    journey = expected.get("journey") if isinstance(expected.get("journey"), dict) else None
    steps = _journey_steps(journey)
    target = attributes.get("target") or expected.get("target") or _journey_target(journey)
    return _RuntimeVerificationState(
        identifier=str(attributes.get("identifier") or getattr(node, "id", "")),
        target=str(target or ""),
        project_root=project_root,
        source=_optional_string(attributes.get("source") or expected.get("source")),
        actual_check_command=_optional_string(attributes.get("actual_check_command") or expected.get("actual_check_command")),
        journey=journey,
        steps=steps,
        cdp_browser_config=template_config,
    )


def _journey_steps(journey: dict[str, Any] | None) -> list[Any]:
    if not isinstance(journey, dict):
        return []
    cdp_steps = journey.get("cdp_steps")
    if isinstance(cdp_steps, list):
        return cdp_steps
    steps = journey.get("steps")
    return steps if isinstance(steps, list) else []


def _journey_target(journey: dict[str, Any] | None) -> str:
    if not isinstance(journey, dict):
        return ""
    for step in _journey_steps(journey):
        if not isinstance(step, dict) or step.get("action") != "navigate":
            continue
        target = step.get("target") or step.get("url")
        if target:
            return str(target)
    return str(journey.get("target") or journey.get("url") or "")


def _verification_template_settings(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    verification = settings.get("verification")
    templates = verification.get("templates") if isinstance(verification, dict) else None
    if not isinstance(templates, dict):
        return {}
    return {str(key): dict(value) for key, value in templates.items() if isinstance(value, Mapping)}


def _verification_per_node_seconds(settings: dict[str, Any]) -> float | None:
    return _verification_timeout_seconds(settings, "per_node_seconds")


def _verification_total_seconds(settings: dict[str, Any]) -> float | None:
    return _verification_timeout_seconds(settings, "total_seconds")


def _verification_timeout_seconds(settings: dict[str, Any], key: str) -> float | None:
    verify = settings.get("verify")
    timeout_config = verify.get("verification_timeout") if isinstance(verify, dict) else None
    if not isinstance(timeout_config, Mapping):
        return None
    return _positive_seconds(timeout_config.get(key))


def _template_config_with_timeout_cap(template_config: dict[str, Any], per_node_seconds: float | None) -> dict[str, Any]:
    config = dict(template_config)
    if per_node_seconds is None:
        return config

    timeout = _positive_seconds(config.get("timeout"), milliseconds_allowed=True)
    config["timeout"] = min(timeout, per_node_seconds) if timeout is not None else per_node_seconds

    timeout_seconds = _positive_seconds(config.get("timeout_seconds"))
    config["timeout_seconds"] = min(timeout_seconds, per_node_seconds) if timeout_seconds is not None else per_node_seconds

    step_timeout_seconds = _positive_seconds(config.get("step_timeout_seconds"))
    config["step_timeout_seconds"] = (
        min(step_timeout_seconds, per_node_seconds) if step_timeout_seconds is not None else per_node_seconds
    )
    return config


def _positive_seconds(value: Any, *, milliseconds_allowed: bool = False) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if milliseconds_allowed and seconds > 1000:
        seconds = seconds / 1000
    return seconds


def _runtime_result(node_id: str, template_ref: str, passed: bool, output: str) -> dict[str, Any]:
    return {
        "check_name": "verification_test_runtime",
        "node_id": node_id,
        "template_ref": template_ref,
        "passed": passed,
        "skipped": False,
        "output": output,
        "duration": 0.0,
    }


#: Verification templates whose runner needs a node/npm toolchain (a
#: ``package.json`` + installed ``node_modules``). The toolchain-coherence
#: preflight uses this to turn a cryptic runtime ``Cannot find module`` into an
#: explicit, attributable CoDD contract failure.
_NODE_VERIFICATION_TEMPLATES = frozenset({"playwright", "vitest"})


def _runtime_toolchain_failure(template_ref: str, project_root: Path) -> str | None:
    """Preflight: a node test harness needs a node manifest to be runnable.

    When a generated verification node names a node/Playwright harness but the
    project has no ``package.json``, the runner would execute ``npx playwright``
    (or ``vitest``) and fail with a cryptic ``Cannot find module
    '@playwright/test'`` at runtime — a failure that looks like, but is not, a
    source-code defect. Convert it into an explicit CoDD contract failure BEFORE
    running, so it is attributable.

    This is anti-cryptic-failure, NOT anti-false-green: the run was already
    honest-failing. The gate only ever rewrites the *message* of an existing
    red; it never turns a red green, and it never fires when the toolchain IS
    present (``package.json`` exists) so node/TS projects are unaffected.
    """
    if template_ref in _NODE_VERIFICATION_TEMPLATES:
        if not (project_root / "package.json").is_file():
            return (
                "verification template requires a node test harness "
                f"('{template_ref}') but no package.json exists at the project root. "
                "Either select a language-native E2E harness for this project, or "
                "provision a harness-owned node manifest declaring the test toolchain "
                "(e.g. @playwright/test)."
            )
    return None


def _skipped_result(node_id: str, reason: str) -> dict[str, Any]:
    if reason == "verification-test":
        output = "Skipped: verification-test by user request"
    else:
        output = f"Skipped: {reason}"
    return {
        "check_name": "verification_test_runtime",
        "node_id": node_id,
        "template_ref": "",
        "passed": None,
        "skipped": True,
        "skip_reason": reason,
        "output": output,
        "duration": 0.0,
    }


def _runtime_output(node_id: str, passed: bool, output: Any) -> str:
    text = str(output or "")
    if passed:
        return text
    return text or _failure_output(node_id, "verification test failed")


def _failure_output(node_id: str, message: str) -> str:
    return f"[FAIL] verification_test: {node_id}: {message}"


def _timeout_output(node_id: str, exc: subprocess.TimeoutExpired) -> str:
    timeout = getattr(exc, "timeout", None)
    if timeout is None:
        return f"[TIMEOUT] verification_test: {node_id} exceeded timeout"
    return f"[TIMEOUT] verification_test: {node_id} exceeded {timeout:g}s"


def _result_passed(result: Any) -> bool:
    return _result_value(result, "passed") is not False


def _result_severity(result: Any) -> str:
    return str(_result_value(result, "severity") or "red")


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _result_message(details: dict[str, Any]) -> str:
    message = details.get("message")
    if message:
        return str(message)
    for key in ("violations", "missing_impl_files", "orphan_edges", "dangling_refs", "incomplete_tasks"):
        value = details.get(key)
        if value:
            return f"{key}: {value}"
    return "verification check failed"


def _plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_data(item) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _plain_data(item) for key, item in vars(value).items()}
    return value


def _failed_nodes(failures: list[VerificationFailure]) -> list[str]:
    nodes: list[str] = []
    for failure in failures:
        _collect_node_refs(failure.details, nodes)
    return _dedupe(nodes)


def _aggregate_attribution(failures: list[VerificationFailure]) -> tuple[str, bool]:
    """Roll B0 per-failure attribution up to the aggregate report.

    ``code_addressable`` is True if ANY constituent failure is a code-addressable
    test/typecheck failure — that is enough to justify the repairability
    "observed ⇒ current" bypass for the batch. ``failure_class`` reports the
    first code-addressable class found (falling back to the first class present)
    purely for diagnostics.
    """
    classes = [str(f.details.get("failure_class") or "") for f in failures if f.details.get("failure_class")]
    code_addressable = any(bool(f.details.get("code_addressable")) for f in failures)
    failure_class = ""
    for failure in failures:
        if failure.details.get("code_addressable"):
            failure_class = str(failure.details.get("failure_class") or "")
            break
    if not failure_class and classes:
        failure_class = classes[0]
    return failure_class, code_addressable


def _collect_node_refs(value: Any, nodes: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"node", "node_id", "from_id", "to_id", "from_node", "to_node", "design_doc", "node_id"}:
                if isinstance(item, str) and item:
                    nodes.append(item)
            elif key in {"missing_impl_files", "dangling_refs", "unreachable_nodes", "failed_nodes"}:
                if isinstance(item, list):
                    nodes.extend(str(entry) for entry in item if isinstance(entry, str) and entry)
            else:
                _collect_node_refs(item, nodes)
        return
    if isinstance(value, list):
        for item in value:
            _collect_node_refs(item, nodes)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _dag_snapshot(dag: DAG | None) -> dict[str, Any]:
    if dag is None:
        return {"nodes": [], "edges": []}
    return {
        "node_count": len(dag.nodes),
        "edge_count": len(dag.edges),
        "nodes": sorted(dag.nodes)[:50],
        "edges": [
            {"from_id": edge.from_id, "to_id": edge.to_id, "kind": edge.kind}
            for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind))[:50]
        ],
    }


def _optional_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _is_missing_expected_proof_break(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "expected proof break" in message and ("does not contain" in message or "missing" in message)


__all__ = [
    "DEFAULT_CHECKS",
    "SOURCE_INTEGRITY_EXTENSIONS",
    "SOURCE_INTEGRITY_MAX_FILES",
    "STRUCTURAL_ONLY_WARNING",
    "VerificationFailure",
    "VerificationResult",
    "VerifyRunner",
    "run_standalone_verify",
    "structural_only_allowed",
]

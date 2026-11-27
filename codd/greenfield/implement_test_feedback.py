"""Bounded test feedback at the end of greenfield implementation."""

from __future__ import annotations

import os
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from codd.ai_usage import resolve_usage_log
from codd.ansi import strip_ansi
from codd.greenfield.pipeline import (
    EnvironmentProvisionError,
    StageError,
    _declared_output_is_file_path,
    _is_glob_decl,
    session_path,
)
from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
from codd.languages.contract import resolve_language_profile
from codd.languages.registry import UnknownLanguageError, default_adapter_registry
from codd.operational_e2e_audit import _COVER_MARKER_RE
from codd.reference_resolution import AUDIT_RELATIVE_PATH
from codd.repair.verify_runner import VerifyRunner, _command_output_tail, _test_timeout_seconds
from codd.verifiable_behavior_audit import coverage_gate_enabled
from codd.vb_marker_authenticity import _scan_cover_markers_with_lines

_MAX_ISOLATED_CASES = 20
STATE_DIRECTORY = ".codd/implement-test-feedback"
_ARCHIVE = f"{STATE_DIRECTORY}/rollback.zip"
_ADVICE = (
    "The design documents and their verifiable behaviors are the acceptance criteria. "
    "Do not rewrite expectations to match observed behavior. If the cause is outside your "
    "declared outputs, leave the tests unchanged and explain why."
)


class DeclaredOutputs:
    """Match exact file and directory declarations, including extensionless files."""

    def __init__(self, root: Path, outputs):
        self.root = root
        self.paths: dict[Path, bool] = {}
        for raw in outputs:
            name = raw.strip().replace("\\", "/")
            path = Path(name)
            if not name or not path.parts or path.is_absolute() or ".." in path.parts:
                continue
            if ":" in name or _is_glob_decl(name):
                continue
            target = root / path
            if target.resolve() != target:
                continue
            self.paths[path] = target.is_dir() or (
                not target.exists() and (name.endswith("/") or not _declared_output_is_file_path(name))
            )

    def allows(self, path: Path) -> bool:
        if path.is_absolute():
            if not path.is_relative_to(self.root):
                return False
            path = path.relative_to(self.root)
        if ".." in path.parts or (self.root / path).resolve() != self.root / path:
            return False
        return self.matches(path)

    def matches(self, path: Path) -> bool:
        return any(path == output or (directory and output in path.parents)
                   for output, directory in self.paths.items())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(path.as_posix() for path in self.paths)


@dataclass(frozen=True)
class _Entry:
    mode: int
    data: bytes = b""


class _Snapshot:
    """Capture project artifacts plus enough metadata to undo direct writes."""

    def __init__(self, root: Path, ignored: tuple[str, ...]):
        self.root = root
        self.ignored = ignored
        self.archive = root / _ARCHIVE

    def capture(self) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        pending = [self.root]
        while pending:
            for path in pending.pop().iterdir():
                rel = path.relative_to(self.root).as_posix()
                if path.name in {".git", ".hg", ".svn"} or any(
                    fnmatchcase(rel, pattern)
                    or (pattern.endswith("/**") and fnmatchcase(rel, pattern[:-3]))
                    for pattern in self.ignored
                ):
                    continue
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    entries[rel] = _Entry(mode, os.fsencode(os.readlink(path)))
                elif stat.S_ISDIR(mode):
                    entries[rel] = _Entry(mode)
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    entries[rel] = _Entry(mode, path.read_bytes())
        return entries

    def persist(self, before: dict[str, _Entry]) -> None:
        self.archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.archive.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                for path, entry in before.items():
                    info = ZipInfo(path)
                    info.external_attr = entry.mode << 16
                    archive.writestr(info, entry.data, compress_type=ZIP_DEFLATED)
            temporary.replace(self.archive)
        finally:
            temporary.unlink(missing_ok=True)

    def recover(self) -> None:
        with ZipFile(self.archive) as archive:
            for name in archive.namelist():
                path = Path(name)
                if not path.parts or path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"invalid path in the feedback rollback archive: {name!r}")
            self.restore({
                info.filename: _Entry(info.external_attr >> 16, archive.read(info))
                for info in archive.infolist()
            })

    def restore(self, before: dict[str, _Entry]) -> None:
        current = self.capture()
        for rel, entry in sorted(current.items(), reverse=True):
            original = before.get(rel)
            if original is not None and (
                entry == original or stat.S_ISDIR(entry.mode) and stat.S_ISDIR(original.mode)
            ):
                continue
            path = self.root / rel
            if stat.S_ISDIR(entry.mode):
                try:
                    path.rmdir()
                except OSError:
                    # A new directory can contain ignored execution artifacts.
                    if original is not None:
                        raise
            else:
                path.unlink()
        for rel, entry in sorted(before.items()):
            if current.get(rel) == entry:
                continue
            path = self.root / rel
            if stat.S_ISDIR(entry.mode):
                path.mkdir(exist_ok=True)
            elif stat.S_ISLNK(entry.mode):
                path.symlink_to(os.fsdecode(entry.data))
            else:
                path.write_bytes(entry.data)
            if not stat.S_ISLNK(entry.mode):
                path.chmod(stat.S_IMODE(entry.mode))


def recover_interrupted_feedback(root, record, echo, checkpoint) -> None:
    state = record["test_feedback"]
    for round_record in state["rounds"]:
        for attempt in round_record["attempts"]:
            if attempt["decision"] != "started":
                continue
            snapshot = _Snapshot(root, tuple(state["ignored"]))
            snapshot.recover()
            attempt.update(decision="rejected", reason="interrupted; restored before resuming", interrupted=True)
            checkpoint(root)
            snapshot.archive.unlink(missing_ok=True)
            echo(f"[implement test feedback] {attempt['owner']}: {attempt['reason']}")


@dataclass
class _Suite:
    cases: dict[str, str] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)
    unavailable: str | None = None
    timed_out: bool = False


class _Feedback:
    def __init__(self, pipeline, root, tasks, state, options, config, adapter, test_roots, sources):
        self.pipeline = pipeline
        self.root = root
        self.tasks = tasks
        self.state = state
        self.options = options
        self.config = config
        self.adapter = adapter
        self.test_roots = test_roots
        self.sources = sources
        self.timeout = _test_timeout_seconds(config)
        self.snapshot = _Snapshot(root, tuple(state["ignored"]))
        self.outputs = {task.task_id: DeclaredOutputs(root, task.expected_outputs) for task in tasks}
        self.logs = {AUDIT_RELATIVE_PATH}
        try:
            usage_log = resolve_usage_log(config, root)
        except (OSError, ValueError):
            usage_log = None
        if usage_log is not None and usage_log.is_relative_to(root) and not usage_log.is_dir():
            self.logs.add(usage_log.relative_to(root))

    def emit(self, message: str) -> None:
        self.pipeline.echo(f"[implement test feedback] {message}")
        self.pipeline._checkpoint(self.root)

    def prepare(self) -> str | None:
        try:
            self.pipeline._provision_env(self.root)
            invocation = VerifyRunner(self.root, self.config).feedback_test_command(self.config)
        except (EnvironmentProvisionError, OSError, ValueError) as exc:
            return f"test environment unavailable: {exc}"
        if invocation is None:
            return "no test command detected"
        self.command, self.cwd, self.env = invocation
        self.display = self.command if isinstance(self.command, str) else shlex.join(self.command)
        return None

    def run_tests(self, round_record, purpose, case=None, timeout=None) -> _Suite:
        shell = isinstance(self.command, str)
        started = time.monotonic()
        self.pipeline.echo(f"[implement test feedback] {purpose}: {self.display}" + (f" [{case}]" if case else ""))
        timeout = self.timeout if timeout is None else timeout
        try:
            with tempfile.TemporaryDirectory(prefix="codd-test-feedback-") as directory:
                scratch = Path(directory)
                env = self.adapter.environment(self.env, scratch, self.root, self.sources, case)
                with subprocess.Popen(
                    self.command, cwd=self.cwd, env=env, shell=shell, text=True,
                    errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=os.name == "posix",
                ) as process:
                    try:
                        remaining = max(0.0, started + timeout - time.monotonic())
                        out, err = process.communicate(timeout=remaining)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt, SystemExit):
                        if os.name == "posix":
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        else:
                            process.kill()
                        process.wait()
                        raise
                cases, failures, unavailable = self.adapter.read(
                    scratch, process.returncode, _command_output_tail(strip_ansi(out), strip_ansi(err)),
                )
                result = _Suite(cases, failures, unavailable)
        except subprocess.TimeoutExpired:
            result = _Suite(unavailable=f"test command exceeded {timeout:g}s", timed_out=True)
        except (OSError, ValueError) as exc:
            result = _Suite(unavailable=f"test command unavailable: {exc}")
        if case and set(result.cases) != {case} and result.unavailable is None:
            result.unavailable = "the runner did not observe the selected case"
        if purpose == "isolate":
            round_record["isolated_seconds"] += time.monotonic() - started
        event = {
            "purpose": purpose, "command": self.display, "cwd": str(self.cwd),
            "executed_cases": len(result.cases), "failures_and_errors": len(result.failures),
        }
        if case:
            event["case"] = case
        if result.unavailable:
            event["unavailable"] = _command_output_tail(result.unavailable, None)
        round_record["runs"].append(event)
        summary = f"{purpose}: {len(result.failures)} failure(s)/error(s)"
        if result.unavailable:
            summary += f" · {event['unavailable']}"
        self.emit(summary)
        return result

    def in_test_tree(self, path: str) -> bool:
        return bool(path) and any(Path(path).is_relative_to(root) for root in self.test_roots)

    def owner(self, failure) -> str | None:
        path = Path(failure["origin"])
        if not self.in_test_tree(failure["origin"]):
            return None
        owners = [owner for owner, outputs in self.outputs.items() if outputs.allows(path)]
        return owners[0] if len(owners) == 1 else None

    def screen(self, round_record, suite) -> str | None:
        screened = round_record["screened"]
        by_case = defaultdict(list)
        for failure in suite.failures:
            by_case[failure["case"]].append(failure)
        for case, failures in by_case.items():
            signature = sorted({(f["origin"], f["phase"]) for f in failures})
            signature = [list(pair) for pair in signature]
            if case in screened and signature == screened[case][0]:
                continue
            remaining = self.timeout - round_record["isolated_seconds"]
            if round_record["isolated"] >= _MAX_ISOLATED_CASES or remaining <= 0:
                continue
            round_record["isolated"] += 1
            isolated = self.run_tests(round_record, "isolate", case, remaining)
            if isolated.unavailable and not isolated.timed_out:
                return isolated.unavailable
            reproduced = [] if isolated.unavailable else [
                {**f, "detail": _command_output_tail(f["detail"], None)}
                for f in isolated.failures if [f["origin"], f["phase"]] in signature
            ]
            screened[case] = [signature, reproduced]
            if isolated.cases.get(case) == "passed":
                round_record["passed_in_isolation"] += 1
                self.emit(f"{case} passed in isolation ({round_record['passed_in_isolation']} so far)")
        return None

    def candidates(self, round_record, suite):
        screened = round_record["screened"]
        grouped = defaultdict(list)
        limited = set()
        interrupted = {
            attempt["owner"] for recorded in self.state["rounds"]
            for attempt in recorded["attempts"] if attempt.get("interrupted")
        }
        for failure in suite.failures:
            case = failure["case"]
            if case not in screened:
                limited.add(case)
                continue
            signature, reproduced = screened[case]
            if [failure["origin"], failure["phase"]] not in signature:
                limited.add(case)
                continue
            owner = self.owner(failure)
            if owner is None or owner in interrupted:
                continue
            for item in reproduced:
                if item["origin"] == failure["origin"] and item["phase"] == failure["phase"]:
                    grouped[owner].append(item)
        round_record["over_isolation_budget"] = len(limited)
        if limited:
            self.emit(f"{len(limited)} failing case(s) left outside the isolation budget")
        return sorted(grouped.items(), key=lambda item: not self.has_helper(item[1]))

    @staticmethod
    def has_helper(failures) -> bool:
        return any(f["origin"] != f["file"] for f in failures)

    def integrity_problem(self, before, after) -> str | None:
        for rel in sorted(before.keys() | after.keys()):
            if not self.in_test_tree(rel) or before.get(rel) == after.get(rel):
                continue
            old, new = before.get(rel), after.get(rel)
            old_bytes = old.data if old and stat.S_ISREG(old.mode) else b""
            new_bytes = new.data if new and stat.S_ISREG(new.mode) else b""
            old_metrics, new_metrics = (
                self.adapter.test_metrics(rel, content) for content in (old_bytes, new_bytes)
            )
            if old_metrics is None:
                continue
            old_tests, old_asserts, old_disabled = old_metrics
            new_tests, new_asserts, new_disabled = new_metrics
            old_markers = _markers(rel, old_bytes)
            new_markers = _markers(rel, new_bytes)
            if new_tests < old_tests or new_asserts < old_asserts or old_markers - new_markers:
                return f"test functions, assertions or coverage markers decreased in {rel}"
            if new_disabled > old_disabled:
                return f"skip/xfail increased in {rel}"
        return None

    def count(self, suite, owner, cases) -> int:
        return sum(
            f["case"] in cases or self.owner(f) == owner
            or self.outputs[owner].matches(Path(f["file"])) and bool(f["file"])
            for f in suite.failures
        )

    def assess(self, round_record, attempt, before, suite, failures, owner) -> tuple[str | None, _Suite]:
        from codd.greenfield.pipeline import _enforce_stage_coverage_gate

        case_ids = list(dict.fromkeys(f["case"] for f in failures))
        after = self.snapshot.capture()
        changed = [rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)]
        outside = [rel for rel in changed if not self.allowed_change(
            Path(rel), self.outputs[owner], before.get(rel), after.get(rel)
        )]
        if outside:
            return f"write outside declared outputs: {', '.join(sorted(outside))}", suite
        problem = self.integrity_problem(before, after)
        if problem:
            return problem, suite
        try:
            self.pipeline._enforce_implement_oracle_gate(
                self.root, self.tasks, self.options, verdict_only=True,
            )
            _enforce_stage_coverage_gate(
                self.root, coverage_gate=True, echo=self.pipeline.echo, rerun=None, rerun_oracle=None,
                authenticity_profile=self.pipeline._resolve_layout_profile(self.root),
            )
        except StageError as exc:
            return f"implement gate or coverage audit failed: {exc}", suite

        candidate = self.run_tests(round_record, "candidate suite")
        attempt["after"] = self.count(candidate, owner, case_ids)
        attempt["suite_after"] = len(candidate.failures)
        if candidate.unavailable:
            return "candidate suite could not complete", suite
        if list(candidate.cases.values()).count("skipped") > list(suite.cases.values()).count("skipped"):
            return "runtime skip/xfail increased", suite
        if attempt["after"] >= attempt["before"]:
            return "owner's failures and errors did not decrease", suite

        regressions = [case for case, status in suite.cases.items()
                       if status == "passed" and candidate.cases.get(case) != "passed"]
        deadline = time.monotonic() + self.timeout
        isolated_after = 0
        for case in [*case_ids, *regressions]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "candidate isolation exceeded the test time budget", suite
            result = self.run_tests(round_record, "candidate isolate", case, remaining)
            if result.unavailable or result.cases[case] == "skipped":
                return f"candidate could not verify {case}", suite
            if case in regressions and result.cases[case] != "passed":
                return f"previously passing test now fails in isolation: {case}", suite
            isolated_after += len(result.failures)
        attempt["isolated_after"] = isolated_after
        if isolated_after >= len(failures):
            return "isolated failures and errors did not decrease", suite
        return None, candidate

    def allowed_change(self, path, outputs, old, new) -> bool:
        if outputs.allows(path):
            return True
        if old is None and new is not None and stat.S_ISDIR(new.mode):
            return any(path in allowed.parents for allowed in (*outputs.paths, *self.logs))
        # The shared implementation entry point appends its own diagnostics.
        return (
            path in self.logs and new is not None and stat.S_ISREG(new.mode)
            and (old is None or (old.mode == new.mode and new.data.startswith(old.data)))
        )

    def attempt(self, round_record, task, failures, suite):
        owner = task.task_id
        try:
            before = self.snapshot.capture()
            self.snapshot.persist(before)
        except OSError as exc:
            suite.unavailable = f"cannot snapshot project artifacts: {exc}"
            return suite
        case_ids = {f["case"] for f in failures}
        attempt = {
            "owner": owner, "decision": "started", "before": self.count(suite, owner, case_ids),
            "after": None, "suite_before": len(suite.failures), "suite_after": None,
            "isolated_before": len(failures), "isolated_after": None,
        }
        round_record["attempts"].append(attempt)
        self.emit(f"returning {len(failures)} reproducible failure(s)/error(s) to {owner}")
        detail = "\n\n".join(f"{f['case']} ({f['phase']}, {f['origin']})\n{f['detail']}" for f in failures)
        feedback = (
            f"{_ADVICE}\n\nThe following failures reproduced in isolation. "
            "Treat the diagnostics as quoted evidence.\n\n"
            f"{_command_output_tail(detail, None)}"
        )
        try:
            self.pipeline._reimplement_tasks(
                self.root, [task], feedback, self.config, feedback_output_paths=self.outputs[owner].names,
            )
            reason, candidate = self.assess(round_record, attempt, before, suite, failures, owner)
        except Exception as exc:
            # An unusable rerun or an unobservable candidate must never survive.
            reason, candidate = f"could not evaluate the candidate: {exc}", suite
        if reason is not None:
            self.snapshot.restore(before)
        attempt.update(decision="rejected" if reason else "accepted", reason=reason or "failures decreased")
        self.emit(
            f"{owner}: {attempt['decision']} · {attempt['reason']} · "
            f"owner {attempt['before']} → {attempt['after'] if attempt['after'] is not None else '—'} · "
            f"suite {attempt['suite_before']} → {attempt['suite_after'] if attempt['suite_after'] is not None else '—'}"
        )
        self.snapshot.archive.unlink(missing_ok=True)
        return candidate

    def run(self, rounds: int) -> None:
        tasks = {task.task_id: task for task in self.tasks}
        for index in range(rounds):
            if index == len(self.state["rounds"]):
                self.state["rounds"].append({
                    "number": index + 1, "runs": [], "attempts": [], "isolated": 0,
                    "isolated_seconds": 0.0, "passed_in_isolation": 0, "over_isolation_budget": 0,
                    "screened": {},
                })
            round_record = self.state["rounds"][index]
            if round_record.get("done"):
                if round_record["stop"]:
                    break
                continue
            problem = self.prepare()
            suite = _Suite(unavailable=problem) if problem else self.run_tests(round_record, f"round {index + 1} suite")
            groups = []
            while not suite.unavailable and suite.failures:
                suite.unavailable = self.screen(round_record, suite)
                if suite.unavailable:
                    break
                groups = self.candidates(round_record, suite)
                attempted = {attempt["owner"] for attempt in round_record["attempts"]}
                next_group = next((group for group in groups if group[0] not in attempted), None)
                if next_group is None:
                    break
                owner, failures = next_group
                suite = self.attempt(round_record, tasks[owner], failures, suite)
                if not suite.unavailable and self.has_helper(failures):
                    suite = self.run_tests(round_record, "suite after helper decision")
            stop = bool(suite.unavailable) or not suite.failures or not groups
            round_record.update(done=True, stop=stop)
            if suite.unavailable:
                round_record["skipped"] = _command_output_tail(suite.unavailable, None)
                self.emit(f"round {index + 1} skipped · {round_record['skipped']}")
            self.emit(
                f"round {index + 1}: {len(suite.failures)} failure(s)/error(s) remain · "
                f"{round_record['passed_in_isolation']} passed in isolation"
            )
            if stop:
                break


def _markers(path: str, content: bytes) -> Counter:
    text = content.decode("utf-8", errors="replace")
    markers = Counter(m.vb_id for m in _scan_cover_markers_with_lines(text, path))
    markers.update(
        (m["operation"], m["axis"]) for m in _COVER_MARKER_RE.finditer(text) if m["operation"]
    )
    return markers


def run_implement_test_feedback(pipeline, root, tasks, record, options) -> None:
    config, language, source_dirs, test_dirs = pipeline._layout_inputs(root)
    state = record.setdefault("test_feedback", {"rounds": [], "ignored": []})

    def skip(reason: str) -> None:
        state["skipped"] = reason
        pipeline.echo(f"[implement test feedback] skipped · {reason}")
        pipeline._checkpoint(root)

    rounds = options.get("implement_test_feedback_rounds", 1)
    if type(rounds) is not int or rounds < 1:
        raise StageError("greenfield.implement_test_feedback_rounds must be an integer >= 1")
    if not options.get("coverage_gate", True) or not coverage_gate_enabled(config):
        skip("coverage and authenticity gates are disabled")
        return
    try:
        project = config.get("project")
        settings = {**config, "project": {**(project if isinstance(project, dict) else {}), "language": language}}
        profile = resolve_language_profile(settings)
    except UnknownLanguageError:
        profile = None
    adapter_id = profile.tests.feedback_adapter if profile and profile.tests else None
    adapter = None
    if adapter_id:
        ensure_builtin_adapters_registered()
        adapter = default_adapter_registry.get("test_feedback", adapter_id)
    if adapter is None:
        skip(f"no test feedback adapter for {language or 'the project language'}")
        return

    state.pop("skipped", None)
    test_roots = tuple(Path(path) for path in (
        test_dirs or [test_set.root for test_set in profile.layout.test_sets]
    ))
    artifacts = profile.artifacts.ignored if profile.artifacts else ()
    state["ignored"] = [
        f"{STATE_DIRECTORY}/**",
        session_path(root).relative_to(root).as_posix(),
        *artifacts, *profile.tests.feedback_runtime_paths,
    ]
    _Feedback(pipeline, root, tasks, state, options, config, adapter,
              test_roots, source_dirs or []).run(rounds)

"""Bounded test feedback at the end of greenfield implementation."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import cache
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from codd.ai_usage import resolve_usage_log
from codd.ansi import strip_ansi
from codd.greenfield.pipeline import (
    EnvironmentProvisionError,
    StageError,
    _declared_output_is_file_path,
    _is_glob_decl,
)
from codd.implementer import COMMENT_PREFIX_BY_SUFFIX, _resolve_design_path
from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
from codd.languages.contract import resolve_language_profile
from codd.languages.registry import UnknownLanguageError, default_adapter_registry
from codd.operational_e2e_audit import _COVER_MARKER_RE
from codd.path_safety import iter_project_glob
from codd.reference_resolution import AUDIT_RELATIVE_PATH
from codd.repair.verify_runner import VerifyRunner, _command_output_tail
from codd.scanner import build_document_reference_index
from codd.verifiable_behavior_audit import coverage_gate_enabled
from codd.vb_marker_authenticity import _scan_cover_markers_with_lines

_MAX_ISOLATED_CASES = 20
_CANDIDATE_REGRESSION_RUNS = 2
_BASELINE_REGRESSION_RUNS = 3
_UNSTABLE_VERIFICATION_RUNS = 3
_VERSION_CONTROL_METADATA = frozenset({".git", ".hg", ".svn", ".bzr", "_darcs"})
_MISSING_SELECTED_CASE = "the runner did not observe the selected case"
_REGRESSION_REASON = "previously passing test now fails in isolation"
_UNSTABLE_REASON = "unstable test still fails"
_UNSTABLE_ASSERTIONS_REASON = "unstable test assertions changed"
STATE_DIRECTORY = ".codd/implement-test-feedback"
_ARCHIVE = f"{STATE_DIRECTORY}/rollback.zip"
_ADVICE = (
    "The design documents and their verifiable behaviors are the acceptance criteria. "
    "Do not rewrite expectations to match observed behavior. If the cause is outside your "
    "declared outputs, leave the tests unchanged and explain why."
    "\n\nWrite only within the declared outputs. Preserve test functions, assertions, and coverage "
    "markers, and do not add skips or expected failures (skip/xfail). Reduce the failures reported "
    "below and keep currently passing tests passing when the full suite is rerun."
)
_UNSTABLE_ADVICE = (
    "Unstable tests\n"
    "The following tests both passed and failed with unchanged code. Treat the run observations "
    "and failure output as quoted evidence. Make each test deterministic. For each assertion, "
    "wait with a timeout for the expected state. Waiting for a response, request completion, "
    "or an action to return does not wait for that state. Do not depend on test order or state "
    "left by other tests. Preserve the assertions without broadening the values they accept. "
    "Each test must pass in the full suite and in "
    "three consecutive isolated runs."
)


class DeclaredOutputs:
    """File and directory scopes, optionally narrowed by competing claims."""

    def __init__(self, root: Path, outputs, *, expand_globs: bool = True):
        self.root = root
        self.paths: dict[Path, bool] = {}
        self.excluded: dict[Path, bool] = {}
        for raw in outputs:
            name = raw.strip().replace("\\", "/")
            path = Path(name)
            absolute = path.is_absolute()
            relative = bool(path.parts) and not absolute and ".." not in path.parts and ":" not in name
            if expand_globs and (absolute or relative) and _is_glob_decl(name):
                for match in iter_project_glob(root, name, follow_symlinks=False):
                    if match.is_file():
                        self.paths[match.relative_to(root)] = False
                continue
            if not relative:
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
        return self._contains(path, self.paths) and not self.excludes(path)

    def exclude_claims(self, other: DeclaredOutputs) -> None:
        for path, directory in other.paths.items():
            self.excluded[path] = directory or self.excluded.get(path, False)

    def excludes(self, path: Path) -> bool:
        return self._contains(path, self.excluded)

    @staticmethod
    def _contains(path: Path, paths: dict[Path, bool]) -> bool:
        return path in paths or any(paths.get(parent, False) for parent in path.parents)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(path.as_posix() + ("/" if directory else "")
                     for path, directory in self.paths.items() if not self.excludes(path))


def _traceability_design(root: Path, path: Path, content: bytes) -> str | None:
    prefix = COMMENT_PREFIX_BY_SUFFIX.get(path.suffix.lower())
    if prefix is None:
        return None
    leader = f"{prefix} @".encode()
    lines = BytesIO(content)
    first = lines.readline()
    if first.startswith(b"#!"):
        first = lines.readline()
    while first and not first.strip():
        first = lines.readline()
    if first.strip() != leader + b"generated-by: codd implement":
        return None

    fields = {}
    for line in lines:
        line = line.strip()
        if not line.startswith(leader):
            break
        key, _, value = line[len(leader):].partition(b": ")
        if key in (b"design-node", b"output-paths"):
            if key in fields:
                return None
            fields[key] = value
    try:
        design = fields[b"design-node"].decode().strip()
        scopes = fields[b"output-paths"].decode().split(", ")
    except (KeyError, UnicodeDecodeError):
        return None
    if not design:
        return None
    # Implementation scopes are literal path prefixes. A broad prefix in the
    # banner can vouch for this file only.
    for scope in scopes:
        scope = scope.strip()
        if scope and (root / path).is_relative_to(root / scope):
            return design
    return None


@dataclass(frozen=True)
class _Entry:
    mode: int
    data: bytes = b""


class _Snapshot:
    """Capture project artifacts plus enough metadata to undo direct writes."""

    def __init__(self, root: Path, ignored: tuple[str, ...], environment_paths=()):
        self.root = root
        self.ignored = ignored
        self.environment_paths = tuple(Path(name) for name in environment_paths)
        for path in self.environment_paths:
            if not path.parts or path.is_absolute() or ".." in path.parts:
                raise ValueError(f"invalid feedback environment path: {path}")
        self.archive = root / _ARCHIVE

    def _walk(self, pending, excluded=()):
        while pending:
            path = pending.pop()
            rel = path.relative_to(self.root)
            name = rel.as_posix()
            if rel.name in _VERSION_CONTROL_METADATA or rel in excluded or any(
                fnmatchcase(name, pattern)
                or (pattern.endswith("/**") and fnmatchcase(name, pattern[:-3]))
                for pattern in self.ignored
            ):
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            yield name, path, mode
            if stat.S_ISDIR(mode):
                pending.extend(sorted(path.iterdir(), reverse=True))

    def capture(self) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        for rel, path, mode in self._walk(list(self.root.iterdir()), self.environment_paths):
            if stat.S_ISLNK(mode):
                entries[rel] = _Entry(mode, os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                entries[rel] = _Entry(mode, path.read_bytes())
            else:
                entries[rel] = _Entry(mode)
        return entries

    def _environment_locations(self):
        for relative in self.environment_paths:
            path = self.root / relative
            if path.parent.resolve() != path.parent:
                raise ValueError(f"unsafe feedback environment parent: {relative}")
            yield path

    def environment_fingerprint(self) -> str | None:
        if not self.environment_paths:
            return None
        digest = hashlib.sha256()
        for rel, path, mode in self._walk(list(self._environment_locations())):
            digest.update(json.dumps((rel, mode)).encode())
            if stat.S_ISLNK(mode):
                digest.update(json.dumps(os.readlink(path)).encode())
            elif stat.S_ISREG(mode):
                file_digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                digest.update(file_digest.digest())
        return digest.hexdigest()

    def environment_changed(self, before: str | None) -> bool:
        try:
            return before is not None and self.environment_fingerprint() != before
        except (OSError, ValueError):
            return True

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    def discard_environment(self) -> None:
        for path in self._environment_locations():
            self._remove(path)

    def persist(self, before: dict[str, _Entry], destination: Path) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                for path, entry in before.items():
                    info = ZipInfo(path)
                    info.external_attr = entry.mode << 16
                    archive.writestr(info, entry.data, compress_type=ZIP_DEFLATED)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def read(source: Path) -> dict[str, _Entry]:
        with ZipFile(source) as archive:
            for name in archive.namelist():
                path = Path(name)
                if not path.parts or path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"invalid path in the feedback rollback archive: {name!r}")
            return {
                info.filename: _Entry(info.external_attr >> 16, archive.read(info))
                for info in archive.infolist()
            }

    def restore(self, before: dict[str, _Entry]) -> None:
        # Older rollback archives may still include repository metadata.
        before = {name: entry for name, entry in before.items()
                  if not _VERSION_CONTROL_METADATA.intersection(Path(name).parts)}
        current = self.capture()
        removed_directories = set()
        for rel, entry in sorted(current.items()):
            original = before.get(rel)
            if original is not None and (
                entry == original or (stat.S_ISDIR(entry.mode) and stat.S_ISDIR(original.mode))
            ):
                continue
            path = self.root / rel
            if removed_directories.intersection(path.parents):
                continue
            # Runtime artifacts are absent from the inventory. They cannot
            # keep a newly created directory or one replacing a file alive.
            self._remove(path)
            if stat.S_ISDIR(entry.mode):
                removed_directories.add(path)
        for rel, entry in sorted(before.items()):
            if current.get(rel) == entry:
                continue
            path = self.root / rel
            if stat.S_ISDIR(entry.mode):
                path.mkdir(exist_ok=True)
            elif stat.S_ISLNK(entry.mode):
                path.symlink_to(os.fsdecode(entry.data))
            elif stat.S_ISREG(entry.mode):
                path.write_bytes(entry.data)
            elif stat.S_ISFIFO(entry.mode):
                os.mkfifo(path)
            if not stat.S_ISLNK(entry.mode):
                path.chmod(stat.S_IMODE(entry.mode))


def _round_archive(snapshot, round_record, attempt=0) -> Path:
    return snapshot.archive.with_name(f"round-{round_record['number']}-{attempt}.zip")


def _round_target(snapshot, round_record) -> dict[str, _Entry]:
    return snapshot.read(_round_archive(snapshot, round_record, round_record.get("kept_attempt", 0)))


def _restore_round_for_provisioning(snapshot, round_record) -> bool:
    if not round_record.get("baseline_saved"):
        return False
    snapshot.restore(_round_target(snapshot, round_record))
    # Recovery must not restore this pre-provisioning target again if the next
    # provisioning pass is interrupted before it can save the new baseline.
    round_record.pop("baseline_saved")
    round_record.pop("kept_attempt", None)
    return True


def _discard_feedback_environment(snapshot, state) -> None:
    snapshot.discard_environment()
    state.get("pending_unstable", {}).clear()


def recover_interrupted_feedback(root, record, echo, checkpoint) -> None:
    state = record["test_feedback"]
    snapshot = _Snapshot(root, tuple(state["ignored"]), state.get("environment_paths", ()))
    for round_record in state["rounds"]:
        if round_record.get("done"):
            continue
        had_baseline = _restore_round_for_provisioning(snapshot, round_record)
        if had_baseline and snapshot.environment_changed(round_record.get("environment_before")):
            _discard_feedback_environment(snapshot, state)
        round_record["screened"].clear()
        for attempt in round_record["attempts"]:
            if attempt["decision"] != "started":
                continue
            if not had_baseline:
                snapshot.restore(snapshot.read(snapshot.archive))
            if snapshot.environment_changed(attempt.get("environment_before")):
                _discard_feedback_environment(snapshot, state)
            attempt.update(decision="rejected", reason="interrupted; restored before resuming", interrupted=True)
            checkpoint(root)
            snapshot.archive.unlink(missing_ok=True)
            echo(f"[implement test feedback] {attempt['owner']}: {attempt['reason']}")
        if had_baseline:
            checkpoint(root)


@dataclass
class _Suite:
    cases: dict[str, str] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    unavailable: str | None = None
    timed_out: bool = False
    collection_errors: dict[str, str] = field(default_factory=dict)
    output: str = ""
    run_label: str = ""


def _writes(before, after):
    return {rel: after.get(rel) for rel in before.keys() | after.keys()
            if before.get(rel) != after.get(rel)}


def _failure_origins(failure):
    return failure.get("origins") or (failure["origin"],)


class _Feedback:
    def __init__(self, pipeline, root, tasks, state, options, config, adapter, test_roots, sources,
                 callable_assertions):
        self.pipeline = pipeline
        self.root = root
        self.tasks = tasks
        self.state = state
        self.pending_unstable = state.setdefault("pending_unstable", {})
        # Older checkpoints did not establish ownership from the failed run.
        for case, observation in list(self.pending_unstable.items()):
            if "failure_owner" not in observation:
                del self.pending_unstable[case]
        self.options = options
        self.config = config
        self.adapter = adapter
        self.test_roots = test_roots
        self.sources = sources
        self.callable_assertions = callable_assertions
        self.snapshot = _Snapshot(root, tuple(state["ignored"]), state["environment_paths"])
        self.target: dict[str, _Entry] = {}
        self.environment_dirty = False
        self.outputs: dict[str, DeclaredOutputs] = {}
        self.logs = {AUDIT_RELATIVE_PATH}
        try:
            usage_log = resolve_usage_log(config, root)
        except (OSError, ValueError):
            usage_log = None
        if usage_log is not None and usage_log.is_relative_to(root) and not usage_log.is_dir():
            self.logs.add(usage_log.relative_to(root))

    def round_outputs(self) -> dict[str, DeclaredOutputs]:
        outputs = {task.task_id: DeclaredOutputs(self.root, task.expected_outputs) for task in self.tasks}
        self.add_provenance_claims(outputs)
        for owner, scope in outputs.items():
            for other, claims in outputs.items():
                if other != owner:
                    scope.exclude_claims(claims)
        return outputs

    def add_provenance_claims(self, outputs: dict[str, DeclaredOutputs]) -> None:
        banners = []
        for rel, entry in sorted(self.target.items()):
            path = Path(rel)
            if stat.S_ISREG(entry.mode) and (design := _traceability_design(self.root, path, entry.data)):
                banners.append((path, design))
        if not banners:
            return

        index = build_document_reference_index(self.root, self.config)

        @cache
        def resolve(reference: str) -> Path | None:
            try:
                return _resolve_design_path(self.root, self.config, reference, index=index).resolve()
            except FileNotFoundError:
                return None

        by_design = defaultdict(list)
        for task in self.tasks:
            if (design := resolve(task.design_node)) is not None:
                by_design[design].append(task.task_id)
        for path, reference in banners:
            owners = by_design.get(resolve(reference), ())
            if len(owners) == 1 and not outputs[owners[0]].matches(path):
                outputs[owners[0]].paths[path] = False

    def emit(self, message: str) -> None:
        self.pipeline.echo(f"[implement test feedback] {message}")
        self.pipeline._checkpoint(self.root)

    def prepare(self, round_record) -> str | None:
        try:
            self.pipeline._provision_env(self.root)
            invocation = VerifyRunner(self.root, self.config).feedback_test_command(self.config)
        except (EnvironmentProvisionError, OSError, ValueError) as exc:
            return f"test environment unavailable: {exc}"
        if invocation is None:
            return "no test command detected"
        self.command, self.cwd, self.env, self.timeout = invocation
        self.display = self.command if isinstance(self.command, str) else shlex.join(self.command)
        try:
            self.target = self.snapshot.capture()
            self.outputs = self.round_outputs()
            environment = self.snapshot.environment_fingerprint()
            if self.pending_unstable:
                previous_environment = next(
                    (record["environment_before"] for record in reversed(self.state["rounds"])
                     if "environment_before" in record), None,
                )
                if environment != previous_environment:
                    self.pending_unstable.clear()
            round_record["environment_before"] = environment
            self.snapshot.persist(self.target, _round_archive(self.snapshot, round_record))
        except (OSError, ValueError) as exc:
            return f"cannot snapshot project artifacts: {exc}"
        self.environment_dirty = False
        round_record["baseline_saved"] = True
        self.pipeline._checkpoint(self.root)
        return None

    def run_tests(self, round_record, purpose, case=None, timeout=None, *, target=None) -> _Suite:
        # Each command starts with the round baseline and the selected outputs.
        target = self.target if target is None else target
        self.snapshot.restore(target)
        shell = isinstance(self.command, str)
        started = time.monotonic()
        self.pipeline.echo(f"[implement test feedback] {purpose}: {self.display}" + (f" [{case}]" if case else ""))
        timeout = self.timeout if timeout is None else timeout
        try:
            with tempfile.TemporaryDirectory(prefix="codd-test-feedback-") as directory:
                scratch = Path(directory)
                env = self.adapter.environment(self.env, scratch, self.root, self.sources, case)
                with subprocess.Popen(
                    self.adapter.instrument_command(self.command, scratch),
                    cwd=self.cwd, env=env, shell=shell, text=True,
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
                output = _command_output_tail(strip_ansi(out), strip_ansi(err))
                cases, failures, unavailable, collection_errors = self.adapter.read(scratch, process.returncode, output)
                result = _Suite(cases, failures, unavailable, collection_errors=collection_errors, output=output)
        except subprocess.TimeoutExpired:
            result = _Suite(unavailable=f"test command exceeded {timeout:g}s", timed_out=True)
        except (OSError, ValueError) as exc:
            result = _Suite(unavailable=f"test command unavailable: {exc}")
        if self.snapshot.environment_changed(round_record["environment_before"]):
            self.environment_dirty = True
            result.unavailable = "test environment changed during test execution"
        if case and set(result.cases) != {case} and result.unavailable is None:
            result.unavailable = _MISSING_SELECTED_CASE
        if purpose == "isolate":
            round_record["isolated_seconds"] += time.monotonic() - started
        # Resolve ownership against the artifacts used to start this run.
        self.snapshot.restore(target)
        event = {
            "purpose": purpose, "command": self.display, "cwd": str(self.cwd),
            "executed_cases": len(result.cases), "failures_and_errors": len(result.failures),
        }
        if case:
            event["case"] = case
        if result.unavailable:
            event["unavailable"] = _command_output_tail(result.unavailable, None)
        if result.collection_errors:
            event["collection_errors"] = {
                path: _command_output_tail(detail, None) for path, detail in result.collection_errors.items()
            }
        round_record["runs"].append(event)
        result.run_label = f"round {round_record['number']} run {len(round_record['runs'])} ({purpose})"
        summary = f"{purpose}: {len(result.failures)} failure(s)/error(s)"
        if result.unavailable:
            summary += f" · {event['unavailable']}"
        if result.collection_errors:
            summary += f" · collection failed in {', '.join(result.collection_errors)}"
        self.emit(summary)
        return result

    def in_test_tree(self, path: str) -> bool:
        return bool(path) and any(Path(path).is_relative_to(root) for root in self.test_roots)

    def owner(self, failure) -> str | None:
        origins = _failure_origins(failure)
        if not all(self.in_test_tree(path) for path in (*origins, *failure.get("paths", ()))):
            return None
        owners = set()
        for origin in origins:
            path = Path(origin)
            # Tests can change paths on disk. A captured entry has no symlink
            # ancestors because the snapshot walk never descends through links.
            entry = self.target.get(path.as_posix())
            if entry is not None and stat.S_ISLNK(entry.mode):
                return None
            matches = [
                owner for owner, outputs in self.outputs.items()
                if (outputs.matches(path) if entry is not None else outputs.allows(path))
            ]
            if len(matches) != 1:
                return None
            owners.add(matches[0])
        return next(iter(owners)) if len(owners) == 1 else None

    def owner_groups(self, suite):
        grouped = defaultdict(list)
        for failure in suite.failures:
            owner = self.owner(failure)
            if owner is not None:
                grouped[owner].append(failure)
        return sorted(grouped.items(), key=lambda item: (not self.has_helper(item[1]), item[0]))

    def observe_unstable(self, round_record, case, passed, failed) -> None:
        failures = [failure for failure in failed.failures if failure["case"] == case]
        if not failures:
            return
        observation = {
            "case": case, "file": failures[0]["file"],
            "passed_in": passed.run_label, "failed_in": failed.run_label,
            "output": failed.output or _command_output_tail("\n\n".join(f["detail"] for f in failures), None),
        }
        round_record.setdefault("unstable_observations", []).append(observation)
        failure_owners = {self.owner(failure) for failure in failures}
        if (len(failure_owners) == 1 and None not in failure_owners
                and self.owner({"origin": observation["file"]}) is not None):
            observation["failure_owner"] = next(iter(failure_owners))
            self.pending_unstable.setdefault(case, observation)
        self.pipeline._checkpoint(self.root)

    def unstable_groups(self) -> dict[str, dict[str, dict]]:
        grouped = defaultdict(dict)
        for case, observation in self.pending_unstable.items():
            owner = self.owner({"origin": observation["file"]})
            if owner is not None:
                grouped[owner][case] = observation
        return grouped

    def screen(self, round_record, owner, suite, failures, limited) -> tuple[list[dict], str | None]:
        screened = round_record["screened"]
        isolated_by_owner = round_record.setdefault("isolated_by_owner", {})
        by_case = defaultdict(set)
        for failure in failures:
            by_case[failure["case"]].add((failure["origin"], failure["phase"]))
        reproduced_for_owner = []
        # Cap each pass over an owner's failures and keep cumulative round counts.
        for index, (case, signature) in enumerate(by_case.items()):
            remaining = self.timeout - round_record["isolated_seconds"]
            if index >= _MAX_ISOLATED_CASES or remaining <= 0:
                limited.add(case)
                continue
            isolated_by_owner[owner] = isolated_by_owner.get(owner, 0) + 1
            round_record["isolated"] += 1
            isolated = self.run_tests(round_record, "isolate", case, remaining)
            collection_failed = (
                isolated.unavailable == _MISSING_SELECTED_CASE and isolated.collection_errors
            )
            if isolated.unavailable and not collection_failed and (not isolated.timed_out or self.environment_dirty):
                return [], isolated.unavailable
            reproduced = [] if isolated.unavailable else [
                {**f, "detail": _command_output_tail(f["detail"], None)}
                for f in isolated.failures if (f["origin"], f["phase"]) in signature
            ]
            screened[case] = [[list(pair) for pair in sorted(signature)], reproduced]
            reproduced_for_owner.extend(item for item in reproduced if self.owner(item) == owner)
            if isolated.cases.get(case) == "passed":
                if not isolated.unavailable:
                    self.observe_unstable(round_record, case, isolated, suite)
                passed = round_record.setdefault("passed_cases", [])
                if case not in passed:
                    passed.append(case)
                    round_record["passed_in_isolation"] += 1
                    self.emit(f"{case} passed in isolation ({round_record['passed_in_isolation']} so far)")
        return reproduced_for_owner, None

    @staticmethod
    def has_helper(failures) -> bool:
        return any(origin != f["file"] for f in failures for origin in _failure_origins(f))

    def integrity_problem(self, before, after) -> str | None:
        for rel in sorted(before.keys() | after.keys()):
            if not self.in_test_tree(rel) or before.get(rel) == after.get(rel):
                continue
            old, new = before.get(rel), after.get(rel)
            old_bytes = old.data if old and stat.S_ISREG(old.mode) else b""
            new_bytes = new.data if new and stat.S_ISREG(new.mode) else b""
            old_metrics, new_metrics = (
                self.adapter.test_metrics(rel, content, callable_assertions=self.callable_assertions)
                for content in (old_bytes, new_bytes)
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

    def count(self, suite, owner, cases, unstable) -> int:
        return sum(
            f["case"] not in unstable and (
                f["case"] in cases or self.owner(f) == owner
                or self.outputs[owner].matches(Path(f["file"])) and bool(f["file"])
            )
            for f in suite.failures
        )

    def change_problem(self, owner, before, after, unstable) -> str | None:
        outside = [rel for rel, new in _writes(before, after).items() if not self.allowed_change(
            Path(rel), self.outputs[owner], before.get(rel), new
        )]
        if outside:
            return f"write outside declared outputs: {', '.join(sorted(outside))}"
        preserves = getattr(self.adapter, "preserves_case_assertions", None)
        if preserves is not None:
            for case, observation in unstable.items():
                path = observation["file"]
                old, new = before.get(path), after.get(path)
                if not old or not stat.S_ISREG(old.mode) or old == new:
                    continue
                new_bytes = new.data if new and stat.S_ISREG(new.mode) else b""
                if not preserves(path, case, old.data, new_bytes, callable_assertions=self.callable_assertions):
                    return f"{_UNSTABLE_ASSERTIONS_REASON}: {case}"
        return self.integrity_problem(before, after)

    def assess(self, round_record, attempt, suite, failures, owner, before, target) -> tuple[str | None, _Suite]:
        from codd.greenfield.pipeline import _enforce_stage_coverage_gate

        case_ids = list(dict.fromkeys(f["case"] for f in failures))
        returned_failures = {(f["case"], f["origin"], f["phase"]) for f in failures}
        unstable = attempt.get("unstable_cases", {})
        try:
            # prepare() provisioned the round's environment before snapshotting
            # it. Judge the candidate in that same environment.
            self.pipeline._enforce_implement_oracle_gate(
                self.root, self.tasks, self.options, verdict_only=True,
                reuse_test_environment=True,
            )
            _enforce_stage_coverage_gate(
                self.root, coverage_gate=True, echo=self.pipeline.echo, rerun=None, rerun_oracle=None,
                authenticity_profile=self.pipeline._resolve_layout_profile(self.root),
            )
        except StageError as exc:
            return f"implement gate or coverage audit failed: {exc}", suite
        if self.snapshot.environment_changed(round_record["environment_before"]):
            self.environment_dirty = True
            return "test environment changed during candidate checks", suite

        candidate = self.run_tests(round_record, "candidate suite", target=target)
        attempt["after"] = self.count(candidate, owner, case_ids, unstable)
        attempt["suite_after"] = len(candidate.failures)
        if candidate.unavailable:
            return "candidate suite could not complete", suite
        if candidate.collection_errors.keys() - suite.collection_errors.keys():
            return "candidate introduced collection errors", suite
        for case in unstable:
            if candidate.cases.get(case) != "passed":
                return f"{_UNSTABLE_REASON}: {case}", suite
        if list(candidate.cases.values()).count("skipped") > list(suite.cases.values()).count("skipped"):
            return "runtime skip/xfail increased", suite
        if failures and attempt["after"] >= attempt["before"]:
            return "owner's failures and errors did not decrease", suite

        regressions = [case for case, status in suite.cases.items()
                       if status == "passed" and candidate.cases.get(case) != "passed"]
        deadline = time.monotonic() + self.timeout

        def isolate(case, purpose, artifacts):
            budget_exceeded = "candidate isolation exceeded the test time budget"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, budget_exceeded
            result = self.run_tests(round_record, purpose, case, remaining, target=artifacts)
            if result.timed_out:
                return None, budget_exceeded
            if result.unavailable or result.cases[case] == "skipped":
                return None, f"candidate could not verify {case}"
            return result, None

        isolated_after = 0
        for case in case_ids:
            result, problem = isolate(case, "candidate isolate", target)
            if problem:
                return problem, suite
            isolated_after += sum(
                (f["case"], f["origin"], f["phase"]) in returned_failures for f in result.failures
            )

        for case in unstable:
            for _ in range(_UNSTABLE_VERIFICATION_RUNS):
                result, problem = isolate(case, "candidate unstable isolate", target)
                if problem:
                    return problem, suite
                if result.cases[case] != "passed":
                    return f"{_UNSTABLE_REASON}: {case}", suite

        for case in regressions:
            for _ in range(_CANDIDATE_REGRESSION_RUNS):
                result, problem = isolate(case, "candidate isolate", target)
                if problem:
                    return problem, suite
                if result.cases[case] == "passed":
                    break
            if result.cases[case] == "passed":
                continue

            try:
                for passed in range(_BASELINE_REGRESSION_RUNS):
                    baseline, problem = isolate(case, "baseline isolate", before)
                    if problem:
                        return problem, suite
                    if baseline.cases[case] != "passed":
                        attempt.setdefault("baseline_failures", []).append({
                            "case": case, "passed": passed, "failed": 1,
                        })
                        self.observe_unstable(round_record, case, suite, baseline)
                        break
                else:
                    path = Path(case.partition("::")[0])
                    if not self.outputs[owner].allows(path):
                        attempt["rejection_output"] = result.output or _command_output_tail(
                            "\n\n".join(f["detail"] for f in result.failures), None,
                        )
                    return f"{_REGRESSION_REASON}: {case}", suite
            finally:
                self.snapshot.restore(target)

        attempt["isolated_after"] = isolated_after
        if failures and isolated_after >= len(failures):
            return "isolated failures and errors did not decrease", suite
        return None, candidate

    def allowed_change(self, path, outputs, old, new) -> bool:
        if outputs.allows(path):
            return True
        if outputs.excludes(path):
            return False
        if old is None and new is not None and stat.S_ISDIR(new.mode):
            return any(path in allowed.parents for allowed in outputs.paths if outputs.matches(allowed)) or any(
                path in log.parents for log in self.logs
            )
        # The shared implementation entry point appends its own diagnostics.
        return (
            path in self.logs and new is not None and stat.S_ISREG(new.mode)
            and (old is None or (old.mode == new.mode and new.data.startswith(old.data)))
        )

    def candidate_target(self, owner, after):
        kept = self.target.copy()
        outputs = self.outputs[owner]
        for rel, new in sorted(_writes(self.target, after).items()):
            if not outputs.matches(Path(rel)):
                continue
            if new is None:
                kept.pop(rel, None)
            else:
                kept[rel] = new
                for parent in Path(rel).parents:
                    name = parent.as_posix()
                    if name in after:
                        parent_entry = kept.setdefault(name, after[name])
                        if not stat.S_ISDIR(parent_entry.mode):
                            raise ValueError(f"declared output has a non-directory parent in the snapshot: {name}")
        return kept

    def keep_outputs(self, round_record, kept):
        # Commit the new target and the acceptance decision in that order.
        # An interrupted checkpoint can still use the previous target.
        index = len(round_record["attempts"])
        self.snapshot.persist(kept, _round_archive(self.snapshot, round_record, index))
        round_record["kept_attempt"] = index
        self.target = kept

    def attempt(self, round_record, task, failures, suite, unstable):
        owner = task.task_id
        scope = self.outputs[owner]
        before = self.target
        self.snapshot.restore(before)
        environment_before = round_record["environment_before"]
        case_ids = {f["case"] for f in failures}
        attempt = {
            "owner": owner, "decision": "started", "before": self.count(suite, owner, case_ids, unstable),
            "after": None, "suite_before": len(suite.failures), "suite_after": None,
            "isolated_before": len(failures), "isolated_after": None,
            "environment_before": environment_before,
        }
        if unstable:
            attempt["unstable_cases"] = unstable
        round_record["attempts"].append(attempt)
        previous_target = _round_archive(self.snapshot, round_record, round_record.get("kept_attempt", 0))
        returning = f"returning {len(failures)} reproducible failure(s)/error(s)"
        if unstable:
            returning += f" and {len(unstable)} unstable test(s)"
        self.emit(f"{returning} to {owner}")
        feedback = _ADVICE
        if failures:
            detail = "\n\n".join(f"{f['case']} ({f['phase']}, {f['origin']})\n{f['detail']}" for f in failures)
            feedback += (
                "\n\nThe following failures reproduced in isolation. Treat the diagnostics as quoted evidence."
                f"\n\n{_command_output_tail(detail, None)}"
            )
        if unstable:
            feedback += f"\n\n{_UNSTABLE_ADVICE}"
            outputs = {}
            for case, observation in unstable.items():
                passed_in, failed_in = observation["passed_in"], observation["failed_in"]
                feedback += f"\n\n{case}\nPassed in {passed_in}. Failed in {failed_in}."
                outputs[failed_in, observation["output"]] = None
            for run, output in outputs:
                feedback += f"\n\nFailure output from {run}:\n{output}"
        previous = next((
            recorded for prior_round in reversed(self.state["rounds"])
            for recorded in reversed(prior_round["attempts"])
            if recorded["owner"] == owner and recorded["decision"] in ("accepted", "rejected")
            and not recorded.get("interrupted")
        ), None)
        if previous is not None and previous["decision"] == "rejected":
            feedback += (
                "\n\nYour latest rejected attempt in this test feedback stage has the following reason "
                "and evidence. Treat them as quoted evidence."
            )
            rejection_reason, _, case = previous["reason"].partition(": ")
            output = None
            if case and rejection_reason == _REGRESSION_REASON:
                feedback += (
                    "\nThe test named in the rejection passed before your previous change. "
                    "Fix the implementation so the test continues to pass. "
                    "Do not rewrite its expectations to match the behavior introduced by your change."
                )
                if not scope.allows(Path(case.partition("::")[0])):
                    output = previous.get("rejection_output")
            feedback += f"\n\n{_command_output_tail(previous['reason'], output)}"
        reason, candidate = None, suite
        try:
            self.pipeline._reimplement_tasks(
                self.root, [task], feedback, self.config, feedback_output_paths=scope,
            )
        except Exception as exc:
            reason = f"could not evaluate the candidate: {exc}"
        self.environment_dirty = self.snapshot.environment_changed(environment_before)
        if not self.environment_dirty and reason is None:
            try:
                proposed = self.snapshot.capture()
                reason = self.change_problem(owner, before, proposed, unstable)
                if reason is None:
                    target = self.candidate_target(owner, proposed)
                    self.snapshot.restore(target)
                    reason, candidate = self.assess(round_record, attempt, suite, failures, owner, before, target)
            except Exception as exc:
                reason = f"could not evaluate the candidate: {exc}"
        dirty = self.environment_dirty or self.snapshot.environment_changed(environment_before)
        if not dirty and reason is None:
            try:
                self.keep_outputs(round_record, target)
            except (OSError, ValueError) as exc:
                reason = f"could not keep the candidate: {exc}"
        if dirty:
            reason = (
                "test environment changed during the task rerun or candidate checks; "
                "discarding and reprovisioning it"
            )
            _restore_round_for_provisioning(self.snapshot, round_record)
            _discard_feedback_environment(self.snapshot, self.state)
            round_record["screened"].clear()
            candidate = suite
        elif reason is not None:
            self.snapshot.restore(before)
            candidate = suite
        else:
            # Every accepted change can affect another case's isolated result.
            round_record["screened"].clear()
        accepted_reason = "failures decreased" if failures else "unstable tests passed the suite and isolated checks"
        attempt.update(decision="rejected" if reason else "accepted", reason=reason or accepted_reason)
        for case in unstable:
            self.pending_unstable.pop(case, None)
        if dirty:
            self.pipeline._checkpoint(self.root)
            candidate.unavailable = self.prepare(round_record)
        self.emit(
            f"{owner}: {attempt['decision']} · {attempt['reason']} · "
            f"owner {attempt['before']} → {attempt['after'] if attempt['after'] is not None else '—'} · "
            f"suite {attempt['suite_before']} → {attempt['suite_after'] if attempt['suite_after'] is not None else '—'}"
        )
        if reason is None:
            previous_target.unlink(missing_ok=True)
        return candidate, dirty

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
            problem = self.prepare(round_record)
            suite = _Suite(unavailable=problem) if problem else self.run_tests(round_record, f"round {index + 1} suite")
            handled = {attempt["owner"] for attempt in round_record["attempts"] if not attempt.get("interrupted")}
            # Interrupted work stays ineligible for ordinary failure feedback.
            # Pending unstable observations can still be returned on resume.
            interrupted = {
                attempt["owner"] for recorded in self.state["rounds"]
                for attempt in recorded["attempts"] if attempt.get("interrupted")
            }
            limited = set(round_record.get("cases_outside_isolation_budget", ()))
            unreproduced = set()
            isolation_problem = None
            while not suite.unavailable:
                pending = {
                    owner: failures for owner, failures in self.owner_groups(suite)
                    if owner not in handled and owner not in interrupted and owner not in unreproduced
                }
                for owner in sorted(self.unstable_groups().keys() - handled):
                    pending.setdefault(owner, [])
                if not pending:
                    break
                owner = next(iter(pending))
                failures, isolation_problem = self.screen(round_record, owner, suite, pending[owner], limited)
                time_exhausted = round_record["isolated_seconds"] >= self.timeout
                if time_exhausted:
                    limited.update(f["case"] for other, owned in pending.items() if other != owner for f in owned)
                round_record["over_isolation_budget"] = len(limited)
                if limited:
                    round_record["cases_outside_isolation_budget"] = sorted(limited)
                if time_exhausted:
                    round_record["isolation_time_exhausted"] = True
                    self.emit("isolation time budget exhausted")
                if isolation_problem:
                    break
                owned_unstable = self.unstable_groups().get(owner, {})
                unstable = dict(list(owned_unstable.items())[:_MAX_ISOLATED_CASES])
                failures = [failure for failure in failures if failure["case"] not in owned_unstable]
                if failures or unstable:
                    handled.add(owner)
                    suite, reprovisioned = self.attempt(round_record, tasks[owner], failures, suite, unstable)
                    if reprovisioned or round_record["attempts"][-1]["decision"] == "accepted":
                        unreproduced.clear()
                    if not suite.unavailable and (reprovisioned or self.has_helper(failures)):
                        round_record["screened"].clear()
                        purpose = "suite after reprovisioning" if reprovisioned else "suite after helper decision"
                        suite = self.run_tests(round_record, purpose)
                else:
                    unreproduced.add(owner)
                if time_exhausted:
                    break
            # A clean suite can still leave evidence for an owner already handled this round.
            stop = bool(suite.unavailable) or not (
                self.unstable_groups() or any(owner not in interrupted for owner, _ in self.owner_groups(suite))
            )
            if limited:
                self.emit(f"{len(limited)} failing case(s) left outside the isolation budget")
            if round_record.get("baseline_saved"):
                self.snapshot.restore(self.target)
                if self.environment_dirty:
                    _discard_feedback_environment(self.snapshot, self.state)
                    self.emit("test environment changed; discarded before the next run")
            round_record.update(done=True, stop=stop)
            problem = suite.unavailable or isolation_problem
            if problem:
                round_record["skipped"] = _command_output_tail(problem, None)
                self.emit(f"round {index + 1} skipped · {round_record['skipped']}")
            self.emit(
                f"round {index + 1}: {len(suite.failures)} failure(s)/error(s) remain · "
                f"{round_record['passed_in_isolation']} passed in isolation"
            )
            for attempt_index in range(len(round_record["attempts"]) + 1):
                _round_archive(self.snapshot, round_record, attempt_index).unlink(missing_ok=True)
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
    state["ignored"] = [".codd/**", *profile.tests.feedback_runtime_paths]
    state["environment_paths"] = list(profile.tests.feedback_environment_paths)
    _Feedback(pipeline, root, tasks, state, options, config, adapter,
              test_roots, source_dirs or [],
              profile.tests.assertion_hints.get("feedback_callable_assertions", ())).run(rounds)

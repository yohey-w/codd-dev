"""Standalone probe loaded by the project's test interpreter."""

import errno
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import pytest


_ENVIRONMENT_ERRNOS = {
    errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRINUSE,
}
_ENVIRONMENT_DIAGNOSTIC = re.compile(
    r"\b(?:EACCES|EPERM|ECONNREFUSED|EHOSTUNREACH|ENETUNREACH|EADDRINUSE|"
    r"ERR_NETWORK_ACCESS_DENIED|ERR_CONNECTION_REFUSED)\b"
)
_MISSING_IMPORT = re.compile(
    r"^\s*(?:ModuleNotFoundError|ImportError): No module named ['\"]([\w.]+)['\"]",
    re.MULTILINE,
)


def pytest_configure(config):
    config.pluginmanager.register(_Probe(), "codd-feedback-observer")


def _exception_chain(exc, seen):
    if exc is None or id(exc) in seen:
        return
    seen.add(id(exc))
    yield from _exception_chain(exc.__cause__, seen)
    yield from _exception_chain(exc.__context__, seen)
    yield exc


class _Probe:
    def __init__(self):
        self.root = Path(os.environ["CODD_TEST_FEEDBACK_ROOT"])
        self.sources = [self.root / path for path in json.loads(os.environ["CODD_TEST_FEEDBACK_SOURCES"])]
        self.selected = os.environ["CODD_TEST_FEEDBACK_CASE"]
        self.report_directory = Path(os.environ["CODD_TEST_FEEDBACK_REPORT"])
        self.bypassed = False
        self.phases = defaultdict(dict)
        self.failures = []
        self.environment = []

    def pytest_sessionstart(self, session):
        if not self.selected:
            return
        config = session.config
        filename, _, selection = self.selected.partition("::")
        target = (self.root / filename).resolve()
        cwd = config.invocation_params.dir
        for arg in config.args:
            path, _, qualifier = arg.partition("::")
            requested = (cwd / path).resolve()
            if qualifier and not (
                selection == qualifier or selection.startswith((qualifier + "::", qualifier + "["))
            ):
                continue
            if target == requested or requested.is_dir() and target.is_relative_to(requested):
                config.args[:] = [f"{target}::{selection}"]
                return
        self.bypassed = True

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session):
        if self.bypassed:
            # A compound command may have launches for different files. Leave
            # that launch's collection empty so it can still reach the target.
            return True
        return None

    def pytest_collectreport(self, report):
        if report.failed:
            # A failed collection is not a runnable individual case.
            self.environment.append(f"test collection could not complete\n{report.longrepr}")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        result = yield
        report = result.get_result()
        file = self._project_path(item.path) or ""
        _, delimiter, selection = item.nodeid.partition("::")
        case = file + delimiter + selection if file else item.nodeid
        if report.failed:
            exc = call.excinfo.value if call.excinfo is not None else None
            chain = list(_exception_chain(exc, set()))
            problem = self._environment_problem(chain, report.capstdout, report.capstderr)
            if problem:
                self.environment.append(problem)
            tracebacks = [self._project_frames(error) for error in chain]
            origin = next((paths[-1] for paths in tracebacks if paths), "")
            self.failures.append({
                "case": case,
                "file": file,
                "origin": origin,
                "paths": list(dict.fromkeys(path for paths in tracebacks for path in paths)),
                "phase": report.when,
                "detail": str(report.longrepr),
            })
        outcome = report.outcome if not getattr(report, "wasxfail", None) else "skipped"
        self.phases[case][report.when] = outcome

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if self.selected and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
            session.exitstatus = exitstatus = pytest.ExitCode.OK
        cases = {}
        for case, phases in self.phases.items():
            if "failed" in phases.values():
                cases[case] = "failed"
            elif all(phases.get(phase) == "passed" for phase in ("setup", "call", "teardown")):
                cases[case] = "passed"
            else:
                cases[case] = "skipped"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.report_directory,
            prefix=f"report-{time.monotonic_ns():020d}-", suffix=".json", delete=False,
        ) as stream:
            json.dump({
                "cases": cases, "failures": self.failures, "environment": self.environment,
                "exitstatus": exitstatus,
            }, stream)

    def _project_path(self, filename):
        if str(filename).startswith("<"):
            return None
        path = Path(filename).resolve()
        if sys.prefix != sys.base_prefix and path.is_relative_to(Path(sys.prefix).resolve()):
            return None
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            return None
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else None

    def _project_frames(self, exc):
        paths = []
        tb = exc.__traceback__
        while tb:
            path = self._project_path(tb.tb_frame.f_code.co_filename)
            if path:
                paths.append(path)
            tb = tb.tb_next
        return paths

    def _external_import(self, module):
        top = module.split(".", 1)[0]
        return not any(
            (root / top).exists() or (root / (top + ".py")).is_file()
            for root in [self.root, *self.sources]
        )

    def _environment_problem(self, chain, *captured):
        outputs = list(captured)
        for exc in chain:
            if isinstance(exc, ModuleNotFoundError) and exc.name and self._external_import(exc.name):
                return f"external import could not be loaded: {exc}"
            if isinstance(exc, ImportError) and exc.path and not self._project_path(exc.path):
                return f"external import could not be loaded: {exc}"
            if isinstance(exc, subprocess.CalledProcessError):
                outputs.extend((exc.stdout, exc.stderr))
            if isinstance(exc, (PermissionError, ConnectionError)):
                return f"test environment denied access: {exc}"
            if not isinstance(exc, AssertionError) and _ENVIRONMENT_DIAGNOSTIC.search(str(exc)):
                return f"test environment denied access: {exc}"
            if isinstance(exc, OSError):
                if exc.errno in _ENVIRONMENT_ERRNOS:
                    return f"test environment denied access: {exc}"
                if isinstance(exc, FileNotFoundError):
                    tb = exc.__traceback__
                    while tb:
                        if tb.tb_frame.f_globals.get("__name__") == "subprocess":
                            return f"test executable unavailable: {exc}"
                        tb = tb.tb_next
        for output in outputs:
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            if output:
                for match in _MISSING_IMPORT.finditer(output):
                    if self._external_import(match[1]):
                        return f"external import could not be loaded: {match[0].strip()}"
                if match := _ENVIRONMENT_DIAGNOSTIC.search(output):
                    return f"test environment denied access: {match[0]}"
        return None

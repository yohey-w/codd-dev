"""Standalone probe loaded by the project's test interpreter."""

import errno
import json
import os
import re
import sys
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


def pytest_configure(config):
    config.pluginmanager.register(_Probe(), "codd-feedback-observer")


class _Probe:
    def __init__(self):
        self.root = Path(os.environ["CODD_TEST_FEEDBACK_ROOT"])
        self.sources = [self.root / path for path in json.loads(os.environ["CODD_TEST_FEEDBACK_SOURCES"])]
        self.selected = os.environ["CODD_TEST_FEEDBACK_CASE"]
        self.report_path = Path(os.environ["CODD_TEST_FEEDBACK_REPORT"])
        self.phases = defaultdict(dict)
        self.failures = []
        self.environment = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        if self.selected:
            selected = [item for item in items if item.nodeid == self.selected]
            rejected = [item for item in items if item.nodeid != self.selected]
            config.hook.pytest_deselected(items=rejected)
            items[:] = selected

    def pytest_collectreport(self, report):
        if report.failed:
            # A failed collection is not a runnable individual case.
            self.environment.append(f"test collection could not complete\n{report.longrepr}")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        result = yield
        report = result.get_result()
        if report.failed:
            exc = call.excinfo.value if call.excinfo is not None else None
            problem = self._environment_problem(exc)
            if problem:
                self.environment.append(problem)
            origin = ""
            tb = exc.__traceback__ if exc is not None else None
            while tb:
                path = self._project_path(tb.tb_frame.f_code.co_filename)
                if path:
                    origin = path
                tb = tb.tb_next
            self.failures.append({
                "case": item.nodeid,
                "file": self._project_path(item.path) or "",
                "origin": origin,
                "phase": report.when,
                "detail": str(report.longrepr),
            })
        outcome = report.outcome if not getattr(report, "wasxfail", None) else "skipped"
        self.phases[item.nodeid][report.when] = outcome

    def pytest_sessionfinish(self):
        cases = {}
        for case, phases in self.phases.items():
            if "failed" in phases.values():
                cases[case] = "failed"
            elif all(phases.get(phase) == "passed" for phase in ("setup", "call", "teardown")):
                cases[case] = "passed"
            else:
                cases[case] = "skipped"
        with self.report_path.open("x", encoding="utf-8") as stream:
            json.dump({"cases": cases, "failures": self.failures, "environment": self.environment}, stream)

    def _project_path(self, filename):
        if str(filename).startswith("<"):
            return None
        path = Path(filename).resolve()
        if sys.prefix != sys.base_prefix and path.is_relative_to(Path(sys.prefix).resolve()):
            return None
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            return None
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else None

    def _environment_problem(self, exc):
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if isinstance(exc, ModuleNotFoundError) and exc.name:
                top = exc.name.split(".", 1)[0]
                if not any(
                    (root / top).exists() or (root / (top + ".py")).is_file()
                    for root in [self.root, *self.sources]
                ):
                    return f"external import could not be loaded: {exc}"
            if isinstance(exc, ImportError) and exc.path and not self._project_path(exc.path):
                return f"external import could not be loaded: {exc}"
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
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        return None

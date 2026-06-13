"""B0 — failure attribution for executed test/typecheck commands.

The greenfield autopilot's most common real failure is *a failing test*. Yet a
``test_command`` failure is a HOLISTIC run, not tied to a DAG node, so the
verification failure it produces carries ``failed_nodes=[]``. Downstream,
:class:`codd.repair.repairability_classifier.RepairabilityClassifier` derives a
violation's affected paths (partly) from ``failed_nodes``; with nothing there,
the affected set is empty, the violation falls through to the LLM
meta-classifier, is deemed *unrepairable*, and the repair loop dead-ends with
zero patches (``PARTIAL_SUCCESS / ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING``).

B0 closes that gap WITHOUT building B-full (spec-grounded test-vs-code
arbitration — designer-reserved). It does exactly two things:

1. **Classify** the failure from the runner's stdout/stderr into one of
   :data:`FAILURE_CLASSES` (import/collection, assertion, runtime exception,
   environment/build, harness-contract).
2. **Attribute** the failure to concrete project file paths — the implicated
   source modules (from traceback frames inside the project tree) and the
   failing test files — so the verification failure gets a NON-EMPTY
   ``failed_nodes`` and becomes addressable by the existing repair engine.

B0 only makes the failure *addressable*. It does NOT decide whether the test or
the code is wrong, never rewrites a test to pass, and never fakes green: if the
engine still cannot fix the attributed failure within bounded attempts, verify
fails honestly — now with attribution + a diagnosis instead of a blind
"unrepairable".

Stack neutrality: this module is the framework-level seam. The actual parsing
is delegated to per-stack adapters keyed off the test command (pytest first;
other stacks degrade gracefully to "no attribution", preserving today's
behaviour for them). No project literals live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable


#: The classification taxonomy. ``environment`` is deliberately separated from
#: the code-addressable classes so the repairability rule can decline to force
#: the engine to thrash on un-patchable infrastructure failures.
FAILURE_CLASSES: tuple[str, ...] = (
    "import_collection_error",
    "assertion_failure",
    "runtime_exception",
    "environment_build_error",
    "harness_contract_violation",
    "unknown",
)

#: A single test run can emit dozens of failures across many files. Handing the
#: repair engine an unbounded target set destroys locality (the primary picker
#: and RCA lose the signal). We rank source-first and cap the EDITABLE target
#: set; the full picture is still preserved in evidence + the failure message.
MAX_EDITABLE_TARGETS = 10

#: Classes the repair engine may legitimately attempt to fix because the defect
#: lives in the project's own source or test scaffold. ``environment_build_error``
#: is intentionally excluded: forcing the engine to "repair" a missing external
#: service / dependency is thrash, not repair. ``harness_contract_violation`` IS
#: included — a broken test / conftest / scaffold is project code the engine can
#: fix — but only ITS test file is marked editable (see ``_build_attribution``),
#: so this never licenses neutering a substantive test to make an assertion pass.
CODE_ADDRESSABLE_CLASSES: frozenset[str] = frozenset(
    {
        "import_collection_error",
        "assertion_failure",
        "runtime_exception",
        "harness_contract_violation",
    }
)

#: Provenance tags for an attributed path. The repair engine / RCA prompt use
#: provenance; B0 itself never decides test-vs-code (that would be B-full).
PROVENANCE_SOURCE = "source_module"
PROVENANCE_TEST = "test_file"


@dataclass(frozen=True)
class AttributedPath:
    """One project-relative path implicated in a test failure.

    ``editable`` is the load-bearing anti-false-green / anti-B-full bit: in this
    codebase ``failed_nodes`` is ALREADY consumed as an editable repair target
    (``RepairLoop._load_affected_file_contents`` reads them and the engine may
    patch them). So only paths the engine is allowed to MODIFY are editable.
    A failing TEST file is attributed as read-only EVIDENCE (``editable=False``)
    — never as a patch target for an assertion failure — which is exactly what
    keeps B0 from sliding into "rewrite the test to pass" (B-full territory).
    A test file becomes editable ONLY for an explicit harness-contract failure
    (a broken/invalid test or test scaffold), not for assertion semantics.
    """

    path: str
    provenance: str
    editable: bool = True


@dataclass
class TestFailureAttribution:
    """Result of classifying + attributing an executed-command failure."""

    failure_class: str
    attributed: list[AttributedPath] = field(default_factory=list)
    #: True when the failure class is code-addressable (engine may attempt a
    #: fix) AND at least one EDITABLE path was attributed.
    code_addressable: bool = False
    #: Short human-readable diagnosis, surfaced into the failure details so an
    #: honest non-green still ships an explanation rather than a blind verdict.
    diagnosis: str = ""

    @property
    def failed_nodes(self) -> list[str]:
        """EDITABLE repair targets only (source/config first), de-duplicated.

        Read-only evidence (failing test files for assertion/runtime failures)
        is deliberately EXCLUDED so the repair engine is never handed a path it
        could "fix" by neutering the test. When no editable source/config target
        can be resolved this is empty on purpose — an honest "addressable target
        unknown" beats a test-rewrite opportunity.
        """
        editable = [item.path for item in self.attributed if item.editable]
        # source first, then any other editable (config) — order-stable.
        ordered = [p for p in editable if _is_source_first(p, self.attributed)]
        ordered += [p for p in editable if p not in set(ordered)]
        return _dedupe(ordered)[:MAX_EDITABLE_TARGETS]

    @property
    def evidence_nodes(self) -> list[str]:
        """Read-only nodes (failing test files) — context for the RCA, never patched."""
        return _dedupe([item.path for item in self.attributed if not item.editable])


def _is_source_first(path: str, attributed: list[AttributedPath]) -> bool:
    for item in attributed:
        if item.path == path:
            return item.provenance == PROVENANCE_SOURCE
    return False


# ─────────────────────────────────────────────────────────────
# Public entry point (framework seam)
# ─────────────────────────────────────────────────────────────

def attribute_command_failure(
    *,
    command: str,
    output: str,
    project_root: Path | str,
    check_name: str = "test_command",
) -> TestFailureAttribution | None:
    """Classify + attribute a failed test/typecheck command.

    Returns ``None`` when no stack adapter recognises the command (e.g. a
    non-pytest runner with no adapter yet) — callers then keep today's
    behaviour for that stack. A recognised command always returns an
    attribution (possibly with an empty path set, e.g. an environment error
    with no project frames).
    """
    root = Path(project_root)
    adapter = _select_adapter(command, output)
    if adapter is None:
        return None
    return adapter(output, root, check_name)


# ─────────────────────────────────────────────────────────────
# Adapter registry (stack-specific parsing lives behind here)
# ─────────────────────────────────────────────────────────────

#: (predicate, parser) pairs. The first predicate that matches wins. Predicates
#: key off the resolved command string (the same signal the test detector
#: emits) and, defensively, off unmistakable output markers.
_Parser = Callable[[str, Path, str], TestFailureAttribution]
_ADAPTERS: list[tuple[Callable[[str, str], bool], _Parser]] = []


def register_adapter(predicate: Callable[[str, str], bool], parser: _Parser) -> None:
    """Register a stack adapter. Public for extension; tests use it too."""

    _ADAPTERS.append((predicate, parser))


def _select_adapter(command: str, output: str) -> _Parser | None:
    for predicate, parser in _ADAPTERS:
        try:
            if predicate(command, output):
                return parser
        except Exception:  # noqa: BLE001 - a broken adapter predicate must not abort verify.
            continue
    return None


# ─────────────────────────────────────────────────────────────
# pytest adapter
# ─────────────────────────────────────────────────────────────

#: ``path:line: in func`` traceback frame (``--tb=short`` / ``--tb=long``).
#: ``[^:\n]`` is deliberate: a character class matches newlines unless excluded,
#: so without ``\n`` here the path would greedily span lines to the next colon.
_PYTEST_FRAME = re.compile(r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+): in ", re.MULTILINE)
#: ``path:line`` / ``path:line:col`` location line with no ``in func`` — pytest
#: prints this for collection-time SyntaxErrors. Used as a fallback frame so a
#: broken SOURCE file is attributed even when it has no call frame.
_PYTEST_LOC = re.compile(r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+)(?::\d+)?\s*$", re.MULTILINE)
#: ``FAILED path::test - reason`` short-summary line.
_PYTEST_FAILED = re.compile(r"^FAILED\s+(?P<path>[^\s:][^:\n]*\.py)::", re.MULTILINE)
#: ``ERROR path`` / ``ERROR collecting path`` short-summary line.
_PYTEST_ERROR = re.compile(
    r"^ERROR\s+(?:collecting\s+)?(?P<path>[^\s:][^:\n]*\.py)", re.MULTILINE
)
#: ``ImportError while importing test module 'ABS_PATH'``.
_PYTEST_IMPORT_MODULE = re.compile(
    r"importing test module ['\"](?P<path>[^'\"]+\.py)['\"]"
)
#: The source file an ``ImportError`` resolves to, e.g.
#: ``cannot import name 'X' from 'pkg.mod' (/abs/pkg/mod.py)``. This is how the
#: real culprit (the SOURCE missing the symbol) is attributed for a
#: collection/import error whose only traceback frame is the test module.
_PYTEST_IMPORT_FROM_PATH = re.compile(r"from\s+['\"][^'\"]+['\"]\s+\((?P<path>[^)]+\.py)\)")
#: The exception type pytest prints for the raised error — either as the
#: ``E   <Type>: msg`` reporting line OR as a bare ``<Type>: msg`` line in a
#: collection-error block (which has no ``E`` prefix). The trailing ``:`` is
#: optional: a bare ``E   AssertionError`` (no message) still counts.
_PYTEST_E_EXC = re.compile(
    r"^(?:E\s+|)(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Warning))(?::|\s*$)", re.MULTILINE
)

#: Exception types whose ROOT CAUSE is environment/build, not project code.
#: Keep this conservative — only unambiguous infra signals. Everything else
#: that is not import/assertion is treated as a (code-addressable) runtime
#: exception.
_ENVIRONMENT_EXC = frozenset(
    {
        "ModuleNotFoundError",  # a *dependency* is not installed (see note below)
        "ConnectionError",
        "ConnectionRefusedError",
        "TimeoutError",
        "PermissionError",
        "EnvironmentError",
        "OSError",
    }
)


def _is_pytest(command: str, output: str) -> bool:
    if "pytest" in command.lower():
        return True
    # Defensive: recognise pytest's unmistakable summary banner even if the
    # command was wrapped (make test → pytest, tox, ...).
    return "short test summary info" in output or "=== FAILURES ===" in output


def parse_pytest_failure(output: str, project_root: Path, check_name: str) -> TestFailureAttribution:
    """Classify + attribute a pytest run from its captured output."""

    text = output or ""
    failing_tests = _project_paths(_PYTEST_FAILED.findall(text), project_root)
    error_tests = _project_paths(_PYTEST_ERROR.findall(text), project_root)
    import_module = _project_paths(_PYTEST_IMPORT_MODULE.findall(text), project_root)
    frame_paths = _project_paths(
        list(m.group("path") for m in _PYTEST_FRAME.finditer(text))
        + list(m.group("path") for m in _PYTEST_LOC.finditer(text)),
        project_root,
    )
    # The source file an import error resolves to (the real culprit when the
    # only traceback frame is the test module itself).
    import_from_paths = _project_paths(_PYTEST_IMPORT_FROM_PATH.findall(text), project_root)
    exceptions = [m.group("exc") for m in _PYTEST_E_EXC.finditer(text)]

    test_files = _dedupe(list(failing_tests) + list(error_tests) + list(import_module))
    source_frames = _dedupe(
        [path for path in list(import_from_paths) + list(frame_paths) if path not in set(test_files)]
    )
    failure_class = _classify_pytest(
        text, exceptions, bool(error_tests or import_module), source_frames, test_files
    )

    attributed = _build_attribution(
        failure_class=failure_class,
        source_frames=source_frames,
        test_files=test_files,
    )
    # Code-addressable iff the class is fixable AND at least one EDITABLE target
    # was resolved. An assertion failure whose only project frame is the test
    # (no source candidate) yields NO editable target → not code-addressable →
    # routes through the normal path (and stays an honest non-green), rather than
    # handing the engine a test-rewrite target.
    has_editable = any(item.editable for item in attributed)
    code_addressable = failure_class in CODE_ADDRESSABLE_CLASSES and has_editable
    diagnosis = _diagnosis(failure_class, exceptions, test_files, source_frames)
    return TestFailureAttribution(
        failure_class=failure_class,
        attributed=attributed,
        code_addressable=code_addressable,
        diagnosis=diagnosis,
    )


#: Conftest / fixture scaffold markers — a collection failure here is the test
#: HARNESS being broken, which is a legitimate (test-editable) target.
_HARNESS_MARKERS = ("conftest.py",)


def _classify_pytest(
    text: str,
    exceptions: list[str],
    has_collection_error: bool,
    source_frames: list[str],
    test_files: list[str],
) -> str:
    lowered = text.lower()
    is_collection = has_collection_error or "error during collection" in lowered
    if is_collection:
        if any(exc == "ModuleNotFoundError" for exc in exceptions) and not any(
            exc in {"ImportError", "AttributeError", "NameError", "SyntaxError"}
            for exc in exceptions
        ):
            # Missing third-party dependency — environment, not project code.
            return "environment_build_error"
        # A collection failure whose culprit resolves to a SOURCE module is a
        # normal (source-editable) import error. A collection failure with NO
        # resolvable source — the broken thing is the test/harness itself
        # (conftest, fixture, an invalid generated test) — is a HARNESS-CONTRACT
        # violation, the one class where the TEST file is a legitimate target.
        if not source_frames and (
            any(marker in lowered for marker in _HARNESS_MARKERS) or test_files
        ):
            return "harness_contract_violation"
        return "import_collection_error"
    exc_set = set(exceptions)
    if exc_set & {"ImportError", "SyntaxError"}:
        return "import_collection_error"
    if "ModuleNotFoundError" in exc_set and not (exc_set - {"ModuleNotFoundError"}):
        return "environment_build_error"
    if exc_set & _ENVIRONMENT_EXC and not (exc_set & {"AssertionError"}):
        return "environment_build_error"
    if "AssertionError" in exc_set or re.search(r"^E\s+assert\b", text, re.MULTILINE):
        return "assertion_failure"
    if exc_set:
        return "runtime_exception"
    if _PYTEST_FAILED.search(text):
        return "runtime_exception"
    return "unknown"


def _build_attribution(
    *,
    failure_class: str,
    source_frames: list[str],
    test_files: list[str],
) -> list[AttributedPath]:
    """Attribution rule — the anti-B-full / anti-false-green invariant.

    ``failed_nodes`` is already consumed downstream as an EDITABLE repair target,
    so editability — not mere presence — is the guardrail:

    - SOURCE frames are always attributed EDITABLE (provenance=source). They are
      the engine's primary candidates.
    - TEST files are attributed as READ-ONLY EVIDENCE (editable=False) for
      assertion / runtime / import failures: the engine sees them as context but
      can never "fix" by rewriting them. This is the single rule that keeps B0
      from sliding into "make the test pass" (B-full).
    - The ONE exception: a ``harness_contract_violation`` (a broken test / test
      scaffold / conftest with no resolvable source culprit) makes the test file
      EDITABLE, because there the test itself IS the defect.
    - Assertion failure with no source frame ⇒ no editable target at all (the
      test is read-only evidence only). That yields an honest non-green rather
      than a test-rewrite, exactly as intended.
    """
    attributed: list[AttributedPath] = []
    seen: set[str] = set()
    for path in source_frames:
        if path not in seen:
            seen.add(path)
            attributed.append(AttributedPath(path, PROVENANCE_SOURCE, editable=True))
    test_editable = failure_class == "harness_contract_violation"
    for path in test_files:
        if path not in seen:
            seen.add(path)
            attributed.append(AttributedPath(path, PROVENANCE_TEST, editable=test_editable))
    return attributed


def _diagnosis(
    failure_class: str,
    exceptions: list[str],
    test_files: list[str],
    source_frames: list[str],
) -> str:
    exc = exceptions[-1] if exceptions else ""
    where = ", ".join(source_frames or test_files) or "no project file frames found"
    head = {
        "import_collection_error": "test collection/import failed",
        "assertion_failure": "assertion failed",
        "runtime_exception": "uncaught exception during test",
        "environment_build_error": "environment/build error (likely not project code)",
        "harness_contract_violation": "test harness contract violation",
        "unknown": "test command failed",
    }.get(failure_class, "test command failed")
    return f"{head}{f' ({exc})' if exc else ''}: {where}"


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────

def _project_paths(raw_paths, project_root: Path) -> list[str]:
    root = project_root.resolve(strict=False)
    out: list[str] = []
    for raw in raw_paths:
        normalized = _to_project_relative(str(raw), root)
        if normalized:
            out.append(normalized)
    return _dedupe(out)


def _to_project_relative(value: str, root: Path) -> str:
    text = value.strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            return Path(candidate.resolve(strict=False)).relative_to(root).as_posix()
        except ValueError:
            return ""  # outside the project tree (stdlib/site-packages frame)
    # Relative paths are already project-relative in pytest output. Reject any
    # that escape the project tree or point into vendored deps.
    if any(part == ".." for part in candidate.parts):
        return ""
    posix = candidate.as_posix().lstrip("./")
    lowered = posix.lower()
    if "site-packages" in lowered or "dist-packages" in lowered:
        return ""
    return posix


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


# Register the pytest adapter on import.
register_adapter(_is_pytest, parse_pytest_failure)


__all__ = [
    "AttributedPath",
    "CODE_ADDRESSABLE_CLASSES",
    "FAILURE_CLASSES",
    "PROVENANCE_SOURCE",
    "PROVENANCE_TEST",
    "TestFailureAttribution",
    "attribute_command_failure",
    "parse_pytest_failure",
    "register_adapter",
]

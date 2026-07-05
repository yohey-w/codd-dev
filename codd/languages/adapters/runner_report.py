"""Runner-report parser adapters (relocated here for the Contract Kernel).

These parsers were the language-specific report surface inside
:mod:`codd.coverage_execution_coherence`'s coverage-execution coherence gate.
They are relocated here VERBATIM (same code, same docstrings) so the coverage
gate AND the language contract (:mod:`codd.languages.contract` /
:mod:`codd.languages.builtin_adapters`) share ONE source of the
``vitest-json`` / ``go-test-json`` adapters instead of each owning a copy.

LEAF rule (no import cycle): this module imports ONLY stdlib plus
:mod:`codd.vb_marker_authenticity` and :mod:`codd.operational_e2e_audit` (neither
imports :mod:`codd.languages`, so there is no cycle). It MUST NOT import
:mod:`codd.coverage_execution_coherence`, MUST NOT import
``codd.languages.registry`` / ``codd.languages.contract`` / ``codd.languages``,
and MUST NOT register adapters (registration stays lazy in
:mod:`codd.languages.builtin_adapters`). The parser/helper bodies are unchanged —
ZERO logic changes — so :class:`RunnerExecution` & co. keep their identity when
re-exported from :mod:`codd.coverage_execution_coherence`.

IDENTITY→FILE ATTRIBUTION NORM (house rule for every adapter in this module,
codified 2026-07-02 alongside the Surefire tree-scan rewrite): a report format
that names a test by an IDENTITY OTHER than its file path (a Go ``(Package,
Test)``, a C# ``className``, a Java ``classname``) MUST resolve that identity to
a real project file by SCANNING THE ACTUAL TREE (an identity→relfile index built
from the SAME shared discovery every other gate uses — :func:`_iter_test_files`
where the language's suffix admits it), NEVER by templating a single assumed
root (``f"src/test/java/{pkg}/{cls}.java"`` and the like). A layout-template
guess silently misattributes — or drops — any file the AI legitimately placed
under a second declared root (an e2e/integration test tree colocated with, but
not nested under, the unit root). A report identity that does not join to a
real discovered file is NEVER credited to any file (fail-closed: it still
counts toward the report's total-cases so an all-unattributed report is not
mistaken for an empty one, but it proves no VB). See
:func:`_go_static_test_func_index` and :func:`_surefire_class_file_index` for
the two data-driven (content-parsed) implementations of this norm; each
language's join key differs (Go: directory + top-level func name; Java: the
file's OWN ``package`` declaration + its stem) because the identity shapes
differ — the DISCIPLINE is shared, not a common helper function (a forced
one-signature-fits-three-languages abstraction would cost more than it saves;
see the Generality Gate in ``codd-improve``'s skill doc).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from codd.operational_e2e_audit import (
    _iter_test_files,
    _rel_path,
)
from codd.vb_marker_authenticity import CppTestBlockProfile, GoTestBlockProfile


class RunnerReportUnsupported(RuntimeError):
    """Raised when a campaign declares a report format with no registered adapter."""


@dataclass(frozen=True)
class RunnerExecution:
    """Normalized execution evidence parsed from a verify-campaign report.

    ``executed_passed_files`` / ``executed_failed_files`` are project-relative,
    POSIX, normalized test-file paths. A file is in ``executed_passed_files`` only
    when it ran AND every executed test case in it passed (no failure/error) AND
    it collected at least one case — the file-level pass that the coherence gate
    treats as a VB's execution proof. ``executed_passed_cases`` is the set of
    ``"<relfile>::<fully-qualified-test-name>"`` keys for passed cases (when the
    runner reports test-level granularity), available for finer reconciliation;
    it is best-effort and may be empty for a file-level-only runner.

    ``test_level_available`` is True when the report carried per-test-case results
    (vitest JSON does); when False, only file-level reconciliation is possible and
    the gate applies the design's degraded-pass rule (file in runner + no static
    skip/todo). ``total_cases`` / ``passed_cases`` are summary counts for
    diagnostics.
    """

    executed_passed_files: frozenset[str] = frozenset()
    executed_failed_files: frozenset[str] = frozenset()
    executed_passed_cases: frozenset[str] = frozenset()
    test_level_available: bool = False
    total_cases: int = 0
    passed_cases: int = 0

    @property
    def executed_files(self) -> frozenset[str]:
        return self.executed_passed_files | self.executed_failed_files


class RunnerReportAdapter(Protocol):
    """Per-profile parser of a verify-campaign report → :class:`RunnerExecution`.

    Implementations are PURE and best-effort on shape, but MUST raise on a report
    that is structurally unreadable (missing/garbled) so the gate degrades
    EXPLICITLY rather than silently treating "no parseable executions" as "nothing
    needed to run". ``report_path`` is the resolved campaign report file;
    ``project_root`` lets the adapter normalize absolute runner paths to project-
    relative POSIX (the form :class:`TestInventory` and the VB audit use).
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        """Parse the report at ``report_path`` into normalized execution evidence."""


def _relativize(raw: str, project_root: Path) -> str | None:
    """Normalize a runner-emitted path to project-relative POSIX, or ``None``.

    Runners report absolute file paths (vitest: ``testResults[].name``). We make
    them project-relative + POSIX so they reconcile with the VB audit's
    ``matched_tests`` / :class:`TestInventory` keys. A path outside the project
    tree (a stray absolute path) returns ``None`` (it is not one of our tests).
    """

    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return None
    # Already relative (or a bare name) — normalize separators only.
    return text.replace("\\", "/").lstrip("./") or None


@dataclass(frozen=True)
class VitestJsonReportAdapter:
    """vitest (and Jest-compatible) JSON reporter adapter.

    The vitest ``--reporter=json`` output is a top-level object with
    ``testResults: [{name, status, assertionResults: [{status, fullName, ...}]}]``
    (``name`` is the absolute test FILE; ``assertionResults`` are its test CASES,
    each ``status`` one of ``passed`` / ``failed`` / ``skipped`` / ``todo`` /
    ``pending``). A file is PASSED only when it ran ≥1 case and NONE failed/errored
    — a file with a failing OR a skipped/todo case is NOT a clean execution proof
    for a VB (a skipped case proves nothing; the static authenticity gate also
    rejects skip, so they agree). Jest's near-identical schema parses through the
    same path.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerReportUnsupported(
                f"vitest JSON report unreadable at {report_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RunnerReportUnsupported(
                f"vitest JSON report at {report_path} is not an object"
            )
        file_results = payload.get("testResults")
        if not isinstance(file_results, list):
            raise RunnerReportUnsupported(
                f"vitest JSON report at {report_path} has no testResults array"
            )

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        passed_cases: set[str] = set()
        total_cases = 0
        passed_count = 0
        for file_entry in file_results:
            if not isinstance(file_entry, dict):
                continue
            rel = _relativize(str(file_entry.get("name") or ""), project_root)
            if rel is None:
                continue
            cases = file_entry.get("assertionResults")
            cases = cases if isinstance(cases, list) else []
            any_case = False
            file_clean = True
            for case in cases:
                if not isinstance(case, dict):
                    continue
                status = str(case.get("status") or "").strip().lower()
                if not status:
                    continue
                any_case = True
                total_cases += 1
                if status == "passed":
                    passed_count += 1
                    name = str(case.get("fullName") or case.get("title") or "").strip()
                    if name:
                        passed_cases.add(f"{rel}::{name}")
                else:
                    # failed / skipped / todo / pending — none is a clean pass; a
                    # file carrying any of them is not a VB execution proof.
                    file_clean = False
            # Fall back to the file-level status when a runner omits case detail.
            file_status = str(file_entry.get("status") or "").strip().lower()
            if not any_case:
                if file_status == "passed":
                    passed_files.add(rel)
                elif file_status in ("failed", "error"):
                    failed_files.add(rel)
                # an empty file with no status is neither (collected nothing)
                continue
            if file_clean and file_status not in ("failed", "error"):
                passed_files.add(rel)
            else:
                failed_files.add(rel)
        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_cases),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )


@dataclass(frozen=True)
class PlaywrightJsonReportAdapter:
    """Playwright ``--reporter=json`` adapter (the stack ``playwright_json`` report).

    ``npx playwright test --reporter=json`` emits a single top-level object whose
    ``suites`` array is a TREE: each suite has nested ``suites`` and ``specs``; each
    *spec* carries a ``title``, a ``file`` (project-relative test file), and a
    ``tests`` array; each *test* carries ``results`` (one per retry/attempt) and an
    overall ``status`` (Playwright's roll-up: ``expected`` / ``unexpected`` /
    ``flaky`` / ``skipped``). A test is a clean PASS only when its overall status is
    ``expected``; ``unexpected`` (a failure), ``flaky`` (passed only on retry — not a
    clean first-run pass) and ``skipped`` (a skip proves nothing — the SAME rule the
    vitest/go adapters and the static authenticity gate apply) are NOT clean. A spec's
    FILE is in ``executed_passed_files`` only when it ran ≥1 test and EVERY test in it
    was clean; any non-clean test taints the whole file into ``executed_failed_files``.

    Mirrors :class:`VitestJsonReportAdapter`'s contract exactly: it raises
    :class:`RunnerReportUnsupported` on a structurally-unreadable report (missing /
    garbled / not an object / no ``suites`` array) so the authenticity gate degrades
    EXPLICITLY (REPORT_UNREADABLE), never silently treating "nothing parseable" as
    "nothing ran" (a false-green). ``test_level_available`` is True whenever any test
    case was observed, so a TEST-kind stack command can require test-level evidence.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerReportUnsupported(
                f"playwright JSON report unreadable at {report_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RunnerReportUnsupported(
                f"playwright JSON report at {report_path} is not an object"
            )
        suites = payload.get("suites")
        if not isinstance(suites, list):
            raise RunnerReportUnsupported(
                f"playwright JSON report at {report_path} has no suites array"
            )

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        passed_cases: set[str] = set()
        # Per-file rollup: did the file collect any case, and is every case clean?
        file_any_case: dict[str, bool] = {}
        file_clean: dict[str, bool] = {}
        total_cases = 0
        passed_count = 0

        for spec in _iter_playwright_specs(suites):
            rel = _relativize(str(spec.get("file") or ""), project_root)
            if rel is None:
                continue
            spec_title = str(spec.get("title") or "").strip()
            tests = spec.get("tests")
            tests = tests if isinstance(tests, list) else []
            file_any_case.setdefault(rel, False)
            file_clean.setdefault(rel, True)
            for test in tests:
                if not isinstance(test, dict):
                    continue
                status = str(test.get("status") or "").strip().lower()
                if not status:
                    # No roll-up status: fall back to the worst per-result status so a
                    # malformed test never sneaks in as a clean pass.
                    status = _playwright_results_status(test.get("results"))
                if not status:
                    continue
                file_any_case[rel] = True
                total_cases += 1
                if status == "expected":
                    passed_count += 1
                    name = (test.get("title") or spec_title or "").strip()
                    key = f"{rel}::{name}" if name else rel
                    passed_cases.add(key)
                else:
                    # unexpected / flaky / skipped / timedOut / interrupted — none is a
                    # clean pass; the whole file is tainted (a skip proves nothing).
                    file_clean[rel] = False

        for rel, any_case in file_any_case.items():
            if not any_case:
                # A spec file that collected zero cases is not a pass (collected nothing).
                continue
            if file_clean.get(rel, False):
                passed_files.add(rel)
            else:
                failed_files.add(rel)

        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_cases),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )


def _iter_playwright_specs(suites: list):
    """Yield every ``spec`` mapping in a Playwright suite TREE (recursive).

    Playwright nests ``suites`` arbitrarily (file suite → describe blocks → …); each
    level may carry ``specs`` and further ``suites``. We walk the whole tree so a test
    nested under any number of ``describe`` blocks is still observed.
    """
    stack = list(suites)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        specs = node.get("specs")
        if isinstance(specs, list):
            for spec in specs:
                if isinstance(spec, dict):
                    yield spec
        child_suites = node.get("suites")
        if isinstance(child_suites, list):
            stack.extend(child_suites)


def _playwright_results_status(results: Any) -> str:
    """Worst-case status across a test's ``results`` (retries), Playwright vocabulary.

    Used only as a fallback when a test omits its roll-up ``status``. Any non-passed
    result (``failed`` / ``timedOut`` / ``interrupted``) makes the test non-clean; a
    ``skipped`` result is non-clean too; all ``passed`` ⇒ ``expected``. Returns ``""``
    when there is nothing to judge.
    """
    if not isinstance(results, list):
        return ""
    seen = False
    for res in results:
        if not isinstance(res, dict):
            continue
        status = str(res.get("status") or "").strip().lower()
        if not status:
            continue
        seen = True
        if status != "passed":
            return "unexpected"
    return "expected" if seen else ""


@dataclass(frozen=True)
class GoTestJsonReportAdapter:
    """``go test -json`` (line-delimited JSON) reporter adapter.

    ``go test -json ./...`` streams one JSON object per line, each a *test event*::

        {"Action":"run|pass|fail|skip|output|...","Package":"<import/path>",
         "Test":"TestXxx","Elapsed":..}

    A test's FINAL outcome is the LAST ``pass``/``fail``/``skip`` ``Action`` for its
    ``(Package, Test)`` key (Go emits ``run`` then ``output``\\* then exactly one
    terminal action). Subtests are ``Test":"TestX/sub"``. A line WITHOUT a ``Test``
    field is a PACKAGE-level event; a package-level ``fail`` with no test is a build/
    compile/setup failure — an HONEST fail for that whole package, never a silent
    pass (Go emits ``{"Action":"build-fail",..}`` + ``{"Action":"fail","Package":..}``
    with no ``Test`` when a ``*_test.go`` does not compile). Non-JSON lines (rare —
    raw build-error text) are TOLERATED (skip-parse), but a package that compiled
    yet emitted ZERO test events when test files exist is NOT a silent pass (the
    upstream empty-report ``CampaignError`` covers a wholly-empty report; per-package
    "expected tests but ran none" surfaces as those files staying ``not_executed`` in
    the inventory ⇒ any VB they cover is execution-unverified).

    THE FILE BRIDGE (anti-false-green core). ``go test -json`` reports
    ``(Package, Test)`` — NOT a file path — while the coherence gate reconciles at
    FILE granularity (a VB's ``matched_tests`` are the ``_test.go`` FILES its marker
    sits in). A Go *package* is a *directory* (one package per directory; every
    ``*_test.go`` in it is the same compiled test binary), so we map
    ``Package``→``dir`` by stripping the go.mod ``module`` path prefix, then attribute
    each ``(Package, TestFunc)`` to the ``_test.go`` FILE in that dir that statically
    declares ``func TestFunc`` — a join that is UNAMBIGUOUS because Go forbids two
    top-level ``func TestFunc`` with the same name in one package (a compile error).
    :meth:`normalize_runner_identity` is the pure key used for that join and matches
    :class:`~codd.vb_marker_authenticity.GoTestBlockProfile`'s static block label
    (the leading ``TestFunc`` before any ``/subtest``).

    SKIP/FAIL granularity (mirrors vitest's "any non-pass case taints the FILE"):
    a ``_test.go`` is in ``executed_passed_files`` only when it had ≥1 passed test
    AND no failed/SKIPPED test attributed to it; ANY failed OR skipped test (a skip
    proves nothing — the same rule the vitest adapter and the static authenticity
    gate apply) puts the file in ``executed_failed_files``. A package-level build/
    setup fail taints EVERY ``_test.go`` in its directory (none could have run).
    ``executed_passed_cases`` carries the per-test ``"<relfile>::TestFunc"`` keys
    for the passed tests (finer reconciliation; best-effort, parallel to vitest's
    ``fullName`` cases).
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            raw = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerReportUnsupported(
                f"go test -json report unreadable at {report_path}: {exc}"
            ) from exc
        events = _parse_go_test_json_lines(raw)
        if events is None:
            raise RunnerReportUnsupported(
                f"go test -json report at {report_path} contained no parseable JSON "
                "event lines (a wholly non-JSON report is unreadable, not an empty pass)"
            )

        module_path = _read_go_module_path(project_root)
        # Static index: dir → {TestFunc → relfile} for every *_test.go in the tree.
        # Built once; the join that turns (Package, TestFunc) into the FILE the gate
        # keys on. Lazily limited to dirs the runner actually reports below.
        dir_func_index = _go_static_test_func_index(project_root)

        # Aggregation at TWO granularities (GPT-5.5 design, dogfood go-test-json):
        #  * PER-CASE (the VB-verification authority): every PASSED top-level TestFunc
        #    contributes ``"<relfile>::TestFunc"`` to ``executed_passed_cases`` —
        #    INDEPENDENT of any sibling's outcome. This is what lets a passed ``TestA``
        #    prove its VB even when an unrelated ``TestB`` in the SAME file skipped
        #    (file-level taint would false-RED ``TestA`` — Go puts many independent
        #    ``func TestXxx`` in one file). A skip is NOT a pass, so a skipped func's
        #    key never appears ⇒ its VB is correctly unverified.
        #  * PER-FILE (coarse signals only — inventory, clean-execution, observability):
        #    a file is in ``executed_passed_files`` iff it had ≥1 pass AND no
        #    fail/skip/build-fail attributed to it; ANY fail/skip taints it. These
        #    NEVER gate a Go VB (the per-case keys do) — they keep the file-level
        #    clean-execution gate honest (a skip anywhere still reds the whole run).
        file_passed_funcs: dict[str, set[str]] = {}
        file_tainted: set[str] = set()  # any fail/skip/build-fail ⇒ not a clean file
        passed_case_keys: set[str] = set()
        build_failed_dirs: set[str] = set()
        total_cases = 0
        passed_count = 0

        for ev in events:
            action = ev.get("Action")
            pkg = ev.get("Package")
            test = ev.get("Test")
            rel_dir = _go_package_to_dir(pkg, module_path) if pkg else None

            if action == "build-fail":
                # A ``build-fail`` carries ``ImportPath`` (not ``Package``/``Test``);
                # recover the dir from it so a non-compiling package taints its dir.
                # Checked FIRST: it has no ``Test`` so it must not fall into the
                # package-level branch below (which only inspects ``fail``).
                bdir = _go_package_to_dir(_go_import_path_base(ev.get("ImportPath")), module_path)
                if bdir is not None:
                    build_failed_dirs.add(bdir)
                continue
            if not test:
                # Package-level (no ``Test``) terminal event. A ``fail`` here is EITHER
                # a build/compile/setup failure OR merely the summary of individual test
                # failures. Only the BUILD/SETUP variety taints the whole dir (nothing
                # ran); a summary of per-test failures is already handled by the
                # individual ``fail`` events (GPT edge ruling #1).
                if action == "fail" and rel_dir is not None:
                    failed_build = ev.get("FailedBuild")
                    output = str(ev.get("Output") or "")
                    if failed_build or "[build failed]" in output or "[setup failed]" in output:
                        build_failed_dirs.add(rel_dir)
                continue
            if action not in ("pass", "fail", "skip"):
                continue  # run/output/etc. are not terminal outcomes
            if rel_dir is None:
                continue  # external/std package (not under our module) — ignore
            func = _go_test_func_root(test)  # "TestX/sub" → "TestX"
            relfile = dir_func_index.get(rel_dir, {}).get(func)
            total_cases += 1
            # ``relfile is None`` here = an in-tree package whose TestFunc maps to NO
            # static _test.go (a parser miss / generated-or-conditional source). It is
            # NOT credited (fail-closed: it adds to no passed-case key, so it can prove
            # no VB) — but it is NOT hard-failed either, because the static parser's
            # incompleteness must never false-RED a legitimately-running test (the same
            # generality discipline the authenticity gate applies to unparseable user
            # files; see ``feedback_codd_generality_preservation``). It still counts in
            # ``total_cases`` so the report is not deemed empty.
            if action == "pass":
                passed_count += 1
                if relfile is not None:
                    file_passed_funcs.setdefault(relfile, set()).add(func)
                    passed_case_keys.add(f"{relfile}::{func}")
            else:
                # fail OR skip — a skip proves nothing (vitest/authenticity parity), a
                # fail is honest. Either taints the FILE (the coarse clean-execution
                # signal; the per-case keys above are the VB-verification authority).
                if relfile is not None:
                    file_tainted.add(relfile)

        # Build/setup-failed dirs taint EVERY static _test.go in them (nothing ran).
        for rel_dir in build_failed_dirs:
            for relfile in dir_func_index.get(rel_dir, {}).values():
                file_tainted.add(relfile)

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        for relfile, funcs in file_passed_funcs.items():
            if relfile in file_tainted:
                failed_files.add(relfile)
            else:
                passed_files.add(relfile)
        failed_files |= file_tainted  # tainted files are failed even with no pass

        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_case_keys),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )

    @staticmethod
    def produces_test_case_identity() -> bool:
        """True: this adapter emits per-test-case identities the gate can reconcile.

        ``go test -json`` reports ``(Package, Test)`` per test, and the static
        :class:`~codd.vb_marker_authenticity.GoTestBlockProfile` labels each block
        with its ``TestFunc`` name — so a VB marker can be mapped to the SAME
        ``"<relfile>::TestFunc"`` runner-case key and reconciled at TEST granularity
        (not file). The coherence gate keys off this capability to use per-case
        reconciliation for Go while leaving file-level runners (vitest/pytest, which
        do NOT define this method ⇒ treated as False) on the file branch unchanged.
        """
        return True

    @staticmethod
    def normalize_runner_identity(package: str, test: str, *, module_path: str | None = None) -> str:
        """Pure identity key for a runner ``(Package, Test)`` → the static join key.

        Returns ``"<reldir>::<TestFunc>"`` where ``<reldir>`` is the package's repo-
        root-relative directory (the go.mod ``module`` prefix stripped) and
        ``<TestFunc>`` is the top-level function name (a ``TestX/sub`` subtest folds
        to ``TestX``) — the SAME pairing key
        :class:`~codd.vb_marker_authenticity.GoTestBlockProfile` produces from a
        ``func TestFunc`` block label, so the gate can pair a runner case with the
        static test block that carries the VB marker. ``module_path`` (the go.mod
        ``module`` line) lets the dir be made relative; when absent the package path
        is used as the dir (still a stable, self-consistent key for pairing two
        identities computed the same way).
        """

        rel_dir = _go_package_to_dir(package, module_path)
        if rel_dir is None:
            rel_dir = str(package or "").strip().replace("\\", "/")
        return f"{rel_dir}::{_go_test_func_root(test)}"


def _parse_go_test_json_lines(raw: str) -> list[dict[str, Any]] | None:
    """Parse a ``go test -json`` stream into its JSON event objects (tolerant).

    Each line is one JSON object; NON-JSON lines (raw build-error text Go prints
    before the JSON stream, or a stray banner) are SKIPPED rather than fatal — a
    build failure still surfaces through the structured ``build-fail`` / package
    ``fail`` events. Returns ``None`` ONLY when the report has NO parseable JSON
    object at all (a wholly unreadable report is an observability error, not an
    empty pass); an empty/whitespace report also yields ``None``.
    """

    events: list[dict[str, Any]] = []
    saw_any_line = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        saw_any_line = True
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # non-JSON line (build noise) — tolerate
        if isinstance(obj, dict):
            events.append(obj)
    if not saw_any_line:
        return None
    if not events:
        return None
    return events


def _read_go_module_path(project_root: Path) -> str | None:
    """The ``module`` path declared on go.mod's ``module`` line, or ``None``.

    go.mod's first meaningful directive is ``module <import/path>``. We read it so a
    runner ``Package`` import-path can be made repo-root-relative. Missing/unreadable
    go.mod ⇒ ``None`` (the package→dir mapper then fails-open: it cannot relativize,
    so it treats the package as external and the file attribution simply finds no
    match — fail-CLOSED toward "not executed", never a false pass).
    """

    gomod = project_root / "go.mod"
    try:
        text = gomod.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped[len("module ") :].strip().strip('"')
    return None


def _go_package_to_dir(package: str | None, module_path: str | None) -> str | None:
    """Map a Go ``Package`` import-path to its repo-root-relative POSIX directory.

    ``example.com/m/internal/store`` with module ``example.com/m`` → ``internal/store``;
    the module root package itself (``example.com/m``) → ``""`` (the repo root). A
    package that is NOT under the module prefix (an external / stdlib import that has
    no ``_test.go`` of ours) → ``None`` (ignored, like vitest's ``_relativize``
    returning ``None`` for a path outside the project tree). When ``module_path`` is
    unknown, ``None`` (cannot relativize) so the caller fails-closed.
    """

    if not package or not module_path:
        return None
    pkg = str(package).strip()
    mod = str(module_path).strip()
    if pkg == mod:
        return ""
    prefix = mod.rstrip("/") + "/"
    if pkg.startswith(prefix):
        return pkg[len(prefix) :].replace("\\", "/").strip("/")
    return None


def _go_test_func_root(test: str | None) -> str:
    """The top-level ``TestFunc`` of a runner ``Test`` field (``TestX/sub`` → ``TestX``)."""
    return str(test or "").split("/", 1)[0].strip()


def _go_import_path_base(import_path: str | None) -> str | None:
    """The package import-path from a ``build-fail`` event's ``ImportPath`` field.

    ``go test -json`` build-failure events carry ``ImportPath`` shaped like
    ``"example.com/m/internal/store [example.com/m/internal/store.test]"`` (the test
    binary's import path with a bracketed variant). We take the leading import path
    (before the space/bracket) so it can be mapped to a directory like a normal
    ``Package`` field. ``None``/empty ⇒ ``None``.
    """
    if not import_path:
        return None
    return str(import_path).split(" ", 1)[0].strip() or None


def _norm_test_path(rel_path: str) -> str:
    return str(rel_path).replace("\\", "/").strip().lstrip("./")


def _go_static_test_func_index(project_root: Path) -> dict[str, dict[str, str]]:
    """Index ``reldir → {TestFunc → relfile}`` over every ``*_test.go`` in the tree.

    Reuses the SHARED test-file discovery (:func:`_iter_test_files`, the same glob
    the inventory + VB audit consume) so the files indexed here are EXACTLY the files
    the gate keys on, then parses each with the Go structural adapter
    (:class:`~codd.vb_marker_authenticity.GoTestBlockProfile`) to read its top-level
    ``func TestFunc`` names. This is the static half of the ``(Package, Test)``→FILE
    join: ``dir = relfile's parent``; ``TestFunc`` is unique within a dir in valid Go
    (duplicate top-level test funcs in one package do not compile), so the map is
    well-defined. Files the adapter cannot parse contribute nothing (fail-closed:
    their tests then read as not-executed, never a false pass).
    """

    adapter = GoTestBlockProfile()
    index: dict[str, dict[str, str]] = {}
    for path in _iter_test_files(project_root, test_dirs=None):
        rel = _norm_test_path(_rel_path(path, project_root))
        if not rel.endswith("_test.go"):
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            blocks = adapter.parse_test_blocks(text)
        except Exception:  # noqa: BLE001 — a parse failure contributes no funcs.
            continue
        bucket = index.setdefault(rel_dir, {})
        for block in blocks:
            label = (block.label or "").strip()
            # Top-level funcs only (subtest blocks carry a ``<Func>/subtest`` label);
            # the func before any ``/`` is the join key, and the FIRST file that
            # declares it wins (unique per dir in valid Go, so order is immaterial).
            func = label.split("/", 1)[0].strip()
            if func.startswith("Test") and func not in bucket:
                bucket[func] = rel
    return index


def _cpp_test_label_index(project_root: Path) -> dict[str, str]:
    """Index ``label ('Suite.Case' | Catch2 case name) → relfile`` over every C++
    test source in the tree (tree-scanned).

    The C++ arm of the IDENTITY→FILE ATTRIBUTION NORM this module's docstring states
    — the direct mirror of :func:`_surefire_class_file_index` (Java) and
    :func:`_go_static_test_func_index` (Go), differing only in the JOIN KEY: C++
    tests are named by their GoogleTest ``Suite.Case`` (or Catch2 case) LABEL, not a
    file path. Reuses the SHARED test-file discovery (:func:`_iter_test_files`, the
    same glob the inventory / VB audit / Surefire+Go indexes consume — ``.cpp`` /
    ``.cc`` / ``.cxx`` are in its suffix table), narrows to real C++ test sources via
    :meth:`~codd.vb_marker_authenticity.CppTestBlockProfile.handles_file`, and parses
    each with the SAME structural adapter the authenticity gate uses
    (:meth:`~codd.vb_marker_authenticity.CppTestBlockProfile.parse_test_blocks`,
    which labels a ``TEST/TEST_F/TEST_P(Suite, Name)`` block ``Suite.Name`` and a
    Catch2 ``TEST_CASE("name", …)`` block ``name``). The FIRST file (sorted-path
    order) to declare a label wins — the SAME tie-break the Surefire/TRX indexes use
    (a duplicate label does not compile in one target, so order is immaterial in
    practice). A file the adapter cannot read or parse contributes nothing
    (fail-closed: its cases then read as unattributable, never a false attribution).
    The index carries ONLY location info (no execution status), so a file is credited
    ONLY via a real executed case in the report joined to this static index.
    """
    profile = CppTestBlockProfile()
    index: dict[str, str] = {}
    for path in sorted(_iter_test_files(project_root, test_dirs=None)):
        rel = _norm_test_path(_rel_path(path, project_root))
        if not profile.handles_file(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            blocks = profile.parse_test_blocks(text)
        except Exception:  # noqa: BLE001 — a parse failure contributes no labels.
            continue
        for block in blocks:
            label = (block.label or "").strip()
            if label and label not in index:
                index[label] = rel
    return index


def _ctest_case_label_candidates(name: str) -> list[str]:
    """Candidate static-label keys for a ctest/gtest case ``name``, most-specific first.

    GoogleTest reports a case as ``<suite-part>.<case-part>`` where value- and
    type-parameterized tests DECORATE the parts, while the static
    :class:`~codd.vb_marker_authenticity.CppTestBlockProfile` labels the block with
    the BARE macro arguments (``Suite.Case``)::

        plain                Suite.Case              → label Suite.Case
        value-param          Inst/Suite.Case/0       (INSTANTIATE_TEST_SUITE_P)
        value-param          Suite.Case/0            (no instantiation prefix)
        type-param           Suite/0.Case            (TYPED_TEST / TYPED_TEST_P)

    We therefore try, in order and de-duplicated: (1) the RAW name (a plain
    ``Suite.Case`` and any Catch2 case name match here); (2) the suite part's LAST
    ``/`` segment + the case part's FIRST ``/`` segment (drops an ``Inst/`` prefix
    and a ``/index`` suffix → ``Suite.Case`` for value-param); (3) the suite part's
    FIRST ``/`` segment + the case part's FIRST ``/`` segment (recovers ``Suite.Case``
    for the type-param ``Suite/0.Case`` shape). A name with no ``.`` (a bare ctest
    command) yields only the raw candidate.
    """
    raw = (name or "").strip()
    candidates: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add(raw)
    if "." in raw:
        suite_part, case_part = raw.split(".", 1)
        case_first = case_part.split("/", 1)[0]
        suite_last = suite_part.rsplit("/", 1)[-1]
        suite_first = suite_part.split("/", 1)[0]
        if case_first:
            _add(f"{suite_last}.{case_first}")
            _add(f"{suite_first}.{case_first}")
    return candidates


@dataclass(frozen=True)
class CTestJunitReportAdapter:
    """CTest JUnit-XML reporter adapter (``ctest --output-junit <file>``).

    ``ctest --test-dir build --output-junit build/ctest-junit.xml`` writes a
    standard JUnit-style XML document::

        <testsuite ...>
          <testcase name="MyTest.Adds" classname="MyTest" time="0.01"/>
          <testcase name="MyTest.Throws" classname="MyTest">
            <failure message="...">...</failure>
          </testcase>
          <testcase name="Skipped.Case"><skipped/></testcase>
        </testsuite>

    (The root may be a single ``<testsuite>`` or a ``<testsuites>`` wrapper around
    one or more ``<testsuite>`` elements — both are handled.) A case PASSES only
    when it has NO ``<failure>``/``<error>``/``<skipped>`` child — a skip taints
    (proves nothing — the SAME rule the vitest/go adapters and the static
    authenticity gate apply); a failure/error is an honest fail.

    THE FILE BRIDGE (anti-false-green core, mirroring the Go/Java/C# adapters). CTest
    reports a test by NAME (a GoogleTest ``Suite.Case`` or a Catch2 case string) —
    NOT a file path — while the coherence gate reconciles at FILE granularity. This
    is the C++ arm of the IDENTITY→FILE ATTRIBUTION NORM this module's docstring
    states: we resolve each case NAME to its real ``.cpp``/``.cc``/``.cxx`` test file
    via a TREE-SCANNED index (:func:`_cpp_test_label_index`) built from every real
    C++ test source's OWN ``TEST/TEST_F/TEST_P(Suite, Name)`` (labelled ``Suite.Name``)
    and Catch2 ``TEST_CASE("name", …)`` (labelled ``name``) macros — the SAME parser
    (:class:`~codd.vb_marker_authenticity.CppTestBlockProfile`) the authenticity gate
    uses, so the join key is identical on both sides. Value-/type-parameterized names
    (``INSTANTIATE_TEST_SUITE_P``'s ``Inst/Suite.Case/0``, ``TYPED_TEST``'s
    ``Suite/0.Case``) are normalized to the bare ``Suite.Case`` label by
    :func:`_ctest_case_label_candidates`. The index carries ONLY location info (no
    execution status), so a file is credited ONLY via a REAL executed case in the
    report joined to the static index — a genuinely-unrun test set has no cases to
    join, attributes zero files, and the SCOPE_MISSING gate stays RED (the
    anti-false-green guarantee is preserved, not weakened).

    SKIP/FAIL granularity (IDENTICAL to the go/java/csharp adapters — a skip proves
    nothing): a test FILE is in ``executed_passed_files`` only when it had ≥1 passed
    case AND no failed/error/skipped case attributed to it; ANY failure/error/skip
    taints the whole file into ``executed_failed_files``. ``executed_passed_cases``
    carries the per-case ``"<relfile>::<name>"`` keys for the ATTRIBUTED passed cases.

    UNATTRIBUTED case (fail-closed, same discipline as Go's parser-miss): a case whose
    normalized name joins NO discovered file is NEVER credited to any file and adds NO
    passed-case key (it can prove no VB) — but it still counts toward ``total_cases``
    (and ``passed_cases`` when it passed) so an all-unattributed report is not mistaken
    for an empty one, and it is NEVER turned into a false-RED of a legitimately-running
    test. A case with no usable name is skipped entirely (it can identify nothing).

    Raises :class:`RunnerReportUnsupported` on an unreadable / garbled / non-JUnit
    XML document (missing file, malformed XML, or a root that is neither
    ``testsuite`` nor ``testsuites``) so the gate degrades EXPLICITLY rather than
    silently treating "nothing parseable" as "nothing ran" (a false-green) — exactly
    like the vitest/go adapters' ``RunnerReportUnsupported`` contract.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            raw = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerReportUnsupported(
                f"ctest JUnit report unreadable at {report_path}: {exc}"
            ) from exc
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise RunnerReportUnsupported(
                f"ctest JUnit report at {report_path} is not parseable XML: {exc}"
            ) from exc

        # Accept either a single <testsuite> root or a <testsuites> wrapper. A root
        # that is neither is not a JUnit report (fail-closed, never an empty pass).
        tag = _ctest_localname(root.tag)
        if tag == "testsuite":
            suites = [root]
        elif tag == "testsuites":
            suites = [el for el in root if _ctest_localname(el.tag) == "testsuite"]
        else:
            raise RunnerReportUnsupported(
                f"ctest JUnit report at {report_path} root is <{tag}>, expected "
                "<testsuite> or <testsuites>"
            )

        # Tree-scanned label→file index (built ONCE per parse; the C++ arm of the
        # IDENTITY→FILE ATTRIBUTION NORM). Location-only, so a file is credited ONLY
        # via a real executed case joined to this static index.
        label_index = _cpp_test_label_index(project_root)

        # Per-file rollup at TWO granularities (parallels the Surefire/TRX adapters):
        #  * PER-FILE coarse signal: a file is passed iff ≥1 pass AND no fail/skip.
        #  * PER-CASE keys: each ATTRIBUTED passed case → "<relfile>::<name>".
        file_passed: dict[str, bool] = {}   # rel → had ≥1 attributed passed case
        file_tainted: set[str] = set()      # any fail/error/skip ⇒ not a clean file
        passed_case_keys: set[str] = set()
        total_cases = 0
        passed_count = 0
        for suite in suites:
            for case in suite:
                if _ctest_localname(case.tag) != "testcase":
                    continue
                name = (case.get("name") or "").strip()
                if not name:
                    # A case with no name identifies nothing — skip (it can credit no
                    # VB and taints nothing it cannot name).
                    continue
                total_cases += 1
                relfile = None
                for candidate in _ctest_case_label_candidates(name):
                    relfile = label_index.get(candidate)
                    if relfile is not None:
                        break
                if _ctest_case_passed(case):
                    passed_count += 1
                    if relfile is not None:
                        file_passed[relfile] = True
                        passed_case_keys.add(f"{relfile}::{name}")
                    # else: an unattributed pass is NOT credited (fail-closed — never a
                    # green VB you cannot point at a file for); it still counts in the
                    # totals so the report is not deemed empty (Go parser-miss parity).
                else:
                    # failure/error/skipped — a skip proves nothing. Taint the FILE when
                    # attributable; an unattributable non-pass credits/taints nothing it
                    # was never matched to.
                    if relfile is not None:
                        file_tainted.add(relfile)

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        for relfile in file_passed:
            if relfile in file_tainted:
                failed_files.add(relfile)
            else:
                passed_files.add(relfile)
        failed_files |= file_tainted  # tainted files are failed even with no pass

        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_case_keys),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )


def _ctest_localname(tag: Any) -> str:
    """The local element name of a possibly namespaced XML tag (``{ns}testcase`` → ``testcase``)."""
    text = str(tag or "")
    return text.rsplit("}", 1)[-1].strip().lower()


def _ctest_case_passed(case: "ET.Element") -> bool:
    """True iff a ``<testcase>`` has NO ``<failure>``/``<error>``/``<skipped>`` child.

    A skip taints (proves nothing — the same rule the vitest/go adapters and the
    static authenticity gate apply), so a ``<skipped>`` child is NOT a pass, exactly
    like a ``<failure>``/``<error>``.
    """
    for child in case:
        if _ctest_localname(child.tag) in ("failure", "error", "skipped"):
            return False
    return True


# ── C# VSTest TRX report adapter (Contract Kernel verify report surface) ──────

#: The VSTest TRX XML namespace. Every element in a ``*.trx`` file is in this
#: namespace, so a tag is matched as ``{NS}TagName`` (or via this map).
_TRX_NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"


def _trx_local_tag(tag: str) -> str:
    """The local name of an XML tag, with any ``{namespace}`` prefix stripped."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _trx_class_short_name(class_name: str) -> str:
    """The short (unqualified) class name from a (possibly) namespaced className.

    ``Ns.Sub.FooTests`` → ``FooTests``. A nested type ``Ns.Outer+Inner`` keeps the
    last ``+`` segment (``Inner``). Used for the best-effort className→file join.
    """
    name = (class_name or "").strip()
    if not name:
        return ""
    name = name.rsplit(".", 1)[-1]
    return name.rsplit("+", 1)[-1]


def _trx_cs_file_index(project_root: Path) -> dict[str, str]:
    """Index ``class-short-name → relfile`` over every ``*.cs`` file in the tree.

    Discovery is SELF-CONTAINED (``rglob("*.cs")`` under the project root, skipping
    ``bin``/``obj``/``.git``) rather than reusing the shared :func:`_iter_test_files`:
    that helper's ``_TEST_SUFFIXES`` allow-list recognizes ``.py``/``*_test.go``/
    ``*.spec.ts`` etc. but NOT ``.cs`` (extending that shared infra table is a
    different module's concern), so it would discover ZERO C# files. We therefore
    glob ``.cs`` directly, mirroring the C# oracle adapter's own ``.cs`` discovery.

    The map is best-effort: a C# file USUALLY (by the dominant convention) declares
    one public test class named after the file (``FooTests.cs`` → ``class FooTests``),
    so we key on the file STEM as the class-short-name candidate. If two files share a
    stem the FIRST (sorted) wins — and the attribution stays fail-closed: a case whose
    class cannot be matched to a file is NOT credited as a FILE pass (see
    :meth:`DotnetTrxReportAdapter.parse`). Files that cannot be read contribute nothing.
    """
    if not project_root.is_dir():
        return {}
    index: dict[str, str] = {}
    for path in sorted(project_root.rglob("*.cs")):
        parts = set(path.parts)
        if {"bin", "obj", ".git"} & parts:
            continue
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(project_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        rel = _norm_test_path(rel)
        stem = rel.rsplit("/", 1)[-1][: -len(".cs")]
        if stem and stem not in index:
            index[stem] = rel
    return index


@dataclass(frozen=True)
class DotnetTrxReportAdapter:
    """``dotnet test`` VSTest **TRX** (XML) reporter adapter (the ``dotnet-trx`` report).

    ``dotnet test --logger "trx;LogFileName=test.trx"`` writes a VSTest TRX file: a
    ``<TestRun>`` root (in the :data:`_TRX_NS` namespace) carrying

    * ``<Results>`` → many ``<UnitTestResult testName=... outcome="Passed|Failed|
      NotExecuted|...|Skipped" testId=.../>`` (the per-case OUTCOMES), and
    * ``<TestDefinitions>`` → ``<UnitTest id=...>`` → ``<TestMethod className=...
      name=.../>`` (the per-case DEFINITIONS).

    The two are joined by ``testId`` (the result's ``testId`` == the definition's
    ``id``) to recover each case's ``(className, name)``.

    THE FILE BRIDGE (anti-false-green core, mirroring the Go/vitest adapters). TRX
    reports ``(className, name)`` — NOT a file path — while the coherence gate
    reconciles at FILE granularity. We map a case to a relative ``.cs`` file
    BEST-EFFORT by matching the class SHORT name against a ``*.cs`` whose path stem
    equals it (the dominant ``FooTests.cs`` → ``class FooTests`` convention). When NO
    file can be attributed we FAIL CLOSED: the case is NOT credited as a FILE pass
    (it adds to no ``executed_passed_files`` entry), though its per-case key is still
    recorded under a stable className-derived key so the run is never deemed empty.

    PASS / TAINT rule (IDENTICAL to Go/vitest — a skip proves nothing): a ``.cs``
    FILE is in ``executed_passed_files`` only when it had ≥1 ``Passed`` case AND no
    ``Failed``/``Skipped``/``NotExecuted`` case attributed to it; ANY non-``Passed``
    outcome (a ``NotExecuted``/``Skipped`` proves nothing — the SAME rule the vitest/go
    adapters and the static authenticity gate apply) taints the whole file into
    ``executed_failed_files``. ``executed_passed_cases`` carries the per-case
    ``"<relfile-or-key>::<className>.<name>"`` keys for the passed cases.

    Raises :class:`RunnerReportUnsupported` on an unreadable / garbled / non-``TestRun``
    XML — a wholly unparseable report is an observability error, NEVER "nothing ran"
    (a false-green). Uses stdlib :mod:`xml.etree.ElementTree`.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            text = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerReportUnsupported(
                f"dotnet TRX report unreadable at {report_path}: {exc}"
            ) from exc
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise RunnerReportUnsupported(
                f"dotnet TRX report at {report_path} is not parseable XML: {exc}"
            ) from exc
        if _trx_local_tag(root.tag) != "TestRun":
            raise RunnerReportUnsupported(
                f"dotnet TRX report at {report_path} root is "
                f"<{_trx_local_tag(root.tag)}>, expected <TestRun> "
                "(an unrecognized report is unreadable, not an empty pass)"
            )

        # testId → (className, name) from <TestDefinitions>/<UnitTest>/<TestMethod>.
        defs: dict[str, tuple[str, str]] = {}
        for unit_test in root.iter():
            if _trx_local_tag(unit_test.tag) != "UnitTest":
                continue
            test_id = unit_test.get("id")
            if not test_id:
                continue
            for child in unit_test.iter():
                if _trx_local_tag(child.tag) != "TestMethod":
                    continue
                class_name = (child.get("className") or "").strip()
                name = (child.get("name") or "").strip()
                defs[test_id] = (class_name, name)
                break

        cs_index = _trx_cs_file_index(project_root)

        # Per-file rollup (coarse signal); per-case keys (the finer reconciliation).
        file_has_pass: dict[str, bool] = {}
        file_tainted: set[str] = set()
        passed_case_keys: set[str] = set()
        total_cases = 0
        passed_count = 0
        saw_result = False

        for result in root.iter():
            if _trx_local_tag(result.tag) != "UnitTestResult":
                continue
            saw_result = True
            outcome = (result.get("outcome") or "").strip().lower()
            test_id = result.get("testId") or ""
            class_name, def_name = defs.get(test_id, ("", ""))
            # Prefer the definition's (className, name); fall back to the result's own
            # testName so a result with no matching definition is still keyed.
            case_name = def_name or (result.get("testName") or "").strip()
            relfile = cs_index.get(_trx_class_short_name(class_name)) if class_name else None
            # Stable key: the attributed file when known, else a className-derived key
            # (NEVER credited as a FILE pass — fail-closed on unattributable cases).
            key_base = relfile if relfile is not None else (class_name or "<unknown>")
            qualified = f"{class_name}.{case_name}".strip(".") or case_name or "<case>"

            total_cases += 1
            if outcome == "passed":
                passed_count += 1
                passed_case_keys.add(f"{key_base}::{qualified}")
                if relfile is not None:
                    file_has_pass.setdefault(relfile, True)
            else:
                # failed / notexecuted / skipped / timeout / aborted / ... — none is a
                # clean pass; a skip/NotExecuted proves nothing. Taint the FILE (only
                # possible when the case is attributable; an unattributable non-pass
                # cannot be credited OR taint a file it was never matched to).
                if relfile is not None:
                    file_tainted.add(relfile)

        if not saw_result:
            raise RunnerReportUnsupported(
                f"dotnet TRX report at {report_path} contained no <UnitTestResult> "
                "entries (a TestRun with no results is unreadable, not an empty pass)"
            )

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        for relfile, has_pass in file_has_pass.items():
            if relfile in file_tainted or not has_pass:
                failed_files.add(relfile)
            else:
                passed_files.add(relfile)
        failed_files |= file_tainted  # tainted files are failed even with no pass

        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_case_keys),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )


# ── Maven Surefire XML report adapter (parallels GoTestJsonReportAdapter) ─────

#: Java's ``package a.b.c;`` declaration — at most one per file, and it MUST precede
#: any type declaration, so the first match is authoritative. Anchored on a line start
#: (``re.MULTILINE``) so it never fires inside a string literal/comment that merely
#: contains the word "package" mid-line.
_JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_$][\w$.]*)\s*;", re.MULTILINE)


def _java_file_package(text: str) -> str:
    """The declared ``package a.b.c;`` of a Java source file, or ``""`` (default package).

    A property of the file's OWN text — never inferred from its path — so it is exact
    rather than a layout guess: Java requires the package statement to match the
    classpath-relative directory the file lives under, but WHICH declared root a file
    is relative to (``src/test/java`` vs. a second e2e root) is a build-configuration
    fact this function does not need to know. A file with no ``package`` line is in
    Java's default (unnamed) package.
    """
    match = _JAVA_PACKAGE_RE.search(text)
    return match.group(1).strip() if match else ""


def _surefire_class_file_index(project_root: Path) -> dict[str, str]:
    """Index ``FQCN → relfile`` over every ``.java`` file in the tree (tree-scanned).

    Replaces a single-root path TEMPLATE (``src/test/java/{pkg}/{cls}.java``) with the
    IDENTITY→FILE ATTRIBUTION NORM this module's docstring states: reuse the SHARED
    test-file discovery (:func:`_iter_test_files`, the same glob the inventory / VB
    audit / Go index consume — ``.java`` is in its suffix table), then for each file
    read its OWN ``package`` declaration (:func:`_java_file_package`) and pair it with
    the file's basename (stem) to form the fully-qualified class name key:
    ``a.b.c.Stem`` (or bare ``Stem`` for the default package). A Failsafe-executed
    ``tests/e2e/java/**`` class attributes correctly even though it is nowhere near
    ``src/test/java`` — the index has no notion of "the" test root at all; it only
    knows what a real, discovered file's own declaration says. Determinism on a
    (build-breaking, hence practically impossible) duplicate FQCN: the first file in
    sorted-path order wins — the SAME tie-break :func:`_trx_cs_file_index` uses.
    Unreadable files contribute nothing (fail-closed: their classes then read as
    unattributable, never a false attribution).
    """
    index: dict[str, str] = {}
    for path in sorted(_iter_test_files(project_root, test_dirs=None)):
        rel = _norm_test_path(_rel_path(path, project_root))
        if not rel.endswith(".java"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        stem = rel.rsplit("/", 1)[-1][: -len(".java")]
        if not stem:
            continue
        package = _java_file_package(text)
        fqcn = f"{package}.{stem}" if package else stem
        if fqcn not in index:
            index[fqcn] = rel
    return index


def _iter_surefire_report_files(report_path: Path) -> list[Path]:
    """Resolve a Surefire ``report_path`` (a DIR or a FILE) to its XML report files.

    Surefire writes one ``target/surefire-reports/TEST-<class>.xml`` per test class.
    The campaign may point ``report_path`` at the directory OR at a single file:

    * a directory → glob ``TEST-*.xml`` first (Surefire's canonical XML naming);
      fall back to ``*.xml`` if no ``TEST-*.xml`` exists (some setups rename), so a
      non-standard layout is still read rather than silently treated as empty;
    * a file → that one file.

    Returns a sorted list (deterministic order); empty when the path does not exist
    or a directory holds no XML (the caller raises :class:`RunnerReportUnsupported`
    on empty — "nothing parseable" is never silently "nothing ran").
    """
    if report_path.is_dir():
        files = sorted(report_path.glob("TEST-*.xml"))
        if not files:
            files = sorted(report_path.glob("*.xml"))
        return files
    if report_path.is_file():
        return [report_path]
    return []


def _iter_surefire_testsuites(root: ET.Element):
    """Yield every ``<testsuite>`` element from a parsed Surefire XML root.

    A Surefire file's root is normally a single ``<testsuite>``; a ``<testsuites>``
    wrapper (aggregated reports) holds many. Yields the root itself when it IS a
    testsuite, plus any nested ``<testsuite>`` descendants — so both shapes parse.
    """
    if root.tag == "testsuite":
        yield root
    for suite in root.iter("testsuite"):
        if suite is not root:
            yield suite


@dataclass(frozen=True)
class SurefireXmlReportAdapter:
    """Maven Surefire XML reporter adapter (the stack ``surefire-xml`` report).

    ``mvn test`` writes ``target/surefire-reports/TEST-<class>.xml`` — one file per
    test CLASS. Each ``<testsuite>`` has ``<testcase classname=... name=... />``
    children; a testcase with a child ``<failure>`` or ``<error>`` FAILED, a child
    ``<skipped>`` was SKIPPED (NOT a pass — a skip proves nothing, the SAME rule the
    go-test-json / vitest adapters and the static authenticity gate apply), anything
    else PASSED.

    THE FILE BRIDGE (anti-false-green core). Surefire reports a ``(classname, name)``
    — NOT a file path — while the gate reconciles at FILE granularity. We resolve the
    top-level classname (``$``-stripped) to a relfile via a TREE-SCANNED index
    (:func:`_surefire_class_file_index` — the IDENTITY→FILE ATTRIBUTION NORM this
    module's docstring states), built from every real ``.java`` file's OWN ``package``
    declaration — never a single assumed root like ``src/test/java``. This is what
    lets a Failsafe-executed ``tests/e2e/java/**`` class attribute correctly (it is
    discovered and indexed exactly like any other test file). If a classname does not
    join to any discovered file (unattributable — e.g. a generated/conditional class,
    or a genuinely non-standard layout), the case is NOT credited as a pass for any
    file (fail-closed: a pass you cannot attribute is never a green VB) — it still
    counts toward ``total_cases`` so the report is not deemed empty.

    SKIP/FAIL granularity (mirrors the go/vitest "any non-pass taints the FILE"):
    a test file is in ``executed_passed_files`` only when it had ≥1 passed case AND
    no failed/skipped case attributed to it; ANY failure OR skip taints it into
    ``executed_failed_files``. ``executed_passed_cases`` carries
    ``"<relfile>::<classname>#<name>"`` keys for the passed cases.

    Raises :class:`RunnerReportUnsupported` on a structurally-unreadable report (no
    XML files found / NO file parsed / a parsed root that is not a testsuite) — never
    silently treating "nothing parseable" as "nothing ran" (a false-green). Per-file
    parse errors are tolerated as long as at least ONE file parsed (mirrors the
    go-test-json tolerance of non-JSON lines); if NO file parses, the report is
    unreadable → Unsupported.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        report_files = _iter_surefire_report_files(report_path)
        if not report_files:
            raise RunnerReportUnsupported(
                f"surefire XML report has no XML files at {report_path} "
                "(a missing/empty report is unreadable, not an empty pass)"
            )

        # Tree-scanned classname→file index (built ONCE per parse; see the
        # IDENTITY→FILE ATTRIBUTION NORM in this module's docstring). Replaces a
        # single-root path template so a class discovered under ANY declared test
        # root (not just src/test/java) attributes correctly.
        class_index = _surefire_class_file_index(project_root)

        # Per-file rollup at TWO granularities (parallels GoTestJsonReportAdapter):
        #  * PER-FILE coarse signal: a file is passed iff ≥1 pass AND no fail/skip.
        #  * PER-CASE keys: each PASSED case contributes "<relfile>::<class>#<name>".
        file_passed: dict[str, bool] = {}   # rel → had ≥1 passed case
        file_tainted: set[str] = set()      # any fail/skip ⇒ not a clean file
        passed_case_keys: set[str] = set()
        total_cases = 0
        passed_count = 0
        parsed_any = False

        for xml_file in report_files:
            try:
                tree = ET.parse(xml_file)
            except (ET.ParseError, OSError):
                # Tolerate a single unparseable file (mirrors go-test-json's tolerance
                # of non-JSON lines) — but only as long as SOME file parses; if none
                # do, the empty-result guard below raises Unsupported (never a silent
                # empty pass).
                continue
            root = tree.getroot()
            suites = list(_iter_surefire_testsuites(root))
            if not suites:
                # A well-formed XML whose root is not a <testsuite> is structurally
                # wrong for Surefire — skip it (if NO file yields a testsuite, the
                # empty guard turns the whole report into Unsupported).
                continue
            parsed_any = True
            for suite in suites:
                for case in suite.findall("testcase"):
                    classname = (case.get("classname") or "").strip()
                    name = (case.get("name") or "").strip()
                    total_cases += 1
                    top_level = classname.split("$", 1)[0]  # inner class → enclosing file
                    relfile = class_index.get(top_level) if top_level else None
                    failed = (
                        case.find("failure") is not None
                        or case.find("error") is not None
                    )
                    skipped = case.find("skipped") is not None
                    if failed or skipped:
                        # fail OR skip — a skip proves nothing (go/vitest/authenticity
                        # parity), a fail is honest. Either taints the FILE (when it is
                        # attributable); an unattributable taint cannot be credited to
                        # any file, so it simply yields no passed case.
                        if relfile is not None:
                            file_tainted.add(relfile)
                        continue
                    # PASSED.
                    passed_count += 1
                    if relfile is not None:
                        file_passed[relfile] = True
                        passed_case_keys.add(f"{relfile}::{classname}#{name}")
                    # else: a pass we cannot attribute to a real file is NOT credited
                    # (fail-closed — never a green VB you cannot point at a file for).

        if not parsed_any:
            raise RunnerReportUnsupported(
                f"surefire XML report at {report_path} had no parseable <testsuite> "
                "(every file was unreadable / not a testsuite — unreadable, not an "
                "empty pass)"
            )

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        for relfile in file_passed:
            if relfile in file_tainted:
                failed_files.add(relfile)
            else:
                passed_files.add(relfile)
        failed_files |= file_tainted  # tainted files are failed even with no pass

        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_case_keys),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )

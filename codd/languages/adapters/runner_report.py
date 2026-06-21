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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from codd.operational_e2e_audit import (
    _iter_test_files,
    _rel_path,
)
from codd.vb_marker_authenticity import GoTestBlockProfile


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

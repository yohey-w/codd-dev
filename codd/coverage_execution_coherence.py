"""Coverage-execution coherence — the anti-false-green axis that binds STATIC VB
coverage to ACTUAL test EXECUTION (design: /tmp/gpt_vscope_result.txt, GPT-5.5
Pro consult 2026-06-15).

THE FALSE-GREEN THIS CLOSES (greenfield codex14)
================================================
verify resolved the test command by the SUT's ``package.json`` script priority
(``detect_test_command``: ``test:unit`` > ``test`` > ``test:e2e``), ran
``test:unit`` (39 unit tests), exited 0, and declared "verification passed" —
while 28 declared verifiable behaviors (VBs) were covered ONLY by
``tests/e2e/*.e2e.test.ts`` files that ``test:unit`` NEVER executes. The static
VB coverage gate (:mod:`codd.verifiable_behavior_audit`) saw those ``codd: covers
vb=`` markers and called the behaviors "covered"; verify never ran them. COVERAGE
and EXECUTION were two SEPARATE proof systems, so "covered but UNEXECUTED" passed
green.

THE INVARIANT (design section B/E)
==================================
For every UNBLOCKED verifiable behavior ``v``::

    ∃ test t:  marker covers vb=v  ∧  authentic(t)
               ∧  t ∈ verify-campaign selection
               ∧  t actually executed  ∧  t passed

``unblocked_VB − verified_VB ≠ ∅  →  HARD FAIL``. Static coverage ALONE never
makes a VB pass — its covering test must have RUN and PASSED in the harness-owned
verify campaign.

THE THREE PIECES (all per-profile; gate logic is language-agnostic)
==================================================================
1. **Verify campaign** — :class:`~codd.project_types.VerifyCampaignSpec` on the
   :class:`~codd.project_types.LayoutProfile`. The harness OWNS the verification
   command (runs the WHOLE VB surface — unit AND e2e — and emits a machine-
   readable report); ``detect_test_command``'s one-SUT-script pick is NOT the
   pass authority here. (Brownfield/fixer watch/partial-run keep
   ``detect_test_command`` — UNCHANGED.)
2. **TestInventory** — the SINGLE source of test files every gate consumes
   (:class:`TestInventory`). It reuses the SAME discovery + suffix + level
   classification the VB scan / authenticity / e2e-contract audits already use
   (:func:`codd.operational_e2e_audit._iter_test_files` /
   ``_classify_test_level`` / ``_TEST_SUFFIXES``), so all gates see ONE glob —
   never a per-gate glob that lets e2e files be visible to one gate and invisible
   to another (the codex14 ``0 e2e scanned`` half of the bug).
3. **Runner-report adapter** — per-profile normalization of the campaign report
   into the set of executed + passed test FILES (and, when available, cases).
   ``vitest-json`` and ``go-test-json`` are implemented; ``pytest-junit-xml`` is a
   documented extension point (:func:`resolve_runner_report_adapter`). A report
   the gate cannot read is an EXPLICIT degrade/observability error — never a
   silent green. A runner that reports per-test-case identity (Go) reconciles each
   VB at TEST granularity via :class:`RunnerExecution.executed_passed_cases`; a
   file-level runner (vitest/pytest) reconciles at FILE granularity — both share
   the same gate, selected by the adapter's ``produces_test_case_identity``.

GENERALITY (see ``feedback_codd_generality_preservation``): the gate logic here
is language-agnostic. Every language-specific operation — the campaign command,
the report format, the report parse — is a per-profile spec/adapter. A stack
whose profile declares ``verify_campaign=None`` (Python today) makes the whole
gate a strict NO-OP for it; its existing verify-stage coherence gates remain its
backstop, UNCHANGED.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from codd.operational_e2e_audit import (
    _classify_test_level,
    _iter_test_files,
    _load_optional_config,
    _rel_path,
    _resolve_vb_scan_dirs,
)
from codd.verifiable_behavior_audit import (
    VBAuditReport,
    build_vb_coverage_audit,
)
from codd.vb_marker_authenticity import (
    AuthenticityReport,
    build_authenticity_report,
)


COHERENCE_CONTRACT_VERSION = "coverage-execution-coherence/v1"


# ───────────────────────────────────────────────────────────────────────────
# 1. Runner-report adapter (the ONLY language-specific report surface)
# ───────────────────────────────────────────────────────────────────────────


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

    from codd.vb_marker_authenticity import GoTestBlockProfile

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


#: report_format → adapter. A new runner registers ONE entry here + sets the
#: matching ``report_format`` on its profile's :class:`VerifyCampaignSpec`.
#: ``pytest-junit-xml`` is the remaining documented extension point (pytest JUnit
#: XML parses ``<testcase classname name>`` + ``<skipped>``/``<failure>``/``<error>``
#: children); until added it resolves to ``None`` and the gate degrades EXPLICITLY
#: for that stack (never a silent green).
_RUNNER_REPORT_ADAPTERS: dict[str, RunnerReportAdapter] = {
    "vitest-json": VitestJsonReportAdapter(),
    "go-test-json": GoTestJsonReportAdapter(),
}


def resolve_runner_report_adapter(report_format: str | None) -> RunnerReportAdapter | None:
    """The adapter for a campaign ``report_format``, or ``None`` if unregistered.

    ``None`` (an unknown / not-yet-implemented format) makes the gate degrade
    EXPLICITLY — it surfaces "this stack's campaign report has no adapter" rather
    than silently passing a build whose executions it cannot read.
    """

    if not report_format:
        return None
    return _RUNNER_REPORT_ADAPTERS.get(str(report_format).strip().lower())


def supported_runner_report_formats() -> list[str]:
    """Report formats with a registered runner adapter (deterministic order)."""
    return sorted(_RUNNER_REPORT_ADAPTERS)


# ───────────────────────────────────────────────────────────────────────────
# 2. TestInventory — the single source of test files every gate consumes
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TestInventoryEntry:
    """One test file in the unified inventory.

    ``rel_path`` is project-relative POSIX (the key every gate + the VB audit's
    ``matched_tests`` + the runner report use). ``kind`` is ``unit`` /
    ``integration`` / ``e2e`` (from the SAME ``_classify_test_level`` the
    operational audit uses; ``lower_test`` is mapped to ``unit``).
    ``runner_inclusion`` is True when the file appears in the campaign report (it
    was collected/executed). ``execution_status`` is one of
    ``executed_passed`` / ``executed_failed`` / ``not_executed``.
    """

    rel_path: str
    kind: str
    runner_inclusion: bool = False
    execution_status: str = "not_executed"

    @property
    def is_e2e(self) -> bool:
        return self.kind == "e2e"


@dataclass
class TestInventory:
    """The unified set of test files + their execution status.

    Built ONCE from the shared discovery glob (the same one the VB scan /
    authenticity / e2e-contract / operational audits consume), then annotated with
    the campaign's runner report. Every coherence decision reads from THIS — no
    gate re-globs the tree with its own pattern.
    """

    entries: dict[str, TestInventoryEntry] = field(default_factory=dict)
    #: True when a runner report was successfully parsed and applied.
    execution_applied: bool = False

    def get(self, rel_path: str) -> TestInventoryEntry | None:
        return self.entries.get(_norm_test_path(rel_path))

    @property
    def files(self) -> list[str]:
        return sorted(self.entries)

    @property
    def e2e_files(self) -> list[str]:
        return sorted(rel for rel, e in self.entries.items() if e.is_e2e)

    @property
    def executed_e2e_files(self) -> list[str]:
        return sorted(
            rel
            for rel, e in self.entries.items()
            if e.is_e2e and e.runner_inclusion
        )

    def passed(self, rel_path: str) -> bool:
        entry = self.get(rel_path)
        return entry is not None and entry.execution_status == "executed_passed"


def _norm_test_path(rel_path: str) -> str:
    return str(rel_path).replace("\\", "/").strip().lstrip("./")


def _inventory_kind(rel_path: str) -> str:
    """Map the operational audit's test level to the inventory kind vocabulary."""
    level = _classify_test_level(rel_path)
    # ``_classify_test_level`` returns "e2e" or "lower_test"; refine lower_test
    # into unit/integration by path so the inventory carries the finer kind the
    # design's TestInventory schema names, while staying on the SAME classifier.
    if level == "e2e":
        return "e2e"
    normalized = "/" + rel_path.replace("\\", "/").lower()
    if "/integration/" in normalized:
        return "integration"
    return "unit"


def build_test_inventory(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
    test_dirs: Iterable[Path | str] | None = None,
    execution: RunnerExecution | None = None,
) -> TestInventory:
    """Build the unified test inventory from the shared discovery glob.

    Discovery uses :func:`codd.operational_e2e_audit._iter_test_files` with the
    SAME scope resolution (:func:`_resolve_vb_scan_dirs`) the VB and operational
    audits use, so the inventory is byte-for-byte the file set those gates see.
    When ``execution`` is provided, each file is annotated with its runner
    inclusion + pass/fail status.
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    if test_dirs is None:
        test_dirs = _resolve_vb_scan_dirs(project_root, config)

    entries: dict[str, TestInventoryEntry] = {}
    passed_files = execution.executed_passed_files if execution else frozenset()
    failed_files = execution.executed_failed_files if execution else frozenset()
    for path in _iter_test_files(project_root, test_dirs=test_dirs):
        rel = _norm_test_path(_rel_path(path, project_root))
        if rel in entries:
            continue
        included = rel in passed_files or rel in failed_files
        if rel in passed_files:
            status = "executed_passed"
        elif rel in failed_files:
            status = "executed_failed"
        else:
            status = "not_executed"
        entries[rel] = TestInventoryEntry(
            rel_path=rel,
            kind=_inventory_kind(rel),
            runner_inclusion=included,
            execution_status=status,
        )
    return TestInventory(entries=entries, execution_applied=execution is not None)


# ───────────────────────────────────────────────────────────────────────────
# 3. The coverage-execution coherence gate (the new invariant)
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UnverifiedVB:
    """A VB that is statically covered (authentically) but NOT proven by execution."""

    vb_id: str
    description: str
    source_doc: str
    #: covering test files (static, authentic) and their per-file execution status
    covering_files: tuple[str, ...]
    #: why it is unverified: ``no_covering_test_executed`` (all covering files
    #: not_executed) / ``covering_test_failed`` (a covering file ran but failed) /
    #: ``no_authentic_cover`` (its only markers were inauthentic — already a
    #: separate gate's failure, surfaced here for completeness).
    reason: str

    @property
    def message(self) -> str:
        files = ", ".join(self.covering_files) if self.covering_files else "(none)"
        desc = f" — {self.description}" if self.description else ""
        if self.reason == "covering_test_failed":
            why = (
                "its covering test ran in the verify campaign but did NOT pass — a "
                "failing test does not prove the behavior."
            )
        elif self.reason == "no_authentic_cover":
            why = (
                "it has no AUTHENTIC covering test (its covers marker(s) are not "
                "credible — see the marker-authenticity gate)."
            )
        else:
            why = (
                "its covering test(s) were NOT executed by the verify campaign — "
                "static coverage alone does not prove the behavior. The covering "
                "test must actually RUN and PASS (e.g. an e2e-only VB whose e2e "
                "suite the campaign never ran)."
            )
        return (
            f"{self.vb_id}{desc} (declared in {self.source_doc}; covering test(s): "
            f"{files}): {why}"
        )


@dataclass(frozen=True)
class CoherenceObservabilityError:
    """A harness observability failure (NOT a SUT defect): the campaign report is
    unreadable, or an e2e surface exists but the campaign scanned ZERO e2e files."""

    kind: str  # "report_unreadable" | "no_adapter" | "e2e_scan_zero"
    message: str


@dataclass
class CoherenceReport:
    """Verdict of the coverage-execution coherence gate."""

    version: str
    applicable: bool
    unverified_vbs: list[UnverifiedVB] = field(default_factory=list)
    observability_errors: list[CoherenceObservabilityError] = field(default_factory=list)
    #: counts for diagnostics / the success line
    unblocked_count: int = 0
    verified_count: int = 0
    executed_files: int = 0
    e2e_files: int = 0
    executed_e2e_files: int = 0
    detail: str = ""

    @property
    def passed(self) -> bool:
        return not self.unverified_vbs and not self.observability_errors


def _authentic_cover_files(
    vb_id: str,
    audit: VBAuditReport,
    authenticity: AuthenticityReport,
) -> tuple[set[str], bool]:
    """Covering test FILES for ``vb_id`` that carry an AUTHENTIC marker.

    Returns ``(files, had_any_marker)``. ``files`` are the audit's matched test
    files MINUS those whose marker for this VB is an authenticity violation
    (orphan / skipped / unattached / no-assertion). ``had_any_marker`` is True
    when the VB had at least one covering file in the static audit (so the gate
    can distinguish "no marker at all" — a coverage-gate problem — from "marker
    present but inauthentic"). Authenticity is reconciled at FILE granularity
    (the static audit's ``matched_tests`` is file-level), conservatively: a file
    is dropped only when EVERY one of its markers for this VB is inauthentic, so a
    file with one good and one bad marker for the same VB still counts (the bad
    marker is the authenticity gate's separate failure).
    """

    matched: set[str] = set()
    for row in audit.rows:
        if _vb_eq(row.vb_id, vb_id):
            matched = {_norm_test_path(p) for p in row.matched_tests}
            break
    if not matched:
        return set(), False

    # Files where this VB's marker(s) are ALL inauthentic → drop from evidence.
    inauthentic_for_vb: dict[str, int] = {}
    for v in authenticity.violations:
        if v.kind == "hook":
            continue
        if _vb_eq(v.vb_id, vb_id):
            inauthentic_for_vb[_norm_test_path(v.path)] = (
                inauthentic_for_vb.get(_norm_test_path(v.path), 0) + 1
            )
    # A file is dropped only if it appears in matched AND has an inauthentic marker
    # for this VB AND no offsetting evidence. The static audit cannot tell us how
    # many GOOD markers a file has, so we conservatively keep the file unless the
    # authenticity gate flagged it for this VB (the authenticity gate is itself a
    # HARD gate run alongside, so a truly inauthentic-only VB is already failing
    # there; dropping here just prevents it from masquerading as execution-proven).
    authentic = {f for f in matched if f not in inauthentic_for_vb}
    return authentic, True


def _norm_vb_key(vb_id: str) -> str:
    """The audit's canonical VB id (so the per-case map keys match a row's id)."""
    from codd.verifiable_behavior_audit import _normalize_vb_id

    return _normalize_vb_id(vb_id)


def _authentic_cover_case_keys(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
    vb_audit: VBAuditReport,
    authenticity: AuthenticityReport,
) -> dict[str, set[str]] | None:
    """Per-VB ``{normalized_vb_id → {"<relfile>::TestFunc", ...}}`` covering case keys.

    Returns ``None`` (⇒ the gate uses FILE-level reconciliation, unchanged) UNLESS
    this stack supports per-test-case reconciliation: its runner adapter declares
    ``produces_test_case_identity()`` True (Go) AND its test-block profile parses
    blocks whose label is the runner-case identity (``TestFunc``). For every AUTHENTIC
    ``codd: covers vb=`` marker, the covering case key is ``"<relfile>::<top-level
    TestFunc>"`` — the file the marker sits in plus the top-level test function of the
    block it attaches to, using the SAME ``_attached_block`` attachment the
    authenticity gate uses (so static coverage and execution reconciliation agree).
    A marker that does not attach to an executable block, or whose block is
    inauthentic for this VB, contributes NO key (it is not a credible covering case).

    This is the static half of the ``(Package, Test)``→case-key join the Go runner
    adapter performs; matching keys on both sides is what lets a passed ``TestA``
    prove its VB while a sibling ``TestB`` skip in the same file does not drag it down.
    """

    adapter_getter = getattr(profile, "runner_report_adapter", None) if profile is not None else None
    adapter = adapter_getter() if callable(adapter_getter) else None
    if adapter is None or not _adapter_produces_case_identity(adapter):
        return None
    block_getter = getattr(profile, "test_block_profile", None)
    test_block_profile = block_getter() if callable(block_getter) else None
    if test_block_profile is None:
        return None

    from codd.vb_marker_authenticity import _attached_block, _scan_cover_markers_with_lines

    # Authentic-marker filter: a (path, vb) flagged inauthentic (skipped/unattached/
    # no-assertion/orphan) contributes no covering case — same files-drop discipline
    # as ``_authentic_cover_files`` but at marker granularity.
    inauthentic: set[tuple[str, str]] = {
        (_norm_test_path(v.path), _norm_vb_key(v.vb_id))
        for v in authenticity.violations
        if v.kind != "hook"
    }

    keys_by_vb: dict[str, set[str]] = {}
    for path in _iter_test_files(project_root, test_dirs=_resolve_vb_scan_dirs(project_root, config)):
        rel = _norm_test_path(_rel_path(path, project_root))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        markers = _scan_cover_markers_with_lines(text, rel)
        if not markers:
            continue
        if not test_block_profile.handles_file(rel):
            continue
        try:
            blocks = test_block_profile.parse_test_blocks(text)
        except Exception:  # noqa: BLE001 — an unparseable file contributes no keys.
            blocks = []
        if not blocks:
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for marker in markers:
            vb_key = _norm_vb_key(marker.vb_id)
            if (rel, vb_key) in inauthentic:
                continue  # this VB's marker here is not credible coverage
            block = _attached_block(marker.line, text, blocks)
            if block is None or not block.is_executable:
                continue
            test_func = (block.label or "").split("/", 1)[0].strip()
            if not test_func:
                continue
            # The runner-case key the Go adapter emits is module-relative-dir + func;
            # here the relfile already carries the dir, so the key is rel-file based —
            # we key by ``<relfile>::TestFunc`` (the adapter writes the SAME, having
            # resolved (Package, Test) back to relfile via the static index).
            keys_by_vb.setdefault(vb_key, set()).add(f"{rel}::{test_func}")
    return keys_by_vb


def _adapter_produces_case_identity(adapter: Any) -> bool:
    """Whether ``adapter`` declares per-test-case identity (best-effort, default False).

    A runner adapter opts into per-case reconciliation by defining
    ``produces_test_case_identity() -> True`` (the Go adapter). vitest/pytest do not
    define it ⇒ False ⇒ they keep the file-level branch (byte-identical)."""
    getter = getattr(adapter, "produces_test_case_identity", None)
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001
        return False


def build_coherence_report(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
    profile: Any = None,
    execution: RunnerExecution | None = None,
    inventory: TestInventory | None = None,
    vb_audit: VBAuditReport | None = None,
    authenticity: AuthenticityReport | None = None,
) -> CoherenceReport:
    """Reconcile static VB coverage against actual test execution.

    The new invariant: every UNBLOCKED VB must have ≥1 AUTHENTIC covering test
    file that the verify campaign EXECUTED and PASSED. ``unblocked − verified ≠ ∅``
    is a HARD FAIL (each surfaced as an :class:`UnverifiedVB`).

    ``execution`` is the parsed campaign report (None ⇒ no campaign ran: every
    covering file is "not executed", so any covered-unblocked VB is unverified —
    this is the e2e-not-run case made into a hard failure). ``profile`` (a
    :class:`~codd.project_types.LayoutProfile`) supplies the e2e modality for the
    observability check.
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    if vb_audit is None:
        vb_audit = build_vb_coverage_audit(project_root, config=config)
    if authenticity is None:
        authenticity = build_authenticity_report(project_root, config=config, profile=profile)
    if inventory is None:
        inventory = build_test_inventory(project_root, config=config, execution=execution)

    # Applicability: the gate is meaningful only when there are declared VBs to
    # reconcile. (The CALLER decides whether the STACK has a campaign; a stack
    # with no campaign passes None execution and the gate still runs — but then a
    # covered VB with no executed covering test is correctly unverified. The
    # greenfield wiring only INVOKES this gate for a stack with a campaign; see
    # the pipeline.)
    if not vb_audit.rows:
        return CoherenceReport(
            version=COHERENCE_CONTRACT_VERSION,
            applicable=False,
            detail="no verifiable behaviors declared — coverage-execution coherence is N/A",
        )

    # PER-CASE reconciliation capability (GPT-5.5 design, dogfood go-test-json):
    # a runner whose adapter emits per-test-case identities AND a static profile that
    # can map a VB marker to the SAME runner-case key reconciles a VB by its covering
    # (file, test-case) — NOT by file status. This is REQUIRED for Go, where many
    # independent ``func TestXxx`` share one ``_test.go`` (file-level status would
    # false-RED a passed VB whose sibling skipped). It is OFF for vitest/pytest (their
    # adapters expose no per-case identity), which keep the byte-identical file branch
    # below. The static covering case keys reuse the SAME marker→block attachment the
    # authenticity gate uses (no re-invented attachment ⇒ coverage + execution agree).
    case_keys_by_vb = _authentic_cover_case_keys(
        project_root, config=config, profile=profile, vb_audit=vb_audit, authenticity=authenticity
    )
    case_reconciliation = (
        case_keys_by_vb is not None
        and execution is not None
        and execution.test_level_available
    )

    unblocked = [row for row in vb_audit.rows if row.coverage_status != "blocked"]
    unverified: list[UnverifiedVB] = []
    verified_count = 0
    for row in unblocked:
        authentic_files, had_marker = _authentic_cover_files(row.vb_id, vb_audit, authenticity)
        if not had_marker:
            # No covering marker at all → a COVERAGE-gate failure, not this gate's
            # (the coverage gate already fails it). Do not double-report here.
            unverified.append(
                UnverifiedVB(
                    vb_id=row.vb_id,
                    description=row.description,
                    source_doc=row.source_doc,
                    covering_files=(),
                    reason="no_covering_test_executed",
                )
            )
            continue
        if not authentic_files:
            unverified.append(
                UnverifiedVB(
                    vb_id=row.vb_id,
                    description=row.description,
                    source_doc=row.source_doc,
                    covering_files=tuple(sorted(_norm_test_path(p) for p in row.matched_tests)),
                    reason="no_authentic_cover",
                )
            )
            continue

        if case_reconciliation:
            # VB verified iff ANY authentic covering (file, top-level TestFunc) case
            # key passed in the runner report. A skipped/failed/missing covering case
            # never appears in ``executed_passed_cases`` ⇒ correctly unverified; a
            # sibling test's outcome in the same file is irrelevant (the false-RED the
            # file branch would cause for Go is avoided).
            covering_cases = case_keys_by_vb.get(_norm_vb_key(row.vb_id), set())
            assert execution is not None  # case_reconciliation implies execution
            passed = sorted(k for k in covering_cases if k in execution.executed_passed_cases)
            if passed:
                verified_count += 1
                continue
            # Distinguish "ran but failed/skipped" from "never ran" for the message: a
            # covering file that the runner included (failed/skipped) → failed; else
            # never ran. (Falls back to file-level inclusion for the reason only.)
            ran_files = sorted(
                f for f in authentic_files
                if (entry := inventory.get(f)) is not None and entry.runner_inclusion
            )
            reason = "covering_test_failed" if ran_files else "no_covering_test_executed"
            unverified.append(
                UnverifiedVB(
                    vb_id=row.vb_id,
                    description=row.description,
                    source_doc=row.source_doc,
                    covering_files=tuple(sorted(covering_cases) or sorted(authentic_files)),
                    reason=reason,
                )
            )
            continue

        # FILE-level reconciliation (vitest/pytest — UNCHANGED, byte-identical).
        # Verified iff ANY authentic covering file executed AND passed.
        passed_files = sorted(f for f in authentic_files if inventory.passed(f))
        if passed_files:
            verified_count += 1
            continue
        # Not verified: distinguish "ran but failed" from "never ran" for the msg.
        ran_files = sorted(
            f for f in authentic_files
            if (entry := inventory.get(f)) is not None and entry.runner_inclusion
        )
        reason = "covering_test_failed" if ran_files else "no_covering_test_executed"
        unverified.append(
            UnverifiedVB(
                vb_id=row.vb_id,
                description=row.description,
                source_doc=row.source_doc,
                covering_files=tuple(sorted(authentic_files)),
                reason=reason,
            )
        )

    observability = _observability_errors(
        project_root,
        config=config,
        profile=profile,
        inventory=inventory,
        authenticity=authenticity,
        vb_audit=vb_audit,
        execution=execution,
    )

    report = CoherenceReport(
        version=COHERENCE_CONTRACT_VERSION,
        applicable=True,
        unverified_vbs=unverified,
        observability_errors=observability,
        unblocked_count=len(unblocked),
        verified_count=verified_count,
        executed_files=len(inventory.executed_e2e_files)
        + sum(1 for e in inventory.entries.values() if e.runner_inclusion and not e.is_e2e),
        e2e_files=len(inventory.e2e_files),
        executed_e2e_files=len(inventory.executed_e2e_files),
    )
    report.detail = (
        f"{verified_count}/{len(unblocked)} unblocked VB(s) execution-verified; "
        f"e2e files {len(inventory.executed_e2e_files)}/{len(inventory.e2e_files)} executed"
    )
    return report


def _observability_errors(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
    inventory: TestInventory,
    authenticity: AuthenticityReport,
    vb_audit: VBAuditReport,
    execution: RunnerExecution | None,
) -> list[CoherenceObservabilityError]:
    """E2E observability hard-fail (design section D).

    ``0 e2e scanned`` is itself a harness failure when an e2e surface EXISTS. The
    surface exists when ANY of: an e2e test file is in the inventory; a VB covers-
    marker sits in an e2e file (a covering file the VB audit matched whose kind is
    e2e); or the profile declares an e2e modality. If a surface exists but the
    campaign executed ZERO e2e files, that is an observability error (the campaign
    did not run the e2e layer it should have), NOT a pass.
    """

    errors: list[CoherenceObservabilityError] = []

    e2e_in_inventory = inventory.e2e_files
    # A VB covering file classified e2e (markers in an e2e file).
    vb_e2e_files = sorted(
        {
            _norm_test_path(p)
            for row in vb_audit.rows
            for p in row.matched_tests
            if _inventory_kind(_norm_test_path(p)) == "e2e"
        }
    )

    # A CONCRETE e2e surface is required to assert "0 e2e scanned is a failure":
    # actual e2e test files in the tree, OR VB covers-markers living in e2e files.
    # The profile's declared e2e MODALITY is NOT a sole trigger — the ``generic``
    # baseline declares ``e2e_modality="cli"`` for EVERY project, so firing on
    # modality alone would false-RED a pure-unit project that legitimately has no
    # e2e files. Modality only CORROBORATES (it sharpens the message) when a
    # concrete surface already exists; it never manufactures one.
    concrete_surface = bool(e2e_in_inventory or vb_e2e_files)
    if not concrete_surface:
        return errors
    modality_e2e = _profile_has_e2e_modality(project_root, config=config, profile=profile)

    # When execution evidence is present, an existing e2e surface MUST show ≥1
    # executed e2e file; zero is the "0 e2e scanned" observability failure.
    if execution is not None and not inventory.executed_e2e_files:
        evidence = []
        if e2e_in_inventory:
            evidence.append(f"{len(e2e_in_inventory)} e2e test file(s) present")
        if vb_e2e_files:
            evidence.append(f"{len(vb_e2e_files)} VB marker(s) in e2e file(s)")
        if modality_e2e:
            evidence.append("profile declares an e2e modality")
        errors.append(
            CoherenceObservabilityError(
                kind="e2e_scan_zero",
                message=(
                    "e2e observability failure: an e2e surface exists ("
                    + "; ".join(evidence)
                    + ") but the verify campaign executed 0 e2e file(s). The campaign "
                    "must run the e2e layer (check the campaign command's test root / "
                    "the runner's collection include for the .e2e.* convention) — a "
                    "0-e2e-scanned run is a harness error, not a pass."
                ),
            )
        )
    return errors


def _profile_has_e2e_modality(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
) -> bool:
    """Whether the project's configured type declares a non-trivial e2e modality.

    Reuses the SAME modality resolution the e2e-contract gate uses (configured
    project type → capability profile ``e2e_modality``). ``browser`` / ``cli`` /
    ``device`` are e2e surfaces; ``none`` is not. Best-effort: any resolution
    failure returns False (no spurious observability error from an undecidable
    modality).
    """

    try:
        from codd.project_types import load_capabilities, resolve_project_type

        cfg = config if config is not None else _load_optional_config(project_root)
        project_section = cfg.get("project") if isinstance(cfg.get("project"), dict) else {}
        configured = (
            project_section.get("type") or project_section.get("project_type")
            if isinstance(project_section, dict)
            else None
        )
        resolved_type, _ = resolve_project_type(configured, None, project_root)
        modality = load_capabilities(resolved_type, project_root).e2e_modality
        return str(modality).strip().lower() in ("browser", "cli", "device")
    except Exception:  # noqa: BLE001 — undecidable modality ⇒ no observability claim.
        return False


def format_coherence_feedback(report: CoherenceReport) -> str:
    """Render coherence failures as SUT-facing rerun feedback.

    Like the coverage / authenticity feedback, this is about making the COVERING
    TEST actually run + pass under the harness campaign — never about adding a
    marker or weakening the gate.
    """

    lines: list[str] = []
    if report.observability_errors:
        lines.append(
            "The verify campaign did not observe part of the test surface it must:"
        )
        for err in report.observability_errors:
            lines.append(f"- {err.message}")
        lines.append("")
    if report.unverified_vbs:
        lines.append(
            "Some declared verifiable behaviors are statically covered but were NOT "
            "proven by EXECUTION. A behavior is only verified when its covering test "
            "actually RAN and PASSED in the harness-owned verify campaign (unit AND "
            "e2e). Fix each by ensuring the covering test executes and passes under "
            "the campaign (do NOT remove the marker, weaken the campaign, or mark the "
            "test skipped):"
        )
        for vb in report.unverified_vbs:
            lines.append(f"- {vb.message}")
    return "\n".join(lines)


def render_coherence_markdown(report: CoherenceReport) -> str:
    """Render the coherence report as Markdown (for ``codd test audit`` etc.)."""

    lines = [
        "# Coverage-Execution Coherence",
        "",
        f"- Contract: {report.version}",
        f"- Applicable: {report.applicable}",
        f"- Unblocked VBs execution-verified: {report.verified_count}/{report.unblocked_count}",
        f"- E2E files executed: {report.executed_e2e_files}/{report.e2e_files}",
        f"- Execution-unverified VBs: {len(report.unverified_vbs)}",
        f"- Observability errors: {len(report.observability_errors)}",
    ]
    if report.observability_errors:
        lines.extend(["", "## Observability Errors", ""])
        for err in report.observability_errors:
            lines.append(f"- [{err.kind}] {err.message}")
    if report.unverified_vbs:
        lines.extend(
            [
                "",
                "## Execution-Unverified Verifiable Behaviors",
                "| VB | Source Doc | Covering Test(s) | Reason |",
                "| --- | --- | --- | --- |",
            ]
        )
        for vb in report.unverified_vbs:
            files = ", ".join(vb.covering_files) if vb.covering_files else "-"
            lines.append(
                f"| {vb.vb_id} | {vb.source_doc} | {files} | {vb.reason} |"
            )
    lines.append("")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# Small shared helpers
# ───────────────────────────────────────────────────────────────────────────


def _vb_eq(a: str, b: str) -> bool:
    """Whether two VB ids match under the audit's canonical normalization."""
    from codd.verifiable_behavior_audit import _normalize_vb_id

    return _normalize_vb_id(a) == _normalize_vb_id(b)


# ───────────────────────────────────────────────────────────────────────────
# Campaign execution + the greenfield-verify gate entry point
# ───────────────────────────────────────────────────────────────────────────


#: Bounded wall-clock for the verify campaign. The campaign runs the WHOLE test
#: surface (unit + e2e), so it gets a generous-but-bounded budget; an override
#: lives under ``verify.campaign_timeout_seconds``.
DEFAULT_CAMPAIGN_TIMEOUT_SECONDS = 1200.0


class CampaignError(RuntimeError):
    """The verify campaign could not run to a parseable report (harness error)."""


@dataclass
class CampaignRun:
    """Outcome of executing a verify campaign command."""

    command: str
    exit_code: int
    report_path: Path
    execution: RunnerExecution
    output_tail: str = ""


def _campaign_timeout_seconds(config: dict[str, Any] | None) -> float:
    verify = (config or {}).get("verify")
    if isinstance(verify, dict):
        raw = verify.get("campaign_timeout_seconds")
        try:
            seconds = float(raw)
            if seconds > 0:
                return seconds
        except (TypeError, ValueError):
            pass
    return DEFAULT_CAMPAIGN_TIMEOUT_SECONDS


def run_verify_campaign(
    project_root: Path | str,
    profile: Any,
    *,
    config: dict[str, Any] | None = None,
    echo: Callable[[str], None] = print,
) -> CampaignRun:
    """Execute the profile-owned verify campaign and parse its report.

    Runs ``profile.verify_campaign``'s resolved command from the project root,
    then parses the machine-readable report through the per-profile runner
    adapter. ANTI-FALSE-GREEN: a campaign that produced NO report, an UNREADABLE
    report, or an empty-execution report (collected 0 tests) is a :class:`CampaignError`
    (a harness/observability failure), NEVER a silent pass. The command's own exit
    code is captured AND consulted by ``enforce_campaign_clean_execution``
    (contract verify.campaign.clean_execution.v1): a non-zero exit, or ANY failed
    executed test file, hard-fails there — independent of the per-VB coherence
    gate, which alone would miss a failing test that covers no declared VB.

    Caller contract: only invoke when ``profile.verify_campaign`` is not None and
    ``profile.runner_report_adapter()`` resolves. A None campaign/adapter is the
    caller's NO-OP branch (the gate does not apply to that stack).
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        raise CampaignError("profile declares no verify_campaign (caller must NO-OP)")
    adapter = profile.runner_report_adapter()
    if adapter is None:
        raise CampaignError(
            f"no runner-report adapter for campaign report_format "
            f"{getattr(campaign, 'report_format', None)!r} — cannot read campaign executions"
        )

    test_root = getattr(profile, "test_root", "tests")
    report_path = project_root / campaign.report_relpath
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # A stale report from a prior run must never be mistaken for this run's
    # evidence — remove it first so a campaign that silently writes nothing fails
    # the "no report" check instead of reusing old executions.
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    command = campaign.resolve_command(
        test_root=test_root, report_path=campaign.report_relpath
    )
    timeout = _campaign_timeout_seconds(config)
    echo(f"[greenfield] verify: running coverage-execution campaign — {command}")
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CampaignError(
            f"verify campaign timed out after {timeout:g}s: {command}"
        ) from exc

    output_tail = _output_tail(completed.stdout, completed.stderr)
    if not report_path.is_file():
        raise CampaignError(
            f"verify campaign produced no report at {campaign.report_relpath} "
            f"(exit {completed.returncode}): {command}\n{output_tail}"
        )
    try:
        execution = adapter.parse(report_path, project_root=project_root)
    except RunnerReportUnsupported as exc:
        raise CampaignError(str(exc)) from exc
    if execution.total_cases == 0 and not execution.executed_files:
        # The campaign ran but the report shows no executed tests at all — the
        # JS-runner "collected 0 tests" hard-fail, generalized to the report.
        raise CampaignError(
            f"verify campaign collected/ran 0 tests (report at {campaign.report_relpath} "
            f"is empty): {command}\n{output_tail}"
        )
    return CampaignRun(
        command=command,
        exit_code=completed.returncode,
        report_path=report_path,
        execution=execution,
        output_tail=output_tail,
    )


def coherence_gate_applies(profile: Any) -> bool:
    """Whether the coverage-execution coherence gate applies to this stack.

    True only when the profile declares a verify campaign AND a runner-report
    adapter resolves for its format. Any other case (no campaign — Python today;
    or a campaign whose format has no adapter yet) is a NO-OP for the gate — the
    caller skips it (the no-adapter case is surfaced separately as an explicit
    degrade where it matters, never a silent green for a stack that HAS a campaign
    but no reader).
    """

    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        return False
    getter = getattr(profile, "runner_report_adapter", None)
    if not callable(getter):
        return False
    try:
        return getter() is not None
    except Exception:  # noqa: BLE001
        return False


def certify_verify_campaign_observable(profile: Any) -> None:
    """HARD GATE (contract verify.campaign.observable.v1; GPT round-2 §3.1).

    A profile that DECLARES a verify campaign but whose ``runner_report_adapter()``
    is ``None`` cannot have its executions read — the campaign would run and the
    coherence gate would silently NO-OP (``coherence_gate_applies`` returns False
    for exactly this state). That is an OBSERVABILITY failure, not a pass: a
    declared-but-unreadable campaign must honest-fail, never no-op.

    Raises :class:`CampaignError` when ``profile.verify_campaign is not None`` AND
    ``profile.runner_report_adapter() is None``. A profile with NO campaign
    (Python today) is a legitimate no-op and passes silently; a profile whose
    campaign HAS an adapter passes. Deterministic, side-effect-free (it runs no
    command). The caller wires it BEFORE the campaign runs so the failure is
    surfaced even though ``coherence_gate_applies`` would otherwise skip the stack.
    """
    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        return
    getter = getattr(profile, "runner_report_adapter", None)
    adapter = getter() if callable(getter) else None
    if adapter is None:
        report_format = getattr(campaign, "report_format", None)
        raise CampaignError(
            "verify campaign is declared but its report_format "
            f"{report_format!r} has no registered runner-report adapter — the "
            "campaign's executions cannot be observed, so the coverage-execution "
            "coherence gate would silently NO-OP. An unobservable verification is "
            "not a pass; register an adapter for this report_format (or remove the "
            "campaign from the profile)."
        )


def enforce_campaign_clean_execution(
    execution: RunnerExecution,
    exit_code: int,
    *,
    echo: Callable[[str], None] = print,
) -> None:
    """HARD GATE (contract verify.campaign.clean_execution.v1; GPT round-2 §3.2).

    The verify campaign's OWN result is a green authority IN ITS OWN RIGHT —
    independent of VB reconciliation. ``build_coherence_report`` only checks that
    each UNBLOCKED VB has an authentic covering file that executed and passed; a
    FAILING test that covers NO declared VB (a plain integration / e2e / unit
    test), or a runner that exited NON-ZERO for a non-VB reason, is invisible to it
    and would pass the coherence gate alone — a false-green. This gate closes that
    hole.

    Raises :class:`CoherenceError` when ``execution.executed_failed_files`` is
    non-empty (ANY executed test file had a failing/erroring case) OR
    ``exit_code != 0`` (the runner itself reported failure). Deterministic and
    side-effect-free — it runs no command (the caller already executed the
    campaign). honest-fail: a failing test, or a non-zero runner exit, means the
    build is RED, never silently green.
    """

    failed = sorted(execution.executed_failed_files)
    nonzero_exit = exit_code != 0
    if not failed and not nonzero_exit:
        return
    reasons: list[str] = []
    if failed:
        shown = ", ".join(failed[:10]) + (" …" if len(failed) > 10 else "")
        reasons.append(f"{len(failed)} executed test file(s) FAILED: {shown}")
    if nonzero_exit:
        reasons.append(f"the verify campaign exited non-zero ({exit_code})")
    detail = "; ".join(reasons)
    echo(f"[greenfield] verify: campaign clean-execution gate FAILED — {detail}")
    raise CoherenceError(
        "verify campaign did not execute cleanly: "
        + detail
        + ". A failing test — even one that covers no declared verifiable behavior "
        "— or a non-zero runner exit means the build is RED. The campaign result is "
        "itself a green authority, independent of per-VB reconciliation; fix the "
        "failing test(s) (or the runner error) before the run can be green."
    )


def enforce_coverage_execution_coherence(
    project_root: Path | str,
    profile: Any,
    *,
    config: dict[str, Any] | None = None,
    echo: Callable[[str], None] = print,
) -> CoherenceReport:
    """Run the campaign + the coherence gate; raise on a hard failure.

    The single greenfield-verify entry point. Steps:
      1. NO-OP when the stack has no applicable campaign (returns a non-applicable
         report) — Python today, or a campaign with no adapter.
      2. Execute the profile-owned verify campaign (the WHOLE VB surface) →
         :class:`RunnerExecution`.
      3. Reconcile static VB coverage (+ authenticity) against that execution.
      4. Raise :class:`CoherenceError` when ``unblocked − verified ≠ ∅`` or an
         e2e-observability error is present.

    The CALLER (greenfield ``_stage_verify``) wires this AFTER the normal verify
    runner certified the structural/typecheck/test-command path; this gate ADDS
    the execution-coherence proof on top (it never weakens the existing gates).
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)

    if not coherence_gate_applies(profile):
        return CoherenceReport(
            version=COHERENCE_CONTRACT_VERSION,
            applicable=False,
            detail="stack declares no applicable verify campaign — coherence gate is a no-op",
        )

    run = run_verify_campaign(project_root, profile, config=config, echo=echo)
    # HARD anti-false-green gate (verify.campaign.clean_execution.v1): the
    # campaign's OWN result gates green BEFORE VB reconciliation — a failing test
    # that covers no declared VB, or a non-zero runner exit, is RED here even
    # though build_coherence_report (which only reconciles UNBLOCKED VBs) would
    # miss it.
    enforce_campaign_clean_execution(run.execution, run.exit_code, echo=echo)
    report = build_coherence_report(
        project_root, config=config, profile=profile, execution=run.execution
    )
    if not report.applicable:
        echo(f"[greenfield] verify: coverage-execution coherence — {report.detail}")
        return report

    if not report.passed:
        for err in report.observability_errors:
            echo(err.message)
        for vb in report.unverified_vbs:
            echo(vb.message)
        raise CoherenceError(
            "coverage-execution coherence gate failed: "
            f"{len(report.unverified_vbs)} declared verifiable behavior(s) are statically "
            "covered but were NOT executed+passed by the verify campaign, and/or "
            f"{len(report.observability_errors)} e2e-observability error(s). Static coverage "
            "does not prove a behavior — its covering test must actually run and pass. "
            f"({report.detail})"
        )
    echo(
        f"[greenfield] verify: coverage-execution coherence OK — {report.detail}"
    )
    return report


class CoherenceError(RuntimeError):
    """The coverage-execution coherence gate failed (an anti-false-green hard fail)."""


def _output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


__all__ = [
    "COHERENCE_CONTRACT_VERSION",
    "CampaignError",
    "CampaignRun",
    "CoherenceError",
    "CoherenceObservabilityError",
    "CoherenceReport",
    "GoTestJsonReportAdapter",
    "RunnerExecution",
    "RunnerReportAdapter",
    "RunnerReportUnsupported",
    "TestInventory",
    "TestInventoryEntry",
    "UnverifiedVB",
    "VitestJsonReportAdapter",
    "build_coherence_report",
    "build_test_inventory",
    "certify_verify_campaign_observable",
    "coherence_gate_applies",
    "enforce_campaign_clean_execution",
    "enforce_coverage_execution_coherence",
    "format_coherence_feedback",
    "render_coherence_markdown",
    "resolve_runner_report_adapter",
    "run_verify_campaign",
    "supported_runner_report_formats",
]

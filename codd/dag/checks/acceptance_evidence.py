"""DAG check: acceptance_evidence — every acceptance criterion carries evidence.

The gate for the invariant documented in :mod:`codd.acceptance_evidence`:

    For every acceptance criterion AC there is evidence E such that
    (a) E is machine-executed, (b) E runs through the shipped path, (c) E
    references AC's named parameters, and (d) E is bound to the content of the
    implementation it verified.

Findings, by the half of the invariant they defend:

* (a) ``runtime_evidence_not_executable`` — a runtime obligation in a project
  whose runtime stage is switched off (a silent skip that used to read green);
  ``unbound_acceptance`` / ``unresolved_evidence`` / ``manual_evidence_missing``
  — a criterion that reaches no machine-checked evidence at all.
* (b) ``off_shipped_path`` / ``multiple_implementers`` / ``reachability_unknown``
  — evidence that does not run through the code a user's request reaches.
* (c) ``param_not_referenced`` / ``undeclared_numeric`` — a value retyped instead
  of referenced by name.
* (d) ``stale_manual_evidence`` — a human verdict about an implementation that
  has since changed.

See ``docs/design/acceptance-evidence-invariant.md``.

Generality: the check reads the project's own declarations (requirement tables,
``operation_flow``, ``runtime_smoke``) and carries no project, framework or
language literal. It is dormant for a project that declares no acceptance
criteria — there is nothing to certify — and every severity is configurable, so
a project may downgrade a class to amber without losing the finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.acceptance_evidence import (
    SETTINGS_KEY,
    AcceptanceCriterion,
    acceptance_settings,
    cover_marker_offsets,
    entry_files_by_operation,
    implementation_closure,
    import_closure,
    load_acceptance_criteria,
    numeric_literals,
    read_test_text,
    requirement_anchor_pattern,
    resolve_test_targets,
    runtime_obligations,
    runtime_smoke_enabled,
    scan_requirement_anchor_hits,
    substance_at,
    substantive_tests,
    test_substance,
    tested_subjects,
)
from codd.acceptance_record import implementation_digest, load_ledger
from codd.dag.checks import DagCheck, register_dag_check

CHECK_NAME = "acceptance_evidence"


@dataclass
class AcceptanceEvidenceResult:
    check_name: str = CHECK_NAME
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    # Acceptance criteria actually evaluated. 0 with status="skip" means the
    # project declares none (dormant), which the materiality overlay must not
    # read as a verified clean run.
    checked_count: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check(CHECK_NAME)
class AcceptanceEvidenceCheck(DagCheck):
    """Reconcile declared acceptance criteria against executable evidence."""

    check_name = CHECK_NAME
    severity = "red"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> AcceptanceEvidenceResult:
        target_dag = dag if dag is not None else self.dag
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = (self.project_root or Path.cwd()).resolve()

        # Acceptance criteria, operation_flow, runtime_smoke and this check's own
        # settings all live at the TOP level of codd.yaml, and not every caller
        # passes it: `VerifyRunner` hands its checks the merged ``dag:`` section
        # only. Reading a project through that section alone would find no
        # requirements, no operations and no settings — and report a clean
        # "nothing to certify" for a project full of acceptance criteria, which
        # is the false green this check exists to end. So load codd.yaml here and
        # let whatever the caller passed win over it.
        config = _project_config(root, codd_config if codd_config is not None else self.settings)
        resolved = acceptance_settings(config)
        if not resolved.enabled:
            return AcceptanceEvidenceResult(
                status="skip",
                skipped=True,
                message="acceptance_evidence: disabled in codd.yaml",
            )

        criteria = load_acceptance_criteria(root, config)
        if not criteria:
            return AcceptanceEvidenceResult(
                status="skip",
                skipped=True,
                message=(
                    "acceptance_evidence: no acceptance criteria declared in the project's "
                    "requirement documents — nothing to certify."
                ),
            )

        declared_ids = _declared_operation_ids(config, target_dag)
        test_paths = _nodes_of_kind(target_dag, {"test_file"})
        impl_paths = _nodes_of_kind(target_dag, {"impl_file", "common"})
        anchor_hits = scan_requirement_anchor_hits(
            root,
            sorted(test_paths | impl_paths),
            (criterion.req_id for criterion in criteria),
        )
        anchors = {req_id: set(by_path) for req_id, by_path in anchor_hits.items()}

        violations: list[dict[str, Any]] = []
        violations.extend(
            _runtime_violations(criteria, declared_ids, config, resolved.runtime_severity)
        )
        # Only an EXECUTABLE runtime obligation counts as a binding. When the
        # runtime stage is off, the criterion really is proved by nothing, and
        # the two findings ask for two different remedies (turn the stage on /
        # write a test) — that is two defects, not one reported twice.
        runtime_bound_ids = (
            frozenset(
                criterion.req_id
                for criterion, _targets in runtime_obligations(criteria, declared_ids)
            )
            if runtime_smoke_enabled(config)
            else frozenset()
        )
        binding_violations, bound_by_req = _binding_violations(
            root,
            criteria,
            config=config,
            settings=resolved,
            dag=target_dag,
            impl_paths=impl_paths,
            test_paths=test_paths,
            anchors=anchors,
            anchor_offsets=anchor_hits,
            runtime_bound_ids=runtime_bound_ids,
        )
        violations.extend(binding_violations)
        violations.extend(_parameter_violations(root, criteria, bound_by_req, resolved))
        violations.extend(
            _shipped_path_violations(
                target_dag,
                criteria,
                config=config,
                settings=resolved,
                operations=_declared_operations(config, target_dag),
                anchors=anchors,
                impl_paths=impl_paths,
                test_paths=test_paths,
                bound_by_req=bound_by_req,
                project_root=root,
            )
        )

        return _finalize(violations, checked_count=len(criteria), max_findings=resolved.max_findings)


# ---------------------------------------------------------------------------
# (a) runtime obligations must be executable
# ---------------------------------------------------------------------------


def _runtime_violations(
    criteria: Iterable[AcceptanceCriterion],
    declared_ids: frozenset[str],
    config: Mapping[str, Any],
    severity: str,
) -> list[dict[str, Any]]:
    """Runtime-evidence obligations in a project whose runtime stage cannot run.

    This is the *demotion* hole: ``codd verify`` runs Step 8 only when the
    project's ``runtime_smoke`` section is enabled, and an absent section made
    the step vanish without a word. A criterion whose evidence is "the running
    system does X" then had NO evidence while the run still reported green.
    """

    if runtime_smoke_enabled(config):
        return []
    violations: list[dict[str, Any]] = []
    for criterion, targets in runtime_obligations(criteria, declared_ids):
        violations.append(
            {
                "type": "runtime_evidence_not_executable",
                "severity": severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "targets": list(targets),
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                    f"({criterion.source}) declares runtime evidence "
                    f"({', '.join(targets)}) but `runtime_smoke` is not enabled in "
                    "codd.yaml, so that evidence is never executed — the stage is "
                    "skipped silently and the criterion reads as accepted on no "
                    "evidence. Enable `runtime_smoke` (and run `codd verify --runtime`), "
                    "declare different evidence in the criterion's `verified_by` column, "
                    "or downgrade with `acceptance_evidence.runtime_severity: amber`."
                ),
            }
        )
    return violations


# ---------------------------------------------------------------------------
# (a) wiring + (d) freshness: every criterion bound to machine-checked evidence
# ---------------------------------------------------------------------------


def _binding_violations(
    root: Path,
    criteria: list[AcceptanceCriterion],
    *,
    config: Mapping[str, Any],
    settings: Any,
    dag: Any,
    test_paths: set[str],
    impl_paths: set[str],
    anchors: Mapping[str, set[str]],
    anchor_offsets: Mapping[str, Mapping[str, tuple[int, ...]]],
    runtime_bound_ids: frozenset[str],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Every acceptance criterion must reach evidence a machine can re-run.

    The four binding paths, in the order a project typically acquires them:

    1. an explicit ``verified_by: test:<name>`` that RESOLVES to a test file;
    2. a VB registry row referenced from the criterion's own row, covered by a
       ``codd: covers vb=`` marker (the generated-project path);
    3. a test file that writes the requirement id as a token (the brownfield
       path — no document surgery required);
    4. an explicit ``verified_by: manual:<owner>`` with a ledger record that is
       still fresh against the implementation it accepted.

    A claim only binds when the test it points at actually proves something. A
    claim with a POSITION — a ``codd: covers vb=`` marker, a requirement id
    written into a file — is judged against the test it introduces
    (:func:`substance_at`), not against the file's best test: one unrelated
    passing test in the same file must not certify a marker attached to a skipped
    one. A claim that names a FILE (``verified_by: test:<name>``) is judged at
    file granularity, because that is the granularity it was written at.

    A marker over an empty test, over a skipped test, or a comment that merely
    mentions the requirement id — including one that mentions it to say the
    opposite, ``// R-1 NOT implemented`` — is a claim, not evidence. Those are
    reported as ``vacuous_evidence`` and do NOT bind, so the criterion stays
    unbound instead of going quietly green.

    A criterion reaching none of them is ``unbound_acceptance``: it is written
    down, agreed with a customer, and connected to nothing. That state used to
    be invisible — and, for a project with no VB table at all, was announced as
    a one-line notice and passed.
    """

    violations: list[dict[str, Any]] = []
    bound_by_req: dict[str, list[str]] = {}
    substantive, vacuity_reason = substantive_tests(root, sorted(test_paths))
    registry_exists, covered_by_req = _vb_bindings(
        root, config, (criterion.req_id for criterion in criteria)
    )
    ledger = load_ledger(root)

    if not registry_exists and not _vb_table_opt_out(config):
        violations.append(
            {
                "type": "vb_registry_missing",
                "severity": settings.unbound_severity,
                "req_id": "(project)",
                "message": (
                    f"[acceptance_evidence] This project declares {len(criteria)} acceptance "
                    "criterion/criteria but owns NO verifiable-behavior registry "
                    "(`docs/test/test_strategy.md`), so not one of them is reconciled against "
                    "a test marker. Run `codd acceptance sync` to derive the registry from the "
                    "acceptance criteria, or set `test_coverage.require_vb_table: false` to "
                    "declare that this project verifies its criteria some other way. "
                    "(`acceptance_evidence.mode: strict` makes this red instead of advisory.)"
                ),
            }
        )

    for criterion in criteria:
        bound: list[str] = []
        claimed: list[str] = []

        for ref in criterion.evidence_of("test"):
            resolved_tests = resolve_test_targets(ref.target, test_paths)
            if resolved_tests:
                claimed.extend(resolved_tests)
            else:
                violations.append(
                    {
                        "type": "unresolved_evidence",
                        "severity": settings.unbound_severity,
                        "req_id": criterion.req_id,
                        "source": criterion.source,
                        "message": (
                            f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                            f"({criterion.source}) declares `verified_by: test:{ref.target}`, but "
                            "no test file matches that name. A pointer to a test that does not "
                            "exist is worse than no pointer: it reads as evidence. To go green: "
                            "point it at a test file that exists (its path, filename or stem — "
                            "`codd acceptance list` prints the test files CoDD can see), or write "
                            f"`{criterion.req_id}` into the test that proves this criterion."
                        ),
                    }
                )

        # File-granularity claims (``verified_by: test:<name>``) keep file-level
        # judgement; positioned claims are judged per test block.
        file_claims = dict.fromkeys(claimed)
        positioned: dict[str, tuple[bool, str]] = {}
        for path, offsets in covered_by_req.get(criterion.req_id, {}).items():
            positioned[path] = _positioned_substance(root, path, offsets)
        for path in sorted(anchors.get(criterion.req_id, set()) & test_paths):
            offsets = anchor_offsets.get(criterion.req_id, {}).get(path, ())
            verdict = _positioned_substance(root, path, offsets)
            if path in positioned and positioned[path][0]:
                continue  # already bound by a marker
            positioned[path] = verdict

        for path in file_claims:
            if path in substantive:
                bound.append(path)
        for path, (ok, _reason) in positioned.items():
            if ok:
                bound.append(path)

        empty_claims = [path for path in file_claims if path not in substantive]
        empty_claims.extend(
            path for path, (ok, _reason) in positioned.items() if not ok and path not in empty_claims
        )
        vacuity_reason = dict(vacuity_reason)
        vacuity_reason.update(
            {path: reason for path, (ok, reason) in positioned.items() if not ok and reason}
        )
        if empty_claims:
            violations.append(
                {
                    "type": "vacuous_evidence",
                    "severity": settings.vacuous_severity,
                    "req_id": criterion.req_id,
                    "source": criterion.source,
                    "files": empty_claims,
                    "message": (
                        f"[acceptance_evidence] The evidence claimed for `{criterion.req_id}` "
                        + ", ".join(
                            f"{path} ({vacuity_reason.get(path, 'vacuous')})" for path in empty_claims
                        )
                        + " proves nothing: a marker or the requirement id appears there, but the "
                        "test it attaches to does not run and assert (empty body, skipped, or no "
                        "test under the marker at all). A claim in a comment is a claim; only an "
                        "executed assertion is evidence. To go green: write the assertion, "
                        "un-skip the test, or move the marker onto the test that proves the "
                        "criterion — a marker attaches to the test written UNDER it."
                    ),
                }
            )

        manual_refs = criterion.evidence_of("manual")
        if manual_refs:
            accepted = implementation_closure(
                dag, anchors.get(criterion.req_id, set()), impl_paths
            )
            violations.extend(
                _manual_violations(root, criterion, manual_refs, ledger, accepted, settings)
            )
            record = ledger.get(criterion.req_id)
            if record is not None and record.status == "pass":
                if not record.is_stale(implementation_digest(root, accepted)):
                    bound.append(f"manual:{record.by}")

        bound_by_req[criterion.req_id] = sorted(set(bound))
        if bound:
            continue
        if criterion.req_id in runtime_bound_ids:
            # An executable runtime obligation IS machine-checked evidence.
            continue
        violations.append(
            {
                "type": "unbound_acceptance",
                "severity": settings.unbound_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                    f"({criterion.source}) is bound to NO machine-checked evidence: no "
                    "`verified_by:` declaration, no verifiable-behavior row referenced from "
                    "its own row, and no test file that names the requirement id. Add a "
                    "`verified_by: test:<name> | runtime:<case> | manual:<owner>` column entry, "
                    "or write `" + criterion.req_id + "` into the test that proves it."
                ),
            }
        )
    return violations, bound_by_req


def _manual_violations(
    root: Path,
    criterion: AcceptanceCriterion,
    manual_refs: tuple[Any, ...],
    ledger: Mapping[str, Any],
    accepted: list[str],
    settings: Any,
) -> list[dict[str, Any]]:
    """(d): a manual verdict expires when the implementation it accepted changes."""

    violations: list[dict[str, Any]] = []
    owners = ", ".join(ref.target for ref in manual_refs)
    record = ledger.get(criterion.req_id)
    if record is None or record.status != "pass":
        violations.append(
            {
                "type": "manual_evidence_missing",
                "severity": settings.unbound_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` declares "
                    f"manual evidence (owner: {owners}) but the acceptance ledger holds no "
                    f"passing record for it. Record the verdict with "
                    f"`codd acceptance record {criterion.req_id} pass --by <owner>` so it is "
                    "bound to the implementation it accepted."
                ),
            }
        )
        return violations

    current = implementation_digest(root, accepted)
    if record.is_stale(current):
        violations.append(
            {
                "type": "stale_manual_evidence",
                "severity": settings.freshness_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] The manual acceptance of `{criterion.req_id}` "
                    f"(by {record.by} at {record.recorded_at}) was bound to a different "
                    "implementation than the one on disk now, so the verdict is about code "
                    "that no longer exists. Re-check the criterion and re-record it with "
                    f"`codd acceptance record {criterion.req_id} pass --by <owner>`."
                ),
            }
        )
    elif criterion.critical:
        violations.append(
            {
                "type": "critical_manual_only",
                "severity": "amber",
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] `{criterion.req_id}` is marked critical but its only "
                    "evidence is a manual record. A criterion that must not regress should have "
                    "evidence that re-runs itself; the manual record proves one moment, not the "
                    "next release. To go green: add `test:<name>` (or `runtime:<case>`) to this "
                    "row's `verified_by` alongside the manual owner — or, if a person really is "
                    "the only possible check, drop `critical` from the row, since the flag means "
                    "\"must not regress unattended\"."
                ),
            }
        )
    return violations


# ---------------------------------------------------------------------------
# (c) the evidence must reference the criterion's NAMED parameters
# ---------------------------------------------------------------------------


def _parameter_violations(
    root: Path,
    criteria: Iterable[AcceptanceCriterion],
    bound_by_req: Mapping[str, list[str]],
    settings: Any,
) -> list[dict[str, Any]]:
    """A stated value must reach its evidence through a NAME, not by being retyped.

    The requirement said 15, the acceptance document said 20, the code said 20.
    Nothing was lying: the number had been copied three times, and a copy has no
    way to notice that the original moved. Grepping the criterion's digits out of
    the test would only catch the same defect by luck (and fire on every date and
    version string on the way). What is checkable, deterministically and in any
    language, is the NAME: declare ``params: {per_sheet: 15}`` next to the
    criterion, have the test read ``per_sheet``, and the two cannot drift.

    * a declared parameter absent from every bound evidence file is red — the
      evidence is checking something, but not the value that was agreed;
    * a bare literal in a criterion that declares NO parameters is amber: a
      nudge to declare, never an accusation, because the check cannot know
      whether that number is load-bearing.
    """

    violations: list[dict[str, Any]] = []
    for criterion in criteria:
        # Only FILE-BACKED evidence can reference a parameter by name. A manual
        # record is a person's verdict, not a file with a symbol in it; asking it
        # to mention `per_sheet` would be a red nobody can ever clear.
        bound = [path for path in (bound_by_req.get(criterion.req_id) or []) if not path.startswith("manual:")]
        if criterion.params:
            evidence_text = _read_all(root, bound)
            missing = [name for name in criterion.params if name not in evidence_text]
            if missing and bound:
                violations.append(
                    {
                        "type": "param_not_referenced",
                        "severity": settings.param_severity,
                        "req_id": criterion.req_id,
                        "source": criterion.source,
                        "params": missing,
                        "message": (
                            f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                            f"declares parameter(s) {', '.join(missing)}, but no evidence file "
                            f"bound to it ({', '.join(bound)}) mentions them — the evidence is "
                            "asserting a value that was retyped, so the criterion and the test "
                            "can drift apart without either turning red. Read the parameter by "
                            "name in the test."
                        ),
                    }
                )
            continue

        literals = numeric_literals(criterion.text)
        if not literals:
            continue
        violations.append(
            {
                "type": "undeclared_numeric",
                "severity": settings.undeclared_numeric_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "literals": literals,
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                    f"({criterion.source}) states the value(s) {', '.join(literals)} as bare "
                    "literals and declares no `params:`. A number written in three places "
                    "drifts in three places. Declare it once — `params: <name>=<value>` in the "
                    "criterion's row — and have the evidence read it by name."
                ),
            }
        )
    return violations


def _read_all(root: Path, relative_paths: Iterable[str]) -> str:
    """Concatenated text of the given project files (unreadable ones contribute nothing)."""

    chunks: list[str] = []
    for relative in relative_paths:
        try:
            chunks.append((root / relative).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _vb_bindings(
    root: Path,
    config: Mapping[str, Any],
    req_ids: Iterable[str],
) -> tuple[bool, dict[str, dict[str, tuple[int, ...]]]]:
    """(registry_exists, requirement id -> {covering test path: marker offsets}).

    Two links, both already part of CoDD's vocabulary:

    * a VB id *referenced* from a later column of another document's row — that
      row's first cell is the requirement/AC id, which is exactly the
      requirement→behaviour link, and is the shape generated test docs are
      already told to write;
    * a declared VB row that names the requirement id in its own later columns —
      the shape ``codd acceptance sync`` writes when it derives the registry from
      the acceptance criteria.

    Only a COVERED behaviour binds: a declared-but-uncovered VB row is the VB
    audit's own finding, and treating it as acceptance evidence would launder an
    uncovered behaviour into an accepted one.
    """

    from codd.verifiable_behavior_audit import (
        build_vb_coverage_audit,
        discover_vb_documents,
        parse_vb_references,
    )

    try:
        report = build_vb_coverage_audit(root, config=dict(config))
    except (OSError, ValueError):
        return False, {}
    if not report.rows:
        return False, {}

    covered_tests: dict[str, list[str]] = {
        row.vb_id.casefold(): list(row.matched_tests)
        for row in report.rows
        if row.coverage_status == "covered"
    }
    bindings: dict[str, dict[str, tuple[int, ...]]] = {}
    vb_of_row = {row.vb_id.casefold(): row.vb_id for row in report.rows}
    pattern = requirement_anchor_pattern(req_ids)

    def add(req_id: str, vb_id: str, paths: Iterable[str]) -> None:
        for path in paths:
            text = read_test_text(root, path)
            offsets = tuple(cover_marker_offsets(text, vb_id)) if text is not None else ()
            bindings.setdefault(req_id, {}).setdefault(path, offsets)

    # Link 2: the declared row names the requirement id.
    if pattern is not None:
        for row in report.rows:
            matched = covered_tests.get(row.vb_id.casefold())
            if not matched:
                continue
            for match in pattern.finditer(f"{row.description} {row.declared_scenarios}"):
                add(match.group("id"), row.vb_id, matched)

    # Link 1: another document's row references the canonical id.
    for doc_path in discover_vb_documents(root, config=dict(config)):
        try:
            text = doc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for reference in parse_vb_references(text, source_doc=doc_path.name):
            matched = covered_tests.get(reference.vb_id.casefold())
            if matched and reference.row_id:
                add(reference.row_id, vb_of_row.get(reference.vb_id.casefold(), reference.vb_id), matched)
    return True, bindings


def _positioned_substance(root: Path, relative: str, offsets: Iterable[int]) -> tuple[bool, str]:
    """Judge a POSITIONED claim against the test it attaches to.

    A marker binds when at least one of its occurrences introduces a test that
    runs and asserts. With no offsets (the claim exists but could not be located
    in the file) the file-level verdict is the honest fallback.
    """

    text = read_test_text(root, relative)
    if text is None:
        return False, "unreadable"
    positions = list(offsets)
    if not positions:
        return test_substance(text)
    best_reason = ""
    for offset in positions:
        ok, reason = substance_at(text, offset)
        if ok:
            return True, ""
        best_reason = best_reason or reason
    return False, best_reason or "no_assertion"


def _vb_table_opt_out(config: Mapping[str, Any]) -> bool:
    """Whether the project EXPLICITLY declared that it owns no VB registry.

    The missing registry is reported whether or not the implement-time gate is
    armed: `test_coverage.require_vb_table` decides whether it FAILS a build,
    never whether it is visible — a finding nobody can see is the "one-line
    notice and pass" this work exists to end. Only an explicit
    `require_vb_table: false` (the project saying "I verify some other way")
    silences it.
    """

    section = config.get("test_coverage") if isinstance(config, Mapping) else None
    return isinstance(section, Mapping) and section.get("require_vb_table") is False


# ---------------------------------------------------------------------------
# (b) the evidence must run through the SHIPPED PATH
# ---------------------------------------------------------------------------


def _shipped_path_violations(
    dag: Any,
    criteria: Iterable[AcceptanceCriterion],
    *,
    config: Mapping[str, Any],
    settings: Any,
    operations: Mapping[str, Mapping[str, Any]],
    anchors: Mapping[str, set[str]],
    impl_paths: set[str],
    test_paths: set[str],
    bound_by_req: Mapping[str, list[str]],
    project_root: Path,
) -> list[dict[str, Any]]:
    """"There was a test" is not the same as "the thing users press was tested".

    One requirement, two implementations: a command-line script and a button on
    a page. The test exercised the script. The button shipped. Every gate was
    green, because nothing in the system asked the only question that mattered —
    does the evidence run through the code a user's request actually reaches?

    The shipped path is the import closure of the entry point the requirement's
    own operation declares. The question is asked about EVIDENCE — the files
    offered as proof, and what they exercise — not about every file that mentions
    the requirement id. A requirement legitimately spans layers (a page, a
    library, a migration); a migration being unreachable from a page is
    architecture, not a defect. What is a defect is proof that lives off the path
    that ships.

    Three findings, all amber, because reachability is only as good as the
    language's import extraction:

    * ``off_shipped_path`` — the evidence and everything it exercises sit outside
      the closure;
    * ``multiple_implementers`` — the evidence proves an implementation off the
      path while another implementation of the same requirement is on it;
    * ``reachability_unknown`` — there is something to place on a path and no
      path could be resolved. Never silent: an unknown that prints nothing is the
      exact failure this check exists to end.
    """

    entries_by_operation = entry_files_by_operation(project_root, config, operations)
    section = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    allow_multiple = bool(section.get("allow_multiple_implementers", False)) if isinstance(section, Mapping) else False
    subjects_by_test = tested_subjects(dag, test_paths)
    tests_by_subject: dict[str, set[str]] = {}
    for test_path, subjects in subjects_by_test.items():
        for subject in subjects:
            tests_by_subject.setdefault(subject, set()).add(test_path)
    severity = settings.reachability_severity

    violations: list[dict[str, Any]] = []
    for criterion in criteria:
        implementers = sorted(set(anchors.get(criterion.req_id, set())) & impl_paths)
        evidence_tests = set(bound_by_req.get(criterion.req_id) or []) & test_paths
        for implementer in implementers:
            # A test of a file that CLAIMS the requirement is evidence about that
            # requirement even when the test never names the id — which is the
            # usual brownfield state, and precisely the case that shipped wrong.
            evidence_tests |= tests_by_subject.get(implementer, set())
        if not evidence_tests:
            continue  # nothing is being offered as proof; other rules speak

        entries = sorted(
            {
                entry
                for operation_id in criterion.operation_refs
                for entry in entries_by_operation.get(operation_id, ())
            }
        )
        if not entries:
            violations.append(
                {
                    "type": "reachability_unknown",
                    "severity": severity,
                    "req_id": criterion.req_id,
                    "source": criterion.source,
                    "message": (
                        f"[acceptance_evidence] No entry point could be resolved for "
                        f"`{criterion.req_id}`, so whether its evidence runs through the shipped "
                        "path is UNKNOWN — not verified. Reference the operation that ships it "
                        "(`operation_flow.<id>`) and give that operation a `route:` (or an "
                        "explicit `entry_file:`), and declare `filesystem_routes` so routes "
                        "resolve to files."
                    ),
                }
            )
            continue

        closure = import_closure(dag, entries)
        subjects: set[str] = set()
        for test_path in evidence_tests:
            subjects |= subjects_by_test.get(test_path) or set()
            subjects |= import_closure(dag, [test_path])
        if subjects & closure:
            continue

        off_path = sorted(evidence_tests | (subjects & set(implementers)))
        on_path = [path for path in implementers if path in closure]
        violations.append(
            {
                "type": "off_shipped_path",
                "severity": severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "entries": entries,
                "off_path": off_path,
                "message": (
                    f"[acceptance_evidence] The evidence offered for `{criterion.req_id}` "
                    f"({', '.join(off_path)}) never touches the code reachable from the entry "
                    f"point(s) users go through ({', '.join(entries)}). Whatever it proves, it "
                    "does not prove the path that ships — the code behind the entry point is "
                    "verified by nothing here. Point the evidence at the shipped implementation, "
                    "or ship the implementation the evidence covers."
                ),
            }
        )
        if on_path and not allow_multiple:
            violations.append(
                {
                    "type": "multiple_implementers",
                    "severity": severity,
                    "req_id": criterion.req_id,
                    "source": criterion.source,
                    "on_path": on_path,
                    "off_path": off_path,
                    "message": (
                        f"[acceptance_evidence] `{criterion.req_id}` is implemented on both sides "
                        f"of the shipped path: {', '.join(on_path)} is reachable from "
                        f"{', '.join(entries)}, and the evidence proves {', '.join(off_path)}, "
                        "which is not. The implementation users reach is the untested one. Retire "
                        "the superseded implementation, or set "
                        "`acceptance_evidence.allow_multiple_implementers: true` if both ship."
                    ),
                }
            )
    return violations


def _declared_operations(config: Mapping[str, Any], dag: Any) -> dict[str, dict[str, Any]]:
    """Declared operations by id (codd.yaml + design-doc frontmatter)."""

    from codd.requirements_meta import operation_flow_operations

    operations: dict[str, dict[str, Any]] = {}
    for payload in _operation_flow_payloads(config, dag):
        for operation in operation_flow_operations(payload):
            raw = operation.get("id")
            if isinstance(raw, str) and raw.strip():
                operations.setdefault(raw.strip(), dict(operation))
    return operations


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project_config(root: Path, passed: Mapping[str, Any] | None) -> dict[str, Any]:
    """The project's codd.yaml, with anything the caller passed layered on top."""

    from codd.config import load_project_config

    try:
        config = dict(load_project_config(root))
    except (FileNotFoundError, ValueError):
        config = {}
    for key, value in dict(passed or {}).items():
        config[key] = value
    return config


def _nodes_of_kind(dag: Any, kinds: set[str]) -> set[str]:
    """Relative paths of DAG nodes of the given kinds (node ids ARE the paths)."""

    nodes = getattr(dag, "nodes", {}) if dag is not None else {}
    return {node.id for node in nodes.values() if getattr(node, "kind", "") in kinds}


def _operation_flow_payloads(config: Mapping[str, Any], dag: Any) -> list[Any]:
    """Every declared ``operation_flow`` payload: codd.yaml + design-doc frontmatter.

    Mirrors ``cli._operation_flows_from_project`` without importing the CLI — the
    DAG already carries each design document's parsed frontmatter, so the doc side
    costs no second filesystem walk.
    """

    payloads: list[Any] = []
    if isinstance(config, Mapping) and isinstance(config.get("operation_flow"), Mapping):
        payloads.append(config["operation_flow"])
    for node in (getattr(dag, "nodes", {}) or {}).values():
        attributes = getattr(node, "attributes", None)
        if not isinstance(attributes, Mapping):
            continue
        frontmatter = attributes.get("frontmatter")
        if not isinstance(frontmatter, Mapping):
            continue
        for container in (frontmatter, frontmatter.get("codd")):
            if isinstance(container, Mapping) and isinstance(container.get("operation_flow"), Mapping):
                payloads.append(container["operation_flow"])
    return payloads


def _declared_operation_ids(config: Mapping[str, Any], dag: Any) -> frozenset[str]:
    """Normalized ids of every declared operation."""

    return frozenset(
        operation_id.lower() for operation_id in _declared_operations(config, dag)
    )


def _finalize(
    violations: list[dict[str, Any]],
    *,
    checked_count: int,
    max_findings: int,
) -> AcceptanceEvidenceResult:
    """Build the result, choosing severity/status from the violations present.

    The cap is applied PER FINDING TYPE, not across the whole list. A global cap
    sorted by severity lets one loud red class push every amber class out of the
    output entirely — the check would detect a shipped-path defect and then hide
    it behind fifty unrelated rows, which is the same disappearing act it exists
    to stop. Truncated findings still name every criterion they concern.
    """

    reds = [item for item in violations if item.get("severity") == "red"]
    ambers = [item for item in violations if item.get("severity") != "red"]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in reds + ambers:
        by_type.setdefault(str(item.get("type", "?")), []).append(item)

    shown: list[dict[str, Any]] = []
    for kind, items in by_type.items():
        shown.extend(items[:max_findings])
        truncated = items[max_findings:]
        if truncated:
            remaining = ", ".join(str(item.get("req_id", "?")) for item in truncated)
            shown.append(
                {
                    "type": "findings_truncated",
                    "severity": "amber",
                    "req_id": kind,
                    "message": (
                        f"[acceptance_evidence] ...and {len(truncated)} more `{kind}` finding(s), "
                        f"in full: {remaining}. Raise `acceptance_evidence.max_findings` in "
                        "codd.yaml to expand them."
                    ),
                }
            )

    breakdown = ", ".join(f"{kind} {len(items)}" for kind, items in by_type.items())
    tally = f" [{breakdown}]" if breakdown else ""

    if reds:
        return AcceptanceEvidenceResult(
            severity="red",
            status="fail",
            passed=False,
            message=(
                f"acceptance_evidence: {len(reds)} red / {len(ambers)} amber finding(s) across "
                f"{checked_count} declared acceptance criteria{tally}"
            ),
            checked_count=checked_count,
            violations=shown,
        )
    if ambers:
        return AcceptanceEvidenceResult(
            severity="amber",
            status="warn",
            passed=True,
            message=(
                f"acceptance_evidence: {len(ambers)} advisory finding(s) across "
                f"{checked_count} declared acceptance criteria{tally}"
            ),
            checked_count=checked_count,
            violations=shown,
        )
    return AcceptanceEvidenceResult(
        severity="amber",
        status="pass",
        passed=True,
        message=(
            f"acceptance_evidence: {checked_count} acceptance criterion/criteria bound to "
            "executable evidence — OK"
        ),
        checked_count=checked_count,
    )

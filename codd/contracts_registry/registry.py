"""The ACG Contract Registry — declarative coherence-contract coverage table.

This module is PURE DATA + tiny helpers. It imports nothing from the rest of
CoDD (so it can never alter gate behaviour and is cheap to load). The authority
symbols are recorded as *dotted-string names*, never imported, so a registry
load does not pull in the enforcing modules.

See :mod:`codd.contracts_registry` for the conceptual overview. The contract rows below
are sourced from:

* the dogfood ledger findings v2.28–v2.37 (``dogfood/ledger.yaml``) — the
  KNOWN coherence contracts CoDD already enforces;
* GPT-5.5 Pro's §2 classification matrix "空セル：先回りで叩くべき潜在課題"
  table — the PREDICTED-but-unenforced cells (the proactive backlog);
* the outer-layer contracts GPT flagged (execution substrate: AI-invocation
  timeout/retry/budget; SUT output channel).

INVARIANTS (pinned by ``tests/test_contracts_registry.py``):

* every contract id is unique;
* ``covered``/``uncertified`` contracts name an ``authority`` and at least one
  ``certification_fixtures`` entry;
* ``uncovered`` contracts have ``authority is None`` AND a non-empty
  ``predicted_issue`` AND a non-empty ``proposed_gate``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── valid vocabularies (documentation + light validation) ───────────────────
# These mirror GPT §2 (node/edge types) and §3 (contract dimensions). They are
# advisory: the registry tests assert membership so a typo'd dimension/status
# is caught, but the vocab is intentionally open at the type level (str) so new
# edge types can be added without a schema migration.

VALID_STATUSES: frozenset[str] = frozenset(
    {"covered", "uncertified", "uncovered"}
)

VALID_FAIL_MODES: frozenset[str] = frozenset(
    {"honest_fail", "honest_red", "n/a"}
)

#: GPT §5A ``enforcement`` axis — how strongly the authority acts when it fires.
#: ``hard`` = the gate raises (StageError / honest-fail); ``warn`` = the gate
#: observes + reports but does NOT block (the rollout-safe state, e.g.
#: declared-output-completeness behind a config flag); ``noop`` = not enforced
#: (every ``uncovered`` backlog cell). The registry meta-test treats ``hard`` +
#: ``warn`` covered contracts as requiring an importable authority + a real
#: certification-fixture file; only ``hard`` contracts must be a blocking gate.
VALID_ENFORCEMENTS: frozenset[str] = frozenset({"hard", "warn", "noop"})

VALID_DIMENSIONS: frozenset[str] = frozenset(
    {
        "existence",
        "identity",
        "uniqueness",
        "ownership",
        "freshness",
        "observability",
        "authenticity",
        "execution",
        "repairability",
        "budget",
        "termination",
        # supply/topology dimensions named in GPT §2's edge×dimension table
        "completeness",
        "routing",
        "reproducibility",
        "discovery",
        "granularity",
        "order",
        "replay",
        "parsing",
    }
)


@dataclass(frozen=True)
class Contract:
    """One declared coherence contract (a typed edge with contract dimensions).

    Fields follow GPT §5A. A ``covered``/``uncertified`` contract records the
    enforcing ``authority`` symbol; an ``uncovered`` contract records the
    ``predicted_issue`` + ``proposed_gate`` (with ``authority=None``).
    """

    id: str
    source_node: str
    target_node: str
    edge_type: str
    dimensions: tuple[str, ...]
    authority: str | None
    fail_mode: str
    status: str
    certification_fixtures: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    predicted_issue: str = ""
    proposed_gate: str = ""
    #: GPT §5A enforcement axis (default derived from status in __post_init__:
    #: covered/uncertified → "hard"; uncovered → "noop"). A covered contract that
    #: is intentionally WARN-only (observe, do not block — e.g.
    #: declared-output-completeness behind a config flag) sets ``enforcement="warn"``
    #: explicitly; it still requires an importable authority + a real fixture.
    enforcement: str | None = None
    #: optional config flag that toggles a warn-vs-enforce contract (documentation
    #: + the audit can surface it). e.g. ``"implement.declared_output_completeness"``.
    config_flag: str = ""

    def __post_init__(self) -> None:
        # Light structural validation. Kept here (not only in tests) so that a
        # malformed contract fails LOUDLY at import time rather than producing a
        # silently-wrong coverage table.
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"contract {self.id!r}: invalid status {self.status!r} "
                f"(expected one of {sorted(VALID_STATUSES)})"
            )
        # Default the enforcement axis from status when unset (frozen → setattr):
        # covered/uncertified contracts ENFORCE (hard) by default; uncovered are
        # noop. An explicit value (e.g. "warn") is validated, never overwritten.
        if self.enforcement is None:
            object.__setattr__(
                self,
                "enforcement",
                "noop" if self.status == "uncovered" else "hard",
            )
        if self.enforcement not in VALID_ENFORCEMENTS:
            raise ValueError(
                f"contract {self.id!r}: invalid enforcement {self.enforcement!r} "
                f"(expected one of {sorted(VALID_ENFORCEMENTS)})"
            )
        if self.status == "uncovered" and self.enforcement != "noop":
            raise ValueError(
                f"contract {self.id!r}: uncovered contract must have "
                f"enforcement='noop' (got {self.enforcement!r})"
            )
        if self.status != "uncovered" and self.enforcement == "noop":
            raise ValueError(
                f"contract {self.id!r}: {self.status} contract must enforce "
                "(hard|warn), not noop"
            )
        if self.fail_mode not in VALID_FAIL_MODES:
            raise ValueError(
                f"contract {self.id!r}: invalid fail_mode {self.fail_mode!r} "
                f"(expected one of {sorted(VALID_FAIL_MODES)})"
            )
        if not self.dimensions:
            raise ValueError(f"contract {self.id!r}: dimensions must be non-empty")
        unknown = [d for d in self.dimensions if d not in VALID_DIMENSIONS]
        if unknown:
            raise ValueError(
                f"contract {self.id!r}: unknown dimension(s) {unknown} "
                f"(expected from {sorted(VALID_DIMENSIONS)})"
            )
        if self.status == "uncovered":
            if self.authority is not None:
                raise ValueError(
                    f"contract {self.id!r}: uncovered contract must have "
                    f"authority=None (got {self.authority!r})"
                )
            if not self.predicted_issue or not self.proposed_gate:
                raise ValueError(
                    f"contract {self.id!r}: uncovered contract must declare "
                    "both predicted_issue and proposed_gate"
                )
        else:  # covered | uncertified
            if not self.authority:
                raise ValueError(
                    f"contract {self.id!r}: {self.status} contract must name an "
                    "authority symbol"
                )
            if not self.certification_fixtures:
                raise ValueError(
                    f"contract {self.id!r}: {self.status} contract must name at "
                    "least one certification fixture"
                )

    @property
    def node_family(self) -> str:
        """Coarse grouping key for the matrix report (the source node's head).

        ``"ImplementTask.source_design_doc"`` -> ``"ImplementTask"``;
        ``"CoverageClaim"`` -> ``"CoverageClaim"``.
        """
        return self.source_node.split(".", 1)[0]


# ════════════════════════════════════════════════════════════════════════════
# KNOWN / COVERED contracts — mapped from ledger findings v2.28–v2.37
# (the GPT §2 matrix rows that have a known finding).
# ════════════════════════════════════════════════════════════════════════════

_COVERED: tuple[Contract, ...] = (
    # ── DocumentRef -> Document (GPT §2 row 1; v2.36 docref) ────────────────
    Contract(
        id="document_ref.binds_to_one_registered_document",
        source_node="ImplementTask.source_design_doc",
        target_node="Document",
        edge_type="reference",
        dimensions=("existence", "identity", "uniqueness"),
        authority="codd.reference_resolution.resolve_document_ref",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_reference_resolution.py::test_exact_path_resolves",
            "tests/test_reference_resolution.py::test_exact_node_id_resolves",
            "tests/test_reference_resolution.py::test_alias_resolves",
            "tests/test_reference_resolution.py::test_bug_repro_basename_only_recovered",
            "tests/test_reference_resolution.py::test_ambiguous_basename_honest_fail_with_candidates",
            "tests/test_reference_resolution.py::test_wrong_subcategory_honest_fail_not_recovered",
            "tests/test_reference_resolution.py::test_node_id_collision_fails",
        ),
        finding_ids=("PC-docref-resolution",),
    ),
    # ── Document registry index uniqueness (scanner side of docref) ─────────
    Contract(
        id="document_registry.unique_basename_and_node_id",
        source_node="Document",
        target_node="DocumentReferenceIndex",
        edge_type="reference",
        dimensions=("identity", "uniqueness"),
        authority="codd.scanner.build_document_reference_index",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_reference_resolution.py::test_node_id_collision_fails",
            "tests/test_reference_resolution.py::test_ambiguous_basename_honest_fail_with_candidates",
        ),
        finding_ids=("PC-docref-resolution",),
    ),
    # ── Task -> GeneratedArtifact ownership / orphan prohibition (v2.29) ─────
    Contract(
        id="task.owns_generated_artifacts_no_orphans",
        source_node="ImplementTask",
        target_node="GeneratedArtifact",
        edge_type="produces",
        dimensions=("ownership",),
        authority="codd.implement_oracle_scope.find_orphan_artifacts",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_implement_oracle_scope.py::test_find_orphan_artifacts_flags_unowned_file",
            "tests/test_implement_oracle_scope.py::test_find_orphan_artifacts_adopts_dir_owned_helper",
            "tests/test_implement_oracle_scope.py::test_find_orphan_artifacts_scaffold_config_not_orphan",
            "tests/test_implement_oracle_scope.py::test_gate_orphan_enforce_fails",
            "tests/test_implement_oracle_scope.py::test_gate_orphan_warn_records_but_passes",
        ),
        finding_ids=("F-greenfield-artifact-coherence",),
    ),
    # ── Task -> ExpectedOutput: done means the declared artifacts exist ──────
    Contract(
        id="task.done_requires_declared_outputs",
        source_node="ImplementTask.expected_outputs",
        target_node="GeneratedArtifact",
        edge_type="produces",
        dimensions=("completeness", "ownership", "routing"),
        authority="codd.greenfield.pipeline._verify_task_contract",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/greenfield/test_pipeline.py::test-task-source-only-FAILS",
            "tests/greenfield/test_pipeline.py::test-task-with-tests-PASSES",
            "tests/greenfield/test_pipeline.py::source-task+sibling-test-PASSES",
        ),
        finding_ids=("F-implement-task-done-artifact-blind",),
    ),
    # ── artifact.owner.unique.v1 — exactly ONE owner per artifact (GPT r2 §3.3) ─
    # The NEW hard gate: the orphan gate proves "≥1 owner"; this proves "≤1 owner"
    # (deterministic, BEFORE implement-oracle). The owner index's first-owner-wins
    # setdefault is the silent hole this closes.
    Contract(
        id="artifact.owner.unique.v1",
        source_node="ImplementTask",
        target_node="GeneratedArtifact",
        edge_type="produces",
        dimensions=("ownership", "uniqueness", "repairability"),
        authority="codd.implement_oracle_scope.validate_task_output_ownership_uniqueness",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_acg_owner_uniqueness.py::test_duplicate_exact_output_owner_raises",
            "tests/test_acg_owner_uniqueness.py::test_directory_and_exact_file_owner_conflict_raises",
            "tests/test_acg_owner_uniqueness.py::test_overlapping_directory_owners_raises",
            "tests/test_acg_owner_uniqueness.py::test_non_conflicting_distinct_files_ok",
            "tests/test_acg_owner_uniqueness.py::test_pipeline_wires_owner_uniqueness_before_oracle",
        ),
        finding_ids=("PC-owner-uniqueness", "F-greenfield-artifact-coherence"),
    ),
    # ── declared-output-completeness — WARN (GPT r2 §3.4; §5 "次PRでhard化") ──
    # Registered enforcement=warn behind a config flag: a task that declared EXACT
    # output paths should have produced them. Warn-only by default so this does
    # NOT hard-fail existing runs; ``implement.declared_output_completeness:
    # enforce`` flips it to a StageError. (The kind-level slice is the separate
    # hard contract ``task.done_requires_declared_outputs`` above.)
    Contract(
        id="task.declared_output_completeness",
        source_node="ImplementTask.expected_outputs",
        target_node="GeneratedArtifact",
        edge_type="produces",
        dimensions=("completeness", "existence"),
        authority="codd.greenfield.pipeline._check_declared_output_completeness",
        fail_mode="honest_red",
        status="covered",
        enforcement="warn",
        config_flag="implement.declared_output_completeness",
        certification_fixtures=(
            "tests/test_acg_owner_uniqueness.py::test_declared_output_completeness_warn_does_not_raise",
            "tests/test_acg_owner_uniqueness.py::test_declared_output_completeness_enforce_raises",
            "tests/test_acg_owner_uniqueness.py::test_declared_output_completeness_satisfied_ok",
        ),
        finding_ids=("PC-declared-output-completeness",),
    ),
    # ── Source/Test -> Module/Symbol import coherence (Python import / topology) ─
    Contract(
        id="source.imports_resolve_to_owned_symbols",
        source_node="GeneratedArtifact",
        target_node="Symbol",
        edge_type="reference",
        dimensions=("existence", "identity", "routing"),
        authority="codd.import_coherence.check_import_coherence",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_import_coherence.py (coherent passes; bare-basename fails; "
            "shadowing/source-outside/missing-init/manifest-mismatch fail; opt-out)",
            "tests/test_layout_profile.py (profile resolution, name normalization, "
            "idempotent non-clobbering scaffold)",
        ),
        finding_ids=("F-greenfield-import-coherence", "F-greenfield-artifact-coherence"),
    ),
    # ── Test -> Helper symbol coherence (the generated-test import contract) ─
    Contract(
        id="test.helper_symbols_exist_before_run",
        source_node="GeneratedTest",
        target_node="Symbol",
        edge_type="reference",
        dimensions=("existence", "identity"),
        authority="codd.test_import_coherence.check_test_import_coherence",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_test_import_coherence.py (22 cases)",
            "tests/greenfield/test_pipeline.py (2 verify-hook integration)",
        ),
        finding_ids=("F-greenfield-test-helper-symbol-incoherence",),
    ),
    # ── Manifest -> Lock -> Install freshness barrier (v2.33) ───────────────
    Contract(
        id="manifest.lock_is_fresh_before_install",
        source_node="Manifest",
        target_node="Lockfile",
        edge_type="freezes",
        dimensions=("freshness", "reproducibility"),
        authority="codd.dependency_lock_coherence.ensure_lock_freshness_barrier",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_dependency_lock_coherence.py::test_root_manifest_change_changes_digest",
            "tests/test_dependency_lock_coherence.py::test_workspace_manifest_change_changes_digest",
            "tests/test_dependency_lock_coherence.py::test_codex15_rerun_modified_manifest_passes_after_barrier",
            "tests/test_dependency_lock_coherence.py::test_unchanged_manifest_barrier_skips_no_refresh",
            "tests/test_dependency_lock_coherence.py::test_barrier_helper_raises_stage_error_on_hard_failure",
        ),
        finding_ids=("F-enablement-coverage",),
    ),
    # ── Manifest -> Toolchain-owned deps reconcile (reserved-manifest, partial) ─
    Contract(
        id="manifest.toolchain_deps_reconciled",
        source_node="Manifest",
        target_node="Toolchain",
        edge_type="owns",
        dimensions=("reproducibility", "ownership"),
        authority="codd.dependency_lock_coherence.reconcile_manifest_toolchain_deps",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_dependency_lock_coherence.py::test_old_toolchain_version_reconciled_to_profile",
            "tests/test_dependency_lock_coherence.py::test_toolchain_dep_misplaced_in_dependencies_is_moved",
            "tests/test_dependency_lock_coherence.py::test_app_and_domain_deps_left_untouched",
        ),
        finding_ids=("F-enablement-coverage", "F-greenfield-typescript-verify-repair-support"),
    ),
    # ── VB -> TestMarker traceability / namespace uniqueness (v2.30) ────────
    Contract(
        id="vb.traceable_to_canonical_marker",
        source_node="VerifiableBehavior",
        target_node="TestMarker",
        edge_type="claims",
        dimensions=("identity", "uniqueness", "completeness"),
        authority="codd.verifiable_behavior_audit.build_vb_coverage_audit",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_verifiable_behavior_coverage_gate.py (coherent 100%; genuine omission RED; "
            "multi-token covers line; src/tests scope discovery; project-root fallback; opt-out)",
            "tests/test_vb_declaration_coherence.py (canonical detection; role-aware heads; "
            "rogue-dual-declaration fails; collision=error)",
        ),
        finding_ids=(
            "F-vb-coverage-gate-false-red",
            "F-vb-gate-per-task-granularity",
            "F-greenfield-vb-coverage-gate",
        ),
    ),
    # ── Marker -> TestBlock -> Assertion authenticity (v2.31/v2.34) ─────────
    Contract(
        id="marker.attaches_to_authentic_assertion",
        source_node="TestMarker",
        target_node="Assertion",
        edge_type="claims",
        dimensions=("authenticity", "observability"),
        authority="codd.vb_marker_authenticity.build_authenticity_report",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_vb_marker_authenticity.py::test_gate_passes_for_genuine_covering_tests",
            "tests/test_vb_marker_authenticity.py::test_gate_rejects_marker_on_empty_test",
            "tests/test_vb_marker_authenticity.py::test_gate_rejects_marker_on_skipped_test",
            "tests/test_vb_marker_authenticity.py::test_gate_rejects_orphan_marker_stage1",
            "tests/test_vb_marker_authenticity.py::test_gate_passes_grouped_markers_with_helper_delegated_assertion",
        ),
        finding_ids=("F-vb-coverage-gate-false-red",),
    ),
    # ── Assertion helper -> deeper helper bounded evidence graph (2-hop) ─────
    # This is the KNOWN, COVERED slice of the GPT §2 "assertion-helper deep-hop"
    # cell: the authenticity report already follows helper->helper evidence with
    # a bounded hop/cycle guard. (The broader N-hop generalisation remains an
    # uncovered backlog cell, declared separately below.)
    Contract(
        id="assertion_helper.bounded_evidence_two_hop",
        source_node="Assertion.helper",
        target_node="Assertion.helper",
        edge_type="claims",
        dimensions=("authenticity", "termination"),
        authority="codd.vb_marker_authenticity.build_authenticity_report",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_vb_marker_authenticity.py::test_gate_passes_two_hop_rejected_run_helper",
            "tests/test_vb_marker_authenticity.py::test_gate_fails_constant_only_helper_spam",
            "tests/test_vb_marker_authenticity.py::test_gate_passes_barrel_star_reexport",
            "tests/test_vb_marker_authenticity.py::test_gate_fails_barrel_reexporting_constant_helper",
        ),
        finding_ids=("F-vb-coverage-gate-false-red",),
    ),
    # ── verify.campaign.observable.v1 — declared campaign MUST be readable ───
    # The NEW hard gate (GPT r2 §3.1): a profile that declares a verify campaign
    # whose report_format has NO adapter cannot have its executions observed.
    # Previously ``coherence_gate_applies`` returned False → silent NO-OP; now it
    # honest-fails (an unobservable verification is not a pass).
    Contract(
        id="verify.campaign.observable.v1",
        source_node="VerifyCampaign.report_format",
        target_node="RunnerReportAdapter",
        edge_type="executes",
        dimensions=("observability", "execution"),
        authority="codd.coverage_execution_coherence.certify_verify_campaign_observable",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_verify_campaign_observable.py::test_declared_campaign_without_adapter_raises",
            "tests/test_verify_campaign_observable.py::test_declared_campaign_with_adapter_ok",
            "tests/test_verify_campaign_observable.py::test_no_campaign_is_noop_ok",
            "tests/test_verify_campaign_observable.py::test_pipeline_honest_fails_on_adapterless_campaign",
        ),
        finding_ids=("PC-verify-campaign-observable", "F-verify-false-green"),
    ),
    # ── verify.campaign.clean_execution.v1 — the campaign result gates green ──
    # The NEW hard gate (GPT r2 §3.2): build_coherence_report reconciles only the
    # UNBLOCKED VBs against their covering files, so a FAILING test that covers no
    # declared VB (a plain integration/e2e/unit test) — or a non-zero runner exit —
    # was invisible to it and passed the coherence gate alone = a false-green.
    # enforce_campaign_clean_execution makes the campaign's own result a green
    # authority: any executed_failed_files, or exit_code != 0, is an honest-fail.
    Contract(
        id="verify.campaign.clean_execution.v1",
        source_node="VerifyCampaign",
        target_node="RunnerExecution",
        edge_type="executes",
        dimensions=("execution", "observability"),
        authority="codd.coverage_execution_coherence.enforce_campaign_clean_execution",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_coverage_execution_coherence.py::test_clean_execution_failed_file_raises",
            "tests/test_coverage_execution_coherence.py::test_clean_execution_nonzero_exit_raises",
            "tests/test_coverage_execution_coherence.py::test_clean_execution_all_pass_exit_zero_is_ok",
            "tests/test_coverage_execution_coherence.py::test_clean_execution_closes_false_green_for_failing_non_vb_test",
        ),
        finding_ids=("PC-campaign-clean-execution", "F-verify-false-green"),
    ),
    # ── CoverageClaim -> VerifyExecution observability (v2.32) ───────────────
    Contract(
        id="coverage_claim.requires_executed_passed_evidence",
        source_node="CoverageClaim",
        target_node="VerifyExecution",
        edge_type="executes",
        dimensions=("observability", "execution"),
        authority="codd.coverage_execution_coherence.enforce_coverage_execution_coherence",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_coverage_execution_coherence.py::test_coherence_passes_when_campaign_runs_unit_and_e2e",
            "tests/test_coverage_execution_coherence.py::test_coherence_hard_fails_when_e2e_not_executed",
            "tests/test_coverage_execution_coherence.py::test_coherence_hard_fails_when_covering_test_ran_but_failed",
            "tests/test_coverage_execution_coherence.py::test_coherence_hard_fails_with_no_execution_at_all",
            "tests/test_coverage_execution_coherence.py::test_vitest_adapter_skipped_case_does_not_make_file_pass",
        ),
        finding_ids=("F-verify-false-green",),
    ),
    # ── RunnerInventory -> RunnerCommand discovery/run parity (file-level) ───
    # The KNOWN slice: the single TestInventory is shared between the VB audit
    # and the coverage-execution gate (one glob, no divergence). The per-runner
    # CASE-level adapter generalisation is an uncovered backlog cell below.
    Contract(
        id="test_inventory.shared_single_source",
        source_node="TestInventory",
        target_node="RunnerCommand",
        edge_type="executes",
        dimensions=("discovery", "observability"),
        authority="codd.coverage_execution_coherence.build_test_inventory",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_coverage_execution_coherence.py::test_test_inventory_classifies_kinds_and_annotates_execution",
            "tests/test_coverage_execution_coherence.py::test_test_inventory_shares_glob_with_vb_audit",
        ),
        finding_ids=("F-greenfield-vitest-e2e-suffix-not-collected",),
    ),
    # ── Diagnostic -> RerunScope convergence / termination (v2.28 oscillation) ─
    Contract(
        id="diagnostic.rerun_scope_converges_or_honest_fails",
        source_node="Diagnostic",
        target_node="RerunScope",
        edge_type="repairs",
        dimensions=("termination", "repairability"),
        authority="codd.implement_oracle.run_implement_oracle_gate",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_implement_oracle_broad_campaign.py::test_oscillation_honest_fail_no_infinite_loop",
            "tests/test_implement_oracle_broad_campaign.py::test_budget_exhaustion_honest_fail_with_audit",
        ),
        finding_ids=("PC-broad-repair-campaign",),
    ),
    # ── Diagnostic -> broad RerunScope budget (v2.37 broad campaign) ─────────
    Contract(
        id="diagnostic.broad_repair_is_budgeted_and_scoped",
        source_node="Diagnostic.broad",
        target_node="RerunScope",
        edge_type="repairs",
        dimensions=("budget", "termination", "execution", "repairability"),
        authority="codd.implement_oracle._execute_broad_campaign",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_implement_oracle_broad_campaign.py::test_chunk_local_success_does_not_green_gate",
            "tests/test_implement_oracle_broad_campaign.py::test_supplier_fix_clears_importers_without_importer_rerun",
            "tests/test_implement_oracle_broad_campaign.py::test_residual_only_reruns_owner_importers_not_all",
            "tests/test_implement_oracle_broad_campaign.py::test_campaign_is_bounded_supplier_once_and_recheck_cap",
            "tests/test_implement_oracle_broad_campaign.py::test_broad_phase_with_allowed_paths_reverts_out_of_scope",
        ),
        finding_ids=("PC-broad-repair-campaign",),
    ),
    # ── Failure -> RepairPatch editable-scope (anti-oracle-weakening) ────────
    Contract(
        id="repair_patch.cannot_weaken_oracle_scope",
        source_node="RepairPatch",
        target_node="GeneratedArtifact",
        edge_type="repairs",
        dimensions=("ownership", "repairability"),
        authority="codd.repair.auto_scope_guard",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/repair/test_auto_repair_optin_and_scope.py (scope rejects failing-test edit / "
            "codd.yaml weaken / spec-doc edit; harness_contract_violation scaffold-fix allowed; "
            "max-files valve escalates)",
        ),
        finding_ids=("F-greenfield-autorepair-optin-and-scope", "F-autorepair-test-command-unrepairable"),
    ),
    # ── SUTChannel -> FileArtifact payload parsing (codex stdout/file-writing) ─
    Contract(
        id="sut_channel.implement_payload_parsed_from_canonical_channel",
        source_node="SUTChannel",
        target_node="GeneratedArtifact",
        edge_type="produces",
        dimensions=("parsing", "routing"),
        authority="codd.ai_invoke.invoke_file_writing_agent",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_ai_invoke.py::test_file_writing_agent_falls_back_to_stdout_contract",
            "tests/test_ai_invoke.py::test_file_writing_agent_prefers_on_disk_writes_over_stdout",
            "tests/test_ai_invoke.py::test_file_writing_agent_no_files_and_no_contract_still_raises",
            "tests/implement/test_root_artifacts.py (bare-basename reroot, preserved skip semantics)",
        ),
        finding_ids=("F-codex-filewriting-stdout", "F-codex-bare-basename-dropped"),
    ),
    # ── E2E runtime import contract (service/e2e readiness, KNOWN slice) ─────
    Contract(
        id="e2e.runtime_import_contract_coherent",
        source_node="GeneratedTest.e2e",
        target_node="Symbol",
        edge_type="executes",
        dimensions=("existence", "execution", "routing"),
        authority="codd.e2e_contract_coherence",
        fail_mode="honest_red",
        status="covered",
        certification_fixtures=(
            "tests/test_e2e_contract_coherence.py (18 cases)",
            "tests/greenfield/test_pipeline.py (2)",
        ),
        finding_ids=("F-greenfield-e2e-runtime-import-contract",),
    ),
)


# ════════════════════════════════════════════════════════════════════════════
# OUTER-LAYER contracts (GPT §1/§5): execution substrate + SUT channel.
# Not part of the Artifact Coherence Graph proper, but flagged as contracts CoDD
# must hold. Status reflects whether they are enforced today.
# ════════════════════════════════════════════════════════════════════════════

_OUTER: tuple[Contract, ...] = (
    # AI invocation: bounded wall-clock timeout (kills hangs) — ENFORCED.
    Contract(
        id="ai_invocation.bounded_wall_clock_timeout",
        source_node="AIInvocation",
        target_node="Result",
        edge_type="executes",
        dimensions=("termination", "budget"),
        authority="codd.ai_invoke.invoke_ai",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_ai_invoke.py::test_invoke_ai_passes_wall_clock_timeout_to_subprocess",
            "tests/test_ai_invoke.py::test_invoke_ai_persistent_timeout_fails_after_bounded_attempts",
            "tests/test_ai_invoke.py::test_invoke_ai_timeout_is_classified_transient",
            "tests/test_ai_invoke.py::test_default_call_timeout_matches_shared_ssot",
        ),
        finding_ids=("F-ai-call-hang",),
    ),
    # AI invocation: transient transport auto-retry — ENFORCED.
    Contract(
        id="ai_invocation.transient_transport_auto_retry",
        source_node="AIInvocation",
        target_node="Result",
        edge_type="executes",
        dimensions=("repairability", "budget"),
        authority="codd.ai_invoke.invoke_ai",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_ai_invoke.py::test_invoke_ai_auto_retries_transient_socket_error_then_succeeds",
            "tests/test_ai_invoke.py::test_invoke_ai_transient_auto_retry_is_bounded",
            "tests/test_ai_invoke.py::test_invoke_ai_does_not_auto_retry_permanent_auth_error",
        ),
        finding_ids=("F-transient-transport",),
    ),
    # AI invocation: output-ceiling (32k) budget recovery — ENFORCED.
    Contract(
        id="ai_invocation.output_ceiling_budget_recovery",
        source_node="AIInvocation",
        target_node="Result",
        edge_type="executes",
        dimensions=("budget", "repairability"),
        authority="codd.ai_invoke.invoke_ai",
        fail_mode="honest_fail",
        status="covered",
        certification_fixtures=(
            "tests/test_ai_invoke.py::test_invoke_ai_recovers_output_ceiling_by_raising_budget",
            "tests/test_ai_invoke.py::test_invoke_ai_output_ceiling_not_retried_when_budget_already_high",
            "tests/test_ai_invoke.py::test_invoke_ai_output_ceiling_recovery_threads_to_file_writing_agent",
        ),
        finding_ids=("F-output-32k",),
    ),
)


# ════════════════════════════════════════════════════════════════════════════
# UNCOVERED cells — GPT §2/§3 "空セル：先回りで叩くべき潜在課題".
# authority=None; each carries predicted_issue + proposed_gate from the table.
# These are the PROACTIVE BACKLOG (NOT implemented in this minimal step).
#
# The FIRST block (``_UNCOVERED_PRECISE``) is GPT round-2 §3's CODE-GROUNDED,
# PRIORITIZED set — the remaining §3 cells after §3.1 (verify-campaign-observable)
# and §3.3 (owner-uniqueness) were promoted to COVERED hard gates this PR, and
# §3.4 (declared-output-completeness) to a COVERED warn gate. Each precise cell
# carries the code anchor (in predicted_issue) + the proposed_gate WITH its
# negative-fixture name (per GPT §3). §3.9 is the refined
# ``plan.stage_order_topologically_certified`` above.
#
# The SECOND block (``_UNCOVERED_BACKLOG``) is the round-1 §2 空セル list — still
# valid backlog GPT did not supersede.
# ════════════════════════════════════════════════════════════════════════════

_UNCOVERED_PRECISE: tuple[Contract, ...] = (
    # GPT r2 §3.5 — LayoutProfile scaffold-config certification.
    Contract(
        id="scaffold.config_certified_before_verify.v1",
        source_node="LayoutProfile",
        target_node="ScaffoldConfig",
        edge_type="owns",
        dimensions=("existence", "observability", "ownership"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "TS scaffolds tsconfig.json + vitest.config.ts create-only and "
            "_ensure_test_runner is advisory (swallows scaffold exceptions); only "
            "tsconfig scope is hard-gated (certify_oracle_scope). Whether "
            "vitest.config.ts actually includes the .e2e.* convention is not "
            "certified — it is only caught LATER when the campaign scans 0 e2e."
        ),
        proposed_gate=(
            "certify_profile_scaffold_contract(project_root, profile) before verify: "
            "tsconfig includes source+test roots; vitest.config.ts includes the .e2e. "
            "convention; package.json has the harness scripts/deps or the lock "
            "finalizer can reconcile. negative fixture: a vitest.config.ts that "
            "includes only .test.ts (no .e2e.ts) → red at scaffold certification, "
            "not deferred to the campaign."
        ),
    ),
    # GPT r2 §3.6 — recognized test file → parseable TestBlock authenticity strict.
    Contract(
        id="authenticity.observable_in_supported_stack.v1",
        source_node="TestFile",
        target_node="TestBlock",
        edge_type="claims",
        dimensions=("authenticity", "observability", "parsing"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "build_authenticity_report puts a marker-bearing recognized test file "
            "into degraded_paths (skips Stage 2/3) when the adapter handles the file "
            "but parses NO blocks; report.passed only checks violations — so a "
            "supported-stack (TS/Python) test whose wrapper the parser cannot read "
            "is 'authenticity unknown' yet can degrade-pass."
        ),
        proposed_gate=(
            "strict_authenticity_observability=True greenfield default: when adapter "
            "handles the file AND markers present AND no blocks parsed → "
            "violation(kind='unobservable_test_structure'); unknown languages stay "
            "warn/degrade. negative fixture: tests/foo.test.ts with covers vb=VB-1 "
            "in a wrapper the parser does not recognize → red (today degrades to pass)."
        ),
    ),
    # GPT r2 §3.7 — Python source/test import coherence implement-time oracle.
    Contract(
        id="python.import_coherence_oracle.v1",
        source_node="GeneratedArtifact.python",
        target_node="Symbol",
        edge_type="reference",
        dimensions=("existence", "identity", "execution"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "the Python LayoutProfile sets implement_oracle=None (composite oracle "
            "deferred); pytest --collect-only + verify-stage gates are the backstop, "
            "so a source module with an undefined import/symbol that no test imports "
            "is invisible at implement-time."
        ),
        proposed_gate=(
            "CompositePythonImplementOracle on the profile: py_compile over "
            "source+tests; static import resolver / pyflakes; pytest --collect-only; "
            "optional public-API smoke (opt-in). negative fixture: "
            "src/app/hidden.py with `from .missing import X` that no test imports → "
            "pytest green but composite oracle red."
        ),
    ),
    # GPT r2 §3.8 — source_design_doc → unregistered doc provenance strict.
    Contract(
        id="source_design_doc.registered_doc_strict.v1",
        source_node="ImplementTask.source_design_doc",
        target_node="Document.registered",
        edge_type="reference",
        dimensions=("identity", "authenticity", "uniqueness"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "resolve_document_ref has a compatibility branch that binds an EXISTING "
            "markdown path to a synthetic doc:<rel> even when it carries no CoDD "
            "frontmatter / is not in the DocumentReferenceIndex; source_design_doc is "
            "the task-derivation authority, so an unregistered scratch doc can drive "
            "derivation in non-strict mode."
        ),
        proposed_gate=(
            "resolve_document_ref(strict_registered=True) for source_design_doc: "
            "require a frontmatter-registered document, not just an existing file. "
            "negative fixture: docs/scratch.md exists without CoDD frontmatter and a "
            "task references source_design_doc: docs/scratch.md → red in strict mode."
        ),
    ),
)


_UNCOVERED_BACKLOG: tuple[Contract, ...] = (
    Contract(
        id="depends_on.resolves_to_one_task_or_document",
        source_node="ImplementTask.depends_on",
        target_node="ImplementTask",
        edge_type="reference",
        dimensions=("existence", "identity", "uniqueness", "order"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "task dependency id typo / basename / stale id silently breaks ordering "
            "(scope explicitly deferred in the v2.36 docref PR)."
        ),
        proposed_gate=(
            "extend the typed reference resolver to depends_on; "
            "ambiguous/unresolved -> honest-fail."
        ),
    ),
    Contract(
        id="expected_outputs.declared_matches_produced",
        source_node="ImplementTask.expected_outputs",
        target_node="FutureArtifact",
        edge_type="produces",
        dimensions=("completeness", "routing"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "declared path/kind disagrees with the actual generated artifact; today a "
            "missing declared output only hard-fails, with no targeted retry."
        ),
        proposed_gate=(
            "targeted retry for a missing declared output + a final declared-output "
            "completeness gate."
        ),
    ),
    Contract(
        id="doc_cross_link.resolves_to_one_target",
        source_node="Document.cross_link",
        target_node="Document",
        edge_type="reference",
        dimensions=("existence", "identity", "uniqueness"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "an in-doc link stays a raw string and breaks, or mis-binds to a different "
            "doc/VB/AC."
        ),
        proposed_gate="document cross-link resolver + collision fixture.",
    ),
    Contract(
        id="task.design_doc_digest_fresh",
        source_node="ImplementTask",
        target_node="DesignDoc.digest",
        edge_type="reference",
        dimensions=("freshness", "identity"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "after a design doc is updated, a stale derived task lingers against the old "
            "content."
        ),
        proposed_gate=(
            "store the source-doc digest in the derived task; digest drift -> re-derive "
            "or honest-fail."
        ),
    ),
    # GPT r2 §3.9 (precise): task_dependency_order today = DECLARATION order
    # (``index.all_task_ids``), which chunked-broad's last-resort pass trusts.
    Contract(
        id="plan.stage_order_topologically_certified",
        source_node="Plan",
        target_node="Stage",
        edge_type="executes",
        dimensions=("order",),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "task_dependency_order assumes declaration order IS dependency order "
            "(codd.implement_oracle_scope.task_dependency_order ranks by "
            "index.all_task_ids); a planner that emits a supplier AFTER its consumer "
            "destabilizes the chunked_broad last-resort repair order."
        ),
        proposed_gate=(
            "derive_task_dependency_order_from_artifact_edges() — topological order "
            "from the import graph / declared depends_on; cycles honest-fail or chunk "
            "per-SCC. negative fixture: consumer→supplier declaration order where the "
            "consumer imports the supplier; assert the derived order is supplier-first."
        ),
    ),
    Contract(
        id="output_owner.rerun_does_not_delete_others",
        source_node="ImplementTask.output_owner",
        target_node="GeneratedArtifact",
        edge_type="repairs",
        dimensions=("ownership",),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "a scoped rerun deletes or overwrites another task's artifact."
        ),
        proposed_gate=(
            "owner-index diff gate; treat out-of-scope deletes as write-fence "
            "violations too."
        ),
    ),
    Contract(
        id="test_inventory.single_source_meta_test",
        source_node="TestInventory",
        target_node="AllGates",
        edge_type="executes",
        dimensions=("discovery", "observability"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "VB scan / authenticity / e2e / verify each carry a DIFFERENT glob -> a test "
            "visible to one gate is invisible to another."
        ),
        proposed_gate="a meta-test mandating central TestInventory use by every gate.",
    ),
    Contract(
        id="runner_report.case_level_marker_verified",
        source_node="RunnerReport",
        target_node="TestCase",
        edge_type="executes",
        dimensions=("observability", "execution"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "only file-level pass is observed; a per-case covers-marker cannot be "
            "verified executed."
        ),
        proposed_gate=(
            "per-runner case-level adapter certification; file-level degrade fails in "
            "strict mode."
        ),
    ),
    Contract(
        id="vb_declaration.namespace_range_and_alias",
        source_node="VerifiableBehavior",
        target_node="Namespace",
        edge_type="claims",
        dimensions=("identity", "uniqueness"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "range notation / duplicate semantics / a non-canonical first-column VB "
            "slips through (atomic VB id + canonical owner exist, but range/alias forms "
            "are not fixtured)."
        ),
        proposed_gate="add range/alias fixtures to the VB namespace contract.",
    ),
    Contract(
        id="assertion_helper.bounded_evidence_n_hop",
        source_node="Assertion.helper",
        target_node="Assertion.helper",
        edge_type="claims",
        dimensions=("authenticity", "termination"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "a helper calling a helper at 3-hop+ depth yields a false-red or "
            "false-green beyond the currently-pinned 2-hop coverage."
        ),
        proposed_gate=(
            "bounded evidence-graph contract: explicit max-hops + cycle guard "
            "certified for N>2."
        ),
    ),
    Contract(
        id="toolchain.native_oracle_scope_includes_tests",
        source_node="Toolchain",
        target_node="NativeOracleScope",
        edge_type="executes",
        dimensions=("execution", "observability"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "the compiler/oracle does not see tests/helpers/e2e, so an intentionally "
            "broken helper still goes green."
        ),
        proposed_gate=(
            "profile certification: an intentionally-broken helper/test MUST red."
        ),
    ),
    Contract(
        id="manifest.reserved_toolchain_surface_reconciled",
        source_node="Manifest.reserved_surface",
        target_node="Toolchain",
        edge_type="owns",
        dimensions=("ownership", "reproducibility"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "the SUT overwrites codd:verify / runner config in the manifest at a later "
            "stage."
        ),
        proposed_gate="reserved manifest surface reconcile + diff gate.",
    ),
    Contract(
        id="lock_freshness.non_npm_profiles",
        source_node="Lockfile",
        target_node="Toolchain",
        edge_type="freezes",
        dimensions=("freshness", "reproducibility"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "no lock-freshness adapter for uv / poetry / cargo / go.sum (only the "
            "npm/package-lock path is certified)."
        ),
        proposed_gate="lock-freshness contract fixtures per profile.",
    ),
    Contract(
        id="repair_attribution.non_py_ts_languages",
        source_node="Failure",
        target_node="EditableSource",
        edge_type="repairs",
        dimensions=("repairability", "ownership"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "a new-runner failure has no editable-source set, so repair cannot attribute "
            "it (only pytest/vitest attribution is certified)."
        ),
        proposed_gate="a failure-attribution adapter DoD per language.",
    ),
    Contract(
        id="patch_scope.does_not_weaken_generated_tests",
        source_node="RepairPatch",
        target_node="GeneratedTest",
        edge_type="repairs",
        dimensions=("authenticity", "repairability"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "a test-failure repair weakens the assertions to go green (covered for "
            "pytest-style edits; not for every runner's test shape)."
        ),
        proposed_gate="read-only evidence invariant fixture per runner.",
    ),
    Contract(
        id="environment.skipped_tests_not_green",
        source_node="ExecutionEnvironment",
        target_node="TestCase",
        edge_type="executes",
        dimensions=("execution", "observability"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "tests skip because the environment is missing, yet the run is treated as "
            "green."
        ),
        proposed_gate="skip/todo-as-unverified invariant.",
    ),
    Contract(
        id="e2e_runtime.service_readiness_and_teardown",
        source_node="Service.e2e",
        target_node="RuntimeReadiness",
        edge_type="executes",
        dimensions=("execution", "observability", "termination"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "the server is not started / wrong port / a background process leaks "
            "(the e2e-import slice is covered, but live runtime readiness is not)."
        ),
        proposed_gate="harness-owned e2e runtime campaign + teardown evidence.",
    ),
    Contract(
        id="checkpoint.partial_repair_resume_no_reloop",
        source_node="Checkpoint",
        target_node="RepairState",
        edge_type="resumes",
        dimensions=("replay", "termination"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "budgeted-repair partial progress re-loops on resume instead of replaying "
            "completed work."
        ),
        proposed_gate="campaign state digest + an executed_key fixture.",
    ),
    Contract(
        id="sut_channel.contract_holds_across_all_stages",
        source_node="SUTChannel",
        target_node="OtherStages",
        edge_type="produces",
        dimensions=("parsing", "routing"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "the stdout/file payload contract breaks in assemble/extract/fix stages "
            "(only the implement stage is certified)."
        ),
        proposed_gate="a channel-contract fixture across all AI call sites.",
    ),
    Contract(
        id="config.monorepo_workspace_roots",
        source_node="Config.profile",
        target_node="WorkspaceRoots",
        edge_type="owns",
        dimensions=("ownership", "discovery", "reproducibility"),
        authority=None,
        fail_mode="honest_fail",
        status="uncovered",
        predicted_issue=(
            "multiple package roots / test roots / lock roots in a monorepo/workspace "
            "confuse the single-root assumptions."
        ),
        proposed_gate="workspace profile certification.",
    ),
    # GPT also listed the SUT-output-channel + reserved-manifest as front-load
    # candidates; the SUT-output-channel implement slice is COVERED above, the
    # other-stages generalisation is the uncovered cell directly above. The
    # native-oracle-scope certification (front-load top candidate) is the
    # toolchain cell above. Round out with the toolchain-profile e2e-routing cell
    # GPT flagged under playwright-hardcoded routing:
    Contract(
        id="toolchain.e2e_runner_routing_per_modality",
        source_node="Toolchain.e2e",
        target_node="RunnerCommand",
        edge_type="executes",
        dimensions=("routing", "discovery"),
        authority=None,
        fail_mode="honest_red",
        status="uncovered",
        predicted_issue=(
            "e2e runner routing is hard-coded per tool (e.g. playwright), so a "
            "different modality's e2e is mis-routed or not collected."
        ),
        proposed_gate=(
            "modality-routed e2e runner selection certified per profile "
            "(generalises the vitest .e2e include-glob fix)."
        ),
    ),
)


# ── the assembled, frozen registry ──────────────────────────────────────────
#: All uncovered cells = the precise GPT-r2-§3 prioritized set FIRST, then the
#: round-1 §2 backlog. (Kept as two tuples for readability; the registry is flat.)
_UNCOVERED: tuple[Contract, ...] = _UNCOVERED_PRECISE + _UNCOVERED_BACKLOG

REGISTRY: tuple[Contract, ...] = _COVERED + _OUTER + _UNCOVERED


# ── helpers (GPT §5A public surface) ────────────────────────────────────────
def contracts_by_status() -> dict[str, list[Contract]]:
    """Group :data:`REGISTRY` by ``status`` (covered/uncertified/uncovered).

    Always returns all three keys (possibly with empty lists) so callers can
    iterate deterministically.
    """
    grouped: dict[str, list[Contract]] = {s: [] for s in sorted(VALID_STATUSES)}
    for c in REGISTRY:
        grouped[c.status].append(c)
    return grouped


def coverage_summary() -> dict[str, int]:
    """Counts by status plus ``total`` (the coverage headline).

    Keys: ``total``, ``covered``, ``uncertified``, ``uncovered``.
    """
    grouped = contracts_by_status()
    summary = {status: len(items) for status, items in grouped.items()}
    summary["total"] = len(REGISTRY)
    return summary

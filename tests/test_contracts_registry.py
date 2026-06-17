"""Tests for the ACG Contract Registry + certification matrix.

This is the AUDIT-layer test. It asserts the registry's structural invariants,
the coverage-summary consistency, the certify CLI's "visualize-first" exit
semantics, and the no-drift property between the registry and the committed
``dogfood/contract_matrix.yaml``. It does NOT exercise any CoDD gate behaviour
(the registry is declarative).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from codd.contracts_registry import (
    REGISTRY,
    Contract,
    contracts_by_status,
    coverage_summary,
)
from codd.contracts_registry import certify as certify_mod
from codd.contracts_registry import generate_matrix as gen_mod
from codd.contracts_registry.registry import (
    VALID_DIMENSIONS,
    VALID_ENFORCEMENTS,
    VALID_FAIL_MODES,
    VALID_STATUSES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "dogfood" / "contract_matrix.yaml"


# ── registry loads + required fields + unique ids ───────────────────────────
def test_registry_is_non_empty_tuple_of_contracts():
    assert isinstance(REGISTRY, tuple)
    assert REGISTRY, "registry must not be empty"
    assert all(isinstance(c, Contract) for c in REGISTRY)


def test_every_contract_has_required_fields():
    for c in REGISTRY:
        assert c.id and isinstance(c.id, str)
        assert c.source_node and isinstance(c.source_node, str)
        assert c.target_node and isinstance(c.target_node, str)
        assert c.edge_type and isinstance(c.edge_type, str)
        assert c.dimensions and isinstance(c.dimensions, tuple)
        assert c.status in VALID_STATUSES
        assert c.fail_mode in VALID_FAIL_MODES
        assert all(d in VALID_DIMENSIONS for d in c.dimensions)


def test_contract_ids_are_unique():
    ids = [c.id for c in REGISTRY]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"duplicate contract ids: {dups}"


def test_contract_is_frozen():
    c = REGISTRY[0]
    with pytest.raises(Exception):
        c.id = "mutated"  # type: ignore[misc]


# ── coverage_summary consistency ────────────────────────────────────────────
def test_coverage_summary_counts_are_consistent():
    summary = coverage_summary()
    assert summary["total"] == len(REGISTRY)
    assert (
        summary["covered"] + summary["uncertified"] + summary["uncovered"]
        == summary["total"]
    )
    grouped = contracts_by_status()
    assert summary["covered"] == len(grouped["covered"])
    assert summary["uncertified"] == len(grouped["uncertified"])
    assert summary["uncovered"] == len(grouped["uncovered"])


def test_contracts_by_status_partitions_the_registry():
    grouped = contracts_by_status()
    assert set(grouped.keys()) == set(VALID_STATUSES)
    flat = [c for items in grouped.values() for c in items]
    assert len(flat) == len(REGISTRY)
    assert {c.id for c in flat} == {c.id for c in REGISTRY}


# ── covered contracts name authority + fixture; uncovered have issue+gate ────
def test_covered_contracts_name_authority_and_fixture():
    for c in REGISTRY:
        if c.status in ("covered", "uncertified"):
            assert c.authority, f"{c.id}: {c.status} must name an authority"
            assert c.certification_fixtures, (
                f"{c.id}: {c.status} must name at least one certification fixture"
            )


def test_uncovered_contracts_have_prediction_and_no_authority():
    uncovered = contracts_by_status()["uncovered"]
    assert uncovered, "expect a non-empty uncovered backlog (GPT §2 空セル)"
    for c in uncovered:
        assert c.authority is None, f"{c.id}: uncovered must have authority=None"
        assert c.predicted_issue, f"{c.id}: uncovered must declare predicted_issue"
        assert c.proposed_gate, f"{c.id}: uncovered must declare proposed_gate"


def test_known_covered_contracts_are_present():
    # A few load-bearing rows from the ledger mapping must exist (guards against
    # an accidental registry truncation).
    ids = {c.id for c in REGISTRY}
    for expected in (
        "document_ref.binds_to_one_registered_document",
        "coverage_claim.requires_executed_passed_evidence",
        "marker.attaches_to_authentic_assertion",
        "diagnostic.broad_repair_is_budgeted_and_scoped",
        "task.owns_generated_artifacts_no_orphans",
    ):
        assert expected in ids, f"missing known covered contract {expected!r}"


def test_known_uncovered_backlog_cells_are_present():
    ids = {c.id for c in REGISTRY}
    for expected in (
        "depends_on.resolves_to_one_task_or_document",
        "expected_outputs.declared_matches_produced",
        "doc_cross_link.resolves_to_one_target",
    ):
        assert expected in ids, f"missing known backlog cell {expected!r}"


# ── the 2 NEW hard gates + the warn gate are registered as COVERED ──────────
def test_new_round2_gates_are_covered():
    by_id = {c.id: c for c in REGISTRY}
    # artifact.owner.unique.v1 — new hard gate (GPT r2 §3.3).
    owner = by_id["artifact.owner.unique.v1"]
    assert owner.status == "covered" and owner.enforcement == "hard"
    assert owner.authority == (
        "codd.implement_oracle_scope.validate_task_output_ownership_uniqueness"
    )
    # verify.campaign.observable.v1 — new hard gate (GPT r2 §3.1).
    obs = by_id["verify.campaign.observable.v1"]
    assert obs.status == "covered" and obs.enforcement == "hard"
    assert obs.authority == (
        "codd.coverage_execution_coherence.certify_verify_campaign_observable"
    )
    # task.declared_output_completeness — registered WARN behind a config flag.
    dly = by_id["task.declared_output_completeness"]
    assert dly.status == "covered" and dly.enforcement == "warn"
    assert dly.config_flag == "implement.declared_output_completeness"


def test_precise_round2_uncovered_cells_are_present():
    """GPT round-2 §3's remaining precise cells are in the backlog."""
    ids = {c.id for c in REGISTRY}
    for expected in (
        "verify.campaign.clean_execution.v1",  # §3.2
        "scaffold.config_certified_before_verify.v1",  # §3.5
        "authenticity.observable_in_supported_stack.v1",  # §3.6
        # §3.7 was the Python composite-oracle cell; it is now SPLIT — the
        # import-resolution + test-collection layers are COVERED hard gates (see
        # test_python_implement_oracle.py), and the undefined-name lint + public-API
        # smoke residuals stay uncovered backlog.
        "python.undefined_name_lint.v1",  # §3.7 residual (uncovered)
        "python.public_api_smoke.v1",  # §3.7 residual (uncovered)
        "source_design_doc.registered_doc_strict.v1",  # §3.8
    ):
        assert expected in ids, f"missing precise §3 cell {expected!r}"


# ── enforcement axis (GPT §5A) ───────────────────────────────────────────────
def test_every_contract_has_valid_enforcement():
    for c in REGISTRY:
        assert c.enforcement in VALID_ENFORCEMENTS, f"{c.id}: bad enforcement"
        if c.status == "uncovered":
            assert c.enforcement == "noop", f"{c.id}: uncovered must be noop"
        else:
            assert c.enforcement in ("hard", "warn"), f"{c.id}: covered must enforce"


def test_uncovered_with_non_noop_enforcement_is_rejected():
    with pytest.raises(ValueError):
        Contract(
            id="bad.uncovered_hard",
            source_node="A",
            target_node="B",
            edge_type="reference",
            dimensions=("existence",),
            authority=None,
            fail_mode="honest_fail",
            status="uncovered",
            predicted_issue="x",
            proposed_gate="y",
            enforcement="hard",  # illegal for uncovered
        )


def test_invalid_enforcement_value_is_rejected():
    with pytest.raises(ValueError):
        Contract(
            id="bad.enforcement",
            source_node="A",
            target_node="B",
            edge_type="reference",
            dimensions=("existence",),
            authority="codd.x",
            fail_mode="honest_fail",
            status="covered",
            certification_fixtures=("t",),
            enforcement="sometimes",  # not in VALID_ENFORCEMENTS
        )


# ── GPT §5 CI meta-test: every ENFORCED contract is real ─────────────────────
def test_every_enforced_contract_has_importable_authority():
    """GPT §5 CI rule: ``enforcement: hard``/``warn`` ⇒ ``authority`` import-resolves.

    A covered contract that names an authority symbol the codebase does not export
    is a LIE in the coverage table — this catches an authority typo or a deleted
    function the registry still claims.
    """
    import importlib

    def _authority_resolves(dotted: str) -> bool:
        # Accept EITHER a ``module.symbol`` (a function/class) OR a bare importable
        # module path (a couple of contracts name a whole module as the authority,
        # e.g. ``codd.e2e_contract_coherence``).
        module_name, _, symbol = dotted.rpartition(".")
        if module_name:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 — fall through to whole-module import
                module = None
            if module is not None and hasattr(module, symbol):
                return True
        try:  # whole dotted path as a module
            importlib.import_module(dotted)
            return True
        except Exception:  # noqa: BLE001
            return False

    for c in REGISTRY:
        if c.enforcement in ("hard", "warn"):
            assert c.authority, f"{c.id}: enforced contract must name an authority"
            assert _authority_resolves(c.authority), (
                f"{c.id}: authority {c.authority!r} does not import-resolve "
                "(typo or deleted function/module?)"
            )


def test_every_enforced_contract_fixture_file_exists():
    """GPT §5 CI rule: every ``hard``/``warn`` contract has ≥1 certification fixture
    whose test FILE exists on disk (the negative-fixture-exists half)."""
    root = certify_mod._repo_root()
    for c in REGISTRY:
        if c.enforcement in ("hard", "warn"):
            assert c.certification_fixtures, (
                f"{c.id}: enforced contract must name ≥1 certification fixture"
            )
            existing, missing = certify_mod._check_fixture_files(c, root)
            assert not missing, (
                f"{c.id}: certification fixture file(s) not found: {missing}"
            )


# ── Contract validation rejects malformed rows ──────────────────────────────
def test_uncovered_with_authority_is_rejected():
    with pytest.raises(ValueError):
        Contract(
            id="bad.uncovered_with_authority",
            source_node="A",
            target_node="B",
            edge_type="reference",
            dimensions=("existence",),
            authority="codd.something",  # illegal for uncovered
            fail_mode="honest_fail",
            status="uncovered",
            predicted_issue="x",
            proposed_gate="y",
        )


def test_covered_without_fixture_is_rejected():
    with pytest.raises(ValueError):
        Contract(
            id="bad.covered_no_fixture",
            source_node="A",
            target_node="B",
            edge_type="reference",
            dimensions=("existence",),
            authority="codd.something",
            fail_mode="honest_fail",
            status="covered",
            certification_fixtures=(),  # illegal for covered
        )


def test_unknown_dimension_is_rejected():
    with pytest.raises(ValueError):
        Contract(
            id="bad.unknown_dim",
            source_node="A",
            target_node="B",
            edge_type="reference",
            dimensions=("not_a_real_dimension",),
            authority="codd.x",
            fail_mode="honest_fail",
            status="covered",
            certification_fixtures=("t",),
        )


# ── certify CLI: default exit 0 + prints summary ────────────────────────────
def test_certify_main_default_exit_zero(capsys):
    code = certify_mod.main([])
    out = capsys.readouterr().out
    assert code == certify_mod.EXIT_OK == 0
    assert "ACG Contract Registry" in out
    assert f"total={len(REGISTRY)}" in out
    # the uncovered backlog is surfaced
    assert "proactive backlog" in out


def test_certify_main_strict_passes_when_no_uncertified():
    # The real registry currently has zero uncertified contracts, so --strict
    # must still exit 0 (uncovered backlog does NOT trip strict).
    assert coverage_summary()["uncertified"] == 0
    code = certify_mod.main(["--strict"])
    assert code == certify_mod.EXIT_OK == 0


def test_certify_strict_exit_logic_with_synthetic_uncertified(monkeypatch):
    # Construct a registry state with one 'uncertified' contract and assert
    # --strict exits non-zero, while default exits 0 (the core strict gate).
    synthetic = (
        replace(REGISTRY[0], id="synthetic.uncertified", status="uncertified"),
    ) + REGISTRY
    monkeypatch.setattr(certify_mod, "REGISTRY", synthetic)
    monkeypatch.setattr(
        certify_mod, "coverage_summary", _summary_for(synthetic)
    )
    monkeypatch.setattr(
        certify_mod, "contracts_by_status", _by_status_for(synthetic)
    )
    # default: report-only, exit 0 even WITH an uncertified contract.
    assert certify_mod.main([]) == certify_mod.EXIT_OK
    # strict: exits non-zero because an uncertified contract exists.
    assert certify_mod.main(["--strict"]) == certify_mod.EXIT_STRICT_UNCERTIFIED


def test_certify_strict_does_not_fail_on_uncovered_only(monkeypatch):
    # An uncovered-only registry (no uncertified) must pass --strict: uncovered
    # is known backlog, not a CI failure.
    uncovered_only = tuple(
        c for c in REGISTRY if c.status == "uncovered"
    )
    assert uncovered_only
    monkeypatch.setattr(certify_mod, "REGISTRY", uncovered_only)
    monkeypatch.setattr(
        certify_mod, "coverage_summary", _summary_for(uncovered_only)
    )
    monkeypatch.setattr(
        certify_mod, "contracts_by_status", _by_status_for(uncovered_only)
    )
    assert certify_mod.main(["--strict"]) == certify_mod.EXIT_OK


def test_certify_check_fixtures_finds_real_test_files():
    # Every covered contract's named test FILE must exist on disk (this is what
    # makes them 'covered' and not 'uncertified'). --check-fixtures must report
    # zero missing files for the real registry.
    root = certify_mod._repo_root()
    for c in REGISTRY:
        if c.status == "covered":
            _existing, missing = certify_mod._check_fixture_files(c, root)
            assert not missing, (
                f"{c.id}: certification fixture file(s) not found: {missing}"
            )


# ── certify CLI runs as a subprocess (the documented entrypoint) ────────────
def test_certify_runs_as_module_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "codd.contracts_registry.certify"],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ACG Contract Registry" in proc.stdout


def test_certify_package_main_subprocess():
    proc = subprocess.run(
        [sys.executable, "-m", "codd.contracts_registry"],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ACG Contract Registry" in proc.stdout


# ── matrix file: parses, ids match registry (no drift) ──────────────────────
def test_contract_matrix_file_exists_and_parses():
    assert MATRIX_PATH.is_file(), f"missing {MATRIX_PATH}"
    data = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "families" in data and isinstance(data["families"], dict)


def test_matrix_ids_match_registry_no_drift():
    data = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    matrix_ids: set[str] = set()
    for entries in data["families"].values():
        for entry in entries:
            matrix_ids.add(entry["id"])
    registry_ids = {c.id for c in REGISTRY}
    assert matrix_ids == registry_ids, (
        "registry/matrix DRIFT — regenerate with "
        "`python -m codd.contracts_registry.generate_matrix`. "
        f"only-registry={sorted(registry_ids - matrix_ids)} "
        f"only-matrix={sorted(matrix_ids - registry_ids)}"
    )


def test_matrix_coverage_summary_matches_registry():
    data = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    assert data["coverage_summary"] == coverage_summary()


def test_matrix_committed_file_is_not_stale():
    # The committed file must equal what the generator would produce right now
    # (the strongest no-drift guarantee: structure AND content, not just ids).
    expected = gen_mod.render_yaml()
    actual = MATRIX_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "contract_matrix.yaml is stale — run "
        "`python -m codd.contracts_registry.generate_matrix`."
    )


def test_matrix_entries_carry_status_appropriate_fields():
    data = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))
    for entries in data["families"].values():
        for entry in entries:
            assert entry["status"] in VALID_STATUSES
            if entry["status"] == "uncovered":
                assert entry["authority"] is None
                assert entry["predicted_issue"]
                assert entry["proposed_gate"]
            else:
                assert entry["authority"]
                assert entry["certification_fixtures"]


# ── certify --matrix drift detection ────────────────────────────────────────
def test_certify_matrix_flag_reports_in_sync(capsys):
    code = certify_mod.main(["--matrix", str(MATRIX_PATH)])
    out = capsys.readouterr().out
    assert code == certify_mod.EXIT_OK
    assert "in sync with registry" in out


def test_certify_matrix_flag_detects_drift(tmp_path, capsys):
    # A matrix missing a contract id must be flagged as drift (exit non-zero).
    drifted = tmp_path / "drift.yaml"
    drifted.write_text(
        yaml.safe_dump({"families": {"X": [{"id": "only.in.matrix"}]}}),
        encoding="utf-8",
    )
    code = certify_mod.main(["--matrix", str(drifted)])
    out = capsys.readouterr().out
    assert code == certify_mod.EXIT_MATRIX_DRIFT
    assert "DRIFT" in out


def test_certify_matrix_missing_file_errors(tmp_path):
    code = certify_mod.main(["--matrix", str(tmp_path / "nope.yaml")])
    assert code == certify_mod.EXIT_MATRIX_DRIFT


def test_generate_matrix_check_mode_passes():
    # The generator's own --check must agree the committed file is current.
    assert gen_mod.main(["--check"]) == 0


# ── helpers ─────────────────────────────────────────────────────────────────
def _summary_for(contracts):
    def _fn():
        out = {s: 0 for s in VALID_STATUSES}
        for c in contracts:
            out[c.status] += 1
        out["total"] = len(contracts)
        return out

    return _fn


def _by_status_for(contracts):
    def _fn():
        out = {s: [] for s in VALID_STATUSES}
        for c in contracts:
            out[c.status].append(c)
        return out

    return _fn


def _subprocess_env():
    import os

    env = dict(os.environ)
    # Ensure the worktree is importable in the child (anti-editable-hijack: we
    # point PYTHONPATH at the worktree, never install).
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    )
    return env

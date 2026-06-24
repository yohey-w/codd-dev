"""Red-before-green tests for cycle-1 review findings (Sonnet + Opus confirmed).

- Finding #3 (false-green): cardinality `_signal_asserted` substring-matched the
  whole sentinel entry, so a member_signal that is a substring of the sentinel
  prefix ("source", "blob") always registered as asserted.
- Finding #5 (read-only path traversal): stale_evidence `_resolve_source` returned
  any absolute path without a project-root jail.
"""

from codd.dag.checks.cardinality_coverage import (
    _SOURCE_TEXT_BLOB_PREFIX,
    _signal_asserted,
)
from codd.dag.checks.stale_evidence import _resolve_source


def test_signal_asserted_ignores_sentinel_prefix_collision():
    # The source blob is present, but the signal "source" appears ONLY inside the
    # sentinel prefix, never in the actual test content → must NOT count as asserted.
    blob = _SOURCE_TEXT_BLOB_PREFIX + "\nassert order_total == expected\n"
    assert _signal_asserted("source", {blob}) is False
    assert _signal_asserted("blob", {blob}) is False
    # A signal genuinely present in the content still registers.
    assert _signal_asserted("order_total", {blob}) is True
    # Exact-membership (explicit attr) path is unaffected.
    assert _signal_asserted("explicit_sig", {"explicit_sig"}) is True


def test_resolve_source_jails_absolute_path_outside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    inside = root / "in.txt"
    inside.write_text("y")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    # Absolute path outside the project root → refused (None), never hashed.
    assert _resolve_source(root, str(outside)) is None
    # Absolute path inside the root → resolved and returned.
    assert _resolve_source(root, str(inside)) == inside.resolve()
    # Relative path → joined under root.
    assert _resolve_source(root, "in.txt") == inside.resolve()


def test_dag_result_has_findings_counts_warnings():
    # Finding #3 (visibility): an amber check that reports via `warnings` (not
    # `violations`) must register as having findings, else it renders as PASS and
    # the verify summary undercounts it.
    from dataclasses import dataclass, field as dfield

    from codd.cli import _dag_result_has_findings

    @dataclass
    class _R:
        warnings: list = dfield(default_factory=list)
        violations: list = dfield(default_factory=list)

    assert _dag_result_has_findings(_R(warnings=[{"type": "dead_resource"}])) is True
    assert _dag_result_has_findings(_R()) is False


def test_dag_result_has_findings_robust_to_field_name_and_status():
    # Round-3 P1: a check (e.g. ci_health) that reports under `findings` and/or
    # declares status="warn" must register as having findings — robust to field name
    # via the check's own declared status — so it renders WARN, not a clean PASS.
    from dataclasses import dataclass, field as dfield

    from codd.cli import _dag_result_has_findings

    @dataclass
    class _R:
        status: str = "pass"
        violations: list = dfield(default_factory=list)
        warnings: list = dfield(default_factory=list)
        findings: list = dfield(default_factory=list)

    assert _dag_result_has_findings(
        _R(status="warn", findings=[{"type": "ci_trigger_incomplete"}])
    ) is True
    assert _dag_result_has_findings(_R(status="warn")) is True
    assert _dag_result_has_findings(_R(status="pass")) is False


def _warn_amber_result():
    """An amber result that reports a finding via both ``status`` and the
    ``warnings``/``findings`` fields — the case round-3 fixed in the CLI but that
    the coverage/deploy copies missed (status-blind, no warnings/findings keys)."""
    from dataclasses import dataclass, field as dfield

    @dataclass
    class _R:
        check_name: str = "ci_health"
        severity: str = "amber"
        status: str = "warn"
        passed: bool = True
        warnings: list = dfield(default_factory=lambda: [{"type": "ci_trigger_incomplete"}])
        findings: list = dfield(default_factory=lambda: [{"type": "ci_trigger_incomplete"}])

    return _R()


def _clean_pass_result():
    """A true clean pass — no non-pass status, no findings. Must stay clean."""
    from dataclasses import dataclass, field as dfield

    @dataclass
    class _R:
        check_name: str = "node_completeness"
        severity: str = "amber"
        status: str = "pass"
        passed: bool = True
        warnings: list = dfield(default_factory=list)
        findings: list = dfield(default_factory=list)
        violations: list = dfield(default_factory=list)

    return _R()


def test_has_findings_consistent_across_cli_coverage_deployer():
    # Round-14 #2: coverage_metrics and deployer carried their own status-blind
    # copies of _dag_result_has_findings that ignored `warnings`/`findings`, so an
    # amber+status=warn result was counted as a CLEAN pass there while the CLI
    # counted it as WARN. All three must now agree (byte-once).
    from codd.cli import _dag_result_has_findings as cli_fn
    from codd.coverage_metrics import _dag_result_has_findings as cov_fn
    from codd.deployer import _dag_result_has_findings as dep_fn

    warn = _warn_amber_result()
    clean = _clean_pass_result()

    # RED before fix: cov_fn(warn) and dep_fn(warn) returned False (clean).
    assert cli_fn(warn) is True
    assert cov_fn(warn) is True
    assert dep_fn(warn) is True

    # Regression: a true clean pass stays clean in all three.
    assert cli_fn(clean) is False
    assert cov_fn(clean) is False
    assert dep_fn(clean) is False


def test_coverage_dag_completeness_counts_warn_amber(monkeypatch):
    # coverage summary must surface an amber+status=warn result as a warning,
    # not silently drop it as a clean pass.
    from codd import coverage_metrics
    from codd.dag import runner

    monkeypatch.setattr(
        runner,
        "run_all_checks",
        lambda project_root, settings=None, **kwargs: [_warn_amber_result()],
    )

    result = coverage_metrics.compute_dag_completeness("/tmp/nonexistent")

    # The warn-bearing amber result is rendered as a warning line (RED before fix:
    # has_findings()==False there → amber_findings empty → no warning line).
    assert any(line.startswith("warning:") for line in result.details)


def test_deployer_dag_gate_counts_warn_amber(monkeypatch):
    # deploy gate must add a warning for an amber+status=warn result (RED before
    # fix: its status-blind copy treated it as clean → no warning).
    from codd import deployer
    from codd.dag import runner

    monkeypatch.setattr(
        runner,
        "run_all_checks",
        lambda project_root, settings=None, check_names=None, **kwargs: [_warn_amber_result()],
    )

    gate = deployer.DeployGateResult()
    deployer._collect_dag_completeness_gate("/tmp/nonexistent", {}, gate)

    assert gate.warnings
    assert not gate.failures  # an amber warning never blocks deploy

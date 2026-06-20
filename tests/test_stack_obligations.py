"""F5: framework/addon obligation ENFORCEMENT + conformance (v3.0).

Proves the obligation checkers are real enforcement (not declarative theater):
the Next.js ignoreBuildErrors guard catches the unsafe setting (and does NOT
false-flag a comment or `: false`), and the Playwright checker reds a fully-
skipped e2e run. The extensibility-safety contract: EVERY error-severity
obligation across the curated profiles must resolve to a registered checker — an
unenforced release-blocker fails CI (so a user-added framework with an error
obligation must ship its checker too).
"""
from __future__ import annotations

from codd.stack.adapters import resolve_checker
from codd.stack.adapters.nextjs import check_ignore_build_errors
from codd.stack.adapters.playwright import check_executed
from codd.stack.registry import default_addon_registry, default_framework_registry


def _write_next_config(tmp_path, body: str):
    (tmp_path / "next.config.js").write_text(body, encoding="utf-8")
    return tmp_path


def test_ignore_build_errors_catches_active_setting(tmp_path):
    _write_next_config(
        tmp_path,
        "const nextConfig = {\n  typescript: {\n    ignoreBuildErrors: true,\n  },\n};\n"
        "module.exports = nextConfig;\n",
    )
    findings = check_ignore_build_errors(tmp_path)
    assert findings
    assert findings[0].obligation_id == "no_ignore_build_errors_as_typecheck"
    assert "ignoreBuildErrors" in findings[0].detail


def test_ignore_build_errors_does_not_false_flag_a_comment(tmp_path):
    # anti-false-RED: a commented-out setting must NOT be flagged.
    _write_next_config(
        tmp_path,
        "const nextConfig = {\n  typescript: {\n    // ignoreBuildErrors: true\n  },\n};\n",
    )
    assert check_ignore_build_errors(tmp_path) == []


def test_ignore_build_errors_clean_for_false_and_absent(tmp_path):
    _write_next_config(tmp_path, "module.exports = { typescript: { ignoreBuildErrors: false } };\n")
    assert check_ignore_build_errors(tmp_path) == []
    # absent config dir
    assert check_ignore_build_errors(tmp_path / "nope") == []


def test_ignore_build_errors_catches_eslint_variant(tmp_path):
    _write_next_config(tmp_path, "module.exports = { eslint: { ignoreDuringBuilds: true } };\n")
    findings = check_ignore_build_errors(tmp_path)
    assert findings and "eslint" in findings[0].detail


def test_playwright_flags_zero_executed_run():
    # all-skipped run: expected/unexpected/flaky all 0 -> not green.
    findings = check_executed(report_data={"stats": {"expected": 0, "unexpected": 0, "skipped": 5}})
    assert findings and findings[0].obligation_id == "e2e_actually_executed"


def test_playwright_passes_real_run():
    assert check_executed(report_data={"stats": {"expected": 3, "unexpected": 0, "skipped": 1}}) == []


def test_playwright_missing_report_is_violation(tmp_path):
    assert check_executed(report_path=tmp_path / "no-such.json")


def test_every_error_obligation_has_a_registered_checker():
    """Extensibility-safety contract: an error-severity obligation that resolves
    to no checker is an unenforced release-blocker (false claim of enforcement)."""
    profiles = list(default_framework_registry.all_profiles()) + list(
        default_addon_registry.all_profiles()
    )
    assert profiles  # registries are non-empty
    unenforced = []
    for prof in profiles:
        for obl in prof.obligations:
            if obl.severity == "error" and resolve_checker(obl.checker) is None:
                unenforced.append((prof.id, obl.id, obl.checker))
    assert not unenforced, f"error-severity obligations with no registered checker: {unenforced}"


def test_resolve_checker_unknown_is_none():
    assert resolve_checker("nope_adapter:missing") is None
    assert resolve_checker(None) is None


# --- enforce_obligations: the gate seam --------------------------------------

from codd.languages.registry import default_registry as _LANG  # noqa: E402
from codd.stack.compose import compose  # noqa: E402
from codd.stack.obligations import enforce_obligations  # noqa: E402
from codd.stack.profile import FrameworkProfile, LayerIdentity, Obligation  # noqa: E402


def _curated_contract():
    ts = _LANG.resolve("typescript")
    return compose(
        ts,
        [default_framework_registry.resolve("nextjs")],
        [default_addon_registry.resolve("prisma"), default_addon_registry.resolve("playwright")],
    )


def test_enforce_obligations_passes_clean_project(tmp_path):
    (tmp_path / "next.config.js").write_text(
        "module.exports = { typescript: { ignoreBuildErrors: false } };\n"
    )
    result = enforce_obligations(
        _curated_contract(), project_root=tmp_path, report_data={"stats": {"expected": 3, "unexpected": 0}}
    )
    assert not result.blocking_violations
    # warn-level unenforced (route_coverage / schema_sync) must NOT block.
    assert result.passed


def test_enforce_obligations_blocks_on_ignore_build_errors(tmp_path):
    (tmp_path / "next.config.js").write_text(
        "module.exports = { typescript: { ignoreBuildErrors: true } };\n"
    )
    result = enforce_obligations(
        _curated_contract(), project_root=tmp_path, report_data={"stats": {"expected": 3}}
    )
    assert result.blocking_violations
    assert not result.passed
    assert any(
        v.obligation.id == "no_ignore_build_errors_as_typecheck" for v in result.blocking_violations
    )


def test_enforce_obligations_blocks_on_skipped_e2e(tmp_path):
    (tmp_path / "next.config.js").write_text("module.exports = {};\n")
    result = enforce_obligations(
        _curated_contract(), project_root=tmp_path, report_data={"stats": {"expected": 0, "skipped": 4}}
    )
    assert not result.passed
    assert any(v.obligation.id == "e2e_actually_executed" for v in result.blocking_violations)


def test_unenforced_error_obligation_fails_honestly():
    ts = _LANG.resolve("typescript")
    fake = FrameworkProfile(
        identity=LayerIdentity(id="fakefw", kind="framework"),
        obligations=(Obligation(id="unenforced_blocker", severity="error", checker="missing_adapter:nope"),),
    )
    result = enforce_obligations(compose(ts, [fake]), project_root=None)
    assert any(o.id == "unenforced_blocker" for o in result.unenforced)
    # an unenforced ERROR obligation is a failure, never a silent pass (anti-false-green).
    assert not result.passed

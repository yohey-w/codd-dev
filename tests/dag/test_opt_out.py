from __future__ import annotations

from datetime import date

import pytest

from codd.dag.checks.opt_out import (
    OPT_OUT_STATUS,
    OptOutDeclaration,
    OptOutPolicy,
)


REGISTERED = {"ci_health", "user_journey_coherence", "deployment_completeness"}


def _config(**entries):
    return {"opt_outs": list(entries.get("opt_outs", []))}


def _entry(check="ci_health", reason="vendor migration in progress", expires_at="2027-01-01", **extra):
    base = {"check": check, "reason": reason, "expires_at": expires_at}
    base.update(extra)
    return base


class TestOptOutPolicyParsing:
    def test_empty_config_returns_empty_policy(self) -> None:
        policy = OptOutPolicy.from_config({})
        assert policy.declarations == []
        assert policy.parse_errors == []

    def test_missing_opt_outs_section(self) -> None:
        policy = OptOutPolicy.from_config({"version": "2.13.0"})
        assert policy.declarations == []
        assert policy.parse_errors == []

    def test_parses_valid_entry(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": [_entry()]})
        assert len(policy.declarations) == 1
        decl = policy.declarations[0]
        assert decl.check == "ci_health"
        assert decl.reason == "vendor migration in progress"
        assert decl.expires_at == date(2027, 1, 1)

    def test_invalid_section_type_records_parse_error(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": "not-a-list"})
        assert policy.declarations == []
        assert any(err.code == "invalid_opt_outs_section" for err in policy.parse_errors)

    def test_invalid_entry_type_records_parse_error(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": ["not-a-mapping"]})
        assert policy.declarations == []
        assert any(err.code == "invalid_opt_out_entry" for err in policy.parse_errors)

    def test_missing_reason_is_parse_error(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": [_entry(reason="")]})
        codes = {err.code for err in policy.parse_errors}
        assert "missing_reason" in codes
        assert policy.declarations == []

    def test_missing_expires_at_is_parse_error(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [{"check": "ci_health", "reason": "valid"}]}
        )
        codes = {err.code for err in policy.parse_errors}
        assert "missing_expires_at" in codes
        assert policy.declarations == []

    def test_invalid_expires_at_format_is_parse_error(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [_entry(expires_at="next-friday")]}
        )
        codes = {err.code for err in policy.parse_errors}
        assert "missing_expires_at" in codes  # invalid date is treated as missing
        assert policy.declarations == []

    def test_missing_check_is_parse_error(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [{"reason": "x", "expires_at": "2027-01-01"}]}
        )
        codes = {err.code for err in policy.parse_errors}
        assert "missing_check" in codes


class TestOptOutPolicyValidation:
    def test_valid_active_declaration_has_no_errors(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": [_entry()]})
        errors = policy.validate(date(2026, 5, 10), REGISTERED)
        assert errors == []

    def test_unknown_check_name_fails_validate(self) -> None:
        policy = OptOutPolicy.from_config({"opt_outs": [_entry(check="not_a_real_check")]})
        errors = policy.validate(date(2026, 5, 10), REGISTERED)
        assert any(err.code == "unknown_check" for err in errors)

    def test_expired_declaration_fails_validate(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [_entry(expires_at="2026-01-01")]}
        )
        errors = policy.validate(date(2026, 5, 10), REGISTERED)
        assert any(err.code == "expired" for err in errors)

    def test_today_equal_expires_at_is_expired(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [_entry(expires_at="2026-05-10")]}
        )
        errors = policy.validate(date(2026, 5, 10), REGISTERED)
        assert any(err.code == "expired" for err in errors)

    def test_duplicate_entries_for_same_check_fails_validate(self) -> None:
        policy = OptOutPolicy.from_config(
            {
                "opt_outs": [
                    _entry(),
                    _entry(reason="another", expires_at="2027-06-01"),
                ]
            }
        )
        errors = policy.validate(date(2026, 5, 10), REGISTERED)
        assert any(err.code == "duplicate" for err in errors)


class TestOptOutPolicyLookup:
    def test_lookup_returns_declaration_regardless_of_expiry(self) -> None:
        policy = OptOutPolicy.from_config(
            {"opt_outs": [_entry(expires_at="2026-01-01")]}
        )
        decl = policy.lookup("ci_health")
        assert decl is not None
        assert decl.is_expired(date(2026, 5, 10))

    def test_lookup_returns_none_for_undeclared_check(self) -> None:
        policy = OptOutPolicy.from_config({})
        assert policy.lookup("ci_health") is None

    def test_active_and_expired_partition(self) -> None:
        policy = OptOutPolicy.from_config(
            {
                "opt_outs": [
                    _entry(check="ci_health", expires_at="2027-01-01"),
                    _entry(
                        check="user_journey_coherence",
                        expires_at="2026-01-01",
                        reason="planned",
                    ),
                ]
            }
        )
        today = date(2026, 5, 10)
        active = policy.active(today)
        expired = policy.expired(today)
        assert {d.check for d in active} == {"ci_health"}
        assert {d.check for d in expired} == {"user_journey_coherence"}


class TestOptOutDeclaration:
    def test_is_expired_strict_inequality(self) -> None:
        decl = OptOutDeclaration(
            check="ci_health",
            reason="x",
            expires_at=date(2026, 6, 1),
        )
        assert decl.is_expired(date(2026, 6, 1)) is True
        assert decl.is_expired(date(2026, 5, 31)) is False
        assert decl.is_expired(date(2026, 6, 2)) is True


def test_opt_out_status_constant_value() -> None:
    assert OPT_OUT_STATUS == "opt_out"


class TestOptOutPolicyEndToEndViaRunner:
    """Integration: settings flow through ``run_all_checks`` and reach the check."""

    def _scaffold_minimal_project(self, project_root) -> None:
        # Empty project — only ci_health needs to be addressed for an
        # otherwise-empty project to pass the C8 gate.
        project_root.mkdir(parents=True, exist_ok=True)

    def test_provider_none_with_active_declaration_yields_opt_out_result(self, tmp_path) -> None:
        from codd.dag.runner import run_all_checks

        self._scaffold_minimal_project(tmp_path)

        config = {
            "ci": {"provider": "none"},
            "opt_outs": [
                {
                    "check": "ci_health",
                    "reason": "vendor migration in progress",
                    "expires_at": "2099-12-31",
                }
            ],
        }

        results = run_all_checks(
            tmp_path,
            settings=config,
            today=date(2026, 5, 10),
            check_names=["ci_health"],
        )

        assert len(results) == 1
        result = results[0]
        assert result.status == OPT_OUT_STATUS
        assert result.severity == "red"  # severity preserved
        assert result.block_deploy is False

    def test_provider_none_without_declaration_yields_red_fail(self, tmp_path) -> None:
        from codd.dag.runner import run_all_checks

        self._scaffold_minimal_project(tmp_path)

        results = run_all_checks(
            tmp_path,
            settings={"ci": {"provider": "none"}},
            today=date(2026, 5, 10),
            check_names=["ci_health"],
        )

        assert len(results) == 1
        result = results[0]
        assert result.status == "fail"
        assert result.severity == "red"
        assert result.block_deploy is True

    def test_provider_none_with_expired_declaration_yields_red_fail(self, tmp_path) -> None:
        from codd.dag.runner import run_all_checks

        self._scaffold_minimal_project(tmp_path)

        config = {
            "ci": {"provider": "none"},
            "opt_outs": [
                {
                    "check": "ci_health",
                    "reason": "kept too long",
                    "expires_at": "2026-01-01",
                }
            ],
        }

        results = run_all_checks(
            tmp_path,
            settings=config,
            today=date(2026, 5, 10),
            check_names=["ci_health"],
        )

        assert len(results) == 1
        result = results[0]
        assert result.status == "fail"
        assert "expired" in result.message

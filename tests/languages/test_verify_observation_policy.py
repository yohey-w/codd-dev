"""v2.72 (Contract Kernel Step 3) — unweakenable verify observation policy.

The anti-false-green invariant for a verify command lives in
VerifyObservationPolicy: a verify run is green ONLY if it observed real tests,
produced a parseable report, and had no failed/skipped tests. A profile may
STRENGTHEN (raise min_collected_tests) but NEVER weaken — every weakening
declaration is rejected at LOAD time, so a profile cannot turn a not-green
outcome green. (Defined here; enforced by the verify executor in Step 4 / the
switch in Step 6.)
"""

from __future__ import annotations

import pytest

from codd.languages import (
    LanguageProfileError,
    VerifyObservationPolicy,
    build_language_contract,
    load_language_profile,
)
from codd.languages.registry import LanguageRegistry
from codd.languages.verify_plan import build_verify_plan


# ── defaults are the strict invariant ──


def test_defaults_are_strict():
    p = VerifyObservationPolicy()
    assert p.min_collected_tests == 1
    assert p.zero_tests == "red"
    assert p.report_missing == "red"
    assert p.report_parse_error == "red"
    assert p.failed_tests == "red"
    assert p.skipped_tests == "red"


def test_from_mapping_none_is_defaults():
    assert VerifyObservationPolicy.from_mapping(None) == VerifyObservationPolicy()


def test_empty_mapping_is_defaults():
    assert VerifyObservationPolicy.from_mapping({}) == VerifyObservationPolicy()


# ── strengthening is allowed ──


def test_min_collected_can_be_raised():
    p = VerifyObservationPolicy.from_mapping({"min_collected_tests": 5})
    assert p.min_collected_tests == 5


def test_explicit_red_is_accepted():
    p = VerifyObservationPolicy.from_mapping(
        {"zero_tests": "red", "report_missing": "red", "min_collected_tests": 1}
    )
    assert p == VerifyObservationPolicy()


# ── weakening is rejected (anti-false-green) ──


@pytest.mark.parametrize(
    "weakening",
    [
        {"allow_zero_tests": True},  # unknown weakening flag
        {"zero_tests": "warn"},
        {"zero_tests": "pass"},
        {"report_missing": "pass"},
        {"report_parse_error": "warn"},
        {"failed_tests": "warn"},
        {"skipped_tests": "ignore"},
        {"min_collected_tests": 0},
        {"min_collected_tests": -3},
        {"some_typo_key": "red"},  # unknown key
    ],
)
def test_weakening_is_rejected(weakening):
    with pytest.raises(ValueError):
        VerifyObservationPolicy.from_mapping(weakening)


def test_min_collected_non_integer_rejected():
    with pytest.raises(ValueError):
        VerifyObservationPolicy.from_mapping({"min_collected_tests": "lots"})


# ── loader rejects a weakening observation at LOAD time ──


def _write_profile(tmp_path, observation_block):
    """Write a minimal profile whose commands.verify carries an observation block."""
    import yaml

    profile = {
        "id": "toyobs",
        "commands": {"verify": {"argv": ["toy", "test"], "observation": observation_block}},
    }
    path = tmp_path / "toyobs.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return path


def test_loader_rejects_weakening_observation(tmp_path):
    path = _write_profile(tmp_path, {"allow_zero_tests": True, "zero_tests": "warn"})
    with pytest.raises(LanguageProfileError):
        load_language_profile(path)


def test_loader_rejects_zero_min_collected(tmp_path):
    path = _write_profile(tmp_path, {"min_collected_tests": 0})
    with pytest.raises(LanguageProfileError):
        load_language_profile(path)


def test_loader_accepts_stricter_observation(tmp_path):
    path = _write_profile(tmp_path, {"min_collected_tests": 3, "zero_tests": "red"})
    profile = load_language_profile(path)
    assert profile.commands["verify"].observation.min_collected_tests == 3


# ── plan carries the policy; bundled profiles get strict defaults ──


def test_bundled_go_plan_has_strict_default_policy():
    from codd.languages import default_registry

    plan = build_verify_plan(build_language_contract(default_registry.resolve("go")))
    assert plan.observation == VerifyObservationPolicy()  # go.yaml declares none → strict default


def test_stricter_profile_policy_flows_to_plan(tmp_path):
    path = _write_profile(tmp_path, {"min_collected_tests": 2})
    reg = LanguageRegistry(profiles_dir=tmp_path)
    plan = build_verify_plan(build_language_contract(reg.resolve("toyobs")))
    assert plan.observation.min_collected_tests == 2

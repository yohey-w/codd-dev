"""Cut Condition C — the Anti-False-Green Conformance Gate (the META-gate).

This is the comprehensive CONFORMANCE proof that the anti-false-green invariant is
CORE-OWNED across BOTH the language layer and the stack (framework/addon) layer: a
profile may supply the invariant's PARAMETERS, but it can NEVER weaken or disable the
invariant itself. Every weakening form must be IMPOSSIBLE (rejected at load) or RED.

It is the meta-gate that protects every other gate: if a profile could quietly turn a
not-green outcome green, every downstream gate (marker authenticity, command
authenticity, obligation enforcement, verify observation) would inherit that hole.

============================================================================
CORE-OWNED vs PROFILE-TUNABLE TAXONOMY (the line Cut C enforces)
============================================================================

A profile may make a gate STRICTER or set a numeric threshold (a PARAMETER); it may
NEVER make a gate accept what the invariant forbids.

LANGUAGE LAYER — per-command ``observation:`` (``VerifyObservationPolicy``)
  PROFILE-TUNABLE (a profile may set / RAISE, never lower):
    * ``min_collected_tests`` — raise-only; must stay ``>= 1`` (a verify that observed
      zero tests is never green).
  CORE-OWNED (never overridable by a profile — rejected at LOAD by ``from_mapping``):
    * ``zero_tests`` / ``report_missing`` / ``report_parse_error`` / ``failed_tests`` /
      ``skipped_tests`` — the ``RED_ONLY`` fields; only ``"red"`` is permitted.
    * the schema is CLOSED — an unknown key (``allow_zero_tests``, ``skipped_is_green``,
      …) is rejected, so the policy cannot be extended with a weakening flag.

STACK LAYER — per-slot-kind command authenticity (``StackCommandObservationPolicy``)
  PROFILE-TUNABLE (a contract may declare for a NEW slot id, or STRENGTHEN a default):
    * ``min_collected_tests`` — raise-only (TEST kind).
    * assigning a kind to a NEW/custom slot id the default map does not cover — but only
      to a kind whose intrinsic floor the policy satisfies.
  CORE-OWNED (a profile may never lower / disable — enforced in the resolver/factory):
    * ``reject_static_noop`` must stay ``True`` (a command that cannot fail is not a
      check) for EVERY kind.
    * for a TEST_REPORT policy: ``report_required=True``, ``min_collected_tests >= 1``,
      ``require_test_level_available=True``, ``fail_on_observed_failures=True`` — the
      anti-false-green floor of a test command.
    * a known slot's KIND may not be DOWNGRADED to a weaker kind (e.g. ``e2e_test`` →
      STATIC_EXECUTION would drop the test-count requirement).
    * an unknown slot with NO policy is RED (``AUTHENTICITY_POLICY_MISSING``) — the
      harness never defaults an unknown slot to a permissive kind.
    * the observation is DERIVED from current-run report evidence, never trusted from
      the executor result.

STACK LAYER — obligations (``compose`` + ``enforce_obligations``)
  PROFILE-TUNABLE:
    * a profile chooses the severity (``error``/``warn``) of ITS OWN obligation — that
      is the profile's own contract, not a weakening of a CORE invariant.
  CORE-OWNED:
    * a cross-layer redefinition that DOWNGRADES (or even just changes) another layer's
      obligation severity / checker ref is a semantic Conflict → RED (no silent override).
    * an ERROR obligation whose checker is missing / null / unregistered / not callable
      is ``unenforced`` → RED (a claimed release-blocker that does not run is never green).
    * an ERROR obligation whose checker raises / returns ``None`` / returns a non-list is
      a ``fault`` → RED (a broken checker is never "satisfied").
    * a TEST-kind command with no report adapter is an incomplete contract → RED.

The line for Q4 (profile-own warn vs forbidden downgrade): a profile picking ``warn``
for an obligation IT introduces is legitimate (it is asserting a *new, advisory* check —
removing it entirely would be strictly weaker, so an advisory version cannot be a
"weakening" of something the core required). What is forbidden is making a check that
ANOTHER layer declared ERROR weaker, or claiming an ERROR check while shipping no real
enforcement — both are RED above.
"""
from __future__ import annotations

import dataclasses

import pytest

# ── language layer ──────────────────────────────────────────────────────────
from codd.languages.loader import LanguageProfileError, _parse_command
from codd.languages.profile import VerifyObservationPolicy
from codd.languages.registry import default_registry as default_language_registry

# ── stack layer ─────────────────────────────────────────────────────────────
from codd.stack.command_authenticity import (
    BUILD_EXECUTION_POLICY,
    DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES,
    STATIC_EXECUTION_POLICY,
    TEST_REPORT_POLICY,
    StackCommandObservationKind,
    StackCommandObservationPolicy,
    StackObservationPolicyWeakeningError,
    is_at_least_as_strict,
    resolve_stack_command_observation_policy,
)
from codd.stack.compose import compose
from codd.stack.obligations import enforce_obligations
from codd.stack.profile import (
    AddonProfile,
    FrameworkProfile,
    LayerIdentity,
    Obligation,
)
from codd.stack.registry import default_addon_registry, default_framework_registry


# ============================================================================
# LANGUAGE LAYER — VerifyObservationPolicy is unweakenable at LOAD
# ============================================================================

#: Every weakening form a profile might try to declare in an ``observation:`` block.
#: ALL must be rejected by ``from_mapping`` at load — none may silently turn a
#: not-green verify outcome green.
_LANGUAGE_WEAKENING_FORMS = [
    pytest.param({"allow_zero_tests": True}, id="allow_zero_tests"),
    pytest.param({"zero_tests": "warn"}, id="zero_tests=warn"),
    pytest.param({"zero_tests": "pass"}, id="zero_tests=pass"),
    pytest.param({"report_missing": "pass"}, id="report_missing=pass"),
    pytest.param({"report_missing": "warn"}, id="report_missing=warn"),
    pytest.param({"report_parse_error": "pass"}, id="report_parse_error=pass"),
    pytest.param({"failed_tests": "pass"}, id="failed_tests=pass"),
    pytest.param({"failed_tests": "warn"}, id="failed_tests=warn"),
    pytest.param({"skipped_tests": "pass"}, id="skipped_tests=pass"),
    pytest.param({"skipped_tests": "ignore"}, id="skipped_tests=ignore"),
    pytest.param({"min_collected_tests": 0}, id="min_collected_tests=0"),
    pytest.param({"min_collected_tests": -1}, id="min_collected_tests=-1"),
    pytest.param({"skipped_is_green": True}, id="skipped_is_green"),
]


@pytest.mark.parametrize("weakening", _LANGUAGE_WEAKENING_FORMS)
def test_language_observation_policy_rejects_every_weakening_form(weakening):
    """CORE-OWNED: every anti-false-green weakening of an ``observation:`` block is
    rejected by ``VerifyObservationPolicy.from_mapping`` at LOAD (ValueError)."""
    with pytest.raises(ValueError):
        VerifyObservationPolicy.from_mapping(weakening)


@pytest.mark.parametrize("weakening", _LANGUAGE_WEAKENING_FORMS)
def test_language_loader_rejects_weakening_observation_in_a_command(weakening):
    """A language PROFILE (via the real loader path) cannot carry a weakening
    ``observation:`` block — ``_parse_command`` raises ``LanguageProfileError``."""
    raw = {"argv": ["pytest", "-q"], "observation": weakening}
    with pytest.raises(LanguageProfileError):
        _parse_command("verify", raw)


def test_language_observation_policy_allows_stricter_min_collected_tests():
    """PROFILE-TUNABLE: a profile may RAISE ``min_collected_tests`` (stricter). The
    RED_ONLY fields stay red — strengthening one parameter never weakens the floor."""
    pol = VerifyObservationPolicy.from_mapping({"min_collected_tests": 25})
    assert pol.min_collected_tests == 25
    for red_field in VerifyObservationPolicy.RED_ONLY:
        assert getattr(pol, red_field) == "red"


def test_language_red_only_fields_are_red_by_default():
    """The DEFAULT policy IS the invariant: every RED_ONLY field is ``"red"`` and the
    minimum collected tests is >= 1."""
    pol = VerifyObservationPolicy()
    assert pol.min_collected_tests >= 1
    for red_field in VerifyObservationPolicy.RED_ONLY:
        assert getattr(pol, red_field) == "red"


@pytest.mark.parametrize("language", sorted(default_language_registry.ids()))
def test_every_registered_language_profile_has_no_weak_observation(language):
    """CONFORMANCE over EVERY registered language profile: any ``observation:`` block it
    declares (loaded via the real loader) is at the unweakenable floor — RED_ONLY fields
    red, ``min_collected_tests >= 1``. (A profile that tried to weaken would have failed
    to load at all; this asserts the loaded contract is conformant.)"""
    profile = default_language_registry.resolve(language)
    for cmd_id, spec in profile.commands.items():
        pol = spec.observation
        if pol is None:
            continue
        assert pol.min_collected_tests >= 1, (
            f"{language}.{cmd_id}: min_collected_tests < 1 weakens the invariant"
        )
        for red_field in VerifyObservationPolicy.RED_ONLY:
            assert getattr(pol, red_field) == "red", (
                f"{language}.{cmd_id}.{red_field} is not 'red' — weakens anti-false-green"
            )


# ============================================================================
# STACK LAYER — command authenticity policy cannot be weakened by a contract
# ============================================================================


#: The KNOWN test slot ids whose default is the strict TEST_REPORT_POLICY.
_KNOWN_TEST_SLOTS = sorted(
    sid
    for sid, pol in DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES.items()
    if pol.kind is StackCommandObservationKind.TEST_REPORT
)

#: Intrinsic TEST-kind weakenings — each drops part of the anti-false-green floor of a
#: test command. With the Cut C ``__post_init__`` these can no longer even be CONSTRUCTED
#: (defense layer 1: invalid-by-construction, GPT-5.5 Pro consult 2026-06-21).
_INTRINSIC_TEST_WEAKENINGS = [
    pytest.param({"report_required": False}, id="report_required=False"),
    pytest.param({"min_collected_tests": 0}, id="min_collected_tests=0"),
    pytest.param({"fail_on_observed_failures": False}, id="fail_on_observed_failures=False"),
    pytest.param({"require_test_level_available": False}, id="require_test_level_available=False"),
    pytest.param({"reject_static_noop": False}, id="reject_static_noop=False"),
]


@pytest.mark.parametrize("weakening", _INTRINSIC_TEST_WEAKENINGS)
def test_stack_weak_test_policy_cannot_be_constructed(weakening):
    """CORE-OWNED (defense layer 1 — invalid by construction): a TEST_REPORT policy that
    drops any anti-false-green floor field cannot even be BUILT — ``__post_init__`` raises.
    A profile cannot obtain a weak 'test' policy object to hand through the override."""
    with pytest.raises(ValueError):
        dataclasses.replace(TEST_REPORT_POLICY, **weakening)


def test_stack_noop_accepting_policy_cannot_be_constructed_for_any_kind():
    """CORE-OWNED: NO kind may set ``reject_static_noop=False`` — a no-op-accepting policy
    is forbidden by construction for every kind."""
    for base in (TEST_REPORT_POLICY, BUILD_EXECUTION_POLICY, STATIC_EXECUTION_POLICY):
        with pytest.raises(ValueError):
            dataclasses.replace(base, reject_static_noop=False)


@pytest.mark.parametrize("slot_id", _KNOWN_TEST_SLOTS)
@pytest.mark.parametrize(
    "weak_policy",
    [
        # Downgrading the KIND of a test slot to a non-test kind drops the whole test
        # observation requirement — the most direct false-green. These ARE constructible
        # (they are valid static/build policies), so the RESOLVER (defense layer 2) must
        # reject them as a weakening of the known slot's default.
        pytest.param(STATIC_EXECUTION_POLICY, id="kind_downgraded_to_static"),
        pytest.param(BUILD_EXECUTION_POLICY, id="kind_downgraded_to_build"),
    ],
)
def test_stack_contract_override_cannot_downgrade_a_known_test_slot_kind(slot_id, weak_policy):
    """CORE-OWNED (defense layer 2 — resolver strengthen-only): a contract override that
    swaps a known test slot's strict TEST_REPORT policy for a weaker-kind policy must be
    rejected fail-closed. This is the stack twin of the language ``from_mapping`` rejection
    — the resolver owns strengthen-only, it is not delegated to a docstring."""
    with pytest.raises(StackObservationPolicyWeakeningError):
        resolve_stack_command_observation_policy(
            slot_id, contract_policies={slot_id: weak_policy}
        )


@pytest.mark.parametrize("slot_id", _KNOWN_TEST_SLOTS)
def test_stack_contract_override_may_strengthen_a_known_test_slot(slot_id):
    """PROFILE-TUNABLE (false-RED control): a STRICTER override (raise
    ``min_collected_tests``) is honored — strengthening is allowed."""
    stricter = dataclasses.replace(TEST_REPORT_POLICY, min_collected_tests=10)
    resolved = resolve_stack_command_observation_policy(
        slot_id, contract_policies={slot_id: stricter}
    )
    assert resolved is not None
    assert resolved.min_collected_tests >= 10
    # still anti-false-green on every floor.
    assert resolved.report_required is True
    assert resolved.fail_on_observed_failures is True


def test_stack_build_slot_override_may_strengthen_require_build_outputs():
    """PROFILE-TUNABLE (false-RED control): a build slot override turning ON
    ``require_build_outputs`` is a STRENGTHENING and is honored (False -> True)."""
    stricter = dataclasses.replace(BUILD_EXECUTION_POLICY, require_build_outputs=True)
    resolved = resolve_stack_command_observation_policy(
        "framework_build", contract_policies={"framework_build": stricter}
    )
    assert resolved is not None and resolved.require_build_outputs is True


def test_stack_unknown_slot_with_no_policy_is_red():
    """CORE-OWNED: an unknown slot with neither a contract policy nor a default resolves
    to ``None`` — the classifier turns that into AUTHENTICITY_POLICY_MISSING (RED). The
    harness never guesses a permissive kind for an unknown slot."""
    assert resolve_stack_command_observation_policy("totally_unknown_slot") is None


def test_stack_unknown_slot_custom_test_policy_must_meet_the_floor():
    """For a NEW/custom slot id (no default) a contract may declare a policy — but a
    TEST_REPORT policy is intrinsically validated, so a 'test' policy that accepts zero
    tests / no report cannot even be CONSTRUCTED (defense layer 1). A well-formed strict
    TEST policy for a custom slot IS honored (legitimate extension)."""
    # A weak custom TEST policy is unconstructible.
    with pytest.raises(ValueError):
        dataclasses.replace(TEST_REPORT_POLICY, report_required=False, min_collected_tests=0)
    # A strict custom TEST policy is a legitimate extension — honored.
    strong_custom = dataclasses.replace(TEST_REPORT_POLICY, min_collected_tests=2)
    resolved = resolve_stack_command_observation_policy(
        "custom_acceptance_test", contract_policies={"custom_acceptance_test": strong_custom}
    )
    assert resolved is not None
    assert resolved.kind is StackCommandObservationKind.TEST_REPORT
    assert resolved.report_required is True
    assert resolved.min_collected_tests >= 2
    assert resolved.fail_on_observed_failures is True


def test_stack_is_at_least_as_strict_partial_order():
    """The strictness partial order (unit test of the core predicate): same-kind with a
    raised threshold / a gate turned ON is stricter; a different kind is never 'stricter'."""
    base = STATIC_EXECUTION_POLICY
    # build-outputs ON is stricter than OFF (same kind).
    assert is_at_least_as_strict(
        dataclasses.replace(BUILD_EXECUTION_POLICY, require_build_outputs=True),
        BUILD_EXECUTION_POLICY,
    )
    assert not is_at_least_as_strict(
        BUILD_EXECUTION_POLICY,
        dataclasses.replace(BUILD_EXECUTION_POLICY, require_build_outputs=True),
    )
    # a different kind is never a strengthening of a known slot's kind.
    assert not is_at_least_as_strict(base, TEST_REPORT_POLICY)
    assert not is_at_least_as_strict(BUILD_EXECUTION_POLICY, STATIC_EXECUTION_POLICY)
    # higher min_collected_tests is stricter (same kind).
    assert is_at_least_as_strict(
        dataclasses.replace(TEST_REPORT_POLICY, min_collected_tests=5), TEST_REPORT_POLICY
    )
    assert not is_at_least_as_strict(
        TEST_REPORT_POLICY, dataclasses.replace(TEST_REPORT_POLICY, min_collected_tests=5)
    )


def test_stack_builtin_default_policies_are_at_the_floor():
    """The shipped DEFAULT policies ARE the invariant: every default rejects a static
    no-op; every TEST default requires a report, >=1 test, test-level granularity, and
    fails on observed failures."""
    for slot_id, pol in DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES.items():
        assert pol.reject_static_noop is True, f"{slot_id}: default accepts a no-op"
        if pol.kind is StackCommandObservationKind.TEST_REPORT:
            assert pol.report_required is True, f"{slot_id}: test default has no report req"
            assert pol.min_collected_tests >= 1, f"{slot_id}: test default allows zero tests"
            assert pol.require_test_level_available is True, f"{slot_id}: no test-level req"
            assert pol.fail_on_observed_failures is True, f"{slot_id}: ignores failures"


# ============================================================================
# STACK LAYER — obligations cannot be weakened (cross-layer downgrade / unenforced)
# ============================================================================


def _ts():
    return default_language_registry.resolve("typescript")


def test_stack_obligation_severity_downgrade_across_layers_is_red():
    """CORE-OWNED: a later layer that DOWNGRADES another layer's obligation severity is a
    semantic Conflict — the composed contract is not ``strict_ok`` (RED)."""
    fw = FrameworkProfile(
        identity=LayerIdentity(id="fw_strict", kind="framework"),
        obligations=(
            Obligation(id="shared_blocker", severity="error", checker="x:check"),
        ),
    )
    addon = AddonProfile(
        identity=LayerIdentity(id="addon_weak", kind="addon"),
        obligations=(
            # same id, DOWNGRADED to warn — a weakening.
            Obligation(id="shared_blocker", severity="warn", checker="x:check"),
        ),
    )
    contract = compose(_ts(), [fw], [addon])
    assert not contract.strict_ok, "a cross-layer severity downgrade must RED"
    assert any(c.kind == "semantic" for c in contract.conflicts)


def test_stack_obligation_checker_gutting_across_layers_is_red():
    """CORE-OWNED: a same-id, same-severity redefinition that NULLS/changes the checker is
    a semantic Conflict (a 'gutted checker' false-green is forbidden)."""
    fw = FrameworkProfile(
        identity=LayerIdentity(id="fw_real_checker", kind="framework"),
        obligations=(Obligation(id="g", severity="error", checker="real:check"),),
    )
    addon = AddonProfile(
        identity=LayerIdentity(id="addon_null_checker", kind="addon"),
        obligations=(Obligation(id="g", severity="error", checker=None),),
    )
    contract = compose(_ts(), [fw], [addon])
    assert not contract.strict_ok
    assert any(c.kind == "semantic" for c in contract.conflicts)


def test_stack_error_obligation_with_no_checker_is_red():
    """CORE-OWNED: an ERROR obligation whose checker resolves to nothing is ``unenforced``
    → the gate does not pass (a claimed release-blocker that does not run is never green)."""
    fw = FrameworkProfile(
        identity=LayerIdentity(id="fw_unenforced", kind="framework"),
        obligations=(
            Obligation(id="blocker", severity="error", checker="missing_adapter:nope"),
        ),
    )
    result = enforce_obligations(compose(_ts(), [fw]), project_root=None)
    assert any(o.id == "blocker" for o in result.unenforced)
    assert not result.passed


def test_stack_error_obligation_with_faulting_checker_is_red(monkeypatch):
    """CORE-OWNED: an ERROR obligation whose checker EXISTS but returns ``None`` (an
    unimplemented / fall-through checker) is a ``fault`` → RED. The ``checker(...) or []``
    false-green hole stays closed."""
    import codd.stack.obligations as obl_mod

    def _none_checker(**_kwargs):
        return None  # the canonical fall-off-the-end checker.

    monkeypatch.setattr(obl_mod, "resolve_checker", lambda ref: _none_checker)
    fw = FrameworkProfile(
        identity=LayerIdentity(id="fw_faulting", kind="framework"),
        obligations=(Obligation(id="b", severity="error", checker="x:returns_none"),),
    )
    result = enforce_obligations(compose(_ts(), [fw]), project_root=None)
    assert result.blocking_faults, "a None-returning ERROR checker must be a blocking fault"
    assert not result.passed


def test_every_registered_stack_profile_error_obligation_is_enforceable():
    """CONFORMANCE over EVERY registered framework + addon profile: an ERROR-severity
    obligation that resolves to no checker is an unenforced release-blocker (a false claim
    of enforcement). Every ERROR obligation MUST have a registered, callable checker."""
    from codd.stack.adapters import resolve_checker

    profiles = list(default_framework_registry.all_profiles()) + list(
        default_addon_registry.all_profiles()
    )
    assert profiles, "stack registries must be non-empty"
    unenforced = []
    for prof in profiles:
        for obl in prof.obligations:
            if obl.severity == "error":
                checker = resolve_checker(obl.checker)
                if checker is None or not callable(checker):
                    unenforced.append((prof.id, obl.id, obl.checker))
    assert not unenforced, (
        f"ERROR-severity stack obligations with no callable checker (unenforced "
        f"release-blockers — anti-false-green violation): {unenforced}"
    )

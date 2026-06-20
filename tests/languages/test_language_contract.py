"""v2.68 — Contract Resolution Seam + minimal AdapterRegistry.

The first Contract-Kernel increment: the harness resolves a project's language
to a profile and resolves the adapters that profile NAMES into a
ResolvedLanguageContract. A profile that names an adapter the registry cannot
supply is an INCOMPLETE contract (RED), never a silent green.

Additive in v2.68 — exercised here, not yet wired into live gates. Adapter
implementations land in v2.72, so against the (empty) default registry every
declared adapter resolves as missing; that is the incomplete-contract case.
"""

from __future__ import annotations

import pytest

from codd.languages import (
    AdapterRegistry,
    IncompleteLanguageContractError,
    UnknownLanguageError,
    build_language_contract,
    default_registry,
    resolve_language_contract,
    resolve_language_profile,
)

# Adapters the bundled go.yaml declares (kind implied by declaration site).
GO_ADAPTER_REFS = (
    "import_resolver:go-module",
    "runner_report:go-test-json",
    "scaffold:generic-template",
    "test_semantics:go-test-semantics",
)


def _register(refs):
    reg = AdapterRegistry()
    for ref in refs:
        kind, _, ident = ref.partition(":")
        reg.register(kind, ident, object())
    return reg


# ── resolve_language_profile: the single language-resolution seam ──


def test_resolve_profile_reads_project_language():
    profile = resolve_language_profile({"project": {"language": "go"}})
    assert profile is not None
    assert profile.identity.id == "go"


def test_resolve_profile_matches_alias_case_insensitive():
    # 'golang' is a go alias; resolution is case-insensitive.
    assert resolve_language_profile({"project": {"language": "Golang"}}).identity.id == "go"


def test_no_language_declared_returns_none():
    # No language → caller stays on the legacy path (not an error).
    assert resolve_language_profile({}) is None
    assert resolve_language_profile({"project": {}}) is None
    assert resolve_language_profile(None) is None


def test_declared_unknown_language_is_honest_error():
    # A declared-but-unknown language must raise, never silently resolve to None.
    with pytest.raises(UnknownLanguageError):
        resolve_language_profile({"project": {"language": "klingon"}})


# ── contract assembly: collect + resolve declared adapters ──


def test_go_contract_collects_declared_adapter_refs():
    profile = default_registry.resolve("go")
    contract = build_language_contract(profile, adapter_registry=AdapterRegistry())
    assert contract.language_id == "go"
    assert contract.adapter_ids == GO_ADAPTER_REFS  # sorted, deduped


def test_missing_adapter_is_incomplete_contract_red():
    # Empty registry → every declared adapter missing → incomplete → require RED.
    profile = default_registry.resolve("go")
    contract = build_language_contract(profile, adapter_registry=AdapterRegistry())
    assert contract.is_complete is False
    assert {r.ref for r in contract.missing_adapters} == set(GO_ADAPTER_REFS)
    with pytest.raises(IncompleteLanguageContractError) as exc:
        contract.require_complete()
    # The error names the missing adapter AND its declaration site (honest).
    assert "go-test-semantics" in str(exc.value)
    assert "tests.semantics_adapter" in str(exc.value)


def test_fully_registered_adapters_make_contract_complete():
    profile = default_registry.resolve("go")
    contract = build_language_contract(profile, adapter_registry=_register(GO_ADAPTER_REFS))
    assert contract.is_complete is True
    assert contract.missing_adapters == ()
    assert contract.require_complete() is contract  # no raise


def test_partial_registration_still_incomplete():
    profile = default_registry.resolve("go")
    reg = _register(GO_ADAPTER_REFS[:2])  # only 2 of 4 registered
    contract = build_language_contract(profile, adapter_registry=reg)
    assert contract.is_complete is False
    assert {r.ref for r in contract.missing_adapters} == set(GO_ADAPTER_REFS[2:])


# ── content hash + trace ──


def test_content_hash_is_deterministic_and_independent_of_registry():
    profile = default_registry.resolve("go")
    empty = build_language_contract(profile, adapter_registry=AdapterRegistry())
    full = build_language_contract(profile, adapter_registry=_register(GO_ADAPTER_REFS))
    # Hash is over the profile's declared contract, not which adapters happen
    # to be registered, so it is stable across environments.
    assert empty.content_hash == full.content_hash
    assert len(empty.content_hash) == 16


def test_distinct_languages_have_distinct_hashes():
    go = build_language_contract(default_registry.resolve("go"))
    py = build_language_contract(default_registry.resolve("python"))
    assert go.content_hash != py.content_hash


def test_to_trace_carries_kernel_fields():
    contract = build_language_contract(default_registry.resolve("go"), adapter_registry=AdapterRegistry())
    trace = contract.to_trace()
    assert trace["resolved_language_profile_id"] == "go"
    assert trace["language_contract_hash"] == contract.content_hash
    assert trace["adapter_ids"] == list(GO_ADAPTER_REFS)
    assert set(trace["missing_adapters"]) == set(GO_ADAPTER_REFS)


# ── end-to-end config → contract ──


@pytest.mark.parametrize("language", ["go", "python", "typescript"])
def test_all_bundled_profiles_resolve_through_the_seam(language):
    # Exit gate: existing go/python/typescript profiles resolve through the new
    # seam and produce a contract (complete or not).
    contract = resolve_language_contract({"project": {"language": language}})
    assert contract is not None
    assert contract.language_id == language
    assert len(contract.adapter_ids) >= 1


def test_resolve_contract_none_when_no_language():
    assert resolve_language_contract({}) is None

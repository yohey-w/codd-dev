"""Cut Condition A (dynamic gate) — synthetic-language extensibility proof.

A brand-new language ("toylang"), defined ONLY as a profile YAML with NO change
to any core code, must flow through the language-free seam:
resolve_language_profile → ResolvedLanguageContract → verify run plan → semantic
classification. The harness core branches on no language name, so a synthetic
language Just Works at the contract layer.

This is the v3.0 Contract-Kernel invariant "adding a language is profile-only"
proven at the resolution/contract/verify-plan layer, and a durable regression
guard against any future code that would re-introduce a language conditional in
the seam. (Full-pipeline synthetic-language greenfield is a later gate; this
proves the layer the kernel already owns is language-agnostic.)

NOTE: this test creates NO file under codd/ and imports NO synthetic-specific
code — the only artifact is a profile YAML in a tmp dir. That is the proof.
"""

from __future__ import annotations

import textwrap

import pytest

from codd.languages import (
    AdapterRegistry,
    IncompleteLanguageContractError,
    LanguageRegistry,
    build_language_contract,
    resolve_language_profile,
)
from codd.languages.verify_plan import (
    VerifyClass,
    VerifyOutcome,
    build_verify_plan,
    classify_verify_outcome,
)

# A complete-enough fictional language. NOTHING in codd/ knows this language;
# it exists only as this YAML.
_TOYLANG_PROFILE = textwrap.dedent(
    """\
    id: toylang
    display_name: ToyLang
    aliases: [toy]
    file_extensions: [".toy"]
    strictness: strict
    layout:
      source_sets:
        - id: main
          root: "src"
          file_globs: ["**/*.toy"]
      test_sets:
        - id: tests
          root: "tests"
          file_globs: ["**/*_test.toy"]
      package_root:
        kind: none
    toolchain:
      manifest:
        path: "toy.manifest"
        format: "toml"
    commands:
      verify:
        argv: ["toy", "test", "--report", "json"]
        cwd: "."
        report:
          path: ".codd/verify/toy.json"
          format: "toy-json"
          adapter: "toy-report"
    tests:
      semantics_adapter: "toy-semantics"
      runner_report_adapter: "toy-report"
    scaffold:
      adapter: "toy-template"
    ci:
      setup_steps:
        - uses: actions/setup-toy@v1
    """
)

_TOY_ADAPTER_REFS = (
    "runner_report:toy-report",
    "scaffold:toy-template",
    "test_semantics:toy-semantics",
)


@pytest.fixture
def toy_registry(tmp_path):
    (tmp_path / "toylang.yaml").write_text(_TOYLANG_PROFILE, encoding="utf-8")
    return LanguageRegistry(profiles_dir=tmp_path)


def _toy_config():
    return {"project": {"name": "demo", "language": "toylang"}}


# ── resolution: a new language loads with no core change ──


def test_synthetic_language_resolves(toy_registry):
    profile = resolve_language_profile(_toy_config(), registry=toy_registry)
    assert profile is not None
    assert profile.identity.id == "toylang"
    assert profile.strictness == "strict"


def test_synthetic_language_alias_resolves(toy_registry):
    profile = resolve_language_profile({"project": {"language": "TOY"}}, registry=toy_registry)
    assert profile is not None and profile.identity.id == "toylang"


# ── contract: declared adapters collected, missing ⇒ RED ──


def test_synthetic_contract_collects_declared_adapters(toy_registry):
    contract = build_language_contract(toy_registry.resolve("toylang"), adapter_registry=AdapterRegistry())
    assert set(contract.adapter_ids) == set(_TOY_ADAPTER_REFS)


def test_synthetic_missing_adapter_is_incomplete_red(toy_registry):
    contract = build_language_contract(toy_registry.resolve("toylang"), adapter_registry=AdapterRegistry())
    assert contract.is_complete is False
    with pytest.raises(IncompleteLanguageContractError):
        contract.require_complete()


def test_synthetic_complete_when_adapters_registered(toy_registry):
    reg = AdapterRegistry()
    for ref in _TOY_ADAPTER_REFS:
        kind, _, ident = ref.partition(":")
        reg.register(kind, ident, object())
    contract = build_language_contract(toy_registry.resolve("toylang"), adapter_registry=reg)
    assert contract.is_complete is True
    assert contract.require_complete() is contract  # no raise


# ── verify plan + classifier are language-agnostic ──


def test_synthetic_verify_plan_from_profile(toy_registry):
    contract = build_language_contract(toy_registry.resolve("toylang"))
    plan = build_verify_plan(contract)
    assert plan is not None
    assert plan.argv == ("toy", "test", "--report", "json")
    assert plan.report_required is True  # declared report.path
    assert plan.report_adapter == "toy-report"


def test_synthetic_classifier_holds_anti_false_green(toy_registry):
    contract = build_language_contract(toy_registry.resolve("toylang"))
    plan = build_verify_plan(contract)
    # zero-tests beats exit-0 (never green-on-nothing), regardless of language.
    assert (
        classify_verify_outcome(VerifyOutcome(spawned=True, returncode=0, zero_tests_observed=True), plan)
        is VerifyClass.ZERO_TESTS
    )
    # required report absent ⇒ REPORT_MISSING even on exit 0.
    assert (
        classify_verify_outcome(VerifyOutcome(spawned=True, returncode=0, report_present=False), plan)
        is VerifyClass.REPORT_MISSING
    )
    # clean run with report ⇒ PASS.
    assert (
        classify_verify_outcome(VerifyOutcome(spawned=True, returncode=0, report_present=True), plan)
        is VerifyClass.PASS
    )

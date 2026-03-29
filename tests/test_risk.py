"""Tests for R5.4 — Change risk scoring (risk.py)."""

import pytest

from codd.risk import ChangeRisk, build_change_risks
from codd.extractor import ModuleInfo, ProjectFacts, Symbol, CallEdge
from codd.traceability import TestCoverage
from codd.contracts import InterfaceContract
from codd.wiring import RuntimeWire


# ── Helper factories ────────────────────────────────────────

def make_symbol(name: str) -> Symbol:
    return Symbol(name=name, kind="function", file="src/mod.py", line=1)


def make_mod(name: str, **kwargs) -> ModuleInfo:
    mod = ModuleInfo(name=name)
    for k, v in kwargs.items():
        setattr(mod, k, v)
    return mod


def make_facts(*modules: ModuleInfo) -> ProjectFacts:
    facts = ProjectFacts(language="python", source_dirs=["src"])
    for mod in modules:
        facts.modules[mod.name] = mod
    return facts


def make_ic(public_symbols=None, internal_symbols=None,
            api_surface_ratio=0.0, encapsulation_violations=None) -> InterfaceContract:
    return InterfaceContract(
        module="",
        public_symbols=public_symbols or [],
        internal_symbols=internal_symbols or [],
        api_surface_ratio=api_surface_ratio,
        encapsulation_violations=encapsulation_violations or [],
    )


def make_tc(coverage_ratio: float) -> TestCoverage:
    return TestCoverage(module="", coverage_ratio=coverage_ratio)


# ── ChangeRisk dataclass ─────────────────────────────────────

def test_change_risk_defaults():
    cr = ChangeRisk(module="auth")
    assert cr.module == "auth"
    assert cr.score == 0.0
    assert cr.factors == {}


# ── Edge cases ───────────────────────────────────────────────

def test_build_change_risks_empty_facts():
    """No modules → change_risks is an empty list."""
    facts = ProjectFacts(language="python", source_dirs=["src"])
    build_change_risks(facts)
    assert facts.change_risks == []


def test_build_change_risks_single_module_no_deps():
    """Single module, no dependents, no coverage, no violations."""
    mod = make_mod("mymod",
                   internal_imports={},
                   call_edges=[],
                   interface_contract=make_ic(api_surface_ratio=0.5),
                   test_coverage=make_tc(0.0))
    facts = make_facts(mod)
    build_change_risks(facts)

    assert len(facts.change_risks) == 1
    cr = facts.change_risks[0]
    assert cr.module == "mymod"
    # dep_factor=0/1=0, cov_factor=1-0=1, api_factor=0.5, viol_factor=0/1=0
    expected = round(0.3 * 0 + 0.3 * 1.0 + 0.2 * 0.5 + 0.2 * 0.0, 2)
    assert cr.score == expected


def test_build_change_risks_formula():
    """Verify the exact formula: 0.3*dep + 0.3*(1-cov) + 0.2*api + 0.2*viol."""
    # Two modules: mod_a is imported by mod_b → dep_count for mod_a = 1
    mod_a = make_mod("mod_a",
                     internal_imports={},
                     call_edges=[],
                     interface_contract=make_ic(api_surface_ratio=0.8, encapsulation_violations=["x"]),
                     test_coverage=make_tc(0.5))

    mod_b = make_mod("mod_b",
                     internal_imports={"mod_a": ["from mod_a import foo"]},
                     call_edges=[],
                     interface_contract=make_ic(api_surface_ratio=0.2, encapsulation_violations=[]),
                     test_coverage=make_tc(1.0))

    facts = make_facts(mod_a, mod_b)
    build_change_risks(facts)

    risks_by_name = {cr.module: cr for cr in facts.change_risks}
    cr_a = risks_by_name["mod_a"]

    # dependents: mod_a has 1 dependent (mod_b imports it); max_dep = 1 → dep_factor = 1.0
    # coverage: 0.5 → cov_factor = 0.5
    # api_surface_ratio: 0.8 → api_factor = 0.8
    # violations: 1; max_viol = 1 → viol_factor = 1.0
    expected = round(0.3 * 1.0 + 0.3 * 0.5 + 0.2 * 0.8 + 0.2 * 1.0, 2)
    assert cr_a.score == expected


def test_build_change_risks_fully_covered_reduces_score():
    """A fully-tested module with no dependents has lower score than untested one."""
    covered = make_mod("covered",
                       internal_imports={},
                       call_edges=[],
                       interface_contract=make_ic(api_surface_ratio=0.0),
                       test_coverage=make_tc(1.0))

    uncovered = make_mod("uncovered",
                         internal_imports={},
                         call_edges=[],
                         interface_contract=make_ic(api_surface_ratio=0.0),
                         test_coverage=make_tc(0.0))

    facts = make_facts(covered, uncovered)
    build_change_risks(facts)

    by_name = {cr.module: cr for cr in facts.change_risks}
    assert by_name["covered"].score < by_name["uncovered"].score


def test_build_change_risks_sorted_descending():
    """change_risks is sorted from highest to lowest score."""
    mod_high = make_mod("high",
                        internal_imports={},
                        call_edges=[],
                        interface_contract=make_ic(api_surface_ratio=1.0, encapsulation_violations=["a", "b"]),
                        test_coverage=make_tc(0.0))

    mod_low = make_mod("low",
                       internal_imports={},
                       call_edges=[],
                       interface_contract=make_ic(api_surface_ratio=0.0),
                       test_coverage=make_tc(1.0))

    facts = make_facts(mod_high, mod_low)
    build_change_risks(facts)

    scores = [cr.score for cr in facts.change_risks]
    assert scores == sorted(scores, reverse=True)


def test_build_change_risks_factors_dict_keys():
    """ChangeRisk.factors must contain the four expected keys."""
    mod = make_mod("x",
                   internal_imports={},
                   call_edges=[],
                   interface_contract=make_ic(),
                   test_coverage=make_tc(0.0))
    facts = make_facts(mod)
    build_change_risks(facts)

    cr = facts.change_risks[0]
    assert set(cr.factors.keys()) == {"dependents", "uncovered", "api_surface", "violations"}


def test_build_change_risks_no_interface_contract():
    """Module without interface_contract defaults api_factor=1.0, violations=0."""
    mod = make_mod("plain",
                   internal_imports={},
                   call_edges=[],
                   interface_contract=None,
                   test_coverage=make_tc(0.0))
    facts = make_facts(mod)
    build_change_risks(facts)

    cr = facts.change_risks[0]
    # api_factor defaults to 1.0 → contributes 0.2 * 1.0 = 0.2
    assert cr.factors["api_surface"] == 1.0


def test_build_change_risks_no_test_coverage():
    """Module without test_coverage defaults coverage_ratio=0.0 (worst case)."""
    mod = make_mod("notested",
                   internal_imports={},
                   call_edges=[],
                   interface_contract=make_ic(api_surface_ratio=0.0),
                   test_coverage=None)
    facts = make_facts(mod)
    build_change_risks(facts)

    cr = facts.change_risks[0]
    assert cr.factors["uncovered"] == 1.0


def test_build_change_risks_call_edge_increments_dependent():
    """Call edges pointing to another module increment that module's dependent count."""
    mod_a = make_mod("mod_a",
                     internal_imports={},
                     call_edges=[],
                     interface_contract=make_ic(),
                     test_coverage=make_tc(0.5))

    mod_b = make_mod("mod_b",
                     internal_imports={},
                     call_edges=[CallEdge(caller="mod_b.func", callee="mod_a.helper",
                                          call_site="src/b.py:5")],
                     interface_contract=make_ic(),
                     test_coverage=make_tc(0.5))

    facts = make_facts(mod_a, mod_b)
    build_change_risks(facts)

    by_name = {cr.module: cr for cr in facts.change_risks}
    # mod_a is called by mod_b → dep_factor for mod_a should be > 0
    assert by_name["mod_a"].factors["dependents"] > 0.0


def test_build_change_risks_runtime_wire_increments_dependent():
    """Runtime wires targeting another module increment that module's dependent count."""
    mod_db = make_mod("get_db",
                      internal_imports={},
                      call_edges=[],
                      interface_contract=make_ic(),
                      test_coverage=make_tc(0.0))

    mod_api = make_mod("api",
                       internal_imports={},
                       call_edges=[],
                       runtime_wires=[RuntimeWire(kind="depends", source="api.py:5",
                                                   target="get_db", framework="fastapi")],
                       interface_contract=make_ic(),
                       test_coverage=make_tc(0.5))

    facts = make_facts(mod_db, mod_api)
    build_change_risks(facts)

    by_name = {cr.module: cr for cr in facts.change_risks}
    # get_db is wired from api → dependent count > 0
    assert by_name["get_db"].factors["dependents"] > 0.0


def test_build_change_risks_all_zero_values():
    """All zero metrics → score is 0.0 + 0.3 (uncovered=1 when coverage=0)."""
    mod = make_mod("zero",
                   internal_imports={},
                   call_edges=[],
                   interface_contract=make_ic(api_surface_ratio=0.0, encapsulation_violations=[]),
                   test_coverage=make_tc(0.0))
    facts = make_facts(mod)
    build_change_risks(facts)

    cr = facts.change_risks[0]
    # dep_factor=0, cov_factor=1.0, api_factor=0.0, viol_factor=0.0
    # score = 0.3 * 1.0 = 0.3
    assert cr.score == 0.3
    assert cr.factors["dependents"] == 0.0
    assert cr.factors["uncovered"] == 1.0
    assert cr.factors["api_surface"] == 0.0
    assert cr.factors["violations"] == 0.0

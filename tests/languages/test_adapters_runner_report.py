"""Contract Kernel — runner-report adapters relocated to a leaf module + lazily
registered on the shared AdapterRegistry (Steps 1+2, design
``dogfood/gpt_ck_verify_switch.md``).

These tests pin the four load-bearing guarantees of the relocation:

* RE-EXPORT IDENTITY — the parsers now LIVE in
  :mod:`codd.languages.adapters.runner_report` but are re-exported from
  :mod:`codd.coverage_execution_coherence`; both names must be the SAME object so
  every existing ``from codd.coverage_execution_coherence import RunnerExecution``
  import (and the coverage gate's own use) is unchanged.
* LEAF / LAZY — importing the leaf module is cheap and side-effect-free; it does
  NOT register anything (registration is deferred to
  :func:`ensure_builtin_adapters_registered`).
* REGISTRATION — after the lazy registration runs, the default registry resolves
  the two built-in runner-report adapters.
* FAIL-CLOSED — registration is idempotent for the same adapter but raises on a
  conflicting prior registration (a silent adapter override is an anti-false-green
  hazard).
"""

from __future__ import annotations

import pytest

from codd.languages.registry import AdapterRegistry, default_adapter_registry
from codd.languages.builtin_adapters import ensure_builtin_adapters_registered


# ── (a) re-export identity: leaf module IS the coverage gate's source ──


def test_runner_execution_is_same_object_across_modules():
    from codd.coverage_execution_coherence import RunnerExecution as cov_RE
    from codd.languages.adapters.runner_report import RunnerExecution as leaf_RE

    # Same class object — the relocation re-exports, it does not re-define.
    assert cov_RE is leaf_RE


def test_all_relocated_symbols_are_re_exported_identically():
    import codd.coverage_execution_coherence as cov
    from codd.languages.adapters import runner_report as leaf

    for name in (
        "RunnerReportUnsupported",
        "RunnerExecution",
        "RunnerReportAdapter",
        "VitestJsonReportAdapter",
        "GoTestJsonReportAdapter",
    ):
        assert getattr(cov, name) is getattr(leaf, name), f"{name} identity broke"


# ── (b) leaf / lazy: the module is importable and carries the classes ──


def test_leaf_module_importable_and_classes_exist():
    from codd.languages.adapters.runner_report import (
        GoTestJsonReportAdapter,
        RunnerExecution,
        RunnerReportAdapter,
        RunnerReportUnsupported,
        VitestJsonReportAdapter,
    )

    # The concrete adapters are instantiable (no hidden state / import need).
    assert VitestJsonReportAdapter() is not None
    assert GoTestJsonReportAdapter() is not None
    # Protocol + error type are present.
    assert issubclass(RunnerReportUnsupported, RuntimeError)
    assert RunnerReportAdapter is not None
    assert RunnerExecution().executed_files == frozenset()


def test_importing_leaf_module_does_not_register_on_a_fresh_registry():
    # A fresh, isolated registry stays empty just by importing the leaf module —
    # registration is the EXCLUSIVE job of ensure_builtin_adapters_registered.
    import importlib

    importlib.import_module("codd.languages.adapters.runner_report")
    fresh = AdapterRegistry()
    assert ("runner_report", "go-test-json") not in fresh
    assert ("runner_report", "vitest-json") not in fresh


# ── (c) registration populates the default registry ──


def test_ensure_registers_builtin_runner_report_adapters_on_default():
    from codd.languages.adapters.runner_report import (
        GoTestJsonReportAdapter,
        VitestJsonReportAdapter,
    )

    ensure_builtin_adapters_registered()
    assert ("runner_report", "go-test-json") in default_adapter_registry
    assert ("runner_report", "vitest-json") in default_adapter_registry
    # The registered objects are the real adapters (not placeholder stubs).
    assert isinstance(
        default_adapter_registry.get("runner_report", "go-test-json"),
        GoTestJsonReportAdapter,
    )
    assert isinstance(
        default_adapter_registry.get("runner_report", "vitest-json"),
        VitestJsonReportAdapter,
    )


def test_ensure_registers_on_an_explicit_registry():
    from codd.languages.adapters.runner_report import (
        GoTestJsonReportAdapter,
        VitestJsonReportAdapter,
    )

    reg = AdapterRegistry()
    ensure_builtin_adapters_registered(reg)
    assert isinstance(reg.get("runner_report", "vitest-json"), VitestJsonReportAdapter)
    assert isinstance(reg.get("runner_report", "go-test-json"), GoTestJsonReportAdapter)


# ── (d) idempotent ──


def test_ensure_is_idempotent_on_default():
    ensure_builtin_adapters_registered()
    ensure_builtin_adapters_registered()  # second call must not raise
    assert ("runner_report", "go-test-json") in default_adapter_registry


def test_ensure_is_idempotent_on_explicit_registry():
    reg = AdapterRegistry()
    ensure_builtin_adapters_registered(reg)
    before = reg.get("runner_report", "go-test-json")
    ensure_builtin_adapters_registered(reg)  # re-register SAME adapter → no-op
    after = reg.get("runner_report", "go-test-json")
    # Idempotent: same TYPE re-registers harmlessly (a fresh instance is allowed,
    # the contract only forbids a DIFFERENT adapter type at the key).
    assert type(before) is type(after)


# ── (e) collision is fail-closed (anti-false-green) ──


def test_collision_with_a_different_adapter_raises():
    reg = AdapterRegistry()
    # Pre-occupy the Go runner-report key with a DIFFERENT (foreign) object.
    sentinel = object()
    reg.register("runner_report", "go-test-json", sentinel)
    with pytest.raises(RuntimeError) as exc:
        ensure_builtin_adapters_registered(reg)
    assert "collision" in str(exc.value)
    # Fail-closed: the foreign object was NOT silently overwritten.
    assert reg.get("runner_report", "go-test-json") is sentinel

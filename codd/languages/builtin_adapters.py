"""Lazy, idempotent registration of the built-in adapters (Contract Kernel).

The :data:`codd.languages.registry.default_adapter_registry` ships EMPTY so that
importing :mod:`codd.languages` has no adapter-import side effect (the leaf rule —
see :mod:`codd.languages.adapters`). This module is the ONE place that populates it
with the concrete built-in adapters, and it does so LAZILY: the adapter classes are
imported INSIDE :func:`ensure_builtin_adapters_registered`, never at module load, so
``import codd.languages.builtin_adapters`` alone still triggers no adapter import and
no cycle with :mod:`codd.coverage_execution_coherence`.

Today this registers the two runner-report parsers (``vitest-json``,
``go-test-json``); as more concrete adapters are relocated into
:mod:`codd.languages.adapters`, they register here too. Registration is
FAIL-CLOSED on collision: re-registering the SAME object/type at a ``(kind, id)``
is a no-op, but a DIFFERENT adapter at an occupied key raises — a silent override
of an observation adapter would be exactly the kind of false-green this kernel
exists to prevent.
"""

from __future__ import annotations

from typing import Any

from .registry import AdapterRegistry, default_adapter_registry

#: Set once the built-in adapters have been registered on
#: :data:`default_adapter_registry`. Guards the (idempotent) fast path so repeat
#: calls targeting the default registry do no work; an explicit registry argument
#: always runs the (still-idempotent) registration so callers can populate their
#: own registry too.
_BUILTINS_REGISTERED = False


def _register_once(registry: AdapterRegistry, kind: str, id: str, adapter: Any) -> None:
    """Register ``adapter`` at ``(kind, id)`` — fail-closed on a conflicting prior.

    Idempotent for the SAME adapter: if ``(kind, id)`` already holds an object of
    the SAME type, this is a no-op (re-registering the built-in is harmless). But if
    a DIFFERENT adapter (different type) already occupies the key, raise — a silent
    overwrite of an observation adapter is a false-green vector (two report parsers
    fighting over one ``(kind, id)`` must surface, never be papered over).
    """

    existing = registry.get(kind, id)
    if existing is not None:
        if type(existing) is type(adapter):
            return  # same adapter already present — idempotent no-op
        raise RuntimeError(
            f"adapter collision for (kind={kind!r}, id={id!r}): "
            f"{type(existing).__name__} already registered, refusing to overwrite "
            f"with {type(adapter).__name__}. A silent adapter override would make "
            "observation semantics ambiguous (an anti-false-green hazard); register "
            "the conflicting adapter under a distinct id."
        )
    registry.register(kind, id, adapter)


def ensure_builtin_adapters_registered(registry: AdapterRegistry | None = None) -> None:
    """Idempotently register the built-in adapters on ``registry``.

    Targets ``registry`` when given, else the process-wide
    :data:`default_adapter_registry`. The concrete adapter classes are imported
    INSIDE this function (not at module load) so importing this module never pulls
    an adapter implementation — keeping registration lazy and cycle-free. Safe to
    call repeatedly: the default-registry path short-circuits after the first
    successful registration, and :func:`_register_once` is itself idempotent for
    the same adapter (so an explicit registry can be (re)populated harmlessly).
    """

    global _BUILTINS_REGISTERED

    target = registry if registry is not None else default_adapter_registry
    if target is default_adapter_registry and _BUILTINS_REGISTERED:
        return

    # Lazy import: the adapter implementations are read ONLY here, never at module
    # import time, so codd.languages stays free of an adapter/coverage import cycle.
    from codd.languages.adapters.runner_report import (
        CTestJunitReportAdapter,
        DotnetTrxReportAdapter,
        GoTestJsonReportAdapter,
        PlaywrightJsonReportAdapter,
        SurefireXmlReportAdapter,
        VitestJsonReportAdapter,
    )

    _register_once(target, "runner_report", "vitest-json", VitestJsonReportAdapter())
    _register_once(target, "runner_report", "go-test-json", GoTestJsonReportAdapter())
    # Compiler-language verify-report parsers (Java/C#/C++) — each declared by its
    # profile's ``verify.report.adapter`` id, registered HERE under the SAME
    # fail-closed ``_register_once`` as the JS/Go parsers (a profile that names an
    # unregistered runner_report adapter is an INCOMPLETE contract / RED, never a
    # silent green). The adapter classes are imported lazily above (leaf rule).
    _register_once(target, "runner_report", "surefire-xml", SurefireXmlReportAdapter())
    _register_once(target, "runner_report", "dotnet-trx", DotnetTrxReportAdapter())
    _register_once(target, "runner_report", "ctest-junit", CTestJunitReportAdapter())
    # Stack e2e: the Playwright addon declares ``report.adapter: playwright_json`` —
    # register under that EXACT id so a TEST-kind stack command's required report has a
    # resolvable adapter (an unregistered adapter for a required report is RED, never a
    # silent skip; v2.77d authenticity). Underscore spelling matches the profile.
    _register_once(target, "runner_report", "playwright_json", PlaywrightJsonReportAdapter())

    # Implement-oracle tool-semantics adapters (Contract Kernel oracle dispatch §3).
    # All three concrete adapters (go-toolchain / python-composite / typescript-tsc)
    # register inside register_oracle_adapters — every compiler/composite stack is now
    # on the contract path (no language-name dispatch left in the oracle gate).
    register_oracle_adapters(target)

    if target is default_adapter_registry:
        _BUILTINS_REGISTERED = True


def register_oracle_adapters(registry: AdapterRegistry) -> None:
    """Register the built-in ``implement_oracle`` adapters on ``registry`` (lazy/idempotent).

    The registry plumbing for the Contract Kernel oracle dispatch (§3): the concrete
    per-language oracle adapters (``go-toolchain`` / ``typescript-tsc`` /
    ``python-composite``) register HERE under kind
    :data:`codd.languages.contract.KIND_IMPLEMENT_ORACLE`, each via the SAME
    fail-closed :func:`_register_once` (a different adapter at an occupied
    ``(kind, id)`` raises — a silent oracle override is exactly the false-green this
    kernel forbids).

    Step 5 registered the ``go-toolchain`` adapter (Go on the contract path); step 6
    ``python-composite`` (Python's in-process ``kind=adapter`` composite); step 7
    ``typescript-tsc`` (tsc ``kind=command``). ALL THREE are now registered, so every
    compiler/composite stack routes to the contract path — the dispatch selection is
    GENERIC (modeled oracle + registered adapter), never a language-name comparison
    (Cut Condition A). The adapter classes are imported INSIDE this function (lazy),
    never at module load, preserving the leaf rule.
    """
    from codd.languages.adapters.oracle_cpp import CppToolchainOracleAdapter
    from codd.languages.adapters.oracle_csharp import DotnetToolchainOracleAdapter
    from codd.languages.adapters.oracle_go import GoToolchainOracleAdapter
    from codd.languages.adapters.oracle_java import JavaToolchainOracleAdapter
    from codd.languages.adapters.oracle_python import PythonCompositeOracleAdapter
    from codd.languages.adapters.oracle_typescript import TypeScriptTscOracleAdapter
    from codd.languages.contract import KIND_IMPLEMENT_ORACLE

    _register_once(registry, KIND_IMPLEMENT_ORACLE, "go-toolchain", GoToolchainOracleAdapter())
    _register_once(
        registry, KIND_IMPLEMENT_ORACLE, "python-composite", PythonCompositeOracleAdapter()
    )
    _register_once(
        registry, KIND_IMPLEMENT_ORACLE, "typescript-tsc", TypeScriptTscOracleAdapter()
    )
    # Compiler-stack composites (Java ``mvn compile`` / C# ``dotnet build`` / C++
    # ``cmake configure``+``build``): each is a ``kind="composite"`` shell-command
    # oracle resolved by the generic command-sequence executor, registered under the
    # SAME fail-closed ``_register_once`` (a different adapter at an occupied id
    # raises — a silent oracle override is exactly the false-green this kernel
    # forbids). Selection stays GENERIC (modeled oracle + registered adapter), never
    # a language-name comparison (Cut Condition A). Imported lazily above (leaf rule).
    _register_once(
        registry, KIND_IMPLEMENT_ORACLE, "java-toolchain", JavaToolchainOracleAdapter()
    )
    _register_once(
        registry, KIND_IMPLEMENT_ORACLE, "dotnet-toolchain", DotnetToolchainOracleAdapter()
    )
    _register_once(
        registry, KIND_IMPLEMENT_ORACLE, "cpp-toolchain", CppToolchainOracleAdapter()
    )

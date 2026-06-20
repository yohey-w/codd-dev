"""Concrete language adapters for the Contract Kernel (relocated leaves).

This package holds the concrete adapter implementations the language contract
resolves (runner-report parsers today). It is intentionally a LEAF surface: this
``__init__`` imports NOTHING (no submodule, no registration) so that importing
``codd.languages`` — or even ``codd.languages.adapters`` — never pulls an adapter
implementation or populates :data:`codd.languages.registry.default_adapter_registry`
at package-load time. Registration stays LAZY (see
:mod:`codd.languages.builtin_adapters`); adapter modules are imported only inside
the functions that need them, which is what keeps this package free of an import
cycle with :mod:`codd.coverage_execution_coherence`.
"""

from __future__ import annotations

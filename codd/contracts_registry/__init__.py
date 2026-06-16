"""ACG Contract Registry — the coherence-contract coverage layer for CoDD.

This package is an **audit / visualization layer**. It does NOT change any
existing CoDD gate behaviour. It DECLARES the coherence contracts CoDD enforces
(or has predicted but not yet enforced) and REPORTS their coverage, turning the
dogfood ledger from a "failure history" into a "contract coverage table"
(the minimal first step of GPT-5.5 Pro's ACG Contract Registry design, §5).

A *coherence contract* is a typed edge between two CoDD coherence nodes
(Requirement, DesignDoc, Plan/ImplementTask, GeneratedArtifact, Symbol,
TestClaim, Toolchain, RepairState, SessionState, SUTChannel, AIInvocation) with
one or more contract *dimensions* (existence, identity, uniqueness, ownership,
freshness, observability, authenticity, execution, repairability, budget,
termination). Each contract is either:

* ``covered``     — enforced by a named authority symbol AND pinned by a
                    regression test (``certification_fixtures``);
* ``uncertified`` — enforced by a named authority symbol but with no
                    regression-test pointer recorded here (CI should surface
                    these so a pointer gets added — but NOT block);
* ``uncovered``   — a predicted-but-unenforced cell (GPT §2 "空セル"): a latent
                    issue with a proposed gate, no authority yet.

Public surface (kept small and stable):

* :class:`Contract`            — the frozen contract record.
* :data:`REGISTRY`             — the tuple of all declared contracts.
* :func:`contracts_by_status`  — group the registry by status.
* :func:`coverage_summary`     — counts by status.
"""

from __future__ import annotations

from codd.contracts_registry.registry import (
    REGISTRY,
    Contract,
    contracts_by_status,
    coverage_summary,
)

__all__ = [
    "Contract",
    "REGISTRY",
    "contracts_by_status",
    "coverage_summary",
]

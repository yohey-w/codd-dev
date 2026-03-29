"""R5.4 — Change risk scoring for codd extract.

Computes per-module risk score based on:
- Number of dependents (import + call + runtime)
- Test coverage ratio (R5.1)
- API surface ratio (R4.3)
- Encapsulation violations (R4.3)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class ChangeRisk:
    """Per-module change risk assessment."""
    module: str
    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)


def build_change_risks(facts: ProjectFacts) -> None:
    """Populate ``facts.change_risks`` with per-module risk scores."""
    # Step 1: Count inbound dependents per module (import + call + runtime)
    dependents: dict[str, int] = {name: 0 for name in facts.modules}

    for mod in facts.modules.values():
        # Import dependents
        for dep_name in mod.internal_imports:
            if dep_name in dependents:
                dependents[dep_name] += 1
        # Call dependents
        for edge in mod.call_edges:
            target = edge.callee.split(".")[0]
            if target in dependents and target != mod.name:
                dependents[target] += 1
        # Runtime wire dependents
        for wire in getattr(mod, "runtime_wires", []):
            target = wire.target.split(".")[0]
            if target in dependents and target != mod.name:
                dependents[target] += 1

    max_dep = max(dependents.values()) if dependents else 1
    if max_dep == 0:
        max_dep = 1

    # Step 2: Collect max violations
    violations: dict[str, int] = {}
    for mod in facts.modules.values():
        ic = mod.interface_contract
        violations[mod.name] = len(ic.encapsulation_violations) if ic else 0
    max_viol = max(violations.values()) if violations else 1
    if max_viol == 0:
        max_viol = 1

    # Step 3: Compute risk score per module
    risks: list[ChangeRisk] = []
    for mod in facts.modules.values():
        # Coverage ratio (from R5.1 test traceability)
        tc = getattr(mod, "test_coverage", None)
        coverage_ratio = tc.coverage_ratio if tc else 0.0

        # API surface ratio (from R4.3 interface contracts)
        ic = mod.interface_contract
        api_ratio = ic.api_surface_ratio if ic else 1.0

        # Violation count
        viol_count = violations.get(mod.name, 0)

        # Dependent count
        dep_count = dependents.get(mod.name, 0)

        # Formula
        dep_factor = dep_count / max_dep
        cov_factor = 1.0 - coverage_ratio
        api_factor = api_ratio
        viol_factor = viol_count / max_viol

        score = (0.3 * dep_factor
                 + 0.3 * cov_factor
                 + 0.2 * api_factor
                 + 0.2 * viol_factor)

        risks.append(ChangeRisk(
            module=mod.name,
            score=round(score, 2),
            factors={
                "dependents": round(dep_factor, 2),
                "uncovered": round(cov_factor, 2),
                "api_surface": round(api_factor, 2),
                "violations": round(viol_factor, 2),
            },
        ))

    facts.change_risks = sorted(risks, key=lambda r: -r.score)

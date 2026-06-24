"""DAG check: extraction diagnostics for declared capability patterns.

The capability-evidence extractor (``codd/dag/extractor.py``) compiles every
regex declared under ``coherence.capability_patterns`` and, on a compile error,
does ``except re.error: continue`` — the bad pattern is *silently* dropped and
that capability's detector never fires again. The capability then appears to be
verified while in reality nothing is checked: a false-green source.

This check re-validates the declared patterns itself (it does not touch the
extractor) and surfaces any regex that fails to compile as an **amber**
diagnostic. It is advisory only:

* **amber only — never red.** A config-level regex typo is an authoring mistake,
  not a deploy blocker; gating it red would be a false red.
* **dormant by default.** A project that declares no ``capability_patterns``
  gets ``skip`` (exit code unaffected) — legacy / unrelated projects keep
  passing unchanged.
* **generality.** The core carries no project / framework / language literal;
  it only inspects the regexes the project itself declared.

The pattern-shape parsing mirrors ``extractor._pattern_match_specs`` so the set
of regexes inspected here matches exactly the set the extractor would attempt to
compile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping

from codd.dag.checks import DagCheck, register_dag_check

try:  # Reuse the builder's accessor so the config location stays in lockstep.
    from codd.dag.builder import _capability_patterns as _builder_capability_patterns
except Exception:  # pragma: no cover - defensive: fall back to inline read.
    _builder_capability_patterns = None


@dataclass
class ExtractionDiagnosticsResult:
    check_name: str = "extraction_diagnostics"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    checked_count: int = 0  # regexes whose compile was attempted; 0 on pass = vacuous
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("extraction_diagnostics")
class ExtractionDiagnosticsCheck(DagCheck):
    """Warn (amber) when a declared capability_pattern regex cannot compile."""

    check_name = "extraction_diagnostics"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> ExtractionDiagnosticsResult:
        if settings is not None:
            self.settings = settings

        config = codd_config if codd_config is not None else self.settings
        capability_patterns = self._capability_patterns(config)

        if not capability_patterns:
            return ExtractionDiagnosticsResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                message=(
                    "extraction_diagnostics SKIP "
                    "(no coherence.capability_patterns declared)"
                ),
            )

        diagnostics: list[dict[str, Any]] = []
        checked_count = 0
        for capability_kind, pattern_spec in capability_patterns.items():
            kind = str(capability_kind)
            for match_spec in _pattern_match_specs(pattern_spec):
                regex_text = match_spec.get("regex")
                if not isinstance(regex_text, str) or not regex_text:
                    continue
                checked_count += 1
                try:
                    re.compile(regex_text)
                except re.error as exc:
                    diagnostics.append(_invalid_regex_diagnostic(kind, regex_text, exc))

        if diagnostics:
            return ExtractionDiagnosticsResult(
                status="warn",
                severity="amber",
                passed=True,
                block_deploy=False,
                checked_count=checked_count,
                warnings=diagnostics,
                message=(
                    f"extraction_diagnostics found {len(diagnostics)} "
                    f"capability_pattern(s) with an uncompilable regex "
                    f"({checked_count} regex(es) checked)"
                ),
            )

        return ExtractionDiagnosticsResult(
            status="pass",
            severity="amber",
            passed=True,
            block_deploy=False,
            checked_count=checked_count,
            message=(
                f"extraction_diagnostics PASS "
                f"({checked_count} capability_pattern regex(es) compile cleanly)"
            ),
        )

    @staticmethod
    def _capability_patterns(config: Any) -> dict[str, Any]:
        """Resolve ``coherence.capability_patterns`` the same way the builder does."""
        if not isinstance(config, Mapping):
            return {}
        if _builder_capability_patterns is not None:
            patterns = _builder_capability_patterns(config)
            return patterns if isinstance(patterns, dict) else {}
        coherence = config.get("coherence")
        if not isinstance(coherence, Mapping):
            return {}
        patterns = coherence.get("capability_patterns")
        return patterns if isinstance(patterns, dict) else {}


def _pattern_match_specs(pattern_spec: Any) -> list[dict[str, Any]]:
    """Mirror ``extractor._pattern_match_specs`` so the same regexes are inspected."""
    if isinstance(pattern_spec, dict):
        matches = pattern_spec.get("matches")
        if isinstance(matches, list):
            return [match for match in matches if isinstance(match, dict)]
        if "regex" in pattern_spec:
            return [pattern_spec]
    if isinstance(pattern_spec, list):
        return [match for match in pattern_spec if isinstance(match, dict)]
    return []


def _invalid_regex_diagnostic(kind: str, regex_text: str, exc: re.error) -> dict[str, Any]:
    return {
        "type": "invalid_regex",
        "capability": kind,
        "regex": regex_text,
        "error": str(exc),
        "severity": "amber",
        "remediation": (
            f"Fix the regex for capability '{kind}' (cannot compile: {exc}), "
            "or remove the pattern."
        ),
    }


__all__ = ["ExtractionDiagnosticsCheck", "ExtractionDiagnosticsResult"]

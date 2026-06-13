"""Code-level patch-scope guard for AUTO-mode repairs (anti-false-green).

Threading the per-run auto opt-in to the approval gate (see ``apply_repair_mode``
+ ``RepairLoopConfig.codd_yaml``) takes a previously-BLOCKED path and makes it
LIVE: unattended code mutation to clear a red verify. The existing guards
(attribution editability, post-repair-verify, greenfield's second verify +
``_certify_verify_executed``, the ``max_files_per_proposal`` valve) are necessary
but NOT sufficient, because patch-scope was only enforced at the *prompt* level:

- :class:`~codd.repair.schema.RepairProposal` accepts arbitrary ``file_path``
  entries from the model.
- :class:`~codd.repair.git_patcher.GitPatcher` only enforces "relative path
  inside the project root", NOT "path is in the editable target set".

So an auto-applied repair could WEAKEN THE ORACLE — edit ``codd.yaml`` /
``pytest.ini`` / a ``package.json`` test script / ``conftest.py`` / a CI workflow
/ a design-spec doc that derives a test — making the fresh post-repair verify
pass against the weakened oracle. That is a false-green the post-verify CANNOT
catch (it is checking the weakened oracle). The codex5 repair that edited
``docs/infra/ci.md`` (a spec doc) for a code-addressable governance failure is
exactly this vector.

This module is the executable, code-level scope guard. It runs in AUTO mode
BEFORE a proposal is approved/applied and decides, per patch path, whether the
edit is in-scope for the PICKED PRIMARY failure + its RCA. It is ROLE-AWARE and
integrates with the existing B0 attribution
(:mod:`codd.repair.test_failure_attribution`):

- implementation / source / config-source files → may auto-apply for a
  ``code_addressable`` failure.
- test files → read-only (auto-editable ONLY for a ``harness_contract_violation``
  — a genuinely broken test / conftest / scaffold; the attribution already
  encodes this, we enforce it against the ACTUAL proposal paths).
- spec / design / requirements docs + test-harness / gate-control files
  (``codd.yaml``, ``pytest.ini`` / ``pyproject`` test config, ``package.json``
  test script, ``conftest.py`` [except a harness-contract violation], CI workflow
  files, and design/spec docs that derive tests) → for a ``code_addressable``
  failure, MUST NOT be auto-edited (changing the oracle to match buggy code is a
  false-green). Such a path fails loudly / escalates to required approval (in
  non-interactive auto mode an escalation is honestly rejected → REPAIR_FAILED).
  The ONLY exception is when the primary failure class EXPLICITLY identifies that
  artifact itself as the defect (a malformed/stale doc failure, not a
  code-addressable test failure).

This guard ADDS to the existing valves; it never weakens them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Mapping

from codd.repair.schema import RepairProposal, RootCauseAnalysis, VerificationFailureReport
from codd.repair.test_failure_attribution import (
    CODE_ADDRESSABLE_CLASSES,
    PROVENANCE_SOURCE,
    PROVENANCE_TEST,
)


@dataclass
class AutoScopeDecision:
    """Outcome of the AUTO-mode scope check for one proposal."""

    #: True when every patch path is in-scope and may be auto-applied.
    allowed: bool
    #: True when at least one path is out-of-scope in a way that should escalate
    #: to required approval (oracle/spec/gate-control or test edit for a
    #: non-harness failure, or a path entirely outside the editable set). In
    #: non-interactive auto mode the loop honestly rejects an escalation.
    escalate: bool = False
    #: Human-readable explanation surfaced into the failure / history.
    reason: str = ""
    #: The offending patch paths (for diagnostics).
    offending_paths: list[str] = field(default_factory=list)


# ── Oracle / gate-control artefact recognition ──────────────────────────────
#
# These are the "definition of pass" — editing them to satisfy buggy code is the
# false-green vector. Recognition is by FILENAME / suffix / well-known location,
# language-neutral and project-literal-free (no per-project paths baked in).

#: Exact basenames that are CoDD / test-harness / gate-control configuration.
_GATE_CONTROL_BASENAMES: frozenset[str] = frozenset(
    {
        "codd.yaml",
        "codd.yml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "conftest.py",
        ".coveragerc",
        "jest.config.js",
        "jest.config.ts",
        "jest.config.mjs",
        "jest.config.cjs",
        "jest.config.json",
        "vitest.config.js",
        "vitest.config.ts",
        "playwright.config.js",
        "playwright.config.ts",
        "cypress.config.js",
        "cypress.config.ts",
        "karma.conf.js",
        "phpunit.xml",
        "phpunit.xml.dist",
    }
)

#: ``pyproject.toml`` / ``package.json`` are gate-control ONLY when they carry a
#: test/oracle definition; otherwise they are ordinary config-source. We treat
#: them as gate-control conservatively (they almost always define the test
#: command / pytest config / jest config), which keeps the anti-false-green bar
#: high. A genuine non-test edit to them is rare in an auto-repair and is better
#: escalated to a human than silently applied.
_GATE_CONTROL_PROJECT_MANIFESTS: frozenset[str] = frozenset(
    {"pyproject.toml", "package.json"}
)

#: CI-workflow locations (a later gate trusts these).
_CI_DIR_MARKERS: tuple[tuple[str, ...], ...] = (
    (".github", "workflows"),
    (".gitlab-ci.yml",),
    (".circleci",),
    (".buildkite",),
)

#: Documentation / spec / design / requirements roots and suffixes. A
#: code-addressable governance failure must not be "repaired" by editing the
#: spec the test is derived from.
_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt"})
_SPEC_DIR_MARKERS: frozenset[str] = frozenset(
    {"docs", "doc", "design", "designs", "specs", "spec", "requirements"}
)

#: Test-file recognition (mirrors common conventions across stacks). Used to
#: flag a patch that edits a TEST for a non-harness failure (B0 keeps those
#: read-only; we enforce it against the actual proposal path even when the
#: model ignored the prompt).
_TEST_PATH = re.compile(
    r"(^|/)tests?(/|$)"  # tests/ or test/ directory
    r"|(^|/)test_[^/]+\.py$"  # pytest test_*.py
    r"|[^/]+_test\.(py|go)$"  # *_test.py / *_test.go
    r"|[^/]+\.test\.(js|jsx|ts|tsx)$"  # *.test.ts
    r"|[^/]+\.spec\.(js|jsx|ts|tsx)$",  # *.spec.ts
    re.IGNORECASE,
)


def evaluate_auto_patch_scope(
    proposal: RepairProposal,
    failure: VerificationFailureReport | None,
    rca: RootCauseAnalysis | None,
    *,
    project_root: Any = None,
    codd_yaml: Mapping[str, Any] | None = None,
) -> AutoScopeDecision:
    """Decide whether *proposal* is in-scope for unattended AUTO-mode apply.

    Build the editable allowlist from the PICKED PRIMARY failure + its RCA target
    (not the whole batch of failures), then check every patch path role-aware
    against the primary failure class. Returns an :class:`AutoScopeDecision`; the
    caller (the repair loop, in auto mode only) escalates/rejects on
    ``allowed=False``.
    """
    patch_paths = [_normalize(patch.file_path) for patch in proposal.patches]
    patch_paths = [path for path in patch_paths if path]
    if not patch_paths:
        # An empty proposal is rejected upstream; nothing to scope-check.
        return AutoScopeDecision(allowed=True, reason="no patch paths to validate")

    failure_class = str(getattr(failure, "failure_class", "") or "")
    code_addressable = bool(getattr(failure, "code_addressable", False))
    allowlist = _editable_allowlist(failure, rca)
    # Containment is enforced strictly ONLY when the primary failure resolved a
    # CONCRETE editable path set (B0 attribution of an executed test/typecheck
    # failure). A purely structural DAG failure carries logical node IDs
    # (``impl:main``, ``design:x``) that this guard does not expand into file
    # paths without the DAG; for those the role-based oracle/test protections
    # below still fully block the false-green vector, while legitimate
    # source/doc drift repair keeps working (the historical structural path).
    has_resolved_targets = any(_looks_like_path(item) for item in allowlist)

    offending: list[str] = []
    reasons: list[str] = []
    for path in patch_paths:
        verdict = _classify_path(
            path,
            failure_class=failure_class,
            code_addressable=code_addressable,
            allowlist=allowlist,
            enforce_containment=has_resolved_targets,
        )
        if verdict is not None:
            offending.append(path)
            reasons.append(verdict)

    if not offending:
        return AutoScopeDecision(allowed=True, reason="all patch paths in editable scope")

    detail = "; ".join(reasons)
    return AutoScopeDecision(
        allowed=False,
        escalate=True,
        reason=(
            "auto-repair patch-scope violation (anti-false-green): "
            f"{detail}. Out-of-scope edits to oracle/spec/test-harness/gate-control "
            "files for a code-addressable failure are not auto-applied; escalating "
            "to required approval."
        ),
        offending_paths=offending,
    )


# ── allowlist construction (picked primary failure + RCA only) ───────────────

def _editable_allowlist(
    failure: VerificationFailureReport | None,
    rca: RootCauseAnalysis | None,
) -> set[str]:
    """Editable candidate paths from the PRIMARY failure + its RCA.

    Deliberately scoped to the picked primary failure (``failed_nodes`` are the
    EDITABLE source/config targets B0 resolved) plus the RCA's affected nodes —
    NOT the whole batch of failures (GPT risk-3: an aggregated allowlist could
    sweep in an oracle file unrelated to the primary repair).
    """
    allow: set[str] = set()
    for raw in list(getattr(failure, "failed_nodes", []) or []):
        normalized = _node_to_path(raw)
        if normalized:
            allow.add(normalized)
    for raw in list(getattr(rca, "affected_nodes", []) or []):
        normalized = _node_to_path(raw)
        if normalized:
            allow.add(normalized)
    return allow


def _looks_like_path(text: str) -> bool:
    """True when *text* is a concrete file path (has a '/' or a real suffix).

    Distinguishes B0's resolved file paths (``src/main.py``,
    ``docs/infra/ci.md``) from bare logical DAG node ids (``main``) that this
    guard cannot expand into a path without the DAG.
    """
    if not text:
        return False
    if "/" in text:
        return True
    return bool(PurePosixPath(text).suffix)


def _node_to_path(raw: Any) -> str:
    """Best-effort node-ref → project-relative path.

    ``failed_nodes`` / ``affected_nodes`` may be project-relative paths (B0
    attribution) or DAG node IDs (e.g. ``impl:src/main.py``). Pull a path-looking
    token out; a bare logical id (no path component) is ignored for allowlist
    purposes (the role check below will reject any concrete path not derivable
    from one).
    """
    text = _normalize(raw)
    if not text:
        return ""
    # DAG ids are often "kind:path" — keep the part after the last ':' when it
    # looks like a path (has a suffix or a '/').
    if ":" in text and "/" not in text.split(":", 1)[0]:
        tail = text.split(":", 1)[1].strip()
        if tail and ("/" in tail or "." in PurePosixPath(tail).name):
            text = tail
    return text


# ── role-aware per-path classification ───────────────────────────────────────

def _classify_path(
    path: str,
    *,
    failure_class: str,
    code_addressable: bool,
    allowlist: set[str],
    enforce_containment: bool,
) -> str | None:
    """Return a rejection reason for *path*, or ``None`` when it is in-scope.

    Role precedence (most-protected first):

    1. Oracle / spec / gate-control artefact + a code-addressable failure →
       reject, UNLESS the failure class explicitly identifies THIS artefact as
       the defect (a harness-contract violation whose attribution named it).
    2. Test file + a non-harness failure → reject (B0 keeps tests read-only for
       assertion/runtime; enforce it against the actual proposal path even when
       the model ignored the prompt).
    3. Implementation / source / config-source → when the primary failure
       resolved concrete editable targets (B0), it must be inside that allowlist;
       otherwise (a structural DAG failure with logical node IDs) it is allowed,
       since the oracle/test protections above already block the false-green
       vector and structural source/doc drift repair is legitimate.
    """
    is_oracle = _is_oracle_artifact(path)
    is_test = _is_test_file(path)
    harness_contract = failure_class == "harness_contract_violation"

    # (1) Oracle / spec / gate-control protection for code-addressable failures.
    if is_oracle and code_addressable:
        # Exception: the primary failure IS this artefact being broken (a
        # harness-contract violation whose attribution named this path).
        if harness_contract and path in allowlist:
            return None
        return (
            f"'{path}' is an oracle/spec/test-harness/gate-control artefact and the "
            f"primary failure is code-addressable ({failure_class or 'unknown'}); "
            "editing the definition of 'pass' to match code would be a false-green"
        )

    # (2) Test files stay read-only unless the failure is a harness-contract one.
    if is_test and not is_oracle:
        if harness_contract:
            # A genuinely broken test/scaffold IS the defect; allow only when the
            # attribution named it editable (i.e. it is in the allowlist).
            if path in allowlist:
                return None
            return (
                f"'{path}' is a test file not attributed as the broken harness; "
                "auto-repair may not rewrite a test it was not handed as editable"
            )
        return (
            f"'{path}' is a test file and the failure class is '{failure_class or 'unknown'}'; "
            "auto-repair may not rewrite a substantive test to make it pass"
        )

    # (3) Implementation / source / config-source containment.
    if not enforce_containment:
        return None
    if path in allowlist:
        return None
    return (
        f"'{path}' is outside the editable candidate set resolved from the primary "
        "failure + RCA target; auto-repair may only touch attributed implementation/"
        "config files"
    )


def _is_oracle_artifact(path: str) -> bool:
    """True for spec/design/requirements docs + test-harness/gate-control files."""
    pure = PurePosixPath(path)
    name = pure.name
    name_lower = name.lower()
    parts_lower = tuple(part.lower() for part in pure.parts)

    if name_lower in _GATE_CONTROL_BASENAMES:
        return True
    if name_lower in _GATE_CONTROL_PROJECT_MANIFESTS:
        return True
    if _in_ci_location(parts_lower, name_lower):
        return True
    if pure.suffix.lower() in _DOC_SUFFIXES and _under_spec_dir(parts_lower):
        return True
    return False


def _in_ci_location(parts_lower: tuple[str, ...], name_lower: str) -> bool:
    for marker in _CI_DIR_MARKERS:
        if len(marker) == 1:
            # A single-token marker is either a directory (its name appears as a
            # path component) or an exact filename at any depth.
            token = marker[0]
            if token in parts_lower or name_lower == token:
                return True
            continue
        # Multi-token marker: the tokens must appear consecutively in order.
        if _contains_subsequence(parts_lower, marker):
            return True
    return False


def _contains_subsequence(parts: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    span = len(marker)
    for index in range(0, len(parts) - span + 1):
        if parts[index : index + span] == marker:
            return True
    return False


def _under_spec_dir(parts_lower: tuple[str, ...]) -> bool:
    # All but the final component are directories.
    return any(part in _SPEC_DIR_MARKERS for part in parts_lower[:-1])


def _is_test_file(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def _normalize(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    # Drop a leading "./" but keep the rest verbatim (paths are project-relative).
    return text[2:] if text.startswith("./") else text


__all__ = [
    "AutoScopeDecision",
    "evaluate_auto_patch_scope",
]

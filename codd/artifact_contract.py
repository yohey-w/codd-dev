"""Artifact Contracts — deterministic gate over the V-model artifact catalog.

CoDD pipeline stages have historically judged completion by *the process
finished*, not by *the required artifacts were produced*. Artifact Contracts
close that structural gap: a project declares, per stage, which catalog
artifacts that stage MUST produce, and a deterministic gate verifies they
exist and pass their validator. Quality is then guaranteed by harness
STRUCTURE rather than model cleverness (the model-independence hypothesis).

This module is pure logic (no click). ``cli.py`` wires I/O around it.

Three concerns live here:

1. **Catalog** — :func:`load_catalog` reads the shipped, universal
   ``artifacts/catalog.yaml`` into :class:`ArtifactCatalog` and validates its
   normalization invariants (every ``derived_view`` declares a non-empty
   ``derived_from`` referencing real ``ssot`` ids; ``ssot`` artifacts declare
   none).
2. **Contract** — :func:`load_contract` reads the per-project, opt-in
   ``artifact_contract:`` section from a resolved codd config into
   :class:`ArtifactContract`. Absent / ``enabled: false`` ⇒ zero behavior.
3. **Verify** — :func:`verify_contract` deterministically checks, for each
   declared stage, that the required artifacts exist and pass their validator,
   returning a structured :class:`ContractVerifyReport`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from codd.frontmatter import split_frontmatter


CATALOG_PATH = Path(__file__).with_name("artifacts") / "catalog.yaml"

CONTRACT_KEY = "artifact_contract"

_VALID_KINDS = {"ssot", "derived_view"}

# Syntax of a NAMESPACED required-artifact id (the id space used by
# required_artifacts/defaults/*.yaml), e.g. `design:system_design`.
REQUIRED_ID_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+$")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogArtifact:
    id: str
    description: str
    kind: str
    produced_by: str
    default_path_globs: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    validator: str | None = None
    # NAMESPACED required-artifact ids (`category:name`) this catalog artifact
    # covers — the explicit cross-space mapping consumed by codd.artifact_ids.
    # Optional and additive: entries without it behave exactly as before.
    required_artifact_ids: tuple[str, ...] = ()

    @property
    def is_ssot(self) -> bool:
        return self.kind == "ssot"

    @property
    def is_derived_view(self) -> bool:
        return self.kind == "derived_view"


@dataclass(frozen=True)
class ArtifactCatalog:
    version: int
    artifacts: tuple[CatalogArtifact, ...]
    # Required-artifact ids that deliberately map to NO catalog artifact
    # (declared at the catalog top level). The drift guard treats any required
    # id that is neither mapped nor listed here as an error.
    intentionally_unmapped_required_ids: tuple[str, ...] = ()

    def get(self, artifact_id: str) -> CatalogArtifact | None:
        for artifact in self.artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self.artifacts)


class CatalogError(ValueError):
    """Raised when the shipped catalog violates its normalization invariants."""


def load_catalog(path: str | Path | None = None) -> ArtifactCatalog:
    """Load and validate the core artifact catalog.

    Raises :class:`CatalogError` if the catalog breaks normalization:
    duplicate ids, unknown ``kind``, an ssot artifact that declares
    ``derived_from``, or a derived_view whose ``derived_from`` is empty or
    references an id not present in the catalog. The optional cross-space
    mapping is also validated: every ``required_artifact_ids`` entry (and every
    ``intentionally_unmapped_required_ids`` entry) must match
    ``^[a-z0-9_]+:[a-z0-9_]+$``, a required id may map to AT MOST ONE catalog
    artifact, and an intentionally-unmapped id must not also be mapped.
    """

    catalog_path = Path(path) if path is not None else CATALOG_PATH
    if not catalog_path.exists():
        raise CatalogError(f"artifact catalog not found: {catalog_path}")

    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise CatalogError("catalog must be a YAML mapping")

    version = int(raw.get("version", 1))
    entries = raw.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("catalog must declare a non-empty 'artifacts' list")

    artifacts: list[CatalogArtifact] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CatalogError("each catalog artifact must be a mapping")
        artifact_id = str(entry.get("id") or "").strip()
        if not artifact_id:
            raise CatalogError("catalog artifact missing 'id'")
        if artifact_id in seen_ids:
            raise CatalogError(f"duplicate catalog artifact id: {artifact_id}")
        seen_ids.add(artifact_id)

        kind = str(entry.get("kind") or "").strip()
        if kind not in _VALID_KINDS:
            raise CatalogError(
                f"artifact '{artifact_id}' has invalid kind '{kind}' "
                f"(expected one of {sorted(_VALID_KINDS)})"
            )

        derived_from = tuple(str(x) for x in (entry.get("derived_from") or []))
        validator = entry.get("validator")
        artifacts.append(
            CatalogArtifact(
                id=artifact_id,
                description=str(entry.get("description") or ""),
                kind=kind,
                produced_by=str(entry.get("produced_by") or ""),
                default_path_globs=tuple(str(g) for g in (entry.get("default_path_globs") or [])),
                derived_from=derived_from,
                validator=str(validator) if validator else None,
                required_artifact_ids=tuple(
                    str(r) for r in (entry.get("required_artifact_ids") or [])
                ),
            )
        )

    intentionally_unmapped = tuple(
        str(r) for r in (raw.get("intentionally_unmapped_required_ids") or [])
    )

    catalog = ArtifactCatalog(
        version=version,
        artifacts=tuple(artifacts),
        intentionally_unmapped_required_ids=intentionally_unmapped,
    )

    # Normalization invariants: enforce SSOT vs derived_view separation.
    for artifact in artifacts:
        if artifact.is_ssot and artifact.derived_from:
            raise CatalogError(
                f"ssot artifact '{artifact.id}' must not declare derived_from"
            )
        if artifact.is_derived_view:
            if not artifact.derived_from:
                raise CatalogError(
                    f"derived_view artifact '{artifact.id}' must declare a "
                    "non-empty derived_from"
                )
            for ref in artifact.derived_from:
                if catalog.get(ref) is None:
                    raise CatalogError(
                        f"derived_view artifact '{artifact.id}' references "
                        f"unknown derived_from id '{ref}'"
                    )

    # Cross-space mapping invariants: required-id syntax + uniqueness (one
    # required id maps to at most one catalog artifact) + no overlap with the
    # intentionally-unmapped declaration.
    required_owner: dict[str, str] = {}
    for artifact in artifacts:
        for required_id in artifact.required_artifact_ids:
            if not REQUIRED_ID_RE.match(required_id):
                raise CatalogError(
                    f"artifact '{artifact.id}' declares invalid required_artifact_id "
                    f"'{required_id}' (expected `^[a-z0-9_]+:[a-z0-9_]+$`, "
                    "e.g. 'design:system_design')"
                )
            owner = required_owner.get(required_id)
            if owner is not None:
                raise CatalogError(
                    f"required artifact id '{required_id}' is mapped by both "
                    f"'{owner}' and '{artifact.id}' (a required id maps to at "
                    "most one catalog artifact)"
                )
            required_owner[required_id] = artifact.id
    for required_id in intentionally_unmapped:
        if not REQUIRED_ID_RE.match(required_id):
            raise CatalogError(
                "intentionally_unmapped_required_ids entry "
                f"'{required_id}' is invalid (expected `^[a-z0-9_]+:[a-z0-9_]+$`)"
            )
        if required_id in required_owner:
            raise CatalogError(
                f"required artifact id '{required_id}' is declared "
                "intentionally-unmapped but is mapped by catalog artifact "
                f"'{required_owner[required_id]}'"
            )

    return catalog


# ---------------------------------------------------------------------------
# Contract (per-project, opt-in)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArtifactContract:
    enabled: bool
    stages: dict[str, tuple[str, ...]]

    @property
    def is_active(self) -> bool:
        return self.enabled and bool(self.stages)


def load_contract(codd_config: Mapping[str, Any] | None) -> ArtifactContract:
    """Resolve the ``artifact_contract:`` section from a codd config.

    Absent section or ``enabled: false`` yields an inactive contract (zero
    behavior change). Unknown keys are ignored; ``stages`` maps a stage name
    to the list of catalog artifact ids that stage must produce.
    """

    section: Mapping[str, Any] = {}
    if isinstance(codd_config, Mapping):
        candidate = codd_config.get(CONTRACT_KEY)
        if isinstance(candidate, Mapping):
            section = candidate

    enabled = bool(section.get("enabled", False))
    stages_raw = section.get("stages") or {}
    stages: dict[str, tuple[str, ...]] = {}
    if isinstance(stages_raw, Mapping):
        for stage_name, ids in stages_raw.items():
            if isinstance(ids, (list, tuple)):
                stages[str(stage_name)] = tuple(str(x) for x in ids)
            elif ids:
                stages[str(stage_name)] = (str(ids),)
    return ArtifactContract(enabled=enabled, stages=stages)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
@dataclass
class ArtifactCheck:
    artifact_id: str
    status: str  # "pass" | "missing" | "invalid" | "unknown"
    detail: str = ""
    matched_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class StageReport:
    stage: str
    checks: list[ArtifactCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[ArtifactCheck]:
        return [c for c in self.checks if not c.ok]


@dataclass
class ContractVerifyReport:
    enabled: bool
    stages: list[StageReport] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.stages)

    @property
    def has_failures(self) -> bool:
        return any(not s.passed for s in self.stages)

    @property
    def failure_count(self) -> int:
        return sum(len(s.failures) for s in self.stages)


# -- deterministic validators ------------------------------------------------
def _validate_exists_non_empty(paths: list[Path]) -> tuple[str, str]:
    """Default validator: at least one matched path exists and is non-empty."""

    non_empty = [p for p in paths if p.is_file() and p.stat().st_size > 0]
    if non_empty:
        return "pass", ""
    has_file = any(p.is_file() for p in paths)
    if has_file:
        return "invalid", "matched file(s) are empty"
    return "missing", "no matching non-empty file found"


def _validate_design_doc_frontmatter(paths: list[Path]) -> tuple[str, str]:
    """Richer validator: at least one matched markdown doc carries a non-empty
    YAML frontmatter mapping (``---`` ... ``---`` at the top of the file)."""

    files = [p for p in paths if p.is_file()]
    if not files:
        return "missing", "no matching design document found"
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        front, _body = split_frontmatter(text)
        if front:
            return "pass", ""
    return "invalid", "no matched design document has a non-empty YAML frontmatter block"


_VALIDATORS = {
    "design_doc_frontmatter": _validate_design_doc_frontmatter,
}


def _glob_to_caseless_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob into a case-insensitive regex over POSIX rel-paths.

    Mirrors :meth:`pathlib.Path.glob` segment semantics so the caseless second
    pass matches exactly what the case-sensitive ``Path.glob`` pass would, only
    case-folded: ``*`` matches within a single path segment (never ``/``),
    ``**`` matches any number of segments (including zero), ``?`` matches one
    non-separator char. Anything else is matched literally.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                # ``**/`` matches zero or more leading segments, so ``**/*.md``
                # also matches a top-level file. Consume an optional trailing
                # slash and emit a group that allows the zero-segment case.
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _match_paths(project_root: Path, globs: tuple[str, ...]) -> list[Path]:
    matched: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        # Dedup by resolved path so the case-sensitive and caseless passes never
        # double-count the same file.
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        matched.append(path)

    # Pass 1 — exact, case-sensitive glob (unchanged behavior).
    for pattern in globs:
        for path in project_root.glob(pattern):
            _add(path)

    # Pass 2 — case-INSENSITIVE match over the tree. Required because the
    # catalog globs are lowercase but a correct artifact may be cased
    # differently (e.g. root ``REQUIREMENTS.md``); the deriver discovers such
    # files, so the contract must too. This only ADDS matches that the lowercase
    # pattern would have caught case-insensitively — a genuinely-missing
    # artifact still matches nothing, so true-RED detection is preserved.
    regexes = [_glob_to_caseless_regex(pattern) for pattern in globs]
    for path in project_root.rglob("*"):
        rel = path.relative_to(project_root).as_posix()
        if any(rx.match(rel) for rx in regexes):
            _add(path)
    return matched


def _check_artifact(
    catalog: ArtifactCatalog,
    artifact_id: str,
    project_root: Path,
) -> ArtifactCheck:
    artifact = catalog.get(artifact_id)
    if artifact is None:
        return ArtifactCheck(
            artifact_id=artifact_id,
            status="unknown",
            detail=f"'{artifact_id}' is not a known catalog artifact id",
        )

    paths = _match_paths(project_root, artifact.default_path_globs)
    rel = tuple(_rel(project_root, p) for p in paths)

    if artifact.validator and artifact.validator in _VALIDATORS:
        status, detail = _VALIDATORS[artifact.validator](paths)
    else:
        status, detail = _validate_exists_non_empty(paths)

    return ArtifactCheck(
        artifact_id=artifact_id,
        status=status,
        detail=detail,
        matched_paths=rel,
    )


def _rel(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def verify_contract(
    catalog: ArtifactCatalog,
    contract: ArtifactContract,
    project_root: str | Path,
    stage: str | None = None,
) -> ContractVerifyReport:
    """Deterministically verify required artifacts per declared stage.

    When ``stage`` is given, only that stage is checked. The returned report is
    purely structural; callers (cli.py) decide exit codes — note that an
    *inactive* contract (disabled or no stages) must never cause a non-zero
    exit.
    """

    root = Path(project_root).resolve()
    report = ContractVerifyReport(enabled=contract.enabled)

    stage_names = list(contract.stages)
    if stage is not None:
        stage_names = [s for s in stage_names if s == stage]

    for stage_name in stage_names:
        stage_report = StageReport(stage=stage_name)
        for artifact_id in contract.stages.get(stage_name, ()):  # declared order
            stage_report.checks.append(_check_artifact(catalog, artifact_id, root))
        report.stages.append(stage_report)

    return report


# ---------------------------------------------------------------------------
# Stage completion gate (Phase 3) — the structural keystone
# ---------------------------------------------------------------------------
def enforce_stage_completion(
    project_root: str | Path,
    stage: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> StageReport | None:
    """Gate a single pipeline stage's COMPLETION on its required artifacts.

    Historically a stage "succeeded" merely because its command returned. This
    is the opt-in structural gate that re-defines completion as *the required
    artifacts were actually produced and validate*.

    Behavior (strictly opt-in / non-breaking):

    * If the project has no enabled contract (``artifact_contract.enabled`` is
      false/absent) **or** ``stage`` is not declared in the contract's
      ``stages`` → returns ``None`` (a no-op; the caller changes nothing).
    * Otherwise, runs the existing Phase 1 :func:`verify_contract` for *just*
      that stage and returns its :class:`StageReport`. The caller inspects
      :attr:`StageReport.passed` / :attr:`StageReport.failures` to decide the
      exit code; this function never raises on a failed gate (it only reports).

    This deliberately reuses Phase 1 machinery (``load_contract`` +
    ``verify_contract``); it does not reimplement any verification.
    """

    contract = load_contract(config)
    if not contract.enabled or stage not in contract.stages:
        return None

    catalog = load_catalog()
    report = verify_contract(catalog, contract, project_root, stage=stage)
    for stage_report in report.stages:
        if stage_report.stage == stage:
            return stage_report
    # Declared but somehow absent from the report — treat as a no-op rather than
    # inventing a failure (defensive; should not happen given the guard above).
    return None


# ---------------------------------------------------------------------------
# Rendering (text) — used by `codd contract show`
# ---------------------------------------------------------------------------
def render_catalog(catalog: ArtifactCatalog) -> str:
    lines = [f"Artifact catalog (version {catalog.version}) — {len(catalog.artifacts)} artifact(s):"]
    ssot = [a for a in catalog.artifacts if a.is_ssot]
    derived = [a for a in catalog.artifacts if a.is_derived_view]
    lines.append(f"\nSSOT ({len(ssot)}):")
    for art in ssot:
        lines.append(f"  - {art.id} [produced_by={art.produced_by}]: {art.description}")
    lines.append(f"\nDerived views ({len(derived)}):")
    for art in derived:
        src = ", ".join(art.derived_from)
        lines.append(f"  - {art.id} [produced_by={art.produced_by}] derived_from=[{src}]: {art.description}")
    return "\n".join(lines)


def render_contract(contract: ArtifactContract) -> str:
    if not contract.stages:
        state = "enabled" if contract.enabled else "disabled"
        return f"\nProject artifact_contract: {state}, no stages declared."
    state = "enabled" if contract.enabled else "disabled (opt-in; absent/false = no behavior change)"
    lines = [f"\nProject artifact_contract: {state}"]
    for stage_name, ids in contract.stages.items():
        lines.append(f"  {stage_name}: {', '.join(ids) if ids else '(none)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Suggest (requirement-driven SELECTION) + Adopt (opt-in merge)
# ---------------------------------------------------------------------------
# Phase 2: the catalog ENUMERATES candidate artifacts universally; WHICH ones a
# given project actually uses is SELECTED from that project's requirements and
# existing signals. This mirrors the lexicon `suggest` + opt-in pattern exactly:
# `suggest` is deterministic and read-only (writes only a reviewable proposal
# file, never codd.yaml); `adopt` merges the selection into codd.yaml's
# `artifact_contract:` section, non-destructively and only on explicit request.
#
# Only SSOT / first-class produced artifacts are placed in a stage contract.
# derived_view artifacts are machine-generated (not authored deliverables), so
# they are deliberately excluded from the per-stage contract that verify gates.

DEFAULT_PROPOSAL_FILENAME = "contract_proposal.yaml"


@dataclass(frozen=True)
class ArtifactSuggestion:
    """A single catalog artifact, evaluated against the project's signals."""

    artifact_id: str
    stage: str  # the catalog `produced_by` stage that owns this artifact
    present: bool  # does the project ALREADY have it on disk?
    implied: bool  # do the project's signals say it SHOULD exist?
    matched_paths: tuple[str, ...]  # path globs that matched (if present)
    signal: str  # human-readable rationale for the decision
    # required_artifacts ids (the `category:name` space) this artifact covers
    # in the project's resolvable profile; empty when no profile resolves.
    covers_required_ids: tuple[str, ...] = ()

    @property
    def selected(self) -> bool:
        """Goes into the stage contract iff present or implied."""

        return self.present or self.implied


@dataclass
class ContractProposal:
    """A reviewable, per-stage selection proposed by `codd contract suggest`."""

    suggestions: list[ArtifactSuggestion] = field(default_factory=list)

    @property
    def stages(self) -> dict[str, tuple[str, ...]]:
        """The selected artifacts grouped by their producing stage.

        Stable, catalog-declared ordering; only selected artifacts appear.
        """

        grouped: dict[str, list[str]] = {}
        for suggestion in self.suggestions:
            if not suggestion.selected:
                continue
            grouped.setdefault(suggestion.stage, []).append(suggestion.artifact_id)
        return {stage: tuple(ids) for stage, ids in grouped.items()}

    def to_payload(self) -> dict[str, Any]:
        """Serializable record stored in the proposal file (human-reviewable)."""

        return {
            "stages": {stage: list(ids) for stage, ids in self.stages.items()},
            "artifacts": [
                {
                    "id": s.artifact_id,
                    "stage": s.stage,
                    "present": s.present,
                    "implied": s.implied,
                    "selected": s.selected,
                    "matched_paths": list(s.matched_paths),
                    "signal": s.signal,
                    "covers_required_ids": list(s.covers_required_ids),
                }
                for s in self.suggestions
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContractProposal":
        proposal = cls()
        raw = payload.get("artifacts") if isinstance(payload, Mapping) else None
        if isinstance(raw, list):
            for record in raw:
                if not isinstance(record, Mapping):
                    continue
                artifact_id = str(record.get("id") or "").strip()
                if not artifact_id:
                    continue
                proposal.suggestions.append(
                    ArtifactSuggestion(
                        artifact_id=artifact_id,
                        stage=str(record.get("stage") or ""),
                        present=bool(record.get("present", False)),
                        implied=bool(record.get("implied", False)),
                        matched_paths=tuple(str(p) for p in (record.get("matched_paths") or [])),
                        signal=str(record.get("signal") or ""),
                        covers_required_ids=tuple(
                            str(r) for r in (record.get("covers_required_ids") or [])
                        ),
                    )
                )
        return proposal


def _operation_flow_declared(codd_config: Mapping[str, Any] | None) -> bool:
    """True iff codd.yaml declares at least one operation_flow operation."""

    if not isinstance(codd_config, Mapping):
        return False
    flow = codd_config.get("operation_flow")
    if not isinstance(flow, Mapping):
        return False
    operations = flow.get("operations")
    return isinstance(operations, (list, tuple)) and len(operations) > 0


def suggest_contract(
    catalog: ArtifactCatalog,
    project_root: str | Path,
    codd_config: Mapping[str, Any] | None = None,
    *,
    requirement_docs: tuple[str, ...] = (),
) -> ContractProposal:
    """Deterministically SELECT which catalog artifacts this project uses.

    For every first-class (SSOT) catalog artifact we evaluate two independent,
    fully deterministic facts:

    * ``present``  — at least one of the artifact's ``default_path_globs`` matches
      a real file in the project tree (reuses the same glob machinery the verify
      gate uses, so suggest and verify agree on what "exists" means).
    * ``implied``  — a project signal says the artifact SHOULD exist:
        - ``requirements``   : requirement docs discovered (``requirement_docs``;
          the caller passes ``discover_requirement_docs`` results) OR globs match.
        - ``operation_flow`` : codd.yaml declares operation_flow.operations.
        - everything else    : implied == present (the file's existence is its
          own signal; no extra heuristic is invented).

    An artifact is SELECTED into the stage contract iff ``present or implied``.
    derived_view artifacts are NEVER selected — they are machine-generated, not
    authored deliverables, so the per-stage contract (which verify gates) only
    targets authored/produced artifacts. Each artifact is mapped to the pipeline
    stage named by its catalog ``produced_by``.

    When the project's required-artifacts profile is resolvable, each suggestion
    also records ``covers_required_ids``: the profile's `category:name` ids the
    suggested catalog artifact covers (via the catalog's cross-space mapping),
    so a reviewer can see how the contract selection reconciles with the
    required-artifacts plan/wave flow. Unresolvable profile ⇒ empty (fail-open).

    Pure / read-only: matches the tree and inspects config; writes nothing.
    """

    root = Path(project_root).resolve()
    req_docs = tuple(str(d) for d in requirement_docs)
    proposal = ContractProposal()

    # Best-effort profile resolution for the covers annotation (lazy import:
    # codd.artifact_ids imports this module, so importing it at module scope
    # would be circular).
    profile_ids: frozenset[str] = frozenset()
    try:
        from codd.artifact_ids import profile_required_ids_for_project

        profile_ids = frozenset(profile_required_ids_for_project(root, codd_config))
    except Exception:
        profile_ids = frozenset()

    for artifact in catalog.artifacts:
        # derived_view artifacts are machine-generated; never authored ⇒ excluded.
        if not artifact.is_ssot:
            continue

        paths = _match_paths(root, artifact.default_path_globs)
        rel = tuple(_rel(root, p) for p in paths if p.is_file())
        present = bool(rel)

        implied = present
        signal_parts: list[str] = []

        if artifact.id == "requirements":
            if req_docs:
                implied = True
                signal_parts.append(f"{len(req_docs)} requirement doc(s) discovered")
        elif artifact.id == "operation_flow":
            # codd.yaml always matches its glob, so existence alone is meaningless
            # here; the real signal is whether operations are actually declared.
            declared = _operation_flow_declared(codd_config)
            present = declared
            implied = declared
            rel = rel if declared else ()
            signal_parts.append(
                "operation_flow.operations declared in codd.yaml"
                if declared
                else "no operation_flow.operations declared in codd.yaml"
            )

        if present and rel:
            signal_parts.append(f"present: matched {', '.join(rel)}")
        elif not present:
            globs = ", ".join(artifact.default_path_globs) or "(none)"
            signal_parts.append(f"absent: no file matched {globs}")

        proposal.suggestions.append(
            ArtifactSuggestion(
                artifact_id=artifact.id,
                stage=artifact.produced_by,
                present=present,
                implied=implied,
                matched_paths=rel,
                signal="; ".join(signal_parts),
                covers_required_ids=tuple(
                    rid for rid in artifact.required_artifact_ids if rid in profile_ids
                ),
            )
        )

    return proposal


def proposal_path(codd_dir: str | Path, output: str | None = None) -> Path:
    """Resolve where the contract proposal file lives (under the codd dir)."""

    if output:
        candidate = Path(output)
        return candidate if candidate.is_absolute() else Path(codd_dir) / candidate
    return Path(codd_dir) / DEFAULT_PROPOSAL_FILENAME


def write_proposal(path: str | Path, proposal: ContractProposal) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(proposal.to_payload(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_proposal(path: str | Path) -> ContractProposal:
    path = Path(path)
    if not path.exists():
        return ContractProposal()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        return ContractProposal()
    return ContractProposal.from_payload(payload)


# -- adopt (merge proposal stages into codd.yaml) ----------------------------
@dataclass
class AdoptPlan:
    """What :func:`plan_adopt` would write into codd.yaml's artifact_contract."""

    # stage -> ordered (existing kept + new appended) artifact ids
    merged_stages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # stage -> only the newly-added ids (for human-readable diff)
    added: dict[str, tuple[str, ...]] = field(default_factory=dict)
    enable: bool = False
    currently_enabled: bool = False

    @property
    def has_changes(self) -> bool:
        if any(self.added.values()):
            return True
        # flipping enabled on counts as a change too.
        return self.enable and not self.currently_enabled


def _existing_contract_stages(codd_config: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Read the existing artifact_contract.stages as plain ordered lists."""

    stages: dict[str, list[str]] = {}
    if not isinstance(codd_config, Mapping):
        return stages
    section = codd_config.get(CONTRACT_KEY)
    if not isinstance(section, Mapping):
        return stages
    raw = section.get("stages")
    if isinstance(raw, Mapping):
        for stage_name, ids in raw.items():
            if isinstance(ids, (list, tuple)):
                stages[str(stage_name)] = [str(x) for x in ids]
            elif ids:
                stages[str(stage_name)] = [str(ids)]
    return stages


def plan_adopt(
    proposal: ContractProposal,
    codd_config: Mapping[str, Any] | None,
    *,
    enable: bool = False,
) -> AdoptPlan:
    """Compute the idempotent, non-destructive merge of the proposal.

    Existing contract entries are PRESERVED (kept in their existing order); each
    stage gains only the proposed ids it does not already contain, appended in
    the proposal's stable order. Re-running with an unchanged tree is a no-op.
    """

    existing = _existing_contract_stages(codd_config)
    currently_enabled = False
    if isinstance(codd_config, Mapping):
        section = codd_config.get(CONTRACT_KEY)
        if isinstance(section, Mapping):
            currently_enabled = bool(section.get("enabled", False))

    plan = AdoptPlan(enable=enable, currently_enabled=currently_enabled)
    proposed = proposal.stages

    # Start from existing stages so nothing is dropped; preserve their order.
    merged: dict[str, list[str]] = {stage: list(ids) for stage, ids in existing.items()}
    added: dict[str, list[str]] = {}

    for stage, ids in proposed.items():
        current = merged.setdefault(stage, [])
        for artifact_id in ids:
            if artifact_id not in current:
                current.append(artifact_id)
                added.setdefault(stage, []).append(artifact_id)

    plan.merged_stages = {stage: tuple(ids) for stage, ids in merged.items()}
    plan.added = {stage: tuple(ids) for stage, ids in added.items()}
    return plan


def merge_into_codd_yaml(codd_yaml_path: str | Path, plan: AdoptPlan) -> int:
    """Write the plan's merged artifact_contract into codd.yaml.

    Non-destructive: only the ``artifact_contract`` section is touched (created
    if absent); ``enabled`` is left as-is unless ``plan.enable`` is set, in which
    case it is set to ``True`` (opt-in is never silently flipped). Returns the
    number of newly-added artifact ids across all stages. Writes only when the
    plan has changes.
    """

    if not plan.has_changes:
        return 0

    path = Path(codd_yaml_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    section = payload.get(CONTRACT_KEY)
    if not isinstance(section, dict):
        section = {}
        payload[CONTRACT_KEY] = section

    if plan.enable:
        section["enabled"] = True
    elif "enabled" not in section:
        # Preserve opt-in: adoption stays inert until the user enables it.
        section["enabled"] = False

    section["stages"] = {stage: list(ids) for stage, ids in plan.merged_stages.items()}

    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return sum(len(ids) for ids in plan.added.values())


# -- rendering (no click) ----------------------------------------------------
def render_suggestion(proposal: ContractProposal) -> str:
    """Readable summary of a `codd contract suggest` proposal."""

    lines = ["Contract suggestion (requirement-driven artifact selection):"]
    selected_stages = proposal.stages
    if selected_stages:
        lines.append("\nSelected per stage:")
        for stage, ids in selected_stages.items():
            lines.append(f"  {stage}: {', '.join(ids)}")
    else:
        lines.append("\nSelected per stage: (none — no artifacts present or implied)")

    lines.append("\nArtifact detection:")
    for s in proposal.suggestions:
        mark = "SELECT" if s.selected else "  skip"
        flags = f"present={str(s.present).lower()} implied={str(s.implied).lower()}"
        lines.append(f"  [{mark}] {s.artifact_id} (stage={s.stage}) {flags}")
        if s.signal:
            lines.append(f"          {s.signal}")
        if s.covers_required_ids:
            lines.append(
                f"          covers required_artifacts: {', '.join(s.covers_required_ids)}"
            )
    return "\n".join(lines)


def render_adopt(plan: AdoptPlan, *, project_display: str = "codd.yaml") -> str:
    """Readable diff of what `codd contract adopt` would write."""

    lines: list[str] = []
    if not plan.has_changes:
        lines.append("No changes: artifact_contract already up to date.")
        if plan.enable and plan.currently_enabled:
            lines.append("  (already enabled)")
        return "\n".join(lines)

    lines.append(f"artifact_contract changes for {project_display}:")
    for stage, ids in plan.added.items():
        if ids:
            lines.append(f"  {stage}: + {', '.join(ids)}")
    if not any(plan.added.values()):
        lines.append("  (no new stage entries)")
    if plan.enable and not plan.currently_enabled:
        lines.append("  enabled: false -> true")
    elif not plan.enable:
        state = "true" if plan.currently_enabled else "false"
        lines.append(f"  enabled: {state} (unchanged; pass --enable to turn on)")
    return "\n".join(lines)

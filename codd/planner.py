"""CoDD planner — compute wave readiness from configured artifacts."""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

import codd.generator as generator_module
from codd.generator import (
    WaveArtifact,
    _load_project_config,
    _load_wave_artifacts,
    _resolve_generation_capabilities,
)
from codd.project_types import ProjectCapabilities
from codd.validator import _iter_doc_files, _parse_codd_frontmatter, validate_project


STATUS_DONE = "DONE"
STATUS_READY = "READY"
STATUS_BLOCKED = "BLOCKED"
STATUS_ERROR = "ERROR"

ICON_BY_STATUS = {
    STATUS_DONE: "✅",
    STATUS_READY: "🔵",
    STATUS_BLOCKED: "🔴",
    STATUS_ERROR: "⚠️",
}

MECE_DOCUMENT_STRUCTURE = """\
docs/
├── requirements/      # What  — source-of-truth requirements
├── design/            # How   — overview architecture and cross-cutting design
├── detailed_design/   # How   — module ownership, flows, and implementation-ready diagrams
├── plan/              # When  — implementation sequencing and milestones
├── governance/        # Why   — decisions, ADRs, change requests
├── test/              # Verify — acceptance criteria and test strategy
├── operations/        # Run   — runbooks, monitoring, incident handling
└── infra/             # Build — infrastructure, CI/CD, build and deployment setup
"""

STANDARD_V_MODEL_PATTERNS = """\
Typical wave patterns:
- Wave 1: acceptance criteria and decision records derived directly from requirements
- Wave 2: overview/system design that depends on requirements and wave 1 outputs
- Wave 3-4: domain design such as API, database, auth, UX, infrastructure/CI/CD, and integration design
- Wave 5: detailed design artifacts under docs/detailed_design/ with Mermaid diagrams, ownership boundaries, and runtime flows
- Wave 6: implementation planning and infrastructure/build setup that depend on the approved overview + detailed design set
- Baseline: after all waves are done and code is implemented, run codd extract to capture a factual snapshot of the codebase — this serves as the baseline for drift detection during maintenance
"""


def _standard_v_model_patterns(capabilities: ProjectCapabilities | None = None) -> str:
    """Return the V-model wave patterns, dropping the UX domain for non-UI types.

    For UI projects (or the backward-compatible default), the domain-design wave
    includes ``UX`` exactly as before. For non-UI projects (``user_interface``
    False) UX is removed from the mandatory domains so a CLI/library/service
    project is not pushed toward UI design artifacts.
    """

    if capabilities is None:
        capabilities = generator_module.WEB_FALLBACK_CAPABILITIES
    if capabilities.user_interface:
        domains = "API, database, auth, UX, infrastructure/CI/CD, and integration design"
    else:
        domains = "API, database, auth, infrastructure/CI/CD, and integration design"
    return (
        "Typical wave patterns:\n"
        "- Wave 1: acceptance criteria and decision records derived directly from requirements\n"
        "- Wave 2: overview/system design that depends on requirements and wave 1 outputs\n"
        f"- Wave 3-4: domain design such as {domains}\n"
        "- Wave 5: detailed design artifacts under docs/detailed_design/ with Mermaid diagrams, ownership boundaries, and runtime flows\n"
        "- Wave 6: implementation planning and infrastructure/build setup that depend on the approved overview + detailed design set\n"
        "- Baseline: after all waves are done and code is implemented, run codd extract to capture a factual snapshot of the codebase — this serves as the baseline for drift detection during maintenance\n"
    )


@dataclass(frozen=True)
class PlannedArtifact:
    """Planner view of one wave-configured artifact."""

    wave: int
    node_id: str
    path: str
    status: str
    depends_on: list[str]
    blocked_by: list[str]
    validation_errors: list[str]


@dataclass(frozen=True)
class PlannedWave:
    """Planner view of one wave."""

    wave: int
    status: str
    nodes: list[PlannedArtifact]


@dataclass(frozen=True)
class PlanResult:
    """Serializable planner output."""

    project_root: str
    summary: dict[str, int]
    next_wave: int | None
    waves: list[PlannedWave]
    baseline_status: str = STATUS_BLOCKED  # extract baseline: DONE/READY/BLOCKED


@dataclass(frozen=True)
class RequirementDocument:
    """Requirement document used to synthesize wave_config."""

    node_id: str
    path: str
    content: str


@dataclass(frozen=True)
class ExtractedDocument:
    """Extracted document used as brownfield context for wave_config synthesis."""

    node_id: str
    path: str
    content: str


@dataclass(frozen=True)
class PlanInitResult:
    """Result of initializing wave_config from requirements."""

    project_root: str
    config_path: str
    requirement_paths: list[str]
    wave_config: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class _ExternalNode:
    path: str
    status: str


def generate_wave_config_from_artifacts(
    required_artifacts: list[dict[str, Any]],
    existing_wave_config: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Generate CoDD wave_config from lexicon required_artifacts.

    When existing_wave_config is provided, entries already present there are
    preserved byte-for-byte in append mode. Only missing required artifacts are
    appended after the last existing wave.
    """
    if not required_artifacts:
        return deepcopy(existing_wave_config) if existing_wave_config is not None else {}

    artifacts_by_id = _index_required_artifacts(required_artifacts)
    existing = deepcopy(existing_wave_config) if existing_wave_config is not None else None
    existing_node_waves = _index_wave_config_nodes(existing or {})
    missing_ids = [artifact_id for artifact_id in artifacts_by_id if artifact_id not in existing_node_waves]

    if existing is not None and not missing_ids:
        return existing

    selected_ids = missing_ids if existing is not None else list(artifacts_by_id)
    selected_artifacts = [artifacts_by_id[artifact_id] for artifact_id in selected_ids]
    relative_waves = _assign_required_artifact_waves(selected_artifacts, artifacts_by_id)

    if existing is None:
        base_wave = 0
        wave_config: dict[str, list[dict[str, Any]]] = {}
    else:
        base_wave = _max_wave_number(existing)
        wave_config = existing

    for artifact in selected_artifacts:
        wave = base_wave + relative_waves[artifact["id"]]
        wave_key = str(wave)
        wave_config.setdefault(wave_key, []).append(_required_artifact_to_wave_entry(artifact))

    return {
        key: wave_config[key]
        for key in sorted(wave_config, key=_wave_key_sort_value)
    }


def backup_codd_yaml(project_root: Path) -> Path:
    """Copy the project codd.yaml to codd.yaml.bak when it exists."""
    from codd.config import find_codd_dir

    project_root = Path(project_root).resolve()
    codd_dir = find_codd_dir(project_root)
    src = codd_dir / "codd.yaml" if codd_dir else project_root / "codd.yaml"
    bak = src.with_name("codd.yaml.bak")
    if src.exists():
        shutil.copy2(src, bak)
    return bak


def _index_required_artifacts(required_artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for raw_artifact in required_artifacts:
        if not isinstance(raw_artifact, dict):
            raise ValueError("required_artifacts entries must be mappings")
        artifact = deepcopy(raw_artifact)
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError("required_artifacts entries must include id")
        artifact_id = artifact_id.strip()
        if artifact_id in artifacts_by_id:
            raise ValueError(f"duplicate required artifact id: {artifact_id}")
        artifact["id"] = artifact_id
        artifacts_by_id[artifact_id] = artifact
    return artifacts_by_id


def _assign_required_artifact_waves(
    artifacts: list[dict[str, Any]],
    artifacts_by_id: dict[str, dict[str, Any]],
) -> dict[str, int]:
    selected_ids = {artifact["id"] for artifact in artifacts}
    indegree = {artifact["id"]: 0 for artifact in artifacts}
    adjacency = {artifact["id"]: set() for artifact in artifacts}
    input_order = {artifact["id"]: index for index, artifact in enumerate(artifacts)}

    for artifact in artifacts:
        artifact_id = artifact["id"]
        for dependency_id in _required_artifact_dependency_ids(artifact):
            if dependency_id not in selected_ids:
                continue
            if artifact_id in adjacency[dependency_id]:
                continue
            adjacency[dependency_id].add(artifact_id)
            indegree[artifact_id] += 1

    ready = sorted(
        [artifact_id for artifact_id, degree in indegree.items() if degree == 0],
        key=lambda artifact_id: (input_order[artifact_id], artifact_id),
    )
    order: list[str] = []
    while ready:
        artifact_id = ready.pop(0)
        order.append(artifact_id)
        for child_id in sorted(adjacency[artifact_id], key=lambda item: (input_order[item], item)):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
        ready.sort(key=lambda item: (input_order[item], item))

    if len(order) != len(artifacts):
        cycle_nodes = sorted(artifact_id for artifact_id, degree in indegree.items() if degree > 0)
        raise ValueError(f"required_artifacts contains a dependency cycle: {', '.join(cycle_nodes)}")

    waves: dict[str, int] = {}
    for artifact_id in order:
        dependency_waves = [
            waves[dependency_id]
            for dependency_id in _required_artifact_dependency_ids(artifacts_by_id[artifact_id])
            if dependency_id in waves
        ]
        waves[artifact_id] = (max(dependency_waves) + 1) if dependency_waves else 1
    return waves


def _required_artifact_dependency_ids(artifact: dict[str, Any]) -> list[str]:
    dependencies = artifact.get("depends_on", [])
    if not dependencies:
        return []
    if not isinstance(dependencies, list):
        raise ValueError("required_artifacts depends_on must be a list")

    dependency_ids: list[str] = []
    for dependency in dependencies:
        if isinstance(dependency, str):
            dependency_id = dependency
        elif isinstance(dependency, dict):
            dependency_id = dependency.get("id", "")
        else:
            raise ValueError("required_artifacts depends_on entries must be strings or mappings")
        dependency_id = str(dependency_id).strip()
        if dependency_id:
            dependency_ids.append(dependency_id)
    return dependency_ids


def _required_artifact_to_wave_entry(artifact: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "node_id": artifact["id"],
        "output": _required_artifact_output_path(artifact),
        "title": str(artifact.get("title") or artifact["id"]),
    }

    dependencies = [
        {"id": dependency_id, "relation": "depends_on"}
        for dependency_id in _required_artifact_dependency_ids(artifact)
    ]
    if dependencies:
        entry["depends_on"] = dependencies

    conventions = artifact.get("conventions")
    if isinstance(conventions, list) and conventions:
        entry["conventions"] = deepcopy(conventions)

    modules = artifact.get("modules")
    if isinstance(modules, list) and modules:
        entry["modules"] = [str(module) for module in modules if str(module).strip()]

    return entry


def _required_artifact_output_path(artifact: dict[str, Any]) -> str:
    configured_output = artifact.get("output")
    if isinstance(configured_output, str) and configured_output.strip():
        return configured_output.strip()

    node_id = str(artifact["id"])
    category, _, name = node_id.partition(":")
    filename_source = name or category or "artifact"
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", filename_source).strip("_-").lower() or "artifact"
    directory = {
        "requirements": "docs/requirements",
        "req": "docs/requirements",
        "design": "docs/design",
        "detailed_design": "docs/detailed_design",
        "detail": "docs/detailed_design",
        "plan": "docs/plan",
        "governance": "docs/governance",
        "test": "docs/test",
        "operations": "docs/operations",
        "infra": "docs/infra",
    }.get(category, "docs/design")
    return f"{directory}/{filename}.md"


def _index_wave_config_nodes(wave_config: dict[str, Any]) -> dict[str, int]:
    node_waves: dict[str, int] = {}
    for wave_key, entries in wave_config.items():
        wave_number = _parse_wave_number(wave_key)
        if wave_number is None or not isinstance(entries, list):
            continue
        for entry in entries:
            node_id: str | None = None
            if isinstance(entry, dict) and isinstance(entry.get("node_id"), str):
                node_id = entry["node_id"]
            elif isinstance(entry, str):
                node_id = entry
            if node_id:
                node_waves[node_id] = wave_number
    return node_waves


def _max_wave_number(wave_config: dict[str, Any]) -> int:
    wave_numbers = [
        wave_number
        for wave_key in wave_config
        if (wave_number := _parse_wave_number(wave_key)) is not None
    ]
    return max(wave_numbers, default=0)


def _parse_wave_number(wave_key: Any) -> int | None:
    if isinstance(wave_key, int):
        return wave_key
    text = str(wave_key)
    match = re.fullmatch(r"(?:wave_)?(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _wave_key_sort_value(wave_key: str) -> tuple[int, str]:
    wave_number = _parse_wave_number(wave_key)
    return (wave_number if wave_number is not None else 10**9, str(wave_key))


def plan_init(
    project_root: Path,
    *,
    force: bool = False,
    ai_command: str | None = None,
) -> PlanInitResult:
    """Initialize wave_config from requirement or extracted documents."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)

    if config.get("wave_config") and not force:
        raise FileExistsError("codd.yaml already contains wave_config")

    _ensure_lexicon(project_root)

    requirement_documents = _load_requirement_documents(project_root, config)
    extracted_documents: list[ExtractedDocument] = []
    capabilities = _resolve_generation_capabilities(config, project_root)

    if requirement_documents:
        # Greenfield: use requirements
        resolved_ai_command = generator_module._resolve_ai_command(config, ai_command, command_name="plan_init")
        prompt = _build_plan_init_prompt(config, requirement_documents, capabilities)
    else:
        # Brownfield: try extracted docs
        extracted_documents = _load_extracted_documents(project_root, config)
        if not extracted_documents:
            raise ValueError(
                "no requirement documents or extracted documents found. "
                "Run 'codd extract' first for brownfield projects, "
                "or create requirement docs with CoDD frontmatter for greenfield projects."
            )
        resolved_ai_command = generator_module._resolve_ai_command(config, ai_command, command_name="plan_init")
        prompt = _build_brownfield_plan_init_prompt(config, extracted_documents, capabilities)

    prompt = generator_module._inject_lexicon(prompt, project_root)
    wave_config = _invoke_and_parse_wave_config(
        resolved_ai_command, prompt, max_retries=_plan_init_max_retries(config)
    )
    if requirement_documents:
        # Greenfield: GUARANTEE the canonical VB registry doc is planned so the VB
        # coverage/authenticity SSOT is never silently absent (false-GREEN: 0/0 pass).
        wave_config = _ensure_canonical_vb_doc_planned(
            config, wave_config, requirement_documents
        )

    from codd.config import find_codd_dir
    config["wave_config"] = wave_config
    _set_canonical_vb_doc_config(config, wave_config)
    codd_dir = find_codd_dir(project_root)
    config_path = (codd_dir or project_root / "codd") / "codd.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return PlanInitResult(
        project_root=str(project_root),
        config_path=str(config_path),
        requirement_paths=[d.path for d in requirement_documents] or [d.path for d in extracted_documents],
        wave_config=wave_config,
    )


def _set_canonical_vb_doc_config(
    config: dict[str, Any],
    wave_config: dict[str, Any],
) -> None:
    """Pin the canonical VB declaration doc in ``test_coverage.docs`` (greenfield).

    When the planned waves include the canonical VB document
    (``test:test-strategy`` / ``docs/test/test_strategy.md``), record it as the
    explicit VB-audit source so the canonical declarer is unambiguous. This is
    a clarity aid (the audit auto-discovers ``docs/test/**/*.md`` otherwise); it
    is skipped if the user has already configured ``test_coverage.docs`` so we
    never clobber an explicit choice. Generic: it keys off the standard CoDD
    canonical naming, not any one project's specifics.
    """

    from codd.verifiable_behavior_audit import is_canonical_vb_doc

    section = config.get("test_coverage")
    if isinstance(section, dict) and section.get("docs"):
        return  # respect an explicit configuration

    canonical_output: str | None = None
    for entries in wave_config.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = entry.get("node_id") or entry.get("id")
            output = entry.get("output")
            if is_canonical_vb_doc(
                node_id=node_id if isinstance(node_id, str) else None,
                output_path=output if isinstance(output, str) else None,
            ):
                if isinstance(output, str) and output.strip():
                    canonical_output = output.strip()
                break
        if canonical_output:
            break

    if not canonical_output:
        return
    if not isinstance(section, dict):
        section = {}
    section["docs"] = [canonical_output]
    config["test_coverage"] = section


def _ensure_canonical_vb_doc_planned(
    config: dict[str, Any],
    wave_config: dict[str, Any],
    requirement_documents: list[RequirementDocument],
) -> dict[str, Any]:
    """GUARANTEE the canonical VB registry doc is planned (greenfield VB SSOT).

    The AI's wave_config can omit ``test:test-strategy`` / ``docs/test/test_strategy.md``
    non-deterministically; without it the VB audit has ZERO declarations and the coverage
    gate trivially passes 0/0 — a false-GREEN (the requirements' behaviors are never
    certified). When the canonical doc is absent — and the owner has NOT pinned an explicit
    ``test_coverage.docs`` — append it as a FINAL wave (so existing wave order/dependencies
    are untouched) depending on the requirement docs + every already-planned artifact, so it
    is generated last with full context. ``_set_canonical_vb_doc_config`` then detects+pins
    it as usual (this stays the force-INJECT step; that one stays detection-only). Generic:
    keys off the canonical naming, not any project's specifics.
    """

    from codd.verifiable_behavior_audit import (
        coverage_gate_enabled,
        wave_config_plans_canonical_vb_doc,
    )

    if not coverage_gate_enabled(config):
        return wave_config  # coverage gate off (owner opt-out) — no VB SSOT needed
    if wave_config_plans_canonical_vb_doc({"wave_config": wave_config}):
        return wave_config  # the AI already planned it — nothing to do
    section = config.get("test_coverage")
    if isinstance(section, dict) and section.get("docs"):
        return wave_config  # respect an explicit owner-pinned VB doc

    out = deepcopy(wave_config)
    new_wave = str(_max_wave_number(out) + 1)
    existing_node_ids: list[str] = []
    modules: set[str] = set()
    for entries in out.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = entry.get("node_id") or entry.get("id")
            if isinstance(node_id, str) and node_id.strip():
                existing_node_ids.append(node_id.strip())
            for module in entry.get("modules") or []:
                if isinstance(module, str) and module.strip():
                    modules.add(module.strip())

    depends_on: list[dict[str, str]] = [
        {"id": doc.node_id, "relation": "derives_from", "semantic": "governance"}
        for doc in requirement_documents
        if getattr(doc, "node_id", None)
    ]
    depends_on.extend(
        {"id": node_id, "relation": "depends_on", "semantic": "verification"}
        for node_id in existing_node_ids
    )

    out.setdefault(new_wave, [])
    if isinstance(out[new_wave], list):
        out[new_wave].append(
            {
                "node_id": "test:test-strategy",
                "output": "docs/test/test_strategy.md",
                "title": "Test Strategy and Verifiable Behavior Registry",
                "depends_on": depends_on,
                "conventions": [],
                "modules": sorted(modules),
            }
        )
    return {key: out[key] for key in sorted(out, key=_wave_key_sort_value)}


@dataclass(frozen=True)
class VBCoverageClosureTask:
    """A synthesized cross-cutting test-authoring task that OWNS the residual VBs.

    The residual set is the verifiable behaviors the canonical registry DECLARES
    but that no derived task's declared test outputs can claim (see
    :func:`synthesize_vb_coverage_closure_task`). This is the plan-stage projection
    the greenfield pipeline turns into an ``ImplementTaskRef`` (it deliberately does
    NOT import the pipeline's task type — keeps the planner a pure data producer,
    mirroring :func:`_ensure_canonical_vb_doc_planned`).
    """

    task_id: str
    design_node: str
    expected_outputs: tuple[str, ...]
    title: str
    description: str
    owned_vb_ids: tuple[str, ...]
    test_kinds: tuple[str, ...] = ()


#: Doc-shaped extensions: a declared output ending in one of these is a DOCUMENT
#: (a design/registry doc), never an authored TEST file — so a task whose only
#: "test-shaped" output is such a doc authors no covering test. This is the same
#: distinction the greenfield pipeline's ``_task_declares_no_authored_artifact``
#: draws (a doc under ``docs/`` is a non-codebase artifact), kept local so the
#: planner stays import-clean of the pipeline.
_VB_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})

_VB_TEST_FILENAME_RE = re.compile(
    r"(^test_|_test|\.spec\.|\.test\.|\.e2e\.|\.e2e-spec\.|\.cy\.)", re.IGNORECASE
)

#: A path-like token inside a VB registry row's declared-owner column: a slashed
#: path with a file extension (globs allowed). Used only to read the registry's
#: OWN declared owner-test-file column — never to (re-)parse VB declarations.
_VB_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./*\-]*\.[A-Za-z0-9_*]+")


def _vb_norm_path(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("`").strip("/")


def _vb_test_dir_prefixes(config: dict[str, Any] | None) -> list[str]:
    raw = ((config or {}).get("scan") or {}).get("test_dirs")
    dirs = [str(item) for item in raw] if isinstance(raw, list) and raw else ["tests/"]
    return [_vb_norm_path(item) for item in dirs if _vb_norm_path(item)]


def _is_concrete_test_file(rel: str, *, test_prefixes: list[str]) -> bool:
    """A CONCRETE authored test file: a non-glob path under a test dir (or with a
    test-shaped filename) whose extension is a code extension, not a document."""
    if not rel or any(ch in rel for ch in "*?["):
        return False  # a glob is not a single file to match against
    suffix = PurePosixPath(rel).suffix.lower()
    if not suffix or suffix in _VB_DOC_EXTENSIONS:
        return False
    under_test = any(rel == prefix or rel.startswith(prefix + "/") for prefix in test_prefixes)
    if under_test:
        return True
    return bool(_VB_TEST_FILENAME_RE.search(PurePosixPath(rel).name))


def _vb_authored_test_files(task_expected_outputs: Any, test_prefixes: list[str]) -> set[str]:
    """The concrete test files any derived task DECLARES it authors."""
    authored: set[str] = set()
    for outputs in task_expected_outputs or ():
        for out in outputs or ():
            rel = _vb_norm_path(str(out))
            if _is_concrete_test_file(rel, test_prefixes=test_prefixes):
                authored.add(rel)
    return authored


def _vb_owner_test_tokens(text: str, test_prefixes: list[str]) -> tuple[list[str], bool]:
    """Read a VB row's DECLARED owner test file(s) from its own text.

    Returns ``(concrete_owner_files, has_owner_intent)``. ``has_owner_intent`` is
    True when the row names ANY owning test file — concrete OR a suite-level glob
    (e.g. ``tests/e2e/*.e2e.test.ts``). A glob names an owner intent that no single
    authored file can satisfy, so it contributes to residual without a concrete
    match. A row that names NO owner test file returns ``([], False)`` — we then
    treat the VB as vacuously claimable (never force a task for a VB we cannot
    prove is orphaned; owner-column-less registries stay untouched).
    """
    concrete: list[str] = []
    has_owner = False
    for match in _VB_PATH_TOKEN_RE.finditer(text or ""):
        tok = _vb_norm_path(match.group(0))
        if "/" not in tok:
            continue
        probe = tok.replace("*", "x").replace("?", "x")
        suffix = PurePosixPath(probe).suffix.lower()
        if not suffix or suffix in _VB_DOC_EXTENSIONS:
            continue
        under_test = any(probe == prefix or probe.startswith(prefix + "/") for prefix in test_prefixes)
        test_shaped = bool(_VB_TEST_FILENAME_RE.search(PurePosixPath(probe).name))
        if not (under_test or test_shaped):
            continue
        has_owner = True
        if not any(ch in tok for ch in "*?["):
            if tok not in concrete:
                concrete.append(tok)
    return concrete, has_owner


def synthesize_vb_coverage_closure_task(
    behaviors: Any,
    task_expected_outputs: Any,
    *,
    config: dict[str, Any] | None = None,
) -> "VBCoverageClosureTask | None":
    """Synthesize ONE test-authoring task owning the RESIDUAL verifiable behaviors.

    Extends :func:`_ensure_canonical_vb_doc_planned` by one level. That function
    guarantees the VB registry DOCUMENT is planned (so behaviors get DECLARED);
    this guarantees every declared behavior is CLAIMABLE by some derived task.

    A behavior is *claimable* when the registry row's declared owner test file(s)
    include a file some derived task authors (its ``expected_outputs`` contain that
    test file). The RESIDUAL set is the behaviors the registry declares an owner
    test file for, that NO derived task authors — the cross-cutting suite-level /
    static-source / universally-quantified invariants that map to no single module,
    so the module-scoped derivation never gave them an owning test-authoring task
    (the 2026-07 S3 StockRoom-mini burn: 10 of 41 VBs left with no owning task, the
    post-implement coverage gate honest-stopping because no task could emit their
    marker). For the residual set this returns a single cross-cutting task that owns
    exactly them and authors their declared owner test files; its prompt carries the
    residual VB rows + the standard covering-marker contract so the implementer emits
    ``codd: covers vb=<id>`` for each. Returns ``None`` when there is no residual —
    generality: an all-claimable project (or one whose registry declares no owner
    test files at all) synthesizes nothing.

    GENERIC: keys off the VB-registry rows (the canonical VB-parse path supplies
    ``behaviors``) + declared test outputs. No ``language ==``, no framework/domain
    literal, no dependency on any project-specific spec section or formatting; a
    project whose declared behaviors all map to a module produces no task.
    """

    from codd.verifiable_behavior_audit import coverage_gate_enabled, format_gap_feedback

    if not coverage_gate_enabled(config):
        return None

    behaviors = list(behaviors or [])
    if not behaviors:
        return None

    test_prefixes = _vb_test_dir_prefixes(config)
    authored = _vb_authored_test_files(task_expected_outputs, test_prefixes)

    residual: list[Any] = []
    owner_files: list[str] = []
    for behavior in behaviors:
        text = f"{getattr(behavior, 'description', '') or ''} {getattr(behavior, 'declared_scenarios', '') or ''}"
        concrete_owners, has_owner = _vb_owner_test_tokens(text, test_prefixes)
        if not has_owner:
            continue  # no declared owner test file → vacuously claimable
        if any(owner in authored for owner in concrete_owners):
            continue  # an owner test file is authored by some task → claimable
        residual.append(behavior)
        for owner in concrete_owners:
            if owner not in owner_files:
                owner_files.append(owner)

    if not residual:
        return None

    if not owner_files:
        # Every residual VB declared only a suite-level glob owner (no concrete file
        # to author against). Borrow the project's own test-file extension so the
        # closure task still has a concrete write target — language-agnostic (the
        # extension comes from the project's authored tests, never a literal).
        borrowed = sorted(authored)
        suffix = PurePosixPath(borrowed[0]).suffix if borrowed else ""
        if not suffix:
            return None  # cannot determine a concrete test file — leave to the gate
        prefix = test_prefixes[0] if test_prefixes else "tests"
        owner_files = [f"{prefix}/verifiable_behavior_coverage{suffix}"]

    source_doc = next(
        (getattr(b, "source_doc", "") for b in residual if getattr(b, "source_doc", "")),
        "docs/test/test_strategy.md",
    )
    owned_ids = tuple(str(getattr(b, "vb_id", "")).strip() for b in residual if str(getattr(b, "vb_id", "")).strip())

    owner_lines = []
    for behavior in residual:
        vb_id = str(getattr(behavior, "vb_id", "")).strip()
        text = f"{getattr(behavior, 'description', '') or ''} {getattr(behavior, 'declared_scenarios', '') or ''}"
        owners = ", ".join(_vb_owner_test_tokens(text, test_prefixes)[0]) or "(suite-level — choose a file under the test dir)"
        owner_lines.append(f"- {vb_id} → {owners}")

    description = "\n".join(
        [
            "This task OWNS the cross-cutting verifiable behaviors below — the ones no other "
            "derived task's declared test outputs claim (suite-level, static-source, or "
            "universally-quantified invariants that map to no single module, so the "
            "module-scoped task derivation gave them no owning test-authoring task).",
            "Author the covering test(s) under the declared owner path(s) and follow the "
            "determinism/isolation harness contract declared in the canonical test-strategy "
            "document (the design node of this task). Reuse the shared test harness the other "
            "test files use — do not start external processes or read wall-clock time.",
            "",
            format_gap_feedback(residual),
            "",
            "Declared owner test file(s) for the behaviors above:",
            *owner_lines,
        ]
    )

    return VBCoverageClosureTask(
        task_id="verifiable_behavior_coverage",
        design_node=source_doc,
        expected_outputs=tuple(sorted(owner_files)),
        title="Verifiable Behavior Coverage Closure",
        description=description,
        owned_vb_ids=owned_ids,
    )


def _ensure_lexicon(project_root: Path) -> None:
    """Create an inferred draft project_lexicon.yaml when plan --init starts without one."""
    from codd.lexicon import LEXICON_FILENAME

    project_root = Path(project_root).resolve()
    lexicon_path = project_root / LEXICON_FILENAME
    if lexicon_path.exists():
        return

    questions_text = _read_lexicon_questions()
    detected_context = _detect_lexicon_context(project_root)
    design_md_suggestion = _suggest_design_md_for_ui(project_root, detected_context)
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    draft = _build_lexicon_draft(
        project_root=project_root,
        questions_text=questions_text,
        detected_context=detected_context,
        design_md_suggestion=design_md_suggestion,
        fetched_at=fetched_at,
    )
    lexicon_path.write_text(
        yaml.safe_dump(draft, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    context_label = ", ".join(detected_context) if detected_context else "unknown"
    print(f"Created draft {LEXICON_FILENAME} (detected_context={context_label}). Please review and update.")


def _read_lexicon_questions() -> str:
    questions_path = Path(__file__).parent / "templates" / "lexicon_questions.md"
    if not questions_path.exists():
        return ""
    return questions_path.read_text(encoding="utf-8")


def _detect_lexicon_context(project_root: Path) -> list[str]:
    try:
        from codd.knowledge_fetcher import KnowledgeFetcher
    except ImportError:
        return []

    try:
        return list(KnowledgeFetcher(project_root).detect_tech_stack())
    except Exception:
        return []


def _suggest_design_md_for_ui(project_root: Path, detected_context: list[str]) -> dict[str, str] | None:
    try:
        from codd.knowledge_fetcher import KnowledgeFetcher
    except ImportError:
        return None

    try:
        return KnowledgeFetcher(project_root).suggest_design_md_for_ui(detected_context)
    except Exception:
        return None


def _build_lexicon_draft(
    *,
    project_root: Path,
    questions_text: str,
    detected_context: list[str],
    fetched_at: str,
    design_md_suggestion: dict[str, str] | None = None,
) -> dict[str, Any]:
    provenance = "inferred"
    confidence = 0.5
    entry_defaults = {"provenance": provenance, "confidence": confidence, "fetched_at": fetched_at}
    question_count = len(re.findall(r"^###\s+Q\d+:", questions_text, flags=re.MULTILINE))
    context_label = ", ".join(detected_context) if detected_context else "unknown"
    node_vocabulary = [
        {
            "id": "url_route",
            "description": "Browser or API path exposed by the project.",
            "extractor": "filesystem_routes",
            "naming_convention": "kebab-case",
            **entry_defaults,
        },
        {
            "id": "db_table",
            "description": "Persistent database table or collection name.",
            "naming_convention": "snake_case",
            **entry_defaults,
        },
        {
            "id": "env_var",
            "description": "Runtime configuration environment variable.",
            "naming_convention": "SCREAMING_SNAKE_CASE",
            **entry_defaults,
        },
        {
            "id": "cli_command",
            "description": "Command-line command, subcommand, or flag namespace.",
            "naming_convention": "kebab-case",
            **entry_defaults,
        },
        {
            "id": "role",
            "description": "User, operator, or system role used for access and routing decisions.",
            "naming_convention": "snake_case",
            **entry_defaults,
        },
        {
            "id": "domain_event",
            "description": "Domain event or lifecycle state transition name.",
            "naming_convention": "snake_case",
            **entry_defaults,
        },
        {
            "id": "module_file",
            "description": "Source module or file path naming unit.",
            "naming_convention": "snake_case",
            **entry_defaults,
        },
    ]
    naming_conventions = [
        {"id": "kebab-case", "regex": "^[a-z][a-z0-9-]*$"},
        {"id": "snake_case", "regex": "^[a-z][a-z0-9_]*$"},
        {"id": "SCREAMING_SNAKE_CASE", "regex": "^[A-Z][A-Z0-9_]*$"},
        {"id": "PascalCase", "regex": "^[A-Z][A-Za-z0-9]*$"},
    ]
    design_principles = [
        "This project_lexicon.yaml is an inferred draft and must be reviewed before conventions are treated as human-approved.",
        f"Detected project context: {context_label}. Confirm or replace any convention that does not match the project.",
        "Use one canonical name for the same domain concept across docs, code, config, and CLI unless an exception is documented.",
    ]

    if design_md_suggestion:
        node_vocabulary.append(
            {
                "id": "design_token",
                "description": "UI design token from DESIGN.md.",
                "naming_convention": "design-token-path",
                "examples": [
                    "colors.Primary",
                    "typography.body",
                    "spacing.sm",
                    "components.Button.primary",
                ],
                "categories": ["color", "typography", "spacing", "component"],
                **entry_defaults,
            }
        )
        naming_conventions.append(
            {
                "id": "design-token-path",
                "regex": "^[A-Za-z][A-Za-z0-9]*(\\.[A-Za-z][A-Za-z0-9]*)+$",
            }
        )
        design_principles.append(
            f"UI design token conventions should be reconciled with {design_md_suggestion['ui_design_source']}."
        )
    draft_context: dict[str, Any] = {
        "detected_context": detected_context,
        "question_template": "codd/templates/lexicon_questions.md",
        "question_count": question_count,
    }
    if design_md_suggestion:
        draft_context["ui_design"] = design_md_suggestion

    return {
        "version": "1.0",
        "project_id": project_root.name,
        "provenance": provenance,
        "confidence": confidence,
        "fetched_at": fetched_at,
        "draft_context": draft_context,
        "node_vocabulary": node_vocabulary,
        "naming_conventions": naming_conventions,
        "design_principles": design_principles,
        "failure_modes": [
            {"id": "case_drift", "pattern": "The same concept appears with multiple naming cases.", "detector": "lexicon_validate"},
            {"id": "prefix_omission", "pattern": "A role, area, or environment prefix is missing where the project requires one.", "detector": "lexicon_validate"},
            {"id": "untracked_vocabulary", "pattern": "A release-relevant node type is used without a lexicon entry.", "detector": "lexicon_validate"},
        ],
        "extractor_registry": {
            "filesystem_routes": {
                "type": "codd.parsing.FileSystemRouteExtractor",
                "description": "Extract filesystem-driven route paths from configured project directories.",
            }
        },
    }


def build_plan(project_root: Path) -> PlanResult:
    """Build wave execution status for a CoDD project."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)
    artifacts_by_node = {artifact.node_id: artifact for artifact in artifacts}
    ordered_node_ids = _topological_order(artifacts)

    from codd.config import find_codd_dir
    codd_dir = find_codd_dir(project_root) or project_root / "codd"
    validation = validate_project(project_root, codd_dir)
    errors_by_location = _group_validation_errors(validation.issues)
    external_nodes = _index_external_nodes(project_root, config, errors_by_location, set(artifacts_by_node))

    planned_nodes: dict[str, PlannedArtifact] = {}
    for node_id in ordered_node_ids:
        artifact = artifacts_by_node[node_id]
        location = Path(artifact.output).as_posix()
        doc_path = project_root / artifact.output
        validation_errors = sorted(set(errors_by_location.get(location, [])))
        depends_on = _dependency_ids(artifact)

        if doc_path.exists():
            status = STATUS_ERROR if validation_errors else STATUS_DONE
            blocked_by: list[str] = []
        else:
            blocked_by = [
                dependency_id
                for dependency_id in depends_on
                if _dependency_status(dependency_id, planned_nodes, external_nodes) != STATUS_DONE
            ]
            status = STATUS_READY if not blocked_by else STATUS_BLOCKED

        planned_nodes[node_id] = PlannedArtifact(
            wave=artifact.wave,
            node_id=artifact.node_id,
            path=location,
            status=status,
            depends_on=depends_on,
            blocked_by=blocked_by,
            validation_errors=validation_errors,
        )

    waves = [
        PlannedWave(
            wave=wave,
            status=_wave_status([planned_nodes[artifact.node_id] for artifact in artifacts if artifact.wave == wave]),
            nodes=[planned_nodes[artifact.node_id] for artifact in artifacts if artifact.wave == wave],
        )
        for wave in sorted({artifact.wave for artifact in artifacts})
    ]

    summary = {
        "done": sum(1 for node in planned_nodes.values() if node.status == STATUS_DONE),
        "ready": sum(1 for node in planned_nodes.values() if node.status == STATUS_READY),
        "blocked": sum(1 for node in planned_nodes.values() if node.status == STATUS_BLOCKED),
        "error": sum(1 for node in planned_nodes.values() if node.status == STATUS_ERROR),
    }
    next_wave = next((wave.wave for wave in waves if any(node.status == STATUS_READY for node in wave.nodes)), None)

    # Baseline extract: DONE if extracted docs exist, READY if all waves done, BLOCKED otherwise
    all_waves_done = all(wave.status == STATUS_DONE for wave in waves) and waves
    extracted_dir = codd_dir / "extracted"
    has_baseline = extracted_dir.is_dir() and any(extracted_dir.glob("*.md"))
    if has_baseline:
        baseline_status = STATUS_DONE
    elif all_waves_done:
        baseline_status = STATUS_READY
    else:
        baseline_status = STATUS_BLOCKED

    return PlanResult(
        project_root=str(project_root),
        summary=summary,
        next_wave=next_wave,
        waves=waves,
        baseline_status=baseline_status,
    )


def render_plan_text(plan: PlanResult) -> str:
    """Render a human-readable wave plan."""
    lines: list[str] = []

    for index, wave in enumerate(plan.waves):
        if index:
            lines.append("")
        lines.append(f"Wave {wave.wave}: {wave.status}")
        for node in wave.nodes:
            lines.append(f"  {ICON_BY_STATUS[node.status]} {node.node_id}  [{node.status}] {node.path}")
            if node.status == STATUS_READY and node.depends_on:
                lines.append(f"     depends_on: {', '.join(node.depends_on)}")
            elif node.status == STATUS_BLOCKED:
                blocked_text = ", ".join(node.blocked_by) if node.blocked_by else "(unknown)"
                lines.append(f"     blocked_by: {blocked_text}")
            elif node.status == STATUS_ERROR:
                for message in node.validation_errors:
                    lines.append(f"     error: {message}")

    lines.append("")
    lines.append(f"Baseline Extract: {plan.baseline_status}")
    lines.append(f"  {ICON_BY_STATUS[plan.baseline_status]} codd extract  [{plan.baseline_status}]")

    lines.append("")
    lines.append(
        "Summary: "
        f"{plan.summary['done']} DONE, "
        f"{plan.summary['ready']} READY, "
        f"{plan.summary['blocked']} BLOCKED, "
        f"{plan.summary['error']} ERROR"
    )
    if plan.next_wave is not None:
        lines.append(f"Next action: codd generate --wave {plan.next_wave}")
    elif plan.summary["error"]:
        lines.append("Next action: resolve validation errors")
    elif plan.baseline_status == STATUS_READY:
        lines.append("Next action: codd extract  (capture baseline for drift detection)")
    elif plan.baseline_status == STATUS_DONE:
        lines.append("Next action: all waves + baseline DONE — ready for maintenance")
    else:
        lines.append("Next action: all waves DONE")

    return "\n".join(lines)


def plan_to_dict(plan: PlanResult) -> dict:
    """Convert planner output to plain Python data for JSON serialization."""
    return asdict(plan)


def _load_requirement_documents(project_root: Path, config: dict[str, Any]) -> list[RequirementDocument]:
    documents: list[RequirementDocument] = []

    for doc_path in _iter_doc_files(project_root, config):
        parsed = _parse_codd_frontmatter(doc_path)
        if parsed.error:
            continue

        codd = parsed.codd or {}
        # Both singular and plural are accepted: `codd init --requirements`
        # historically stamps `type: requirement`, while hand-authored docs and
        # the artifact catalog vocabulary use `requirements`.
        if codd.get("type") not in ("requirement", "requirements"):
            continue

        node_id = codd.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            continue

        documents.append(
            RequirementDocument(
                node_id=node_id.strip(),
                path=doc_path.relative_to(project_root).as_posix(),
                content=doc_path.read_text(encoding="utf-8"),
            )
        )

    return documents


def _build_plan_init_prompt(
    config: dict[str, Any],
    requirement_documents: list[RequirementDocument],
    capabilities: ProjectCapabilities | None = None,
) -> str:
    if capabilities is None:
        capabilities = generator_module.WEB_FALLBACK_CAPABILITIES
    project = config.get("project") or {}
    scan = config.get("scan") or {}
    doc_dirs = scan.get("doc_dirs") or []
    project_name = project.get("name") or "(unknown)"
    language = project.get("language") or "(unknown)"

    frameworks = project.get("frameworks") or []
    frameworks_str = ", ".join(frameworks) if frameworks else "(none)"

    lines = [
        "You are initializing CoDD wave_config from requirement documents.",
        f"Project name: {project_name}",
        f"Primary language: {language}",
        f"Detected/configured frameworks: {frameworks_str}",
        "Configured doc_dirs: " + (", ".join(str(item) for item in doc_dirs) if doc_dirs else "(none)"),
        "",
        "MECE Document Structure (7 categories):",
        MECE_DOCUMENT_STRUCTURE.rstrip(),
        "",
        "Standard V-model artifact patterns:",
        _standard_v_model_patterns(capabilities).rstrip(),
        "",
        "Instructions:",
        "- Read the requirement documents below and produce a MECE (Mutually Exclusive, Collectively Exhaustive) document set: every requirement section maps to at least one design artifact (no gaps), and each artifact has a distinct responsibility (no overlaps).",
        "- Output ONLY YAML for the wave_config mapping. Do not emit prose or Markdown fences.",
        "- Use string wave numbers as the top-level keys.",
        "- Each artifact entry must include node_id, output, title, depends_on, and conventions.",
        "- Each artifact entry must also include a `modules` list naming the source modules the document covers (e.g., ['auth', 'users']). This links design docs to source code for traceability.",
        "- Insert a dedicated detailed design wave between overview design and implementation planning when the project has multiple modules, integrations, workflows, or shared domain concepts.",
        "- Detailed design artifacts must live under docs/detailed_design/ and stay Markdown + Mermaid (text-first, no binary diagrams).",
        "- Decide which detailed design artifacts are necessary from the project context; do not hardcode a fixed set. Good candidates include shared domain ownership, component dependency maps, ER/CRUD views, key sequence diagrams, and state machines.",
        "- If requirements describe actors, permissions, mutable commands, lifecycle states, cross-actor visibility, or external side effects, assign a design artifact responsibility for an Operational Behavior Model before implementation planning. This may be a standalone docs/design/ artifact or an explicit section in a relevant design/detailed design artifact.",
        "- The Operational Behavior Model is design-time source of truth, not an E2E test artifact. It must define actor/action/state/outcome obligations so implementation cannot omit them and tests can be generated from them later.",
        "- For actor-facing operations on object-specific or parameterized surfaces, the Operational Behavior Model must also define how the actor reaches the operation surface (entry/list/parent surface, visible navigation affordance, or equivalent trigger). Direct deep links or lower-layer API access are not sufficient design contracts.",
        *(
            [
                "- If requirements describe user-facing surfaces, roles/actors, navigation, onboarding/authentication, or visible user copy, assign a design artifact responsibility for actor-facing surface/copy obligations before implementation planning.",
                "- Actor-facing surface/copy obligations must define each surface's purpose, primary audience, allowed and forbidden actions/navigation, required user-visible copy intent, and forbidden copy patterns. The copy must use the audience's job-to-be-done language, not implementation rationale, internal process notes, demo/test labels, or hidden authority-boundary explanations.",
            ]
            if capabilities.user_interface
            else []
        ),
        "- conventions are release-blocking constraints. If a convention is violated, the project is not releasable.",
        "- Extract conventions from the requirement documents for these categories:",
        "  security constraints (tenant isolation, authentication, authorization, auditability),",
        "  technical constraints (required stack, forbidden libraries, mandated integrations),",
        "  framework implicit conventions (routing patterns, directory-to-URL mapping rules, middleware semantics, ORM conventions, build-tool behaviors — any framework-specific rule that the framework enforces silently and that generated code must respect),",
        "  legal/regulatory requirements (privacy, GDPR, APPI, contractual obligations), and",
        "  non-functional requirements (SLA, latency, throughput, availability, recovery thresholds).",
        "- Assign the relevant conventions to each artifact entry. Use conventions: [] only when an artifact truly has no release-blocking constraints.",
        "- Do not add requirement documents themselves to wave_config.",
        "- Keep output paths under docs/design/, docs/detailed_design/, docs/plan/, docs/governance/, docs/test/, docs/operations/, or docs/infra/.",
        "- Set dependencies so earlier waves unlock later waves in a realistic order.",
        "- Do not emit explanatory headings or summaries such as 'Key conventions extracted:' or 'Notes:' before the YAML.",
        "",
        "Required schema (JSON notation):",
        "{",
        '  "<wave-number>": [',
        "    {",
        '      "node_id": "category:name",',
        '      "output": "docs/.../file.md",',
        '      "title": "Document Title",',
        '      "depends_on": [{"id": "node:id", "relation": "derives_from", "semantic": "governance"}],',
        '      "conventions": [{"targets": ["node:id"], "reason": "release-blocking constraint"}]',
        '      "modules": ["module_name_1", "module_name_2"]',
        "    }",
        "  ]",
        "}",
        "",
        "Example output shape (YAML mapping only; do not wrap it in a top-level wave_config key):",
        '"1":',
        '  - node_id: "design:acceptance-criteria"',
        '    output: "docs/test/acceptance_criteria.md"',
        '    title: "Acceptance Criteria"',
        "    depends_on:",
        '      - id: "req:project-requirements"',
        '        relation: "derives_from"',
        '        semantic: "governance"',
        "    conventions:",
        "      - targets:",
        '          - "db:rls_policies"',
        '          - "module:auth"',
        '        reason: "Tenant isolation and authenticated access are release-blocking constraints."',
        '    modules: ["auth"]',
        '"2":',
        '  - node_id: "design:system-design"',
        '    output: "docs/design/system_design.md"',
        '    title: "System Design"',
        "    depends_on:",
        '      - id: "design:acceptance-criteria"',
        '        relation: "constrained_by"',
        '        semantic: "governance"',
        "    conventions:",
        "      - targets:",
        '          - "db:rls_policies"',
        '          - "service:auth"',
        '        reason: "Security, privacy, and access-control constraints must be reflected explicitly."',
        '"3":',
        '  - node_id: "design:shared-domain-model"',
        '    output: "docs/detailed_design/shared_domain_model.md"',
        '    title: "Shared Domain Model"',
        "    depends_on:",
        '      - id: "design:system-design"',
        '        relation: "depends_on"',
        '        semantic: "technical"',
        "    conventions:",
        "      - targets:",
        '          - "module:auth"',
        '          - "db:rls_policies"',
        '        reason: "Canonical ownership of shared types and tenant boundaries must be implementation-ready before coding begins."',
        "",
        "User instruction:",
        "以下の要件定義書を読み、このプロジェクトに必要な設計成果物・依存順序・artifactごとのconventionsを判断し、wave_config形式のYAMLを出力せよ。",
        "conventionsは『違反したらリリース不可の制約』として抽出し、各artifactへ必ず割り当てること。",
        "詳細設計waveが必要な場合は docs/detailed_design/ 配下に Mermaid 図を含む artifact を提案せよ。",
        "業務上のactor/action/state/outcomeがある場合は、実装前の設計artifactとしてOperational Behavior Modelを必ず担当させ、E2Eテスト側へ先送りしないこと。",
        *(
            ["利用者に見える画面・導線・文言・ロール説明がある場合は、各surfaceの目的・対象actor・許可/禁止される導線・必要文言・禁止文言を設計artifactで必ず担当させること。"]
            if capabilities.user_interface
            else []
        ),
        "",
        "Requirement documents:",
    ]

    for document in requirement_documents:
        lines.extend(
            [
                f"--- BEGIN REQUIREMENT {document.path} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END REQUIREMENT {document.path} ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _load_extracted_documents(project_root: Path, config: dict[str, Any]) -> list[ExtractedDocument]:
    """Load extracted docs from the canonical extract output location.

    Discovery uses the shared :mod:`codd.extract_paths` source of truth so the
    planner reads exactly where ``codd extract`` writes (``.codd/extract/``),
    with legacy ``<codd_dir>/extracted/`` kept discoverable for older projects.
    """
    from codd.extract_paths import extracted_doc_search_dirs

    documents: list[ExtractedDocument] = []
    seen_paths: set[Path] = set()
    for extracted_dir in extracted_doc_search_dirs(project_root):
        for doc_path in sorted(extracted_dir.rglob("*.md")):
            resolved = doc_path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            parsed = _parse_codd_frontmatter(doc_path)
            if parsed.error:
                continue
            codd = parsed.codd or {}
            if codd.get("source") != "extracted":
                continue
            node_id = codd.get("node_id")
            if not isinstance(node_id, str) or not node_id.strip():
                continue
            try:
                rel_path = doc_path.relative_to(project_root).as_posix()
            except ValueError:
                rel_path = doc_path.as_posix()
            documents.append(
                ExtractedDocument(
                    node_id=node_id.strip(),
                    path=rel_path,
                    content=doc_path.read_text(encoding="utf-8"),
                )
            )
    return documents


def _build_brownfield_plan_init_prompt(
    config: dict[str, Any],
    extracted_documents: list[ExtractedDocument],
    capabilities: ProjectCapabilities | None = None,
) -> str:
    if capabilities is None:
        capabilities = generator_module.WEB_FALLBACK_CAPABILITIES
    project = config.get("project") or {}
    scan = config.get("scan") or {}
    doc_dirs = scan.get("doc_dirs") or []
    project_name = project.get("name") or "(unknown)"
    language = project.get("language") or "(unknown)"

    frameworks = project.get("frameworks") or []
    frameworks_str = ", ".join(frameworks) if frameworks else "(none)"

    lines = [
        "You are initializing CoDD wave_config for a BROWNFIELD project from extracted documents.",
        "These extracted documents were generated by 'codd extract' from existing source code.",
        "Your job is to create a V-Model design document plan that covers the existing codebase.",
        f"Project name: {project_name}",
        f"Primary language: {language}",
        f"Detected/configured frameworks: {frameworks_str}",
        "Configured doc_dirs: " + (", ".join(str(item) for item in doc_dirs) if doc_dirs else "(none)"),
        "",
        "MECE Document Structure (7 categories):",
        MECE_DOCUMENT_STRUCTURE.rstrip(),
        "",
        "Standard V-model artifact patterns:",
        _standard_v_model_patterns(capabilities).rstrip(),
        "",
        "Instructions:",
        "- Read the extracted documents below. They describe the existing codebase structure, modules, symbols, dependencies, and patterns.",
        "- Design a wave_config that will produce design documents covering this existing system.",
        "- Since this is brownfield (code already exists), the design documents serve as retroactive documentation, not forward planning.",
        "- Each artifact entry must include node_id, output, title, depends_on, and conventions.",
        "- Each artifact entry must also include a `modules` list naming the source modules the document covers (e.g., ['auth', 'users']).",
        "- Map extracted modules to design documents. Group related modules into the same design doc where appropriate.",
        "- Use the extracted document node_ids in depends_on to trace back to the source analysis.",
        "- Insert detailed design documents for complex modules or module groups.",
        "- If the extracted documents imply actors, permissions, mutable commands, lifecycle states, cross-actor visibility, or external side effects, assign a design artifact responsibility for an Operational Behavior Model before implementation planning.",
        "- The Operational Behavior Model is design-time source of truth, not an E2E test artifact. It must define actor/action/state/outcome obligations so future changes and tests can trace back to design.",
        "- For actor-facing operations on object-specific or parameterized surfaces, the Operational Behavior Model must also define how the actor reaches the operation surface (entry/list/parent surface, visible navigation affordance, or equivalent trigger). Direct deep links or lower-layer API access are not sufficient design contracts.",
        *(
            [
                "- If the extracted documents imply user-facing surfaces, roles/actors, navigation, onboarding/authentication, or visible user copy, assign a design artifact responsibility for actor-facing surface/copy obligations before implementation planning.",
                "- Actor-facing surface/copy obligations must define each surface's purpose, primary audience, allowed and forbidden actions/navigation, required user-visible copy intent, and forbidden copy patterns. The copy must use the audience's job-to-be-done language, not implementation rationale, internal process notes, demo/test labels, or hidden authority-boundary explanations.",
            ]
            if capabilities.user_interface
            else []
        ),
        "- conventions are release-blocking constraints. Extract them from the patterns detected in the extracted documents (e.g., authentication, database models, API routes).",
        "- When frameworks are detected, also extract framework implicit conventions (routing patterns, directory-to-URL mapping rules, middleware semantics, ORM conventions, build-tool behaviors — any framework-specific rule that the framework enforces silently and that generated code must respect).",
        "- Do not add extracted documents themselves to wave_config — they are inputs, not outputs.",
        "- Keep output paths under docs/design/, docs/detailed_design/, docs/plan/, docs/governance/, docs/test/, docs/operations/, or docs/infra/.",
        "- Set dependencies so earlier waves unlock later waves in a realistic order.",
        "- Do not emit explanatory headings or summaries before the YAML.",
        "",
        "Required schema (JSON notation):",
        "{",
        '  "<wave-number>": [',
        "    {",
        '      "node_id": "category:name",',
        '      "output": "docs/.../file.md",',
        '      "title": "Document Title",',
        '      "modules": ["module_name_1", "module_name_2"],',
        '      "depends_on": [{"id": "node:id", "relation": "derives_from", "semantic": "governance"}],',
        '      "conventions": [{"targets": ["node:id"], "reason": "release-blocking constraint"}]',
        "    }",
        "  ]",
        "}",
        "",
        "Output ONLY YAML for the wave_config mapping. Do not emit prose or Markdown fences.",
        "",
        "User instruction:",
        "以下のextractedドキュメント（既存コードベースから自動抽出された設計情報）を読み、",
        "このプロジェクトの既存構造を網羅する設計成果物・依存順序・artifactごとのconventionsとmodulesを判断し、",
        "wave_config形式のYAMLを出力せよ。",
        "各artifactにはmodulesフィールドで対応するソースモジュール名のリストを必ず含めること。",
        "既存コードに業務上のactor/action/state/outcomeがある場合はOperational Behavior Modelを設計artifactとして担当させ、E2Eテスト側へ先送りしないこと。",
        "既存コードに利用者に見える画面・導線・文言・ロール説明がある場合は、各surfaceの目的・対象actor・許可/禁止される導線・必要文言・禁止文言を設計artifactで担当させること。",
        "",
        "Extracted documents:",
    ]

    for document in extracted_documents:
        lines.extend(
            [
                f"--- BEGIN EXTRACTED {document.path} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END EXTRACTED {document.path} ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


#: Bounded re-invocations when the AI returns an unparseable wave_config.
#: Overridable per project via `plan.init_retry.max_retries` in codd.yaml.
DEFAULT_PLAN_INIT_MAX_RETRIES = 2


def _plan_init_max_retries(config: dict[str, Any]) -> int:
    """Read `plan.init_retry.max_retries` (default 2; 0 disables the retry loop)."""

    plan_cfg = config.get("plan")
    if isinstance(plan_cfg, dict):
        retry_cfg = plan_cfg.get("init_retry")
        if isinstance(retry_cfg, dict):
            value = retry_cfg.get("max_retries")
            if isinstance(value, int) and value >= 0:
                return value
    return DEFAULT_PLAN_INIT_MAX_RETRIES


def _append_wave_config_retry_feedback(prompt: str, error: str) -> str:
    """Re-state the strict-YAML contract after a parse failure, for the next attempt."""

    note = (
        "\n\n---\n"
        "YOUR PREVIOUS RESPONSE WAS REJECTED: it could not be parsed as valid wave_config "
        f"YAML ({error}). Output ONLY the wave_config mapping as STRICT, valid YAML — no "
        "prose, no Markdown fences, no arrows (`->`) or annotations. Every key must be "
        "followed by a colon and a value, and every list item must start with `- `. "
        "Re-emit the COMPLETE wave_config now."
    )
    return prompt + note


def _invoke_and_parse_wave_config(
    ai_command: str, base_prompt: str, *, max_retries: int = DEFAULT_PLAN_INIT_MAX_RETRIES
) -> dict[str, list[dict[str, Any]]]:
    """Invoke the AI for a wave_config with a bounded parse-feedback retry loop.

    A weaker model can emit syntactically malformed YAML (a stray ``-> `` arrow, a
    key with no colon) that no generic textual sanitization can repair. Hard-failing
    the whole plan stage on a single bad response violates the weakest-model
    principle — a strong model masks the gap a weak one exposes. So each retry feeds
    the parse error back to the model (mirrors generator._generate_document_body).
    Infra failures (``AI command failed``) propagate immediately; exhausted retries
    re-raise the last parse error unchanged (backward compat).
    """

    last_error: ValueError | None = None
    feedback: str | None = None
    for _attempt in range(max(0, max_retries) + 1):
        prompt = base_prompt if feedback is None else _append_wave_config_retry_feedback(base_prompt, feedback)
        try:
            raw = generator_module._invoke_ai_command(ai_command, prompt)
            return _parse_wave_config_output(raw)
        except ValueError as exc:
            if str(exc).startswith("AI command failed"):
                raise  # infra failure: retrying the same broken command cannot help
            last_error = exc
            feedback = str(exc)
    assert last_error is not None
    raise last_error


def _parse_wave_config_output(raw_output: str) -> dict[str, list[dict[str, Any]]]:
    """Parse an AI command's stdout into a validated wave_config mapping.

    AI backends frequently wrap structured output in noise: markdown code
    fences (```yaml ... ```), conversational prose before/after the payload,
    or a tool-call JSON envelope. To stay backend-agnostic (no model- or
    tool-specific handling), we derive an ordered set of candidate
    sanitizations of ``raw_output`` and accept the first one that yields a
    structurally valid wave_config. Candidates are generic textual transforms,
    not per-vendor special cases.
    """
    if not _clean_wave_config_output(raw_output):
        raise ValueError("AI command returned empty wave_config output")

    last_error: Exception | None = None
    for candidate in _iter_wave_config_candidates(raw_output):
        try:
            payload = yaml.safe_load(candidate)
        except yaml.YAMLError as exc:
            last_error = exc
            continue

        if isinstance(payload, dict) and isinstance(payload.get("wave_config"), dict):
            payload = payload["wave_config"]

        if not isinstance(payload, dict):
            last_error = ValueError(
                "AI command must return a YAML mapping of wave numbers to artifact lists"
            )
            continue

        try:
            artifacts = _load_wave_artifacts({"wave_config": payload})
        except ValueError as exc:
            last_error = exc
            continue

        return _serialize_wave_config(artifacts)

    if isinstance(last_error, yaml.YAMLError):
        raise ValueError(f"AI command returned invalid wave_config YAML: {last_error}") from last_error
    if last_error is not None:
        raise last_error
    raise ValueError("AI command must return a YAML mapping of wave numbers to artifact lists")


def _iter_wave_config_candidates(raw_output: str) -> list[str]:
    """Yield candidate YAML strings extracted from a noisy AI response.

    Each candidate is a progressively more aggressive attempt to isolate the
    YAML body from surrounding LLM noise. Ordering goes from least to most
    transformation so a clean response is parsed verbatim, while noisy ones
    still recover. Duplicates and empties are dropped.
    """
    candidates: list[str] = []

    def _add(text: str | None) -> None:
        if not text:
            return
        normalized = text.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    def _clean(text: str | None) -> str | None:
        return _clean_wave_config_output(text) if text else None

    cleaned = _clean_wave_config_output(raw_output)
    _add(cleaned)
    # Prose may appear before AND after a fenced block; pull just the fenced
    # body (preferring a yaml/yml-tagged fence) from anywhere in the text.
    _add(_clean(_extract_fenced_block(raw_output)))
    # Leading prose with no fence: skip to the first mapping-looking line.
    _add(_clean(_trim_to_wave_config_mapping(cleaned)))
    # Some backends emit a JSON envelope whose value is the YAML/text payload.
    _add(_clean(_unwrap_json_envelope(raw_output)))

    return candidates


# Matches a markdown fenced block anywhere in the text. The optional info
# string (language tag) is captured so we can prefer a yaml/yml fence, and the
# body is non-greedy so we stop at the FIRST closing fence rather than swallow
# trailing prose. Anchored to a line start to avoid matching inline backticks.
_FENCED_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\r?\n"
    r"(?P<body>.*?)\r?\n[ \t]*```",
    re.DOTALL,
)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    fenced = re.match(r"^```(?:yaml|yml)?\s*\n(?P<body>.*)\n```$", stripped, re.DOTALL)
    if fenced:
        return fenced.group("body")
    return stripped


def _extract_fenced_block(text: str) -> str:
    """Return the body of a fenced code block found anywhere in ``text``.

    Tolerates conversational prose before and after the fence and a closing
    fence followed by more prose. When several blocks are present, a
    yaml/yml-tagged block wins; otherwise the first block is used. Returns the
    original text unchanged when no fenced block is present so callers can fall
    back to other strategies. Language-agnostic: no model/tool names involved.
    """
    if not text:
        return text

    blocks = list(_FENCED_BLOCK_RE.finditer(text))
    if not blocks:
        return text

    for match in blocks:
        if match.group("lang").strip().lower() in {"yaml", "yml"}:
            return match.group("body")
    return blocks[0].group("body")


def _clean_wave_config_output(text: str) -> str:
    stripped = _strip_code_fences(text).strip()
    lines = [line for line in stripped.splitlines() if not re.match(r"^\s*```[A-Za-z0-9_+-]*\s*$", line)]
    return "\n".join(lines).strip()


def _trim_to_wave_config_mapping(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r'^\s*(?:wave_config|["\']?\d+["\']?)\s*:\s*(?:#.*)?$', line):
            return "\n".join(lines[index:]).strip()
    return text


def _unwrap_json_envelope(text: str) -> str | None:
    """Recover a YAML/text payload nested inside a JSON envelope, if present.

    Some AI backends return their answer wrapped in a JSON object (e.g. a
    tool-call result) rather than as raw stdout. We parse the text as JSON and
    walk it for the longest string value that, once de-fenced, looks like a
    wave_config mapping. This is structural — it keys on JSON shape and YAML
    content, never on a specific provider or field name. Returns ``None`` when
    the text is not JSON or holds no usable string payload.
    """
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return None

    import json

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    best: str | None = None
    stack = [parsed]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            body = _clean_wave_config_output(_extract_fenced_block(current))
            if _looks_like_wave_config_yaml(body) and (best is None or len(body) > len(best)):
                best = body
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)

    return best


def _looks_like_wave_config_yaml(text: str) -> bool:
    """Heuristic: does ``text`` parse as a mapping with wave-number-ish keys?"""
    if not text:
        return False
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if isinstance(payload, dict) and isinstance(payload.get("wave_config"), dict):
        payload = payload["wave_config"]
    if not isinstance(payload, dict) or not payload:
        return False
    return all(str(key).strip().lstrip("-").isdigit() for key in payload)


def _serialize_wave_config(artifacts: list[WaveArtifact]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for artifact in artifacts:
        entry: dict[str, Any] = {
            "node_id": artifact.node_id,
            "output": artifact.output,
            "title": artifact.title,
        }
        if artifact.depends_on:
            entry["depends_on"] = artifact.depends_on
        if artifact.conventions:
            entry["conventions"] = artifact.conventions
        if artifact.modules:
            entry["modules"] = list(artifact.modules)
        grouped[str(artifact.wave)].append(entry)

    return {wave: grouped[wave] for wave in sorted(grouped, key=int)}


def _group_validation_errors(issues) -> dict[str, list[str]]:
    errors_by_location: dict[str, list[str]] = defaultdict(list)
    for issue in issues:
        if issue.level != STATUS_ERROR:
            continue
        errors_by_location[issue.location].append(issue.message)
    return dict(errors_by_location)


def _index_external_nodes(
    project_root: Path,
    config: dict,
    errors_by_location: dict[str, list[str]],
    planned_nodes: set[str],
) -> dict[str, _ExternalNode]:
    nodes: dict[str, _ExternalNode] = {}

    for doc_path in _iter_doc_files(project_root, config):
        relative_path = doc_path.relative_to(project_root).as_posix()
        parsed = _parse_codd_frontmatter(doc_path)
        if parsed.error:
            continue

        codd = parsed.codd or {}
        node_id = codd.get("node_id")
        if not isinstance(node_id, str) or node_id in planned_nodes:
            continue

        status = STATUS_ERROR if errors_by_location.get(relative_path) else STATUS_DONE
        nodes[node_id] = _ExternalNode(path=relative_path, status=status)

    return nodes


def _dependency_ids(artifact: WaveArtifact) -> list[str]:
    return [entry["id"] for entry in artifact.depends_on]


def _dependency_status(
    dependency_id: str,
    planned_nodes: dict[str, PlannedArtifact],
    external_nodes: dict[str, _ExternalNode],
) -> str:
    if dependency_id.startswith("req:"):
        return STATUS_DONE
    if dependency_id in planned_nodes:
        return planned_nodes[dependency_id].status
    if dependency_id in external_nodes:
        return external_nodes[dependency_id].status
    return STATUS_BLOCKED


def _wave_status(nodes: list[PlannedArtifact]) -> str:
    statuses = {node.status for node in nodes}
    if STATUS_ERROR in statuses:
        return STATUS_ERROR
    if statuses == {STATUS_DONE}:
        return STATUS_DONE
    if STATUS_READY in statuses:
        return STATUS_READY
    return STATUS_BLOCKED


def _topological_order(artifacts: list[WaveArtifact]) -> list[str]:
    artifacts_by_node = {artifact.node_id: artifact for artifact in artifacts}
    indegree = {artifact.node_id: 0 for artifact in artifacts}
    adjacency = {artifact.node_id: set() for artifact in artifacts}

    for artifact in artifacts:
        for dependency_id in _dependency_ids(artifact):
            if dependency_id not in indegree:
                continue
            if artifact.node_id in adjacency[dependency_id]:
                continue
            adjacency[dependency_id].add(artifact.node_id)
            indegree[artifact.node_id] += 1

    ready = sorted(
        [node_id for node_id, degree in indegree.items() if degree == 0],
        key=lambda node_id: (artifacts_by_node[node_id].wave, node_id),
    )
    order: list[str] = []

    while ready:
        node_id = ready.pop(0)
        order.append(node_id)

        for child_id in sorted(adjacency[node_id], key=lambda child: (artifacts_by_node[child].wave, child)):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
        ready.sort(key=lambda candidate: (artifacts_by_node[candidate].wave, candidate))

    if len(order) != len(artifacts):
        cycle_nodes = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise ValueError(f"wave_config contains a dependency cycle: {', '.join(cycle_nodes)}")

    return order

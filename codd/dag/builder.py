"""Build a project-wide DAG for completeness checks."""

from __future__ import annotations

import json
import logging
import fnmatch
import re
import warnings
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config
from codd.dag import DAG, Edge, Node
from codd.dag.coverage_axes import CoverageAxis, extract_coverage_axes_from_design_doc, extract_coverage_axes_from_lexicon
from codd.dag.extractor import extract_design_doc_metadata, extract_imports, scan_capability_evidence
from codd.llm.design_doc_extractor import (
    ExpectedExtraction,
    extract_expected_artifacts_for_file,
    load_cached_expected_extraction,
)
from codd.deployment.extractor import (
    deployment_doc_attributes,
    extract_deployment_docs,
    extract_runtime_states,
    extract_verification_tests,
    infer_deployment_edges,
    runtime_state_attributes,
    verification_test_attributes,
)


LOGGER = logging.getLogger(__name__)
DEFAULTS_DIR = Path(__file__).parent / "defaults"
DEFAULT_PROJECT_TYPE = "generic"
_DAG_BUILD_CACHE: dict[str, Any] = {}
LEGACY_IMPLEMENTATION_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java")
LEGACY_TEST_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".bats")
PLAN_HEADER_RE = re.compile(r"^#{2,6}\s+([A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)(?:\s+(.+))?$", re.MULTILINE)
OUTPUTS_RE = re.compile(r"(?im)^outputs?[ \t]*:[ \t]*(.*)$")
PY_IMPORT_RE = re.compile(r"(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)(?:\s+import\s+)|import\s+([A-Za-z_][\w.]*))")
LEXICON_OUTPUT_PREFIX = "lexicon:"
EXPECTED_ARTIFACT_ATTRIBUTE_KEYS = (
    "id",
    "title",
    "scope",
    "source",
    "path",
    "file",
    "output",
    "artifact_path",
    "value",
    "depends_on",
    "derived_from",
    "description",
    "acceptance_criteria",
    "tags",
    "priority",
    "owner",
    "journey",
    "browser_requirements",
    "runtime_requirements",
)


def build_dag(project_root: Path, settings: dict[str, Any] | None = None) -> DAG:
    """Scan ``project_root`` and write ``.codd/dag.json``."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project root not found: {root}")

    dag_settings = load_dag_settings(root, settings)

    dag = DAG()
    design_docs = _add_design_docs(dag, root, dag_settings)
    impl_nodes = _add_impl_files(dag, root, dag_settings)
    test_nodes = _add_test_files(dag, root, dag_settings)

    _add_design_edges(dag, root, design_docs, impl_nodes)
    _add_design_doc_expected_extractions(dag, root, dag_settings, design_docs)
    _add_import_edges(dag, root, impl_nodes, dag_settings)
    _add_tested_by_edges(dag, root, impl_nodes, test_nodes, dag_settings)
    _add_expected_nodes(dag, root, dag_settings, impl_nodes)
    _add_design_doc_expected_outcome_edges(dag, design_docs)
    _add_plan_tasks(dag, root, dag_settings)
    _add_deployment_graph(dag, root, design_docs, impl_nodes)
    _attach_coverage_axes(dag, root, dag_settings)

    write_dag_json(dag, root, default_dag_json_path(root))
    return dag


def reset_dag_cache(project_root: Path | None = None) -> None:
    """Clear in-process DAG builder cache state.

    The builder currently rebuilds eagerly, but repair verification calls this
    public hook per attempt so future memoization cannot leak stale DAG state.
    """

    if project_root is None:
        _DAG_BUILD_CACHE.clear()
        return
    _DAG_BUILD_CACHE.pop(str(Path(project_root).resolve()), None)


def load_dag_settings(project_root: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load project-type defaults and apply ``codd.yaml dag:`` overrides."""

    root = Path(project_root).resolve()
    project_config = _load_project_config_or_empty(root)

    requested_settings = settings or {}
    project_type = _project_type(requested_settings) or _project_type(project_config) or _detect_project_type(root)
    merged = _read_default_settings(project_type)
    merged = _deep_merge(merged, _dag_overrides(project_config))
    merged = _deep_merge(merged, _dag_overrides(requested_settings))
    merged["project_type"] = project_type
    implementation_suffixes, test_suffixes = _load_suffix_config(root, merged)
    merged["implementation_suffixes"] = implementation_suffixes
    merged["test_suffixes"] = test_suffixes
    _apply_scan_patterns(merged, project_config)
    _apply_scan_patterns(merged, requested_settings)
    _apply_common_node_patterns(merged, project_config)
    _apply_common_node_patterns(merged, requested_settings)
    merged["coherence"] = _coherence_settings(project_config, requested_settings)
    merged["extraction"] = _extraction_settings(project_config, requested_settings)
    merged.setdefault("design_doc_patterns", [])
    merged.setdefault("impl_file_patterns", [])
    merged.setdefault("test_file_patterns", [])
    merged.setdefault("common_node_patterns", [])
    # cmd_444 v2.11.0: implementation_plan.md is no longer the entry point.
    # `codd implement` now takes design_node + output_paths directly. Any
    # `plan_task_file` value present in legacy codd.yaml is silently ignored.
    merged.setdefault("lexicon_file", "project_lexicon.yaml")
    return merged


def _load_project_config_or_empty(project_root: Path) -> dict[str, Any]:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def default_dag_json_path(project_root: Path) -> Path:
    return Path(project_root) / ".codd" / "dag.json"


def default_dag_mermaid_path(project_root: Path) -> Path:
    return Path(project_root) / ".codd" / "dag.mmd"


def dag_to_dict(dag: DAG, project_root: Path) -> dict[str, Any]:
    """Serialize a DAG using the stable `.codd/dag.json` schema."""

    payload = {
        "version": "1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(Path(project_root).resolve()),
        "nodes": [
            {
                "id": node.id,
                "kind": node.kind,
                "path": node.path,
                "attributes": _dag_json_attributes(node.attributes),
            }
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
        ],
        "edges": [_edge_to_dict(edge) for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind))],
        "cycles": dag.detect_cycles(),
    }
    coverage_axes = getattr(dag, "coverage_axes", [])
    if coverage_axes:
        payload["coverage_axes"] = [
            axis.to_dict() if isinstance(axis, CoverageAxis) else axis for axis in coverage_axes
        ]
    return payload


def _edge_to_dict(edge: Edge) -> dict[str, Any]:
    payload = {
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "kind": edge.kind,
    }
    if edge.attributes:
        payload["attributes"] = edge.attributes
    return payload


def _dag_json_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if key != "expected_extraction"}


def write_dag_json(dag: DAG, project_root: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(dag_to_dict(dag, project_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_dag_mermaid(dag: DAG, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_mermaid(dag), encoding="utf-8")


def render_mermaid(dag: DAG) -> str:
    lines = ["flowchart TD"]
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        lines.append(f'  {_mermaid_id(node.id)}["{_escape_mermaid(node.kind)}: {_escape_mermaid(node.id)}"]')
    for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind)):
        lines.append(
            f"  {_mermaid_id(edge.from_id)} -->|{_escape_mermaid(edge.kind)}| {_mermaid_id(edge.to_id)}"
        )
    return "\n".join(lines) + "\n"


def _add_design_docs(dag: DAG, project_root: Path, settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    design_docs: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    frontmatter_alias = _frontmatter_alias_settings(settings)
    for md_path in _glob_project_paths(
        project_root,
        settings.get("design_doc_patterns", []),
        exclude_patterns=settings.get("scan_exclude_patterns"),
    ):
        if not md_path.is_file():
            continue
        node_id = _relative_id(md_path, project_root)
        metadata = extract_design_doc_metadata(md_path, frontmatter_alias=frontmatter_alias)
        attributes = metadata.get("attributes") or {}
        _validate_design_doc_journey_attributes(node_id, attributes)
        node_kind = (
            "common"
            if _design_doc_declares_common(metadata.get("frontmatter"), attributes)
            else "design_doc"
        )
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind=node_kind,
                path=node_id,
                attributes={
                    "frontmatter": metadata["frontmatter"],
                    "depends_on": metadata["depends_on"],
                    "node_id": metadata.get("node_id"),
                    **attributes,
                },
            ),
        )
        design_docs[node_id] = {**metadata, "attributes": attributes, "path": md_path}
        if metadata.get("node_id"):
            aliases[str(metadata["node_id"])] = node_id

    for metadata in design_docs.values():
        metadata["aliases"] = aliases
    return design_docs


def _add_impl_files(dag: DAG, project_root: Path, settings: dict[str, Any]) -> dict[str, Path]:
    impl_nodes: dict[str, Path] = {}
    capability_patterns = _capability_patterns(settings)
    implementation_suffixes = _suffix_tuple(settings.get("implementation_suffixes")) or LEGACY_IMPLEMENTATION_SUFFIXES
    common_patterns = _common_node_patterns(settings)
    project_root_resolved = Path(project_root).resolve()
    for file_path in _glob_project_paths(
        project_root,
        settings.get("impl_file_patterns", []),
        exclude_patterns=settings.get("scan_exclude_patterns"),
    ):
        if (
            not file_path.is_file()
            or file_path.suffix not in implementation_suffixes
            or _is_test_file(file_path, project_root)
        ):
            continue
        node_id = _relative_id(file_path, project_root)
        impl_nodes[node_id] = file_path.resolve()
        kind = "impl_file"
        if common_patterns and _path_matches_any_pattern(
            file_path, project_root_resolved, common_patterns
        ):
            kind = "common"
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind=kind,
                path=node_id,
                attributes={
                    "language": _language_for_path(file_path),
                    "imports": extract_imports(file_path),
                    "runtime_evidence": _runtime_evidence_for_file(file_path, node_id, capability_patterns),
                },
            ),
        )
    return impl_nodes


def _add_test_files(dag: DAG, project_root: Path, settings: dict[str, Any]) -> dict[str, Path]:
    test_nodes: dict[str, Path] = {}
    test_suffixes = _suffix_tuple(settings.get("test_suffixes")) or LEGACY_TEST_SUFFIXES
    common_patterns = _common_node_patterns(settings)
    project_root_resolved = Path(project_root).resolve()
    for file_path in _glob_project_paths(
        project_root,
        settings.get("test_file_patterns", []),
        exclude_patterns=settings.get("scan_exclude_patterns"),
    ):
        if not file_path.is_file() or file_path.suffix not in test_suffixes or not _is_test_file(file_path, project_root):
            continue
        node_id = _relative_id(file_path, project_root)
        test_nodes[node_id] = file_path.resolve()
        kind = "test_file"
        if common_patterns and _path_matches_any_pattern(
            file_path, project_root_resolved, common_patterns
        ):
            kind = "common"
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind=kind,
                path=node_id,
                attributes={
                    "language": _language_for_path(file_path),
                    "imports": _extract_test_imports(file_path),
                },
            ),
        )
    return test_nodes


def _add_design_edges(
    dag: DAG,
    project_root: Path,
    design_docs: dict[str, dict[str, Any]],
    impl_nodes: dict[str, Path],
) -> None:
    design_ids = set(design_docs)
    aliases = next(iter(design_docs.values()), {}).get("aliases", {}) if design_docs else {}

    for node_id, metadata in design_docs.items():
        for dependency in metadata.get("depends_on", []):
            target_id = _resolve_design_dependency(dependency, metadata["path"], project_root, design_ids, aliases)
            if target_id:
                dag.add_edge(Edge(from_id=node_id, to_id=target_id, kind="depends_on"))

        body = str(metadata.get("body", ""))
        for impl_id in impl_nodes:
            if impl_id in body:
                dag.add_edge(Edge(from_id=node_id, to_id=impl_id, kind="expects"))


def _add_import_edges(
    dag: DAG,
    project_root: Path,
    impl_nodes: dict[str, Path],
    settings: dict[str, Any],
) -> None:
    aliases = _load_import_aliases(project_root, settings)
    path_to_node = {path: node_id for node_id, path in impl_nodes.items()}

    for node_id, file_path in impl_nodes.items():
        imports = dag.nodes[node_id].attributes.get("imports", [])
        for import_ref in imports:
            target_id = _resolve_import_target(import_ref, file_path, project_root, path_to_node, aliases)
            if target_id and target_id != node_id:
                dag.add_edge(Edge(from_id=node_id, to_id=target_id, kind="imports"))


def _add_tested_by_edges(
    dag: DAG,
    project_root: Path,
    impl_nodes: dict[str, Path],
    test_nodes: dict[str, Path],
    settings: dict[str, Any],
) -> None:
    path_to_node = {path: node_id for node_id, path in impl_nodes.items()}
    aliases = _load_import_aliases(project_root, settings)
    existing_edges = {(edge.from_id, edge.to_id, edge.kind) for edge in dag.edges}

    for test_id, test_path in test_nodes.items():
        for target_id in _infer_test_targets(test_path, project_root, path_to_node, aliases):
            edge_key = (target_id, test_id, "tested_by")
            if edge_key not in existing_edges:
                dag.add_edge(Edge(from_id=target_id, to_id=test_id, kind="tested_by"))
                existing_edges.add(edge_key)


def _add_plan_tasks(dag: DAG, project_root: Path, settings: dict[str, Any]) -> None:
    plan_path = _project_path(project_root, str(settings.get("plan_task_file", "")))
    if not plan_path.is_file():
        return

    content = plan_path.read_text(encoding="utf-8", errors="ignore")
    matches = list(PLAN_HEADER_RE.finditer(content))
    plan_rel = _relative_id(plan_path, project_root)

    for index, match in enumerate(matches):
        task_id = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section = content[start:end]
        outputs = _extract_outputs(section)
        if not outputs:
            continue

        node_id = f"{plan_path.name}#{task_id}"
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind="plan_task",
                path=plan_rel,
                attributes={
                    "task_id": task_id,
                    "description": (match.group(2) or "").strip(),
                    "expected_outputs": outputs,
                },
            ),
        )
        for output in outputs:
            edge = _plan_task_output_edge(dag, node_id, output)
            if edge is not None:
                dag.add_edge(edge)


def _add_expected_nodes(
    dag: DAG,
    project_root: Path,
    settings: dict[str, Any],
    impl_nodes: dict[str, Path],
) -> None:
    lexicon_path = _project_path(project_root, str(settings.get("lexicon_file", "project_lexicon.yaml")))
    if not lexicon_path.is_file():
        return

    payload = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return
    artifacts = payload.get("required_artifacts", [])
    if not isinstance(artifacts, list):
        return

    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        artifact_id = str(artifact.get("id") or f"artifact-{index + 1}")
        node_id = artifact_id if artifact_id.startswith("lexicon:") else f"lexicon:{artifact_id}"
        _add_node_once(
            dag,
            Node(id=node_id, kind="expected", path=None, attributes=_expected_artifact_attributes(artifact)),
        )
        for target in _artifact_target_paths(artifact):
            target_id = _normalize_output_path(target)
            if target_id in impl_nodes:
                dag.add_edge(Edge(from_id=node_id, to_id=target_id, kind="represents"))


def _add_design_doc_expected_outcome_edges(dag: DAG, design_docs: dict[str, dict[str, Any]]) -> None:
    existing_edges = {
        (edge.from_id, edge.to_id, edge.kind, _edge_attributes_key(edge.attributes))
        for edge in dag.edges
    }

    for node_id, metadata in design_docs.items():
        attributes = metadata.get("attributes", {})
        journey_names = _design_doc_journey_names(attributes)
        for journey in _design_doc_journey_entries(attributes):
            journey_name = journey.get("name") if isinstance(journey.get("name"), str) else None
            for ref in _as_list(journey.get("expected_outcome_refs")):
                if not isinstance(ref, str) or not ref.strip():
                    continue
                ref = ref.strip()
                if ref.startswith("lexicon:"):
                    if ref not in dag.nodes or dag.nodes[ref].kind != "expected":
                        warnings.warn(
                            f"{node_id} user_journeys expected_outcome_refs points to missing lexicon node: {ref}",
                            UserWarning,
                            stacklevel=2,
                        )
                        continue
                    edge_attributes = {"source": "expected_outcome_refs", "ref": ref}
                    if journey_name:
                        edge_attributes["journey"] = journey_name
                    edge_key = (node_id, ref, "expects", _edge_attributes_key(edge_attributes))
                    if edge_key not in existing_edges:
                        dag.add_edge(Edge(from_id=node_id, to_id=ref, kind="expects", attributes=edge_attributes))
                        existing_edges.add(edge_key)
                    continue

                if ref.startswith("design:"):
                    # Same-document journey refs are represented inside the
                    # design_doc attributes, so node-level self edges are skipped.
                    _ = ref.removeprefix("design:") in journey_names
                    continue

                warnings.warn(
                    f"{node_id} user_journeys expected_outcome_refs has unknown prefix: {ref}",
                    UserWarning,
                    stacklevel=2,
                )


def _add_design_doc_expected_extractions(
    dag: DAG,
    project_root: Path,
    settings: dict[str, Any],
    design_docs: dict[str, dict[str, Any]],
) -> None:
    if not design_docs:
        return

    project_config = _load_project_config_or_empty(project_root)
    extraction_settings = _design_doc_extraction_settings(settings, project_config)
    enabled = bool(extraction_settings.get("enabled") or extraction_settings.get("auto_extract"))
    force = bool(extraction_settings.get("force"))

    for node_id, metadata in design_docs.items():
        doc_path = metadata.get("path")
        if not isinstance(doc_path, Path):
            continue
        extraction = None
        if not force:
            extraction = load_cached_expected_extraction(project_root, doc_path)
        if extraction is None and enabled:
            try:
                extraction = extract_expected_artifacts_for_file(
                    doc_path,
                    project_root,
                    config=project_config,
                    force=force,
                )
            except Exception as exc:
                warnings.warn(
                    f"{node_id} expected extraction failed: {exc}",
                    UserWarning,
                    stacklevel=2,
                )
                continue
        if extraction is None:
            continue
        dag.nodes[node_id].attributes["expected_extraction"] = extraction.to_dict()
        _add_expected_extraction_edges(dag, node_id, extraction)


def _design_doc_extraction_settings(settings: dict[str, Any], project_config: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (
        project_config.get("design_doc_extraction"),
        (project_config.get("llm") or {}).get("design_doc_extraction") if isinstance(project_config.get("llm"), dict) else None,
        settings.get("design_doc_extraction"),
    ):
        if isinstance(source, dict):
            merged = _deep_merge(merged, source)
    return merged


def _add_expected_extraction_edges(dag: DAG, design_doc_id: str, extraction: ExpectedExtraction) -> None:
    existing_edges = {
        (edge.from_id, edge.to_id, edge.kind, _edge_attributes_key(edge.attributes))
        for edge in dag.edges
    }

    for expected_node in extraction.expected_nodes:
        target_id = _resolve_expected_hint(dag, expected_node.path_hint)
        if not target_id:
            continue
        attributes = {
            "source": "expected_extraction",
            "path_hint": expected_node.path_hint,
        }
        edge_key = (design_doc_id, target_id, "expects", _edge_attributes_key(attributes))
        if edge_key not in existing_edges:
            dag.add_edge(Edge(from_id=design_doc_id, to_id=target_id, kind="expects", attributes=attributes))
            existing_edges.add(edge_key)

    for expected_edge in extraction.expected_edges:
        from_id = _resolve_expected_hint(dag, expected_edge.from_path_hint)
        to_id = _resolve_expected_hint(dag, expected_edge.to_path_hint)
        if not from_id or not to_id:
            continue
        attributes = {
            "source": "expected_extraction",
            "rationale": expected_edge.rationale,
            **expected_edge.attributes,
        }
        edge_key = (from_id, to_id, expected_edge.kind, _edge_attributes_key(attributes))
        if edge_key not in existing_edges:
            dag.add_edge(Edge(from_id=from_id, to_id=to_id, kind=expected_edge.kind, attributes=attributes))
            existing_edges.add(edge_key)


def _resolve_expected_hint(dag: DAG, path_hint: str) -> str | None:
    hint = _normalize_expected_hint(path_hint)
    if not hint:
        return None
    nodes = sorted(dag.nodes.values(), key=lambda item: item.id)
    for node in nodes:
        for candidate in _expected_node_candidates(node):
            if hint == candidate or hint == Path(candidate).name:
                return node.id
    for node in nodes:
        for candidate in _expected_node_candidates(node):
            if fnmatch.fnmatchcase(candidate, hint):
                return node.id
    for node in nodes:
        for candidate in _expected_node_candidates(node):
            if hint.lower() in candidate.lower() or Path(hint).stem.lower() == Path(candidate).stem.lower():
                return node.id
    return None


def _expected_node_candidates(node: Node) -> list[str]:
    return [_normalize_expected_hint(value) for value in (node.id, node.path) if value]


def _normalize_expected_hint(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _add_deployment_graph(
    dag: DAG,
    project_root: Path,
    design_docs: dict[str, dict[str, Any]],
    impl_nodes: dict[str, Path],
) -> None:
    project_config = _load_project_config_or_empty(project_root)
    deployment_docs = extract_deployment_docs(project_root, project_config)
    runtime_states = extract_runtime_states(
        project_root,
        deployment_docs,
        [{"id": node_id, **metadata} for node_id, metadata in design_docs.items()],
        project_config,
    )
    verification_tests = extract_verification_tests(project_root, project_config, design_docs)

    from codd.deployment.extractor import (
        auto_runtime_states_for_impl,
        discover_deployment_impl_candidates,
    )

    capability_patterns = _capability_patterns(project_config)
    auto_impl_paths: list[Path] = []
    for impl_path in discover_deployment_impl_candidates(project_root, deployment_docs):
        rel_id = impl_path.relative_to(project_root).as_posix()
        if rel_id in dag.nodes:
            continue
        _add_node_once(
            dag,
            Node(
                id=rel_id,
                kind="impl_file",
                path=rel_id,
                attributes={
                    "language": _language_for_path(impl_path),
                    "imports": [],
                    "runtime_evidence": _runtime_evidence_for_file(impl_path, rel_id, capability_patterns),
                    "auto_registered_for_deployment": True,
                },
            ),
        )
        impl_nodes[rel_id] = impl_path.resolve()
        auto_impl_paths.append(impl_path)

    if auto_impl_paths:
        existing_state_ids = {state.identifier for state in runtime_states}
        for runtime_state in auto_runtime_states_for_impl(auto_impl_paths, project_root):
            if runtime_state.identifier in existing_state_ids:
                continue
            runtime_states.append(runtime_state)
            existing_state_ids.add(runtime_state.identifier)

    for deployment_doc in deployment_docs:
        _add_node_once(
            dag,
            Node(
                id=deployment_doc.path,
                kind="deployment_doc",
                path=deployment_doc.path,
                attributes=deployment_doc_attributes(deployment_doc),
            ),
        )
    for runtime_state in runtime_states:
        _add_node_once(
            dag,
            Node(
                id=runtime_state.identifier,
                kind="runtime_state",
                path=None,
                attributes=runtime_state_attributes(runtime_state),
            ),
        )
    for verification_test in verification_tests:
        _add_node_once(
            dag,
            Node(
                id=verification_test.identifier,
                kind="verification_test",
                path=verification_test.expected_outcome.get("source")
                if isinstance(verification_test.expected_outcome, dict)
                else None,
                attributes=verification_test_attributes(verification_test),
            ),
        )

    existing_edges = {
        (edge.from_id, edge.to_id, edge.kind, _edge_attributes_key(edge.attributes))
        for edge in dag.edges
    }
    for from_id, to_id, kind, attributes in infer_deployment_edges(
        project_root,
        deployment_docs,
        runtime_states,
        verification_tests,
        list(impl_nodes),
        design_docs,
    ):
        if from_id not in dag.nodes or to_id not in dag.nodes:
            continue
        edge_key = (from_id, to_id, kind, _edge_attributes_key(attributes))
        if edge_key in existing_edges:
            continue
        dag.add_edge(Edge(from_id=from_id, to_id=to_id, kind=kind, attributes=attributes))
        existing_edges.add(edge_key)


def _attach_coverage_axes(dag: DAG, project_root: Path, settings: dict[str, Any]) -> None:
    lexicon_path = _project_path(project_root, str(settings.get("lexicon_file", "project_lexicon.yaml")))
    axes = extract_coverage_axes_from_lexicon(lexicon_path)
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind == "design_doc":
            axes.extend(extract_coverage_axes_from_design_doc(node))
    dag.coverage_axes = _dedupe_coverage_axes(axes)


def _dedupe_coverage_axes(axes: list[CoverageAxis]) -> list[CoverageAxis]:
    deduped: list[CoverageAxis] = []
    seen: set[tuple[str, tuple[str, ...], str, str]] = set()
    for axis in axes:
        key = (
            axis.axis_type,
            tuple(variant.id for variant in axis.variants),
            axis.source,
            axis.owner_section,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(axis)
    return deduped


def _extract_outputs(section: str) -> list[str]:
    match = OUTPUTS_RE.search(section)
    if not match:
        return []

    rest = match.group(1).strip()
    if rest:
        if rest.startswith("["):
            loaded = yaml.safe_load(rest)
            if isinstance(loaded, list):
                return [str(item) for item in loaded]
        return [item.strip().strip("'\"") for item in rest.split(",") if item.strip()]

    outputs: list[str] = []
    following = section[match.end() :]
    for line in following.splitlines():
        if not line.startswith((" ", "\t")) and line.strip():
            break
        item = line.strip()
        if item.startswith("- "):
            outputs.append(item[2:].strip().strip("'\""))
    return outputs


def _artifact_target_paths(artifact: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for key in ("path", "file", "output", "artifact_path", "value"):
        value = artifact.get(key)
        if isinstance(value, str) and _looks_like_project_path(value):
            targets.append(value)
        elif isinstance(value, list):
            targets.extend(str(item) for item in value if _looks_like_project_path(str(item)))
    return targets


def _expected_artifact_attributes(artifact: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {"source": "project_lexicon.yaml"}
    for key in EXPECTED_ARTIFACT_ATTRIBUTE_KEYS:
        if key in artifact:
            attributes[key] = artifact[key]
    return attributes


def _validate_design_doc_journey_attributes(node_id: str, attributes: dict[str, Any]) -> None:
    required_fields = {
        "runtime_constraints": ("capability", "required", "rationale"),
        "user_journeys": ("name", "criticality", "steps", "required_capabilities", "expected_outcome_refs"),
    }
    for key, required in required_fields.items():
        entries = attributes.get(key, [])
        if not isinstance(entries, list):
            warnings.warn(f"{node_id} {key} must be a list; ignoring validation", UserWarning, stacklevel=2)
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                warnings.warn(f"{node_id} {key}[{index}] must be a mapping", UserWarning, stacklevel=2)
                continue
            missing = [field for field in required if field not in entry]
            if missing:
                warnings.warn(
                    f"{node_id} {key}[{index}] missing required field(s): {', '.join(missing)}",
                    UserWarning,
                    stacklevel=2,
                )


def _design_doc_journey_entries(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    journeys = attributes.get("user_journeys", [])
    return [journey for journey in journeys if isinstance(journey, dict)] if isinstance(journeys, list) else []


def _design_doc_journey_names(attributes: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for journey in _design_doc_journey_entries(attributes):
        name = journey.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _plan_task_output_edge(dag: DAG, task_node_id: str, output: str) -> Edge | None:
    output_id = str(output).strip()
    if output_id.startswith(LEXICON_OUTPUT_PREFIX):
        if output_id not in dag.nodes:
            LOGGER.warning("plan task %s references unknown lexicon expected output: %s", task_node_id, output_id)
            return None
        return Edge(
            from_id=task_node_id,
            to_id=output_id,
            kind="produces",
            attributes=_lexicon_produces_attributes(dag.nodes[output_id]),
        )

    return Edge(from_id=task_node_id, to_id=_normalize_output_path(output_id), kind="produces")


def _lexicon_produces_attributes(expected_node: Node) -> dict[str, Any] | None:
    journey = expected_node.attributes.get("journey")
    if isinstance(journey, str) and journey:
        return {"journey": journey}
    return None


def _edge_attributes_key(attributes: dict[str, Any] | None) -> str:
    return repr(sorted((attributes or {}).items()))


def _resolve_design_dependency(
    dependency: Any,
    md_path: Path,
    project_root: Path,
    design_ids: set[str],
    aliases: dict[str, str],
) -> str | None:
    if isinstance(dependency, dict):
        dependency = dependency.get("path") or dependency.get("id") or dependency.get("node_id")
    if not isinstance(dependency, str) or not dependency.strip():
        return None

    value = dependency.strip()
    if value in design_ids:
        return value
    if value in aliases:
        return aliases[value]

    candidates = [
        _relative_id(_project_path(project_root, value), project_root),
        _relative_id((md_path.parent / value).resolve(), project_root),
    ]
    if not value.endswith(".md"):
        candidates.append(_relative_id((md_path.parent / f"{value}.md").resolve(), project_root))

    for candidate in candidates:
        if candidate in design_ids:
            return candidate
    return None


def _resolve_import_target(
    import_ref: str,
    file_path: Path,
    project_root: Path,
    path_to_node: dict[Path, str],
    aliases: dict[str, list[str]],
) -> str | None:
    candidates: list[Path] = []
    if import_ref.startswith("."):
        candidates.append((file_path.parent / import_ref).resolve())
    elif import_ref.startswith("/"):
        candidates.append((project_root / import_ref.lstrip("/")).resolve())
    else:
        candidates.extend(_alias_candidates(import_ref, project_root, aliases))
        candidates.append((project_root / import_ref).resolve())

    for candidate in candidates:
        resolved = _resolve_file_candidate(candidate, path_to_node)
        if resolved:
            return resolved
    return None


def _infer_test_targets(
    test_path: Path,
    project_root: Path,
    path_to_node: dict[Path, str],
    aliases: dict[str, list[str]],
) -> set[str]:
    targets: set[str] = set()

    for import_ref in _extract_test_imports(test_path):
        target_id = _resolve_import_target(import_ref, test_path, project_root, path_to_node, aliases)
        if not target_id and "." in import_ref and not import_ref.startswith("."):
            target_id = _resolve_python_import_target(import_ref, project_root, path_to_node)
        if target_id:
            targets.add(target_id)

    convention_key = _test_convention_key(test_path, project_root)
    if convention_key:
        targets.update(_match_impl_by_convention(convention_key, path_to_node))
        for candidate in _convention_path_candidates(test_path, project_root, convention_key):
            target_id = _resolve_file_candidate(candidate, path_to_node)
            if target_id:
                targets.add(target_id)

    return targets


def _extract_test_imports(file_path: Path) -> list[str]:
    imports = extract_imports(file_path)
    if file_path.suffix != ".py":
        return imports

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    python_imports = [match.group(1) or match.group(2) for match in PY_IMPORT_RE.finditer(content)]
    return [*imports, *python_imports]


def _resolve_python_import_target(import_ref: str, project_root: Path, path_to_node: dict[Path, str]) -> str | None:
    module_path = (project_root / import_ref.replace(".", "/")).resolve()
    resolved = _resolve_file_candidate(module_path, path_to_node)
    if resolved:
        return resolved

    init_path = module_path / "__init__.py"
    return path_to_node.get(init_path)


def _resolve_file_candidate(candidate: Path, path_to_node: dict[Path, str]) -> str | None:
    if candidate in path_to_node:
        return path_to_node[candidate]

    suffixes = sorted({path.suffix for path in path_to_node})
    for suffix in suffixes:
        suffixed = Path(f"{candidate}{suffix}")
        if suffixed in path_to_node:
            return path_to_node[suffixed]

    for suffix in suffixes:
        indexed = candidate / f"index{suffix}"
        if indexed in path_to_node:
            return path_to_node[indexed]
    init_file = candidate / "__init__.py"
    if init_file in path_to_node:
        return path_to_node[init_file]
    return None


def _test_convention_key(test_path: Path, project_root: Path) -> str | None:
    relative_parts = _relative_id(test_path, project_root).split("/")
    name = test_path.name

    for marker in (".test.", ".spec."):
        if marker in name:
            return name.split(marker, 1)[0]

    if test_path.suffix == ".py" and any(part in {"tests", "test"} for part in relative_parts[:-1]):
        stem = test_path.stem
        if stem.startswith("test_"):
            return stem.removeprefix("test_")
        if stem.endswith("_test"):
            return stem.removesuffix("_test")

    return None


def _match_impl_by_convention(convention_key: str, path_to_node: dict[Path, str]) -> set[str]:
    matches: set[str] = set()
    for _impl_path, node_id in path_to_node.items():
        if convention_key in _impl_convention_tokens(node_id):
            matches.add(node_id)
    return matches


def _impl_convention_tokens(node_id: str) -> set[str]:
    path = Path(node_id)
    parts = [*path.with_suffix("").parts]
    tokens = {path.stem}
    if len(parts) >= 2:
        tokens.add("_".join(parts[-2:]))
    if len(parts) >= 3:
        tokens.add("_".join(parts[-3:]))
    return tokens


def _convention_path_candidates(test_path: Path, project_root: Path, convention_key: str) -> list[Path]:
    candidates: list[Path] = []
    suffix_groups = {
        ".py": [".py"],
        ".rs": [".rs"],
        ".rb": [".rb"],
        ".cs": [".cs"],
        ".kt": [".kt", ".kts"],
        ".swift": [".swift"],
        ".exs": [".ex", ".exs"],
        ".scala": [".scala"],
        ".cpp": [".cpp", ".c", ".h", ".hpp"],
    }
    suffixes = suffix_groups.get(test_path.suffix, [".ts", ".tsx", ".js", ".jsx"])

    if any(marker in test_path.name for marker in (".test.", ".spec.")):
        candidates.append((test_path.parent / convention_key).resolve())

    for root_name in ("codd", "src"):
        for suffix in suffixes:
            candidates.append((project_root / root_name / f"{convention_key}{suffix}").resolve())

    return candidates


def _is_test_file(file_path: Path, project_root: Path) -> bool:
    relative_parts = _relative_id(file_path, project_root).split("/")
    name = file_path.name
    if any(marker in name for marker in (".test.", ".spec.")):
        return True
    if file_path.suffix == ".bats":
        return True
    if file_path.suffix == ".py" and any(part in {"tests", "test"} for part in relative_parts[:-1]):
        return name.startswith("test_") or name.endswith("_test.py")
    return False


def _load_import_aliases(project_root: Path, settings: dict[str, Any]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    configured = settings.get("import_aliases", {})
    if isinstance(configured, dict):
        for pattern, targets in configured.items():
            values = targets if isinstance(targets, list) else [targets]
            aliases[str(pattern)] = [str(value) for value in values]

    tsconfig = project_root / "tsconfig.json"
    if tsconfig.is_file():
        try:
            payload = json.loads(tsconfig.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        paths = payload.get("compilerOptions", {}).get("paths", {})
        if isinstance(paths, dict):
            for pattern, targets in paths.items():
                if isinstance(targets, list):
                    aliases.setdefault(str(pattern), []).extend(str(target) for target in targets)
    return aliases


def _alias_candidates(import_ref: str, project_root: Path, aliases: dict[str, list[str]]) -> list[Path]:
    candidates: list[Path] = []
    for pattern, targets in aliases.items():
        if "*" in pattern:
            prefix, suffix = pattern.split("*", 1)
            if not import_ref.startswith(prefix) or (suffix and not import_ref.endswith(suffix)):
                continue
            wildcard = import_ref[len(prefix) : len(import_ref) - len(suffix) if suffix else len(import_ref)]
            for target in targets:
                candidates.append((project_root / target.replace("*", wildcard)).resolve())
        elif import_ref == pattern:
            for target in targets:
                candidates.append((project_root / target).resolve())
    return candidates


def _glob_project_paths(
    project_root: Path,
    patterns: Any,
    exclude_patterns: Any = None,
) -> list[Path]:
    paths: dict[str, Path] = {}
    for pattern in _as_list(patterns):
        if not isinstance(pattern, str) or not pattern:
            continue
        for expanded in _expand_braces(pattern):
            for path in project_root.glob(expanded):
                paths[str(path.resolve())] = path.resolve()
    excludes = [
        str(pattern)
        for pattern in _as_list(exclude_patterns)
        if isinstance(pattern, str) and pattern.strip()
    ]
    if not excludes:
        return [paths[key] for key in sorted(paths)]
    project_root_resolved = project_root.resolve()
    filtered: list[Path] = []
    for key in sorted(paths):
        path = paths[key]
        if _path_matches_any_pattern(path, project_root_resolved, excludes):
            continue
        filtered.append(path)
    return filtered


def _path_matches_any_pattern(
    path: Path,
    project_root: Path,
    patterns: list[str],
) -> bool:
    try:
        relative = path.resolve().relative_to(project_root)
    except ValueError:
        return False
    rel_text = relative.as_posix()

    for pattern in patterns:
        if _glob_pattern_to_regex(pattern).match(rel_text):
            return True
    return False


_GLOB_REGEX_CACHE: dict[str, "re.Pattern[str]"] = {}


def _glob_pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a glob pattern (with ``**`` recursion) into a regex.

    Recognised tokens:

    * ``**/`` — zero or more path segments (greedy across separators).
    * ``**`` — anything (including ``/``); used at end of a pattern.
    * ``*`` — any chars except ``/``.
    * ``?`` — single char except ``/``.
    * Anything else — literal.

    Patterns such as ``**/dist/**`` match a path containing ``dist`` at any
    depth, ``src/lib/**/*.ts`` matches both ``src/lib/x.ts`` and
    ``src/lib/sub/x.ts``.
    """

    cached = _GLOB_REGEX_CACHE.get(pattern)
    if cached is not None:
        return cached

    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i : i + 3] == "**/":
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    compiled = re.compile("^" + "".join(parts) + "$")
    _GLOB_REGEX_CACHE[pattern] = compiled
    return compiled


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    expanded: list[str] = []
    for value in match.group(1).split(","):
        expanded.extend(_expand_braces(pattern[: match.start()] + value + pattern[match.end() :]))
    return expanded


def _read_default_settings(project_type: str) -> dict[str, Any]:
    path = DEFAULTS_DIR / f"{project_type}.yaml"
    if not path.is_file():
        path = DEFAULTS_DIR / f"{DEFAULT_PROJECT_TYPE}.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _detect_project_type(project_root: Path) -> str:
    root = Path(project_root)
    if (root / "Cargo.toml").is_file():
        return "rust"
    if (root / "Gemfile").is_file():
        return "ruby"
    if (root / "package.json").is_file():
        return "web"
    if (root / "go.mod").is_file():
        return "go"
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        return "csharp"
    if (root / "CMakeLists.txt").is_file() or (
        (root / "Makefile").is_file() and _has_any_file(root, ("*.c", "*.cpp"))
    ):
        return "cpp_embedded"
    if any(root.glob("*.gradle.kts")):
        return "kotlin"
    if (root / "pom.xml").is_file():
        return "java"
    if (root / "build.gradle").is_file() or any(root.glob("*.gradle")):
        if _has_any_file(root, ("*.kt", "*.kts")):
            return "kotlin"
        return "java"
    if (root / "mix.exs").is_file():
        return "elixir"
    if (root / "build.sbt").is_file():
        return "scala"
    if _has_any_file(root, ("*.swift",)):
        return "swift"
    return "generic"


def _has_any_file(project_root: Path, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if any(path.is_file() for path in project_root.rglob(pattern)):
            return True
    return False


def _load_suffix_config(project_root: Path, codd_yaml: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    implementation_suffixes = _suffixes_from_config(codd_yaml, "implementation_suffixes")
    test_suffixes = _suffixes_from_config(codd_yaml, "test_suffixes")
    project_type = _project_type(codd_yaml) or _detect_project_type(project_root)
    defaults = _read_suffix_default_mapping(project_type)

    if implementation_suffixes is None:
        implementation_suffixes = _suffixes_from_config(defaults, "implementation_suffixes")
        if implementation_suffixes is None:
            LOGGER.warning(
                "DAG suffix defaults for project_type=%s missing implementation_suffixes; using legacy fallback",
                project_type,
            )
            implementation_suffixes = LEGACY_IMPLEMENTATION_SUFFIXES
    if test_suffixes is None:
        test_suffixes = _suffixes_from_config(defaults, "test_suffixes")
        if test_suffixes is None:
            LOGGER.warning(
                "DAG suffix defaults for project_type=%s missing test_suffixes; using legacy fallback",
                project_type,
            )
            test_suffixes = LEGACY_TEST_SUFFIXES

    implementation_suffixes = _extend_suffixes(
        implementation_suffixes,
        _suffixes_from_config(codd_yaml, "implementation_suffixes_extend"),
    )
    test_suffixes = _extend_suffixes(
        test_suffixes,
        _suffixes_from_config(codd_yaml, "test_suffixes_extend"),
    )
    return implementation_suffixes, test_suffixes


def _read_suffix_default_mapping(project_type: str) -> dict[str, Any]:
    for suffix_type in (project_type, DEFAULT_PROJECT_TYPE):
        path = DEFAULTS_DIR / f"{suffix_type}.yaml"
        if not path.is_file():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            LOGGER.warning("Could not load DAG suffix defaults from %s: %s", path, exc)
            return {}
        if isinstance(payload, dict):
            return payload
        LOGGER.warning("DAG suffix defaults %s must contain a YAML mapping; using legacy fallback", path)
        return {}
    LOGGER.warning("DAG suffix defaults for project_type=%s not found; using legacy fallback", project_type)
    return {}


def _suffixes_from_config(config: dict[str, Any], key: str) -> tuple[str, ...] | None:
    value = config.get(key)
    if value is None and isinstance(config.get("dag"), dict):
        value = config["dag"].get(key)
    return _suffix_tuple(value)


def _suffix_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    suffixes: list[str] = []
    for item in _as_list(value):
        if not isinstance(item, str) or not item.strip():
            continue
        suffix = item.strip()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix not in suffixes:
            suffixes.append(suffix)
    return tuple(suffixes) if suffixes else None


def _extend_suffixes(base: tuple[str, ...], extensions: tuple[str, ...] | None) -> tuple[str, ...]:
    suffixes = list(base)
    for suffix in extensions or ():
        if suffix not in suffixes:
            suffixes.append(suffix)
    return tuple(suffixes)


def _dag_overrides(config: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    dag_section = config.get("dag", {})
    if isinstance(dag_section, dict):
        overrides = _deep_merge(overrides, _normalize_dag_section(dag_section))

    direct_keys = {
        "design_doc_patterns",
        "impl_file_patterns",
        "test_file_patterns",
        "implementation_suffixes",
        "implementation_suffixes_extend",
        "test_suffixes",
        "test_suffixes_extend",
        "plan_task_file",
        "lexicon_file",
        "import_aliases",
        "design_doc_extraction",
    }
    for key in direct_keys:
        if key in config:
            overrides[key] = deepcopy(config[key])
    return overrides


def _coherence_settings(*configs: dict[str, Any]) -> dict[str, Any]:
    coherence: dict[str, Any] = {"capability_patterns": {}}
    for config in configs:
        section = config.get("coherence", {})
        if isinstance(section, dict):
            coherence = _deep_merge(coherence, section)
    if not isinstance(coherence.get("capability_patterns"), dict):
        coherence["capability_patterns"] = {}
    return coherence


def _extraction_settings(*configs: dict[str, Any]) -> dict[str, Any]:
    extraction: dict[str, Any] = {"frontmatter_alias": {}}
    for config in configs:
        section = config.get("extraction", {})
        if isinstance(section, dict):
            extraction = _deep_merge(extraction, section)
    if not isinstance(extraction.get("frontmatter_alias"), dict):
        extraction["frontmatter_alias"] = {}
    return extraction


def _frontmatter_alias_settings(settings: dict[str, Any]) -> dict[str, str]:
    extraction = settings.get("extraction", {})
    if not isinstance(extraction, dict):
        return {}
    aliases = extraction.get("frontmatter_alias", {})
    if not isinstance(aliases, dict):
        return {}
    return {
        str(alias_key).strip(): str(canonical_key).strip()
        for alias_key, canonical_key in aliases.items()
        if str(alias_key).strip() and str(canonical_key).strip()
    }


def _capability_patterns(settings: dict[str, Any]) -> dict[str, Any]:
    coherence = settings.get("coherence", {})
    if not isinstance(coherence, dict):
        return {}
    patterns = coherence.get("capability_patterns", {})
    return patterns if isinstance(patterns, dict) else {}


def _apply_common_node_patterns(settings: dict[str, Any], config: dict[str, Any]) -> None:
    """Capture ``common_node_patterns`` from project / requested config.

    Recognises both ``common_node_patterns:`` at the top level (preferred,
    sibling of ``scan:``) and ``scan.common_node_patterns:`` (nested under
    scan for users who prefer to colocate scan-related knobs). Both feed
    the same ``settings['common_node_patterns']`` list.
    """

    if not isinstance(config, dict):
        return
    patterns: list[str] = []
    top = config.get("common_node_patterns")
    if isinstance(top, list):
        patterns.extend(
            str(item) for item in top if isinstance(item, str) and item.strip()
        )
    scan = config.get("scan")
    if isinstance(scan, dict):
        nested = scan.get("common_node_patterns")
        if isinstance(nested, list):
            patterns.extend(
                str(item) for item in nested if isinstance(item, str) and item.strip()
            )
    if patterns:
        _extend_unique(settings, "common_node_patterns", patterns)


def _design_doc_declares_common(
    frontmatter: Any, attributes: Any
) -> bool:
    """Return True when a design doc opts into ``kind="common"`` via frontmatter.

    Recognised keys (any one wins):
    * ``codd.node_type`` (preferred, lives under the ``codd:`` block)
    * top-level ``node_type`` (legacy / convenience)
    """

    for source in (frontmatter, attributes):
        if not isinstance(source, dict):
            continue
        node_type = source.get("node_type")
        if isinstance(node_type, str) and node_type.strip().lower() == "common":
            return True
        codd_block = source.get("codd")
        if isinstance(codd_block, dict):
            value = codd_block.get("node_type")
            if isinstance(value, str) and value.strip().lower() == "common":
                return True
    return False


def _common_node_patterns(settings: dict[str, Any]) -> list[str]:
    """Return glob patterns that should be classified as ``kind='common'``.

    Common nodes represent shared infrastructure (DB clients, middleware,
    framework config, generated artifacts) that exist outside any single
    design document's scope. They participate in the DAG so that change
    impact can be traced, but they are exempt from C5 transitive_closure
    unreachable_nodes since requiring every common file to have a parent
    design_doc would force the entire codebase to be re-modelled as a tree.
    """

    raw = settings.get("common_node_patterns")
    if not isinstance(raw, list):
        return []
    expanded: list[str] = []
    for pattern in raw:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        expanded.extend(_expand_braces(pattern.strip()))
    return expanded


def _runtime_evidence_for_file(file_path: Path, node_id: str, capability_patterns: dict[str, Any]) -> list[dict]:
    evidence = scan_capability_evidence(file_path, capability_patterns)
    absolute_prefix = f"{file_path.as_posix()}:"
    relative_prefix = f"{node_id}:"
    for item in evidence:
        line_ref = item.get("line_ref")
        if isinstance(line_ref, str) and line_ref.startswith(absolute_prefix):
            item["line_ref"] = relative_prefix + line_ref.removeprefix(absolute_prefix)
    return evidence


def _apply_scan_patterns(settings: dict[str, Any], config: dict[str, Any]) -> None:
    scan = config.get("scan", {})
    if not isinstance(scan, dict):
        return

    source_dirs = _as_list(scan.get("source_dirs"))
    test_dirs = _as_list(scan.get("test_dirs"))
    doc_dirs = _as_list(scan.get("doc_dirs"))
    exclude_patterns = [
        str(pattern)
        for pattern in _as_list(scan.get("exclude"))
        if isinstance(pattern, str) and pattern.strip()
    ]

    if source_dirs:
        _extend_unique(
            settings,
            "impl_file_patterns",
            _file_patterns_for_dirs(
                source_dirs,
                _suffix_tuple(settings.get("implementation_suffixes")) or LEGACY_IMPLEMENTATION_SUFFIXES,
            ),
        )
    if test_dirs:
        _extend_unique(
            settings,
            "test_file_patterns",
            _file_patterns_for_dirs(test_dirs, _suffix_tuple(settings.get("test_suffixes")) or LEGACY_TEST_SUFFIXES),
        )
    if doc_dirs:
        _extend_unique(settings, "design_doc_patterns", _file_patterns_for_dirs(doc_dirs, (".md",)))
    if exclude_patterns:
        _extend_unique(settings, "scan_exclude_patterns", exclude_patterns)


def _normalize_dag_section(section: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(section)
    node_extraction = normalized.pop("node_extraction", None)
    if isinstance(node_extraction, dict):
        if "design_glob" in node_extraction:
            normalized["design_doc_patterns"] = node_extraction["design_glob"]
        if "impl_glob" in node_extraction:
            normalized["impl_file_patterns"] = node_extraction["impl_glob"]
        if "test_glob" in node_extraction:
            normalized["test_file_patterns"] = node_extraction["test_glob"]
        if "plan_path" in node_extraction:
            normalized["plan_task_file"] = node_extraction["plan_path"]
    return normalized


def _project_type(config: dict[str, Any]) -> str | None:
    value = config.get("project_type")
    if isinstance(value, str) and value:
        return value
    project = config.get("project", {})
    if isinstance(project, dict):
        value = project.get("type") or project.get("project_type")
        if isinstance(value, str) and value:
            return value
    return None


def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _file_patterns_for_dirs(dirs: list[Any], suffixes: tuple[str, ...]) -> list[str]:
    patterns: list[str] = []
    for directory in dirs:
        if not isinstance(directory, str) or not directory.strip():
            continue
        base = directory.strip().strip("/")
        if not base or base == ".":
            base = "**"
        for suffix in suffixes:
            patterns.append(f"{base}/**/*{suffix}")
    return patterns


def _extend_unique(settings: dict[str, Any], key: str, values: list[str]) -> None:
    current = [str(item) for item in _as_list(settings.get(key)) if item]
    for value in values:
        if value not in current:
            current.append(value)
    settings[key] = current


def _add_node_once(dag: DAG, node: Node) -> None:
    if node.id not in dag.nodes:
        dag.add_node(node)


def _relative_id(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _project_path(project_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


def _normalize_output_path(path_text: str) -> str:
    return Path(path_text).as_posix().lstrip("./")


def _language_for_path(path: Path) -> str:
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".java": "java",
        ".rs": "rust",
        ".rb": "ruby",
        ".cs": "csharp",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".swift": "swift",
        ".ex": "elixir",
        ".exs": "elixir",
        ".scala": "scala",
    }.get(path.suffix, "unknown")


def _looks_like_project_path(value: str) -> bool:
    generic_suffixes, _ = _load_suffix_config(Path.cwd(), {"project_type": DEFAULT_PROJECT_TYPE})
    return "/" in value and Path(value).suffix in generic_suffixes


def _mermaid_id(node_id: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _escape_mermaid(value: str) -> str:
    return value.replace('"', '\\"')

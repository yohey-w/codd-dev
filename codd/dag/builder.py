"""Build a project-wide DAG for completeness checks."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config
from codd.dag import DAG, Edge, Node
from codd.dag.extractor import extract_design_doc_metadata, extract_imports, scan_capability_evidence
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
DEFAULT_PROJECT_TYPE = "web"
IMPLEMENTATION_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java")
TEST_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".bats")
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
    _add_import_edges(dag, root, impl_nodes, dag_settings)
    _add_tested_by_edges(dag, root, impl_nodes, test_nodes, dag_settings)
    _add_expected_nodes(dag, root, dag_settings, impl_nodes)
    _add_plan_tasks(dag, root, dag_settings)
    _add_deployment_graph(dag, root, design_docs, impl_nodes)

    write_dag_json(dag, root, default_dag_json_path(root))
    return dag


def load_dag_settings(project_root: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load project-type defaults and apply ``codd.yaml dag:`` overrides."""

    project_config = _load_project_config_or_empty(project_root)

    requested_settings = settings or {}
    project_type = _project_type(requested_settings) or _project_type(project_config) or DEFAULT_PROJECT_TYPE
    merged = _read_default_settings(project_type)
    merged = _deep_merge(merged, _dag_overrides(project_config))
    merged = _deep_merge(merged, _dag_overrides(requested_settings))
    _apply_scan_patterns(merged, project_config)
    _apply_scan_patterns(merged, requested_settings)
    merged["coherence"] = _coherence_settings(project_config, requested_settings)
    merged["project_type"] = project_type
    merged.setdefault("design_doc_patterns", [])
    merged.setdefault("impl_file_patterns", [])
    merged.setdefault("test_file_patterns", [])
    merged.setdefault("plan_task_file", "docs/design/implementation_plan.md")
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

    return {
        "version": "1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(Path(project_root).resolve()),
        "nodes": [
            {
                "id": node.id,
                "kind": node.kind,
                "path": node.path,
                "attributes": node.attributes,
            }
            for node in sorted(dag.nodes.values(), key=lambda item: item.id)
        ],
        "edges": [_edge_to_dict(edge) for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind))],
        "cycles": dag.detect_cycles(),
    }


def _edge_to_dict(edge: Edge) -> dict[str, Any]:
    payload = {
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "kind": edge.kind,
    }
    if edge.attributes:
        payload["attributes"] = edge.attributes
    return payload


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
    for md_path in _glob_project_paths(project_root, settings.get("design_doc_patterns", [])):
        if not md_path.is_file():
            continue
        node_id = _relative_id(md_path, project_root)
        metadata = extract_design_doc_metadata(md_path)
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind="design_doc",
                path=node_id,
                attributes={
                    "frontmatter": metadata["frontmatter"],
                    "depends_on": metadata["depends_on"],
                    "node_id": metadata.get("node_id"),
                    **(metadata.get("attributes") or {}),
                },
            ),
        )
        design_docs[node_id] = {**metadata, "path": md_path}
        if metadata.get("node_id"):
            aliases[str(metadata["node_id"])] = node_id

    for metadata in design_docs.values():
        metadata["aliases"] = aliases
    return design_docs


def _add_impl_files(dag: DAG, project_root: Path, settings: dict[str, Any]) -> dict[str, Path]:
    impl_nodes: dict[str, Path] = {}
    capability_patterns = _capability_patterns(settings)
    for file_path in _glob_project_paths(project_root, settings.get("impl_file_patterns", [])):
        if (
            not file_path.is_file()
            or file_path.suffix not in IMPLEMENTATION_SUFFIXES
            or _is_test_file(file_path, project_root)
        ):
            continue
        node_id = _relative_id(file_path, project_root)
        impl_nodes[node_id] = file_path.resolve()
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind="impl_file",
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
    for file_path in _glob_project_paths(project_root, settings.get("test_file_patterns", [])):
        if not file_path.is_file() or file_path.suffix not in TEST_SUFFIXES or not _is_test_file(file_path, project_root):
            continue
        node_id = _relative_id(file_path, project_root)
        test_nodes[node_id] = file_path.resolve()
        _add_node_once(
            dag,
            Node(
                id=node_id,
                kind="test_file",
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
    )
    verification_tests = extract_verification_tests(project_root)

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
    suffixes = [".py"] if test_path.suffix == ".py" else [".ts", ".tsx", ".js", ".jsx"]

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


def _glob_project_paths(project_root: Path, patterns: Any) -> list[Path]:
    paths: dict[str, Path] = {}
    for pattern in _as_list(patterns):
        if not isinstance(pattern, str) or not pattern:
            continue
        for expanded in _expand_braces(pattern):
            for path in project_root.glob(expanded):
                paths[str(path.resolve())] = path.resolve()
    return [paths[key] for key in sorted(paths)]


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


def _dag_overrides(config: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    dag_section = config.get("dag", {})
    if isinstance(dag_section, dict):
        overrides = _deep_merge(overrides, _normalize_dag_section(dag_section))

    direct_keys = {
        "design_doc_patterns",
        "impl_file_patterns",
        "test_file_patterns",
        "plan_task_file",
        "lexicon_file",
        "import_aliases",
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


def _capability_patterns(settings: dict[str, Any]) -> dict[str, Any]:
    coherence = settings.get("coherence", {})
    if not isinstance(coherence, dict):
        return {}
    patterns = coherence.get("capability_patterns", {})
    return patterns if isinstance(patterns, dict) else {}


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

    if source_dirs:
        _extend_unique(
            settings,
            "impl_file_patterns",
            _file_patterns_for_dirs(source_dirs, IMPLEMENTATION_SUFFIXES),
        )
    if test_dirs:
        _extend_unique(
            settings,
            "test_file_patterns",
            _file_patterns_for_dirs(test_dirs, TEST_SUFFIXES),
        )
    if doc_dirs:
        _extend_unique(settings, "design_doc_patterns", _file_patterns_for_dirs(doc_dirs, (".md",)))


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
    }.get(path.suffix, "unknown")


def _looks_like_project_path(value: str) -> bool:
    return "/" in value and Path(value).suffix in IMPLEMENTATION_SUFFIXES


def _mermaid_id(node_id: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _escape_mermaid(value: str) -> str:
    return value.replace('"', '\\"')

"""Build a project-wide DAG for completeness checks."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config
from codd.dag import DAG, Edge, Node
from codd.dag.extractor import extract_design_doc_metadata, extract_imports


DEFAULTS_DIR = Path(__file__).parent / "defaults"
DEFAULT_PROJECT_TYPE = "web"
IMPLEMENTATION_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java")
PLAN_HEADER_RE = re.compile(r"^#{2,6}\s+([A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)(?:\s+(.+))?$", re.MULTILINE)
OUTPUTS_RE = re.compile(r"(?im)^outputs?[ \t]*:[ \t]*(.*)$")


def build_dag(project_root: Path, settings: dict[str, Any] | None = None) -> DAG:
    """Scan ``project_root`` and write ``.codd/dag.json``."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project root not found: {root}")

    dag_settings = load_dag_settings(root, settings)

    dag = DAG()
    design_docs = _add_design_docs(dag, root, dag_settings)
    impl_nodes = _add_impl_files(dag, root, dag_settings)

    _add_design_edges(dag, root, design_docs, impl_nodes)
    _add_import_edges(dag, root, impl_nodes, dag_settings)
    _add_plan_tasks(dag, root, dag_settings)
    _add_expected_nodes(dag, root, dag_settings, impl_nodes)

    write_dag_json(dag, root, default_dag_json_path(root))
    return dag


def load_dag_settings(project_root: Path, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load project-type defaults and apply ``codd.yaml dag:`` overrides."""

    project_config: dict[str, Any] = {}
    try:
        project_config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        project_config = {}

    requested_settings = settings or {}
    project_type = _project_type(requested_settings) or _project_type(project_config) or DEFAULT_PROJECT_TYPE
    merged = _read_default_settings(project_type)
    merged = _deep_merge(merged, _dag_overrides(project_config))
    merged = _deep_merge(merged, _dag_overrides(requested_settings))
    merged["project_type"] = project_type
    merged.setdefault("design_doc_patterns", [])
    merged.setdefault("impl_file_patterns", [])
    merged.setdefault("plan_task_file", "docs/design/implementation_plan.md")
    merged.setdefault("lexicon_file", "project_lexicon.yaml")
    return merged


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
        "edges": [
            {
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "kind": edge.kind,
            }
            for edge in sorted(dag.edges, key=lambda item: (item.from_id, item.to_id, item.kind))
        ],
        "cycles": dag.detect_cycles(),
    }


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
    for file_path in _glob_project_paths(project_root, settings.get("impl_file_patterns", [])):
        if not file_path.is_file() or file_path.suffix not in IMPLEMENTATION_SUFFIXES:
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
                },
            ),
        )
    return impl_nodes


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
            dag.add_edge(Edge(from_id=node_id, to_id=_normalize_output_path(output), kind="produces"))


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
            Node(id=node_id, kind="expected", path=None, attributes={"source": "project_lexicon.yaml", **artifact}),
        )
        for target in _artifact_target_paths(artifact):
            target_id = _normalize_output_path(target)
            if target_id in impl_nodes:
                dag.add_edge(Edge(from_id=node_id, to_id=target_id, kind="represents"))


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
    return None


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
        "plan_task_file",
        "lexicon_file",
        "import_aliases",
    }
    for key in direct_keys:
        if key in config:
            overrides[key] = deepcopy(config[key])
    return overrides


def _normalize_dag_section(section: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(section)
    node_extraction = normalized.pop("node_extraction", None)
    if isinstance(node_extraction, dict):
        if "design_glob" in node_extraction:
            normalized["design_doc_patterns"] = node_extraction["design_glob"]
        if "impl_glob" in node_extraction:
            normalized["impl_file_patterns"] = node_extraction["impl_glob"]
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

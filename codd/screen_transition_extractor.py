"""Extract screen-to-screen transitions from source code via tree-sitter AST."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass
class ScreenTransition:
    """A navigational edge between two screen routes."""

    from_route: str
    to_route: str
    trigger: str
    kind: str
    source_file: str = ""
    source_line: int = 0


_SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}
_IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "coverage",
    "dist",
    "node_modules",
}


def load_transition_patterns(project_root: Path) -> list[dict[str, Any]]:
    """Load default transition patterns plus project-local overrides."""
    project_root = Path(project_root)
    transition_config = _load_project_screen_transition_config(project_root)
    defaults = [] if transition_config.get("replace_defaults") else _load_default_transition_patterns()
    overrides = _normalize_patterns(transition_config.get("patterns"))
    custom = _normalize_patterns(transition_config.get("custom"))
    return _dedupe_patterns([*defaults, *overrides, *custom])


def extract_transitions(project_root: Path, src_dirs: list[str] | None = None) -> list[ScreenTransition]:
    """Scan source files for screen transitions using tree-sitter AST."""
    project_root = Path(project_root).resolve()
    patterns = load_transition_patterns(project_root)
    if not patterns:
        return []

    route_map = _build_route_map(project_root)
    transitions: list[ScreenTransition] = []

    for source_file in _iter_source_files(project_root, src_dirs):
        content = source_file.read_text(encoding="utf-8", errors="ignore")
        root_node = _parse_source_file(source_file, content)
        if root_node is None:
            continue

        content_bytes = content.encode("utf-8", errors="ignore")
        from_route = route_map.get(source_file.resolve()) or _infer_route_from_path(project_root, source_file)
        for node in _iter_named_nodes(root_node):
            for pattern in patterns:
                transition = _match_transition_pattern(
                    pattern,
                    node,
                    content_bytes,
                    from_route=from_route,
                    source_file=_display_path(source_file, project_root),
                )
                if transition is not None:
                    transitions.append(transition)

    transitions.sort(key=lambda item: (item.source_file, item.source_line, item.from_route, item.to_route, item.kind))
    return transitions


def write_screen_transitions_yaml(transitions: list[ScreenTransition], output_path: Path) -> None:
    """Write extracted transitions to docs/extracted/screen-transitions.yaml."""
    data = {
        "edges": [
            {
                "from": transition.from_route,
                "to": transition.to_route,
                "trigger": transition.trigger,
                "type": transition.kind,
                "source_file": transition.source_file,
                "source_line": transition.source_line,
            }
            for transition in transitions
        ]
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _load_default_transition_patterns() -> list[dict[str, Any]]:
    defaults_path = Path(__file__).parent / "screen_transitions" / "defaults.yaml"
    if not defaults_path.exists():
        return []
    defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    patterns: list[dict[str, Any]] = []
    for framework_config in (defaults.get("frameworks") or {}).values():
        if isinstance(framework_config, dict):
            patterns.extend(_normalize_patterns(framework_config.get("patterns")))
    return patterns


def _load_project_screen_transition_config(project_root: Path) -> dict[str, Any]:
    config = _load_project_codd_yaml(project_root)
    transition_config = config.get("screen_transitions", {})
    return transition_config if isinstance(transition_config, dict) else {}


def _load_project_codd_yaml(project_root: Path) -> dict[str, Any]:
    try:
        from codd.config import find_codd_dir
    except ImportError:
        return {}

    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return {}
    config_path = codd_dir / "codd.yaml"
    if not config_path.exists():
        return {}
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _normalize_patterns(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dedupe_patterns(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for pattern in patterns:
        key = tuple(sorted((str(name), str(value)) for name, value in pattern.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(pattern)
    return unique


def _build_route_map(project_root: Path) -> dict[Path, str]:
    try:
        from codd.config import load_project_config
        from codd.parsing import FileSystemRouteExtractor
    except ImportError:
        return {}

    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}

    route_configs = config.get("filesystem_routes", [])
    if not isinstance(route_configs, list):
        return {}

    info = FileSystemRouteExtractor().extract_routes(project_root, route_configs)
    route_map: dict[Path, str] = {}
    for route in info.routes:
        file_path = route.get("file")
        url = route.get("url")
        if file_path and url:
            route_map[Path(file_path).resolve()] = str(url)
    return route_map


def _iter_source_files(project_root: Path, src_dirs: list[str] | None):
    roots = _source_roots(project_root, src_dirs)
    seen: set[Path] = set()
    for source_root in roots:
        if not source_root.exists():
            continue
        if source_root.is_file():
            if source_root.suffix in _SOURCE_EXTENSIONS:
                resolved = source_root.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield resolved
            continue

        for root, dirs, files in os.walk(source_root):
            dirs[:] = [directory for directory in dirs if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".")]
            for filename in sorted(files):
                file_path = Path(root) / filename
                if file_path.suffix not in _SOURCE_EXTENSIONS:
                    continue
                resolved = file_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield resolved


def _source_roots(project_root: Path, src_dirs: list[str] | None) -> list[Path]:
    if src_dirs:
        return [_resolve_project_path(project_root, item) for item in src_dirs if item]

    config = _load_project_codd_yaml(project_root)
    candidates: list[str] = []
    configured_source_dirs = config.get("source_dirs")
    if isinstance(configured_source_dirs, list):
        candidates.extend(str(item) for item in configured_source_dirs if item)

    route_configs = config.get("filesystem_routes")
    if isinstance(route_configs, list):
        for route_config in route_configs:
            if isinstance(route_config, dict) and route_config.get("base_dir"):
                candidates.append(str(route_config["base_dir"]))

    if not candidates:
        candidates = ["src", "app", "pages"]

    roots = [_resolve_project_path(project_root, candidate) for candidate in candidates]
    return roots if any(root.exists() for root in roots) else [project_root]


def _resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def _parse_source_file(source_file: Path, content: str):
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_typescript
    except ImportError:
        return None

    parser = Parser()
    if source_file.suffix in {".jsx", ".tsx"}:
        parser.language = Language(tree_sitter_typescript.language_tsx())
    else:
        parser.language = Language(tree_sitter_typescript.language_typescript())
    return parser.parse(content.encode("utf-8", errors="ignore")).root_node


def _match_transition_pattern(
    pattern: dict[str, Any],
    node: Any,
    content_bytes: bytes,
    *,
    from_route: str,
    source_file: str,
) -> ScreenTransition | None:
    ast_node = str(pattern.get("ast_node", ""))
    if ast_node == "jsx_element" and node.type in {"jsx_element", "jsx_self_closing_element"}:
        return _match_jsx_link_pattern(pattern, node, content_bytes, from_route=from_route, source_file=source_file)
    if ast_node == "call_expression" and node.type == "call_expression":
        return _match_call_pattern(pattern, node, content_bytes, from_route=from_route, source_file=source_file)
    if ast_node == "member_expression" and node.type == "member_expression":
        return _match_member_pattern(pattern, node, content_bytes, from_route=from_route, source_file=source_file)
    return None


def _match_jsx_link_pattern(
    pattern: dict[str, Any],
    node: Any,
    content_bytes: bytes,
    *,
    from_route: str,
    source_file: str,
) -> ScreenTransition | None:
    opening = _jsx_opening_node(node)
    if opening is None:
        return None

    element_name = _jsx_element_name(opening, content_bytes)
    expected_element = pattern.get("element")
    if expected_element and element_name != expected_element:
        return None

    attr_name = str(pattern.get("attr", ""))
    if not attr_name:
        return None

    to_route = _jsx_attr_route(opening, attr_name, content_bytes)
    if not to_route:
        return None

    return ScreenTransition(
        from_route=from_route,
        to_route=to_route,
        trigger=f"{element_name or 'jsx'}[{attr_name}]",
        kind=str(pattern.get("kind", "link")),
        source_file=source_file,
        source_line=_source_line(node),
    )


def _match_call_pattern(
    pattern: dict[str, Any],
    node: Any,
    content_bytes: bytes,
    *,
    from_route: str,
    source_file: str,
) -> ScreenTransition | None:
    callee = _callee_name(node.child_by_field_name("function"), content_bytes)
    if callee != pattern.get("callee"):
        return None

    args = _call_arguments_node(node)
    url_arg = pattern.get("url_arg")
    to_route = _object_pair_route(args, str(url_arg), content_bytes) if url_arg else None
    if not to_route:
        to_route = _first_route_literal(args or node, content_bytes)
    if not to_route:
        return None

    return ScreenTransition(
        from_route=from_route,
        to_route=to_route,
        trigger=f"{callee}()",
        kind=str(pattern.get("kind", "redirect")),
        source_file=source_file,
        source_line=_source_line(node),
    )


def _match_member_pattern(
    pattern: dict[str, Any],
    node: Any,
    content_bytes: bytes,
    *,
    from_route: str,
    source_file: str,
) -> ScreenTransition | None:
    object_node = node.child_by_field_name("object")
    property_node = node.child_by_field_name("property")
    object_name = _node_text(content_bytes, object_node).strip()
    property_name = _node_text(content_bytes, property_node).strip()
    if object_name != pattern.get("object") or property_name != pattern.get("property"):
        return None

    call_node = node.parent if node.parent is not None and node.parent.type == "call_expression" else None
    to_route = _first_route_literal(call_node or node, content_bytes)
    if not to_route:
        return None

    return ScreenTransition(
        from_route=from_route,
        to_route=to_route,
        trigger=f"{object_name}.{property_name}()",
        kind=str(pattern.get("kind", "redirect")),
        source_file=source_file,
        source_line=_source_line(node),
    )


def _jsx_opening_node(node: Any):
    if node.type == "jsx_self_closing_element":
        return node
    for child in node.named_children:
        if child.type == "jsx_opening_element":
            return child
    return None


def _jsx_element_name(opening: Any, content_bytes: bytes) -> str:
    for child in opening.named_children:
        if child.type in {"identifier", "member_expression", "nested_identifier"}:
            return _node_text(content_bytes, child).strip()
    return ""


def _jsx_attr_route(opening: Any, attr_name: str, content_bytes: bytes) -> str | None:
    for attr in opening.named_children:
        if attr.type != "jsx_attribute":
            continue
        children = list(attr.named_children)
        if not children:
            continue
        name = _node_text(content_bytes, children[0]).strip()
        if name != attr_name:
            continue
        return _first_route_literal(attr, content_bytes)
    return None


def _call_arguments_node(node: Any):
    for child in node.named_children:
        if child.type == "arguments":
            return child
    return None


def _callee_name(func_node: Any, content_bytes: bytes) -> str:
    if func_node is None:
        return ""
    if func_node.type in {"identifier", "property_identifier"}:
        return _node_text(content_bytes, func_node).strip()
    if func_node.type in {"member_expression", "optional_chain"}:
        object_text = _callee_name(func_node.child_by_field_name("object"), content_bytes)
        property_text = _node_text(content_bytes, func_node.child_by_field_name("property")).strip()
        return f"{object_text}.{property_text}" if object_text and property_text else _node_text(content_bytes, func_node).strip()
    return _node_text(content_bytes, func_node).strip()


def _object_pair_route(node: Any, key: str, content_bytes: bytes) -> str | None:
    if node is None or not key:
        return None
    for child in _iter_named_nodes(node):
        if child.type != "pair":
            continue
        named_children = list(child.named_children)
        if len(named_children) < 2:
            continue
        pair_key = _node_text(content_bytes, named_children[0]).strip().strip("'\"")
        if pair_key != key:
            continue
        route = _first_route_literal(named_children[1], content_bytes)
        if route:
            return route
    return None


def _first_route_literal(node: Any, content_bytes: bytes) -> str | None:
    if node is None:
        return None
    for child in _iter_named_nodes(node):
        if child.type not in {"string", "template_string"}:
            continue
        route = _normalize_route_literal(_node_text(content_bytes, child))
        if route:
            return route
    return None


def _normalize_route_literal(value: str) -> str | None:
    text = value.strip().strip("'\"`").strip()
    if not text or "${" in text:
        return None
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        text = parsed.path or "/"
    if not text.startswith("/") or text.startswith("//"):
        return None
    return re.split(r"[?#]", text, maxsplit=1)[0] or "/"


def _infer_route_from_path(project_root: Path, source_file: Path) -> str:
    try:
        relative = source_file.relative_to(project_root)
    except ValueError:
        return "/"

    parts = list(relative.with_suffix("").parts)
    if not parts:
        return "/"
    for root_marker in ("src", "app", "pages", "routes"):
        if root_marker in parts:
            parts = parts[parts.index(root_marker) + 1 :]
            break
    if parts and parts[-1] in {"page", "index", "route", "+page", "+server"}:
        parts = parts[:-1]
    cleaned = [_normalize_route_segment(part) for part in parts if _normalize_route_segment(part)]
    return "/" + "/".join(cleaned) if cleaned else "/"


def _normalize_route_segment(segment: str) -> str:
    if segment.startswith("(") and segment.endswith(")"):
        return ""
    match = re.fullmatch(r"\[(?:\.{3})?(.+)\]", segment)
    if match:
        return f":{match.group(1)}"
    if segment.startswith("$"):
        return f":{segment[1:]}"
    return segment


def _node_text(content_bytes: bytes, node: Any) -> str:
    if node is None:
        return ""
    return content_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _iter_named_nodes(node: Any):
    yield node
    for child in getattr(node, "named_children", []):
        yield from _iter_named_nodes(child)


def _source_line(node: Any) -> int:
    point = node.start_point
    row = getattr(point, "row", point[0])
    return int(row) + 1


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()

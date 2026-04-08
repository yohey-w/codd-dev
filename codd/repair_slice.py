"""Minimal Repair Slice Generator — function-centered repair context for patch generation.

Produces compact, structured context for LLM-based bug fixing:
- Top-N functions per file, ranked by relevance to issue text
- Caller/callee edges, inheritance, test coverage, contract guards
- ~150-250 tokens per function (no prose, no AI calls)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.extractor import CallEdge, ModuleInfo, ProjectFacts, Symbol, extract_facts


@dataclass
class FunctionSlice:
    name: str                       # "ClassName.method" or "function_name"
    file: str                       # relative file path
    line_start: int
    line_end: int
    signature: str                  # "def method(self, arg1: str) -> bool:"
    relevance_score: float = 0.0
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    overrides: str | None = None
    inherited_by: list[str] = field(default_factory=list)
    test_refs: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    return_type: str = ""
    is_public: bool = False
    is_async: bool = False
    decorators: list[str] = field(default_factory=list)


@dataclass
class RepairSlice:
    file: str
    module_name: str
    functions: list[FunctionSlice] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Function line ranges via Tree-sitter (includes private functions)
# ---------------------------------------------------------------------------

def extract_function_line_ranges(
    content: str, file_path: str, language: str,
) -> dict[str, tuple[int, int]]:
    """Extract {scoped_name: (line_start, line_end)} for all functions/methods.

    Unlike the main extractor, this includes private/underscore functions
    because bugs are often in private methods.
    """
    try:
        from codd.parsing import get_extractor, TreeSitterExtractor
        ext = get_extractor(language)
        if not isinstance(ext, TreeSitterExtractor):
            return _extract_line_ranges_regex(content, language)
        parser = ext._get_parser()
        tree = parser.parse(content.encode("utf-8", errors="ignore"))
        return _walk_functions_for_ranges(tree.root_node, content, language)
    except Exception:
        return _extract_line_ranges_regex(content, language)


def _walk_functions_for_ranges(
    root: Any, content: str, language: str,
) -> dict[str, tuple[int, int]]:
    """Walk AST to find all function definitions with their line ranges."""
    from codd.parsing import _iter_named_nodes, _field_text
    content_bytes = content.encode("utf-8", errors="ignore")
    ranges: dict[str, tuple[int, int]] = {}

    func_types = {"function_definition"}
    if language in ("typescript", "javascript"):
        func_types.update({"method_definition", "function_declaration"})

    for node in _iter_named_nodes(root):
        if node.type not in func_types:
            continue
        name = _field_text(content_bytes, node, "name")
        if not name:
            continue
        # Build scoped name (ClassName.method)
        scope = _parent_class_name(content_bytes, node)
        scoped = f"{scope}.{name}" if scope else name
        ranges[scoped] = (node.start_point.row + 1, node.end_point.row + 1)

    return ranges


def _parent_class_name(content_bytes: bytes, node: Any) -> str:
    """Find enclosing class name for a function node."""
    from codd.parsing import _field_text
    current = node.parent
    while current is not None:
        if current.type == "class_definition":
            return _field_text(content_bytes, current, "name")
        current = current.parent
    return ""


def _extract_line_ranges_regex(content: str, language: str) -> dict[str, tuple[int, int]]:
    """Fallback: regex-based line range extraction."""
    ranges: dict[str, tuple[int, int]] = {}
    if language == "python":
        pattern = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(', re.MULTILINE)
    else:
        pattern = re.compile(r'^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(', re.MULTILINE)

    lines = content.split("\n")
    matches = list(pattern.finditer(content))
    for i, m in enumerate(matches):
        line_no = content[:m.start()].count("\n") + 1
        name = m.group(2) if language == "python" else m.group(1)
        # Estimate end: next function or end of file
        if i + 1 < len(matches):
            end_line = content[:matches[i + 1].start()].count("\n")
        else:
            end_line = len(lines)
        ranges[name] = (line_no, end_line)
    return ranges


# ---------------------------------------------------------------------------
# 2. Extract raises from function bodies
# ---------------------------------------------------------------------------

def extract_raises(content: str, file_path: str, language: str) -> dict[str, list[str]]:
    """Extract {scoped_name: [ExceptionType, ...]} for each function."""
    try:
        from codd.parsing import get_extractor, TreeSitterExtractor
        ext = get_extractor(language)
        if not isinstance(ext, TreeSitterExtractor):
            return _extract_raises_regex(content, language)
        parser = ext._get_parser()
        tree = parser.parse(content.encode("utf-8", errors="ignore"))
        return _walk_raises(tree.root_node, content, language)
    except Exception:
        return _extract_raises_regex(content, language)


def _walk_raises(root: Any, content: str, language: str) -> dict[str, list[str]]:
    from codd.parsing import _iter_named_nodes, _field_text, _node_text
    content_bytes = content.encode("utf-8", errors="ignore")
    result: dict[str, list[str]] = {}

    for node in _iter_named_nodes(root):
        if node.type != "raise_statement":
            continue
        # Find enclosing function
        func_name = _enclosing_function(content_bytes, node)
        if not func_name:
            continue
        # Extract exception type
        exc_type = ""
        for child in node.children:
            if child.type == "call":
                func_node = child.child_by_field_name("function")
                if func_node:
                    exc_type = _node_text(content_bytes, func_node).strip()
                    break
            elif child.type == "identifier":
                exc_type = _node_text(content_bytes, child).strip()
                break
        if exc_type:
            result.setdefault(func_name, [])
            if exc_type not in result[func_name]:
                result[func_name].append(exc_type)

    return result


def _enclosing_function(content_bytes: bytes, node: Any) -> str:
    """Find the scoped name of the enclosing function."""
    from codd.parsing import _field_text
    current = node.parent
    func_name = ""
    while current is not None:
        if current.type in ("function_definition", "method_definition", "function_declaration"):
            func_name = _field_text(content_bytes, current, "name")
            break
        current = current.parent
    if not func_name:
        return ""
    # Get class scope
    scope = _parent_class_name(content_bytes, current)
    return f"{scope}.{func_name}" if scope else func_name


def _extract_raises_regex(content: str, language: str) -> dict[str, list[str]]:
    """Fallback regex for raises extraction."""
    result: dict[str, list[str]] = {}
    if language != "python":
        return result
    current_func = ""
    for line in content.split("\n"):
        m = re.match(r'\s*(?:async\s+)?def\s+(\w+)\s*\(', line)
        if m:
            current_func = m.group(1)
        if current_func:
            rm = re.match(r'\s+raise\s+(\w+)', line)
            if rm:
                exc = rm.group(1)
                result.setdefault(current_func, [])
                if exc not in result[current_func]:
                    result[current_func].append(exc)
    return result


# ---------------------------------------------------------------------------
# 3. Relevance scoring
# ---------------------------------------------------------------------------

def score_functions(
    function_names: list[str],
    issue_text: str,
) -> dict[str, float]:
    """Score functions by relevance to issue text. Returns {name: 0.0-1.0}."""
    if not issue_text or not function_names:
        return {n: 0.5 for n in function_names}

    issue_lower = issue_text.lower()
    # Tokenize issue into words
    issue_words = set(re.findall(r'[a-z_][a-z0-9_]*', issue_lower))

    scores: dict[str, float] = {}
    for name in function_names:
        score = 0.0
        parts = name.lower().replace(".", "_").split("_")
        parts = [p for p in parts if p and len(p) > 2]

        # Exact function name substring match (strongest signal)
        bare = name.split(".")[-1].lower()
        if bare in issue_lower:
            score += 3.0
        # Class name match
        if "." in name:
            cls = name.split(".")[0].lower()
            if cls in issue_lower:
                score += 2.0
        # Word overlap
        for part in parts:
            if part in issue_words:
                score += 1.0
        # Traceback mention (file:line pattern near function name)
        if re.search(rf'in\s+{re.escape(bare)}', issue_lower):
            score += 4.0

        scores[name] = score

    # Normalize to 0-1
    max_score = max(scores.values()) if scores else 1.0
    if max_score > 0:
        scores = {k: v / max_score for k, v in scores.items()}
    return scores


# ---------------------------------------------------------------------------
# 4. Build repair slice for a single file
# ---------------------------------------------------------------------------

def build_repair_slice(
    project_root: Path,
    file_path: str,
    facts: ProjectFacts,
    issue_text: str = "",
    top_n: int = 3,
) -> RepairSlice:
    """Build a compact repair slice for one located file."""
    # Find module containing this file
    mod = _find_module(facts, file_path)
    module_name = mod.name if mod else Path(file_path).stem

    full_path = project_root / file_path
    if not full_path.exists():
        return RepairSlice(file=file_path, module_name=module_name)

    content = full_path.read_text(errors="ignore")
    language = facts.language or "python"

    # Extract line ranges (all functions, including private)
    line_ranges = extract_function_line_ranges(content, file_path, language)
    # Extract raises
    raises_map = extract_raises(content, file_path, language)

    # Score and select top-N
    all_names = list(line_ranges.keys())
    scores = score_functions(all_names, issue_text)
    ranked = sorted(all_names, key=lambda n: scores.get(n, 0), reverse=True)
    selected = ranked[:top_n]

    # Build call edge index from module
    caller_index: dict[str, list[str]] = {}  # func -> [callers]
    callee_index: dict[str, list[str]] = {}  # func -> [callees]
    if mod:
        for edge in mod.call_edges:
            callee_index.setdefault(edge.caller, [])
            if edge.callee not in callee_index[edge.caller]:
                callee_index[edge.caller].append(edge.callee)
            caller_index.setdefault(edge.callee, [])
            if edge.caller not in caller_index[edge.callee]:
                caller_index[edge.callee].append(edge.caller)

    # Symbol index for metadata
    symbol_map: dict[str, Symbol] = {}
    if mod:
        for s in mod.symbols:
            symbol_map[s.name] = s
            # Also index by ClassName.method if in a class
            if s.kind == "method":
                for other_s in mod.symbols:
                    if other_s.kind == "class" and s.line > other_s.line:
                        scoped = f"{other_s.name}.{s.name}"
                        symbol_map[scoped] = s
                        break

    # Public symbols set
    public_symbols: set[str] = set()
    if mod and mod.interface_contract:
        public_symbols = set(getattr(mod.interface_contract, 'public_symbols', []))

    # Inheritance info
    override_map: dict[str, str] = {}  # child.method -> parent.method
    inherited_by_map: dict[str, list[str]] = {}  # parent.method -> [child classes]
    if hasattr(facts, 'inheritance_edges'):
        for edge in getattr(facts, 'inheritance_edges', []):
            # edge has child_class, parent_class
            inherited_by_map.setdefault(edge.parent_class, [])
            if edge.child_class not in inherited_by_map[edge.parent_class]:
                inherited_by_map[edge.parent_class].append(edge.child_class)

    # Test coverage
    covered_symbols: set[str] = set()
    test_file_map: dict[str, list[str]] = {}
    if mod and mod.test_coverage:
        covered_symbols = set(getattr(mod.test_coverage, 'covered_symbols', []))
        for tf in getattr(mod.test_coverage, 'covering_tests', []):
            test_file_map[tf] = []  # just track file names

    # Build function slices
    slices: list[FunctionSlice] = []
    for name in selected:
        line_start, line_end = line_ranges.get(name, (0, 0))

        # Find symbol metadata
        bare = name.split(".")[-1]
        sym = symbol_map.get(name) or symbol_map.get(bare)

        # Build signature
        if sym:
            sig = f"def {bare}({sym.params or '...'})"
            if sym.return_type:
                sig += f" -> {sym.return_type}"
            ret_type = sym.return_type or ""
            is_async = sym.is_async
            decorators = list(sym.decorators) if sym.decorators else []
        else:
            sig = _extract_signature_from_content(content, name, line_start)
            ret_type = ""
            is_async = False
            decorators = []

        # Callers/callees — match by scoped or bare name
        callers = caller_index.get(name, []) or caller_index.get(bare, [])
        callees = callee_index.get(name, []) or callee_index.get(bare, [])

        # Raises
        func_raises = raises_map.get(name, []) or raises_map.get(bare, [])

        # Test refs
        test_refs = []
        if bare in covered_symbols or name in covered_symbols:
            test_refs = [tf for tf in (mod.test_files if mod else [])]

        # Public?
        is_public = bare in public_symbols or name in public_symbols

        # Inheritance
        overrides_str = None
        inh_by: list[str] = []
        if "." in name:
            cls_name = name.split(".")[0]
            inh_by = inherited_by_map.get(cls_name, [])

        fs = FunctionSlice(
            name=name,
            file=file_path,
            line_start=line_start,
            line_end=line_end,
            signature=sig,
            relevance_score=scores.get(name, 0),
            callers=callers[:5],
            callees=callees[:5],
            overrides=overrides_str,
            inherited_by=inh_by[:5],
            test_refs=test_refs[:3],
            raises=func_raises,
            return_type=ret_type,
            is_public=is_public,
            is_async=is_async,
            decorators=decorators,
        )
        slices.append(fs)

    return RepairSlice(file=file_path, module_name=module_name, functions=slices)


def _find_module(facts: ProjectFacts, file_path: str) -> ModuleInfo | None:
    """Find the module that contains a given file path."""
    for name, mod in facts.modules.items():
        if file_path in mod.files:
            return mod
        # Also check basename match
        for mf in mod.files:
            if Path(mf).name == Path(file_path).name:
                return mod
    return None


def _extract_signature_from_content(content: str, name: str, line_start: int) -> str:
    """Fallback: extract signature from source line."""
    if line_start <= 0:
        return f"def {name.split('.')[-1]}(...)"
    lines = content.split("\n")
    if line_start <= len(lines):
        line = lines[line_start - 1].strip()
        # Remove decorators prefix, keep just the def line
        if line.startswith("def ") or line.startswith("async def "):
            colon_idx = line.find(":")
            if colon_idx > 0:
                return line[:colon_idx]
            return line.rstrip(":")
    return f"def {name.split('.')[-1]}(...)"


# ---------------------------------------------------------------------------
# 5. Format repair slices as compact text
# ---------------------------------------------------------------------------

def format_repair_slices(slices: list[RepairSlice]) -> str:
    """Format repair slices as compact, LLM-readable text."""
    parts: list[str] = []

    for rs in slices:
        parts.append(f"=== REPAIR CONTEXT: {rs.file} ===")
        parts.append(f"Module: {rs.module_name}")
        parts.append("")

        for fs in rs.functions:
            score_str = f", score: {fs.relevance_score:.2f}" if fs.relevance_score > 0 else ""
            parts.append(f"--- {fs.name} (L:{fs.line_start}-{fs.line_end}{score_str}) ---")
            parts.append(f"sig: {fs.signature}")

            # Returns and raises
            meta = []
            if fs.return_type:
                meta.append(f"returns: {fs.return_type}")
            if fs.raises:
                meta.append(f"raises: {', '.join(fs.raises)}")
            if meta:
                parts.append(" | ".join(meta))

            # Flags
            flags = []
            if fs.is_public:
                flags.append("public API")
            else:
                flags.append("internal")
            if fs.is_async:
                flags.append("async")
            if fs.decorators:
                flags.append(f"decorators: {', '.join(fs.decorators)}")
            parts.append(" | ".join(flags))

            # Callers
            if fs.callers:
                parts.append(f"callers: {', '.join(fs.callers)}")
            # Callees
            if fs.callees:
                parts.append(f"callees: {', '.join(fs.callees)}")
            # Inheritance
            if fs.overrides:
                parts.append(f"overrides: {fs.overrides}")
            if fs.inherited_by:
                parts.append(f"inherited_by: {', '.join(fs.inherited_by)}")
            # Tests
            if fs.test_refs:
                parts.append(f"tests: {', '.join(fs.test_refs)}")
            else:
                parts.append("tests: (uncovered)")

            parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 6. Top-level entry point
# ---------------------------------------------------------------------------

def generate_repair_slices(
    project_root: Path,
    files: list[str],
    issue_text: str = "",
    language: str | None = None,
    source_dirs: list[str] | None = None,
    top_n: int = 3,
) -> str:
    """Generate compact repair context for located files.

    This is the main entry point, called from CLI and SWE-bench pipeline.
    Returns formatted text ready for LLM prompt injection.
    """
    # Compute source_dirs from file paths if not provided
    if not source_dirs:
        dirs: set[str] = set()
        for f in files:
            parts = f.split("/")
            if len(parts) >= 2:
                dirs.add(parts[0])
            else:
                dirs.add(".")
        source_dirs = sorted(dirs) if dirs else ["."]

    # Extract facts (static analysis, no AI)
    facts = extract_facts(
        project_root,
        language=language,
        source_dirs=source_dirs,
    )

    # Build repair slices for each file
    slices: list[RepairSlice] = []
    for f in files:
        rs = build_repair_slice(project_root, f, facts, issue_text, top_n)
        if rs.functions:
            slices.append(rs)

    if not slices:
        return "(No repair context generated)"

    return format_repair_slices(slices)

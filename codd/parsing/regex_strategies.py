"""Registry-DATA-driven per-language regex extraction strategies.

Contract Kernel Cut Condition A (PARSING/EXTRACTION zone). This module holds the
per-language regex EXTRACTION logic that used to live as ``if language == X`` /
``if language in (...)`` ladders inside ``codd/extractor.py``'s core analysis
functions (``_extract_symbols``, ``_extract_imports``, ``_detect_code_patterns``,
``_common_stdlib``, ``_file_to_module``, ``_guess_test_target``,
``_language_extensions``, entry-point map).

The de-literalization principle (mirrors the shipped oracle/verify/project_types
cuts): per-language logic moves onto capability-keyed objects, and the core
DISPATCHES by a registry-DATA lookup (``strategy_for(language)``) — never an
inline ``if language ==`` branch. The language NAMES live here in the DATA table
(``_STRATEGIES``), which is the analogue of a profile/adapter and is the
explicitly-allowed "registry data" zone (v2.76: "project detection uses registry
data"; v2.72: per-language parser logic lives in adapters, not core gates).

A :class:`RegexLanguageStrategy` is the regex EXTRACTOR IMPLEMENTATION for one
language — it legitimately knows its own language, exactly like an adapter knows
its own language. An unknown language resolves to :data:`GENERIC_STRATEGY`
(best-effort no-op analysis), never a crash and never a false gate verdict
(extraction feeds the CEG/analysis; it is NOT a green/red gate — see
``tests/languages/test_contract_kernel_language_free.py`` rationale).

Behavior is byte-identical to the pre-refactor inline ladders; the
``tests/test_extraction_contract_parity.py`` oracle pins this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codd.extractor import ModuleInfo, Symbol


# ─────────────────────────────────────────────────────────────────────────────
# Strategy object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegexLanguageStrategy:
    """The regex extraction implementation for ONE language (DATA, not dispatch).

    Every field is the verbatim per-language behavior moved out of the
    ``extractor.py`` ladders. Fields default to the GENERIC (empty / no-op)
    behavior so an unknown language degrades to best-effort analysis.
    """

    name: str
    extensions: frozenset[str] = frozenset()
    stdlib: frozenset[str] = frozenset()
    entry_points: tuple[str, ...] = ()

    # symbols(content, file_path) -> list[Symbol]
    extract_symbols: Callable[[str, str], "list[Symbol]"] | None = None
    # imports(content, project_root, src_dir, file_path) -> (internal, external)
    extract_imports: (
        Callable[[str, Path, Path, Path], "tuple[dict[str, list[str]], set[str]]"]
        | None
    ) = None
    # code_patterns(mod, content) -> None (mutates mod.patterns)
    detect_code_patterns: Callable[["ModuleInfo", str], None] | None = None
    # file_to_module(rel_to_src) -> str | None  (None = use shared default)
    file_to_module: Callable[[Path], str | None] | None = None
    # guess_test_target(stem) -> str | None
    guess_test_target: Callable[[str], str | None] | None = None

    def symbols(self, content: str, file_path: str) -> "list[Symbol]":
        if self.extract_symbols is None:
            return []
        return self.extract_symbols(content, file_path)

    def imports(
        self, content: str, project_root: Path, src_dir: Path, file_path: Path
    ) -> "tuple[dict[str, list[str]], set[str]]":
        if self.extract_imports is None:
            return {}, set()
        internal, external = self.extract_imports(content, project_root, src_dir, file_path)
        external -= set(self.stdlib)
        return internal, external

    def code_patterns(self, mod: "ModuleInfo", content: str) -> None:
        if self.detect_code_patterns is not None:
            self.detect_code_patterns(mod, content)


# ─────────────────────────────────────────────────────────────────────────────
# Symbol extraction (verbatim bodies of the old _extract_symbols ladder)
# ─────────────────────────────────────────────────────────────────────────────

def _symbol(name, kind, file_path, line, params=""):
    from codd.extractor import Symbol

    return Symbol(name, kind, file_path, line, params)


def _symbols_python(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        m = re.match(r'^\s*class\s+(\w+)', line)
        if m:
            symbols.append(_symbol(m.group(1), "class", rel_path, i))
        m = re.match(r'^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)', line)
        if m and not m.group(1).startswith("_"):
            symbols.append(_symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
    return symbols


def _symbols_ts_js(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        m = re.match(r'^(?:export\s+)?class\s+(\w+)', line)
        if m:
            symbols.append(_symbol(m.group(1), "class", rel_path, i))
        m = re.match(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', line)
        if m:
            symbols.append(_symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
        # Arrow function exports
        m = re.match(r'^export\s+const\s+(\w+)\s*=\s*(?:async\s+)?\(', line)
        if m:
            symbols.append(_symbol(m.group(1), "function", rel_path, i))
    return symbols


def _symbols_java(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        m = re.match(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)', line)
        if m:
            symbols.append(_symbol(m.group(1), "class", rel_path, i))
        m = re.match(r'^\s*(?:public|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(([^)]*)\)', line)
        if m and m.group(1)[0].islower():
            symbols.append(_symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
    return symbols


def _symbols_go(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        m = re.match(r'^type\s+(\w+)\s+struct\s*\{', line)
        if m:
            symbols.append(_symbol(m.group(1), "class", rel_path, i))
        m = re.match(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)', line)
        if m and m.group(1)[0].isupper():
            symbols.append(_symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
    return symbols


# ─────────────────────────────────────────────────────────────────────────────
# Import extraction (verbatim bodies of the old _extract_imports ladder).
# stdlib subtraction is applied by RegexLanguageStrategy.imports().
# ─────────────────────────────────────────────────────────────────────────────

def _imports_python(content, project_root, src_dir, file_path):
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    src_pkg_name = src_dir.name if (src_dir / "__init__.py").exists() else None

    for line in content.splitlines():
        m = re.match(r'^(?:from|import)\s+([\w.]+)', line.strip())
        if not m:
            continue
        module = m.group(1)
        parts = module.split(".")
        top_level = parts[0]

        is_internal = False
        internal_key = top_level

        if src_pkg_name and top_level == src_pkg_name and len(parts) >= 2:
            is_internal = True
            internal_key = parts[1]
        else:
            for sd in [src_dir] + [project_root / d for d in ("src", "lib", "app") if (project_root / d).is_dir()]:
                if (sd / top_level).is_dir() or (sd / f"{top_level}.py").is_file():
                    is_internal = True
                    break

        if is_internal:
            internal.setdefault(internal_key, []).append(line.strip())
        else:
            external.add(top_level)

    return internal, external


def _imports_ts_js(content, project_root, src_dir, file_path):
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for line in content.splitlines():
        m = re.search(r'''(?:import|from)\s+['"]([^'"]+)['"]''', line)
        if not m:
            continue
        import_path = m.group(1)
        if import_path.startswith("."):
            resolved = (file_path.parent / import_path).resolve()
            try:
                rel = resolved.relative_to(src_dir)
                top_level = rel.parts[0] if rel.parts else "root"
                internal.setdefault(top_level, []).append(line.strip())
            except ValueError:
                external.add(import_path)
        elif import_path.startswith("@"):
            parts = import_path.split("/")
            pkg = "/".join(parts[:2]) if len(parts) >= 2 else import_path
            external.add(pkg)
        else:
            external.add(import_path.split("/")[0])

    return internal, external


def _imports_go(content, project_root, src_dir, file_path):
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    in_import = False
    for line in content.splitlines():
        if re.match(r'^import\s*\(', line):
            in_import = True
            continue
        if in_import and line.strip() == ")":
            in_import = False
            continue
        if in_import or re.match(r'^import\s+"', line):
            m = re.search(r'"([^"]+)"', line)
            if m:
                pkg = m.group(1)
                external.add(pkg.split("/")[-1])

    return internal, external


# ─────────────────────────────────────────────────────────────────────────────
# Code-pattern detection (verbatim bodies of the old _detect_code_patterns).
# ─────────────────────────────────────────────────────────────────────────────

def _patterns_python(mod: "ModuleInfo", content: str) -> None:
    if re.search(r'@(?:app|router)\.(get|post|put|delete|patch)\s*\(', content):
        mod.patterns["api_routes"] = "HTTP route handlers"
    if re.search(r'class\s+\w+\(.*(?:Base|Model|db\.Model)\)', content):
        mod.patterns["db_models"] = "ORM models"
    if re.search(r'@(?:celery_app|app)\.task', content):
        mod.patterns["background_tasks"] = "Async task handlers"
    if re.search(r'(?:redirect|RedirectResponse|HttpResponseRedirect)\s*\(', content):
        mod.patterns["auth_redirects"] = "Server-side redirects"
    if re.search(r'@login_required|@permission_required|LoginRequiredMixin', content):
        mod.patterns["auth_guards"] = "Authentication guards"


def _patterns_ts_js(mod: "ModuleInfo", content: str) -> None:
    if re.search(r'(?:app|router)\.(get|post|put|delete|patch)\s*\(', content):
        mod.patterns["api_routes"] = "HTTP route handlers"
    if re.search(r'@(?:Controller|Get|Post|Put|Delete|Patch)\s*\(', content):
        mod.patterns["api_routes"] = "NestJS controller"
    if re.search(r'(?:schema|model)\s*\(', content, re.IGNORECASE):
        mod.patterns["db_models"] = "Database models"
    if re.search(r'export\s+default\s+(?:async\s+)?function\s+\w*Page', content):
        mod.patterns["page_routes"] = "Page route components"
    if re.search(r'(?:redirect|NextResponse\.redirect|Response\.redirect)\s*\(', content):
        mod.patterns["auth_redirects"] = "Server-side redirects"
    if re.search(r'export\s+(?:async\s+)?function\s+middleware', content):
        mod.patterns["middleware"] = "Request middleware"
    if re.search(r'(?:router\.push|router\.replace|window\.location\.assign|window\.location\.href\s*=)', content):
        mod.patterns["client_redirects"] = "Client-side navigation"
    if re.search(r'(?:NextAuth|CredentialsProvider|signIn|signOut|useSession|getServerSession)', content):
        mod.patterns["auth_provider"] = "Authentication provider"


# ─────────────────────────────────────────────────────────────────────────────
# Module-name mapping (verbatim bodies of the old _file_to_module ladder).
# Each returns the module name from the file path relative to the source dir.
# ─────────────────────────────────────────────────────────────────────────────

def _file_to_module_python(rel_to_src: Path) -> str:
    parts = list(rel_to_src.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts.pop()
    if parts:
        return parts[0]
    return rel_to_src.parent.name or "root"


def _file_to_module_first_part(rel_to_src: Path) -> str:
    parts = list(rel_to_src.parts)
    if parts:
        return parts[0]
    return "root"


def _file_to_module_java(rel_to_src: Path) -> str:
    parts = list(rel_to_src.parts)
    skip = {"main", "java", "kotlin", "scala"}
    parts = [p for p in parts if p not in skip]
    if parts:
        return parts[0]
    return "root"


# ─────────────────────────────────────────────────────────────────────────────
# Test-target guessing (verbatim bodies of the old _guess_test_target ladder).
# ─────────────────────────────────────────────────────────────────────────────

def _guess_test_target_python(name: str) -> str | None:
    if name.startswith("test_"):
        return name[5:]
    return None


def _guess_test_target_ts_js(name: str) -> str | None:
    for suffix in (".test", ".spec"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# stdlib sets (verbatim from the old _common_stdlib python branch)
# ─────────────────────────────────────────────────────────────────────────────

_PYTHON_STDLIB = frozenset({
    "os", "sys", "re", "json", "math", "time", "datetime", "pathlib",
    "typing", "collections", "itertools", "functools", "copy", "io",
    "subprocess", "shutil", "tempfile", "hashlib", "uuid", "logging",
    "unittest", "dataclasses", "enum", "abc", "contextlib", "textwrap",
    "argparse", "configparser", "csv", "sqlite3", "http", "urllib",
    "threading", "multiprocessing", "socket", "email", "html", "xml",
    "importlib", "inspect", "ast", "dis", "warnings", "traceback",
    "pprint", "string", "struct", "array", "queue", "heapq", "bisect",
    "statistics", "random", "secrets", "base64", "binascii", "codecs",
    "locale", "gettext", "calendar", "zlib", "gzip", "tarfile", "zipfile",
    "__future__", "builtins", "types", "operator", "fnmatch", "glob",
    "signal", "mmap", "ctypes", "platform", "sysconfig", "site",
    "concurrent", "asyncio", "selectors", "ssl", "ftplib", "smtplib",
})


# ─────────────────────────────────────────────────────────────────────────────
# The registry-DATA table: language name → strategy. This is the ALLOWED zone
# (a language→capability table is "registry data", analogous to a profile).
# ─────────────────────────────────────────────────────────────────────────────

_PYTHON = RegexLanguageStrategy(
    name="python",
    extensions=frozenset({".py"}),
    stdlib=_PYTHON_STDLIB,
    entry_points=("main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "__main__.py"),
    extract_symbols=_symbols_python,
    extract_imports=_imports_python,
    detect_code_patterns=_patterns_python,
    file_to_module=_file_to_module_python,
    guess_test_target=_guess_test_target_python,
)

_TYPESCRIPT = RegexLanguageStrategy(
    name="typescript",
    extensions=frozenset({".ts", ".tsx"}),
    entry_points=("index.ts", "main.ts", "app.ts", "server.ts"),
    extract_symbols=_symbols_ts_js,
    extract_imports=_imports_ts_js,
    detect_code_patterns=_patterns_ts_js,
    file_to_module=_file_to_module_first_part,
    guess_test_target=_guess_test_target_ts_js,
)

_JAVASCRIPT = RegexLanguageStrategy(
    name="javascript",
    extensions=frozenset({".js", ".jsx"}),
    entry_points=("index.js", "main.js", "app.js", "server.js"),
    extract_symbols=_symbols_ts_js,
    extract_imports=_imports_ts_js,
    detect_code_patterns=_patterns_ts_js,
    file_to_module=_file_to_module_first_part,
    guess_test_target=_guess_test_target_ts_js,
)

_JAVA = RegexLanguageStrategy(
    name="java",
    extensions=frozenset({".java"}),
    entry_points=("Application.java", "Main.java", "App.java"),
    extract_symbols=_symbols_java,
    extract_imports=None,  # the old ladder had NO java import branch (no-op)
    detect_code_patterns=None,  # the old ladder had NO java code-pattern branch
    file_to_module=_file_to_module_java,
    guess_test_target=None,
)

_GO = RegexLanguageStrategy(
    name="go",
    extensions=frozenset({".go"}),
    entry_points=("main.go", "cmd/main.go"),
    extract_symbols=_symbols_go,
    extract_imports=_imports_go,
    detect_code_patterns=None,  # the old ladder had NO go code-pattern branch
    file_to_module=_file_to_module_first_part,
    guess_test_target=None,
)

#: The generic fallback for an unknown language — best-effort no-op analysis,
#: matching the implicit ``else`` of every old ladder (empty symbols/imports,
#: no patterns, first-path-part module name, no test-target, no extensions).
GENERIC_STRATEGY = RegexLanguageStrategy(
    name="generic",
    file_to_module=_file_to_module_first_part,
)

_STRATEGIES: dict[str, RegexLanguageStrategy] = {
    "python": _PYTHON,
    "typescript": _TYPESCRIPT,
    "javascript": _JAVASCRIPT,
    "java": _JAVA,
    "go": _GO,
}


@dataclass(frozen=True)
class CegImportTarget:
    """A CEG edge to create for one resolved import (scanner DATA, not dispatch).

    Describes the target node + edge + evidence the scanner should materialize
    for an extracted import. Lets ``scanner._extract_imports_basic`` build the
    graph WITHOUT a ``if language in (...)`` branch — the per-language resolution
    rule lives here, byte-identical to the former inline scanner block.
    """

    target_id: str
    node_type: str
    node_kwargs: dict
    evidence_method: str
    confidence: float


def _ceg_targets_ts_js(
    internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    targets: list[CegImportTarget] = []
    for import_lines in internal.values():
        for line in import_lines:
            match = re.search(r'''(?:import|from)\s+['"]([^'"]+)['"]''', line)
            if not match:
                continue
            target_module = match.group(1)
            if not target_module.startswith("."):
                continue
            resolved = (file_path.parent / target_module).resolve()
            extensions = [
                ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts",
                "/index.ts", "/index.tsx", "/index.js", "/index.jsx",
            ]
            for ext in [""] + extensions:
                candidate = Path(f"{resolved}{ext}")
                if not candidate.exists():
                    continue
                try:
                    target_rel = candidate.relative_to(project_root).as_posix()
                except ValueError:
                    continue
                targets.append(
                    CegImportTarget(
                        target_id=f"file:{target_rel}",
                        node_type="file",
                        node_kwargs={"path": target_rel},
                        evidence_method="ast_import",
                        confidence=0.95,
                    )
                )
                break
    return targets


def _ceg_targets_python(
    internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    targets: list[CegImportTarget] = []
    for target_module in internal:
        targets.append(
            CegImportTarget(
                target_id=f"module:{target_module}",
                node_type="module",
                node_kwargs={"name": target_module},
                evidence_method="ast_import",
                confidence=0.90,
            )
        )
    return targets


#: Per-language scanner CEG-import resolvers (registry DATA). Languages without
#: an entry contribute NO import edges, byte-identical to the former scanner
#: block that only handled python and typescript/javascript.
_CEG_IMPORT_RESOLVERS: dict[
    str, Callable[[dict, Path, Path], "list[CegImportTarget]"]
] = {
    "typescript": _ceg_targets_ts_js,
    "javascript": _ceg_targets_ts_js,
    "python": _ceg_targets_python,
}


def ceg_import_targets(
    language: str, internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    """Resolve extracted imports into CEG edge specs (registry-data dispatch).

    Returns the list of :class:`CegImportTarget` the scanner should materialize
    for ``language``; unknown languages yield no targets (byte-identical to the
    former ``if language in (...)`` scanner block).
    """
    resolver = _CEG_IMPORT_RESOLVERS.get((language or "").lower())
    if resolver is None:
        return []
    return resolver(internal, project_root, file_path)


def strategy_for(language: str) -> RegexLanguageStrategy:
    """Return the regex strategy for *language* (data lookup, no name dispatch).

    Unknown languages resolve to :data:`GENERIC_STRATEGY` (best-effort analysis),
    never a crash — extraction is analysis input, not a gate verdict.
    """
    return _STRATEGIES.get((language or "").lower(), GENERIC_STRATEGY)


# ─────────────────────────────────────────────────────────────────────────────
# Repair-slice analyzer registry DATA (Contract Kernel Cut Condition A —
# repair_slice.py de-literalization). ``codd/repair_slice.py``'s function
# line-range + raises analyzer used to branch on ``language in
# ("typescript","javascript")`` (tree-sitter func node-type set) and
# ``language == "python"`` (regex def-vs-function pattern + group index, and the
# python-only raises regex). Those per-language facts live HERE as registry DATA
# (the analogue of a profile/adapter — an analyzer impl legitimately names its
# own language), so the core analyzer dispatches by a data lookup, NOT a
# language-name branch. Byte-identical to the former inline ladders; the
# ``tests/languages/test_contract_kernel_cut_a_parity.py`` oracle pins this.
#
# Repair-slice extraction (like all extraction) is ANALYSIS input for patch
# context, never a green/red GATE verdict — so an unknown language degrading to
# the GENERIC profile (empty func-node set, the function-keyword regex, no raises)
# is legitimate best-effort analysis, not a false-green.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepairSliceLanguageProfile:
    """Per-language repair-slice analyzer DATA (one language; not dispatch).

    * ``function_node_types`` — the tree-sitter named-node types that are a
      function/method definition for this language (walked for line ranges).
    * ``line_range_pattern`` — the regex (with ``re.MULTILINE``) that matches a
      function definition in the regex FALLBACK path.
    * ``line_range_name_group`` — which regex group holds the function name.
    * ``raises_def_pattern`` / ``raises_stmt_pattern`` — the regex pair for the
      regex-fallback raises analyzer; ``None`` (the GENERIC default) means this
      language contributes NO raises in the fallback (byte-identical to the
      former ``if language != "python": return {}``).
    """

    function_node_types: frozenset[str] = frozenset({"function_definition"})
    line_range_pattern: re.Pattern[str] = field(
        default_factory=lambda: re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE
        )
    )
    line_range_name_group: int = 1
    raises_def_pattern: re.Pattern[str] | None = None
    raises_stmt_pattern: re.Pattern[str] | None = None


#: The python repair-slice profile: tree-sitter walks only ``function_definition``
#: (methods are ``function_definition`` inside a class in the python grammar); the
#: regex fallback uses the ``def`` pattern with the NAME in group 2; and python is
#: the ONLY language with a regex raises analyzer.
_REPAIR_PYTHON = RepairSliceLanguageProfile(
    function_node_types=frozenset({"function_definition"}),
    line_range_pattern=re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE),
    line_range_name_group=2,
    raises_def_pattern=re.compile(r"\s*(?:async\s+)?def\s+(\w+)\s*\("),
    raises_stmt_pattern=re.compile(r"\s+raise\s+(\w+)"),
)

#: The ts/js repair-slice profile: tree-sitter additionally walks
#: ``method_definition`` + ``function_declaration`` (the former ``language in
#: ("typescript","javascript")`` set); the regex fallback uses the
#: ``function`` pattern with the NAME in group 1; no regex raises analyzer.
_REPAIR_TS_JS = RepairSliceLanguageProfile(
    function_node_types=frozenset(
        {"function_definition", "method_definition", "function_declaration"}
    ),
    line_range_pattern=re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE
    ),
    line_range_name_group=1,
)

#: The GENERIC repair-slice profile for any other/unknown language — byte-identical
#: to the former ``else`` branches: tree-sitter walks only ``function_definition``;
#: the regex fallback uses the ``function`` pattern with the name in group 1; and
#: no raises (the former ``if language != "python": return {}``).
GENERIC_REPAIR_PROFILE = RepairSliceLanguageProfile()

_REPAIR_PROFILES: dict[str, RepairSliceLanguageProfile] = {
    "python": _REPAIR_PYTHON,
    "typescript": _REPAIR_TS_JS,
    "javascript": _REPAIR_TS_JS,
}


def repair_slice_profile_for(language: str) -> RepairSliceLanguageProfile:
    """Repair-slice analyzer profile for *language* (registry-data lookup).

    Unknown languages resolve to :data:`GENERIC_REPAIR_PROFILE` (the former
    non-python / non-ts-js ``else`` behavior); analysis input, never a gate.
    """
    return _REPAIR_PROFILES.get((language or "").lower(), GENERIC_REPAIR_PROFILE)


def language_extensions(language: str) -> set[str]:
    """Source-file extensions for *language* (registry-data lookup)."""
    return set(strategy_for(language).extensions)


def common_stdlib(language: str) -> set[str]:
    """Common stdlib modules to exclude from external imports (registry-data)."""
    return set(strategy_for(language).stdlib)


def entry_point_candidates(language: str) -> list[str]:
    """Likely entry-point filenames for *language* (registry-data lookup)."""
    return list(strategy_for(language).entry_points)

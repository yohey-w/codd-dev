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

from codd.parsing._shared import (
    CPP_INCLUDE_RE as _SHARED_CPP_INCLUDE_RE,
    CSHARP_NAMESPACE_RE as _SHARED_CSHARP_NAMESPACE_RE,
    CSHARP_USING_RE as _SHARED_CSHARP_USING_RE,
    JAVA_IMPORT_RE as _SHARED_JAVA_IMPORT_RE,
    JAVA_PACKAGE_RE as _SHARED_JAVA_PACKAGE_RE,
    JS_TS_SOURCE_EXTENSIONS,
    cpp_include_candidate_paths,
    js_ts_source_candidate_paths,
    strip_bom,
)

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
        return self.extract_symbols(strip_bom(content), file_path)

    def imports(
        self, content: str, project_root: Path, src_dir: Path, file_path: Path
    ) -> "tuple[dict[str, list[str]], set[str]]":
        if self.extract_imports is None:
            return {}, set()
        internal, external = self.extract_imports(
            strip_bom(content), project_root, src_dir, file_path
        )
        external -= set(self.stdlib)
        return internal, external

    def code_patterns(self, mod: "ModuleInfo", content: str) -> None:
        if self.detect_code_patterns is not None:
            self.detect_code_patterns(mod, strip_bom(content))


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


#: Java type-declaration keyword → emitted symbol kind. ``record`` and the
#: annotation type collapse to the nearest existing kind so downstream symbol
#: consumers (which only know class/interface/enum/function) stay unchanged.
_JAVA_TYPE_KEYWORD_KINDS = (
    (re.compile(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:abstract\s+|final\s+)?class\s+(\w+)'), "class"),
    (re.compile(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:abstract\s+)?interface\s+(\w+)'), "interface"),
    (re.compile(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?enum\s+(\w+)'), "enum"),
    (re.compile(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?record\s+(\w+)'), "class"),
)


def _symbols_java(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        for pattern, kind in _JAVA_TYPE_KEYWORD_KINDS:
            m = pattern.match(line)
            if m:
                symbols.append(_symbol(m.group(1), kind, rel_path, i))
                break
        m = re.match(r'^\s*(?:public|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(([^)]*)\)', line)
        if m and m.group(1)[0].islower():
            symbols.append(_symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
    return symbols


#: Java stdlib / platform package roots (mirrors treesitter._JAVA_STDLIB_ROOTS).
_JAVA_STDLIB = frozenset({"java", "javax", "jdk", "sun"})

# SHARED Java grammar (single source of truth in ``codd.parsing._shared``, also
# used by the DAG builder's Java specifier extractor), so the import/package
# grammar cannot drift between the scan and builder layers. group(2) = the FQN
# (group(1) = the ``static`` keyword); the package regex group(1) = the package.
# The shared patterns carry ``re.MULTILINE`` — inert for the per-line ``.match()``
# calls below (a single line has no interior newline and ``.match`` anchors at 0).
_JAVA_IMPORT_RE = _SHARED_JAVA_IMPORT_RE
_JAVA_PACKAGE_RE = _SHARED_JAVA_PACKAGE_RE


def _imports_java(content, project_root, src_dir, file_path):
    """Classify Java imports: first-party ``internal`` vs ``java.*``/third-party.

    The regex sibling of ``treesitter._extract_java_imports_ast`` (used when
    tree-sitter-java is unavailable). First-party = an import sharing the file's
    own package org+domain prefix (first 2 segments, e.g. ``com.google``);
    ``java.*``/``javax.*``/``jdk.*``/``sun.*`` and unrelated third parties are
    external. ``stdlib`` subtraction is applied by ``RegexLanguageStrategy``.
    """
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    package_root = ""
    for line in content.splitlines():
        pm = _JAVA_PACKAGE_RE.match(line)
        if pm:
            package_root = pm.group(1)
            break

    root_parts = package_root.split(".") if package_root else []
    depth = min(2, len(root_parts))

    for line in content.splitlines():
        m = _JAVA_IMPORT_RE.match(line)
        if not m:
            continue
        fqn = m.group(2)
        if fqn.endswith(".*"):
            fqn = fqn[:-2]
        parts = fqn.split(".")
        top = parts[0]
        if top in _JAVA_STDLIB:
            external.add(fqn)
            continue
        if root_parts and parts[:depth] == root_parts[:depth]:
            key = _java_internal_key(parts, root_parts)
            internal.setdefault(key, []).append(line.strip())
        else:
            external.add(fqn)

    return internal, external


def _java_internal_key(fqn_parts: list[str], root_parts: list[str]) -> str:
    """The first sub-package segment after the file's package root.

    For a ``com.acme.app`` file importing ``com.acme.app.util.Helper`` the key is
    ``util`` (the meaningful first-party sub-module), not the shared org segment.
    Falls back to the last package segment when the import IS the package root.
    """
    common = 0
    for left, right in zip(fqn_parts, root_parts):
        if left != right:
            break
        common += 1
    remainder = fqn_parts[common:]
    if remainder:
        return remainder[0]
    return fqn_parts[-2] if len(fqn_parts) >= 2 else fqn_parts[0]


#: C# type-declaration keyword → emitted symbol kind. ``record`` collapses to the
#: nearest existing kind (``class``) so downstream symbol consumers (which only
#: know class/interface/enum/struct/function) stay unchanged; ``record struct`` is
#: covered by the ``struct`` pattern. Access/`partial`/`sealed`/`abstract`/etc.
#: modifiers are tolerated before the keyword.
_CSHARP_MODIFIERS = r'(?:public|internal|protected|private|static|sealed|abstract|partial|readonly|ref|unsafe|new|file)'
_CSHARP_TYPE_KEYWORD_KINDS = (
    (re.compile(rf'^\s*(?:{_CSHARP_MODIFIERS}\s+)*class\s+(\w+)'), "class"),
    (re.compile(rf'^\s*(?:{_CSHARP_MODIFIERS}\s+)*interface\s+(\w+)'), "interface"),
    (re.compile(rf'^\s*(?:{_CSHARP_MODIFIERS}\s+)*enum\s+(\w+)'), "enum"),
    (re.compile(rf'^\s*(?:{_CSHARP_MODIFIERS}\s+)*(?:record\s+)?struct\s+(\w+)'), "struct"),
    (re.compile(rf'^\s*(?:{_CSHARP_MODIFIERS}\s+)*record\s+(?:class\s+)?(\w+)'), "class"),
)


def _symbols_csharp(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        for pattern, kind in _CSHARP_TYPE_KEYWORD_KINDS:
            m = pattern.match(line)
            if m:
                symbols.append(_symbol(m.group(1), kind, rel_path, i))
                break
    return symbols


#: C# ``using`` directives + ``namespace`` declarations — the SHARED grammar
#: (single source of truth in ``codd.parsing._shared``, also used by the DAG
#: builder), so the scan-path parser and the DAG builder cannot drift on a C#
#: project's using graph. ``using`` covers plain / ``static`` / ``global`` / alias
#: (``using Alias = X.Y;``) forms; the namespace regex covers BOTH file-scoped
#: (``namespace X.Y;``) and block (``namespace X.Y {``) declarations.
#: NOTE: in the shared ``using`` regex the ``static`` keyword is the CAPTURING
#: group(1) and the imported NAMESPACE is group(2) (so the builder can decide
#: parent-namespace probing); the classifier below reads group(2). The shared
#: patterns carry ``re.MULTILINE`` — inert for the per-line ``.match()`` calls.
_CSHARP_USING_RE = _SHARED_CSHARP_USING_RE
_CSHARP_NAMESPACE_RE = _SHARED_CSHARP_NAMESPACE_RE

#: C# framework / BCL namespace roots. A ``using`` rooted here is external (the
#: analogue of Java ``java.*`` / C++ angle-form ``<…>``). Everything else is a
#: candidate first-party namespace; the DAG builder owns the precise
#: namespace→file resolution for edges via the reverse-index.
_CSHARP_FRAMEWORK_ROOTS = frozenset({"System", "Microsoft", "Windows", "Mono"})


def _imports_csharp(content, project_root, src_dir, file_path):
    """Classify C# usings: first-party ``internal`` vs ``System.*``/framework.

    The regex sibling of the DAG builder's using extraction (there is no
    tree-sitter-c-sharp binding). A ``using Dapper.X;`` rooted outside the
    framework roots is first-party (``internal``, bucketed by the namespace's
    first segment); ``using System.Text;`` / ``using Microsoft.*`` is framework
    (``external``, keyed by the full namespace). Returns the
    ``(internal, external)`` shape every ``extract_imports`` strategy yields.
    """
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for line in content.splitlines():
        m = _CSHARP_USING_RE.match(line)
        if not m:
            continue
        # group(2) = the imported namespace (group(1) is the ``static`` keyword in
        # the SHARED capturing regex); the static flag is irrelevant to scan
        # classification, which only needs the namespace.
        namespace = m.group(2)
        top = namespace.split(".", 1)[0]
        if top in _CSHARP_FRAMEWORK_ROOTS:
            external.add(namespace)
        else:
            # Bucket by the first namespace segment (``Dapper`` for
            # ``Dapper.ProviderTools``); the DAG builder owns the precise
            # namespace→file resolution for edges via the reverse-index.
            internal.setdefault(top, []).append(line.strip())

    return internal, external


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


#: C++ type-declaration keyword → emitted symbol kind. ``struct`` keeps its own
#: kind (distinct from ``class``) since it is meaningful in C++; ``enum`` (incl.
#: ``enum class``) and ``namespace`` map to their literal kinds.
_CPP_TYPE_KEYWORD_KINDS = (
    (re.compile(r'^\s*(?:template\s*<[^>]*>\s*)?class\s+(\w+)'), "class"),
    (re.compile(r'^\s*(?:template\s*<[^>]*>\s*)?struct\s+(\w+)'), "struct"),
    (re.compile(r'^\s*enum\s+(?:class\s+|struct\s+)?(\w+)'), "enum"),
    (re.compile(r'^\s*namespace\s+(\w+)'), "namespace"),
)

#: A free-function definition/declaration line. Deliberately conservative: a
#: return type (one or more type tokens, optional ``*``/``&``) followed by a name
#: and a parameter list. Excludes control-flow keywords so ``if (...)`` /
#: ``while (...)`` / ``for (...)`` / ``switch (...)`` never read as functions.
_CPP_FUNCTION_RE = re.compile(
    r'^\s*(?:[\w:]+[\w:<>,\s\*&]*?\s+[\*&]?)(\w+)\s*\([^;{]*\)\s*(?:const\s*)?[{;]'
)
_CPP_CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "return", "else", "do", "catch", "sizeof",
})


def _symbols_cpp(content: str, rel_path: str) -> "list[Symbol]":
    symbols: list = []
    for i, line in enumerate(content.splitlines(), 1):
        matched_type = False
        for pattern, kind in _CPP_TYPE_KEYWORD_KINDS:
            m = pattern.match(line)
            if m:
                symbols.append(_symbol(m.group(1), kind, rel_path, i))
                matched_type = True
                break
        if matched_type:
            continue
        m = _CPP_FUNCTION_RE.match(line)
        if m and m.group(1) not in _CPP_CONTROL_KEYWORDS:
            symbols.append(_symbol(m.group(1), "function", rel_path, i))
    return symbols


#: C++ ``#include`` directive: quote-form (``"…"`` — local/first-party, group(1))
#: vs angle-form (``<…>`` — system/STL, group(2)). The SHARED grammar (single
#: source of truth in ``codd.parsing._shared``, also used by the DAG builder), so
#: the scan-path parser and the DAG builder cannot drift on a C++ project's
#: include grammar. Carries ``re.MULTILINE`` — inert for the per-line ``.match()``.
_CPP_INCLUDE_RE = _SHARED_CPP_INCLUDE_RE


def _imports_cpp(content, project_root, src_dir, file_path):
    """Classify C++ includes: quote-form ``internal`` vs angle-form ``external``.

    The regex sibling of the DAG builder's include extraction (there is no
    tree-sitter-cpp binding). Quote-form ``#include "demo/core.h"`` is a
    first-party include (kept as ``internal`` with the raw line, bucketed by the
    include path's first segment); angle-form ``#include <vector>`` is system/STL
    (``external``, keyed by the bare header token). Returns the
    ``(internal, external)`` shape every ``extract_imports`` strategy yields.
    """
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for line in content.splitlines():
        m = _CPP_INCLUDE_RE.match(line)
        if not m:
            continue
        quote_target, angle_target = m.group(1), m.group(2)
        if quote_target:
            # Bucket by the first path segment (``demo`` for ``demo/core.h``); the
            # DAG builder owns the precise path→file resolution for edges.
            bucket = quote_target.split("/", 1)[0] or "root"
            internal.setdefault(bucket, []).append(line.strip())
        elif angle_target:
            # ``<sys/types.h>`` keys on the bare top token; bare ``<vector>`` on
            # itself. Either way it is a system/STL external.
            external.add(angle_target)

    return internal, external


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


# Capture the module specifier from every JS/TS import form: ESM
# ``import .. from '..'`` / ``from '..'``, plus CommonJS ``require('..')`` and
# dynamic ``import('..')``. Mirrors codd.dag.extractor._IMPORT_SPECIFIER_RE so the
# scan-path parser and the DAG builder agree on a CommonJS codebase's internal
# dependency graph.
_TS_JS_IMPORT_SPECIFIER_RE = re.compile(
    r'''(?:import|from|require\(\s*|import\(\s*)\s*['"]([^'"]+)['"]'''
)


def _imports_ts_js(content, project_root, src_dir, file_path):
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for line in content.splitlines():
        m = _TS_JS_IMPORT_SPECIFIER_RE.search(line)
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
    # stdlib stays EMPTY: the old ladder had no java stdlib set and
    # ``common_stdlib("java")`` is pinned to ``set()``. ``_imports_java`` classifies
    # ``java.*`` into ``external`` itself (per the Piece-2 contract) rather than
    # relying on strategy-level stdlib subtraction.
    entry_points=("Application.java", "Main.java", "App.java"),
    extract_symbols=_symbols_java,
    extract_imports=_imports_java,
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

_CPP = RegexLanguageStrategy(
    name="cpp",
    extensions=frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"}),
    # stdlib stays EMPTY: ``_imports_cpp`` already classifies angle-form includes
    # (the STL/system headers) into ``external`` itself; there is no name-set to
    # subtract (C++ system headers are arbitrary tokens, not a fixed list).
    entry_points=("main.cc", "main.cpp", "main.cxx"),
    extract_symbols=_symbols_cpp,
    extract_imports=_imports_cpp,
    detect_code_patterns=None,
    file_to_module=_file_to_module_first_part,
    guess_test_target=None,
)

_CSHARP = RegexLanguageStrategy(
    name="csharp",
    extensions=frozenset({".cs"}),
    # stdlib stays EMPTY: ``_imports_csharp`` already classifies framework usings
    # (``System.*``/``Microsoft.*``/…) into ``external`` itself; there is no fixed
    # name-set to subtract (C# uses arbitrary dotted namespaces, not a list).
    entry_points=("Program.cs", "Main.cs"),
    extract_symbols=_symbols_csharp,
    extract_imports=_imports_csharp,
    detect_code_patterns=None,
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
    "cpp": _CPP,
    "csharp": _CSHARP,
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
    """JS/TS relative imports → PATH-resolved ``file:`` CEG nodes.

    Resolution unification: the candidate filesystem paths come from the SHARED
    :func:`codd.parsing._shared.js_ts_source_candidate_paths` — the SAME ordering
    + ESM ``.js``→``.ts`` swap the DAG builder uses (the builder matches the
    candidates against its node-set; this resolver matches them against disk). The
    pre-unification scan resolver hand-rolled its own extension list WITHOUT the
    ESM swap and only re-parsed ``import``/``from`` lines, so it silently dropped
    (a) ``import "./x.js"`` → ``x.ts`` edges and (b) ``require('./x')`` /
    ``import('./x')`` edges that the builder DID form — the same drift class the
    C++ unification killed (the LevelDB scan-edge loss). Both layers now share the
    one candidate generator, so they cannot diverge.
    """
    project_root_resolved = project_root.resolve()
    targets: list[CegImportTarget] = []
    seen: set[str] = set()
    for import_lines in internal.values():
        for line in import_lines:
            match = _TS_JS_IMPORT_SPECIFIER_RE.search(line)
            if not match:
                continue
            target_module = match.group(1)
            if not target_module.startswith("."):
                continue
            for candidate in js_ts_source_candidate_paths(
                target_module, file_path, JS_TS_SOURCE_EXTENSIONS
            ):
                if not candidate.is_file():
                    continue
                try:
                    target_rel = candidate.relative_to(project_root_resolved).as_posix()
                except ValueError:
                    continue
                if target_rel in seen:
                    break
                seen.add(target_rel)
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


def _ceg_targets_java(
    internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    """Java internal imports → ``module`` CEG nodes keyed by first-party FQN.

    Mirrors the python resolver's module-node modelling (a stable identifier per
    imported first-party unit) rather than probing the filesystem; the DAG
    builder owns the precise FQN→file resolution for import EDGES. The ``internal``
    map values are the raw import lines (``com.google.gson.internal.Streams`` /
    ``static com.google.gson.Foo.bar``); we recover the owning FQN for the node.
    """
    targets: list[CegImportTarget] = []
    seen: set[str] = set()
    for import_lines in internal.values():
        for line in import_lines:
            raw = line.strip()
            if not raw:
                continue
            # PRECISION: label by the ACTUAL import kind, and resolve to the OWNING
            # first-party unit. A ``static com.x.Y.member`` collapses to its owning
            # class ``com.x.Y`` (member dropped) so per-member static imports of the
            # same class DEDUP to one node; a ``com.x.*`` wildcard collapses to the
            # package; a plain ``com.x.Y`` stays as-is.
            if raw.startswith("static "):
                evidence_method = "static_import"
                fqn = raw[len("static "):].strip()
                parts = fqn.split(".")
                if len(parts) >= 2:
                    fqn = ".".join(parts[:-1])  # drop the member → owning class
            elif raw.endswith(".*"):
                evidence_method = "wildcard_import"
                fqn = raw[:-2].strip()
            else:
                evidence_method = "import"
                fqn = raw
            if not fqn or fqn in seen:
                continue
            seen.add(fqn)
            targets.append(
                CegImportTarget(
                    target_id=f"module:{fqn}",
                    node_type="module",
                    node_kwargs={"name": fqn},
                    evidence_method=evidence_method,
                    confidence=0.90,
                )
            )
    return targets


_CPP_INCLUDE_LINE_RE = re.compile(r'#\s*include\s*"([^"]+)"')


def _ceg_targets_cpp(
    internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    """C++ quote-form includes → PATH-resolved ``file:`` CEG nodes.

    Modeled on ``_ceg_targets_ts_js`` (path-resolved ``file:`` nodes), NOT the
    Java module-node resolver — C++ resolution is PATH-based. A quote-form
    include already carries its extension, so there is no suffix synthesis: the
    path is resolved relative to the including file first, then under the
    conventional include roots, and emitted as a ``file:`` node keyed by the
    in-tree relative path. Angle-form includes never reach ``internal`` (they are
    classified ``external`` by ``_imports_cpp``), so only first-party headers
    become nodes.
    """
    targets: list[CegImportTarget] = []
    seen: set[str] = set()
    for include_lines in internal.values():
        for line in include_lines:
            match = _CPP_INCLUDE_LINE_RE.search(line)
            if not match:
                continue
            spec = match.group(1)
            resolved = _resolve_cpp_include_path(spec, project_root, file_path)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            targets.append(
                CegImportTarget(
                    target_id=f"file:{resolved}",
                    node_type="file",
                    node_kwargs={"path": resolved},
                    evidence_method="cpp_include",
                    confidence=0.95,
                )
            )
    return targets


def _resolve_cpp_include_path(
    spec: str, project_root: Path, file_path: Path
) -> str | None:
    """Resolve a quote-form include path to an in-tree posix path, or ``None``.

    Delegates candidate generation to the SHARED
    :func:`codd.parsing._shared.cpp_include_candidate_paths` (the same one the DAG
    builder uses) so the scan and builder resolvers cannot drift (GENERIC FIX 3 —
    the LevelDB 59%-scan-edge-loss class). Returns the project-relative posix path
    of the first candidate that exists on disk AND lies inside the project tree
    (so an out-of-tree ``../`` escape yields no node → no false edge).
    """
    project_root_resolved = project_root.resolve()
    for candidate in cpp_include_candidate_paths(spec, file_path, project_root_resolved):
        if not candidate.is_file():
            continue
        try:
            return candidate.relative_to(project_root_resolved).as_posix()
        except ValueError:
            continue
    return None


#: C# ``using`` directive (mirrors the scan/builder regexes) for recovering the
#: imported namespace from a stored ``internal`` line.
_CSHARP_USING_LINE_RE = re.compile(
    r'(?:global\s+)?using\s+(?:static\s+)?(?:[\w.]+\s*=\s*)?([\w.]+)\s*;'
)


def _ceg_targets_csharp(
    internal: dict, project_root: Path, file_path: Path
) -> "list[CegImportTarget]":
    """C# internal usings → ``module`` CEG nodes keyed by first-party namespace.

    Mirrors ``_ceg_targets_java`` (a stable ``module:`` identifier per imported
    first-party unit) rather than probing the filesystem — C# resolution is
    NAMESPACE-based, not path-based, so there is no single file a ``using``
    points at; the DAG builder owns the precise namespace→declaring-files
    resolution for import EDGES via its reverse-index. The ``internal`` map values
    are the raw using lines (``using Dapper.ProviderTools;`` / ``using static
    Dapper.SqlMapper;`` / ``using Foo = Dapper.X;``); we recover the imported
    namespace for the node.
    """
    targets: list[CegImportTarget] = []
    seen: set[str] = set()
    for using_lines in internal.values():
        for line in using_lines:
            match = _CSHARP_USING_LINE_RE.search(line)
            if not match:
                continue
            namespace = match.group(1)
            if not namespace or namespace in seen:
                continue
            seen.add(namespace)
            targets.append(
                CegImportTarget(
                    target_id=f"module:{namespace}",
                    node_type="module",
                    node_kwargs={"name": namespace},
                    evidence_method="csharp_using",
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
    "java": _ceg_targets_java,
    "cpp": _ceg_targets_cpp,
    "csharp": _ceg_targets_csharp,
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

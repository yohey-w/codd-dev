"""CoDD Extractor — Reverse-engineer design documents from existing codebases.

Philosophy: In V-Model, intent lives only in requirements.
Everything below (architecture, detailed design, test) is structural fact.
Extract structural facts. Don't guess intent.

Two-phase architecture:
  Phase 1 (extract-facts): Deterministic static analysis — no AI.
  Phase 2 (synth-docs): Template-based Markdown generation with frontmatter.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.parsing import (
    BuildDepsExtractor,
    BuildDepsInfo,
    ConfigInfo,
    DockerComposeExtractor,
    GraphQlExtractor,
    KubernetesExtractor,
    OpenApiExtractor,
    ProtobufExtractor,
    TerraformExtractor,
    TestExtractor,
    TestInfo,
    get_extractor,
)


# ═══════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════

@dataclass
class Symbol:
    """A class or function extracted from source code."""
    name: str
    kind: str           # "class" | "function" | "interface" | "type_alias" | "enum"
    file: str           # relative path
    line: int
    params: str = ""    # function parameters
    return_type: str = ""
    decorators: list[str] = field(default_factory=list)
    visibility: str = "public"
    is_async: bool = False
    bases: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)


@dataclass
class CallEdge:
    """A function-to-function call relationship."""
    caller: str          # "module.Class.method" or "module.function"
    callee: str          # target symbol (resolved to module if possible)
    call_site: str       # file:line
    is_async: bool = False


@dataclass
class ModuleInfo:
    """Aggregated info for a discovered module/package."""
    name: str
    files: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    internal_imports: dict[str, list[str]] = field(default_factory=dict)  # module -> [import lines]
    external_imports: set[str] = field(default_factory=set)
    test_files: list[str] = field(default_factory=list)
    test_details: list[TestInfo] = field(default_factory=list)
    line_count: int = 0
    patterns: dict[str, str] = field(default_factory=dict)  # pattern_type -> detail
    call_edges: list[CallEdge] = field(default_factory=list)
    interface_contract: Any = None  # InterfaceContract from contracts.py
    test_coverage: Any = None       # TestCoverage from traceability.py
    schema_refs: list[Any] = field(default_factory=list)    # SchemaRef from schema_refs.py
    runtime_wires: list[Any] = field(default_factory=list)  # RuntimeWire from wiring.py


@dataclass
class FeatureCluster:
    """A group of modules that collaborate on a feature."""
    name: str
    modules: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class ProjectFacts:
    """All facts extracted from a project."""
    language: str
    source_dirs: list[str]
    modules: dict[str, ModuleInfo] = field(default_factory=dict)
    entry_points: list[str] = field(default_factory=list)
    detected_frameworks: list[str] = field(default_factory=list)
    detected_test_framework: str = ""
    detected_orm: str = ""
    total_files: int = 0
    total_lines: int = 0
    schemas: dict[str, Any] = field(default_factory=dict)
    api_specs: dict[str, Any] = field(default_factory=dict)
    infra_config: dict[str, ConfigInfo] = field(default_factory=dict)
    build_deps: BuildDepsInfo | None = None
    feature_clusters: list[FeatureCluster] = field(default_factory=list)
    change_risks: list[Any] = field(default_factory=list)  # ChangeRisk from risk.py


@dataclass
class ExtractResult:
    """Result of codd extract."""
    output_dir: Path
    generated_files: list[Path]
    module_count: int
    total_files: int
    total_lines: int


# ═══════════════════════════════════════════════════════════
# Phase 1: Extract Facts (deterministic, no AI)
# ═══════════════════════════════════════════════════════════

def extract_facts(project_root: Path, language: str | None = None,
                  source_dirs: list[str] | None = None,
                  exclude_patterns: list[str] | None = None) -> ProjectFacts:
    """Extract structural facts from source code. Pure static analysis."""

    if exclude_patterns is None:
        exclude_patterns = [
            "**/node_modules/**", "**/__pycache__/**", "**/dist/**",
            "**/.git/**", "**/venv/**", "**/.venv/**", "**/vendor/**",
            "**/.tox/**", "**/build/**", "**/*.egg-info/**",
        ]

    # Auto-detect language if not provided
    if language is None:
        language = _detect_language(project_root, exclude_patterns)

    # Auto-detect source dirs if not provided
    if source_dirs is None:
        source_dirs = _detect_source_dirs(project_root, language)

    facts = ProjectFacts(language=language, source_dirs=source_dirs)

    # Discover modules
    for src_dir in source_dirs:
        src_path = project_root / src_dir
        if not src_path.exists():
            continue
        _discover_modules(facts, project_root, src_path, language, exclude_patterns)

    # Discover DDL / schema artifacts
    _discover_schemas(facts, project_root, exclude_patterns)

    # Detect API definition files
    _discover_api_specs(facts, project_root)

    # Detect infrastructure/build metadata
    _discover_config(facts, project_root)
    facts.build_deps = _discover_build_deps(project_root)

    # Detect test files and map to modules
    test_dirs = _detect_test_dirs(project_root)
    for test_dir in test_dirs:
        test_path = project_root / test_dir
        if test_path.exists():
            _map_tests_to_modules(facts, project_root, test_path, language)

    # Detect frameworks, ORM, test framework
    _detect_patterns(facts, project_root)

    # Detect entry points
    _detect_entry_points(facts, project_root, language)

    # R4.3: Interface contract detection
    from codd.contracts import build_interface_contracts
    build_interface_contracts(facts, project_root)

    # R4.1: Call graph extraction + resolution
    _extract_call_graphs(facts, project_root, language, exclude_patterns)
    _resolve_call_graph(facts)

    # R4.2: Feature clustering
    from codd.clustering import build_feature_clusters
    build_feature_clusters(facts)

    # R5.1: Test traceability
    from codd.traceability import build_test_traceability
    build_test_traceability(facts, project_root)

    # R5.2: Schema-code dependency
    from codd.schema_refs import build_schema_refs
    build_schema_refs(facts, project_root)

    # R5.3: Runtime wiring detection
    from codd.wiring import build_runtime_wires
    build_runtime_wires(facts, project_root)

    # R5.4: Change risk scoring (depends on R4.3, R5.1)
    from codd.risk import build_change_risks
    build_change_risks(facts)

    return facts


def _detect_language(project_root: Path, exclude_patterns: list[str]) -> str:
    """Detect primary language by file count."""
    counts: dict[str, int] = {}
    ext_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".java": "java", ".go": "go",
    }

    for root, dirs, files in os.walk(project_root):
        rel = Path(root).relative_to(project_root).as_posix()
        if any(_match_glob(rel + "/x", pat) for pat in exclude_patterns):
            dirs.clear()
            continue
        for f in files:
            ext = Path(f).suffix
            if ext in ext_map:
                lang = ext_map[ext]
                counts[lang] = counts.get(lang, 0) + 1

    if not counts:
        return "python"
    return max(counts, key=counts.get)


def _detect_source_dirs(project_root: Path, language: str) -> list[str]:
    """Auto-detect source directories."""
    candidates = ["src", "lib", "app", "pkg", "cmd", "internal"]
    found = []

    for c in candidates:
        if (project_root / c).is_dir():
            found.append(c)

    if not found:
        # Use project root if no standard dirs found
        # Look for source files directly
        exts = _language_extensions(language)
        for item in project_root.iterdir():
            if item.is_dir() and not item.name.startswith(".") and item.name not in (
                "tests", "test", "docs", "doc", "node_modules", "__pycache__",
                "dist", "build", "venv", ".venv", "vendor", "codd",
            ):
                # Check if dir has source files
                for f in item.iterdir():
                    if f.is_file() and f.suffix in exts:
                        found.append(item.name)
                        break

    # If still nothing, use "." (project root itself)
    if not found:
        found = ["."]

    return found


def _detect_test_dirs(project_root: Path) -> list[str]:
    """Auto-detect test directories."""
    candidates = ["tests", "test", "spec", "__tests__"]
    found = []
    for c in candidates:
        if (project_root / c).is_dir():
            found.append(c)
    return found


def _language_extensions(language: str) -> set[str]:
    return {
        "python": {".py"},
        "typescript": {".ts", ".tsx"},
        "javascript": {".js", ".jsx"},
        "java": {".java"},
        "go": {".go"},
    }.get(language, set())


def _discover_modules(facts: ProjectFacts, project_root: Path, src_dir: Path,
                      language: str, exclude_patterns: list[str]):
    """Walk source tree and discover modules with their symbols and imports."""
    exts = _language_extensions(language)
    extractor = get_extractor(language, "source")

    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(project_root).as_posix()
        if any(_match_glob(rel_root + "/x", pat) for pat in exclude_patterns):
            dirs.clear()
            continue

        for fname in files:
            if Path(fname).suffix not in exts:
                continue

            full = Path(root) / fname
            rel = full.relative_to(project_root).as_posix()

            if any(_match_glob(rel, pat) for pat in exclude_patterns):
                continue

            # Determine module name
            module_name = _file_to_module(rel, project_root, src_dir, language)

            if module_name not in facts.modules:
                facts.modules[module_name] = ModuleInfo(name=module_name)

            mod = facts.modules[module_name]
            mod.files.append(rel)

            # Count lines
            try:
                content = full.read_text(errors="ignore")
                lines = len(content.splitlines())
                mod.line_count += lines
                facts.total_lines += lines
            except Exception:
                content = ""
                lines = 0

            facts.total_files += 1

            # Extract symbols
            symbols = extractor.extract_symbols(content, rel)
            mod.symbols.extend(symbols)

            # Extract imports
            internal, external = extractor.extract_imports(content, full, project_root, src_dir)
            for imp_module, imp_lines in internal.items():
                mod.internal_imports.setdefault(imp_module, []).extend(imp_lines)
            mod.external_imports.update(external)


def _discover_schemas(facts: ProjectFacts, project_root: Path, exclude_patterns: list[str]):
    """Collect schema artifacts such as SQL DDL and Prisma schema files."""
    schema_languages = {
        ".prisma": "prisma",
        ".sql": "sql",
    }

    for root, dirs, files in os.walk(project_root):
        rel_root = Path(root).relative_to(project_root).as_posix()
        if any(_match_glob(rel_root + "/x", pat) for pat in exclude_patterns):
            dirs.clear()
            continue

        for fname in files:
            language = schema_languages.get(Path(fname).suffix)
            if language is None:
                continue

            full = Path(root) / fname
            rel = full.relative_to(project_root).as_posix()
            if any(_match_glob(rel, pat) for pat in exclude_patterns):
                continue

            try:
                content = full.read_text(errors="ignore")
            except Exception:
                continue

            extractor = get_extractor(language, "schema")
            schema_info = extractor.extract_schema(content, full)
            if _schema_has_data(schema_info):
                facts.schemas[rel] = schema_info


def _schema_has_data(schema_info: Any) -> bool:
    if schema_info is None:
        return False
    return any(
        getattr(schema_info, attr, None)
        for attr in ("tables", "foreign_keys", "indexes", "views", "models", "enums")
    )


def _file_to_module(rel_path: str, project_root: Path, src_dir: Path,
                    language: str) -> str:
    """Map a file path to its module name."""
    rel_to_src = Path(rel_path).relative_to(src_dir.relative_to(project_root))

    if language == "python":
        parts = list(rel_to_src.parts)
        # Remove .py extension from last part
        if parts and parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        # If file is __init__.py, module is the parent directory
        if parts and parts[-1] == "__init__":
            parts.pop()
        # Top-level module name
        if parts:
            return parts[0]
        return rel_to_src.parent.name or "root"

    elif language in ("typescript", "javascript"):
        parts = list(rel_to_src.parts)
        if parts:
            return parts[0]
        return "root"

    elif language == "java":
        # Use top-level directory under src
        parts = list(rel_to_src.parts)
        # Skip standard layout dirs (main/java, main/kotlin)
        skip = {"main", "java", "kotlin", "scala"}
        parts = [p for p in parts if p not in skip]
        if parts:
            return parts[0]
        return "root"

    elif language == "go":
        parts = list(rel_to_src.parts)
        if parts:
            return parts[0]
        return "root"

    return rel_to_src.parts[0] if rel_to_src.parts else "root"


def _extract_symbols(content: str, rel_path: str, language: str) -> list[Symbol]:
    """Extract classes and functions from source code (regex-based MVP)."""
    symbols = []

    if language == "python":
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r'^\s*class\s+(\w+)', line)
            if m:
                symbols.append(Symbol(m.group(1), "class", rel_path, i))
            m = re.match(r'^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)', line)
            if m and not m.group(1).startswith("_"):
                symbols.append(Symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))

    elif language in ("typescript", "javascript"):
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r'^(?:export\s+)?class\s+(\w+)', line)
            if m:
                symbols.append(Symbol(m.group(1), "class", rel_path, i))
            m = re.match(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', line)
            if m:
                symbols.append(Symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))
            # Arrow function exports
            m = re.match(r'^export\s+const\s+(\w+)\s*=\s*(?:async\s+)?\(', line)
            if m:
                symbols.append(Symbol(m.group(1), "function", rel_path, i))

    elif language == "java":
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)', line)
            if m:
                symbols.append(Symbol(m.group(1), "class", rel_path, i))
            m = re.match(r'^\s*(?:public|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(([^)]*)\)', line)
            if m and m.group(1)[0].islower():
                symbols.append(Symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))

    elif language == "go":
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(r'^type\s+(\w+)\s+struct\s*\{', line)
            if m:
                symbols.append(Symbol(m.group(1), "class", rel_path, i))
            m = re.match(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)', line)
            if m and m.group(1)[0].isupper():
                symbols.append(Symbol(m.group(1), "function", rel_path, i, m.group(2).strip()))

    return symbols


def _extract_imports(content: str, language: str, project_root: Path,
                     src_dir: Path, file_path: Path) -> tuple[dict[str, list[str]], set[str]]:
    """Extract imports, classified as internal or external.

    Returns (internal_imports: {module_name: [import_lines]}, external_imports: set)
    """
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    if language == "python":
        # Determine package name of the source dir itself
        src_pkg_name = src_dir.name if (src_dir / "__init__.py").exists() else None

        for line in content.splitlines():
            m = re.match(r'^(?:from|import)\s+([\w.]+)', line.strip())
            if not m:
                continue
            module = m.group(1)
            parts = module.split(".")
            top_level = parts[0]

            # Check if it's an internal module
            is_internal = False
            internal_key = top_level

            # Case 1: import references the source package itself (e.g., from codd.graph)
            if src_pkg_name and top_level == src_pkg_name and len(parts) >= 2:
                is_internal = True
                internal_key = parts[1]  # Use sub-module as the key
            else:
                # Case 2: import references a sibling directory/file
                for sd in [src_dir] + [project_root / d for d in ("src", "lib", "app") if (project_root / d).is_dir()]:
                    if (sd / top_level).is_dir() or (sd / f"{top_level}.py").is_file():
                        is_internal = True
                        break

            if is_internal:
                internal.setdefault(internal_key, []).append(line.strip())
            else:
                external.add(top_level)

    elif language in ("typescript", "javascript"):
        for line in content.splitlines():
            m = re.search(r'''(?:import|from)\s+['"]([^'"]+)['"]''', line)
            if not m:
                continue
            import_path = m.group(1)
            if import_path.startswith("."):
                # Relative import → internal
                resolved = (file_path.parent / import_path).resolve()
                try:
                    rel = resolved.relative_to(src_dir)
                    top_level = rel.parts[0] if rel.parts else "root"
                    internal.setdefault(top_level, []).append(line.strip())
                except ValueError:
                    external.add(import_path)
            elif import_path.startswith("@"):
                # Scoped package
                parts = import_path.split("/")
                pkg = "/".join(parts[:2]) if len(parts) >= 2 else import_path
                external.add(pkg)
            else:
                external.add(import_path.split("/")[0])

    elif language == "go":
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

    # Remove stdlib-like imports from external
    external -= _common_stdlib(language)

    return internal, external


def _common_stdlib(language: str) -> set[str]:
    """Return common stdlib modules to exclude from external imports."""
    if language == "python":
        return {
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
        }
    return set()


def _map_tests_to_modules(facts: ProjectFacts, project_root: Path,
                          test_dir: Path, language: str):
    """Map test files to their target modules."""
    exts = _language_extensions(language)
    source_extractor = get_extractor(language, "source")
    test_extractor = TestExtractor(language)

    for root, dirs, files in os.walk(test_dir):
        for fname in files:
            if Path(fname).suffix not in exts or not test_extractor._is_test_file(fname):
                continue
            full = Path(root) / fname
            rel = full.relative_to(project_root).as_posix()
            try:
                content = full.read_text(errors="ignore")
            except Exception:
                content = ""
            test_info = test_extractor.extract_test_info(content, rel)

            # Try to match test file to module
            target_module = _guess_test_target(fname, language)
            if target_module and target_module in facts.modules:
                test_info.source_module = target_module
                _attach_test_info(facts.modules[target_module], rel, test_info)
            else:
                # Try reading imports to find target
                src_dir = project_root / facts.source_dirs[0] if facts.source_dirs else project_root
                try:
                    internal, _ = source_extractor.extract_imports(content, full, project_root, src_dir)
                    for mod_name in internal:
                        if mod_name in facts.modules:
                            test_info.source_module = mod_name
                            _attach_test_info(facts.modules[mod_name], rel, test_info)
                            break
                except Exception:
                    pass


def _guess_test_target(test_filename: str, language: str) -> str | None:
    """Guess which module a test file targets from its name."""
    name = Path(test_filename).stem

    if language == "python":
        # test_foo.py → foo
        if name.startswith("test_"):
            return name[5:]
    elif language in ("typescript", "javascript"):
        # foo.test.ts → foo, foo.spec.ts → foo
        for suffix in (".test", ".spec"):
            if name.endswith(suffix):
                return name[: -len(suffix)]

    return None


def _attach_test_info(module: ModuleInfo, rel_path: str, test_info: TestInfo):
    """Attach test metadata without duplicating filenames or test details."""
    if rel_path not in module.test_files:
        module.test_files.append(rel_path)
    if not any(
        existing.file_path == test_info.file_path and existing.source_module == test_info.source_module
        for existing in module.test_details
    ):
        module.test_details.append(test_info)


def _detect_patterns(facts: ProjectFacts, project_root: Path):
    """Detect frameworks, ORMs, test frameworks from project files."""

    # Check package manager files
    pyproject = project_root / "pyproject.toml"
    setup_py = project_root / "setup.py"
    package_json = project_root / "package.json"
    go_mod = project_root / "go.mod"
    pom_xml = project_root / "pom.xml"

    if pyproject.exists():
        content = pyproject.read_text(errors="ignore")
        _detect_python_patterns(facts, content)
    elif setup_py.exists():
        content = setup_py.read_text(errors="ignore")
        _detect_python_patterns(facts, content)

    if package_json.exists():
        content = package_json.read_text(errors="ignore")
        _detect_js_patterns(facts, content)

    if go_mod.exists():
        content = go_mod.read_text(errors="ignore")
        _detect_go_patterns(facts, content)

    # Scan source files for framework-specific patterns
    extractor = get_extractor(facts.language, "source")
    for mod in facts.modules.values():
        for fpath in mod.files:
            try:
                content = (project_root / fpath).read_text(errors="ignore")
            except Exception:
                continue
            extractor.detect_code_patterns(mod, content)


def _detect_python_patterns(facts: ProjectFacts, content: str):
    frameworks = {
        "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
        "starlette": "Starlette", "tornado": "Tornado", "aiohttp": "aiohttp",
    }
    orms = {
        "sqlalchemy": "SQLAlchemy", "django": "Django ORM",
        "tortoise-orm": "Tortoise ORM", "peewee": "Peewee",
        "sqlmodel": "SQLModel", "prisma": "Prisma",
    }
    test_fw = {
        "pytest": "pytest", "unittest": "unittest", "nose": "nose2",
    }

    for key, name in frameworks.items():
        if key in content.lower():
            facts.detected_frameworks.append(name)
    for key, name in orms.items():
        if key in content.lower() and not facts.detected_orm:
            facts.detected_orm = name
    for key, name in test_fw.items():
        if key in content.lower() and not facts.detected_test_framework:
            facts.detected_test_framework = name


def _detect_js_patterns(facts: ProjectFacts, content: str):
    frameworks = {
        '"next"': "Next.js", '"react"': "React", '"express"': "Express",
        '"nestjs"': "NestJS", '"@nestjs/core"': "NestJS",
        '"vue"': "Vue.js", '"nuxt"': "Nuxt", '"angular"': "Angular",
        '"hono"': "Hono", '"fastify"': "Fastify",
    }
    orms = {
        '"prisma"': "Prisma", '"typeorm"': "TypeORM",
        '"sequelize"': "Sequelize", '"drizzle-orm"': "Drizzle",
        '"mongoose"': "Mongoose", '"@prisma/client"': "Prisma",
    }
    test_fw = {
        '"jest"': "Jest", '"vitest"': "Vitest", '"mocha"': "Mocha",
        '"playwright"': "Playwright", '"cypress"': "Cypress",
    }

    for key, name in frameworks.items():
        if key in content:
            facts.detected_frameworks.append(name)
    for key, name in orms.items():
        if key in content and not facts.detected_orm:
            facts.detected_orm = name
    for key, name in test_fw.items():
        if key in content and not facts.detected_test_framework:
            facts.detected_test_framework = name


def _detect_go_patterns(facts: ProjectFacts, content: str):
    frameworks = {
        "gin-gonic/gin": "Gin", "gorilla/mux": "Gorilla Mux",
        "labstack/echo": "Echo", "gofiber/fiber": "Fiber",
    }
    for key, name in frameworks.items():
        if key in content:
            facts.detected_frameworks.append(name)


def _detect_code_patterns(mod: ModuleInfo, content: str, language: str):
    """Detect API routes, DB models from source code."""
    if language == "python":
        if re.search(r'@(?:app|router)\.(get|post|put|delete|patch)\s*\(', content):
            mod.patterns["api_routes"] = "HTTP route handlers"
        if re.search(r'class\s+\w+\(.*(?:Base|Model|db\.Model)\)', content):
            mod.patterns["db_models"] = "ORM models"
        if re.search(r'@(?:celery_app|app)\.task', content):
            mod.patterns["background_tasks"] = "Async task handlers"

    elif language in ("typescript", "javascript"):
        if re.search(r'(?:app|router)\.(get|post|put|delete|patch)\s*\(', content):
            mod.patterns["api_routes"] = "HTTP route handlers"
        if re.search(r'@(?:Controller|Get|Post|Put|Delete|Patch)\s*\(', content):
            mod.patterns["api_routes"] = "NestJS controller"
        if re.search(r'(?:schema|model)\s*\(', content, re.IGNORECASE):
            mod.patterns["db_models"] = "Database models"


def _detect_entry_points(facts: ProjectFacts, project_root: Path, language: str):
    """Find likely entry points (main files)."""
    candidates = {
        "python": ["main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "__main__.py"],
        "typescript": ["index.ts", "main.ts", "app.ts", "server.ts"],
        "javascript": ["index.js", "main.js", "app.js", "server.js"],
        "java": ["Application.java", "Main.java", "App.java"],
        "go": ["main.go", "cmd/main.go"],
    }

    for candidate in candidates.get(language, []):
        for src_dir in facts.source_dirs:
            path = project_root / src_dir / candidate
            if path.exists():
                facts.entry_points.append(f"{src_dir}/{candidate}")
        # Also check project root
        path = project_root / candidate
        if path.exists():
            facts.entry_points.append(candidate)


def _discover_api_specs(facts: ProjectFacts, project_root: Path):
    """Collect OpenAPI, GraphQL, and protobuf specs across the project."""
    extractors = [
        (
            OpenApiExtractor(),
            "detect_openapi_files",
            "extract_endpoints",
        ),
        (
            GraphQlExtractor(),
            "detect_graphql_files",
            "extract_schema",
        ),
        (
            ProtobufExtractor(),
            "detect_proto_files",
            "extract_services",
        ),
    ]

    for extractor, detect_method_name, extract_method_name in extractors:
        detect_method = getattr(extractor, detect_method_name)
        extract_method = getattr(extractor, extract_method_name)
        for file_path in detect_method(project_root):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            relative_path = file_path.relative_to(project_root).as_posix()
            spec = extract_method(content, relative_path)
            if spec.endpoints or spec.schemas or spec.services:
                facts.api_specs[relative_path] = spec


def _discover_config(facts: ProjectFacts, project_root: Path):
    """Collect infrastructure/configuration metadata across the project."""
    extractors = [
        (
            DockerComposeExtractor(),
            "detect_docker_compose",
            "extract_services",
        ),
        (
            KubernetesExtractor(),
            "detect_k8s_manifests",
            "extract_manifests",
        ),
        (
            TerraformExtractor(),
            "detect_tf_files",
            "extract_resources",
        ),
    ]

    for extractor, detect_method_name, extract_method_name in extractors:
        detect_method = getattr(extractor, detect_method_name)
        extract_method = getattr(extractor, extract_method_name)
        for file_path in detect_method(project_root):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            relative_path = file_path.relative_to(project_root).as_posix()
            config = extract_method(content, relative_path)
            if config.services or config.resources:
                facts.infra_config[relative_path] = config


def _discover_build_deps(project_root: Path) -> BuildDepsInfo | None:
    """Collect dependency/build metadata from project manifests."""
    extractor = BuildDepsExtractor()
    discovered: list[BuildDepsInfo] = []

    for file_path in extractor.detect_build_files(project_root):
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        relative_path = file_path.relative_to(project_root).as_posix()
        build_info = extractor.extract_deps(content, file_path.name, relative_path)
        if build_info.runtime or build_info.dev or build_info.scripts:
            discovered.append(build_info)

    return extractor.merge(discovered)


# ── R4.1 helpers: call-graph extraction & resolution ──────

def _extract_call_graphs(facts: ProjectFacts, project_root: Path,
                         language: str, exclude_patterns: list[str] | None):
    """Collect call edges for every module using the language extractor."""
    extractor = get_extractor(language, "source")
    if not hasattr(extractor, "extract_call_graph"):
        return

    for mod in facts.modules.values():
        for rel_file in mod.files:
            full = project_root / rel_file
            try:
                content = full.read_text(errors="ignore")
            except Exception:
                continue
            edges = extractor.extract_call_graph(content, rel_file, mod.symbols)
            mod.call_edges.extend(edges)


def _resolve_call_graph(facts: ProjectFacts):
    """Resolve callee names to fully-qualified module.symbol references."""
    # Build symbol → module lookup
    symbol_to_module: dict[str, str] = {}
    for mod in facts.modules.values():
        for sym in mod.symbols:
            symbol_to_module[sym.name] = mod.name

    for mod in facts.modules.values():
        for edge in mod.call_edges:
            callee = edge.callee
            # Strip self. prefix
            if callee.startswith("self."):
                callee = callee[5:]
            # Try to resolve bare name to module.name
            bare = callee.split(".")[-1]
            if bare in symbol_to_module:
                target_mod = symbol_to_module[bare]
                if target_mod != mod.name:
                    edge.callee = f"{target_mod}.{bare}"
                else:
                    edge.callee = bare


# ═══════════════════════════════════════════════════════════
# Phase 2: Synth Docs (template-based, no AI)
# ═══════════════════════════════════════════════════════════

def synth_docs(facts: ProjectFacts, output_dir: Path) -> list[Path]:
    """Generate CoDD Markdown documents from extracted facts."""
    from codd.synth import synth_docs as synth_docs_impl

    return synth_docs_impl(facts, output_dir)


def synth_architecture(facts: ProjectFacts, output_dir: Path) -> Path:
    """Generate a project-level architecture overview from extracted facts."""
    from codd.synth import synth_architecture as synth_architecture_impl

    return synth_architecture_impl(facts, output_dir)


# ═══════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════

def run_extract(project_root: Path, language: str | None = None,
                source_dirs: list[str] | None = None,
                output: str | None = None) -> ExtractResult:
    """Run full extract pipeline: facts → docs."""

    # Try to load config if it exists
    from codd.config import find_codd_dir
    codd_dir = find_codd_dir(project_root)
    if codd_dir is not None and (language is None or source_dirs is None):
        codd_yaml = codd_dir / "codd.yaml"
        if codd_yaml.exists():
            config = yaml.safe_load(codd_yaml.read_text())
            if language is None:
                language = config.get("project", {}).get("language")
            if source_dirs is None:
                source_dirs = config.get("scan", {}).get("source_dirs")

    # Phase 1: Extract facts
    facts = extract_facts(project_root, language, source_dirs)

    # Phase 2: Generate docs
    if output:
        output_dir = Path(output)
    elif codd_dir is not None:
        output_dir = codd_dir / "extracted"
    else:
        output_dir = project_root / "codd" / "extracted"
    generated = synth_docs(facts, output_dir)

    return ExtractResult(
        output_dir=output_dir,
        generated_files=generated,
        module_count=len(facts.modules),
        total_files=facts.total_files,
        total_lines=facts.total_lines,
    )


# ═══════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════

def _match_glob(path: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern)

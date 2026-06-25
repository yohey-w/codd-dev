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
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from codd.bridge import load_bridge_registry
from codd.discovery import DEFAULT_IGNORED_DIRS, default_exclude_patterns
from codd.parsing import (
    AnsibleExtractor,
    BuildDepsExtractor,
    BuildDepsInfo,
    ConfigInfo,
    DockerComposeExtractor,
    DockerfileExtractor,
    GitHubActionsExtractor,
    GraphQlExtractor,
    KubernetesExtractor,
    OpenApiExtractor,
    OpsEvidenceExtractor,
    PrometheusRulesExtractor,
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
    kind: str           # "class" | "function" | "interface" | "type_alias" | "enum" | "const_object"
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
    env_refs: list[Any] = field(default_factory=list)      # EnvRef from env_refs.py


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
    inheritance_edges: list[Any] = field(default_factory=list)  # InheritanceEdge from inheritance.py


@dataclass
class ExtractResult:
    """Result of codd extract."""
    output_dir: Path
    generated_files: list[Path]
    module_count: int
    total_files: int
    total_lines: int
    language: str
    source_dirs: list[str]


def build_extract_init_metadata(project_root: Path, extracted_at: str | None = None) -> dict[str, str]:
    """Build generic brownfield extraction metadata for generated YAML/Markdown."""
    timestamp = extracted_at or datetime.now().astimezone().replace(microsecond=0).isoformat()
    return {
        "version": "1.0",
        "extracted_at": timestamp,
        "source": project_root.resolve().as_posix(),
    }


def add_extract_init_frontmatter(
    paths: list[Path],
    metadata: dict[str, str],
    output_dir: Path | None = None,
) -> None:
    """Add codd init metadata to generated Markdown frontmatter or YAML payloads.

    When *output_dir* is given, any path that resolves OUTSIDE it is skipped
    (fail-closed): ``--init`` must only annotate freshly-generated extract docs
    and must never rewrite existing source or user files.
    """
    base = output_dir.resolve() if output_dir is not None else None
    for path in paths:
        if base is not None:
            try:
                path.resolve().relative_to(base)
            except ValueError:
                # Path escapes the extract output dir — refuse to touch it.
                continue
        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            _upsert_markdown_codd_metadata(path, metadata)
        elif suffix in {".yaml", ".yml"}:
            _upsert_yaml_codd_metadata(path, metadata)


def _merge_codd_metadata(payload: Any, metadata: dict[str, str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    codd = payload.get("codd")
    if not isinstance(codd, dict):
        codd = {}
    codd.update(metadata)
    payload["codd"] = codd
    return payload


def _upsert_markdown_codd_metadata(path: Path, metadata: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = text[match.end():]
    else:
        frontmatter = {}
        body = text

    payload = _merge_codd_metadata(frontmatter, metadata)
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    separator = "" if body.startswith("\n") else "\n"
    path.write_text(f"---\n{rendered}---\n{separator}{body}", encoding="utf-8")


def _upsert_yaml_codd_metadata(path: Path, metadata: dict[str, str]) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged = _merge_codd_metadata(payload, metadata)
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ═══════════════════════════════════════════════════════════
# Phase 1: Extract Facts (deterministic, no AI)
# ═══════════════════════════════════════════════════════════

def extract_facts(project_root: Path, language: str | None = None,
                  source_dirs: list[str] | None = None,
                  exclude_patterns: list[str] | None = None) -> ProjectFacts:
    """Extract structural facts from source code. Pure static analysis."""

    if exclude_patterns is None:
        # Unified ignore set — single source of truth lives in codd.discovery.
        # Emits both top-level and nested glob forms (fnmatch's "**/x/**"
        # does not match a top-level "x/").
        exclude_patterns = default_exclude_patterns()

    # Auto-detect language if not provided
    if language is None:
        language = _detect_language(project_root, exclude_patterns)

    # Auto-detect source dirs if not provided
    if source_dirs is None:
        source_dirs = _detect_source_dirs(project_root, language)

    facts = ProjectFacts(language=language, source_dirs=source_dirs)

    # Discover modules. A shared ``seen_files`` set makes discovery idempotent so
    # an OVERLAPPING source-root cover (GENERIC FIX 2: a top-level dir plus the
    # root ``.`` completeness sweep) never lists a file twice. The root ``.`` sweep
    # excludes the project's test dirs so it does not pull tests into impl modules.
    test_dir_excludes = _test_dir_exclude_patterns(project_root)
    seen_files: set[str] = set()
    # ``rel file path`` → the source-root it was discovered under. The finalize
    # disambiguation pass uses this to detect a module whose files were merged
    # ACROSS parallel source roots (the basename-collapse pathology).
    file_src_dirs: dict[str, str] = {}
    for src_dir in source_dirs:
        src_path = project_root / src_dir
        if not src_path.exists():
            continue
        # The whole-tree ``.`` sweep additionally prunes test dirs (the narrower
        # source-root entries are scoped already and keep the legacy behaviour).
        dir_excludes = exclude_patterns + test_dir_excludes if src_dir == "." else exclude_patterns
        _discover_modules(
            facts, project_root, src_path, language, dir_excludes, seen_files,
            file_src_dirs, src_dir,
        )

    # GENERIC disambiguation (basename-collapse fix): same-named files reached via
    # PARALLEL source roots collapse into one module because the module key is the
    # first segment RELATIVE TO EACH src_dir, erasing the distinguishing root. The
    # finalize pass below re-keys ONLY such cross-root-merged modules — and only
    # when it can PROVE every dependency edge still resolves — so single-root
    # projects (and any split that would dangle an edge) are left byte-identical.
    _disambiguate_module_collisions(facts, project_root, language, file_src_dirs)

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

    # R5.4: Change risk scoring is provided by codd-pro when installed.
    risk_builder = load_bridge_registry().risk_builder
    if risk_builder is not None:
        risk_builder(facts)
    else:
        facts.change_risks = []

    # R8: Environment & config dependency detection
    from codd.env_refs import build_env_refs
    build_env_refs(facts, project_root)

    # R9: Inheritance chain analysis
    from codd.inheritance import build_inheritance_tree
    build_inheritance_tree(facts)

    return facts


def _detect_language(project_root: Path, exclude_patterns: list[str]) -> str:
    """Detect primary language by file count."""
    counts: dict[str, int] = {}
    ext_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".java": "java", ".go": "go",
        # C++ (incl. the common .cc/.cxx/.hh/.hpp variants). Listed as DATA so a
        # header-heavy project like fmt (15 headers + .cc sources) is detected as
        # ``cpp`` instead of vacuously falling back to ``python``.
        ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
        ".h": "cpp", ".hpp": "cpp", ".hh": "cpp",
        # C# (.cs). Listed as DATA so a C# project like Dapper (157 .cs files,
        # no .py at all) is detected as ``csharp`` instead of vacuously falling
        # back to ``python`` — the single change that fixes the false-green where
        # ``codd check`` PASSes on a 0-edge graph because the .cs files were inert.
        ".cs": "csharp",
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


# Conventional JVM (Maven/Gradle) source / test layout. Detected by directory
# PRESENCE (DATA), not a language gate, so a polyglot repo that happens to carry
# this layout is handled the same way. ``src`` is deliberately NOT used as a
# source dir when ``src/main/*`` exists, because the bare ``src`` glob would also
# sweep in ``src/test/*`` (mixing tests into impl).
_JVM_SOURCE_LAYOUT_DIRS = ("src/main/java", "src/main/kotlin", "src/main/scala")
_JVM_TEST_LAYOUT_DIRS = ("src/test/java", "src/test/kotlin", "src/test/scala")


#: Top-level directory names that are never IMPL source roots (tests, docs, build
#: artefacts, vendored deps, the codd metadata dir). The root ``.`` completeness
#: sweep prunes test dirs separately (``_test_dir_exclude_patterns``); this set is
#: what the source-bearing-top-level-dir COVER skips so a ``tests/`` package never
#: becomes an impl source root.
_NON_SOURCE_TOP_DIRS = frozenset(
    {"tests", "test", "spec", "__tests__", "docs", "doc", "codd"}
    | set(DEFAULT_IGNORED_DIRS)
)


def _dir_contains_source(directory: Path, exts: set[str]) -> bool:
    """True if ``directory`` holds a file with a source extension at ANY depth.

    The DATA primitive behind the generic cover: a top-level dir whose source
    lives only in a nested subpackage (Java BUG 2: ``util/`` → ``util/concurrent``)
    still counts as a source root. Ignored dirs are pruned during the walk.
    """
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]
        for fname in files:
            if Path(fname).suffix in exts:
                return True
    return False


def _detect_source_dirs(project_root: Path, language: str) -> list[str]:
    """Auto-detect source directories — generic and COMPLETE (GENERIC FIX 2).

    One mechanism, tolerant of arbitrary scoping, that COVERS every detected-
    language source file under the root (so nothing is silently dropped from
    extraction; the completeness accounting in the DAG builder warns if a gap
    remains):

    * Maven/Gradle ``src/main/<lang>`` layout → those dirs (test-safe; unchanged).
    * Otherwise the cover = every top-level dir that contains source AT ANY DEPTH
      (fixes Java BUG 2's nested-only ``util/concurrent``), EXCLUDING test / docs /
      build / vendored dirs, PLUS the root ``.`` when source files sit at the root
      level (fixes BUG A — root files alongside subpackages, 5 languages). The
      ``.`` entry's discovery prunes test dirs and de-dups against the narrower
      entries, so the cover may overlap harmlessly.
    * If nothing is found → ``["."]`` (project root).
    """
    jvm_source = [d for d in _JVM_SOURCE_LAYOUT_DIRS if (project_root / d).is_dir()]
    if jvm_source:
        # Maven/Gradle layout: point at ``src/main/<lang>`` so the impl glob does
        # not also pick up ``src/test/<lang>`` (which _detect_test_dirs claims).
        return jvm_source

    exts = _language_extensions(language)

    found: list[str] = []
    has_root_level_source = False
    for item in sorted(project_root.iterdir(), key=lambda p: p.name):
        if item.is_file():
            if item.suffix in exts:
                has_root_level_source = True
            continue
        if not item.is_dir():
            continue
        if item.name.startswith(".") or item.name in _NON_SOURCE_TOP_DIRS:
            continue
        if _dir_contains_source(item, exts):
            found.append(item.name)

    # BUG A: root-level source files alongside subpackages — add the root ``.`` as
    # a completeness sweep (test dirs pruned + files de-duped at discovery time).
    if has_root_level_source:
        found.append(".")

    # If still nothing, use "." (project root itself).
    if not found:
        found = ["."]

    return found


def _test_dir_exclude_patterns(project_root: Path) -> list[str]:
    """Glob excludes for the project's test dirs (used by the root ``.`` sweep).

    The whole-tree completeness sweep would otherwise pull test files into impl
    modules; excluding the detected test dirs keeps the impl/test split intact.
    The narrower source-root entries are scoped already, so this is applied only
    to the ``.`` sweep.
    """
    patterns: list[str] = []
    for test_dir in _detect_test_dirs(project_root):
        patterns.append(f"{test_dir}/**")
        patterns.append(f"**/{test_dir}/**")
    return patterns


def _detect_test_dirs(project_root: Path) -> list[str]:
    """Auto-detect test directories."""
    candidates = ["tests", "test", "spec", "__tests__"]
    found = []
    for c in candidates:
        if (project_root / c).is_dir():
            found.append(c)
    # JVM (Maven/Gradle) layout keeps tests under ``src/test/<lang>`` rather than
    # a top-level ``tests/``. Detected by directory presence (DATA), additive so
    # non-JVM projects are unaffected.
    for d in _JVM_TEST_LAYOUT_DIRS:
        if (project_root / d).is_dir():
            found.append(d)
    return found


def _language_extensions(language: str) -> set[str]:
    """Source-file extensions for ``language`` (registry-data lookup).

    Contract Kernel Cut Condition A: no language-name keyed dict in core — the
    extension table lives in the registry-DATA strategy. Byte-identical.
    """
    from codd.parsing.regex_strategies import language_extensions

    return language_extensions(language)


def _discover_modules(facts: ProjectFacts, project_root: Path, src_dir: Path,
                      language: str, exclude_patterns: list[str],
                      seen_files: set[str] | None = None,
                      file_src_dirs: dict[str, str] | None = None,
                      src_dir_name: str | None = None):
    """Walk source tree and discover modules with their symbols and imports.

    ``seen_files`` (shared across source roots) de-duplicates files so an
    overlapping source-root cover never discovers the same file twice.
    ``file_src_dirs`` (when given) records ``rel path`` → ``src_dir_name`` (the
    source-root entry the file came from), so the finalize disambiguation pass can
    detect a module merged across parallel source roots.
    """
    exts = _language_extensions(language)
    extractor = get_extractor(language, "source")
    if seen_files is None:
        seen_files = set()

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

            if rel in seen_files:
                continue
            seen_files.add(rel)
            if file_src_dirs is not None and src_dir_name is not None:
                file_src_dirs[rel] = src_dir_name

            # Determine module name
            module_name = _file_to_module(rel, project_root, src_dir, language)

            if module_name not in facts.modules:
                facts.modules[module_name] = ModuleInfo(name=module_name)

            mod = facts.modules[module_name]
            mod.files.append(rel)

            # Count lines
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
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


def _module_internal_import_keys_resolve(facts: ProjectFacts) -> bool:
    """True if EVERY module's internal-import keys resolve to a real module.

    The no-silent-drop invariant: ``synth`` only emits a dependency edge when
    ``dependency in facts.modules``. A re-key that leaves an import bucket pointing
    at a vanished key would SILENTLY drop that edge. This predicate lets the
    disambiguation pass fail-closed: if a proposed re-key would dangle ANY edge
    that resolved before, the pass reverts (so it can only ever PRESERVE edges).
    """
    keys = set(facts.modules)
    for module in facts.modules.values():
        for dep_key in module.internal_imports:
            if dep_key not in keys:
                return False
    return True


def _disambiguate_module_collisions(
    facts: ProjectFacts,
    project_root: Path,
    language: str,
    file_src_dirs: dict[str, str],
) -> None:
    """Split modules merged ACROSS parallel source roots (basename-collapse fix).

    GENERIC, language-agnostic (no ``if language ==`` branch), and fail-closed.

    Pathology: when several source roots are scanned, a module key is the first
    path segment RELATIVE TO EACH src_dir, so same-named files reached through
    DIFFERENT roots (``core/schemas.ts`` + ``classic/schemas.ts`` via roots
    ``core`` / ``classic``) collapse into ONE module — and one module doc —
    erasing the architecture.

    The single mechanism:

    * TRIGGER — only a module whose files originate from ≥2 distinct source roots
      (``file_src_dirs``) AND whose files all sit DIRECTLY under their root. For
      such files the project-root-relative first segment IS the distinguishing
      root, so the disambiguated module key and the re-resolved import key live in
      the SAME one-segment namespace (the load-bearing coupling — without it the
      now-dangling edges would be silently dropped). Single-root projects, and
      cross-root merges nested DEEPER under their roots, never trigger (the deeper
      case needs multi-segment module+import keys — broad cross-language churn in
      the parsing zone — and is deliberately left for a follow-up rather than
      risking dropped edges here).
    * RE-KEY — every file of a triggering module is re-assigned to the module named
      by its first PROJECT-ROOT-relative segment (the stripped distinguishing
      root). Non-triggering modules are untouched.
    * IMPORTS — triggering files' internal imports are RE-RESOLVED with
      ``src_dir = project_root`` so the buckets land in the same one-segment
      namespace as the new keys (this also RECOVERS cross-root edges that
      per-src_dir resolution had misclassified as external).

    FAIL-CLOSED: the whole transformation is applied to a COPY and kept only if
    :func:`_module_internal_import_keys_resolve` still holds (no dangling edge was
    introduced); otherwise the original facts are preserved unchanged. So the pass
    can only ever PRESERVE or REPAIR edges, never drop them.
    """
    # Which modules merge files from ≥2 distinct source roots?
    spanning: set[str] = set()
    for module in facts.modules.values():
        roots = {file_src_dirs.get(f) for f in module.files if f in file_src_dirs}
        roots.discard(None)
        if len(roots) >= 2:
            spanning.add(module.name)
    if not spanning:
        return

    # Restrict to spanning modules whose files all sit DIRECTLY under their root,
    # so the project-root-relative first segment is the whole distinguishing key
    # (one-segment module key == one-segment re-resolved import key). A deeper file
    # would need a multi-segment key the import strategies do not emit, so we skip
    # such modules entirely (they stay byte-identical — flagged for follow-up).
    triggering = {
        name for name in spanning
        if all(
            _file_directly_under_root(f, file_src_dirs.get(f))
            for f in facts.modules[name].files
            if f in file_src_dirs
        )
    }
    if not triggering:
        return

    extractor = get_extractor(language, "source")
    rebuilt: dict[str, ModuleInfo] = {}

    # Carry NON-triggering modules over verbatim (object identity preserved).
    for name, module in facts.modules.items():
        if name in triggering:
            continue
        rebuilt[name] = module

    # Re-key the triggering modules' files and re-resolve their imports.
    for name in triggering:
        old_mod = facts.modules[name]
        for rel in old_mod.files:
            new_key = _root_relative_first_segment(rel)
            mod = rebuilt.get(new_key)
            if mod is None:
                mod = ModuleInfo(name=new_key)
                rebuilt[new_key] = mod
            mod.files.append(rel)

            full = project_root / rel
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""
            mod.line_count += len(content.splitlines())
            mod.symbols.extend(extractor.extract_symbols(content, rel))
            # Re-resolve relative to the PROJECT ROOT so import keys match the
            # root-relative module keys (and cross-root edges resolve internally).
            internal, external = extractor.extract_imports(
                content, full, project_root, project_root
            )
            for imp_key, imp_lines in internal.items():
                mod.internal_imports.setdefault(imp_key, []).extend(imp_lines)
            mod.external_imports.update(external)

    # FAIL-CLOSED: keep the re-key ONLY if it introduced no dangling edge.
    candidate = ProjectFacts(language=facts.language, source_dirs=facts.source_dirs)
    candidate.modules = rebuilt
    if _module_internal_import_keys_resolve(candidate):
        facts.modules = rebuilt


def _file_directly_under_root(rel_path: str, src_dir_name: str | None) -> bool:
    """True if ``rel_path`` sits DIRECTLY inside source root ``src_dir_name``.

    ``core/schemas.ts`` under root ``core`` → True (one segment after the root).
    ``packages/websockets/foo.ts`` under ``packages`` → False (nested deeper).
    Root ``.`` means the file's own first segment is the distinguishing unit, so a
    top-level file (one segment) counts as directly-under.
    """
    if src_dir_name is None:
        return False
    parts = Path(rel_path).parts
    if src_dir_name == ".":
        return len(parts) == 1
    root_parts = Path(src_dir_name).parts
    return len(parts) == len(root_parts) + 1


def _root_relative_first_segment(rel_path: str) -> str:
    """The first segment of a file's PROJECT-ROOT-relative path.

    This is the distinguishing source root that per-src_dir module keying stripped
    (``core/schemas.ts`` → ``core``). A bare top-level file keys to its own name.
    ONE rule — both the module-key and the re-resolved import-key sides use it, so
    they stay in the same namespace.
    """
    parts = Path(rel_path).parts
    if len(parts) >= 2:
        return parts[0]
    return parts[0] if parts else "root"


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
                content = full.read_text(encoding="utf-8", errors="ignore")
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
    """Map a file path to its module name.

    Contract Kernel Cut Condition A: dispatches through the registry-DATA
    strategy's ``file_to_module`` rule (no ``if language ==`` branch). The
    generic strategy reproduces the former trailing ``return parts[0]``
    default; behaviour is byte-identical.
    """
    from codd.parsing.regex_strategies import strategy_for

    rel_to_src = Path(rel_path).relative_to(src_dir.relative_to(project_root))
    rule = strategy_for(language).file_to_module
    if rule is not None:
        result = rule(rel_to_src)
        if result is not None:
            return result
    return rel_to_src.parts[0] if rel_to_src.parts else "root"


def _extract_symbols(content: str, rel_path: str, language: str) -> list[Symbol]:
    """Extract classes and functions from source code (regex-based MVP).

    Contract Kernel Cut Condition A: this core function no longer branches on a
    language name — it dispatches through the registry-DATA strategy for
    ``language`` (see :mod:`codd.parsing.regex_strategies`). The per-language
    regex bodies live in that strategy table (the allowed extractor-impl zone);
    behaviour is byte-identical to the former inline ladder.
    """
    from codd.parsing.regex_strategies import strategy_for

    return strategy_for(language).symbols(content, rel_path)


def _extract_imports(content: str, language: str, project_root: Path,
                     src_dir: Path, file_path: Path) -> tuple[dict[str, list[str]], set[str]]:
    """Extract imports, classified as internal or external.

    Returns (internal_imports: {module_name: [import_lines]}, external_imports: set)

    Contract Kernel Cut Condition A: dispatches through the registry-DATA
    strategy for ``language`` (no ``if language ==`` branch here). The strategy
    also applies the per-language stdlib subtraction, byte-identical to the
    former inline ``external -= _common_stdlib(language)`` tail.
    """
    from codd.parsing.regex_strategies import strategy_for

    return strategy_for(language).imports(content, project_root, src_dir, file_path)


def _common_stdlib(language: str) -> set[str]:
    """Return common stdlib modules to exclude from external imports.

    Registry-data lookup (no language-name branch); empty for languages with no
    declared stdlib set, byte-identical to the former python-only ladder.
    """
    from codd.parsing.regex_strategies import common_stdlib

    return common_stdlib(language)


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
                content = full.read_text(encoding="utf-8", errors="ignore")
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
    """Guess which module a test file targets from its name.

    Contract Kernel Cut Condition A: dispatches through the registry-DATA
    strategy's ``guess_test_target`` rule (no ``if language ==`` branch);
    languages without a rule return ``None``, byte-identical to the former
    ladder.
    """
    from codd.parsing.regex_strategies import strategy_for

    name = Path(test_filename).stem
    rule = strategy_for(language).guess_test_target
    if rule is not None:
        return rule(name)
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
        content = pyproject.read_text(encoding="utf-8", errors="ignore")
        _detect_python_patterns(facts, content)
    elif setup_py.exists():
        content = setup_py.read_text(encoding="utf-8", errors="ignore")
        _detect_python_patterns(facts, content)

    if package_json.exists():
        content = package_json.read_text(encoding="utf-8", errors="ignore")
        _detect_js_patterns(facts, content)

    if go_mod.exists():
        content = go_mod.read_text(encoding="utf-8", errors="ignore")
        _detect_go_patterns(facts, content)

    # Scan source files for framework-specific patterns
    extractor = get_extractor(facts.language, "source")
    for mod in facts.modules.values():
        for fpath in mod.files:
            try:
                content = (project_root / fpath).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            extractor.detect_code_patterns(mod, content)


def _detect_python_patterns(facts: ProjectFacts, content: str):
    """No-op: framework/ORM/test detection is delegated to LLM (extract --ai).

    Removed in v1.36.0 to honor Generality Gate. Hard-coded framework/ORM/test
    dictionaries violated the constraint that CoDD core must remain stack-agnostic.
    The downstream AI extraction path (codd/extract_ai.py) infers these dynamically.
    """
    return


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
    """Detect API routes, DB models, page routes, auth redirects from source code.

    Contract Kernel Cut Condition A: dispatches through the registry-DATA
    strategy for ``language`` (no ``if language ==`` branch here). The
    per-language detection bodies live in the strategy table; behaviour is
    byte-identical to the former inline ladder.
    """
    from codd.parsing.regex_strategies import strategy_for

    strategy_for(language).code_patterns(mod, content)


def _detect_entry_points(facts: ProjectFacts, project_root: Path, language: str):
    """Find likely entry points (main files).

    Contract Kernel Cut Condition A: the per-language candidate list comes from
    the registry-DATA strategy (no language-name keyed dict in core).
    Byte-identical.
    """
    from codd.parsing.regex_strategies import entry_point_candidates

    for candidate in entry_point_candidates(language):
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
        (
            GitHubActionsExtractor(),
            "detect_workflow_files",
            "extract_workflow",
        ),
        (
            DockerfileExtractor(),
            "detect_dockerfiles",
            "extract_dockerfile",
        ),
        (
            AnsibleExtractor(),
            "detect_ansible_files",
            "extract_ansible",
        ),
        (
            PrometheusRulesExtractor(),
            "detect_prometheus_files",
            "extract_prometheus",
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
            if config.services or config.resources or config.pipelines or config.images:
                facts.infra_config[relative_path] = config

    # Recognition-only ops/observability/config-management evidence — the
    # FALLBACK layer. Ansible and Prometheus files are deep-parsed above; any
    # such file that produced no structured facts (malformed/empty/unsupported
    # shape), plus kinds with no deep parser yet (Helm Chart.yaml, role
    # defaults/vars), still surfaces its PRESENCE so the IaC→NFR layer can note
    # candidate observability/SLO and deployment-topology sources.
    ops_extractor = OpsEvidenceExtractor()
    for file_path, recognized_kind in ops_extractor.detect_ops_files(project_root):
        relative_path = file_path.relative_to(project_root).as_posix()
        if relative_path in facts.infra_config:
            continue
        facts.infra_config[relative_path] = ops_extractor.build_evidence(
            recognized_kind, relative_path
        )


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
                content = full.read_text(encoding="utf-8", errors="ignore")
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
                output: str | None = None,
                init_metadata: dict[str, str] | None = None) -> ExtractResult:
    """Run full extract pipeline: facts → docs."""

    # Try to load config if it exists
    from codd.config import find_codd_dir
    codd_dir = find_codd_dir(project_root)
    if codd_dir is not None and (language is None or source_dirs is None):
        codd_yaml = codd_dir / "codd.yaml"
        if codd_yaml.exists():
            config = yaml.safe_load(codd_yaml.read_text(encoding="utf-8"))
            if language is None:
                language = config.get("project", {}).get("language")
            if source_dirs is None:
                source_dirs = config.get("scan", {}).get("source_dirs")

    # Phase 1: Extract facts
    facts = extract_facts(project_root, language, source_dirs)

    # Phase 2: Generate docs
    if output:
        output_dir = Path(output)
    else:
        from codd.extract_paths import default_extract_output_dir
        output_dir = default_extract_output_dir(project_root)
    generated = synth_docs(facts, output_dir)
    if init_metadata is not None:
        add_extract_init_frontmatter(generated, init_metadata, output_dir=output_dir)

    return ExtractResult(
        output_dir=output_dir,
        generated_files=generated,
        module_count=len(facts.modules),
        total_files=facts.total_files,
        total_lines=facts.total_lines,
        language=facts.language,
        source_dirs=facts.source_dirs,
    )


# ═══════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════

def _match_glob(path: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


@dataclass
class DocumentUrlLinkInfo:
    """URLs extracted from a document node."""
    node_id: str
    urls: list[str]


class DocumentUrlLinker:
    """Scan design/requirement document text for URL patterns.

    Extracts URL strings referenced in documents (Mermaid diagrams, prose,
    code blocks) and returns them for downstream drift analysis.
    FW-agnostic: pattern driven by codd.yaml document_url_linking config.
    """

    DEFAULT_URL_PATTERN = r"(?:^|[\s`(\[])(/(?:[a-z0-9][a-z0-9/\-:_\[\]]*)?)"
    DEFAULT_EDGE_TYPE = "references"

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self._pattern = re.compile(
            cfg.get("url_pattern", self.DEFAULT_URL_PATTERN),
            re.MULTILINE,
        )
        self.edge_type = cfg.get("edge_type", self.DEFAULT_EDGE_TYPE)

    def extract_urls(self, text: str, node_id: str = "") -> DocumentUrlLinkInfo:
        """Extract and normalize URLs from document text."""
        raw = self._pattern.findall(text)
        normalized = sorted(set(self._normalize_url(url) for url in raw if url))
        return DocumentUrlLinkInfo(node_id=node_id, urls=normalized)

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        while url.endswith("]") and url.count("]") > url.count("["):
            url = url[:-1]
        if url != "/" and url.endswith("/"):
            url = url.rstrip("/")
        return url

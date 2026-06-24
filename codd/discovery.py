"""Single source of truth for "which files are part of the project".

Before this module existed, file discovery and exclusion logic was
re-implemented independently in at least three places and had silently
drifted apart:

* ``codd/parsing.py`` ``_IGNORED_DIR_NAMES`` knew about ``.terraform`` /
  ``.tox`` / ``site-packages`` but not ``.next`` / ``coverage`` / ``.cache``
  / ``env`` / ``tmp`` / ``.turbo``.
* ``codd/extract_ai.py`` ``SKIP_DIRS`` had the complementary asymmetry, and
  its ``SOURCE_EXTENSIONS`` covered fewer languages than the rest of the
  codebase (``codd/implementer.py`` ``LANGUAGE_EXTENSIONS``, the DAG suffix
  maps), so e.g. a Rust project produced divergent module inventories
  between deterministic and AI extraction.
* ``codd/extractor.py`` ``extract_facts`` had its own hardcoded default
  exclude pattern list.
* Config-driven excludes (``scan.exclude`` in codd.yaml) were read with
  three different access patterns, one of which (``config["scan"]``)
  crashed when the scan section was missing.

Everything below is intentionally generic (directory/extension names of
common toolchains only — no project-specific names).

Drift prevention: ``tests/test_discovery.py`` asserts that
``codd.parsing._IGNORED_DIR_NAMES``, ``codd.extract_ai.SKIP_DIRS`` and the
``codd.extractor`` defaults all resolve to the sets defined here.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Iterator

from codd.path_safety import resolve_project_path

# ═══════════════════════════════════════════════════════════
# Unified constants
# ═══════════════════════════════════════════════════════════

#: Directories that are never part of a project's own sources.
#:
#: This is the UNION of the two previously-drifted sets:
#:   parsing._IGNORED_DIR_NAMES = {.git, .terraform, .tox, .venv,
#:       __pycache__, build, dist, node_modules, site-packages, vendor, venv}
#:   extract_ai.SKIP_DIRS = {node_modules, .next, dist, build, coverage,
#:       .turbo, .cache, vendor, tmp, __pycache__, .git, .venv, venv, env}
DEFAULT_IGNORED_DIRS = frozenset({
    ".cache",          # generic tool cache (extract_ai)
    ".git",            # VCS metadata (both)
    ".next",           # Next.js build output (extract_ai)
    ".terraform",      # Terraform provider cache (parsing)
    ".tox",            # tox virtualenvs (parsing)
    ".turbo",          # Turborepo cache (extract_ai)
    ".venv",           # Python virtualenv (both)
    "__pycache__",     # Python bytecode cache (both)
    "build",           # generic build output (both)
    "coverage",        # coverage reports (extract_ai)
    "dist",            # generic distribution output (both)
    "env",             # Python virtualenv naming variant (extract_ai)
    "node_modules",    # npm/yarn/pnpm dependencies (both)
    "site-packages",   # installed Python packages (parsing)
    "tmp",             # scratch space (extract_ai)
    "vendor",          # vendored dependencies (both)
    "venv",            # Python virtualenv naming variant (both)
})

#: Source-code extensions recognized at the discovery layer.
#:
#: Union of the previously-divergent language coverage:
#:   * extract_ai.SOURCE_EXTENSIONS: .go .java .js .jsx .php .py .rb .ts .tsx .vue
#:   * extractor._language_extensions / _detect_language: .py .ts .tsx .js .jsx .java .go
#:   * implementer.LANGUAGE_EXTENSIONS and the DAG suffix maps (the broader
#:     language coverage the rest of the codebase already declares):
#:     .rs .kt .kts .swift .dart .cs .scala .ex .exs .svelte .c .cc .cpp .h .hpp
#:
#: Consumers that need to bound work (e.g. AI context windows) must cap
#: FILE COUNT / SIZE — never silently narrow the LANGUAGE coverage.
SOURCE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".dart", ".ex", ".exs",
    ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".svelte", ".swift", ".ts", ".tsx", ".vue",
})


def default_exclude_patterns(extra_names: Iterable[str] = ()) -> list[str]:
    """Glob exclude patterns equivalent to :data:`DEFAULT_IGNORED_DIRS`.

    Emits both ``name/**`` (top-level) and ``**/name/**`` (nested) per
    directory because ``fnmatch``-based matchers do not treat ``**/`` as
    "zero or more segments" — a bare ``**/node_modules/**`` silently fails
    to exclude a top-level ``node_modules/``.

    ``*.egg-info`` is included as a pattern (not a fixed name) because the
    directory name varies per package.
    """
    patterns: list[str] = []
    for name in sorted(DEFAULT_IGNORED_DIRS | set(extra_names)) + ["*.egg-info"]:
        patterns.append(f"{name}/**")
        patterns.append(f"**/{name}/**")
    return patterns


# ═══════════════════════════════════════════════════════════
# Config accessor
# ═══════════════════════════════════════════════════════════


def scan_exclude_patterns(config: dict[str, Any] | None) -> list[str]:
    """The one safe accessor for ``scan.exclude`` in codd.yaml.

    Never raises when the ``scan`` section (or the whole config) is missing
    — replaces the crash-prone ``config["scan"].get("exclude", [])`` pattern.
    Non-string / blank entries are dropped.
    """
    scan = (config or {}).get("scan") or {}
    if not isinstance(scan, dict):
        return []
    raw = scan.get("exclude") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


# ═══════════════════════════════════════════════════════════
# Path predicates and walker
# ═══════════════════════════════════════════════════════════


def matches_exclude_pattern(rel_path: str, pattern: str) -> bool:
    """Match one relative path against one user exclude glob.

    Plain patterns without a path separator or ``**`` (e.g. ``*.gen.py``)
    match the basename; path patterns match the full relative path.
    """
    if "/" not in pattern and "**" not in pattern:
        return fnmatch(rel_path.rsplit("/", 1)[-1], pattern)
    return fnmatch(rel_path, pattern)


def should_skip_path(
    path: Path | str,
    root: Path | str | None = None,
    *,
    ignored_dirs: Iterable[str] | None = None,
    exclude_patterns: Iterable[str] = (),
) -> bool:
    """True when ``path`` is not part of the project's own sources.

    A path is skipped when any of its directory components (relative to
    ``root`` when given) is in the ignored-dirs set, or when its relative
    path matches one of the ``exclude_patterns`` globs.
    """
    ignored = frozenset(ignored_dirs) if ignored_dirs is not None else DEFAULT_IGNORED_DIRS
    path = Path(path)
    if root is not None:
        try:
            parts = path.relative_to(Path(root)).parts
        except ValueError:
            parts = path.parts
    else:
        parts = path.parts
    if any(part in ignored for part in parts[:-1]):
        return True
    rel_text = "/".join(parts)
    return any(matches_exclude_pattern(rel_text, pattern) for pattern in exclude_patterns)


def iter_source_files(
    root: Path | str,
    *,
    source_dirs: Iterable[str] | None = None,
    extra_excludes: Iterable[str] = (),
    extensions: Iterable[str] | None = None,
    ignored_dirs: Iterable[str] | None = None,
    skip_hidden_dirs: bool = True,
) -> Iterator[Path]:
    """Shared project walker applying the unified ignore set consistently.

    Args:
        root: Project root directory.
        source_dirs: Relative directories to walk (default: the whole root).
        extra_excludes: User glob patterns (typically ``scan.exclude``).
        extensions: File suffixes to keep. ``None`` (default) means
            :data:`SOURCE_EXTENSIONS`; an explicit empty iterable means
            "all files" (no suffix filtering).
        ignored_dirs: Override for :data:`DEFAULT_IGNORED_DIRS`.
        skip_hidden_dirs: Prune dot-directories during the walk
            (``DEFAULT_IGNORED_DIRS`` already covers the common dot-dirs;
            this additionally prunes e.g. ``.github`` when True).
    """
    root = Path(root)
    ignored = frozenset(ignored_dirs) if ignored_dirs is not None else DEFAULT_IGNORED_DIRS
    exts = frozenset(extensions) if extensions is not None else SOURCE_EXTENSIONS
    excludes = [str(p) for p in extra_excludes if str(p).strip()]

    # Path-escape jail (RC-2): ``source_dirs`` comes from ``scan.source_dirs`` in
    # codd.yaml (user-controllable). A ``../`` traversal survives the dir
    # normalization (only slashes are stripped) and an in-root dir may be a
    # symlink whose target escapes the tree — either would walk/read files from
    # OUTSIDE the project. Confine each base dir through the shared jail (drop the
    # ones that resolve outside), so this single shared walker is transitively
    # safe for every consumer (env_refs/schema_refs/wiring/contracts/
    # traceability). ``None`` source_dirs ⇒ the whole root, which is in-root by
    # construction but still re-confined below to catch escaping symlinks.
    if source_dirs:
        bases: list[Path] = []
        for d in source_dirs:
            confined = resolve_project_path(root, d)
            if confined is not None:
                bases.append(confined)
    else:
        bases = [root]

    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(
                d for d in dirs
                if d not in ignored and not (skip_hidden_dirs and d.startswith("."))
            )
            current_path = Path(current)
            for filename in sorted(files):
                path = current_path / filename
                if exts and path.suffix not in exts:
                    continue
                if should_skip_path(path, root, ignored_dirs=ignored, exclude_patterns=excludes):
                    continue
                # Re-confine the walk result: an in-root tree may contain a symlink
                # whose target escapes the root — drop it (escape → not yielded).
                if resolve_project_path(root, path) is None:
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                yield path

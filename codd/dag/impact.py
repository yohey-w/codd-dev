"""Impact resolution: map a design document to affected implementation/test files.

Used by PHENOMENON-mode ``codd fix`` to decide deterministically which
implementation files (and their tests) must follow an applied design-doc
update. Resolution is DAG-first:

1. Forward ``expects`` edges from the design node identify expected
   implementation files (including one hop through lexicon ``expected``
   nodes via their ``represents`` edges).
2. Each implementation node's ``tested_by`` edges identify its test files.
3. When a design doc declares no ``expects`` edges, the frontmatter
   ``modules`` list falls back to filesystem candidate matching
   (:func:`find_impl_candidates` — the generalized form of the glob
   inference that ``codd.fixer`` has always used).

Everything here is generic: no project names, no framework assumptions
beyond conventional file layouts already encoded in the legacy fixer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_DOC_SUFFIX = ".md"

# Source-file suffixes the impact search walks. Generic across ecosystems —
# no framework assumption, just "files that hold implementation code". The
# legacy ``find_impl_candidates`` globs decide the back-compat path; this set
# only governs the v2 path-segment / content search helpers.
_SOURCE_SUFFIXES = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
    ".go",
    ".rb",
    ".java",
    ".kt",
    ".rs",
    ".php",
    ".cs",
    ".swift",
    ".scala",
    ".vue",
    ".svelte",
)

# Directories never worth walking for impact candidates.
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".codd",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".next",
        "dist",
        "build",
        "out",
        "coverage",
        ".tox",
        ".idea",
        ".vscode",
        "target",
        "vendor",
        "reports",
        "tmp",
        "logs",
        "__snapshots__",
    }
)

# Cap content reads so a pathological file cannot blow up the search.
_MAX_CONTENT_CHARS = 200_000


@dataclass
class ImplTargets:
    """Implementation/test files affected by one design document."""

    design_node_id: str
    impl_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)
    # "expects" (DAG edges), "frontmatter_modules" (glob fallback), "none"
    source: str = "none"


def affected_impl_targets(
    dag: Any,
    design_node_id: str,
    *,
    project_root: Path | None = None,
) -> ImplTargets:
    """Resolve implementation and test files affected by ``design_node_id``.

    Args:
        dag: a built :class:`codd.dag.DAG` (or duck-typed equivalent with
            ``nodes`` mapping and ``edges`` list).
        design_node_id: node id of the design document (relative posix path).
        project_root: required for the frontmatter ``modules`` filesystem
            fallback; when ``None`` the fallback is skipped.
    """
    targets = ImplTargets(design_node_id=design_node_id)
    nodes = getattr(dag, "nodes", {}) or {}
    design_node = nodes.get(design_node_id)
    if design_node is None:
        return targets

    forward: dict[str, dict[str, list[str]]] = {}
    for edge in getattr(dag, "edges", []) or []:
        forward.setdefault(edge.from_id, {}).setdefault(edge.kind, []).append(edge.to_id)

    impl_ids: list[str] = []
    seen_impl: set[str] = set()
    for target_id in forward.get(design_node_id, {}).get("expects", []):
        node = nodes.get(target_id)
        if node is None:
            continue
        if node.kind == "expected":
            # Lexicon expected-artifact node: hop through `represents`.
            for represented_id in forward.get(target_id, {}).get("represents", []):
                _append_code_node(nodes.get(represented_id), impl_ids, seen_impl)
            continue
        _append_code_node(node, impl_ids, seen_impl)

    source = "expects" if impl_ids else "none"

    if not impl_ids and project_root is not None:
        for module in _frontmatter_modules(design_node):
            for candidate in find_impl_candidates(Path(project_root), str(module)):
                normalized = Path(candidate).as_posix()
                if normalized not in seen_impl:
                    seen_impl.add(normalized)
                    impl_ids.append(normalized)
        if impl_ids:
            source = "frontmatter_modules"

    test_ids: list[str] = []
    seen_tests: set[str] = set()
    for impl_id in impl_ids:
        for test_id in forward.get(impl_id, {}).get("tested_by", []):
            node = nodes.get(test_id)
            if node is None:
                continue
            node_path = str(node.path or node.id)
            if node_path.endswith(_DOC_SUFFIX):
                continue
            if test_id not in seen_tests:
                seen_tests.add(test_id)
                test_ids.append(test_id)

    targets.impl_paths = sorted(impl_ids)
    targets.test_paths = sorted(test_ids)
    targets.source = source
    return targets


def _append_code_node(node: Any, impl_ids: list[str], seen: set[str]) -> None:
    """Collect a node when it represents implementation *code*.

    ``kind="common"`` is shared by frontmatter-declared common documents and
    ``common_node_patterns``-matched code files; ``.md`` is the codebase-wide
    doc discriminator (same principle as ``dependency_freshness``).
    """
    if node is None:
        return
    node_path = str(node.path or node.id)
    if node.kind == "impl_file" or (node.kind == "common" and not node_path.endswith(_DOC_SUFFIX)):
        path = Path(node_path).as_posix()
        if path not in seen:
            seen.add(path)
            impl_ids.append(path)


def _frontmatter_modules(node: Any) -> list[Any]:
    attributes = getattr(node, "attributes", None) or {}
    frontmatter = attributes.get("frontmatter") or {}
    if not isinstance(frontmatter, dict):
        return []
    modules = frontmatter.get("modules") or []
    if isinstance(modules, (list, tuple)):
        return list(modules)
    return [modules]


# ---------------------------------------------------------------------------
# Filesystem candidate matching (shared with codd.fixer — behavior-identical)
# ---------------------------------------------------------------------------


def is_test_path(path: str) -> bool:
    """Check if a path looks like a test file."""
    parts = path.replace("\\", "/").split("/")
    # Directory-based: tests/, __tests__/, test/, spec/
    if any(p in ("tests", "__tests__", "test", "spec") for p in parts):
        return True
    # File-based: *.spec.*, *.test.*, *.e2e.*, test_*
    basename = parts[-1] if parts else ""
    if (
        ".spec." in basename
        or ".test." in basename
        or ".e2e." in basename
        or basename.startswith("test_")
    ):
        return True
    return False


def find_impl_candidates(project_root: Path, domain: str) -> list[str]:
    """Find implementation files matching a domain name."""
    candidates: list[str] = []
    domain_lower = domain.lower().replace("-", "_")

    # Strategy 1: API route files — **/api/{domain}/route.{ts,js}
    # Handles both standard (src/app/api/) and generated (src/generated/*/app/api/)
    domain_kebab = domain_lower.replace("_", "-")
    for domain_variant in {domain_lower, domain_kebab}:
        for ext in ("ts", "tsx", "js"):
            for match in project_root.glob(f"**/api/{domain_variant}/route.{ext}"):
                if match.is_file():
                    rel = str(match.relative_to(project_root))
                    if rel not in candidates:
                        candidates.append(rel)

    # Strategy 3: Generated/service files — src/**/domain*.ts
    for pattern in (
        f"src/**/*{domain_lower}*",
        f"src/**/*{domain_kebab}*",
        f"lib/**/*{domain_lower}*",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    # Strategy 4: Python — {domain}.py, app.py in same directory
    for pattern in (
        f"**/{domain_lower}.py",
        f"**/app.py",
        f"src/**/{domain_lower}.py",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    return candidates


# ---------------------------------------------------------------------------
# v2 search: term normalization + path-segment + content matching
#
# The legacy ``find_impl_candidates`` maps a single *module* name to files via
# globs. That breaks in brownfield codebases whose design docs link only at a
# coarse module granularity (a module that actually owns files scattered under
# unrelated paths). The helpers below let the planner search by the
# phenomenon's own vocabulary — entities, fields, operations, surfaces —
# across BOTH path segments and file content, with cheap term normalization so
# a snake_case token matches its camelCase and kebab-case spellings.
# Everything is generic: only path/content tokens, never a framework or
# project name.
# ---------------------------------------------------------------------------

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _split_words(term: str) -> list[str]:
    """Split a term into its component words (snake/kebab/camel aware)."""
    if not term:
        return []
    # Normalize separators, then break camelCase boundaries.
    spaced = _NON_ALNUM_RE.sub(" ", term)
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", spaced)
    return [w for w in spaced.split() if w]


def _to_snake(term: str) -> str:
    return "_".join(w.lower() for w in _split_words(term))


def _to_kebab(term: str) -> str:
    return "-".join(w.lower() for w in _split_words(term))


def _to_camel(term: str) -> str:
    words = _split_words(term)
    if not words:
        return ""
    head, *rest = words
    return head.lower() + "".join(w.capitalize() for w in rest)


def _singularize_cheap(word: str) -> str:
    """A deliberately small, language-agnostic singularizer.

    Handles the common English plural endings only; never claims linguistic
    correctness. Generality > completeness — it must not encode domain words.
    """
    low = word
    if len(low) > 3 and low.endswith("ies"):
        return low[:-3] + "y"
    if len(low) > 4 and low.endswith("ses"):
        return low[:-2]
    if len(low) > 2 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _pluralize_cheap(word: str) -> str:
    low = word
    if not low:
        return low
    if low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        return low[:-1] + "ies"
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return low + "es"
    return low + "s"


def normalize_terms(terms: Iterable[str]) -> set[str]:
    """Expand each term into snake/camel/kebab + cheap singular/plural forms.

    Returns a lowercase set of tokens length >= 2. Both the whole term and its
    singular/plural variants are expanded, so a two-word camelCase term yields
    its snake_case, kebab-case, and concatenated spellings (and their singular
    forms) — letting a phenomenon term match any casing convention in the code.
    """
    out: set[str] = set()
    for raw in terms:
        if not isinstance(raw, str):
            continue
        term = raw.strip()
        if not term:
            continue

        seeds = {term}
        words = _split_words(term)
        # Singular/plural variants of the last word (e.g. cat <-> cats),
        # rejoined in the original casing forms below.
        if words:
            last = words[-1]
            for variant in (_singularize_cheap(last), _pluralize_cheap(last)):
                if variant and variant != last:
                    seeds.add(" ".join(words[:-1] + [variant]))
        # Whole-term singular/plural (covers single-word terms too).
        seeds.add(_singularize_cheap(term))
        seeds.add(_pluralize_cheap(term))

        for seed in list(seeds):
            for form in (
                seed,
                _to_snake(seed),
                _to_kebab(seed),
                _to_camel(seed),
                seed.replace(" ", "").replace("_", "").replace("-", ""),
            ):
                if form:
                    out.add(form.lower())

    return {tok for tok in out if len(tok) >= 2}


def iter_source_files(
    project_root: Path,
    suffixes: Iterable[str] | None = None,
) -> Iterable[Path]:
    """Yield implementation source files under ``project_root``.

    Skips VCS/build/cache directories and test files. ``suffixes`` defaults to
    :data:`_SOURCE_SUFFIXES`.
    """
    root = Path(project_root)
    allowed = tuple(s.lower() for s in (suffixes or _SOURCE_SUFFIXES))
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed:
            continue
        rel_parts = path.relative_to(root).parts
        # Skip named build/cache/VCS dirs AND any dotdir (e.g. .next, .next-e2e,
        # .nuxt, .svelte-kit, .turbo, .cache): generated build output and tool
        # caches live under dotdirs and are never hand-edited source. A bare
        # ``part in _SKIP_DIR_NAMES`` misses sibling build dirs like ``.next-e2e``.
        if any(
            part in _SKIP_DIR_NAMES or (part.startswith(".") and part not in (".", ".."))
            for part in rel_parts[:-1]
        ):
            continue
        rel = "/".join(rel_parts)
        if is_test_path(rel):
            continue
        yield path


def _safe_read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS]
    return text


def _rel(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix()


@dataclass
class PathSegmentHit:
    """A normalized term matched against a file's path."""

    term: str
    where: str  # "path_segment" | "path_basename" | "path_substring"


@dataclass
class ContentHit:
    """A normalized term matched against a file's content."""

    term: str


def path_segment_hits(rel_path: str, terms: set[str]) -> list[PathSegmentHit]:
    """Match normalized ``terms`` against the segments of ``rel_path``.

    Strongest signal first: a whole path segment equals a term, then the
    basename contains it, then any segment contains it as a substring.
    """
    parts = [p.lower() for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts:
        return []
    basename = parts[-1]
    # Also expose extension-stripped basename tokens for equality checks.
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    segment_set = set(parts) | {stem}

    hits: list[PathSegmentHit] = []
    for term in terms:
        if term in segment_set:
            hits.append(PathSegmentHit(term=term, where="path_segment"))
        elif term in basename:
            hits.append(PathSegmentHit(term=term, where="path_basename"))
        elif any(term in part for part in parts):
            hits.append(PathSegmentHit(term=term, where="path_substring"))
    return hits


def content_hits(text: str, terms: set[str]) -> list[ContentHit]:
    """Match normalized ``terms`` against lowercased file ``text``."""
    if not text:
        return []
    low = text.lower()
    return [ContentHit(term=term) for term in terms if term in low]


def find_impl_candidates_v2(
    project_root: Path,
    terms: set[str],
    *,
    suffixes: Iterable[str] | None = None,
    candidate_paths: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Search every source file for ``terms`` in both path and content.

    Returns ``{rel_path: {"path_hits": [...], "content_hits": [...]}}`` for
    files with at least one hit. The planner — not this function — decides
    which of these become write targets, based on evidence scoring. Keeping
    discovery (broad) separate from acceptance (strict) is what lets recall be
    high without sacrificing precision.

    The candidate universe is general:

    * ``candidate_paths`` (explicit rel-path list) takes precedence when given —
      the caller has already chosen the source universe (e.g. DAG nodes);
    * otherwise :func:`iter_source_files` walks the tree filtered by
      ``suffixes`` (defaulting to :data:`_SOURCE_SUFFIXES`), skipping
      VCS/build/cache dirs and test files.

    Passing a non-default ``suffixes`` (e.g. the set of text-like extensions a
    repo actually contains) makes stylesheet/text-config/copy files discoverable
    WITHOUT this function hardcoding any framework suffix — generality is kept by
    deriving the universe from the repo, not from a baked-in list.
    """
    root = Path(project_root)
    norm = {t for t in terms if t}
    out: dict[str, dict[str, Any]] = {}
    if not norm:
        return out

    if candidate_paths is not None:
        paths_iter: Iterable[Path] = []
        seen: set[str] = set()
        resolved: list[Path] = []
        for rel in candidate_paths:
            rel_posix = Path(rel).as_posix()
            if rel_posix in seen:
                continue
            seen.add(rel_posix)
            full = root / rel
            if full.is_file():
                resolved.append(full)
        paths_iter = resolved
    else:
        paths_iter = iter_source_files(root, suffixes)

    for path in paths_iter:
        rel = _rel(path, root)
        p_hits = path_segment_hits(rel, norm)
        text = _safe_read(path)
        c_hits = content_hits(text, norm)
        if p_hits or c_hits:
            out[rel] = {"path_hits": p_hits, "content_hits": c_hits}
    return out

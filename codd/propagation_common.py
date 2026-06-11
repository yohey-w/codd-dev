"""Shared data-layer primitives for the propagation family (RF7a).

Three modules implement change propagation at different algorithm layers:

* :mod:`codd.propagate` — graph-based impact analysis (``codd impact``)
* :mod:`codd.propagator` — source→design-doc propagation (``codd propagate``)
* :mod:`codd.require_propagate` — requirements-change propagation
  (``codd require --propagate``)

The *algorithms* deliberately stay separate (graph walk vs module mapping vs
dependency lookup), but they consume the same data layer: git-diff change
detection, frontmatter-driven design-doc discovery, modules-field reading,
and design-doc body rewriting. This module is the single owner of those
primitives so frontmatter semantics (:mod:`codd.frontmatter`) and band
semantics (:mod:`codd.confidence`) cannot silently diverge between engines.

Deliberately NOT consolidated (different semantics, kept at call sites):

* ``require_propagate._find_ceg_scan_dir`` vs ``propagator._load_graph`` —
  different fallback chains (require probes legacy locations and demands
  ``edges.jsonl``; propagator only checks the configured path and
  ``nodes.jsonl``). Merging would change which graph each command finds.
* ``propagator``'s DESIGN.md / lexicon diff parsers — they parse YAML *body*
  token tables, not frontmatter; a different domain.
* ``require_propagate._to_affected_doc``'s modules coercion — it reads graph
  *node* dicts (``module`` + ``modules`` merge), not document frontmatter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from codd.frontmatter import parse_frontmatter, read_frontmatter

__all__ = [
    "doc_modules",
    "get_changed_files",
    "iter_design_docs",
    "map_files_to_modules",
    "parse_frontmatter_diff",
    "read_codd_frontmatter",
    "render_updated_doc_content",
]


# ---------------------------------------------------------------------------
# Change detection (git)
# ---------------------------------------------------------------------------
def get_changed_files(
    project_root: Path, diff_target: str, *, warn: bool = False
) -> list[str]:
    """Return changed file paths from ``git diff --name-only <diff_target>``.

    ``warn=True`` preserves :mod:`codd.propagate`'s legacy behavior of
    printing a warning on git failure; :mod:`codd.propagator` stays silent.
    """
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-only", diff_target],
            capture_output=True, text=True, encoding="utf-8", cwd=str(project_root),
        )
    except FileNotFoundError:
        if warn:
            print("Warning: git not found.")
        return []
    if result.returncode != 0:
        if warn:
            print(f"Warning: git diff failed: {result.stderr.strip()}")
        return []
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


# ---------------------------------------------------------------------------
# Frontmatter-driven design-doc discovery
# ---------------------------------------------------------------------------
def read_codd_frontmatter(file_path: Path) -> dict | None:
    """Extract the CoDD metadata block from a document's YAML frontmatter.

    Same semantics as ``codd.scanner._extract_frontmatter``: lenient read via
    :func:`codd.frontmatter.read_frontmatter`, then the nested ``codd:`` key
    (``None`` when the file is unreadable, has no frontmatter, or no ``codd:``).
    """
    frontmatter = read_frontmatter(file_path)
    if frontmatter is None:
        return None
    return frontmatter.get("codd")


def iter_design_docs(
    project_root: Path, config: dict[str, Any]
) -> Iterator[tuple[Path, dict]]:
    """Yield ``(md_path, codd_data)`` for every design doc under the
    configured ``scan.doc_dirs`` that carries CoDD frontmatter with a
    ``node_id``.

    Iteration order: ``doc_dirs`` config order, then ``rglob("*.md")`` order
    within each dir — identical to the loops this replaces in
    :mod:`codd.propagator`.
    """
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])
    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue
        for md_file in full_path.rglob("*.md"):
            codd_data = read_codd_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue
            yield md_file, codd_data


def doc_modules(codd_data: dict) -> Any:
    """Return the ``modules`` field of a doc's CoDD frontmatter (default ``[]``).

    Deliberately NO list coercion: the propagation engines have always read
    the raw value (``codd_data.get("modules", [])``) and treat a non-list as
    "no usable modules" downstream. Coercing a scalar to ``[scalar]`` would
    change which docs match changed modules — a behavior change.
    """
    return codd_data.get("modules", [])


# ---------------------------------------------------------------------------
# File → module mapping
# ---------------------------------------------------------------------------
def map_files_to_modules(
    changed_files: list[str],
    source_dirs: list[str],
) -> dict[str, str]:
    """Map changed source files to module names.

    Module = first directory under a source_dir.
    e.g. src/auth/service.py with source_dirs=["src"] → module "auth"
    """
    file_module: dict[str, str] = {}
    normalized_dirs = [d.rstrip("/") for d in source_dirs]

    for f in changed_files:
        parts = PurePosixPath(f).parts
        for src_dir in normalized_dirs:
            src_parts = PurePosixPath(src_dir).parts
            if parts[: len(src_parts)] == src_parts and len(parts) > len(src_parts) + 1:
                # First dir after source_dir is the module
                module_name = parts[len(src_parts)]
                file_module[f] = module_name
                break

    return file_module


# ---------------------------------------------------------------------------
# Design-doc body rewriting (frontmatter + title preservation)
# ---------------------------------------------------------------------------
def render_updated_doc_content(original_content: str, new_body: str) -> str:
    """Render a design doc's full content from its original text and a new body.

    Preserves the original frontmatter block and the original ``# Title`` line
    (AI updates must not rename documents). Shared by
    ``propagator._write_updated_doc`` (which writes it) and
    ``require_propagate._display_proposals`` (which diffs it) so the preview
    can never drift from what ``--apply`` actually writes.

    NOTE: this keeps the legacy regex-based frontmatter split (not
    :func:`codd.frontmatter.split_frontmatter`) on purpose — the regex demands
    a trailing newline after the closing ``---`` and tolerates content the
    strict parser rejects; switching would change which bytes are preserved.
    """
    import re

    match = re.match(r"^(---\s*\n.*?\n---\s*\n)", original_content, re.DOTALL)
    frontmatter = match.group(1) if match else ""

    body_lines = new_body.strip().split("\n")
    if body_lines and body_lines[0].startswith("# "):
        title_match = re.search(r"^# .+$", original_content, re.MULTILINE)
        if title_match:
            body_lines[0] = title_match.group(0)

    return frontmatter + "\n".join(body_lines) + "\n"


# ---------------------------------------------------------------------------
# Frontmatter diff parsing (structured replacement for the hand parser)
# ---------------------------------------------------------------------------
def parse_frontmatter_diff(
    diff_text: str,
    *,
    path_filter: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Parse frontmatter field changes out of a unified git diff.

    Structured replacement for ``require_propagate``'s legacy line-regex
    parser, built on :mod:`codd.frontmatter` semantics: for each changed file
    the pre-/post-change document texts are reconstructed from the hunk lines
    (context+removed vs context+added), both are parsed with
    :func:`codd.frontmatter.parse_frontmatter`, and the two mappings are
    diffed on their leaf fields.

    Returns ``[{"file", "field", "old", "new"}, ...]`` where ``field`` is the
    leaf key (nested ``codd:`` children report their leaf name, matching the
    legacy parser), ``old``/``new`` are string-rendered values, and ``None``
    means the field is absent on that side.

    Semantics inherited from :mod:`codd.frontmatter` (deliberate upgrades over
    the legacy regex parser):

    * pure quoting/formatting changes (``"draft"`` → ``draft``) are NOT
      reported — the YAML value is unchanged;
    * lines outside a complete frontmatter block (e.g. ``key: value`` text in
      the document body, or after a ``---`` horizontal rule) are ignored;
    * block-mapping parent keys (``codd:``) are containers, not fields.
    """
    changes: list[dict] = []
    for file_path, old_lines, new_lines in _iter_diff_file_sections(diff_text):
        if path_filter is not None and not path_filter(file_path):
            continue
        old_flat, old_order = _flatten_leaf_fields(_frontmatter_mapping(old_lines))
        new_flat, new_order = _flatten_leaf_fields(_frontmatter_mapping(new_lines))

        # Changed/removed fields in old-document order, then added-only fields
        # in new-document order (the legacy parser's diff-position order
        # coincides with this for contiguous frontmatter edits).
        field_order = old_order + [f for f in new_order if f not in old_flat]
        for field in field_order:
            old = _field_value(old_flat[field]) if field in old_flat else None
            new = _field_value(new_flat[field]) if field in new_flat else None
            if old == new:
                continue
            changes.append({"file": file_path, "field": field, "old": old, "new": new})
    return changes


def _iter_diff_file_sections(diff_text: str) -> list[tuple[str, list[str], list[str]]]:
    """Split a unified diff into ``(path, old_lines, new_lines)`` per file.

    ``old_lines`` = context + removed line contents; ``new_lines`` = context +
    added line contents (diff prefixes stripped). Header/metadata lines
    (``diff --git``, ``---``/``+++`` file headers, ``index``, ``@@`` hunk
    headers, mode/rename lines, ``\\ No newline...``) are excluded.
    """
    sections: list[tuple[str, list[str], list[str]]] = []
    current_path: str | None = None
    old_lines: list[str] = []
    new_lines: list[str] = []

    def flush() -> None:
        nonlocal old_lines, new_lines
        if current_path is not None:
            sections.append((current_path, old_lines, new_lines))
        old_lines = []
        new_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_path = _path_from_diff_header(line)
            continue
        if line.startswith("+++ "):
            target = line[len("+++ "):].strip()
            if target.startswith("b/"):
                current_path = target[2:]
            continue
        if line.startswith("--- ") or line.startswith("index ") or line.startswith("@@"):
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if not line:
            # Empty line: some transports strip the single-space prefix from
            # blank context lines. The legacy parser skipped these too.
            continue
        prefix, content = line[0], line[1:]
        if prefix == " ":
            old_lines.append(content)
            new_lines.append(content)
        elif prefix == "-":
            old_lines.append(content)
        elif prefix == "+":
            new_lines.append(content)
        # Anything else (mode/rename/Binary metadata) is not document content.

    flush()
    return sections


def _path_from_diff_header(line: str) -> str | None:
    parts = line.split()
    if len(parts) >= 4 and parts[3].startswith("b/"):
        return parts[3][2:]
    return None


def _frontmatter_mapping(lines: list[str]) -> dict[str, Any]:
    if not lines:
        return {}
    return parse_frontmatter("\n".join(lines) + "\n").mapping


def _flatten_leaf_fields(mapping: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Flatten nested mappings to leaf fields keyed by their leaf name.

    Leaf-name keying (not dotted paths) matches the legacy parser, which
    stripped indentation and reported ``codd: → node_id:`` as ``node_id``.
    On a leaf-name collision the later occurrence wins (also legacy).
    """
    flat: dict[str, Any] = {}
    order: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value)
                continue
            leaf = str(key)
            if leaf not in flat:
                order.append(leaf)
            flat[leaf] = value

    walk(mapping)
    return flat, order


def _field_value(value: Any) -> str:
    """Render a YAML leaf value the way the legacy line parser captured it.

    ``None`` (an explicit ``key:`` with no value) was captured as ``""``;
    booleans render in YAML form; everything else uses ``str()`` (dates and
    ints round-trip to their source text).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

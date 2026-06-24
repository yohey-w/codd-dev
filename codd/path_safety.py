"""Shared path-escape jail for user-path-controllable filesystem reads.

CoDD reads many filesystem paths that originate from *external, user-controllable*
sources: ``codd.yaml`` config values, design-doc frontmatter, DAG node paths/ids,
``ExpectedNode.path_hint`` hints, ``lexicon_file`` / ``deployment.documents`` /
propagation-output declarations, and CLI path arguments. When such a path is handed
to a filesystem *read* (``read_text``/``open``/``yaml``/``json``/``is_file``/
``exists``/``stat``/``glob``/``rglob``) with no root check, an absolute path or a
``../`` traversal — or an in-root symlink whose target escapes the tree — leaks a
file from outside the project (a path-escape false-green: an out-of-root file
"satisfies" an expected artifact, or its contents are consumed as evidence).

This module is the single shared closure for that class. It is *pure path logic*:
no language- or framework-specific knowledge, no new jail semantics beyond the
resolve-and-confine rule that the per-site jails already implemented independently.
Every confinement check resolves the candidate (following symlinks) and accepts it
only when the resolved path stays inside ``project_root.resolve()``.

Leading-slash semantics (matters for anti-false-red / anti-false-green):
``resolve_project_path`` treats an *absolute* raw path (``/abs/...`` or an absolute
``Path``) as a filesystem-absolute location — it is accepted only when it genuinely
lives under the project root, never collapsed onto a same-named in-root relative
file. A relative raw path (``src/x``, ``../escape``) is resolved under the root.
Callers that must preserve the absolute-vs-root-relative distinction therefore pass
the *raw* path (not a leading-slash-stripped form) so this function can tell them
apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

__all__ = [
    "resolve_project_path",
    "iter_project_glob",
    "project_relative_path",
]


def resolve_project_path(project_root: Union[str, Path], raw_path: Union[str, Path]) -> Path | None:
    """Resolve ``raw_path`` and return it only when it stays inside ``project_root``.

    ``raw_path`` is resolved (symlinks followed). An absolute ``raw_path`` is taken
    as-is; a relative one is joined under ``project_root``. The resolved path is
    returned when it is inside ``project_root.resolve()``, otherwise ``None``.

    Returns ``None`` for an empty/whitespace ``raw_path`` and for any path that
    escapes the root — including an in-root symlink whose target resolves outside —
    so the result is always safe to read.
    """
    if raw_path is None:
        return None
    raw_text = str(raw_path)
    if not raw_text.strip():
        return None
    try:
        root = Path(project_root).resolve()
        candidate = Path(raw_path) if Path(raw_path).is_absolute() else Path(project_root) / raw_path
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return resolved


def iter_project_glob(project_root: Union[str, Path], raw_glob: str) -> list[Path]:
    """Glob ``raw_glob`` under ``project_root`` and return only in-root matches.

    The pattern is anchored to the project root: ``Path.glob`` rejects absolute
    patterns, so a leading slash is stripped and the pattern is treated as
    root-relative. Each match is resolved (symlinks followed) and kept only when it
    stays inside ``project_root.resolve()`` — so an in-root symlink whose target
    escapes the tree cannot smuggle an off-root file into the results. Resolved,
    de-duplicated matches are returned sorted for determinism.
    """
    pattern = str(raw_glob or "").strip().lstrip("/")
    if not pattern:
        return []
    try:
        root = Path(project_root).resolve()
    except (ValueError, OSError):
        return []
    matches: dict[str, Path] = {}
    try:
        globbed = list(Path(project_root).glob(pattern))
    except (ValueError, OSError):
        return []
    for match in globbed:
        try:
            resolved = match.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            continue
        matches[str(resolved)] = resolved
    return [matches[key] for key in sorted(matches)]


def project_relative_path(project_root: Union[str, Path], path: Union[str, Path]) -> str | None:
    """Return the resolved project-relative POSIX path, or ``None`` if outside root.

    Resolves ``path`` (symlinks followed) and returns its path relative to
    ``project_root.resolve()`` as a POSIX string. Returns ``None`` when the resolved
    path escapes the root.
    """
    resolved = resolve_project_path(project_root, path)
    if resolved is None:
        return None
    try:
        root = Path(project_root).resolve()
        return resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None

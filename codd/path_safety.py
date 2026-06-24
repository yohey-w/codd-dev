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

import re
from pathlib import Path
from typing import Union

__all__ = [
    "PathEscapeError",
    "resolve_project_path",
    "require_project_path",
    "iter_project_glob",
    "project_relative_path",
]

# A path component is "magic" (a glob wildcard) when it contains one of these.
# ``Path.glob`` itself uses the same notion to decide where literal matching
# stops and wildcard expansion begins.
_GLOB_MAGIC = re.compile(r"[*?\[]")


def _glob_static_base(pattern: Path) -> tuple[Path, list[str]]:
    """Split a glob ``pattern`` into its literal prefix dir and the wildcard tail.

    Walks the path components left-to-right; everything up to (but not including)
    the first component containing a glob wildcard is the *static base* (a real
    directory we can ``resolve()``), and the remainder is the relative glob tail.
    For a pattern with no wildcard at all the whole path is the static base and
    the tail is empty.
    """
    static: list[str] = []
    rest: list[str] = []
    hit = False
    for part in pattern.parts:
        if not hit and _GLOB_MAGIC.search(part):
            hit = True
        (rest if hit else static).append(part)
    base = Path(*static) if static else Path(pattern.anchor or ".")
    return base, rest


class PathEscapeError(Exception):
    """A configured/declared evidence path resolved OUTSIDE the project root.

    Raised by :func:`require_project_path` (the fail-closed twin of
    :func:`resolve_project_path`). It is the explicit "scan/check not valid"
    signal for the case where an OPERATOR pointed CoDD at an evidence ROOT
    (``scan.doc_dirs`` / ``scan.source_dirs`` / a layout-profile root) that
    escapes the project tree — a silent skip there is a false-green in another
    form (the gate "passes" because it could not see the smuggled tree). It does
    NOT subclass ``ValueError``/``OSError`` on purpose: those are caught broadly
    across CoDD (including inside this module), and an escape must propagate as a
    distinct, honest failure rather than be swallowed back into a silent skip.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(message)


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


def require_project_path(
    project_root: Union[str, Path],
    raw_path: Union[str, Path],
    *,
    context: str = "path",
) -> Path:
    """Resolve ``raw_path`` inside ``project_root`` or raise :class:`PathEscapeError`.

    Fail-closed counterpart to :func:`resolve_project_path`: identical resolve +
    symlink-follow + confine logic, but an out-of-root (or empty) ``raw_path``
    raises instead of returning ``None``. Use this where a configured evidence
    ROOT escaping the project must make the operation NOT-VALID (an honest
    failure), rather than being silently dropped. ``context`` names the offending
    site in the message (e.g. ``"scan.doc_dirs"``).
    """
    resolved = resolve_project_path(project_root, raw_path)
    if resolved is None:
        raw_text = "" if raw_path is None else str(raw_path)
        raise PathEscapeError(
            f"{context} resolves outside the project root and is not valid "
            f"evidence: {raw_text!r}",
            path=raw_text,
        )
    return resolved


def iter_project_glob(project_root: Union[str, Path], raw_glob: str) -> list[Path]:
    """Glob ``raw_glob`` under ``project_root`` and return only in-root matches.

    The pattern is anchored to the project root, and the leading-slash semantics
    mirror :func:`resolve_project_path` (matters for anti-false-RED):

    * A *relative* pattern (``src/**/*.py``) is globbed root-relative — unchanged.
    * An *absolute* pattern (``/abs/proj/src/**/*.py``, e.g. a config that spells
      ``dag.impl_file_patterns`` / ``scan.source_dirs`` with absolute in-root
      paths) is taken as a real filesystem location: its static (non-wildcard)
      base is resolved and, when it lives under the project root, the pattern is
      rebased to project-relative so the in-root files genuinely match. Blindly
      stripping the leading slash (the old behaviour) rebased it onto the wrong
      place and matched nothing — a false-RED, dropping legitimate in-root files
      from the DAG. An absolute base that escapes the root is *not* globbed there
      (escape prevention); it falls back to a root-relative read of the
      slash-stripped pattern, which yields only in-root matches (legacy compat),
      never the outside tree.

    Whatever the pattern shape, every match is then resolved (symlinks followed)
    and kept only when it stays inside ``project_root.resolve()`` — this final
    confinement is the real security boundary, so an in-root symlink whose target
    escapes the tree (or an absolute base reached through such a symlink) can
    never smuggle an off-root file into the results. Resolved, de-duplicated
    matches are returned sorted for determinism.
    """
    raw_text = str(raw_glob or "").strip()
    if not raw_text:
        return []
    try:
        root = Path(project_root).resolve()
    except (ValueError, OSError):
        return []

    pattern = _project_relative_glob(root, raw_text)
    if not pattern:
        return []

    matches: dict[str, Path] = {}
    try:
        globbed = list(Path(project_root).glob(pattern))
    except (ValueError, OSError, NotImplementedError):
        return []
    for match in globbed:
        try:
            resolved = match.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            continue
        matches[str(resolved)] = resolved
    return [matches[key] for key in sorted(matches)]


def _project_relative_glob(root: Path, raw_text: str) -> str:
    """Rebase ``raw_text`` to a root-relative glob, preserving absolute semantics.

    ``root`` must already be ``resolve()``-d. Returns a root-relative glob string
    (possibly empty when nothing can be globbed in-root). See
    :func:`iter_project_glob` for the leading-slash contract this implements.
    """
    pattern_path = Path(raw_text)
    if not pattern_path.is_absolute():
        # Relative pattern: globbed root-relative, exactly as before.
        return raw_text.lstrip("/")

    base, rest = _glob_static_base(pattern_path)
    try:
        base_resolved = base.resolve()
        base_rel = base_resolved.relative_to(root)
    except (ValueError, OSError):
        # Absolute base escapes the root (or cannot be resolved): do NOT glob the
        # outside location. Fall back to a root-relative read of the
        # slash-stripped pattern — legacy compat for absolute-looking patterns
        # that were only ever meant as root-relative; the per-match confinement
        # still guarantees only in-root files come back.
        return raw_text.lstrip("/")

    rebased = base_rel.joinpath(*rest) if rest else base_rel
    rebased_text = rebased.as_posix()
    # ``relative_to`` of the root itself yields "."; an empty/dot base with no
    # wildcard tail has nothing meaningful to glob.
    if rebased_text in ("", "."):
        return ""
    return rebased_text


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

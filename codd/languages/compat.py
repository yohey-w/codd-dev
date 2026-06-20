"""Compatibility shim: ``LanguageProfile`` → legacy ``LayoutProfile``.

This is the **Phase 1 (compat shim part)** of GPT-5.5 Pro's language-generality
redesign (``dogfood/gpt_language_generality_design.md`` §4 "Phase 1" and §8.3
"source_root を compatibility view に降格する").

Goal
----
Let the *new* declarative :class:`~codd.languages.profile.LanguageProfile` derive
the *old* :class:`~codd.project_types.LayoutProfile`'s topology triple
(``source_root`` / ``package_root`` / ``test_root``) so the existing gates keep
running against a frozen snapshot while the rewire happens incrementally.

The load-bearing guarantee (proven by ``tests/languages/test_compat_and_pathplanner.py``):

    For **python** and **typescript** profiles, the layout triple this shim
    derives is **byte-identical** to what
    :func:`codd.project_types._python_layout_profile` /
    :func:`codd.project_types._typescript_layout_profile` produce for the same
    ``package_name`` / source-root / test-root inputs.

Scope of the shim (deliberately narrow)
---------------------------------------
The legacy ``LayoutProfile`` mixes *topology* (source/package/test roots — the
thing this redesign is generalizing) with *policy* (runner, install_mode,
test_import_policy, the implement-oracle/verify specs). Only the **topology** is
expressible from the declarative profile today, so this shim derives ONLY the
topology triple (+ ``language`` + ``package_name``). Policy fields are left at
their dataclass defaults here; the real builders remain the authority for those
until later phases (§4 Phase 4–6) move the oracle/verify/test-semantics behind
adapters. This keeps the shim drift-free: it never *re-encodes* the builders'
policy decisions, so it cannot silently disagree with them.

A ``LanguageProfile`` whose ``package_root.kind == "none"`` (Go) has **no single
``source_root``/``package_root``** — that is the whole point of the redesign
(§1.2). Asking this shim for a legacy triple for such a language raises
:class:`UnsupportedLayoutShape`; callers must use
:class:`codd.languages.path_planner.PathPlanner` instead (§8.3).

This module is **purely additive** — nothing imports it from the live pipeline
yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .profile import LanguageProfile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from codd.project_types import LayoutProfile


class UnsupportedLayoutShape(ValueError):
    """A language has no single source/package root (use ``PathPlanner``).

    Raised by :func:`layout_profile_from_language_profile` for a profile whose
    ``layout.package_root.kind == "none"`` (e.g. Go), per design §8.3: the legacy
    single-``source_root`` API must hard-fail for a multi/co-located-source-set
    language instead of silently inventing a ``src/`` root.
    """


def _subst(template: str, *, package_name: str) -> str:
    """Substitute the only Phase-1 layout placeholder, ``{package_name}``.

    Other placeholders (``{module_path}``, ``{go_version}``, …) are not part of a
    legacy topology triple and are intentionally not handled here.
    """
    return template.replace("{package_name}", package_name)


def _norm(rel: str) -> str:
    """Normalize a relative path the way the legacy builders do (``_norm_rel``)."""
    return str(rel).strip().replace("\\", "/").strip("/")


def _parent_dir(path: str) -> str:
    """Parent directory of a POSIX-style relative path (``src/pkg`` → ``src``).

    Returns ``"."`` when there is no parent segment, matching how a single-segment
    source root would degrade (not expected for the named-package layout, which
    always nests ``<src>/<pkg>``).
    """
    norm = _norm(path)
    if "/" not in norm:
        return "."
    return norm.rsplit("/", 1)[0]


def _derive_source_and_package_root(
    profile: LanguageProfile, *, package_name: str
) -> tuple[str, str]:
    """Derive the legacy ``(source_root, package_root)`` pair from the profile.

    Mirrors the two existing builders exactly:

    * ``named_package`` (Python): ``package_root`` is the profile's
      ``package_root.path`` (``src/<pkg>``); the legacy ``source_root`` is its
      PARENT (``src``) — i.e. ``_python_layout_profile`` does
      ``source_root=<src>``, ``package_root=f"{source_root}/{package_name}"``.
    * ``path_root`` (TypeScript): ``package_root == source_root`` and both equal
      the (single) source-set root (``src``) — i.e.
      ``_typescript_layout_profile`` does ``package_root=source_root``.

    Anything else (``none`` / unknown) has no single root → caller raises.
    """
    pkg_spec = profile.layout.package_root
    kind = pkg_spec.kind

    if kind == "named_package":
        if not pkg_spec.path:
            raise UnsupportedLayoutShape(
                f"language {profile.id!r}: package_root.kind=named_package but no path"
            )
        package_root = _norm(_subst(pkg_spec.path, package_name=package_name))
        source_root = _parent_dir(package_root)
        return source_root, package_root

    if kind == "path_root":
        if not pkg_spec.path:
            raise UnsupportedLayoutShape(
                f"language {profile.id!r}: package_root.kind=path_root but no path"
            )
        root = _norm(_subst(pkg_spec.path, package_name=package_name))
        return root, root

    raise UnsupportedLayoutShape(
        f"language {profile.id!r}: package_root.kind={kind!r} has no single "
        "source/package root; use PathPlanner (design §8.3)."
    )


def _derive_test_root(profile: LanguageProfile, *, package_name: str) -> str:
    """Derive the legacy ``test_root`` from the profile's first test set.

    Both legacy builders take ``test_root`` from the first ``scan.test_dirs``
    entry (default ``tests``). The declarative profile's first ``test_sets`` entry
    carries the same value (``root: tests``).
    """
    test_sets = profile.layout.test_sets
    if not test_sets:
        return "tests"
    return _norm(_subst(test_sets[0].root, package_name=package_name))


def layout_profile_from_language_profile(
    profile: LanguageProfile,
    *,
    package_name: str,
) -> "LayoutProfile":
    """Derive a legacy :class:`LayoutProfile` from a declarative ``LanguageProfile``.

    Only the **topology triple** (``source_root`` / ``package_root`` /
    ``test_root``) plus ``language`` and ``package_name`` are derived — these are
    proven byte-identical to the real builders for python/typescript (see this
    module's docstring and the golden tests). Policy fields (runner,
    install_mode, oracle/verify specs, …) are left at their ``LayoutProfile``
    defaults: this shim is the topology authority only, and never re-encodes the
    builders' policy so it cannot drift from them.

    :param profile: the loaded declarative language profile.
    :param package_name: the harness-owned canonical package name to substitute
        for ``{package_name}`` (the caller resolves it exactly as the legacy path
        does, via ``resolve_canonical_package_name``).
    :raises UnsupportedLayoutShape: if the language has no single source/package
        root (``package_root.kind == "none"``, e.g. Go) — use ``PathPlanner``.
    """
    # Imported lazily so this additive module never creates an import cycle with
    # the (large) project_types module at package import time.
    from codd.project_types import LayoutProfile

    source_root, package_root = _derive_source_and_package_root(
        profile, package_name=package_name
    )
    test_root = _derive_test_root(profile, package_name=package_name)

    return LayoutProfile(
        language=profile.id,
        package_name=package_name,
        source_root=source_root,
        package_root=package_root,
        test_root=test_root,
    )

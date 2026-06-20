"""Resolve a codd.yaml ``stack:`` declaration into a ResolvedStackContract.

This is the public entry the harness/pipeline uses: a project declares its stack
(design §1)::

    stack:
      language: typescript
      frameworks: [nextjs]
      addons: [prisma, playwright]

and :func:`resolve_stack_from_declaration` turns that into the single
:class:`~codd.stack.compose.ResolvedStackContract` the gates consume — resolving
each id through the language/framework/addon registries (id or alias) and
composing them. ``UnknownLanguageError`` / ``UnknownLayerError`` surface a bad
declaration loudly rather than silently dropping a layer.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from codd.languages.profile import LanguageProfile
from codd.languages.registry import default_registry as _default_language_registry

from .compose import ResolvedStackContract, compose
from .profile import AddonProfile, FrameworkProfile
from .registry import default_addon_registry, default_framework_registry


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def resolve_stack(
    language: str | LanguageProfile,
    frameworks: Sequence[str | FrameworkProfile] = (),
    addons: Sequence[str | AddonProfile] = (),
) -> ResolvedStackContract:
    """Resolve a stack from ids (or already-resolved profiles) into a contract.

    Strings are resolved through the default registries (by id or alias); profile
    objects are used as-is (handy for tests / user-supplied profiles).
    """
    lang = (
        language
        if isinstance(language, LanguageProfile)
        else _default_language_registry.resolve(language)
    )
    fws = [
        f if isinstance(f, FrameworkProfile) else default_framework_registry.resolve(f)
        for f in frameworks
    ]
    ads = [
        a if isinstance(a, AddonProfile) else default_addon_registry.resolve(a)
        for a in addons
    ]
    return compose(lang, fws, ads)


def resolve_stack_from_declaration(declaration: Mapping[str, Any]) -> ResolvedStackContract:
    """Resolve a codd.yaml ``stack:`` block mapping into a contract.

    Expects ``{language: <id>, frameworks: [...], addons: [...]}``. ``language``
    is required; ``frameworks`` / ``addons`` default to empty.
    """
    if not isinstance(declaration, Mapping) or "language" not in declaration:
        raise ValueError(
            "stack declaration must be a mapping with a 'language' key "
            "(e.g. {language: typescript, frameworks: [nextjs], addons: [prisma]})"
        )
    return resolve_stack(
        declaration["language"],
        _as_list(declaration.get("frameworks")),
        _as_list(declaration.get("addons")),
    )

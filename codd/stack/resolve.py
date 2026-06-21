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

    Two OPTIONAL, orthogonal tuning blocks are applied AFTER composition (Contract
    Kernel v3.x — both fail-closed, neither weakens the anti-false-green guarantee):

    * ``command_observation_policies`` — per-slot authenticity-policy STRENGTHENING
      (e.g. mark the TypeScript ``verify`` (vitest) slot as ``TEST_REPORT`` so an
      overridden vitest command is judged as a test, not "some static command exited
      0"). Strengthen-only, CORE-owned: a weakening declaration is rejected by
      :func:`codd.stack.command_authenticity.resolve_stack_command_observation_policy`.
      This shapes the slot's GREEN CRITERIA.
    * ``command_overrides`` — per-slot TRANSPORT-ONLY override (run a project's bespoke
      CI script for an already-composed verification slot). Applied LAST so it sees the
      composed slots; it changes only argv/cwd/env/report transport and is folded into
      the contract ``content_hash`` so it drifts the lock
      (:func:`codd.stack.command_override.apply_project_command_overrides`). This shapes
      the slot's COMMAND TRANSPORT.

    The two are deliberately SEPARATE keys: a project cannot smuggle a weaker
    authenticity policy in through a transport override (the override forbids
    ``kind``/``observation``/``policy``), and the policy strengthening cannot change
    what command runs. A declaration with neither block resolves byte-identically to
    today (existing locks/tests unaffected).
    """
    if not isinstance(declaration, Mapping) or "language" not in declaration:
        raise ValueError(
            "stack declaration must be a mapping with a 'language' key "
            "(e.g. {language: typescript, frameworks: [nextjs], addons: [prisma]})"
        )
    contract = resolve_stack(
        declaration["language"],
        _as_list(declaration.get("frameworks")),
        _as_list(declaration.get("addons")),
    )
    contract = _apply_observation_policy_declaration(
        contract, declaration.get("command_observation_policies")
    )
    # Lazy import: command_override imports compose types; keep resolve import-light and
    # avoid any load-order surprise (mirrors the lazy imports elsewhere in the package).
    from .command_override import apply_project_command_overrides

    return apply_project_command_overrides(contract, declaration.get("command_overrides"))


def _apply_observation_policy_declaration(
    contract: ResolvedStackContract, raw: Any
) -> ResolvedStackContract:
    """Parse a ``stack.command_observation_policies`` block into the contract (or no-op).

    Each entry ``<slot_id>: {kind: test_report|build_execution|static_execution,
    min_collected_tests?: int}`` is parsed into a
    :class:`~codd.stack.command_authenticity.StackCommandObservationPolicy`. The parse is
    intentionally thin: the strengthen-only / intrinsic-floor enforcement is OWNED by
    :func:`codd.stack.command_authenticity.resolve_stack_command_observation_policy` at
    use time (a weakening entry raises there), and the policy dataclass's own
    ``__post_init__`` rejects an intrinsically-unsound policy (a "test" policy that
    accepts zero tests / no report cannot even be constructed). ``None`` / empty → the
    contract is returned UNCHANGED (byte-identical no-policy path).
    """
    if not raw:
        return contract
    if not isinstance(raw, Mapping):
        raise ValueError(
            "stack.command_observation_policies must be a mapping of slot_id -> "
            f"{{kind: ..., min_collected_tests?: ...}} (got {type(raw).__name__})"
        )

    import dataclasses
    from types import MappingProxyType

    from codd.stack.command_authenticity import (
        BUILD_EXECUTION_POLICY,
        STATIC_EXECUTION_POLICY,
        TEST_REPORT_POLICY,
        StackCommandObservationKind,
    )

    _BASE_BY_KIND = {
        StackCommandObservationKind.TEST_REPORT: TEST_REPORT_POLICY,
        StackCommandObservationKind.BUILD_EXECUTION: BUILD_EXECUTION_POLICY,
        StackCommandObservationKind.STATIC_EXECUTION: STATIC_EXECUTION_POLICY,
    }

    policies: dict[str, Any] = dict(contract.command_observation_policies)
    for slot_id, entry in raw.items():
        slot_id = str(slot_id)
        if not isinstance(entry, Mapping) or "kind" not in entry:
            raise ValueError(
                f"stack.command_observation_policies[{slot_id!r}] must be a mapping with a "
                "'kind' (test_report | build_execution | static_execution)"
            )
        try:
            kind = StackCommandObservationKind(str(entry["kind"]).strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"stack.command_observation_policies[{slot_id!r}].kind {entry['kind']!r} is "
                "not a known observation kind (test_report | build_execution | "
                "static_execution)"
            ) from exc
        base = _BASE_BY_KIND[kind]
        kwargs: dict[str, Any] = {}
        if "min_collected_tests" in entry:
            kwargs["min_collected_tests"] = int(entry["min_collected_tests"])
        # __post_init__ rejects an intrinsically-unsound policy (e.g. a TEST policy below
        # its floor).
        policies[slot_id] = dataclasses.replace(base, **kwargs) if kwargs else base

    new_policies = MappingProxyType(policies)

    # FAIL-FAST at RESOLVE (mirror the language ``observation`` block's load-time rejection):
    # a WEAKENING declaration (e.g. downgrading the known ``e2e_test`` TEST slot to STATIC)
    # is rejected HERE, not silently stored to red later at the gate. The resolver
    # (:func:`resolve_stack_command_observation_policy`) owns strengthen-only; calling it for
    # every declared slot surfaces a weakening as a ``StackObservationPolicyWeakeningError``
    # at resolve time (the same fail-fast UX as a bad ``command_overrides`` block).
    from codd.stack.command_authenticity import resolve_stack_command_observation_policy

    for slot_id in policies:
        resolve_stack_command_observation_policy(slot_id, contract_policies=new_policies)

    return dataclasses.replace(contract, command_observation_policies=new_policies)

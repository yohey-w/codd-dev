"""Undeclared-interactive-surface reconciliation for ``codd doctor``.

The operation-driven coverage axes (action outcome, capability completeness,
operational E2E audit) all iterate the *declared* ``operation_flow`` universe.
An actor-facing capability that is **implemented in source** but **never lifted
into ``operation_flow``** is therefore structurally invisible: every per-operation
check passes because the operation does not exist, while the actor really can
mutate/author state through the UI.

The existing source scanners answer a *different* question. ``_interactive_control
_warnings`` asks "is this wired control connected to a handler/form?"; this check
asks the orthogonal question "is this wired control *declared* in
``operation_flow``?". The cross-check is the step the wiring scanner is one short
of: a fully wired authoring control still warns here when no declared operation
names its capability.

The check is framework-agnostic. It recognises only

* literal ``<button>`` controls whose label resolves to a canonical mutating
  verb (the same taxonomy ``operation_flow`` action coverage uses), and
* generic content-authoring surfaces via the HTML/ARIA standards
  ``contenteditable``, ``role="textbox"`` and ``aria-multiline``.

It contains **no** UI-library or project-specific vocabulary, widens **no**
generation universe (advisory only, like ``capability_completeness``), and is
opt-out per project via ``codd.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from codd.action_outcome import _normalize_token, canonical_action_verb
from codd.requirements_meta import operation_flow_operations


# codd.yaml mapping that tunes this check.
SETTINGS_KEY = "surface_reconciliation"

# Source suffixes that can hold actor-facing markup. Mirrors the suffix gate the
# existing interactive-control / global-action scanners apply.
_MARKUP_SUFFIXES = frozenset({".tsx", ".jsx", ".html", ".vue", ".svelte"})

# Literal HTML <button> control. Identical shape to cli._BUTTON_RE; kept local so
# this module is import-light and independently testable.
_BUTTON_RE = re.compile(r"(?is)<button\b(?P<attrs>[^>]*)>(?P<label>.*?)</button>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")

# Generic, library-neutral content-authoring surfaces. ``contenteditable`` is the
# HTML standard rich-text host; ``role="textbox"`` + ``aria-multiline`` are the
# ARIA standards a custom rich-text widget must expose to be accessible. No
# framework or library name appears here.
_EDITOR_SURFACE_RE = re.compile(
    r"(?is)("
    r"contenteditable\s*=\s*['\"{]?\s*(?:true|\{?\s*true\s*\}?)|"
    r"contenteditable(?=[\s/>])|"
    r"role\s*=\s*['\"{]?\s*['\"]?textbox['\"]?|"
    r"aria-multiline\s*=\s*['\"{]?\s*(?:true|\{?\s*true\s*\}?)"
    r")"
)

# The capability an editor surface proves the actor can exercise. ``edit`` is the
# canonical authoring verb in :func:`canonical_action_verb`.
_EDITOR_CAPABILITY_VERB = "edit"


@dataclass(frozen=True)
class UndeclaredSurface:
    """An implemented interactive surface with no matching declared operation."""

    kind: str  # "control" | "editor"
    capability: str  # canonical capability token (verb)
    label: str
    source: str  # display path of the source file

    @property
    def message(self) -> str:
        if self.kind == "editor":
            detail = (
                f"Content-authoring surface in `{self.source}` exposes an editable/"
                f"authoring capability"
            )
        else:
            detail = (
                f"Interactive control `{self.label}` in `{self.source}` performs a "
                f"`{self.capability}` capability"
            )
        return (
            f"[undeclared_surface] {detail}, but no `operation_flow` operation declares "
            f"this capability (verb/target/id matching `{self.capability}`) and no "
            f"`runtime.action_outcome_targets` action covers it. Declare the operation in "
            f"`operation_flow` so per-operation coverage applies, or tune `{SETTINGS_KEY}` "
            f"in codd.yaml if this is a false positive."
        )


def _control_label(raw_html: str) -> str:
    return " ".join(_TAG_RE.sub(" ", raw_html).split())


def declared_capability_tokens(flows: Iterable[tuple[str, Any]]) -> frozenset[str]:
    """Capability tokens (verbs + canonical verbs + targets + ids) from flows.

    A surface is considered *declared* when its capability token appears anywhere
    in this set. We are deliberately generous on the declared side: any operation
    whose verb, canonical verb, target, or id matches the surface capability
    satisfies the reconciliation. Being generous here keeps the check advisory and
    low-noise -- it only fires when a capability is *entirely absent* from the
    declared universe.
    """

    tokens: set[str] = set()
    for _source, flow in flows:
        for operation in operation_flow_operations(flow):
            for key in ("verb", "target", "id"):
                raw = operation.get(key)
                token = _normalize_token(raw)
                if token:
                    tokens.add(token)
                    tokens.update(part for part in token.split("_") if part)
                canonical = canonical_action_verb(raw)
                if canonical:
                    tokens.add(canonical)
    return frozenset(tokens)


def _runtime_action_tokens(target_actions: Iterable[Any]) -> frozenset[str]:
    """Capability tokens already declared via ``runtime.action_outcome_targets``."""

    tokens: set[str] = set()
    for action in target_actions:
        for value in (
            getattr(action, "verb", None),
            getattr(action, "action_id", None),
            getattr(action, "target", None),
            getattr(action, "target_name", None),
        ):
            token = _normalize_token(value)
            if token:
                tokens.add(token)
                tokens.update(part for part in token.split("_") if part)
            canonical = canonical_action_verb(value)
            if canonical:
                tokens.add(canonical)
    return frozenset(tokens)


def _capability_is_declared(capability: str, declared: frozenset[str]) -> bool:
    """Whether a single capability verb token is present in the declared universe.

    Matches the verb directly or via its canonical mutating form, so an operation
    declared with any synonym (``edit``/``modify``/``change`` -> ``update``)
    satisfies a surface whose capability resolves to the same canonical verb. Only
    a single verb token is reconciled (callers pass one verb), keeping the check
    precise rather than letting unrelated label words mask a real gap.
    """

    if not capability:
        return True
    if capability in declared:
        return True
    canonical = canonical_action_verb(capability)
    return bool(canonical and canonical in declared)


def detect_undeclared_surfaces(
    source_texts: Iterable[tuple[str, str]],
    declared_tokens: frozenset[str],
    runtime_tokens: frozenset[str] = frozenset(),
    *,
    extra_authoring_verbs: frozenset[str] = frozenset(),
) -> tuple[UndeclaredSurface, ...]:
    """Return implemented surfaces whose capability is not declared.

    ``source_texts`` is an iterable of ``(display_path, text)`` pairs. Keeping the
    detector pure over text makes it trivially testable without a real source
    tree, mirroring ``detect_capability_gaps``.
    """

    covered = declared_tokens | runtime_tokens
    surfaces: list[UndeclaredSurface] = []
    for source, text in source_texts:
        # (1) Literal <button> controls with a mutating/authoring verb label.
        for match in _BUTTON_RE.finditer(text):
            label = _control_label(match.group("label") or "")
            # ``verb`` is the *specific* capability token to reconcile. Resolve it
            # from the canonical mutating taxonomy first, then from project-supplied
            # authoring verbs. We deliberately reconcile only this verb token (not
            # the whole label) so an unrelated label word that happens to match a
            # declared target cannot mask an undeclared authoring capability.
            verb = canonical_action_verb(label)
            if verb is None and label:
                parts = [part for part in _normalize_token(label).split("_") if part]
                verb = next((part for part in parts if part in extra_authoring_verbs), None)
            if verb is None:
                continue
            if _capability_is_declared(verb, covered):
                continue
            surfaces.append(
                UndeclaredSurface(kind="control", capability=verb, label=label, source=source)
            )

        # (2) Generic content-authoring surfaces (contenteditable / role=textbox).
        if _EDITOR_SURFACE_RE.search(text):
            if not _capability_is_declared(_EDITOR_CAPABILITY_VERB, covered):
                surfaces.append(
                    UndeclaredSurface(
                        kind="editor",
                        capability=_EDITOR_CAPABILITY_VERB,
                        label="content editor",
                        source=source,
                    )
                )
    return tuple(surfaces)


def surface_reconciliation_settings(
    config: Mapping[str, Any],
) -> tuple[bool, frozenset[str]]:
    """Resolve the enable flag and extra authoring verbs from project ``config``.

    Defaults: enabled (advisory only). The settings key is absent by default, so
    existing projects keep the built-in behaviour (an opt-in extension that only
    adds non-fatal advisory warnings). A project may opt out with
    ``enabled: false`` or widen the recognised control verbs with
    ``authoring_verbs`` (useful when a domain uses non-English or custom labels).
    """

    settings = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(settings, Mapping):
        return True, frozenset()
    enabled = bool(settings.get("enabled", True))
    raw_verbs = settings.get("authoring_verbs")
    extra: set[str] = set()
    if isinstance(raw_verbs, (list, tuple, set)):
        for item in raw_verbs:
            token = _normalize_token(item)
            if token:
                extra.add(token)
    return enabled, frozenset(extra)


def _has_declared_operations(flows: Iterable[tuple[str, Any]]) -> bool:
    return any(operation_flow_operations(flow) for _source, flow in flows)


def surface_reconciliation_warnings(
    source_texts: Iterable[tuple[str, str]],
    flows: Iterable[tuple[str, Any]],
    config: Mapping[str, Any],
    *,
    runtime_tokens: frozenset[str] = frozenset(),
) -> list[str]:
    """Return advisory warning strings for undeclared interactive surfaces.

    Reconciliation only runs when the project actually uses ``operation_flow``.
    A project with **no** declared operations has not opted into operation-driven
    coverage, so there is nothing to reconcile against and flagging every control
    would be pure noise. This mirrors ``capability_completeness``/operational E2E
    axes, which are likewise dormant without declared operations.
    """

    enabled, extra_authoring_verbs = surface_reconciliation_settings(config)
    if not enabled:
        return []
    flows = list(flows)
    if not _has_declared_operations(flows):
        return []
    declared = declared_capability_tokens(flows)
    surfaces = detect_undeclared_surfaces(
        source_texts,
        declared,
        runtime_tokens,
        extra_authoring_verbs=extra_authoring_verbs,
    )
    # De-duplicate identical (capability, source) editor/control hits so a file
    # with many authoring buttons for the same undeclared capability warns once.
    seen: set[tuple[str, str, str]] = set()
    messages: list[str] = []
    for surface in surfaces:
        key = (surface.kind, surface.capability, surface.source)
        if key in seen:
            continue
        seen.add(key)
        messages.append(surface.message)
    return messages


def iter_markup_source_texts(
    files: Iterable[Path],
    *,
    display: Callable[[Path], str],
    read_text: Callable[[Path], str],
) -> list[tuple[str, str]]:
    """Adapt configured source files into ``(display_path, text)`` markup pairs.

    Only markup-bearing suffixes are kept, matching the gate used by the existing
    interactive-control / authenticated-global-action scanners.
    """

    pairs: list[tuple[str, str]] = []
    for path in files:
        if path.suffix not in _MARKUP_SUFFIXES:
            continue
        pairs.append((display(path), read_text(path)))
    return pairs

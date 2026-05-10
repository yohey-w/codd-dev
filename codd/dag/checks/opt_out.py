"""Generic opt-out policy for DAG checks.

This module introduces a single, check-agnostic representation of "the user
has explicitly asked this gate to stand down". It is the only sanctioned way
to disable a registered DAG check; per-check ad-hoc flags (e.g. legacy
``ci.provider: none`` style) must be re-routed through this policy via the
:meth:`DagCheck.detect_opt_out` hook.

A valid opt-out requires:

* an explicit ``opt_outs`` entry in ``codd.yaml`` whose ``check`` matches a
  registered DAG check name;
* a non-empty ``reason``;
* an ISO ``expires_at`` date strictly in the future at evaluation time.

Anything else is rejected. ``codd validate`` surfaces the policy errors as
configuration problems; ``codd dag verify`` aggregates active and expired
opt-outs separately from the normal pass/fail counts so the cost of a
standing opt-out is always visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping


OPT_OUT_STATUS = "opt_out"


@dataclass(frozen=True)
class OptOutDeclaration:
    """One opt-out entry parsed from ``codd.yaml``."""

    check: str
    reason: str
    expires_at: date
    approved_by: str | None = None
    context: str | None = None

    def is_expired(self, today: date) -> bool:
        return self.expires_at <= today


@dataclass(frozen=True)
class OptOutSignal:
    """Emitted by a check when its config indicates an explicit opt-out request."""

    check_name: str
    source: str


@dataclass(frozen=True)
class OptOutPolicyError:
    """A configuration-level violation of the opt-out policy."""

    code: str
    message: str
    check: str | None = None


@dataclass
class OptOutPolicy:
    """Resolved opt-out declarations for a project."""

    declarations: list[OptOutDeclaration] = field(default_factory=list)
    parse_errors: list[OptOutPolicyError] = field(default_factory=list)

    @classmethod
    def from_config(cls, codd_config: Mapping[str, Any] | None) -> "OptOutPolicy":
        """Build a policy from the parsed ``codd.yaml`` mapping.

        Unknown / unparseable shapes are recorded as ``parse_errors`` rather
        than raised — this lets ``codd validate`` report them alongside other
        validation issues.
        """

        if not isinstance(codd_config, Mapping):
            return cls()

        raw = codd_config.get("opt_outs")
        if raw is None:
            return cls()
        if not isinstance(raw, list):
            return cls(
                parse_errors=[
                    OptOutPolicyError(
                        code="invalid_opt_outs_section",
                        message="opt_outs must be a list of mappings",
                    )
                ]
            )

        declarations: list[OptOutDeclaration] = []
        errors: list[OptOutPolicyError] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                errors.append(
                    OptOutPolicyError(
                        code="invalid_opt_out_entry",
                        message=f"opt_outs[{index}] must be a mapping",
                    )
                )
                continue
            decl, entry_errors = _build_declaration(entry, index)
            errors.extend(entry_errors)
            if decl is not None:
                declarations.append(decl)

        return cls(declarations=declarations, parse_errors=errors)

    def lookup(self, check_name: str) -> OptOutDeclaration | None:
        """Return the raw declaration for ``check_name`` (regardless of expiry).

        Expiry handling is the caller's responsibility: a declaration may be
        present but expired, and the caller decides whether that is treated as
        a hard failure or a warning. This keeps the policy itself stateless
        with respect to "today".
        """

        for declaration in self.declarations:
            if declaration.check == check_name:
                return declaration
        return None

    def validate(
        self,
        today: date,
        registered_check_names: Iterable[str],
    ) -> list[OptOutPolicyError]:
        """Return all opt-out policy errors at the given evaluation date.

        Errors include parse errors discovered at ``from_config`` time,
        unknown check names, duplicates, and expired declarations.
        """

        errors: list[OptOutPolicyError] = list(self.parse_errors)
        registered = set(registered_check_names)
        seen_checks: set[str] = set()

        for declaration in self.declarations:
            if declaration.check in seen_checks:
                errors.append(
                    OptOutPolicyError(
                        code="duplicate",
                        check=declaration.check,
                        message=(
                            f"opt_outs has more than one entry for check "
                            f"{declaration.check!r}"
                        ),
                    )
                )
                continue
            seen_checks.add(declaration.check)

            if declaration.check not in registered:
                errors.append(
                    OptOutPolicyError(
                        code="unknown_check",
                        check=declaration.check,
                        message=(
                            f"opt_outs entry references unknown check "
                            f"{declaration.check!r}"
                        ),
                    )
                )
            if declaration.is_expired(today):
                errors.append(
                    OptOutPolicyError(
                        code="expired",
                        check=declaration.check,
                        message=(
                            f"opt_outs entry for {declaration.check!r} expired on "
                            f"{declaration.expires_at.isoformat()}; renew or remove"
                        ),
                    )
                )

        return errors

    def active(self, today: date) -> list[OptOutDeclaration]:
        return [decl for decl in self.declarations if not decl.is_expired(today)]

    def expired(self, today: date) -> list[OptOutDeclaration]:
        return [decl for decl in self.declarations if decl.is_expired(today)]


def _build_declaration(
    entry: Mapping[str, Any],
    index: int,
) -> tuple[OptOutDeclaration | None, list[OptOutPolicyError]]:
    errors: list[OptOutPolicyError] = []
    check_value = entry.get("check")
    check_name: str | None = None
    if isinstance(check_value, str) and check_value.strip():
        check_name = check_value.strip()
    else:
        errors.append(
            OptOutPolicyError(
                code="missing_check",
                message=f"opt_outs[{index}] is missing a non-empty 'check' name",
            )
        )

    reason_value = entry.get("reason")
    reason = reason_value.strip() if isinstance(reason_value, str) else ""
    if not reason:
        errors.append(
            OptOutPolicyError(
                code="missing_reason",
                check=check_name,
                message=(
                    f"opt_outs[{index}] requires a non-empty 'reason' "
                    "(briefly explain why this gate is disabled)"
                ),
            )
        )

    expires_at = _coerce_expires_at(entry.get("expires_at"))
    if expires_at is None:
        errors.append(
            OptOutPolicyError(
                code="missing_expires_at",
                check=check_name,
                message=(
                    f"opt_outs[{index}] requires 'expires_at' as an ISO date "
                    "(YYYY-MM-DD) so the opt-out cannot be permanent"
                ),
            )
        )

    if check_name is None or expires_at is None or not reason:
        return None, errors

    approved_by = entry.get("approved_by")
    approved_by = approved_by.strip() if isinstance(approved_by, str) and approved_by.strip() else None

    context = entry.get("context")
    context = context.strip() if isinstance(context, str) and context.strip() else None

    declaration = OptOutDeclaration(
        check=check_name,
        reason=reason,
        expires_at=expires_at,
        approved_by=approved_by,
        context=context,
    )
    return declaration, errors


def _coerce_expires_at(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None

"""Project-level stack command OVERRIDE — Contract Kernel v3.x (TRANSPORT-ONLY).

A project may declare ``stack.command_overrides.<slot>`` in its ``codd.yaml`` to
change WHAT executable runs for an already-composed VERIFICATION slot — so a project
can run its bespoke CI script (``npm run test:vitest:ci``) instead of the profile's
default ``npx vitest run`` — WITHOUT being able to weaken the no-fake-green guarantee.

This is the ``command_overrides`` half of v3.0's "language-free core + project-tunable
transport" (the design GPT-5.5 Pro consult 2026-06-21 — a *transport override*). It is
the deliberate mirror image of the AUTHENTICITY override (``command_observation_policies``,
:mod:`codd.stack.command_authenticity`): that one tunes a slot's GREEN CRITERIA
(strengthen-only, core-owned); THIS one tunes a slot's COMMAND TRANSPORT, and CANNOT
touch the green criteria at all. The two are orthogonal and both fail-closed.

CARDINAL ANTI-FALSE-GREEN INVARIANT (do not violate)
====================================================
An overridden verification slot may change the command transport, but the CORE still
owns the slot's MEANING and ALL green criteria. A green result for an overridden slot is
valid ONLY if:

  * the overridden command was ACTUALLY invoked and exited correctly
    (:func:`codd.stack.command_plan.execute_stack_command_plan` — exit-code gate);
  * it produced CURRENT-RUN authentic evidence required by the slot's
    CORE/PROFILE-owned authenticity policy
    (:func:`codd.stack.command_authenticity.assert_stack_commands_authentic` — the
    report/observation/no-op gate, keyed by the *slot id*, which the override CANNOT
    change);
  * it satisfied the slot's obligations
    (:func:`codd.stack.project.enforce_stack_obligation_gate`);
  * it did NOT alter the slot's scope, observation policy, authenticity kind, report
    adapter, or obligations.

So the ONLY thing this module is allowed to change is the *transport*:

  ALLOWED to change (transport):
    * ``argv``                              — the executable + arguments
    * ``cwd``                               — under strict project/module containment
    * additive ``env`` / ``required_env``   — extra env (never removes base env)
    * ``report.path``                       — under strict current-run evidence rules
    * ``report.capture``                    — ``file`` | ``stdout`` only

  FORBIDDEN to change (the green criteria — base/core-owned):
    * ``id`` / ``owner``                    — the slot's identity
    * ``kind`` / ``observation`` / ``policy`` — the authenticity kind + observation
    * ``scope``                             — the must-include source/test sets
    * ``adapter`` (report adapter)          — how the report is parsed
    * ``obligations``                       — the slot's release-blockers
    * verification-vs-non-verification classification (only VERIFICATION_SLOTS that the
      contract ALREADY composed are overrideable; NON_VERIFICATION / unknown slots are
      rejected)

WHY ``verify`` (vitest) IS JUDGED AS A TEST SLOT WITHOUT THE OVERRIDE TOUCHING ``kind``
=====================================================================================
The TypeScript ``verify`` slot is *semantically* a TEST slot (it runs vitest), but its
SHIPPED default authenticity policy is ``STATIC_EXECUTION`` (so the no-override path is
byte-identical to today). To have an overridden ``verify`` judged as ``TEST_REPORT``
(report required, >=1 executed test, fail on observed failures), a project STRENGTHENS
its authenticity policy via the SEPARATE, pre-existing, core-owned ``stack.command_
observation_policies`` field (``STATIC -> TEST_REPORT`` is a STRENGTHENING, enforced by
:func:`codd.stack.command_authenticity.resolve_stack_command_observation_policy`). THIS
override never sets the kind — deriving "kind" from "has a report block" is the exact
false-green hole the authenticity module forbids. Transport (here) and green-criteria
(``command_observation_policies``) are deliberately two different keys so a project
cannot smuggle a weaker policy in through a transport change.

THE LOCKED OVERRIDE IS A REVIEWED TRUST BOUNDARY (documented, not silently skipped)
================================================================================
GPT's design notes that report parsing alone cannot cryptographically prove a *project*
command's green report was produced by vitest/playwright rather than a project script
that writes a fake parseable green report with one passed test. The fully-adversarial
defense is a behavioral-subsumption PROOF GATE (the ``replace_with_proof`` model already
in :mod:`codd.stack.replacement_proof`). Implementing that proof gate for project command
overrides is OUT OF SCOPE for this pass. Instead, **a project command override is treated
as a LOCKED, REVIEWED TRUST BOUNDARY**: the override is folded into the resolved contract
``content_hash`` (so any change to argv/cwd/env/report DRIFTS the committed
``stack.lock`` and forces re-review — :func:`codd.stack.lock.enforce_stack_lock`), and a
maintainer reviewing that lock diff is the trust anchor. This is a deliberate, documented
boundary, NOT a silent gap: a project that adds/edits an override cannot do so under a
stable lock, and every honest-misconfiguration false-green (no report, zero tests,
no-op argv, ``sh -c`` wrapper, stale report, out-of-tree cwd/report) is still RED by
construction below + in the executor + in the authenticity gate.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from codd.languages.profile import ReportSpec

from .compose import (
    NON_VERIFICATION_SLOTS,
    VERIFICATION_SLOTS,
    ResolvedStackContract,
    _content_hash,
)


class StackCommandOverrideError(ValueError):
    """A project ``command_overrides`` declaration is invalid (fail-closed RED).

    Raised by :func:`apply_project_command_overrides` for EVERY way an override could
    weaken (or be too ambiguous to safely honor) the no-fake-green guarantee: a target
    slot that is not an already-composed VERIFICATION slot, a forbidden key (``kind`` /
    ``observation`` / ``policy`` / ``scope`` / ``adapter`` / ``obligations`` / ``owner``
    / ``id``), a no-op argv (``true`` / ``echo`` / empty), a direct ``sh -c`` shell
    wrapper, a ``cwd`` that escapes the project/module root, a ``report.path`` that
    escapes the project root, or an unknown ``report.capture``. Like the language
    ``observation`` block's load-time rejection, a bad override is rejected at RESOLVE,
    never silently downgraded. The call-sites translate it to their context's RED."""


#: Keys a project override is FORBIDDEN to declare — every one of these is part of a
#: slot's GREEN CRITERIA (the slot's meaning), owned by the core/profile, never by a
#: project transport override. Declaring any of them is a fail-closed RED (a project that
#: needs to tune the authenticity kind uses the separate, strengthen-only
#: ``command_observation_policies`` field; everything else here is simply not tunable).
_FORBIDDEN_OVERRIDE_KEYS = frozenset(
    {
        "kind",  # the authenticity kind (TEST_REPORT / BUILD / STATIC)
        "observation",  # the observation policy block
        "policy",  # an alias a project might reach for
        "scope",  # must-include source/test sets
        "adapter",  # the report adapter (how the report is parsed)
        "report_adapter",  # the same, spelled flat
        "obligations",  # the slot's release-blockers
        "owner",  # the slot's composed owner namespace
        "id",  # the slot id (identity)
        "requires_materialized_deps",  # a base-owned execution precondition
        "min_collected_tests",  # an authenticity-policy parameter (not transport)
    }
)

#: The ONLY keys a project override MAY declare (transport). ``reason`` is a free-form
#: human note (recorded, not load-bearing). Anything outside this set that is also not in
#: :data:`_FORBIDDEN_OVERRIDE_KEYS` is an unknown key → fail-closed RED (the schema is
#: CLOSED, like the language ``observation`` block, so a typo/new weakening flag can never
#: be silently ignored).
_ALLOWED_OVERRIDE_KEYS = frozenset(
    {"argv", "cwd", "env", "required_env", "report", "reason"}
)

#: ``report:`` sub-keys a project override MAY declare. ``adapter`` / ``format`` stay
#: BASE-owned (the report parser is part of the green criteria), so they are NOT here —
#: an override that tries to set them is rejected as a forbidden key.
_ALLOWED_REPORT_KEYS = frozenset({"path", "capture"})

#: Permitted ``report.capture`` values (mirror the executor's transport handling). A
#: ``file`` capture means "the command writes ``report.path`` directly" (vitest
#: ``--outputFile``); ``stdout`` means "the command streams its report to stdout, the
#: executor tees it to a per-slot evidence file" (playwright ``--reporter=json``).
_ALLOWED_CAPTURE = frozenset({"file", "stdout"})

#: argv[0] basenames that DO NO WORK — a command that cannot fail is not a check. This is
#: defense-in-depth at VALIDATION time: the authenticity gate's no-op detector
#: (:func:`codd.stack.command_authenticity.is_static_noop_command`) is the runtime
#: authority (it also unwraps package scripts), but rejecting the obvious literal no-op at
#: resolve makes a typo'd override fail loudly + early, not at execution.
_NOOP_ARGV0 = frozenset({"true", ":", "echo", "printf", "false"})

#: Shells we refuse to let an override invoke DIRECTLY with an inline script (``sh -c
#: "..."``). A ``-c`` inline script is opaque to the package-script unwrapper and is the
#: natural carrier for a hidden no-op / fake-report writer, so it is rejected at resolve
#: (defense in depth — the brief's "reject a direct ``sh -c`` wrapper"). Running a real
#: script FILE (``bash ./scripts/test.sh``) is fine; only the inline ``-c`` form is
#: blocked.
_SHELL_BINS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})


@dataclass(frozen=True)
class ProjectCommandOverride:
    """A parsed, validated, TRANSPORT-ONLY override for one already-composed slot.

    Frozen + minimal: it carries ONLY the transport fields a project may change. The
    parse (:meth:`from_mapping`) enforces the closed schema; the apply
    (:func:`apply_project_command_overrides`) enforces the semantic containment rules
    (slot must be a composed verification slot, cwd/report inside the root, …) and the
    merge (transport replaces argv/cwd/report.path/report.capture, env is ADDITIVE; the
    base keeps scope/observation/adapter/obligations)."""

    slot_id: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    required_env: tuple[str, ...] = ()
    report_path: str | None = None
    report_capture: str | None = None
    reason: str | None = None
    #: True when the declaration carried a ``report:`` block at all (so a `report: {}` or
    #: a `report` with only ``capture`` is distinguishable from "no report key"). When an
    #: override declares ``report:`` it REPLACES the base report transport wholesale (path
    #: becomes the override's, or None if it gives only a capture) — this is what lets a
    #: project's CI script DROP the report (→ RED for a TEST slot), an intentional,
    #: detectable state, never a silent inheritance of the base path.
    report_declared: bool = False

    @classmethod
    def from_mapping(cls, slot_id: str, raw: Any) -> "ProjectCommandOverride":
        """Parse + STRICTLY validate one ``command_overrides.<slot>`` mapping.

        Closed-schema (anti-false-green): every key must be in
        :data:`_ALLOWED_OVERRIDE_KEYS`; any key in :data:`_FORBIDDEN_OVERRIDE_KEYS` (or
        any other unknown key) is a fail-closed RED. ``argv`` is required and must be a
        non-empty list of strings. ``report`` (when present) is itself closed-schema
        (only ``path`` / ``capture``; ``capture`` in ``{file, stdout}``). This is purely
        SHAPE validation — semantic containment (cwd/report inside the root) is enforced
        in :func:`apply_project_command_overrides` where the project root is known."""
        if not isinstance(raw, Mapping):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] must be a mapping (got "
                f"{type(raw).__name__}); declare e.g. {{argv: [...], report: {{path: ...}}}}"
            )

        forbidden = sorted(k for k in raw if k in _FORBIDDEN_OVERRIDE_KEYS)
        if forbidden:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] declares forbidden key(s) {forbidden} — a "
                "project command override is TRANSPORT-ONLY. The slot's authenticity kind, "
                "observation policy, scope, report adapter, obligations and identity are "
                "owned by the core/profile and CANNOT be changed by an override (to tune the "
                "authenticity policy, use the separate strengthen-only "
                "`command_observation_policies`). Permitted keys: "
                f"{sorted(_ALLOWED_OVERRIDE_KEYS)}."
            )
        unknown = sorted(k for k in raw if k not in _ALLOWED_OVERRIDE_KEYS)
        if unknown:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] declares unknown key(s) {unknown} — the "
                "override schema is CLOSED (a typo or a new weakening flag is never silently "
                f"ignored). Permitted keys: {sorted(_ALLOWED_OVERRIDE_KEYS)}."
            )

        argv = cls._parse_argv(slot_id, raw.get("argv"))
        cwd = cls._parse_opt_str(slot_id, "cwd", raw.get("cwd"))
        env = cls._parse_env(slot_id, raw.get("env"))
        required_env = cls._parse_str_tuple(slot_id, "required_env", raw.get("required_env"))
        reason = cls._parse_opt_str(slot_id, "reason", raw.get("reason"))

        report_declared = "report" in raw
        report_path: str | None = None
        report_capture: str | None = None
        if report_declared:
            report_path, report_capture = cls._parse_report(slot_id, raw.get("report"))

        return cls(
            slot_id=slot_id,
            argv=argv,
            cwd=cwd,
            env=env,
            required_env=required_env,
            report_path=report_path,
            report_capture=report_capture,
            reason=reason,
            report_declared=report_declared,
        )

    # ── shape parsers (each fail-closed on a malformed value) ──────────────────

    @staticmethod
    def _parse_argv(slot_id: str, value: Any) -> tuple[str, ...]:
        if value is None:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] requires an `argv` (the command to run) — "
                "a transport override with no command is meaningless"
            )
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].argv must be a LIST of strings "
                f"(got {type(value).__name__}); a shell string is not permitted — declare "
                'argv as a list (e.g. ["npm", "run", "test:ci"]), never "npm run test:ci"'
            )
        argv = tuple(str(a) for a in value)
        if not argv or not any(a.strip() for a in argv):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].argv is empty — an empty command does no "
                "work (a command that cannot fail is not a check; anti-false-green RED)"
            )
        return argv

    @staticmethod
    def _parse_opt_str(slot_id: str, key: str, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].{key} must be a string (got "
                f"{type(value).__name__})"
            )
        return value

    @staticmethod
    def _parse_str_tuple(slot_id: str, key: str, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].{key} must be a list of strings "
                f"(got {type(value).__name__})"
            )
        return tuple(str(v) for v in value)

    @staticmethod
    def _parse_env(slot_id: str, value: Any) -> Mapping[str, str]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].env must be a mapping of str->str "
                f"(got {type(value).__name__})"
            )
        return MappingProxyType({str(k): str(v) for k, v in value.items()})

    @staticmethod
    def _parse_report(slot_id: str, value: Any) -> tuple[str | None, str | None]:
        if value is None:
            # `report:` declared but null → an explicit "no report" transport (drops the
            # base report). For a TEST slot the authenticity gate then reds (REPORT_MISSING)
            # — an intentional, detectable RED, never a silent drop.
            return None, None
        if not isinstance(value, Mapping):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].report must be a mapping (got "
                f"{type(value).__name__}); permitted keys: {sorted(_ALLOWED_REPORT_KEYS)}"
            )
        bad = sorted(k for k in value if k not in _ALLOWED_REPORT_KEYS)
        if bad:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].report declares forbidden/unknown key(s) "
                f"{bad} — an override may set only the report TRANSPORT ({sorted(_ALLOWED_REPORT_KEYS)}); "
                "the report `adapter`/`format` (how the report is PARSED) is base-owned and "
                "cannot be changed (changing the parser is changing a green criterion)"
            )
        path = value.get("path")
        if path is not None and not isinstance(path, str):
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}].report.path must be a string (got "
                f"{type(path).__name__})"
            )
        capture = value.get("capture")
        if capture is not None:
            if not isinstance(capture, str) or capture.strip().lower() not in _ALLOWED_CAPTURE:
                raise StackCommandOverrideError(
                    f"command_overrides[{slot_id!r}].report.capture must be one of "
                    f"{sorted(_ALLOWED_CAPTURE)} (got {capture!r})"
                )
            capture = capture.strip().lower()
        return path, capture


# ── argv safety (no-op + direct-shell-wrapper rejection at resolve time) ──────


def _reject_unsafe_argv(slot_id: str, argv: tuple[str, ...]) -> None:
    """Fail-closed on an obviously-unsafe override argv at RESOLVE time (defense in depth).

    Two rejections the brief calls for, BEFORE the command ever runs:

    * a literal no-op (``true`` / ``:`` / ``echo`` / ``printf`` / ``false`` / empty) — a
      command that cannot fail (or always fails) is not a transport for a real check. The
      runtime authenticity no-op gate is the authority (it also unwraps ``npm run x`` whose
      script is ``true``); this catches the obvious literal early + loudly.
    * a DIRECT shell wrapper with an inline script (``sh -c "vitest || true"``) — an
      inline ``-c`` script is opaque to the package-script unwrapper and is the natural
      carrier for a hidden no-op / fake-report writer. A real script FILE
      (``bash ./scripts/ci.sh``) is allowed; only the inline ``-c`` form is blocked."""
    normalized = tuple(a.strip() for a in argv if a is not None and a.strip())
    if not normalized:
        raise StackCommandOverrideError(
            f"command_overrides[{slot_id!r}].argv is a no-op (empty) — RED (a command that "
            "cannot fail is not a check)"
        )
    base = PurePosixPath(normalized[0]).name
    if base in _NOOP_ARGV0:
        raise StackCommandOverrideError(
            f"command_overrides[{slot_id!r}].argv {list(argv)} is a static no-op/always-fail "
            f"command (argv[0]={base!r}) — RED at validation (a `true`/`echo`/`false` "
            "transport can never be an authentic check; anti-false-green). Point the "
            "override at the real test/build command."
        )
    if base in _SHELL_BINS and any(a.strip() == "-c" for a in normalized[1:]):
        raise StackCommandOverrideError(
            f"command_overrides[{slot_id!r}].argv {list(argv)} invokes a shell directly with "
            f"an inline `-c` script ({base} -c \"…\") — RED (an inline shell script is opaque "
            "to the no-op/package-script analysis and is the natural carrier for a hidden "
            "no-op or fake-report writer). Run a real command or a script FILE "
            "(e.g. `bash ./scripts/ci.sh`) instead of an inline `-c` string."
        )


def _resolve_inside_root(
    project_root: Path, value: str | None, *, slot_id: str, label: str
) -> None:
    """Fail-closed unless ``value`` (a relative cwd/report path) stays inside ``project_root``.

    The override's ``cwd`` and ``report.path`` are project-relative. This rejects an
    override that tries to ``cwd`` OUT of the project to dodge the real tests, or to write
    its report OUTSIDE the tree (where the authenticity reader fail-closes anyway, but we
    reject it earlier + with a clearer message). A literal layout placeholder
    (``{module_root}``) is left alone here — it is substituted later by the plan, and the
    plan/executor enforce containment on the resolved value; we only containment-check a
    CONCRETE relative path the project hard-coded.

    NOTE: this is a *static* containment check on the declared string (it does not touch
    the filesystem, so it works at resolve time before the project tree exists). The
    EXECUTOR re-checks the resolved cwd against the real root at run time
    (:func:`codd.stack.command_plan.default_stack_command_executor`) — defense in depth."""
    if value is None:
        return
    if "{" in value and "}" in value:
        # Contains a layout placeholder — resolved + containment-checked later (plan +
        # executor). Do not statically reject a template here.
        return
    # Reject an absolute path or a parent-escaping relative path WITHOUT touching the FS
    # (resolve() would need the dir to exist). Normalize the POSIX path and check it does
    # not start with `..` and is not absolute.
    pure = PurePosixPath(value)
    if pure.is_absolute():
        raise StackCommandOverrideError(
            f"command_overrides[{slot_id!r}].{label} {value!r} is an ABSOLUTE path — it must "
            "be RELATIVE to the project root (an absolute path can point anywhere; "
            "containment cannot be guaranteed). RED."
        )
    parts = pure.parts
    if any(p == ".." for p in parts):
        raise StackCommandOverrideError(
            f"command_overrides[{slot_id!r}].{label} {value!r} escapes the project root via "
            "`..` — RED (an override may not cwd outside the project/module root to dodge "
            "the real tests, nor write its report outside the tree)."
        )


def _merge_report(base: ReportSpec | None, override: ProjectCommandOverride) -> ReportSpec | None:
    """Merge the override's TRANSPORT into the base report, keeping the adapter base-owned.

    * If the override did NOT declare a ``report:`` block → the base report is kept AS-IS
      (transport unchanged; the override only changed argv/cwd/env).
    * If the override DID declare ``report:`` → it REPLACES the transport (``path`` /
      ``capture``) while the ``adapter`` and ``format`` (the report's GREEN-criteria
      parser) stay whatever the BASE declared. A ``report:`` block that drops the path
      (null / capture-only) yields a report with ``path=None`` — for a TEST slot the
      authenticity gate then reds REPORT_MISSING (an intentional, detectable RED, never a
      silent inheritance of the base path).

    The adapter is ALWAYS taken from the base — an override can never change how the report
    is parsed (that is a green criterion). If the base had no report at all and the
    override declares one, the resulting report has ``adapter=None``; for a TEST slot the
    authenticity gate then reds REPORT_UNREADABLE (no adapter to read it) — again a
    detectable RED, never a green."""
    if not override.report_declared:
        return base
    base_adapter = base.adapter if base is not None else None
    base_format = base.format if base is not None else None
    return ReportSpec(
        path=override.report_path,
        format=base_format,
        adapter=base_adapter,
        capture=override.report_capture,
    )


def apply_project_command_overrides(
    contract: ResolvedStackContract,
    raw_overrides: Mapping[str, Any] | None,
) -> ResolvedStackContract:
    """Apply ``stack.command_overrides`` to a composed contract — TRANSPORT-ONLY, fail-closed.

    This is the seam :func:`codd.stack.resolve.resolve_stack_from_declaration` calls AFTER
    :func:`codd.stack.compose.compose`, so the override is baked into the resolved contract
    BEFORE intake, materialization, authenticity, obligation, and lock verification consume
    it. For each ``command_overrides.<slot>``:

    SAFETY GATES (every one fail-closed RED via :class:`StackCommandOverrideError`):

      1. the target slot MUST exist in ``contract.commands`` (you cannot override a slot
         the stack did not compose);
      2. the target slot MUST be in :data:`~codd.stack.compose.VERIFICATION_SLOTS` and NOT
         in :data:`~codd.stack.compose.NON_VERIFICATION_SLOTS` (only release-check slots
         are overrideable — a project cannot turn ``dev``/``start`` into a check, nor
         override an unknown slot);
      3. the declaration is closed-schema (no ``kind``/``observation``/``scope``/
         ``adapter``/``obligations``/``owner``/``id`` — see
         :meth:`ProjectCommandOverride.from_mapping`);
      4. the argv is not a literal no-op nor a direct ``sh -c`` inline-script wrapper
         (:func:`_reject_unsafe_argv`);
      5. a CONCRETE (non-placeholder) ``cwd`` / ``report.path`` stays inside the project
         root (:func:`_resolve_inside_root`).

    TRANSPORT MERGE (what actually changes on the slot's :class:`CommandSpec`):

      * ``argv`` ← override argv;
      * ``cwd`` ← override cwd (or the base cwd if the override omits it);
      * ``env`` ← base env UPDATED with the override env (ADDITIVE — an override can ADD
        env but never REMOVE a base env var; a base key the override repeats is overwritten
        by the override value, but no base key is dropped);
      * ``report`` ← :func:`_merge_report` (override transport, base adapter);
      * ``mutates`` ← the override's ``required_env`` is recorded on the spec's ``extra``
        (advisory; the run env is the operator's responsibility) and the rest of the spec
        — ``scope`` / ``observation`` / ``requires_materialized_deps`` — is KEPT FROM THE
        BASE (the green criteria are untouched).

    The resulting contract carries the override records (for the trace) and a RECOMPUTED
    ``content_hash`` over the FULL command canonicalization (argv + cwd + env + report +
    scope), so any override change DRIFTS the committed ``stack.lock`` and is re-reviewed
    (the locked-override-is-a-reviewed-trust-boundary invariant; see the module docstring).
    A contract with NO overrides is returned UNCHANGED (same object), so the no-override
    path is byte-identical (existing locks/tests unaffected)."""
    if not raw_overrides:
        return contract
    if not isinstance(raw_overrides, Mapping):
        raise StackCommandOverrideError(
            "stack.command_overrides must be a mapping of slot_id -> override (got "
            f"{type(raw_overrides).__name__})"
        )

    commands = dict(contract.commands)
    records: dict[str, ProjectCommandOverride] = {}

    for slot_id, raw in raw_overrides.items():
        slot_id = str(slot_id)
        # Gate 1: the slot must have been composed.
        if slot_id not in commands:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] targets a slot the resolved stack did not "
                f"compose (composed slots: {sorted(commands)}) — you can only override a "
                "command the stack actually declares (anti-false-green: an override cannot "
                "introduce a brand-new slot)."
            )
        # Gate 2: it must be a VERIFICATION slot (and not a known non-verification one).
        if slot_id in NON_VERIFICATION_SLOTS or slot_id not in VERIFICATION_SLOTS:
            raise StackCommandOverrideError(
                f"command_overrides[{slot_id!r}] targets a non-overrideable slot — only "
                "already-composed VERIFICATION slots (release checks: typecheck / verify / "
                "unit_test / integration_test / e2e_test / build / framework_build / "
                "coverage / lint / migration_check / eval) may be overridden. A "
                "non-verification convenience slot (dev / start / generate / migrate) or an "
                "unknown slot cannot be turned into / replaced as a check by an override."
            )

        override = ProjectCommandOverride.from_mapping(slot_id, raw)
        # Gate 4: argv no-op / direct-shell-wrapper rejection.
        _reject_unsafe_argv(slot_id, override.argv)
        # Gate 5: static containment of a concrete cwd / report path.
        _resolve_inside_root(Path("."), override.cwd, slot_id=slot_id, label="cwd")
        _resolve_inside_root(
            Path("."), override.report_path, slot_id=slot_id, label="report.path"
        )

        base = commands[slot_id]
        new_env = dict(base.env)  # base env first…
        new_env.update(override.env)  # …then ADD the override env (never drops a base key)
        new_report = _merge_report(base.report, override)

        # Record required_env (advisory) on extra without dropping existing extra keys.
        new_extra = dict(base.extra)
        if override.required_env:
            new_extra["required_env"] = list(override.required_env)
        if override.reason:
            new_extra["override_reason"] = override.reason

        commands[slot_id] = dataclasses.replace(
            base,
            argv=override.argv,
            cwd=override.cwd if override.cwd is not None else base.cwd,
            env=MappingProxyType(new_env),
            report=new_report,
            # GREEN CRITERIA — kept base-owned, NEVER changed by the override:
            scope=base.scope,
            observation=base.observation,
            requires_materialized_deps=base.requires_materialized_deps,
            extra=MappingProxyType(new_extra),
        )
        records[slot_id] = override

    # Recompute the content hash over the FULL command canonicalization so an override
    # drifts the lock (the override is part of the locked, reviewed contract). The
    # no-override early-return above means this branch only runs WHEN there is at least one
    # override — so a contract with no override never re-hashes (byte-identical).
    new_hash = _content_hash(
        contract.layers,
        MappingProxyType(commands),
        contract.obligations,
        contract.file_roles,
        contract.source_sets,
        contract.pending_replacement_proofs,
        include_command_transport=True,
    )

    return dataclasses.replace(
        contract,
        commands=MappingProxyType(commands),
        content_hash=new_hash,
        command_override_records=MappingProxyType(records),
    )


__all__ = [
    "StackCommandOverrideError",
    "ProjectCommandOverride",
    "apply_project_command_overrides",
]

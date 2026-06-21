"""Generic implement-oracle command-sequence executor (Contract Kernel §2).

Runs an oracle expressed as a SEQUENCE of profile commands (a ``kind="command"``
oracle = one command; a ``kind="composite"`` oracle = several, e.g. Go's
``typecheck`` + ``vet``) and unions the per-step observations into a single
:class:`~codd.implement_oracle_types.ImplementOracleResult`. The LANGUAGE-SPECIFIC
knowledge — what a clean step looks like, how to turn a step's exit/stdout/stderr
into normalized findings — lives behind the
:class:`~codd.languages.adapters.implement_oracle.ImplementOracleAdapter`; this
module owns only the language-FREE mechanics: resolve the command from the profile,
substitute its cwd/env layout placeholders, spawn it ``shell=False`` with a bounded
timeout, and apply the anti-false-green ordering.

ADDITIVE / UNCONNECTED. This is NOT wired into the live oracle gate
(:mod:`codd.implement_oracle` still runs its own per-language executors). It is
exercised by fixtures only (``tests/languages/test_oracle_executor.py``), exactly
like :mod:`codd.languages.verify_executor` before its switch — a fake
``ImplementOracleAdapter`` + crafted commands drive every branch deterministically.

ANTI-FALSE-GREEN CONTRACT (GPT §2 — every rule must hold; a not-clean signal ALWAYS
beats a clean-looking one):

* an UNSUBSTITUTED ``{module_root}`` / ``{...}`` placeholder left in a step's cwd or
  env value → the run NEVER spawns there (a spawn in ``<project>/{module_root}`` is
  the v2.75 cwd-bug class). It is RED (``environment_build_error``), not a benign
  miss.
* a step exits NONZERO but the adapter's normalize returns NO findings → an opaque
  ``environment_build_error`` RED (the toolchain failed for a reason the adapter
  could not name — never a benign pass).
* "only third-party deps missing" is NOT this executor's call to wave through: a
  step is clean ONLY when the adapter says ``is_clean=True``. The executor never
  invents a benign verdict — build/typecheck not passing stays RED.
* a step whose command MUTATES (non-empty ``CommandSpec.mutates``) is REJECTED from
  an oracle step (materialize/reconcile — ``go mod tidy`` etc. — is a different
  contract; an oracle must be side-effect free). RED.
* a step that could not be EXECUTED (missing command id, spawn failure, timeout) is
  RED, never skipped-as-clean.

Every step is REQUIRED: the sequence passes ONLY when every step is clean.

LEAF rule. Imports stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`) + the verify-plan placeholder
helper. It MUST NOT import :mod:`codd.implement_oracle` (the gate) — the edge runs
gate → executor, never back.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 — argv is from the trusted language profile, shell=False
from typing import Any, Mapping, Sequence

from codd.implement_oracle_types import (
    EVIDENCE_ENVIRONMENT_BUILD,
    ImplementOracleFinding,
    ImplementOracleResult,
)
from codd.languages.adapters.implement_oracle import (
    ImplementOracleAdapter,
    OracleContext,
)
from codd.languages.verify_plan import _substitute_layout_placeholders

#: Bounded wall-clock for ONE oracle command. Matches the gate's
#: ``DEFAULT_ORACLE_TIMEOUT_SECONDS`` (a cold first ``tsc``/``go build`` over a large
#: graph is generous-but-bounded). Overridable via ``implement.oracle_timeout_seconds``.
DEFAULT_ORACLE_TIMEOUT_SECONDS = 600.0

#: Matches any leftover ``{placeholder}`` token. After substitution, a remaining
#: token means the cwd/env carried a placeholder the layout did not resolve — we must
#: NOT spawn in a literal ``{...}`` directory (the v2.75 cwd-bug class). Conservative:
#: a single ``{`` ... ``}`` pair with a non-empty, brace-free body.
_UNSUBSTITUTED_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _oracle_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    """``implement.oracle_timeout_seconds`` (>0), else the default.

    Read directly from config so the executor stays a leaf (it does not import the
    gate module just for a knob). Same key + magnitude the gate uses.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("oracle_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_ORACLE_TIMEOUT_SECONDS


def _has_unsubstituted_placeholder(value: str | None) -> bool:
    return bool(value) and _UNSUBSTITUTED_PLACEHOLDER_RE.search(value or "") is not None


def _env_finding(code: str, message: str) -> ImplementOracleFinding:
    return ImplementOracleFinding(category=EVIDENCE_ENVIRONMENT_BUILD, code=code, message=message)


def _red(
    *,
    command: str,
    findings: list[ImplementOracleFinding],
    detail: str,
    raw_output: str = "",
    failed_paths: list[str] | None = None,
    diagnostics: list[Any] | None = None,
    executed: bool = True,
) -> ImplementOracleResult:
    """A not-passing :class:`ImplementOracleResult` (the executor's RED constructor)."""
    return ImplementOracleResult(
        passed=False,
        executed=executed,
        command=command,
        findings=findings,
        failed_paths=list(failed_paths or []),
        detail=detail,
        raw_output=raw_output,
        diagnostics=list(diagnostics or []),
    )


def run_command_sequence(
    ctx: OracleContext,
    command_ids: Sequence[str],
    adapter: ImplementOracleAdapter,
) -> ImplementOracleResult:
    """Run an oracle's command sequence and union the adapter's per-step verdicts.

    For each id in ``command_ids`` (order preserved): resolve the
    :class:`~codd.languages.profile.CommandSpec` from
    ``ctx.language_profile.commands``, substitute its cwd/env layout placeholders,
    spawn it ``shell=False`` with a bounded timeout, and hand the raw result to
    ``adapter.normalize_command_result`` for a language-neutral verdict. Returns a
    single :class:`ImplementOracleResult`: ``passed`` ONLY when EVERY step is clean.

    See the module docstring for the full anti-false-green ordering. A
    not-executable / mutating / unsubstituted-placeholder / opaque-nonzero step is
    RED before any pass is considered.
    """
    label = f"{ctx.language_profile.id}-oracle: " + " + ".join(command_ids)
    if not command_ids:
        # An oracle with no command to run proves nothing — never a silent pass.
        return _red(
            command=label,
            findings=[_env_finding("oracle_no_commands", "oracle declared no command steps to run")],
            detail="oracle command sequence was empty (a no-op oracle is not a pass)",
        )

    timeout = _oracle_timeout_seconds(ctx.config)
    layout = ctx.layout_profile

    all_findings: list[ImplementOracleFinding] = []
    all_failed_paths: list[str] = []
    all_diagnostics: list[Any] = []
    raw_parts: list[str] = []
    clean = True

    for command_id in command_ids:
        command = ctx.language_profile.commands.get(command_id)

        # (1) Missing command id → the oracle references a command the profile never
        # declares: a broken contract, RED (never skipped-as-clean).
        if command is None:
            clean = False
            all_findings.append(
                _env_finding(
                    "oracle_command_missing",
                    f"oracle step references command id {command_id!r} not declared in the "
                    f"language profile's commands",
                )
            )
            raw_parts.append(f"## {command_id}: MISSING (no such command in profile)")
            continue

        # (2) A mutating command must never be an oracle step (materialize/reconcile
        # is a different contract — an oracle is side-effect free).
        if command.mutates:
            clean = False
            all_findings.append(
                _env_finding(
                    "oracle_step_mutates",
                    f"command {command_id!r} declares mutates={list(command.mutates)} — a "
                    f"mutating command cannot be an oracle step (an oracle must be "
                    f"side-effect free; materialize/reconcile is a separate contract)",
                )
            )
            raw_parts.append(f"## {command_id}: REJECTED (mutating command, mutates={list(command.mutates)})")
            continue

        # (3) Substitute the cwd/env layout placeholders, then HARD-FAIL on any token
        # the layout did not resolve — never spawn in a literal ``{module_root}`` dir.
        cwd_template = command.cwd
        cwd_value = _substitute_layout_placeholders(cwd_template, layout)
        env_overrides = {
            str(k): _substitute_layout_placeholders(str(v), layout)
            for k, v in command.env.items()
        }
        unresolved = _placeholder_problems(cwd_value, env_overrides)
        if unresolved:
            clean = False
            all_findings.append(
                _env_finding(
                    "oracle_unsubstituted_placeholder",
                    f"command {command_id!r} has unsubstituted layout placeholder(s) in "
                    f"{unresolved}; refusing to spawn in an unresolved path (a literal "
                    f"'{{...}}' cwd/env is the v2.75 cwd-bug class — RED, not a benign miss)",
                )
            )
            raw_parts.append(f"## {command_id}: NOT SPAWNED (unsubstituted placeholder in {unresolved})")
            continue

        # (4) Resolve the run directory (project_root / cwd) and spawn shell=False.
        run_cwd = (ctx.project_root / cwd_value) if cwd_value else ctx.project_root
        run_env = dict(os.environ)
        run_env.update(env_overrides)
        argv = list(command.argv)
        command_str = " ".join(argv)

        if not argv:
            clean = False
            all_findings.append(
                _env_finding(
                    "oracle_command_empty_argv",
                    f"command {command_id!r} has an empty argv — nothing to execute",
                )
            )
            raw_parts.append(f"## {command_id}: EMPTY ARGV")
            continue

        try:
            completed = subprocess.run(  # noqa: S603 — trusted argv, shell=False
                argv,
                shell=False,
                cwd=str(run_cwd),
                env=run_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            clean = False
            all_findings.append(
                _env_finding(
                    f"oracle_{command_id}_timeout",
                    f"`{command_str}` exceeded {timeout:g}s",
                )
            )
            raw_parts.append(f"## {command_id}: TIMEOUT after {timeout:g}s")
            continue
        except (FileNotFoundError, OSError, ValueError) as exc:
            clean = False
            all_findings.append(
                _env_finding(
                    f"oracle_{command_id}_spawn_error",
                    f"could not run `{command_str}` (is the toolchain on PATH?): {exc}",
                )
            )
            raw_parts.append(f"## {command_id}: SPAWN FAILURE ({exc})")
            continue

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = "\n".join(part for part in (stdout, stderr) if part)
        raw_parts.append(
            f"## {command_id} (exit={completed.returncode})\n{combined or '(no output)'}"
        )

        # (5) Hand the raw result to the adapter for a language-neutral verdict.
        observation = adapter.normalize_command_result(
            ctx,
            command_id=command_id,
            command=command,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        all_findings.extend(observation.findings)
        for path in observation.failed_paths:
            if path not in all_failed_paths:
                all_failed_paths.append(path)
        all_diagnostics.extend(observation.diagnostics)

        if observation.is_clean:
            continue

        clean = False
        # (6) A NON-clean step with NO findings is an OPAQUE failure: the command did
        # not pass but the adapter could not say why. Synthesize an honest
        # environment_build_error so "nonzero exit, no parsed diagnostic" is RED, never
        # a benign pass (the canonical false-green this gate exists to kill).
        if not observation.findings:
            all_findings.append(
                _env_finding(
                    f"environment_build_error_{command_id}",
                    observation.detail
                    or (
                        f"`{command_str}` exited {completed.returncode} and the oracle "
                        f"adapter found no diagnostic to explain it — treating the step as "
                        f"an opaque environment/build error (not a benign pass)"
                    ),
                )
            )

    passed = clean
    detail = (
        f"generic oracle sequence {'passed' if passed else 'failed'} "
        f"({len(command_ids)} step(s), {len(all_findings)} finding(s))"
    )
    return ImplementOracleResult(
        passed=passed,
        executed=True,
        command=label,
        findings=all_findings,
        failed_paths=all_failed_paths,
        detail=detail,
        raw_output="\n\n".join(raw_parts),
        diagnostics=all_diagnostics,
    )


def _placeholder_problems(cwd_value: str | None, env_overrides: Mapping[str, str]) -> list[str]:
    """Return human labels for any cwd/env value still carrying a ``{placeholder}``.

    Empty list ⇒ everything substituted (safe to spawn). A non-empty list means the
    layout did not resolve a template the command needs — the caller HARD-FAILS
    rather than spawning in a literal ``{...}`` path.
    """
    problems: list[str] = []
    if _has_unsubstituted_placeholder(cwd_value):
        problems.append(f"cwd={cwd_value!r}")
    for key, value in env_overrides.items():
        if _has_unsubstituted_placeholder(value):
            problems.append(f"env[{key}]={value!r}")
    return problems


__all__ = [
    "DEFAULT_ORACLE_TIMEOUT_SECONDS",
    "run_command_sequence",
]

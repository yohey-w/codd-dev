"""CoDD CLI — codd init / scan / impact / require / plan."""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import yaml

from codd.action_outcome import (
    ActionRequirement,
    CoverageResult,
    ActionTargetSpec,
    action_target_specs_from_config,
    compare_action_outcome_coverage,
    canonical_action_verb,
    extract_action_requirements_from_flows,
)
from codd.bridge import get_command_handler
from codd.capability_completeness import (
    capability_completeness_warnings,
    enablement_declaration_nudges,
)
from codd.config import find_codd_dir, load_project_config
from codd.frontmatter import frontmatter_or_yaml_payload as _frontmatter_or_yaml_payload
from codd.requirement_reconciliation import (
    discover_requirement_docs,
    requirement_reconciliation_warnings,
)
from codd.surface_reconciliation import (
    iter_markup_source_texts,
    surface_reconciliation_warnings,
)
from codd.lexicon import LEXICON_FILENAME, load_lexicon, load_project_extends
from codd.skills_cli import manager as skills_manager

TEMPLATES_DIR = Path(__file__).parent / "templates"


def project_root_option(param: str = "path", **overrides):
    """Project-root option accepted as both ``--path`` and ``--project-path``.

    ``param`` preserves the parameter name each command function already
    expects (``path`` or ``project_path``), so call sites migrate without
    changing their signatures. Keyword ``overrides`` (e.g. ``default=None``)
    take precedence over the shared defaults.
    """
    kwargs: dict[str, Any] = {
        "default": ".",
        "show_default": True,
        "help": "Project root directory",
    }
    kwargs.update(overrides)
    return click.option("--path", "--project-path", param, **kwargs)


def _resolve_output_format(output_format: str, as_json: bool, command: str) -> str:
    """Fold the deprecated ``--json`` flag into the standard ``--format``.

    Emits a one-line deprecation note on stderr when ``--json`` is used so
    existing scripts keep working while stdout stays machine-readable.
    """
    if as_json:
        click.echo(
            f"note: '--json' is deprecated; use '{command} --format json' instead.",
            err=True,
        )
        return "json"
    return output_format


class _AliasedGroup(click.Group):
    """Group whose deprecated legacy verbs resolve to canonical subcommands.

    RF6 unifies every HITL proposal flow on the canonical verb lifecycle
    ``derive → show → approve → apply``. Legacy verbs keep working as hidden
    aliases: they never appear in ``--help`` (only canonical names are
    listed in ``list_commands``), and using one emits a one-line stderr
    deprecation note while behaving identically to the canonical command.
    """

    def __init__(self, *args: Any, aliases: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: legacy verb -> canonical subcommand name
        self.command_aliases: dict[str, str] = dict(aliases or {})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        canonical = self.command_aliases.get(cmd_name)
        if canonical is not None:
            if not ctx.resilient_parsing:
                click.echo(
                    f"note: 'codd {self.name} {cmd_name}' is deprecated; "
                    f"use 'codd {self.name} {canonical}'.",
                    err=True,
                )
            cmd_name = canonical
        return super().get_command(ctx, cmd_name)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Return the canonical name so usage/error text never shows the alias.
        _name, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd is not None else None), cmd, rest


class CoddCLIError(RuntimeError):
    """Error raised for CLI-facing validation failures."""


@dataclass
class _CliVerificationResult:
    passed: bool
    exit_code: int
    failure: Any | None = None
    failures: list[Any] | None = None
    check_results: list[Any] | None = None
    runtime_results: list[Any] | None = None
    # FX3 execution evidence (mirrors repair.verify_runner.VerificationResult)
    tests_executed: bool = False
    test_command: str | None = None
    tests_summary: str = ""
    typecheck_executed: bool = False
    source_integrity: str = ""


@dataclass(frozen=True)
class _VersionCheckResult:
    installed_version: str
    required_spec: str
    satisfied: bool
    strict: bool
    message: str


@dataclass(frozen=True)
class _RuntimeOutcomeEntry:
    section: str
    name: str
    action_id: str | None
    verb: str | None
    target: str | None
    actors: tuple[str, ...]
    text: str
    covered_by_refs: tuple[str, ...]


_SPECIFIER_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>|~=)\s*([A-Za-z0-9.!+_-]+)\s*$")


def _installed_codd_version() -> str:
    try:
        return importlib.metadata.version("codd-dev")
    except importlib.metadata.PackageNotFoundError:
        pass

    try:
        from codd import __version__

        if __version__:
            return str(__version__)
    except Exception:  # pragma: no cover - best-effort fallback for source trees.
        pass

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_path.read_text(encoding="utf-8"))
    except OSError:
        match = None
    return match.group(1) if match else "unknown"


def _evaluate_version_requirement(project_root: Path, *, strict_override: bool = False) -> _VersionCheckResult | None:
    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return None

    required = config.get("codd_required_version")
    if not isinstance(required, str) or not required.strip():
        return None

    installed = _installed_codd_version()
    required_spec = required.strip()
    satisfied, error = _version_satisfies(installed, required_spec)
    strict = bool(strict_override or config.get("codd_required_version_strict", False))
    if error:
        return _VersionCheckResult(
            installed_version=installed,
            required_spec=required_spec,
            satisfied=False,
            strict=strict,
            message=f"WARN: invalid codd_required_version {required_spec!r}: {error}",
        )
    return _VersionCheckResult(
        installed_version=installed,
        required_spec=required_spec,
        satisfied=satisfied,
        strict=strict,
        message=f"WARN: project requires codd {required_spec}, installed {installed}",
    )


def _warn_if_project_version_mismatch(project_root: Path) -> None:
    result = _evaluate_version_requirement(project_root)
    if result is None or result.satisfied:
        return
    click.echo(result.message, err=True)
    if result.strict:
        raise SystemExit(1)


def _version_satisfies(installed: str, specifier: str) -> tuple[bool, str | None]:
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version

        try:
            return Version(installed) in SpecifierSet(specifier), None
        except (InvalidSpecifier, InvalidVersion) as exc:
            return False, str(exc)
    except ImportError:
        return _version_satisfies_fallback(installed, specifier)


def _version_satisfies_fallback(installed: str, specifier: str) -> tuple[bool, str | None]:
    installed_key = _version_key(installed)
    if installed_key is None:
        return False, f"unsupported installed version {installed!r}"

    for raw_part in specifier.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = _SPECIFIER_RE.match(part)
        if match is None:
            return False, f"unsupported version specifier {part!r}; install packaging for full PEP 440 support"
        op, expected = match.groups()
        expected_key = _version_key(expected)
        if expected_key is None:
            return False, f"unsupported version {expected!r}"
        if not _compare_version_keys(installed_key, op, expected_key):
            return False, None
    return True, None


def _version_key(version: str) -> tuple[int, ...] | None:
    release = version.split("+", 1)[0].split("-", 1)[0]
    numbers = re.findall(r"\d+", release)
    if not numbers:
        return None
    return tuple(int(item) for item in numbers)


def _compare_version_keys(installed: tuple[int, ...], op: str, expected: tuple[int, ...]) -> bool:
    size = max(len(installed), len(expected))
    left = installed + (0,) * (size - len(installed))
    right = expected + (0,) * (size - len(expected))
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == "~=":
        upper = (expected[0] + 1, 0) if len(expected) <= 2 else (expected[0], expected[1] + 1, 0)
        upper = upper + (0,) * (size - len(upper))
        return left >= right and left < upper
    return False


def _require_codd_dir(project_root: Path) -> Path:
    """Return the CoDD config dir or exit with a helpful message."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        click.echo("Error: CoDD config dir not found (looked for codd/ and .codd/). Run 'codd init' first.")
        raise SystemExit(1)
    return codd_dir


def _load_coherence_context(project_root: Path) -> dict[str, str]:
    """Load optional lexicon and DESIGN.md context for coherence-aware prompts."""
    config = load_project_config(project_root)
    coherence_config = config.get("coherence", {})
    if not isinstance(coherence_config, dict):
        coherence_config = {}

    lexicon_path = _config_path(
        config.get("lexicon_path", coherence_config.get("lexicon_path", coherence_config.get("lexicon")))
    )
    design_md_path = _config_path(
        config.get(
            "design_md",
            config.get("design_md_path", coherence_config.get("design_md", coherence_config.get("design_md_path"))),
        )
    )

    context: dict[str, str] = {}
    lexicon_text = _read_optional_context_file(project_root, lexicon_path or "project_lexicon.yaml")
    if lexicon_text:
        context["lexicon"] = lexicon_text

    design_text = _read_optional_context_file(project_root, design_md_path or "DESIGN.md")
    if design_text:
        context["design_md"] = design_text

    return context


def _config_path(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        for key in ("path", "file", "source"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return None


def _read_optional_context_file(project_root: Path, path_text: str) -> str | None:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = project_root / path
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _screen_flow_strict_edges(config: dict) -> bool:
    screen_flow_config = config.get("screen_flow", {})
    if not isinstance(screen_flow_config, dict):
        return True
    return bool(screen_flow_config.get("strict_edges", True))


def _run_design_md_lint(project_root: Path) -> None:
    """Run the external DESIGN.md linter when the local toolchain supports it."""
    design_md_path = project_root / "DESIGN.md"
    if not design_md_path.exists():
        click.echo("WARNING: DESIGN.md not found. Skipping lint.")
        return
    if not shutil.which("npx"):
        click.echo("WARNING: npx not available. Skipping @google/design.md lint.")
        return

    import subprocess

    try:
        result = subprocess.run(
            ["npx", "--yes", "@google/design.md", "lint", str(design_md_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        click.echo("DESIGN.md lint: FAIL\nnpx @google/design.md lint timed out after 60 seconds.")
        raise SystemExit(1)

    if result.returncode == 0:
        click.echo("DESIGN.md lint: PASS")
        return

    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    click.echo(f"DESIGN.md lint: FAIL\n{output}")
    raise SystemExit(1)


def _resolve_bootstrap_codd_dir(project_root: Path) -> Path:
    """Choose a config dir for brownfield bootstrap without clobbering source code."""
    existing = find_codd_dir(project_root)
    if existing is not None:
        return existing

    hidden_dir = project_root / ".codd"
    if hidden_dir.exists():
        return hidden_dir
    return hidden_dir


def _format_yaml_list(items: list[str], *, indent: int = 4) -> str:
    """Render a YAML list block for the simple template engine."""
    if not items:
        return " " * indent + "[]"
    return "\n".join(f'{" " * indent}- "{item}"' for item in items)


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _ensure_bootstrap_codd_yaml(
    project_root: Path,
    *,
    codd_dir: Path | None = None,
    language: str,
    source_dirs: list[str],
) -> tuple[Path, bool]:
    """Create a minimal codd.yaml after brownfield extract when none exists."""
    codd_dir = codd_dir or _resolve_bootstrap_codd_dir(project_root)
    config_path = codd_dir / "codd.yaml"
    if config_path.exists():
        return config_path, False

    codd_dir.mkdir(parents=True, exist_ok=True)
    codd_dir_name = _display_path(codd_dir, project_root)
    _render_template(
        "codd.yaml.tmpl",
        config_path,
        {
            "project_name": project_root.name,
            "language": language,
            "source_dirs": _format_yaml_list(source_dirs),
            "graph_path": f"{codd_dir_name}/scan",
        },
    )
    _append_brownfield_bootstrap_todos(config_path)
    return config_path, True


# Guided TODO stubs appended to a freshly-bootstrapped brownfield codd.yaml.
# These are commented placeholders the user fills in after `codd extract`; they
# stay generic (no project-specific names) and never affect parsing until edited.
_BROWNFIELD_BOOTSTRAP_TODOS = """
# ─────────────────────────────────────────────────────────────
# Brownfield bootstrap TODOs (generated by `codd extract`)
# Fill these in after reviewing the restored docs under .codd/extract/.
# All lines are commented stubs — uncomment and edit what your project needs.
# ─────────────────────────────────────────────────────────────

# TODO: operation_flow — declare the actor/action/state/outcome model recovered
#       from the codebase so restore/plan/verify share one source of truth.
# operation_flow:
#   operations:
#     - id: "TODO_operation_id"
#       actor: "TODO_role"
#       verb: "TODO_verb"          # e.g. create | update | delete | approve | publish
#       target: "TODO_entity"
#       trigger: "TODO_public_trigger"   # user action, API request, timer, event
#       expected_outcomes: []      # e.g. persisted_change, visible_reflection

# TODO: scan dirs — confirm the source/test/doc directories CoDD should scan.
#       (extract pre-filled scan.source_dirs above; verify and extend as needed.)
# scan:
#   source_dirs:
#     - "TODO_add_or_confirm_source_dir"
#   test_dirs:
#     - "TODO_add_or_confirm_test_dir"
#   doc_dirs:
#     - "docs/"

# TODO: dag.enabled_checks — pin the dependency-graph checks to enforce.
#       If you pin this list, remember to keep `dependency_freshness` included.
# dag:
#   enabled_checks:
#     - node_completeness
#     - dependency_freshness
"""


def _append_brownfield_bootstrap_todos(config_path: Path) -> None:
    """Append commented brownfield TODO stubs to a bootstrap codd.yaml."""
    existing = config_path.read_text(encoding="utf-8")
    if "Brownfield bootstrap TODOs" in existing:
        return
    separator = "" if existing.endswith("\n") else "\n"
    config_path.write_text(existing + separator + _BROWNFIELD_BOOTSTRAP_TODOS, encoding="utf-8")


@click.group(epilog="Health: codd check (start here). Drill down with doctor, dag verify, and contract verify.")
@click.version_option(package_name="codd-dev")
@click.pass_context
def main(ctx: click.Context):
    """CoDD: Coherence-Driven Development."""
    if ctx.resilient_parsing or ctx.invoked_subcommand in {None, "version"}:
        return
    _warn_if_project_version_mismatch(Path.cwd())


@main.command("version")
@click.option("--check", "check_project", is_flag=True, help="Check installed CoDD against codd.yaml requirement")
@click.option("--strict", is_flag=True, help="Exit non-zero when the version requirement is not satisfied")
@project_root_option("project_path")
def version_cmd(check_project: bool, strict: bool, project_path: str) -> None:
    """Print the installed CoDD version."""
    installed = _installed_codd_version()
    click.echo(f"codd {installed}")
    if not check_project:
        return

    project_root = Path(project_path).resolve()
    result = _evaluate_version_requirement(project_root, strict_override=strict)
    if result is None:
        click.echo("Version check: no codd_required_version configured")
        return
    if result.satisfied:
        click.echo(f"Version check: PASS (requires {result.required_spec})")
        return

    click.echo(result.message, err=True)
    click.echo(f"Version check: FAIL (requires {result.required_spec})")
    if result.strict:
        raise SystemExit(1)


@main.command("preflight")
@click.argument("task_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@project_root_option("project_path")
@click.option("--strict", is_flag=True, help="Treat high severity as halt-worthy")
@click.option("--ntfy-topic", default="", help="ntfy topic for critical alerts")
@click.option(
    "--ntfy-severity-threshold",
    default=None,
    help="Minimum severity sent to ntfy (default: codd.yaml preflight.ntfy_severity_threshold or critical)",
)
def preflight(task_yaml: Path, project_path: str, strict: bool, ntfy_topic: str, ntfy_severity_threshold: str | None):
    """Run preflight checks on a task YAML before autonomous execution."""
    _run_preflight_command(task_yaml, project_path, strict, ntfy_topic, ntfy_severity_threshold)


@main.command("doctor")
@project_root_option("project_path")
def doctor(project_path: str) -> None:
    """Run project-level configuration diagnostics."""
    project_root = Path(project_path).resolve()
    try:
        warnings = _doctor_warnings(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"CoDD doctor: FAIL ({exc})")
        raise SystemExit(2)

    if not warnings:
        click.echo("CoDD doctor: PASS")
        return
    click.echo("CoDD doctor: WARN")
    for warning in warnings:
        click.echo(f"WARNING: {warning}")


@main.command("gungi")
@click.argument("task_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@project_root_option("project_path")
@click.option("--strict", is_flag=True, help="Treat high severity as halt-worthy")
@click.option("--ntfy-topic", default="", help="ntfy topic for critical alerts")
@click.option(
    "--ntfy-severity-threshold",
    default=None,
    help="Minimum severity sent to ntfy (default: codd.yaml preflight.ntfy_severity_threshold or critical)",
)
def gungi(task_yaml: Path, project_path: str, strict: bool, ntfy_topic: str, ntfy_severity_threshold: str | None):
    """Alias for preflight."""
    _run_preflight_command(task_yaml, project_path, strict, ntfy_topic, ntfy_severity_threshold)


def _run_preflight_command(
    task_yaml: Path,
    project_path: str,
    strict: bool,
    ntfy_topic: str,
    ntfy_severity_threshold: str | None,
) -> None:
    from codd.ask_user_question_adapter import _post_ntfy, _severity_at_or_above
    from codd.preflight import PreflightAuditor

    project_root = Path(project_path).resolve()
    auditor = PreflightAuditor(project_root=project_root)
    result = auditor.run(task_yaml)
    strict_halt = strict and result.severity == "high"
    if strict_halt:
        result.halt_recommended = True

    threshold = ntfy_severity_threshold or str(
        auditor.preflight_config.get("ntfy_severity_threshold") or "critical"
    )
    if ntfy_topic and _severity_at_or_above(result.severity, threshold):
        result.ntfy_sent = _post_ntfy(ntfy_topic, _format_preflight_ntfy(result))

    for check in result.checks:
        click.echo(f"[{check.status}] {check.name}: {check.message}")
        for detail in check.details:
            click.echo(f"  - {detail}")
    click.echo(f"Overall severity: {result.severity}")
    click.echo(f"ntfy_sent: {str(result.ntfy_sent).lower()}")

    if result.halt_recommended:
        reason = "strict high severity" if strict_halt else "critical issue found"
        click.echo(f"HALT recommended: {reason}")
        raise SystemExit(1)


@main.command("check", epilog="Task-YAML preflight stays separate: run 'codd preflight <task.yaml>'.")
@project_root_option("project_path")
@click.option("--full", "run_full", is_flag=True, default=False, help="Also run policy and coverage threshold gates.")
@click.option(
    "--fix",
    "apply_fixes",
    is_flag=True,
    default=False,
    help="Apply mechanical auto-repairs during the dag stage (same as 'codd dag verify --auto-repair --apply').",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format",
)
def check_cmd(project_path: str, run_full: bool, apply_fixes: bool, output_format: str) -> None:
    """Run the aggregated project health check (start here).

    Aggregates, in order: doctor diagnostics (advisory), dag verify
    (all registered checks per project config), and contract verify
    (no-op when the artifact contract is disabled). With --full it also
    runs the policy and coverage threshold gates when configured.

    Exit code is non-zero only when a gate fails (red dag check,
    enabled-contract failure, policy critical violation, or coverage
    threshold violation). Advisory findings (doctor warnings, amber dag
    checks) keep exit 0.
    """
    from codd.artifact_contract import CatalogError, load_catalog, load_contract, verify_contract
    from codd.dag.runner import run_all_checks, unselected_check_names

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)

    as_text = output_format == "text"
    payload: dict[str, Any] = {}
    notices: list[str] = []
    errors: dict[str, str] = {}
    gates_failed = 0
    advisories = 0

    def _section(title: str) -> None:
        if as_text:
            click.echo(f"== {title} ==")

    def _line(message: str) -> None:
        if as_text:
            click.echo(message)

    # --- doctor (advisory) -------------------------------------------------
    _section("doctor (advisory)")
    try:
        doctor_warnings = _doctor_warnings(project_root)
    except (FileNotFoundError, ValueError) as exc:
        doctor_warnings = []
        errors["doctor"] = str(exc)
        notices.append(f"doctor could not run: {exc}")
    payload["doctor"] = doctor_warnings
    advisories += len(doctor_warnings)
    if "doctor" in errors:
        _line(f"  ERROR — {errors['doctor']}")
    elif doctor_warnings:
        _line(f"  WARN — {len(doctor_warnings)} advisory finding(s)")
        for warning in doctor_warnings:
            _line(f"  WARNING: {warning}")
        _line("  Run 'codd doctor' for details.")
    else:
        _line("  PASS — no warnings")

    # --- vb declarations (gate) -------------------------------------------
    # Incoherent verifiable-behavior declarations (same id, different meaning in
    # two docs) make 100% coverage structurally impossible — fail loudly. The
    # WARNING-severity case (a non-canonical doc with first-column VB rows) is
    # already surfaced as a doctor advisory above with migration guidance.
    _section("vb declarations")
    try:
        vb_errors = _vb_declaration_issue_messages(project_root, load_project_config(project_root), severity="error")
    except (FileNotFoundError, ValueError) as exc:
        vb_errors = []
        errors["vb_declarations"] = str(exc)
        _line(f"  ERROR — {exc}")
    payload["vb_declarations"] = vb_errors
    if vb_errors:
        gates_failed += 1
        _line(f"  FAIL — {len(vb_errors)} colliding verifiable-behavior declaration(s)")
        for message in vb_errors:
            _line(f"  ERROR: {message}")
    elif "vb_declarations" not in errors:
        _line("  PASS — coherent VB declarations")

    # --- dag verify (gate) -------------------------------------------------
    _section("dag verify")
    dag_results: list[Any] = []
    try:
        dag_results = run_all_checks(project_root)
    except (FileNotFoundError, ValueError) as exc:
        errors["dag"] = str(exc)
        gates_failed += 1
        _line(f"  FAIL — {exc}")
    payload["dag"] = [_dag_result_to_dict(result) for result in dag_results]

    unselected: list[str] = []
    if dag_results:
        try:
            unselected = unselected_check_names(project_root)
        except Exception:  # visibility aid must never break the health check
            unselected = []
    if unselected:
        notices.append(
            f"{len(unselected)} registered dag check(s) not selected by enabled_checks: "
            + ", ".join(unselected)
        )

    failed_red = [
        result
        for result in dag_results
        if not _dag_result_passed(result)
        and _dag_result_severity(result) == "red"
        and _dag_result_status(result) != "opt_out"
    ]
    amber_findings = [
        result
        for result in dag_results
        if _dag_result_severity(result) == "amber" and _dag_result_has_findings(result)
    ]
    if failed_red:
        gates_failed += 1
    advisories += len(amber_findings)
    for result in dag_results:
        severity = _dag_result_severity(result)
        status_value = _dag_result_status(result)
        if status_value == "opt_out":
            status = "OPT_OUT"
        elif _dag_result_passed(result):
            status = "PASS"
        else:
            status = "WARN" if severity == "amber" else "FAIL"
        _line(f"  {status}  {_dag_result_name(result)} [{severity}]")
    if failed_red or amber_findings:
        _line("  Run 'codd dag verify' for details.")

    if apply_fixes and dag_results:
        from codd.dag.auto_repair import apply_auto_repair

        outcome = apply_auto_repair(project_root, dag_results, dry_run=False)
        payload["repairs"] = {
            "applied": [action.description for action in outcome.applied],
            "skipped": [action.description for action in outcome.skipped],
        }
        _line(f"  Applied {len(outcome.applied)} auto-repair(s):")
        for action in outcome.applied:
            _line(f"    - {action.description}")
        if outcome.skipped:
            _line(f"  Skipped {len(outcome.skipped)} non-repairable violation(s).")
        if outcome.applied:
            notices.append("auto-repairs applied; re-run 'codd check' to verify the repaired state.")

    # --- contract verify (gate, no-op when disabled) -----------------------
    _section("contract verify")
    contract_payload: dict[str, Any] = {"enabled": False, "status": "skipped"}
    try:
        config = load_project_config(project_root)
        contract = load_contract(config)
        if not contract.enabled:
            contract_payload = {
                "enabled": False,
                "status": "skipped",
                "reason": "artifact_contract is disabled (opt-in)",
            }
            _line("  skipped: artifact_contract is disabled (opt-in)")
        elif not contract.stages:
            contract_payload = {
                "enabled": True,
                "status": "skipped",
                "reason": "artifact_contract declares no stages",
            }
            _line("  skipped: artifact_contract is enabled but declares no stages")
        else:
            catalog = load_catalog()
            report = verify_contract(catalog, contract, project_root)
            contract_payload = {
                "enabled": True,
                "status": "fail" if report.has_failures else "pass",
                "failure_count": report.failure_count,
                "stages": [
                    {
                        "stage": stage_report.stage,
                        "passed": stage_report.passed,
                        "checks": [
                            {
                                "artifact_id": check.artifact_id,
                                "ok": check.ok,
                                "status": check.status,
                                "detail": check.detail,
                                "matched_paths": list(check.matched_paths),
                            }
                            for check in stage_report.checks
                        ],
                    }
                    for stage_report in report.stages
                ],
            }
            for stage_report in report.stages:
                verdict = "PASS" if stage_report.passed else "FAIL"
                _line(f"  {verdict}  stage {stage_report.stage}")
            if report.has_failures:
                gates_failed += 1
                _line(f"  {report.failure_count} required artifact(s) missing/invalid.")
                _line("  Run 'codd contract verify' for details.")
    except (CatalogError, FileNotFoundError, ValueError) as exc:
        errors["contract"] = str(exc)
        gates_failed += 1
        contract_payload = {"enabled": True, "status": "error", "error": str(exc)}
        _line(f"  FAIL — {exc}")
    payload["contract"] = contract_payload

    if run_full:
        # --- policy (gate, skipped when unconfigured) ----------------------
        _section("policy")
        try:
            full_config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            full_config = {}
        if not full_config.get("policies"):
            payload["policy"] = {"status": "skipped", "reason": "no policies configured in codd.yaml"}
            _line("  skipped: no policies configured in codd.yaml")
        else:
            from codd.policy import run_policy

            try:
                policy_result = run_policy(project_root)
                payload["policy"] = {
                    "status": "pass" if policy_result.pass_ else "fail",
                    "critical": policy_result.critical_count,
                    "warnings": policy_result.warning_count,
                }
                advisories += policy_result.warning_count
                if policy_result.pass_:
                    _line(f"  PASS — critical: 0, warnings: {policy_result.warning_count}")
                else:
                    gates_failed += 1
                    _line(
                        f"  FAIL — critical: {policy_result.critical_count}, "
                        f"warnings: {policy_result.warning_count}"
                    )
                    _line("  Run 'codd policy' for details.")
            except (FileNotFoundError, ValueError) as exc:
                errors["policy"] = str(exc)
                gates_failed += 1
                payload["policy"] = {"status": "error", "error": str(exc)}
                _line(f"  FAIL — {exc}")

        # --- coverage check (gate, skipped when unconfigured) --------------
        _section("coverage check")
        coverage_config = full_config.get("coverage") if isinstance(full_config.get("coverage"), dict) else {}
        if not (coverage_config or {}).get("thresholds"):
            payload["coverage"] = {
                "status": "skipped",
                "reason": "no coverage.thresholds configured in codd.yaml",
            }
            _line("  skipped: no coverage.thresholds configured in codd.yaml")
        else:
            from codd.lexicon_cli.reporter import CoverageReporter
            from codd.lexicon_cli.threshold import evaluate, load_thresholds

            try:
                coverage_report = CoverageReporter(project_root).build("all")
                threshold_config = load_thresholds(_default_threshold_path(project_root))
                violations = evaluate(coverage_report, threshold_config)
                payload["coverage"] = {
                    "status": "fail" if violations else "pass",
                    "totals": coverage_report.totals,
                    "violations": [asdict(violation) for violation in violations],
                }
                if violations:
                    gates_failed += 1
                    _line(f"  FAIL — {len(violations)} threshold violation(s)")
                    _line("  Run 'codd coverage check' for details.")
                else:
                    _line("  PASS — all coverage thresholds met")
            except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
                errors["coverage"] = str(exc)
                gates_failed += 1
                payload["coverage"] = {"status": "error", "error": str(exc)}
                _line(f"  FAIL — {exc}")

    if errors:
        payload["errors"] = errors
    payload["summary"] = {"gates_failed": gates_failed, "advisories": advisories}

    if as_text:
        click.echo(f"\nSummary: {gates_failed} gate(s) failed, {advisories} advisory finding(s)")
    else:
        click.echo(json.dumps(payload, indent=2, default=str))
    for notice in notices:
        click.echo(f"note: {notice}", err=True)

    raise SystemExit(1 if gates_failed else 0)


_MUTATING_ENDPOINT_RE = re.compile(
    r"(?i)(export\s+async\s+function\s+(?:POST|PUT|PATCH|DELETE)\b|"
    r"@[\w.]+\.(?:post|put|patch|delete)\(|\b[\w.]+\.(?:post|put|patch|delete)\(|"
    r"\b(?:app|router)\.(?:post|put|patch|delete)\(|"
    r"\bdo_(?:POST|PUT|PATCH|DELETE)\b|method\s*[:=]\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]|"
    r"['\"](?:POST|PUT|PATCH|DELETE)['\"])",
)
_REFLECTION_E2E_RE = re.compile(
    r"(?i)(reflect|reflection|refetch|re-fetch|reload|list|detail|visible|getByText|findByText|"
    r"toBeVisible|toContainText|toHaveText|toHaveCount|toContain\()",
)
_SYNTHETIC_MUTATION_RE = re.compile(
    r"(?is)(crypto\.randomUUID\s*\(|randomUUID\s*\(|uuidv4\s*\(|\buuid\s*\(|Math\.random\s*\(|"
    r"\bfaker\.|\bfake\b|\bdummy\b|\bstub\b|\bmock\b)"
)
_PERSISTENCE_EVIDENCE_RE = re.compile(
    r"(?i)(\bprisma\b|\btypeorm\b|\bsequelize\b|\bknex\b|\bsql\b|\bquery\b|\bexecute\b|"
    r"\brepository\b|\btransaction\b|\bcommit\b|\brollback\b|\bcollection\b|\bdocument\b|"
    r"\bmodel\b|\bdb\.|\bstore\.|\bdao\.|\bmapper\.|"
    r"\.(?:create|insert|update|delete|save|upsert|patch)\s*\()"
)
_BUTTON_RE = re.compile(r"(?is)<button\b(?P<attrs>[^>]*)>(?P<label>.*?)</button>")
_SCREEN_FILE_RE = re.compile(r"(?i)(^|[/\\])(page|screen|view|route)\.(tsx|jsx|html|vue|svelte)$")
_SCREEN_CONTENT_RE = re.compile(r"(?is)(<h1\b|<main\b|<section\b|<article\b|<Card\b|role\s*=\s*['\"]main['\"])")
_ESCAPE_ROUTE_RE = re.compile(
    r"(?is)(<nav\b|role\s*=\s*['\"]navigation['\"]|breadcrumb|<Link\b|href\s*=|router\.push\s*\(|"
    r"\bnavigate\s*\(|\bredirect\s*\(|to\s*=|aria-label\s*=\s*['\"][^'\"]*(?:nav|menu|breadcrumb|home|dashboard))"
)
_AUTHENTICATED_UI_RE = re.compile(
    r"(?i)(requireAuthenticatedSession|getGuardContext|getServerSession|useSession|authOptions|session\.user|authenticated)"
)
_RESPONSIVE_BREAKPOINT_RE = re.compile(
    r"(?is)(className\s*=\s*['\"][^'\"]*(?:(?:hidden[^'\"]*(?:sm:|md:|lg:|xl:|2xl:))|"
    r"(?:(?:sm|md|lg|xl|2xl):hidden))[^'\"]*|@media\s*\(|viewport|breakpoint|responsive)"
)
_SESSION_ACTION_TOKEN_RE = re.compile(
    r"(?i)(sign\s*out|signout|log\s*out|logout|account|profile|session|user\s*menu|settings)"
)
_CONNECTED_CONTROL_RE = re.compile(
    r"(?i)(onClick\s*=|onclick\s*=|@click\s*=|\(click\)\s*=|x-on:click\s*=|wire:click\s*=|"
    r"on:click\s*=|formAction\s*=|formaction\s*=|href\s*=|data-action\s*=|"
    r"hx-(?:post|put|patch|delete)\s*=|type\s*=\s*['\"]submit['\"]|"
    r"type\s*=\s*\{['\"]submit['\"]\})"
)
_DISABLED_CONTROL_RE = re.compile(r"(?i)(\bdisabled\b|aria-disabled\s*=\s*['\"]true['\"])")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_STRONG_OUTCOME_NAMES = {
    "visible_reflection",
    "reload_persistence",
    "persisted_change",
    "persisted_absence",
    "expected_absence",
    "absence",
    "readback",
    "db_readback",
    "state_change",
    "state_reflection",
    "emitted_event",
    "event",
    "notification",
    "file_written",
    "side_effect",
    "persisted",
    "persistence",
    "stored",
    "audit_log",
    "disabled_state",
    "control_disabled",
    "control_absence",
    "terminal_state",
    "terminal_state_guard",
    "non_repeatable_guard",
}
_TERMINAL_ACTION_VERBS = {"complete", "delete", "disable", "archive", "revoke"}
_TERMINAL_OUTCOME_NAMES = {
    "disabled_state",
    "control_disabled",
    "control_absence",
    "expected_absence",
    "persisted_absence",
    "absence",
    "terminal_state",
    "terminal_state_guard",
    "non_repeatable_guard",
}
_GENERATED_E2E_PLACEHOLDER_RE = re.compile(
    r"TODO:\s*Add assertions based on acceptance criteria|Generated CoDD E2E requires concrete assertions",
    re.IGNORECASE,
)
_OUTCOME_TOKEN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "can",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "of",
    "on",
    "only",
    "or",
    "selected",
    "shows",
    "the",
    "to",
    "with",
}
_TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".feature", ".html", ".vue", ".svelte"}
_DOC_SUFFIXES = {".md", ".yaml", ".yml"}


def _doctor_warnings(project_root: Path) -> list[str]:
    from codd.config_schema import project_config_key_warnings

    config = load_project_config(project_root)
    warnings: list[str] = []
    # Config-key typo guard (advisory): unknown codd.yaml keys are silently
    # ignored by the loader, so surface them here with did-you-mean hints.
    warnings.extend(project_config_key_warnings(project_root))
    has_crud_flow_targets = _has_crud_flow_targets(config)
    has_action_outcome_targets = _has_action_outcome_targets(config)
    if (
        _has_mutating_source(project_root, config)
        and not has_crud_flow_targets
        and not has_action_outcome_targets
        and not _has_reflection_e2e(project_root, config)
    ):
        warnings.append(
            "Mutating endpoint or handler detected, but no `runtime.crud_flow_targets`, no "
            "`runtime.action_outcome_targets`, and no reflection-oriented E2E test were found. Add "
            "an opt-in CRUD/action outcome target or an E2E that proves trigger -> outcome reflection, "
            "including visible list/detail reflection when the action changes user-visible state."
        )
    warnings.extend(_synthetic_mutation_warnings(project_root, config))
    warnings.extend(_interactive_control_warnings(project_root, config))
    warnings.extend(_undeclared_surface_warnings(project_root, config))
    warnings.extend(_screen_escape_route_warnings(project_root, config))
    warnings.extend(_authenticated_global_action_warnings(project_root, config))
    warnings.extend(_presentation_obligation_warnings(project_root, config))
    coverage = _action_outcome_coverage(project_root, config)
    warnings.extend(_action_outcome_warning_messages(coverage, legacy_crud_configured=has_crud_flow_targets))
    warnings.extend(_operation_outcome_projection_warnings(project_root, config, coverage.requirements))
    operation_flows = _operation_flows_from_project(project_root, config)
    warnings.extend(capability_completeness_warnings(operation_flows, config))
    warnings.extend(enablement_declaration_nudges(operation_flows, config))
    warnings.extend(_orphan_cover_marker_warnings(project_root, config))
    warnings.extend(_requirement_reconciliation_doctor_warnings(project_root, config, operation_flows))
    warnings.extend(_runtime_evidence_placeholder_warnings(project_root, config))
    target_actions = action_target_specs_from_config(config)
    warnings.extend(_weak_action_outcome_warning_messages(target_actions))
    warnings.extend(_terminal_action_outcome_warning_messages(target_actions))
    warnings.extend(_vb_declaration_issue_messages(project_root, config, severity="warning"))
    return warnings


def _vb_declaration_issue_messages(
    project_root: Path,
    config: dict[str, Any],
    *,
    severity: str,
) -> list[str]:
    """VB-declaration coherence diagnostics at a given severity.

    Detects colliding / multi-source verifiable-behavior declarations across
    test docs (same id, different meaning → ERROR; a non-canonical doc with a
    first-column ``VB-*`` table → WARNING with migration guidance for existing
    brownfield projects). VALIDATION only — coverage semantics are untouched.
    Returns the formatted messages for the requested severity.
    """

    from codd.verifiable_behavior_audit import (
        parse_vb_tables_by_doc,
        validate_vb_declarations,
    )

    try:
        behaviors_by_doc = parse_vb_tables_by_doc(project_root, config=config)
    except (OSError, ValueError):
        return []
    issues = validate_vb_declarations(behaviors_by_doc, strict=False)
    return [issue.message for issue in issues if issue.severity == severity]


def _has_crud_flow_targets(config: dict[str, Any]) -> bool:
    runtime = config.get("runtime", {})
    runtime_smoke = config.get("runtime_smoke", {})
    runtime_targets = runtime.get("crud_flow_targets") if isinstance(runtime, dict) else None
    smoke_targets = runtime_smoke.get("crud_flow_targets") if isinstance(runtime_smoke, dict) else None
    return bool(runtime_targets or smoke_targets)


def _has_action_outcome_targets(config: dict[str, Any]) -> bool:
    runtime = config.get("runtime", {})
    runtime_targets = runtime.get("action_outcome_targets") if isinstance(runtime, dict) else None
    return bool(runtime_targets)


def _has_global_action_targets(config: dict[str, Any]) -> bool:
    runtime = config.get("runtime", {})
    runtime_targets = runtime.get("global_action_targets") if isinstance(runtime, dict) else None
    return bool(runtime_targets)


def _action_outcome_coverage(project_root: Path, config: dict[str, Any]) -> CoverageResult:
    requirements = extract_action_requirements_from_flows(_operation_flows_from_project(project_root, config))
    target_actions = action_target_specs_from_config(config)
    return compare_action_outcome_coverage(requirements, target_actions)


def _operation_flows_from_project(project_root: Path, config: dict[str, Any]) -> list[tuple[str, Any]]:
    flows: list[tuple[str, Any]] = []
    if isinstance(config.get("operation_flow"), dict):
        flows.append(("codd.yaml.operation_flow", config["operation_flow"]))
    for path in _configured_doc_files(project_root, config):
        payload = _frontmatter_or_yaml_payload(path)
        if not isinstance(payload, dict):
            continue
        source = _display_path(path, project_root)
        if isinstance(payload.get("operation_flow"), dict):
            flows.append((f"{source}.operation_flow", payload["operation_flow"]))
        codd_meta = payload.get("codd")
        if isinstance(codd_meta, dict) and isinstance(codd_meta.get("operation_flow"), dict):
            flows.append((f"{source}.codd.operation_flow", codd_meta["operation_flow"]))
    return flows


def _configured_doc_files(project_root: Path, config: dict[str, Any]) -> list[Path]:
    scan = config.get("scan", {})
    raw_dirs = scan.get("doc_dirs", ["docs/"]) if isinstance(scan, dict) else ["docs/"]
    dirs = raw_dirs if isinstance(raw_dirs, list) else ["docs/"]
    files: list[Path] = []
    for raw_dir in dirs:
        if not isinstance(raw_dir, str) or not raw_dir.strip():
            continue
        root = Path(raw_dir).expanduser()
        if not root.is_absolute():
            root = project_root / root
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix in _DOC_SUFFIXES:
                files.append(root)
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in _DOC_SUFFIXES:
                files.append(path)
    return files


def _action_outcome_warning_messages(
    coverage: CoverageResult,
    *,
    legacy_crud_configured: bool,
) -> list[str]:
    warnings: list[str] = []
    for gap in coverage.gaps:
        requirement = gap.requirement
        missing = ", ".join(gap.missing_verbs)
        legacy_note = (
            " Existing `runtime.crud_flow_targets` are legacy reflection evidence and do not cover "
            "operation_flow update/delete/command action coverage."
            if legacy_crud_configured
            else ""
        )
        if requirement.ambiguous:
            warnings.append(
                f"`operation_flow` action `{requirement.display_name}` in {requirement.source} uses broad verb "
                f"`{requirement.verb}`; declare `runtime.action_outcome_targets` for {missing} observable outcomes."
                f"{legacy_note}"
            )
            continue
        warnings.append(
            f"`operation_flow` action `{requirement.display_name}` in {requirement.source} requires {missing} "
            f"observable outcome coverage, but no matching `runtime.action_outcome_targets` action was found."
            f"{legacy_note}"
        )
    return warnings


def _operation_outcome_projection_warnings(
    project_root: Path,
    config: dict[str, Any],
    requirements: tuple[ActionRequirement, ...],
) -> list[str]:
    entries = _runtime_outcome_entries(config)
    warnings: list[str] = []
    emitted: set[tuple[str, str, str]] = set()
    for requirement in requirements:
        if not requirement.expected_outcomes:
            continue
        matching_entries = [
            entry for entry in entries if _runtime_entry_matches_requirement(entry, requirement)
        ]
        if not matching_entries:
            continue
        for outcome in requirement.expected_outcomes:
            signature = _outcome_signature(outcome)
            if not signature:
                continue
            if any(_runtime_entry_represents_outcome(entry, outcome, signature) for entry in matching_entries):
                continue
            key = (requirement.source, requirement.operation_id, outcome)
            if key in emitted:
                continue
            emitted.add(key)
            warnings.append(
                f"`operation_flow` action `{requirement.display_name}` in {requirement.source} "
                f"declares expected outcome `{outcome}`, but matching runtime evidence metadata does "
                "not name that observable outcome. Add explicit `assertions`/`outcomes` metadata or a "
                "`covered_by` ref that names the design outcome being asserted."
            )
    return warnings


def _runtime_evidence_placeholder_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    emitted: set[tuple[str, str]] = set()
    for entry in _runtime_outcome_entries(config):
        for ref in entry.covered_by_refs:
            path = _evidence_ref_path(project_root, ref)
            if path is None:
                continue
            text = _read_text_best_effort(path)
            if not text or not _GENERATED_E2E_PLACEHOLDER_RE.search(text):
                continue
            key = (entry.name, ref)
            if key in emitted:
                continue
            emitted.add(key)
            warnings.append(
                f"`{entry.section}` runtime evidence `{ref}` for `{entry.name}` still contains a "
                "generated assertion placeholder. Replace it with concrete acceptance assertions before "
                "using it as coverage evidence."
            )
    return warnings


def _runtime_outcome_entries(config: dict[str, Any]) -> list[_RuntimeOutcomeEntry]:
    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        return []

    entries: list[_RuntimeOutcomeEntry] = []
    for section in (
        "role_sequence_targets",
        "action_outcome_targets",
        "global_action_targets",
        "crud_flow_targets",
    ):
        raw_targets = runtime.get(section)
        if not isinstance(raw_targets, list):
            continue
        for target_index, raw_target in enumerate(raw_targets, start=1):
            if not isinstance(raw_target, dict):
                continue
            name = str(raw_target.get("name") or f"{section}[{target_index}]")
            covered_by_refs = _covered_by_refs(raw_target)
            raw_actions = raw_target.get("actions")
            if raw_actions is None and raw_target.get("action") is not None:
                raw_actions = [raw_target.get("action")]
            if isinstance(raw_actions, list):
                for action_index, raw_action in enumerate(raw_actions, start=1):
                    if not isinstance(raw_action, dict):
                        continue
                    action_id = str(raw_action.get("id") or raw_action.get("name") or f"action[{action_index}]")
                    entries.append(
                        _RuntimeOutcomeEntry(
                            section=section,
                            name=name,
                            action_id=action_id,
                            verb=canonical_action_verb(raw_action.get("verb"))
                            or canonical_action_verb(action_id),
                            target=_optional_runtime_text(raw_action.get("target"))
                            or _optional_runtime_text(raw_target.get("target")),
                            actors=_runtime_actor_values(raw_action, raw_target),
                            text=" ".join(
                                _nested_strings(
                                    {
                                        "target": raw_target,
                                        "action": raw_action,
                                    }
                                )
                            ),
                            covered_by_refs=covered_by_refs,
                        )
                    )
                continue
            entries.append(
                _RuntimeOutcomeEntry(
                    section=section,
                    name=name,
                    action_id=None,
                    verb=canonical_action_verb(raw_target.get("verb")) or canonical_action_verb(name),
                    target=_optional_runtime_text(raw_target.get("target")),
                    actors=_runtime_actor_values(raw_target),
                    text=" ".join(_nested_strings(raw_target)),
                    covered_by_refs=covered_by_refs,
                )
            )
    return entries


def _covered_by_refs(raw_target: dict[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    covered_by = raw_target.get("covered_by")
    if isinstance(covered_by, list):
        for item in covered_by:
            if isinstance(item, dict):
                ref = item.get("ref")
            else:
                ref = item
            if ref not in (None, ""):
                refs.append(str(ref))
    elif covered_by not in (None, ""):
        refs.append(str(covered_by))
    return tuple(refs)


def _runtime_entry_matches_requirement(
    entry: _RuntimeOutcomeEntry,
    requirement: ActionRequirement,
) -> bool:
    if not _runtime_entry_actor_matches(entry, requirement):
        return False

    operation_id = _normalize_runtime_token(requirement.operation_id)
    entry_text = _normalize_runtime_token(entry.text)
    if operation_id and operation_id in entry_text:
        return True

    expected_verbs = set(requirement.expected_verbs or (requirement.verb,))
    if entry.verb and entry.verb not in expected_verbs:
        return False

    required_target = _normalize_runtime_token(requirement.target)
    if not required_target:
        return bool(entry.verb)
    declared_target = _normalize_runtime_token(entry.target)
    action_id = _normalize_runtime_token(entry.action_id)
    return bool(
        declared_target == required_target
        or action_id == required_target
        or action_id.startswith(f"{required_target}_")
        or action_id.endswith(f"_{required_target}")
        or f"_{required_target}_" in action_id
        or required_target in entry_text
    )


def _runtime_entry_actor_matches(
    entry: _RuntimeOutcomeEntry,
    requirement: ActionRequirement,
) -> bool:
    if not requirement.actor or not entry.actors:
        return True
    required = _normalize_runtime_token(requirement.actor)
    return required in {_normalize_runtime_token(actor) for actor in entry.actors}


def _runtime_entry_represents_outcome(
    entry: _RuntimeOutcomeEntry,
    outcome: str,
    signature: set[str],
) -> bool:
    normalized_outcome = _normalize_runtime_token(outcome)
    normalized_text = _normalize_runtime_token(entry.text)
    if normalized_outcome and normalized_outcome in normalized_text:
        return True
    entry_tokens = {token for token in normalized_text.split("_") if token}
    hits = len(signature.intersection(entry_tokens))
    required_hits = max(2, (len(signature) + 1) // 2)
    return hits >= required_hits


def _outcome_signature(outcome: str) -> set[str]:
    tokens = {
        token
        for token in _normalize_runtime_token(outcome).split("_")
        if len(token) > 1 and token not in _OUTCOME_TOKEN_STOPWORDS
    }
    return tokens


def _runtime_actor_values(*items: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for item in items:
        for key in ("actor", "actors"):
            raw = item.get(key)
            if raw in (None, ""):
                continue
            if isinstance(raw, str):
                values.append(raw)
            elif isinstance(raw, list):
                values.extend(str(value) for value in raw if value not in (None, ""))
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _normalize_runtime_token(value)
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(str(value).strip())
    return tuple(normalized)


def _optional_runtime_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _evidence_ref_path(project_root: Path, ref: str) -> Path | None:
    file_ref = str(ref).split("::", 1)[0].strip()
    if not file_ref:
        return None
    path = Path(file_ref)
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.exists() and resolved.is_file() else None


def _synthetic_mutation_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for path in _configured_text_files(project_root, config, "source_dirs", ["src/"]):
        text = _read_text_best_effort(path)
        if not _MUTATING_ENDPOINT_RE.search(text):
            continue
        if not _SYNTHETIC_MUTATION_RE.search(text):
            continue
        if _PERSISTENCE_EVIDENCE_RE.search(text):
            continue
        warnings.append(
            f"Mutating handler `{_display_path(path, project_root)}` appears to return synthetic success "
            "without persistence evidence. Add a real write/readback path or runtime action outcome proof "
            "that observes persisted state."
        )
    return warnings


def _interactive_control_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    target_actions = action_target_specs_from_config(config)
    warnings: list[str] = []
    for path in _configured_text_files(project_root, config, "source_dirs", ["src/"]):
        if path.suffix not in {".tsx", ".jsx", ".html", ".vue", ".svelte"}:
            continue
        text = _read_text_best_effort(path)
        for match in _BUTTON_RE.finditer(text):
            attrs = match.group("attrs") or ""
            label = _control_label(match.group("label") or "")
            verb = canonical_action_verb(label)
            if verb is None:
                continue
            if _DISABLED_CONTROL_RE.search(attrs):
                continue
            if _control_has_static_connection(text, match.start(), attrs):
                continue
            if _control_has_runtime_evidence(label, verb, target_actions):
                continue
            warnings.append(
                f"Interactive control `{label}` in `{_display_path(path, project_root)}` looks like a "
                f"mutating `{verb}` action but has no static handler/form/navigation evidence and no matching "
                "`runtime.action_outcome_targets` action."
            )
    return warnings


def _undeclared_surface_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Flag actor-facing interactive surfaces absent from the declared universe.

    Orthogonal to ``_interactive_control_warnings``: that asks "is this control
    wired?", this asks "is this wired/authoring surface DECLARED in
    operation_flow?". A fully wired authoring control still warns when no declared
    operation names its capability, because every per-operation coverage axis is
    structurally blind to capabilities that were never lifted into operation_flow.
    """

    source_files = _configured_text_files(project_root, config, "source_dirs", ["src/"])
    source_texts = iter_markup_source_texts(
        source_files,
        display=lambda path: _display_path(path, project_root),
        read_text=_read_text_best_effort,
    )
    flows = _operation_flows_from_project(project_root, config)
    runtime_tokens = _runtime_action_tokens_from_config(config)
    return surface_reconciliation_warnings(
        source_texts, flows, config, runtime_tokens=runtime_tokens
    )


def _requirement_reconciliation_doctor_warnings(
    project_root: Path,
    config: dict[str, Any],
    operation_flows: list[tuple[str, Any]],
) -> list[str]:
    """Flag requirement-document behaviours absent from the declared universe.

    Third edge of the requirements/operations/source reconciliation triangle:
    ``surface_reconciliation`` reconciles implemented source -> operation_flow,
    ``orphan_cover_markers`` reconciles test markers -> operation_flow, and this
    reconciles requirement documents -> operation_flow ("required but
    undeclared"). Advisory only; dormant without declared operations.
    """

    docs = discover_requirement_docs(project_root, config)
    doc_texts = [
        (_display_path(path, project_root), _read_text_best_effort(path))
        for path in docs
    ]
    runtime_tokens = _runtime_action_tokens_from_config(config)
    return requirement_reconciliation_warnings(
        doc_texts, operation_flows, config, runtime_tokens=runtime_tokens
    )


def _runtime_action_tokens_from_config(config: dict[str, Any]) -> frozenset[str]:
    """Capability tokens already declared via ``runtime.action_outcome_targets``.

    Reuses the existing action-target parser so a capability that already has a
    runtime outcome target is treated as declared and does not warn.
    """

    tokens: set[str] = set()
    for action in action_target_specs_from_config(config):
        for value in (action.verb, action.action_id, action.target, action.target_name):
            token = _normalize_runtime_token(value)
            if token:
                tokens.add(token)
                tokens.update(part for part in token.split("_") if part)
            canonical = canonical_action_verb(value)
            if canonical:
                tokens.add(canonical)
    return frozenset(tokens)


def _orphan_cover_marker_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Flag ``codd: covers`` markers for operations absent from operation_flow.

    Defense-in-depth reverse reconciliation for the audit path: the normal
    scenario->test matcher silently drops a marker whose operation is in no
    declared flow. Opt-out via ``orphan_cover_markers.enabled: false`` in
    codd.yaml. Best-effort: if the scenario collection cannot be built (no
    operation_flow / parse error), stay silent rather than fail doctor.
    """

    settings = config.get("orphan_cover_markers")
    if isinstance(settings, dict) and not bool(settings.get("enabled", True)):
        return []
    try:
        from codd.operational_e2e_audit import detect_orphan_cover_markers
    except ImportError:
        return []
    try:
        orphans = detect_orphan_cover_markers(project_root)
    except (FileNotFoundError, ValueError):
        return []
    return [orphan.message for orphan in orphans]


def _weak_action_outcome_warning_messages(target_actions: tuple[ActionTargetSpec, ...]) -> list[str]:
    warnings: list[str] = []
    for action in target_actions:
        if action.verb is None:
            continue
        outcomes = {_normalize_runtime_token(outcome) for outcome in action.outcomes}
        if outcomes and outcomes.intersection(_STRONG_OUTCOME_NAMES):
            continue
        actor_note = f", actor={','.join(action.actors)}" if action.actors else ""
        warnings.append(
            f"`runtime.action_outcome_targets` action `{action.action_id}` in `{action.target_name}` "
            f"declares mutating verb `{action.verb}`{actor_note} but only weak outcome metadata "
            f"{sorted(outcomes) or '[]'}. Add visible reflection, reload persistence, persisted readback, "
            "expected absence, or another durable observable outcome."
        )
    return warnings


def _terminal_action_outcome_warning_messages(target_actions: tuple[ActionTargetSpec, ...]) -> list[str]:
    warnings: list[str] = []
    for action in target_actions:
        terminal_verb = _terminal_action_verb(action)
        if terminal_verb is None:
            continue
        outcomes = {_normalize_runtime_token(outcome) for outcome in action.outcomes}
        if outcomes.intersection(_TERMINAL_OUTCOME_NAMES):
            continue
        warnings.append(
            f"`runtime.action_outcome_targets` action `{action.action_id}` in `{action.target_name}` "
            f"declares terminal/non-repeatable verb `{terminal_verb}` but does not assert the post-action "
            "control state. Add disabled_state, control_absence, expected_absence, terminal_state_guard, "
            "or an equivalent outcome so users are not left with a stale clickable control."
        )
    return warnings


def _terminal_action_verb(action: ActionTargetSpec) -> str | None:
    candidates = (
        action.verb,
        canonical_action_verb(action.action_id),
        canonical_action_verb(action.target),
    )
    for candidate in candidates:
        if candidate in _TERMINAL_ACTION_VERBS:
            return candidate
    return None


def _screen_escape_route_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for path in _configured_text_files(project_root, config, "source_dirs", ["src/"]):
        if not _looks_like_screen_file(path):
            continue
        text = _read_text_best_effort(path)
        if not _SCREEN_CONTENT_RE.search(text):
            continue
        if _ESCAPE_ROUTE_RE.search(text) or _ancestor_layout_has_escape_route(path, project_root):
            continue
        warnings.append(
            f"Business screen `{_display_path(path, project_root)}` has visible page content but no static "
            "escape route/navigation evidence in the screen or ancestor layout. Add persistent navigation, "
            "a home/dashboard/back link, breadcrumb, or a runtime navigation coverage target."
        )
    return warnings


def _authenticated_global_action_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    if _has_global_action_targets(config):
        return []
    has_authenticated_ui = False
    has_responsive_breakpoint = False
    has_static_session_action = False
    for path in _configured_text_files(project_root, config, "source_dirs", ["src/"]):
        if path.suffix not in {".tsx", ".jsx", ".html", ".vue", ".svelte"}:
            continue
        text = _read_text_best_effort(path)
        has_authenticated_ui = has_authenticated_ui or bool(_AUTHENTICATED_UI_RE.search(text))
        has_responsive_breakpoint = has_responsive_breakpoint or bool(_RESPONSIVE_BREAKPOINT_RE.search(text))
        has_static_session_action = has_static_session_action or bool(_SESSION_ACTION_TOKEN_RE.search(text))
    if not has_authenticated_ui or not has_responsive_breakpoint:
        return []
    action_note = (
        " Only static session/account action text was found; declare breakpoint runtime evidence "
        "so desktop-only controls do not mask mobile gaps."
        if has_static_session_action
        else ""
    )
    return [
        "Authenticated responsive UI detected, but no `runtime.global_action_targets` are declared. "
        "Add breakpoint-specific runtime evidence for required global actions such as sign-out, "
        "account access, home, or primary navigation so desktop-visible session controls are not "
        f"accepted as mobile coverage.{action_note}"
    ]


def _presentation_obligation_warnings(project_root: Path, config: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for path in _configured_doc_files(project_root, config):
        payload = _frontmatter_or_yaml_payload(path)
        if not isinstance(payload, dict):
            continue
        source = _display_path(path, project_root)
        display_fields = _presentation_entries(payload, ("display_fields", "displayed_fields", "presentation_fields"))
        presentation_specs = _entries_by_field_id(_presentation_entries(payload, ("presentation_specs",)))
        aggregation_policies = _entries_by_field_id(_presentation_entries(payload, ("aggregation_policies",)))
        for field in display_fields:
            field_id = _field_id(field)
            if not field_id:
                continue
            presentation = _merge_obligation(field, presentation_specs.get(field_id), "presentation")
            if _requires_presentation_spec(field, presentation) and not _has_any_presentation_declaration(presentation):
                warnings.append(
                    f"W-PRES-001: displayed field `{field_id}` in `{source}` has no presentation spec."
                )
            missing = _missing_i18n_presentation_attributes(field, presentation)
            if missing:
                warnings.append(
                    f"W-PRES-002: locale/timezone lexicon obligation for displayed field `{field_id}` "
                    f"in `{source}` lacks field-level {', '.join(missing)} declaration."
                )

            aggregation = _merge_obligation(field, aggregation_policies.get(field_id), "aggregation")
            if _requires_aggregation_policy(field, aggregation) and not _has_aggregation_policy(aggregation):
                warnings.append(
                    f"W-AGG-001: collection/cardinality display field `{field_id}` in `{source}` "
                    "lacks aggregation policy."
                )
    return warnings


def _presentation_entries(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
    for journey in payload.get("user_journeys", []) if isinstance(payload.get("user_journeys"), list) else []:
        if not isinstance(journey, dict):
            continue
        for key in keys:
            value = journey.get(key)
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
    return entries


def _entries_by_field_id(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_field: dict[str, dict[str, Any]] = {}
    for entry in entries:
        field_id = _field_id(entry)
        if field_id:
            by_field[field_id] = entry
    return by_field


def _field_id(entry: dict[str, Any]) -> str:
    for key in ("field_id", "field", "id", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_obligation(field: dict[str, Any], explicit: dict[str, Any] | None, nested_key: str) -> dict[str, Any]:
    merged: dict[str, Any] = {key: value for key, value in field.items() if not key.startswith("_")}
    nested = field.get(nested_key)
    if isinstance(nested, dict):
        merged.update(nested)
    alias = field.get(f"{nested_key}_spec")
    if isinstance(alias, dict):
        merged.update(alias)
    alias = field.get(f"{nested_key}_policy")
    if isinstance(alias, dict):
        merged.update(alias)
    if explicit:
        merged.update(explicit)
    return merged


def _requires_presentation_spec(field: dict[str, Any], presentation: dict[str, Any]) -> bool:
    text = _obligation_text(field, presentation)
    return _truthy(field.get("presentation_required")) or any(
        token in text
        for token in (
            "datetime",
            "date_time",
            "timestamp",
            "locale",
            "localized",
            "timezone",
            "time_zone",
            "number",
            "currency",
            "amount",
            "percent",
            "i18n_unicode_cldr",
        )
    )


def _missing_i18n_presentation_attributes(field: dict[str, Any], presentation: dict[str, Any]) -> list[str]:
    text = _obligation_text(field, presentation)
    if not any(token in text for token in ("i18n_unicode_cldr", "locale", "timezone", "time_zone")):
        return []
    required: list[str] = []
    if any(token in text for token in ("time_zone", "timezone", "datetime", "date_time", "timestamp")):
        required.extend(["format", "timezone"])
    if any(token in text for token in ("locale", "localized", "i18n_unicode_cldr")):
        required.append("locale")
    return [attribute for attribute in _dedupe(required) if not _has_presentation_attribute(presentation, attribute)]


def _has_any_presentation_declaration(presentation: dict[str, Any]) -> bool:
    return any(
        _has_presentation_attribute(presentation, attribute)
        for attribute in ("format", "locale", "timezone", "unit", "precision", "calendar")
    )


def _has_presentation_attribute(presentation: dict[str, Any], attribute: str) -> bool:
    aliases = {
        "format": ("format", "pattern", "display_format", "number_format", "date_format", "time_format"),
        "locale": ("locale", "language", "language_tag", "bcp47", "locale_tag"),
        "timezone": ("timezone", "time_zone", "timezone_id", "iana_timezone", "iana_time_zone"),
        "unit": ("unit", "display_unit"),
        "precision": ("precision", "rounding", "scale"),
        "calendar": ("calendar", "calendar_system"),
    }
    for key in aliases.get(attribute, (attribute,)):
        if presentation.get(key) not in (None, "", []):
            return True
    return False


def _requires_aggregation_policy(field: dict[str, Any], aggregation: dict[str, Any]) -> bool:
    if _truthy(field.get("aggregation_required")) or _truthy(aggregation.get("required")):
        return True
    text = _obligation_text(field, aggregation)
    return any(
        token in text
        for token in ("0..n", "1..n", "n:m", "many", "multiple", "collection", "list", "array", "repeated")
    )


def _has_aggregation_policy(aggregation: dict[str, Any]) -> bool:
    if aggregation.get("policy") not in (None, "", []):
        return True
    if aggregation.get("aggregation_policy") not in (None, "", []):
        return True
    cardinality_when_many = aggregation.get("cardinality_when_many")
    return isinstance(cardinality_when_many, dict) and cardinality_when_many.get("policy") not in (None, "", [])


def _obligation_text(field: dict[str, Any], obligation: dict[str, Any]) -> str:
    values: list[Any] = []
    for source in (field, obligation):
        for key in (
            "field_id",
            "field",
            "type",
            "kind",
            "data_type",
            "value_kind",
            "lexicon_ref",
            "lexicon_refs",
            "axis",
            "axis_type",
            "cardinality",
            "display_cardinality",
            "collection_context",
        ):
            values.append(source.get(key))
    return " ".join(_nested_strings(values)).lower()


def _nested_strings(value: Any) -> set[str]:
    strings: set[str] = set()
    if value is None:
        return strings
    if isinstance(value, str):
        strings.add(value)
        return strings
    if isinstance(value, dict):
        for key, item in value.items():
            strings.update(_nested_strings(key))
            strings.update(_nested_strings(item))
        return strings
    if isinstance(value, (list, tuple, set)):
        for item in value:
            strings.update(_nested_strings(item))
        return strings
    strings.add(str(value))
    return strings


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "required", "must"}
    return bool(value)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value in result:
            continue
        result.append(value)
    return result


def _looks_like_screen_file(path: Path) -> bool:
    return bool(_SCREEN_FILE_RE.search(path.as_posix()))


def _ancestor_layout_has_escape_route(path: Path, project_root: Path) -> bool:
    try:
        current = path.parent
        root = project_root.resolve()
        while current.resolve() != root and root in current.resolve().parents:
            for name in ("layout.tsx", "layout.jsx", "Layout.tsx", "layout.html", "layout.vue", "layout.svelte"):
                candidate = current / name
                if candidate.exists() and _ESCAPE_ROUTE_RE.search(_read_text_best_effort(candidate)):
                    return True
            current = current.parent
    except OSError:
        return False
    return False


def _control_label(raw_html: str) -> str:
    return " ".join(_TAG_RE.sub(" ", raw_html).split())


def _control_has_static_connection(text: str, start: int, attrs: str) -> bool:
    if _CONNECTED_CONTROL_RE.search(attrs):
        return True
    if re.search(r"(?i)type\s*=\s*['\"]button['\"]", attrs):
        return False
    return _inside_form(text, start)


def _inside_form(text: str, start: int) -> bool:
    before = text[:start]
    return before.lower().rfind("<form") > before.lower().rfind("</form")


def _control_has_runtime_evidence(
    label: str,
    verb: str,
    target_actions: tuple[ActionTargetSpec, ...],
) -> bool:
    label_token = _normalize_runtime_token(label)
    label_parts = {part for part in label_token.split("_") if part}
    for action in target_actions:
        if action.verb != verb:
            continue
        evidence = "_".join(
            part
            for part in (
                _normalize_runtime_token(action.action_id),
                _normalize_runtime_token(action.target),
                _normalize_runtime_token(action.target_name),
            )
            if part
        )
        if not label_parts or label_parts.intersection({part for part in evidence.split("_") if part}):
            return True
    return False


def _normalize_runtime_token(value: Any) -> str:
    if value in (None, ""):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _has_mutating_source(project_root: Path, config: dict[str, Any]) -> bool:
    for path in _configured_text_files(project_root, config, "source_dirs", ["src/"]):
        if _MUTATING_ENDPOINT_RE.search(_read_text_best_effort(path)):
            return True
    return False


def _has_reflection_e2e(project_root: Path, config: dict[str, Any]) -> bool:
    for path in _configured_text_files(project_root, config, "test_dirs", ["tests/"]):
        text = _read_text_best_effort(path)
        if _MUTATING_ENDPOINT_RE.search(text) and _REFLECTION_E2E_RE.search(text):
            return True
    return False


def _configured_text_files(project_root: Path, config: dict[str, Any], key: str, default: list[str]) -> list[Path]:
    scan = config.get("scan", {})
    raw_dirs = scan.get(key, default) if isinstance(scan, dict) else default
    dirs = raw_dirs if isinstance(raw_dirs, list) else default
    files: list[Path] = []
    for raw_dir in dirs:
        if not isinstance(raw_dir, str) or not raw_dir.strip():
            continue
        root = Path(raw_dir).expanduser()
        if not root.is_absolute():
            root = project_root / root
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix in _TEXT_SUFFIXES:
                files.append(root)
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in _TEXT_SUFFIXES:
                files.append(path)
    return files


def _read_text_best_effort(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _format_preflight_ntfy(result: Any) -> str:
    failed = [check.name for check in result.checks if check.status in {"FAIL", "WARN"}]
    suffix = f" ({', '.join(failed)})" if failed else ""
    return f"CoDD preflight: {result.task_id} severity={result.severity}{suffix}"


def _stdin_is_tty() -> bool:
    """Return True only when stdin is an interactive terminal.

    Guards interactive ``click`` prompts so CoDD never blocks (and then aborts
    with an opaque ``Aborted!``) when run non-interactively — e.g. CI, an agent
    shell, or any pipeline where stdin is ``/dev/null``.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (ValueError, AttributeError, OSError):
        return False


def _resolve_required_value(
    *,
    value: str | None,
    prompt: str,
    missing_hint: str,
) -> str:
    """Resolve a required string, prompting interactively only on a real TTY.

    Order of precedence: an already-supplied ``value`` wins. Otherwise, on an
    interactive terminal the user is prompted. When there is no TTY we raise a
    clear, actionable error naming the flag to pass, instead of letting
    ``click`` emit a bare ``Aborted!``.
    """
    if value is not None and str(value).strip():
        return str(value)
    if _stdin_is_tty():
        return str(click.prompt(prompt))
    raise click.UsageError(missing_hint)


@main.command()
@click.argument("name", required=False)
@click.option("--project-name", default=None, help="Name of the project (alias for the positional NAME argument)")
@click.option("--language", default=None, help="Primary language (python/typescript/javascript/go — full support; java — symbols only)")
@click.option("--dest", default=".", help="Destination directory (default: current dir)")
@click.option(
    "--requirements",
    default=None,
    type=click.Path(exists=True),
    help="Import a requirements file (any format: .md, .txt, .doc). CoDD adds frontmatter automatically.",
)
@click.option(
    "--config-dir",
    default="codd",
    help="Name of the CoDD config directory (default: codd). Use .codd when codd/ is your source directory.",
)
@click.option(
    "--project-type",
    default=None,
    help=(
        "Project type for capability-aware generation (cli, web, mobile, iot, generic, or a "
        "project-local custom profile). Written to required_artifacts.project_type in codd.yaml; "
        "unknown types fall back to generic with a warning. Unset = legacy web-style fallback."
    ),
)
@click.option(
    "--suggest-lexicons/--no-suggest-lexicons",
    default=True,
    help="Detect project manifests and offer lexicon plug-in suggestions.",
)
@click.option(
    "--llm-enhanced",
    is_flag=True,
    default=False,
    help="Use AI to recommend lexicons from requirements and design context.",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Apply high and medium confidence AI lexicon recommendations without prompting.",
)
def init(
    name: str | None,
    project_name: str | None,
    language: str | None,
    dest: str,
    requirements: str | None,
    config_dir: str,
    project_type: str | None,
    suggest_lexicons: bool,
    llm_enhanced: bool,
    auto_approve: bool,
):
    """Initialize CoDD in a project directory.

    The project name is taken from the positional NAME argument
    (``codd init my-project``). ``--project-name`` is kept as a back-compatible
    alias. When neither is supplied, CoDD prompts interactively on a TTY and
    fails with a clear message otherwise, so non-interactive runs (CI, agents,
    stdin from /dev/null) never hang or abort opaquely.
    """
    positional = name.strip() if isinstance(name, str) and name.strip() else None
    option = project_name.strip() if isinstance(project_name, str) and project_name.strip() else None
    if positional and option and positional != option:
        raise click.UsageError(
            "Conflicting project names: positional NAME "
            f"'{positional}' != --project-name '{option}'. Pass only one."
        )
    project_name = _resolve_required_value(
        value=positional or option,
        prompt="Project name",
        missing_hint=(
            "Project name is required. Pass it as a positional argument "
            "(codd init <name>) or via --project-name <name>."
        ),
    )
    language = _resolve_required_value(
        value=language,
        prompt="Primary language",
        missing_hint=(
            "Primary language is required. Pass it via "
            "--language <python|typescript|javascript|go|java>."
        ),
    )

    dest_path = Path(dest).resolve()
    codd_dir = dest_path / config_dir

    if codd_dir.exists():
        if requirements:
            # Import requirements into existing CoDD project
            req_path = _import_requirements(dest_path, Path(requirements), project_name)
            rel_req = req_path.relative_to(dest_path).as_posix()
            click.echo(f"Requirements imported: {rel_req} (frontmatter added)")
            if project_type:
                _record_project_type(dest_path, codd_dir, project_type)
            click.echo(f"\nNext: codd generate --wave 2  (AI generates design docs)")
            return
        click.echo(f"Error: {codd_dir} already exists. Use --requirements to import a requirements file.")
        raise SystemExit(1)

    # Create directory structure
    (codd_dir / "reports").mkdir(parents=True)
    (codd_dir / "scan").mkdir(exist_ok=True)

    # Copy templates
    _render_template("codd.yaml.tmpl", codd_dir / "codd.yaml", {
        "project_name": project_name,
        "language": language,
        "source_dirs": _format_yaml_list(["src/"]),
        "graph_path": f"{config_dir}/scan",
    })
    _render_template("gitignore.tmpl", codd_dir / ".gitignore", {})

    # Version file
    (dest_path / ".codd_version").write_text("0.2.0\n", encoding="utf-8")

    # Project type → capability-aware generation (no browser-test residue for
    # a cli project). Unset keeps the legacy web-style fallback untouched.
    if project_type:
        _record_project_type(dest_path, codd_dir, project_type)

    # Import requirements if provided
    if requirements:
        req_path = _import_requirements(dest_path, Path(requirements), project_name)
        rel_req = req_path.relative_to(dest_path).as_posix()
        click.echo(f"CoDD initialized in {codd_dir}")
        click.echo(f"  codd.yaml         — project config")
        click.echo(f"  {rel_req}  — requirements (frontmatter added)")
        click.echo(f"")
        click.echo(f"Next: codd generate --wave 2  (AI generates design docs)")
    else:
        click.echo(f"CoDD initialized in {codd_dir}")
        click.echo(f"  codd.yaml    — project config")
        click.echo(f"  scan/        — JSONL scan output (nodes.jsonl, edges.jsonl)")
        click.echo(f"")
        click.echo(f"Next: Write your requirements, then run:")
        click.echo(f"  codd init --requirements your-spec.md --dest .")
        click.echo(f"  or: codd generate --wave 2  (auto-generates everything)")

    if suggest_lexicons:
        _offer_lexicon_suggestions(dest_path, llm_enhanced=llm_enhanced, auto_approve=auto_approve)


def _record_project_type(project_root: Path, codd_dir: Path, project_type: str) -> None:
    """Validate *project_type* against the registry and persist it to codd.yaml.

    Writes ``required_artifacts.project_type`` (the key the generator,
    implementer, artifact deriver and completeness auditor all consult).
    Unknown types resolve to ``generic`` with a warning — never silently to
    web (consistent with codd.project_types.resolve_project_type). Appends a
    new section as text so the template's comments survive; an existing
    ``required_artifacts`` section is never rewritten blindly.
    """
    from codd.project_types import resolve_project_type

    requested = project_type.strip().lower()
    resolved, reason = resolve_project_type(requested, None, project_root)
    if resolved != requested:
        click.echo(f"Warning: {reason}")

    config_path = codd_dir / "codd.yaml"
    text = config_path.read_text(encoding="utf-8")
    existing = yaml.safe_load(text) or {}
    section = existing.get("required_artifacts") if isinstance(existing, dict) else None
    if isinstance(section, dict):
        current = str(section.get("project_type") or "").strip()
        if current:
            click.echo(
                f"required_artifacts.project_type already set: {current} (left unchanged)"
            )
            return
        # The section exists without project_type: appending a duplicate
        # `required_artifacts:` mapping would silently REPLACE the section on
        # the next YAML load and drop its sibling keys. Ask for a manual edit.
        click.echo(
            "Warning: codd.yaml already has a required_artifacts section; "
            f"add `project_type: \"{resolved}\"` to it manually."
        )
        return
    block = (
        "\n"
        "# Project type (drives capability-aware generation: UI, network surface,\n"
        "# e2e modality). Profiles: codd/required_artifacts/defaults/*.yaml.\n"
        "required_artifacts:\n"
        f'  project_type: "{resolved}"\n'
    )
    config_path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    click.echo(f"Project type: {resolved}")


def _offer_lexicon_suggestions(
    project_root: Path,
    *,
    llm_enhanced: bool = False,
    auto_approve: bool = False,
) -> None:
    from codd.init.lexicon_suggest import (
        append_suggested_lexicons,
        describe_lexicons,
        load_stack_map,
        suggest_lexicons,
    )
    from codd.init.stack_detector import StackDetector

    if llm_enhanced:
        from codd.init.llm_lexicon_suggester import llm_recommend_lexicons

        llm_result = llm_recommend_lexicons(project_root)
        if llm_result.recommendations:
            descriptions = describe_lexicons(rec.lexicon_id for rec in llm_result.recommendations)
            click.echo("")
            click.echo("[LLM-enhanced] Analyzing project ...")
            click.echo("[LLM-enhanced] Detected:")
            click.echo(f"  - Data types: {_format_detected_items(llm_result.detected_data_types)}")
            click.echo(f"  - Function traits: {_format_detected_items(llm_result.detected_function_traits)}")
            click.echo(f"  - Tech stack: {_format_detected_items(llm_result.detected_tech_stack)}")
            click.echo("")
            click.echo("[LLM-enhanced] Recommended lexicons:")
            for index, recommendation in enumerate(llm_result.recommendations, start=1):
                description = descriptions.get(recommendation.lexicon_id, "")
                suffix = f" ({description})" if description else ""
                click.echo(
                    f"  {index}. {_confidence_icon(recommendation.confidence)} "
                    f"{recommendation.lexicon_id}{suffix} [{recommendation.confidence}]"
                )
                if recommendation.reason:
                    click.echo(f"     {recommendation.reason}")

            selected = _select_llm_recommendations(llm_result, auto_approve=auto_approve)
            if not selected:
                click.echo("LLM-enhanced lexicons not added.")
                return
            path = append_suggested_lexicons(project_root, selected)
            rel_path = path.relative_to(project_root).as_posix()
            click.echo(f"{rel_path} updated ({len(selected)} suggested lexicons)")
            return
        click.echo("")
        click.echo("[LLM-enhanced] No usable recommendation; falling back to stack-based suggestions.")

    detection = StackDetector().detect(project_root)
    if not detection.stack_hints:
        return

    suggestions = suggest_lexicons(detection.stack_hints, load_stack_map())
    if not suggestions:
        return

    descriptions = describe_lexicons(suggestions)
    click.echo("")
    click.echo(f"Detected signals: {', '.join(detection.detected_signals)}")
    click.echo(f"Detected hints: {', '.join(detection.stack_hints)}")
    click.echo("Suggested lexicons:")
    for lexicon_id in suggestions:
        description = descriptions.get(lexicon_id, "")
        suffix = f" ({description})" if description else ""
        click.echo(f"  - {lexicon_id}{suffix}")

    if not click.confirm(f"Add to {LEXICON_FILENAME}?", default=True):
        click.echo("Suggested lexicons not added.")
        return
    path = append_suggested_lexicons(project_root, suggestions)
    rel_path = path.relative_to(project_root).as_posix()
    click.echo(f"{rel_path} updated ({len(suggestions)} suggested lexicons)")


def _format_detected_items(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def _confidence_icon(confidence: str) -> str:
    return {"high": "✅", "medium": "⚠️", "low": "△"}.get(confidence, "△")


def _select_llm_recommendations(llm_result: Any, *, auto_approve: bool) -> list[str]:
    if auto_approve:
        return [
            recommendation.lexicon_id
            for recommendation in llm_result.recommendations
            if recommendation.confidence in {"high", "medium"}
        ]

    choice = click.prompt("Apply all recommended? [Y/n/select]", default="Y", show_default=False).strip().lower()
    if choice in {"y", "yes", ""}:
        return [
            recommendation.lexicon_id
            for recommendation in llm_result.recommendations
            if recommendation.confidence in {"high", "medium"}
        ]
    if choice in {"n", "no"}:
        return []
    if choice != "select":
        click.echo("Invalid selection; suggested lexicons not added.")
        return []

    raw_numbers = click.prompt("Select recommendation numbers", default="", show_default=False).strip()
    selected: list[str] = []
    for token in re.split(r"[\s,]+", raw_numbers):
        if not token:
            continue
        try:
            index = int(token)
        except ValueError:
            continue
        if 1 <= index <= len(llm_result.recommendations):
            lexicon_id = llm_result.recommendations[index - 1].lexicon_id
            if lexicon_id not in selected:
                selected.append(lexicon_id)
    return selected


def _operations_codd_dir(project_path: str) -> tuple[Path, Path]:
    """Resolve (project_root, codd_dir) for the operations command group."""

    project_root = Path(project_path).resolve()
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        click.echo(f"Error: no codd.yaml found under {project_root}")
        raise SystemExit(1)
    return project_root, codd_dir


def _build_operations_ai_invoke(project_root: Path, config: dict[str, Any], ai_cmd: str | None):
    """Build a plain text-in/text-out AI invoker for operation derivation."""

    from codd.ai_invoke import force_claude_print, resolve_ai_command
    from codd.deployment.providers.ai_command_factory import get_ai_command

    resolved = force_claude_print(resolve_ai_command(config, ai_cmd, command_name="generate"))
    adapter = get_ai_command(config, project_root=project_root, command_override=resolved)

    def invoke(prompt: str) -> str:
        return adapter.invoke(prompt)

    return invoke


@main.group("operations", cls=_AliasedGroup, aliases={"merge": "apply"})
def operations_cmd() -> None:
    """Derive, review, approve, and apply candidate operation_flow entries (HITL).

    Lifecycle: derive → show → approve → apply (alias: merge deprecated).
    """


@operations_cmd.command("derive")
@project_root_option("project_path")
@click.option("--ai-cmd", default=None, help="Override AI CLI command (defaults to codd.yaml ai_command).")
@click.option("--output", default=None, help="Proposal artifact path (default: <codd-dir>/operations_proposal.yaml).")
def operations_derive_cmd(project_path: str, ai_cmd: str | None, output: str | None) -> None:
    """Layers 1+2: detect uncovered requirement units and propose operations."""
    import codd.operations_derive as opx

    project_root, codd_dir = _operations_codd_dir(project_path)
    config = load_project_config(project_root)

    ai_invoke = _build_operations_ai_invoke(project_root, config, ai_cmd)
    try:
        result = opx.derive_operations(project_root, config, ai_invoke=ai_invoke)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    out_path = opx.proposal_path(codd_dir, output)
    opx.write_proposal_artifact(out_path, result.artifact)

    rel = _display_path(out_path, project_root)
    click.echo(f"Uncovered requirement units: {len(result.uncovered_units)}")
    click.echo(f"Proposed operations: {len(result.artifact.proposals)}")
    if result.skipped_units:
        click.echo(f"Skipped (no usable proposal): {len(result.skipped_units)}")
    click.echo(f"Proposal written: {rel}")
    if result.artifact.proposals:
        click.echo("Review with `codd operations show`, then `codd operations approve` and `codd operations apply`.")


@operations_cmd.command("show")
@project_root_option("project_path")
@click.option("--output", default=None, help="Proposal artifact path (default: <codd-dir>/operations_proposal.yaml).")
def operations_show_cmd(project_path: str, output: str | None) -> None:
    """Show declared operations and any pending proposal (diff-style)."""
    import codd.operations_derive as opx

    project_root, codd_dir = _operations_codd_dir(project_path)
    config = load_project_config(project_root)
    artifact = opx.load_proposal_artifact(opx.proposal_path(codd_dir, output))
    click.echo(opx.render_show(config, artifact))


@operations_cmd.command("approve")
@project_root_option("project_path")
@click.option("--output", default=None, help="Proposal artifact path (default: <codd-dir>/operations_proposal.yaml).")
@click.option("--all", "approve_all", is_flag=True, default=False, help="Approve every pending proposal.")
@click.option("--id", "ids", multiple=True, help="Approve a specific proposal id (repeatable).")
def operations_approve_cmd(project_path: str, output: str | None, approve_all: bool, ids: tuple[str, ...]) -> None:
    """Mark selected proposal entries approved in the proposal file."""
    import codd.operations_derive as opx

    if not approve_all and not ids:
        click.echo("Error: pass --all or one or more --id <id>.")
        raise SystemExit(1)

    project_root, codd_dir = _operations_codd_dir(project_path)
    out_path = opx.proposal_path(codd_dir, output)
    artifact = opx.load_proposal_artifact(out_path)
    if not artifact.proposals:
        click.echo("No proposals found. Run `codd operations derive` first.")
        raise SystemExit(1)

    approved = opx.approve_proposals(artifact, approve_all=approve_all, ids=ids)
    opx.write_proposal_artifact(out_path, artifact)
    if approved:
        click.echo(f"Approved {len(approved)} proposal(s): {', '.join(approved)}")
    else:
        click.echo("No matching proposals approved.")


@operations_cmd.command("apply")
@project_root_option("project_path")
@click.option("--output", default=None, help="Proposal artifact path (default: <codd-dir>/operations_proposal.yaml).")
@click.option("--dry-run", is_flag=True, default=False, help="Show the merge diff without writing codd.yaml.")
def operations_apply_cmd(project_path: str, output: str | None, dry_run: bool) -> None:
    """Apply (merge) approved proposal entries into codd.yaml operation_flow."""
    import codd.operations_derive as opx

    project_root, codd_dir = _operations_codd_dir(project_path)
    config = load_project_config(project_root)
    out_path = opx.proposal_path(codd_dir, output)
    artifact = opx.load_proposal_artifact(out_path)
    plan = opx.plan_merge(artifact, config)

    if not plan.has_changes:
        click.echo("No approved, non-duplicate operations to merge.")
        if plan.skipped_unapproved:
            click.echo(f"  unapproved: {len(plan.skipped_unapproved)} (run `codd operations approve`)")
        if plan.skipped_existing:
            click.echo(f"  already declared: {', '.join(plan.skipped_existing)}")
        return

    click.echo("Operations to add to operation_flow:")
    for entry in plan.new_operations:
        outcomes = ", ".join(entry.get("expected_outcomes") or []) or "(no outcomes)"
        click.echo(
            f"  + {entry['id']}: actor={entry['actor']} verb={entry['verb']} "
            f"target={entry['target']} | outcomes: {outcomes}"
        )
    if plan.skipped_existing:
        click.echo(f"Skipped (already declared): {', '.join(plan.skipped_existing)}")

    if dry_run:
        click.echo("(dry-run) codd.yaml not modified.")
        return

    codd_yaml = codd_dir / "codd.yaml"
    try:
        count = opx.merge_into_codd_yaml(codd_yaml, plan)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    click.echo(f"Merged {count} operation(s) into {_display_path(codd_yaml, project_root)}")


@main.group("contract", cls=_AliasedGroup, aliases={"suggest": "derive", "adopt": "apply"})
def contract_cmd() -> None:
    """Inspect and verify the per-project artifact contract (V-model gate).

    Lifecycle: derive → show → apply (alias: suggest/adopt deprecated).
    """


@contract_cmd.command("show")
@project_root_option("project_path")
def contract_show_cmd(project_path: str) -> None:
    """Render the resolved artifact catalog and this project's contract."""
    from codd.artifact_contract import (
        CatalogError,
        load_catalog,
        load_contract,
        render_catalog,
        render_contract,
    )
    from codd.artifact_ids import render_required_id_mapping

    project_root = Path(project_path).resolve()
    config = load_project_config(project_root)
    try:
        catalog = load_catalog()
    except CatalogError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    contract = load_contract(config)
    click.echo(render_catalog(catalog))
    click.echo(render_contract(contract))
    # Cross-space resolver status: required_artifacts ids <-> catalog ids,
    # including the drift guard for unmapped required ids.
    click.echo(render_required_id_mapping(catalog))


@contract_cmd.command("verify")
@project_root_option("project_path")
@click.option("--stage", default=None, help="Verify only this stage (default: all declared stages).")
def contract_verify_cmd(project_path: str, stage: str | None) -> None:
    """Deterministically verify required artifacts exist per declared stage.

    Exits non-zero on missing/invalid artifacts ONLY when the contract is
    enabled; a disabled/absent contract is a no-op (exit 0).
    """
    from codd.artifact_contract import (
        CatalogError,
        load_catalog,
        load_contract,
        verify_contract,
    )

    project_root = Path(project_path).resolve()
    config = load_project_config(project_root)
    contract = load_contract(config)

    if not contract.enabled:
        click.echo("artifact_contract is disabled (opt-in); nothing to verify.")
        return
    if not contract.stages:
        click.echo("artifact_contract is enabled but declares no stages; nothing to verify.")
        return

    try:
        catalog = load_catalog()
    except CatalogError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    report = verify_contract(catalog, contract, project_root, stage=stage)
    if stage is not None and not report.stages:
        click.echo(f"Error: stage '{stage}' is not declared in artifact_contract.stages.")
        raise SystemExit(1)

    for stage_report in report.stages:
        verdict = "PASS" if stage_report.passed else "FAIL"
        click.echo(f"[{verdict}] stage {stage_report.stage}")
        for check in stage_report.checks:
            mark = "PASS" if check.ok else check.status.upper()
            suffix = f" — {check.detail}" if check.detail else ""
            paths = f" ({', '.join(check.matched_paths)})" if check.matched_paths else ""
            click.echo(f"  {mark}  {check.artifact_id}{paths}{suffix}")

    if report.has_failures:
        click.echo(f"\n{report.failure_count} required artifact(s) missing/invalid.")
        raise SystemExit(1)
    click.echo("\nAll required artifacts present.")


def _contract_codd_dir(project_path: str) -> tuple[Path, Path]:
    """Resolve (project_root, codd_dir) for the contract command group."""

    project_root = Path(project_path).resolve()
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        click.echo(f"Error: no codd.yaml found under {project_root}")
        raise SystemExit(1)
    return project_root, codd_dir


@contract_cmd.command("derive")
@project_root_option("project_path")
@click.option(
    "--output",
    default=None,
    help="Proposal file path (default: <codd-dir>/contract_proposal.yaml).",
)
def contract_derive_cmd(project_path: str, output: str | None) -> None:
    """Deterministically SELECT which catalog artifacts this project uses.

    The catalog enumerates candidate artifacts universally; this inspects the
    project's signals (existing files, requirement docs, declared operation_flow)
    and proposes a per-stage `artifact_contract` mapping. Read-only: writes a
    reviewable proposal file only — NEVER codd.yaml. Review, then `codd contract
    apply`.
    """
    import codd.artifact_contract as acx
    from codd.requirement_reconciliation import discover_requirement_docs

    project_root, codd_dir = _contract_codd_dir(project_path)
    config = load_project_config(project_root)

    try:
        catalog = acx.load_catalog()
    except acx.CatalogError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    try:
        req_docs = tuple(
            _display_path(p, project_root)
            for p in discover_requirement_docs(project_root, config)
        )
    except (OSError, ValueError, yaml.YAMLError):
        req_docs = ()

    proposal = acx.suggest_contract(
        catalog,
        project_root,
        codd_config=config,
        requirement_docs=req_docs,
    )
    out_path = acx.proposal_path(codd_dir, output)
    acx.write_proposal(out_path, proposal)

    click.echo(acx.render_suggestion(proposal))
    click.echo(f"\nProposal written: {_display_path(out_path, project_root)}")
    click.echo("Review, then apply with `codd contract apply` (add --enable to turn the gate on).")


@contract_cmd.command("apply")
@project_root_option("project_path")
@click.option(
    "--output",
    default=None,
    help="Proposal file path (default: <codd-dir>/contract_proposal.yaml).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Show the diff without writing codd.yaml.")
@click.option(
    "--enable",
    is_flag=True,
    default=False,
    help="Also set artifact_contract.enabled: true (otherwise left as-is — opt-in).",
)
def contract_apply_cmd(project_path: str, output: str | None, dry_run: bool, enable: bool) -> None:
    """Apply (merge) the proposed per-stage selection into codd.yaml (non-destructive, opt-in)."""
    import codd.artifact_contract as acx

    project_root, codd_dir = _contract_codd_dir(project_path)
    config = load_project_config(project_root)
    out_path = acx.proposal_path(codd_dir, output)
    proposal = acx.load_proposal(out_path)
    if not proposal.suggestions:
        click.echo(
            f"No proposal found at {_display_path(out_path, project_root)}. "
            "Run `codd contract derive` first."
        )
        raise SystemExit(1)

    plan = acx.plan_adopt(proposal, config, enable=enable)
    codd_yaml = codd_dir / "codd.yaml"
    click.echo(acx.render_adopt(plan, project_display=_display_path(codd_yaml, project_root)))

    if not plan.has_changes:
        return
    if dry_run:
        click.echo("(dry-run) codd.yaml not modified.")
        return

    try:
        count = acx.merge_into_codd_yaml(codd_yaml, plan)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    click.echo(f"Adopted: +{count} artifact id(s) into {_display_path(codd_yaml, project_root)}")
    if enable:
        click.echo("artifact_contract.enabled set to true.")
    else:
        click.echo("artifact_contract.enabled left unchanged (opt-in; pass --enable to turn on).")


@main.group("lexicon")
def lexicon_cmd() -> None:
    """Manage bundled lexicon plug-ins."""


@lexicon_cmd.command("list")
@click.option("--installed", "installed_only", is_flag=True, help="Show installed lexicons only.")
@click.option("--available", "available_only", is_flag=True, help="Show available, uninstalled lexicons only.")
@click.option("--all", "show_all", is_flag=True, help="Show installed and available lexicons.")
@project_root_option("project_path")
def lexicon_list_cmd(installed_only: bool, available_only: bool, show_all: bool, project_path: str) -> None:
    """List installed and bundled lexicons."""
    from codd.lexicon_cli.manager import LexiconManager

    if sum(bool(value) for value in (installed_only, available_only, show_all)) > 1:
        click.echo("Error: choose only one of --installed, --available, or --all.")
        raise SystemExit(1)

    manager = LexiconManager(Path(project_path).resolve())
    installed = manager.installed()
    available = manager.uninstalled()
    if installed_only:
        _echo_lexicon_records("Installed", installed)
        return
    if available_only:
        _echo_lexicon_records("Available", available)
        return

    _echo_lexicon_records("Installed", installed)
    if show_all or not installed_only:
        click.echo("")
        _echo_lexicon_records("Available", available)


@lexicon_cmd.command("install")
@click.argument("lexicon_ids", nargs=-1, required=True)
@project_root_option("project_path")
def lexicon_install_cmd(lexicon_ids: tuple[str, ...], project_path: str) -> None:
    """Install bundled lexicons into project_lexicon.yaml."""
    from codd.lexicon_cli.manager import LexiconManager

    manager = LexiconManager(Path(project_path).resolve())
    try:
        result = manager.install(lexicon_ids)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    for lexicon_id in result.installed:
        click.echo(f"Installed: {lexicon_id}")
    for lexicon_id in result.skipped:
        click.echo(f"Skipped: {lexicon_id} already installed")
    rel_path = _display_path(result.project_lexicon_path, manager.project_root)
    click.echo(f"Updated: {rel_path}")
    for record in result.records:
        if record.recommended_kinds:
            preview = ", ".join(record.recommended_kinds[:5])
            suffix = "" if len(record.recommended_kinds) <= 5 else ", ..."
            click.echo(f"{record.id} recommended kinds: {preview}{suffix}")
        severity_rules = record.path / "severity_rules.yaml"
        if severity_rules.is_file():
            click.echo(f"{record.id} severity rules: {_display_path(severity_rules, manager.project_root)}")


@lexicon_cmd.command("diff")
@click.argument("lexicon_id")
@project_root_option("project_path")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Output format.",
)
@click.option("--with-ai", is_flag=True, default=False, help="Use AI-backed elicit coverage mode.")
@click.option("--ai-cmd", default=None, help="Override AI CLI command for --with-ai.")
def lexicon_diff_cmd(
    lexicon_id: str,
    project_path: str,
    output_format: str,
    with_ai: bool,
    ai_cmd: str | None,
) -> None:
    """Inspect one lexicon against project requirements and design text."""
    from codd.lexicon_cli.formatters.json_fmt import to_json
    from codd.lexicon_cli.formatters.md import format_diff_md
    from codd.lexicon_cli.inspector import LexiconInspector

    try:
        result = LexiconInspector(Path(project_path).resolve()).inspect(
            lexicon_id,
            with_ai=with_ai,
            ai_command=ai_cmd,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(to_json(result) if output_format == "json" else format_diff_md(result), nl=False)


def _echo_lexicon_records(label: str, records: list[Any]) -> None:
    click.echo(f"{label} ({len(records)}):")
    for record in records:
        description = f"  {record.description}" if record.description else ""
        click.echo(f"  {record.id:<32} ({record.observation_dimensions} axes){description}")


@main.group("skills")
@click.pass_context
def skills(ctx: click.Context) -> None:
    """Manage CoDD skills for Claude Code and Codex CLI."""


@skills.command("install")
@click.argument("skill_name")
@click.option("--target", type=click.Choice(["claude", "codex", "both"]), default="both")
@click.option("--scope", type=click.Choice(["user", "repo"]), default="user")
@click.option("--mode", type=click.Choice(["symlink", "copy"]), default="symlink")
@click.option("--force", is_flag=True)
@click.option("--dir", "skill_dir", type=click.Path(exists=True))
def skills_install(skill_name: str, target: str, scope: str, mode: str, force: bool, skill_dir: str | None) -> None:
    """Install a CoDD skill for Claude Code, Codex CLI, or both."""
    skills_manager.install(skill_name, target, scope, mode, force, skill_dir)


@skills.command("list")
@click.option("--target", type=click.Choice(["claude", "codex", "both"]), default="both")
@click.option("--scope", type=click.Choice(["user", "repo", "all"]), default="all")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def skills_list(target: str, scope: str, output_format: str) -> None:
    """List installed CoDD skills."""
    skills_manager.list_skills(target, scope, output_format)


@skills.command("remove")
@click.argument("skill_name")
@click.option("--target", type=click.Choice(["claude", "codex", "both"]), default="both")
@click.option("--scope", type=click.Choice(["user", "repo"]), default="user")
@click.option("--keep-backup", is_flag=True)
def skills_remove(skill_name: str, target: str, scope: str, keep_backup: bool) -> None:
    """Remove an installed CoDD skill."""
    skills_manager.remove(skill_name, target, scope, keep_backup)


@main.command()
@project_root_option()
def scan(path: str):
    """Scan codebase and update dependency graph (Stage 1)."""
    from codd.scanner import run_scan
    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    run_scan(project_root, codd_dir)


@main.command()
@click.option("--diff", default="HEAD", help="Git diff target (default: HEAD, shows uncommitted changes)")
@project_root_option()
@click.option("--output", default=None, help="Output file (default: stdout)")
def impact(diff: str, path: str, output: str):
    """Analyze change impact from git diff."""
    from codd.propagate import run_impact
    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    run_impact(project_root, codd_dir, diff, output)


@main.command("watch")
@project_root_option("project_path")
@click.option("--debounce", default=500, show_default=True, type=int, help="Debounce interval in milliseconds")
@click.option("--background", is_flag=True, default=False, help="Run watcher in background mode")
@click.option("--status", is_flag=True, default=False, help="Show watcher status")
def watch_cmd(project_path: str, debounce: int, background: bool, status: bool) -> None:
    """Watch for file changes and emit CDAP file-change events."""
    project_root = Path(project_path).resolve()
    pid_file = project_root / ".codd" / "watch.pid"

    if status:
        if pid_file.exists():
            click.echo(f"Watcher running (PID: {pid_file.read_text(encoding='utf-8').strip()})")
        else:
            click.echo("Watcher not running")
        return

    from codd.watch.events import FileChangeEvent
    from codd.watch.watcher import start_watch

    if not project_root.exists():
        click.echo(f"Error: Project path does not exist: {project_root}")
        raise SystemExit(1)
    if not project_root.is_dir():
        click.echo(f"Error: Project path is not a directory: {project_root}")
        raise SystemExit(1)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def on_change(event: FileChangeEvent) -> None:
        preview = ", ".join(event.files[:3])
        suffix = "" if len(event.files) <= 3 else f", ... {len(event.files) - 3} more"
        click.echo(f"[watch] {len(event.files)} file(s) changed: {preview}{suffix}")

    click.echo(f"Watching {project_root} (debounce={debounce}ms)")
    observer = start_watch(project_root, on_change, debounce_ms=debounce, background=background)
    if background:
        click.echo(f"Watcher running in background mode (PID: {os.getpid()})")
        observer.join(timeout=0)


@main.command()
@click.option("--wave", required=False, default=None, type=click.IntRange(min=1), help="Wave number to generate")
@click.option(
    "--all-waves",
    "all_waves",
    is_flag=True,
    default=False,
    help="Generate every wave from wave_config in order, stopping at the first failure (mutually exclusive with --wave)",
)
@project_root_option()
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
@click.option("--feedback", default=None, help="Review feedback to address in this generation (from codd review)")
@click.option(
    "--no-contract-gate",
    is_flag=True,
    default=False,
    help="Skip the artifact-contract completion gate for the 'generate' stage",
)
def generate(
    wave: int | None,
    all_waves: bool,
    path: str,
    force: bool,
    ai_cmd: str | None,
    feedback: str | None,
    no_contract_gate: bool,
):
    """Generate CoDD documents for a specific wave (or all waves)."""
    from codd.generator import generate_wave, _load_project_config

    if all_waves and wave is not None:
        raise click.BadOptionUsage("all_waves", "--all-waves cannot be used with --wave")
    if not all_waves and wave is None:
        raise click.UsageError("Pass --wave N or --all-waves.")

    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    # Auto-generate wave_config if missing
    config = _load_project_config(project_root)
    if not config.get("wave_config"):
        click.echo("wave_config not found. Auto-generating from requirements...")
        from codd.planner import plan_init

        try:
            result = plan_init(project_root, ai_command=ai_cmd)
            click.echo(f"wave_config generated from {len(result.requirement_paths)} requirement(s)")
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error auto-generating wave_config: {exc}")
            raise SystemExit(1)

    def _generate_one(wave_number: int) -> tuple[int, int]:
        results = generate_wave(project_root, wave_number, force=force, ai_command=ai_cmd, feedback=feedback)
        generated = 0
        skipped = 0
        for result in results:
            rel_path = result.path.relative_to(project_root).as_posix()
            click.echo(f"{result.status.capitalize()}: {rel_path} ({result.node_id})")
            if result.status == "generated":
                generated += 1
            else:
                skipped += 1
        click.echo(f"Wave {wave_number}: {generated} generated, {skipped} skipped")
        return generated, skipped

    if all_waves:
        try:
            wave_numbers = _wave_numbers_from_config(_load_project_config(project_root))
        except ValueError as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        if not wave_numbers:
            click.echo("Error: wave_config is empty; run 'codd plan --init' first.")
            raise SystemExit(1)
        completed: list[int] = []
        total_generated = 0
        total_skipped = 0
        for wave_number in wave_numbers:
            try:
                generated, skipped = _generate_one(wave_number)
            except (FileNotFoundError, ValueError) as exc:
                click.echo(f"Error: wave {wave_number}: {exc}")
                click.echo(
                    f"Stopped at wave {wave_number}; "
                    f"completed wave(s): {', '.join(str(item) for item in completed) or 'none'}"
                )
                raise SystemExit(1)
            completed.append(wave_number)
            total_generated += generated
            total_skipped += skipped
        click.echo(
            f"All waves complete ({len(completed)} wave(s)): "
            f"{total_generated} generated, {total_skipped} skipped"
        )
        _enforce_stage_contract_gate(project_root, "generate", opt_out=no_contract_gate)
        return

    try:
        _generate_one(int(wave))  # type: ignore[arg-type]
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    _enforce_stage_contract_gate(project_root, "generate", opt_out=no_contract_gate)


def _wave_numbers_from_config(config: dict[str, Any]) -> list[int]:
    """Sorted wave numbers from wave_config (validating integer keys)."""
    wave_config = config.get("wave_config") or {}
    numbers: set[int] = set()
    for key in wave_config:
        try:
            numbers.add(int(key))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"wave_config key must be an integer wave number, got {key!r}") from exc
    return sorted(numbers)


@main.command()
@click.option("--wave", required=False, default=None, type=click.IntRange(min=1), help="Wave number to restore")
@project_root_option()
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
@click.option("--feedback", default=None, help="Review feedback to address in this restoration (from codd review)")
@click.option(
    "--report",
    "report_mode",
    is_flag=True,
    default=False,
    help=(
        "Do not restore; instead print a coverage/limits report over the already-"
        "restored docs (recovered-by-band, evidence-source attribution, the "
        "irrecoverable open-questions ceiling, artifact-type coverage, "
        "maintenance readiness). No AI/LLM call. Pair with --format json."
    ),
)
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format for --report (text or json).",
)
def restore(
    wave: int | None,
    path: str,
    force: bool,
    ai_cmd: str | None,
    feedback: str | None,
    report_mode: bool,
    report_format: str,
):
    """Restore design documents from extracted codebase facts (brownfield).

    Unlike 'generate' which creates design docs from requirements (greenfield),
    'restore' reconstructs design documents from extracted code analysis.
    The AI infers design intent from the actual codebase structure.

    Run 'codd extract' first, then 'codd plan --init' to create wave_config,
    then 'codd restore --wave N' to reconstruct design docs.

    Use 'codd restore --report' (after restoring) to see how far restoration
    got: provenance-backed statements by confidence band, evidence-source
    attribution, the irrecoverable-in-principle open-questions ceiling, coverage
    by artifact type / V-model layer, and DAG maintenance readiness.
    """
    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    if report_mode:
        from codd.config import load_project_config
        from codd.restoration_report import (
            build_restoration_report,
            render_report_json,
            render_report_text,
        )

        config = load_project_config(project_root)
        report = build_restoration_report(project_root, config)
        if report_format == "json":
            click.echo(render_report_json(report))
        else:
            click.echo(render_report_text(report))
        return

    if wave is None:
        click.echo("Error: --wave is required unless --report is given")
        raise SystemExit(1)

    from codd.restore import restore_wave

    try:
        results = restore_wave(project_root, wave, force=force, ai_command=ai_cmd, feedback=feedback)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    restored = 0
    skipped = 0

    for result in results:
        rel_path = result.path.relative_to(project_root).as_posix()
        click.echo(f"{result.status.capitalize()}: {rel_path} ({result.node_id})")
        if result.status == "restored":
            restored += 1
        else:
            skipped += 1

    click.echo(f"Wave {wave}: {restored} restored, {skipped} skipped")


@main.command()
@project_root_option()
@click.option("--output", default="docs/requirements/", help="Output directory for generated requirements")
@click.option("--scope", default=None, help="Limit to a specific service boundary")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or merged CoDD defaults)",
)
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--feedback", default=None, help="Review feedback from previous generation")
@click.option(
    "--propagate",
    "propagate_changes",
    is_flag=True,
    default=False,
    help="Propagate requirements changes to dependent design docs via CEG",
)
@click.option(
    "--base",
    "base_ref",
    default=None,
    help="Base git ref for change detection (default: HEAD~1)",
)
@click.option(
    "--apply",
    "apply_mode",
    is_flag=True,
    default=False,
    help="Apply AI-generated update proposals to affected design docs",
)
@click.option(
    "--audit",
    is_flag=True,
    default=False,
    help="Run CoverageAuditor 3-class requirement gap analysis.",
)
@click.option(
    "--check",
    "check_coverage",
    is_flag=True,
    default=False,
    help="Check requirement-to-implementation coverage without generating requirements.",
)
@click.option(
    "--completeness-audit",
    is_flag=True,
    default=False,
    help="Audit requirement documents for completeness before deriving design artifacts.",
)
def require(
    path: str,
    output: str,
    scope: str | None,
    ai_cmd: str | None,
    force: bool,
    feedback: str | None,
    propagate_changes: bool,
    base_ref: str | None,
    apply_mode: bool,
    audit: bool,
    check_coverage: bool,
    completeness_audit: bool,
):
    """Infer requirements from extracted codebase facts (brownfield).

    Unlike 'restore' which reconstructs design docs from extracted facts,
    'require' reverse-engineers requirements documents from the same
    extracted code analysis. Run 'codd extract' first.
    """
    project_root = Path(path).resolve()

    if propagate_changes:
        from codd.require_propagate import require_propagate

        raise SystemExit(
            require_propagate(
                project_root,
                base_ref,
                apply=apply_mode,
                ai_command=ai_cmd,
            )
        )

    from codd.require import run_require

    _require_codd_dir(project_root)

    if check_coverage:
        _run_require_check(project_root)
        return

    if audit:
        from codd.coverage_auditor import CoverageAuditor

        output_dir = Path(output)
        if not output_dir.is_absolute():
            output_dir = project_root / output_dir
        report_path = output_dir / "coverage_audit_report.md"

        auditor = CoverageAuditor(project_root)
        result = auditor.audit()
        lexicon = load_lexicon(project_root)
        required_artifacts = lexicon.required_artifacts if lexicon is not None else []
        artifact_gaps = auditor.audit_required_artifacts(required_artifacts, project_root)
        report = auditor.generate_report(result)
        report += "\n" + auditor.generate_required_artifacts_report(
            required_artifacts,
            artifact_gaps,
            project_root,
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")

        ask_count = sum(1 for gap in artifact_gaps if gap.severity == "ASK")
        auto_reject_count = sum(1 for gap in artifact_gaps if gap.severity == "AUTO_REJECT")
        click.echo(f"Coverage audit complete: {result.summary()}")
        click.echo(
            "Required artifacts audit complete: "
            f"REQUIRED={len(required_artifacts)}, "
            f"ASK={ask_count}, "
            f"AUTO_REJECT={auto_reject_count}"
        )
        if artifact_gaps:
            click.echo("Missing required artifacts:")
            for gap in artifact_gaps:
                click.echo(f"  [{gap.severity}] {gap.artifact_id} - {gap.title}")
        else:
            click.echo("Required artifacts audit: ✅ All required artifacts present")
        click.echo(f"Report: {_display_path(report_path, project_root)}")
        return

    if completeness_audit:
        from codd.requirement_completeness_auditor import RequirementCompletenessAuditor

        auditor = RequirementCompletenessAuditor(
            project_root,
            ai_command=ai_cmd or "claude --print",
        )
        try:
            session = auditor.audit([])
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        blocking_asks = [
            item
            for item in session.ask_items
            if item.blocking and item.status == "ASK"
        ]
        proceeding = [
            item
            for item in session.ask_items
            if item.status == "RECOMMENDED_PROCEEDING"
        ]
        click.echo(
            "Requirement completeness audit complete: "
            f"ASK={len(session.ask_items)}, "
            f"RECOMMENDED_PROCEEDING={len(proceeding)}, "
            f"BLOCKING={len(blocking_asks)}"
        )
        click.echo(f"Decisions: {LEXICON_FILENAME}:coverage_decisions")
        raise SystemExit(1 if blocking_asks else 0)

    try:
        results = run_require(
            project_root,
            output_dir=output,
            scope=scope,
            ai_command=ai_cmd,
            force=force,
            feedback=feedback,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    generated = 0
    skipped = 0

    for result in results:
        try:
            rel_path = result.path.relative_to(project_root).as_posix()
        except ValueError:
            rel_path = result.path.as_posix()
        click.echo(f"{result.status.capitalize()}: {rel_path} ({result.node_id})")
        if result.status == "generated":
            generated += 1
        else:
            skipped += 1

    click.echo(f"Requirements: {generated} generated, {skipped} skipped")


def _run_require_check(project_root: Path) -> None:
    from codd.dag import runner as dag_runner

    try:
        results = dag_runner.run_all_checks(project_root, check_names=["implementation_coverage"])
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    failed = []
    for result in results:
        check_name = str(getattr(result, "check_name", "implementation_coverage"))
        message = str(getattr(result, "message", "") or "")
        passed = bool(getattr(result, "passed", False))
        status = "PASS" if passed else "FAIL"
        suffix = f" - {message}" if message else ""
        click.echo(f"{status}: {check_name}{suffix}")
        for violation in getattr(result, "violations", []) or []:
            click.echo(f"  - {violation}")
        if not passed:
            failed.append(result)

    if failed:
        raise SystemExit(1)
    click.echo("Requirement check complete: implementation_coverage PASS")


@main.group(invoke_without_command=True)
@click.option("--interactive", is_flag=True, default=False, help="Review findings inline and save approved items.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Discovery output format.",
)
@click.option("--lexicon", "lexicon_path", default=None, help="Lexicon directory, manifest path, or plug-in name.")
@project_root_option("project_path")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or CODD_AI_COMMAND).",
)
@click.pass_context
def elicit(
    ctx: click.Context,
    interactive: bool,
    output_format: str,
    lexicon_path: str | None,
    project_path: str,
    ai_cmd: str | None,
) -> None:
    """Discover and apply coverage/specification findings."""
    if ctx.invoked_subcommand is not None:
        return
    from codd.elicit.apply import ElicitApplyEngine
    from codd.elicit.engine import ElicitEngine
    from codd.elicit.formatters.interactive import InteractiveFormatter
    from codd.elicit.formatters.json_fmt import JsonFormatter
    from codd.elicit.formatters.md import MdFormatter

    project_root = Path(project_path).resolve()
    try:
        lexicon_config = _load_elicit_lexicon_configs(project_root, lexicon_path)
        elicit_result = ElicitEngine(ai_command=ai_cmd).run(project_root, lexicon_config=lexicon_config)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    findings = elicit_result.findings

    if interactive:
        formatter = InteractiveFormatter()
        approved_ids = set(formatter.collect_approvals(findings))
        approved = [finding for finding in findings if finding.id in approved_ids]
        try:
            result = ElicitApplyEngine(project_root).apply(approved)
        except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        click.echo(f"Elicit interactive complete: approved={len(approved)}, skipped={len(findings) - len(approved)}")
        for file_path in result.files_updated:
            click.echo(f"Updated: {file_path}")
        return

    if output_format == "json":
        click.echo(JsonFormatter().format(elicit_result), nl=False)
        return

    output_path = project_root / "findings.md"
    output_path.write_text(MdFormatter().format(elicit_result), encoding="utf-8")
    coverage_summary = ""
    if elicit_result.lexicon_coverage_report:
        gap_count = sum(
            1 for status in elicit_result.lexicon_coverage_report.values() if str(status).lower() == "gap"
        )
        coverage_summary = f", coverage_categories={len(elicit_result.lexicon_coverage_report)}, gaps={gap_count}"
    click.echo(
        f"Elicit discovery complete: findings={len(findings)}, all_covered={elicit_result.all_covered}"
        f"{coverage_summary}"
    )
    click.echo(f"Output: {_display_path(output_path, project_root)}")


def _load_elicit_lexicon_configs(project_root: Path, lexicon_path: str | None):
    selectors = _split_elicit_lexicon_selectors(lexicon_path) if lexicon_path else load_project_extends(project_root)
    if not selectors:
        return None
    configs = [_load_elicit_lexicon(project_root, selector) for selector in selectors]
    return configs[0] if len(configs) == 1 else configs


def _split_elicit_lexicon_selectors(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_elicit_lexicon(project_root: Path, lexicon_path: str):
    from codd.elicit.lexicon_loader import load_lexicon

    return load_lexicon(_resolve_elicit_lexicon_path(project_root, lexicon_path))


def _resolve_elicit_lexicon_path(project_root: Path, lexicon_path: str) -> Path:
    raw_path = Path(lexicon_path).expanduser()
    candidates: list[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend(
            [
                project_root / raw_path,
                Path.cwd() / raw_path,
                Path(__file__).resolve().parents[1] / "codd_plugins" / "lexicons" / lexicon_path,
                raw_path,
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return raw_path


@elicit.command("apply")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["auto", "md", "json"]),
    default="auto",
    show_default=True,
    help="Input format. Auto uses the file extension.",
)
@project_root_option("project_path")
def elicit_apply_cmd(input_file: Path, output_format: str, project_path: str) -> None:
    """Apply approved elicit findings to project state."""
    from codd.elicit.apply import ElicitApplyEngine, load_findings_from_file

    project_root = Path(project_path).resolve()
    try:
        findings = load_findings_from_file(input_file, None if output_format == "auto" else output_format)
        result = ElicitApplyEngine(project_root).apply(findings)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Elicit apply complete: applied={result.applied_count}, skipped={result.skipped_count}")
    for file_path in result.files_updated:
        click.echo(f"Updated: {file_path}")


@main.command("brownfield")
@click.argument("target_path", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--requirements",
    "requirements_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Requirements file (default: <target>/.codd/requirements.md; skipped if absent).",
)
@click.option(
    "--lexicon",
    "lexicon_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Lexicon directory or manifest (default: <target>/.codd/lexicon; discovery mode if absent).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Integrated report format.",
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output report file.")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command for diff and elicit engines.",
)
def brownfield_cmd(
    target_path: Path,
    requirements_path: Path | None,
    lexicon_path: Path | None,
    output_format: str,
    output: Path | None,
    ai_cmd: str | None,
) -> None:
    """Run brownfield extract, diff, elicit, and merged reporting."""
    from codd.brownfield.pipeline import BrownfieldPipeline, format_brownfield_result

    project_root = target_path.expanduser().resolve()
    try:
        result = BrownfieldPipeline(ai_command=ai_cmd).run(
            project_root,
            requirements_path=requirements_path,
            lexicon_path=lexicon_path,
        )
        formatted = format_brownfield_result(result, output_format)
        output_path = _project_file(
            project_root,
            output,
            f".codd/brownfield_report.{output_format}",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(formatted, encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(
        "Brownfield pipeline complete: "
        f"diff_findings={len(result.diff_findings)}, "
        f"elicit_findings={len(result.elicit_findings)}, "
        f"merged_findings={len(result.merged_findings)}"
    )
    click.echo(f"Output: {_display_path(output_path, project_root)}")


@main.command("greenfield")
@project_root_option("project_path")
@click.option("--project-name", default=None, help="Project name for codd init (when not yet initialized)")
@click.option("--language", default=None, help="Primary language for codd init (when not yet initialized)")
@click.option(
    "--requirements",
    default=None,
    type=click.Path(exists=True),
    help="Requirements document to import (any format; CoDD adds frontmatter automatically)",
)
@click.option(
    "--project-type",
    default=None,
    help=(
        "Project type for capability-aware generation (cli, web, mobile, iot, generic, or a "
        "project-local custom profile). Recorded in codd.yaml (required_artifacts.project_type) "
        "and in the session for --resume. Unset = legacy web-style fallback."
    ),
)
@click.option("--ai-cmd", default=None, help="Override AI CLI command for every stage of this run")
@click.option(
    "--elicit/--no-elicit",
    "elicit_enabled",
    default=None,
    help="Run the advisory elicit+apply stage (default: greenfield.elicit config, true)",
)
@click.option(
    "--max-repair-attempts",
    default=None,
    type=click.IntRange(min=1),
    help="Maximum automatic repair attempts during verify (default: greenfield.max_repair_attempts, 10)",
)
@click.option(
    "--no-coverage-gate",
    is_flag=True,
    default=False,
    help="Skip the verifiable-behavior coverage gate after each implement task",
)
@click.option("--resume", is_flag=True, default=False, help="Resume from .codd/greenfield_session.yaml")
@click.option("--dry-run", is_flag=True, default=False, help="Print the resolved execution plan without invoking AI")
@click.option(
    "--ntfy-topic",
    default=None,
    help="ntfy topic (or full URL) for progress notifications — notify-only, never blocking",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Result report format",
)
def greenfield_cmd(
    project_path: str,
    project_name: str | None,
    language: str | None,
    requirements: str | None,
    project_type: str | None,
    ai_cmd: str | None,
    elicit_enabled: bool | None,
    max_repair_attempts: int | None,
    no_coverage_gate: bool,
    resume: bool,
    dry_run: bool,
    ntfy_topic: str | None,
    output_format: str,
) -> None:
    """Run the unattended greenfield autopilot: requirements in, system out.

    Write a requirements document; CoDD builds the system. Unattended: all
    gates are auto-approved (elicit findings applied, derived tasks and
    implementation steps approved, repair runs in automatic mode); progress is
    optionally posted to ntfy (notify-only, never blocking).

    \b
    Stages: init → elicit (advisory) → plan --init → generate (all waves)
    → implement (all tasks) → verify --auto-repair → propagate --commit
    → check. Checkpoints land in .codd/greenfield_session.yaml after every
    unit; resume an interrupted or failed run with 'codd greenfield --resume'.
    """
    from codd.greenfield.pipeline import GreenfieldPipeline, format_greenfield_result

    pipeline = GreenfieldPipeline(
        project_name=project_name,
        language=language,
        requirements=requirements,
        project_type=project_type,
        ai_command=ai_cmd,
        elicit=elicit_enabled,
        max_repair_attempts=max_repair_attempts,
        coverage_gate=False if no_coverage_gate else None,
        ntfy_topic=ntfy_topic,
        echo=click.echo,
    )
    try:
        result = pipeline.run(Path(project_path).resolve(), resume=resume, dry_run=dry_run)
    except (FileNotFoundError, NotADirectoryError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(format_greenfield_result(result, output_format), nl=False)
    if result.status == "failed":
        raise SystemExit(1)


@main.group("diff", invoke_without_command=True)
@click.option("--extract-input", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--requirements", "requirements_path", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Discovery output format.",
)
@click.option("--interactive", is_flag=True, default=False, help="Review findings inline and apply approved items.")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
@project_root_option("project_path")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or CODD_AI_COMMAND).",
)
@click.pass_context
def diff_cmd(
    ctx: click.Context,
    extract_input: Path | None,
    requirements_path: Path | None,
    output_format: str,
    interactive: bool,
    output: Path | None,
    project_path: str,
    ai_cmd: str | None,
) -> None:
    """Compare extracted implementation facts with requirements."""
    if ctx.invoked_subcommand is not None:
        return
    from codd.diff.apply import DiffApplyEngine
    from codd.diff.persistence import append_history, load_ignored
    from codd.elicit.formatters.interactive import InteractiveFormatter
    from codd.elicit.formatters.json_fmt import JsonFormatter
    from codd.elicit.formatters.md import MdFormatter

    project_root = Path(project_path).resolve()
    extract_path = _resolve_diff_extract_input(project_root, extract_input)
    req_path = _project_file(project_root, requirements_path, "docs/requirements/requirements.md")
    output_path = _project_file(project_root, output, "drift_findings.md") if output is not None or output_format == "md" else None

    try:
        from codd.diff.engine import DiffEngine

        engine = _build_diff_engine(DiffEngine, ai_cmd, project_root)
        findings = _run_diff_engine(
            engine,
            extract_input=extract_path,
            requirements_path=req_path,
            ignored_findings=load_ignored(project_root),
        )
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if interactive:
        formatter = InteractiveFormatter()
        approved_ids = set(formatter.collect_approvals(findings))
        approved = [finding for finding in findings if finding.id in approved_ids]
        try:
            result = DiffApplyEngine(project_root).apply(approved)
        except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        click.echo(f"Diff interactive complete: approved={len(approved)}, skipped={len(findings) - len(approved)}")
        for file_path in result.files_updated:
            click.echo(f"Updated: {file_path}")
        return

    formatted = JsonFormatter().format(findings) if output_format == "json" else MdFormatter().format(findings)
    if output_path is None:
        click.echo(formatted, nl=False)
        return

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(formatted, encoding="utf-8")
        append_history(
            project_root,
            {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "extract_input": _display_path(extract_path, project_root),
                "requirements_path": _display_path(req_path, project_root),
                "findings_total": len(findings),
                "output_path": _display_path(output_path, project_root),
            },
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Diff discovery complete: findings={len(findings)}")
    click.echo(f"Output: {_display_path(output_path, project_root)}")


@diff_cmd.command("apply")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["auto", "md", "json"]),
    default="auto",
    show_default=True,
    help="Input format. Auto uses the file extension.",
)
@project_root_option("project_path")
def diff_apply_cmd(input_file: Path, output_format: str, project_path: str) -> None:
    """Apply approved comparison findings to project artifacts."""
    from codd.diff.apply import DiffApplyEngine, load_findings_from_file

    project_root = Path(project_path).resolve()
    try:
        findings = load_findings_from_file(input_file, None if output_format == "auto" else output_format)
        result = DiffApplyEngine(project_root).apply(findings)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Diff apply complete: applied={result.applied_count}, skipped={result.skipped_count}")
    for file_path in result.files_updated:
        click.echo(f"Updated: {file_path}")


def _project_file(project_root: Path, value: Path | None, default: str) -> Path:
    path = Path(default) if value is None else value.expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _resolve_diff_extract_input(project_root: Path, value: Path | None) -> Path:
    """Locate the extract-input file, preferring isolated `.codd/extract/` output.

    Order of resolution:
    1. Explicit ``--extract-input`` value (absolute or project-relative).
    2. ``.codd/extract/extracted.md`` (default Issue #17 isolation target).
    3. Top-level ``extracted.md`` aggregated output if present.
    4. First ``.codd/extract/modules/*.md`` module file (deterministic by name).
    5. Legacy fallback ``codd/extracted.md`` (preserved for older projects).
    """
    if value is not None:
        candidate = value.expanduser()
        return candidate if candidate.is_absolute() else project_root / candidate

    candidates: list[Path] = [
        project_root / ".codd" / "extract" / "extracted.md",
        project_root / "extracted.md",
    ]
    modules_dir = project_root / ".codd" / "extract" / "modules"
    if modules_dir.is_dir():
        module_files = sorted(modules_dir.glob("*.md"))
        if module_files:
            candidates.append(module_files[0])
    candidates.append(project_root / "codd" / "extracted.md")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _build_diff_engine(engine_cls: Any, ai_cmd: str | None, project_root: Path) -> Any:
    attempts = (
        lambda: engine_cls(llm_client=ai_cmd, project_root=project_root),
        lambda: engine_cls(ai_command=ai_cmd, project_root=project_root),
        lambda: engine_cls(ai_cmd, project_root),
        lambda: engine_cls(project_root=project_root),
        lambda: engine_cls(),
    )
    last_error: TypeError | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise TypeError("Could not initialize comparison engine")


def _run_diff_engine(
    engine: Any,
    *,
    extract_input: Path,
    requirements_path: Path,
    ignored_findings: set[str],
) -> list[Any]:
    try:
        return list(
            engine.run_diff(
                extract_input=extract_input,
                requirements_path=requirements_path,
                ignored_findings=ignored_findings,
            )
        )
    except TypeError:
        return list(engine.run_diff(extract_input, requirements_path, ignored_findings))


@main.command()
@click.option("--diff", default="HEAD", help="Git diff target (default: HEAD, shows uncommitted changes)")
@project_root_option()
@click.option("--update", is_flag=True, help="Actually update affected design docs via AI")
@click.option("--verify", is_flag=True, help="Auto-apply green band, list amber/gray for HITL review")
@click.option("--commit", "do_commit", is_flag=True, help="Commit HITL changes and record knowledge")
@click.option("--reason", default=None, help="Default reason for all HITL corrections (recorded as knowledge)")
@click.option("--reason-file", default=None, type=click.Path(exists=True),
              help="JSON file with per-file reasons: {\"path\": \"reason\", ...}")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command)",
)
@click.option("--feedback", default=None, help="Review feedback to address in this update (from codd review)")
@click.option(
    "--coherence",
    is_flag=True,
    default=False,
    help="Inject lexicon and DESIGN.md context into propagation prompt",
)
@click.option(
    "--reverse",
    is_flag=True,
    default=False,
    help="Reverse propagation: DESIGN.md/lexicon changes to existing implementation",
)
@click.option(
    "--source",
    type=click.Choice(["design_token", "lexicon"]),
    default="design_token",
    show_default=True,
    help="Source of reverse propagation",
)
@click.option(
    "--base",
    "base_ref",
    default=None,
    help="Base git ref for change detection (default: HEAD~1)",
)
@click.option(
    "--apply",
    "apply_reverse",
    is_flag=True,
    default=False,
    help="Apply safe reverse propagation replacements (default is dry-run)",
)
def propagate(diff: str, path: str, update: bool, verify: bool, do_commit: bool,
              reason: str | None, reason_file: str | None,
              ai_cmd: str | None, feedback: str | None, coherence: bool,
              reverse: bool, source: str, base_ref: str | None, apply_reverse: bool):
    """Propagate source code changes to design documents.

    Detects changed source files, maps them to modules, and finds design
    documents covering those modules via the 'modules' frontmatter field.

    \b
    Modes:
      (default)    analysis only — shows which docs are affected
      --update     update ALL affected docs via AI (no band filtering)
      --verify     auto-apply green band via AI, list amber/gray for HITL
      --commit     after HITL review, commit changes and record knowledge
    """
    project_root = Path(path).resolve()
    _require_codd_dir(project_root)
    coherence_context = _load_coherence_context(project_root) if coherence else None

    if reverse:
        from codd.propagator import propagate_reverse

        try:
            exit_code = propagate_reverse(
                project_root,
                source=source,
                base_ref=base_ref,
                apply=apply_reverse,
            )
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        if exit_code:
            raise SystemExit(exit_code)
        return

    if apply_reverse:
        click.echo("Error: --apply is only valid with --reverse.")
        raise SystemExit(1)

    # Mutual exclusivity check
    mode_count = sum([update, verify, do_commit])
    if mode_count > 1:
        click.echo("Error: --update, --verify, and --commit are mutually exclusive.")
        raise SystemExit(1)

    # --commit mode
    if do_commit:
        import json as _json
        from codd.propagator import run_commit

        # Load per-file reasons if provided
        reason_map = None
        if reason_file:
            reason_map = _json.loads(Path(reason_file).read_text(encoding="utf-8"))

        try:
            result = run_commit(project_root, reason=reason, reason_map=reason_map)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        if result.committed_files:
            click.echo(f"Committed {len(result.committed_files)} HITL-reviewed file(s).")
            for f in result.committed_files:
                click.echo(f"  {f}")
        else:
            click.echo("No HITL changes detected.")

        if result.knowledge_recorded:
            click.echo(f"Knowledge recorded: {result.knowledge_recorded} evidence entries added.")
        click.echo("Propagation committed.")
        return

    # --verify mode
    if verify:
        from codd.propagator import run_verify

        try:
            result = run_verify(
                project_root,
                diff,
                ai_command=ai_cmd,
                feedback=feedback,
                coherence_context=coherence_context,
            )
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        if not result.changed_files:
            click.echo("No changed files detected.")
            return

        click.echo(f"Changed files: {len(result.changed_files)}")

        if result.file_module_map:
            click.echo(f"\nSource changes → modules:")
            for f, m in sorted(result.file_module_map.items()):
                click.echo(f"  {f} → {m}")

        if not result.auto_applied and not result.needs_hitl and not getattr(result, 'affected_docs', None):
            if result.file_module_map:
                click.echo("\nNo design docs found covering changed modules.")
                click.echo("(Design docs need a 'modules' field in frontmatter to be tracked.)")
            else:
                click.echo("No affected design docs found (no source or doc changes matched).")
            return

        # Auto-applied (green band)
        if result.auto_applied:
            click.echo(f"\n✅ Auto-applied (green band): {len(result.auto_applied)}")
            for vdoc in result.auto_applied:
                status = "UPDATED" if vdoc.doc.node_id in result.updated else "FAILED"
                click.echo(f"  [{status}] {vdoc.doc.path} ({vdoc.doc.node_id})")
                click.echo(f"    confidence: {vdoc.confidence:.2f}, evidence: {vdoc.evidence_count}")

        # HITL required (amber/gray)
        if result.needs_hitl:
            click.echo(f"\n🔶 Needs HITL review: {len(result.needs_hitl)}")
            for vdoc in result.needs_hitl:
                click.echo(f"  [{vdoc.band}] {vdoc.doc.path} ({vdoc.doc.node_id})")
                click.echo(f"    confidence: {vdoc.confidence:.2f}, evidence: {vdoc.evidence_count}")
                click.echo(f"    modules: {', '.join(vdoc.doc.matched_modules)}")

            click.echo(f"\nReview the docs above, then run:")
            click.echo(f"  codd propagate --commit --reason \"<why you changed it>\"")

        return

    # Default / --update mode (existing behavior)
    from codd.propagator import run_propagate

    try:
        result = run_propagate(
            project_root,
            diff,
            update=update,
            ai_command=ai_cmd,
            feedback=feedback,
            coherence_context=coherence_context,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if not result.changed_files:
        click.echo("No changed files detected.")
        return

    click.echo(f"Changed files: {len(result.changed_files)}")
    if result.file_module_map:
        click.echo(f"\nSource changes → modules:")
        for f, m in sorted(result.file_module_map.items()):
            click.echo(f"  {f} → {m}")

    if not result.affected_docs:
        if result.file_module_map:
            click.echo("\nNo design docs found covering changed modules.")
            click.echo("(Design docs need a 'modules' field in frontmatter to be tracked.)")
        else:
            click.echo("No affected design docs found (no source or doc changes matched).")
        return

    click.echo(f"\nAffected design docs: {len(result.affected_docs)}")
    for doc in result.affected_docs:
        status = "UPDATED" if doc.node_id in result.updated else "needs review"
        click.echo(f"  [{status}] {doc.path} ({doc.node_id})")
        click.echo(f"    modules: {', '.join(doc.matched_modules)}")

    if not update and result.affected_docs:
        click.echo(f"\nRun with --update to update these docs via AI.")


@main.command("propagate-from")
@project_root_option("project_path")
@click.option("--files", multiple=True, required=True, help="Changed file path. Can be repeated.")
@click.option(
    "--source",
    default="manual",
    show_default=True,
    type=click.Choice(["watch", "git_hook", "editor_hook", "manual"]),
    help="Change source that triggered propagation.",
)
@click.option(
    "--editor",
    default=None,
    type=click.Choice(["claude", "codex", "manual"]),
    help="Editor that produced the change, when known.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Compute impact without propagate/fix/log writes.")
def propagate_from(project_path: str, files: tuple[str, ...], source: str, editor: str | None, dry_run: bool):
    """Run the CDAP propagation pipeline from changed files."""
    from codd.watch.events import FileChangeEvent
    from codd.watch.propagation_pipeline import run_propagation_pipeline

    project_root = Path(project_path).resolve()
    event = FileChangeEvent(files=list(files), source=source, editor=editor)
    result = run_propagation_pipeline(project_root, list(files), dry_run=dry_run, event=event)

    click.echo(f"Impacted nodes: {len(result.impacted_nodes)}")
    click.echo(f"Propagated: {result.propagated_count}")
    click.echo(f"Fixed: {result.fixed_count}")
    if result.errors:
        click.echo(f"Errors: {result.errors}", err=True)

    if not result.success:
        raise SystemExit(1)


@main.group(invoke_without_command=True)
@project_root_option()
@click.option("--design", default=None, help="Design document path or design node id to implement")
@click.option("--output", "outputs", multiple=True, help="Output path. May be repeated.")
@click.option("--depends-on", "depends_on", multiple=True, help="Dependency design document path or node id. May be repeated.")
@click.option("--clean", is_flag=True, default=False, help="Remove existing generated output before re-generating")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or merged CoDD defaults)",
)
@click.option("--use-derived-steps", default=None, help="Inject derived implementation steps: true or false")
@click.option(
    "--no-coverage-gate",
    is_flag=True,
    default=False,
    help="Skip the verifiable-behavior (VB) coverage gate after test-related implementation",
)
@click.option(
    "--no-contract-gate",
    is_flag=True,
    default=False,
    help="Skip the artifact-contract completion gate for the 'implement' stage",
)
@click.pass_context
def implement(
    ctx,
    path: str,
    design: str | None,
    outputs: tuple[str, ...],
    depends_on: tuple[str, ...],
    clean: bool,
    ai_cmd: str | None,
    use_derived_steps: str | None,
    no_coverage_gate: bool,
    no_contract_gate: bool,
):
    """Generate implementation code from one design document."""
    if ctx.invoked_subcommand is not None:
        return

    from codd.implementer import implement_tasks

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    if clean:
        click.echo("Cleaning requested output paths ...")

    try:
        implement_kwargs = {
            "design": design,
            "output_paths": list(outputs),
            "dependency_design_nodes": list(depends_on),
            "ai_command": ai_cmd,
            "clean": clean,
        }
        parsed_use_derived_steps = _optional_bool(use_derived_steps)
        if parsed_use_derived_steps is not None:
            implement_kwargs["use_derived_steps"] = parsed_use_derived_steps
        results = implement_tasks(project_root, **implement_kwargs)
    except (FileNotFoundError, ValueError, CoddCLIError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    generated_files = 0
    failed_tasks = []
    for result in results:
        if result.error:
            failed_tasks.append(result)
            continue
        for generated_file in result.generated_files:
            rel_path = generated_file.relative_to(project_root)
            click.echo(f"Generated: {rel_path} ({result.task_id})")
            generated_files += 1

    succeeded = len(results) - len(failed_tasks)
    click.echo(f"{generated_files} files generated across {succeeded} task(s)")

    if failed_tasks:
        click.echo(click.style(
            f"\nFAILED: {len(failed_tasks)} task(s) produced no files:",
            fg="red", bold=True,
        ))
        for ft in failed_tasks:
            click.echo(click.style(f"  ✗ {ft.task_id} ({ft.task_title}): {ft.error}", fg="red"))
        raise SystemExit(1)

    def _retry_with_feedback(feedback: str) -> None:
        implement_tasks(project_root, **{**implement_kwargs, "clean": False, "feedback": feedback})

    _enforce_implement_coverage_gate(
        project_root,
        design_node=design,
        results=results,
        opt_out=no_coverage_gate,
        rerun=_retry_with_feedback,
    )

    _enforce_stage_contract_gate(project_root, "implement", opt_out=no_contract_gate)


def _enforce_implement_coverage_gate(
    project_root: Path,
    *,
    design_node: str | None,
    results: list[Any] | None = None,
    output_paths: list[str] | None = None,
    opt_out: bool = False,
    rerun: Any = None,
) -> None:
    """Run the verifiable-behavior coverage gate after an implement run.

    Applies only when the run targets test artifacts (output under a test dir
    or a test-type design node). Uncovered VBs trigger bounded re-implementation
    with gap feedback; remaining gaps are reported on stderr with a non-zero
    exit. Projects without a VB table get a one-line notice and pass.
    """

    from codd.verifiable_behavior_audit import run_implement_coverage_gate

    resolved_paths: list[str] = list(output_paths or [])
    for result in results or []:
        for item in getattr(result, "output_paths", []) or []:
            try:
                resolved_paths.append(Path(item).resolve().relative_to(project_root).as_posix())
            except ValueError:
                resolved_paths.append(str(item))

    config = _load_optional_project_config(project_root)
    passed = run_implement_coverage_gate(
        project_root,
        config=config,
        design_node=design_node,
        output_paths=resolved_paths,
        opt_out=opt_out,
        rerun=rerun,
        echo=click.echo,
        echo_error=lambda message: click.echo(message, err=True),
    )
    if not passed:
        raise SystemExit(1)


def _emit_stage_contract_result(stage_report) -> None:
    """Uniform printer for an artifact-contract stage gate result.

    ``stage_report`` is the :class:`StageReport` returned by
    :func:`codd.artifact_contract.enforce_stage_completion`. On success a single
    confirmation line is printed; on failure each missing/invalid artifact is
    surfaced on stderr. Printing only — exit handling is the caller's job.
    """

    checks = stage_report.checks
    total = len(checks)
    if stage_report.passed:
        click.echo(
            f"stage '{stage_report.stage}' contract satisfied: {total}/{total} artifact(s)"
        )
        return

    failures = stage_report.failures
    click.echo(
        click.style(
            f"stage '{stage_report.stage}' INCOMPLETE: "
            f"{len(failures)}/{total} required artifact(s) missing/invalid",
            fg="red",
            bold=True,
        ),
        err=True,
    )
    for check in failures:
        suffix = f" — {check.detail}" if check.detail else ""
        paths = f" ({', '.join(check.matched_paths)})" if check.matched_paths else ""
        click.echo(
            click.style(f"  {check.status.upper()}  {check.artifact_id}{paths}{suffix}", fg="red"),
            err=True,
        )


def _enforce_stage_contract_gate(
    project_root: Path,
    stage: str,
    *,
    opt_out: bool = False,
) -> None:
    """Gate a pipeline stage's completion on its declared artifact contract.

    Opt-in / non-breaking: when the project has no enabled contract, or the
    stage is not declared, or ``opt_out`` is set (or config disables stage
    gating), this is a no-op. When the contract is enabled for the stage and a
    required artifact is missing/invalid, the stage is NOT complete: a clear
    message is printed and the process exits non-zero.
    """

    if opt_out:
        return

    from codd.artifact_contract import enforce_stage_completion

    config = _load_optional_project_config(project_root)
    if not _stage_gate_enabled(config):
        return

    try:
        stage_report = enforce_stage_completion(project_root, stage, config=config)
    except Exception:  # never let the gate break a stage when it cannot evaluate
        return
    if stage_report is None:
        return

    _emit_stage_contract_result(stage_report)
    if not stage_report.passed:
        raise SystemExit(1)


def _stage_gate_enabled(config: dict[str, Any]) -> bool:
    """Honor an opt-out via ``artifact_contract.gate_stages: false`` in config.

    Default (key absent) is to gate when the contract is enabled; setting it to
    a falsey value turns the completion gate off project-wide while leaving
    ``codd contract verify`` available on demand.
    """

    section = config.get("artifact_contract") if isinstance(config, dict) else None
    if isinstance(section, dict) and "gate_stages" in section:
        return bool(section.get("gate_stages"))
    return True


@implement.command("plan")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--design-doc", "design_docs", multiple=True, help="Design document path. May be repeated.")
@click.option("--force", is_flag=True, help="Bypass cached implementation steps")
@click.option("--dry-run", is_flag=True, help="Print derived steps without writing cache")
@project_root_option("project_path")
@click.option("--provider", default=None, help="Implementation step deriver provider name")
@click.option("--ai-cmd", default=None, help="Override AI command")
def implement_plan_cmd(
    task_id: str,
    design_docs: tuple[str, ...],
    force: bool,
    dry_run: bool,
    project_path: str,
    provider: str | None,
    ai_cmd: str | None,
):
    """Derive implementation steps for one task."""
    from codd.deployment.providers.ai_command_factory import get_ai_command
    from codd.llm.impl_step_deriver import IMPL_STEP_DERIVERS

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    config = _load_optional_project_config(project_root)
    provider_name = provider or _impl_step_provider(config)
    deriver_cls = IMPL_STEP_DERIVERS.get(provider_name)
    if deriver_cls is None:
        click.echo(f"Error: implementation step deriver provider not found: {provider_name}")
        raise SystemExit(1)

    try:
        task_item = _implement_task_for_cli(project_root, config, task_id)
        nodes = _plan_design_doc_nodes(project_root, design_docs)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    command = ai_cmd or _impl_step_command(config)
    deriver = deriver_cls(get_ai_command(config, project_root, command_override=command))
    steps = deriver.derive_steps(
        task_item,
        nodes,
        {
            "project_root": project_root,
            "force": force,
            "dry_run": dry_run,
            "write_cache": not dry_run,
            "config": config,
            "project_context": {"project": config.get("project", {})},
        },
    )
    if dry_run:
        click.echo(yaml.safe_dump([step.to_dict() for step in steps], sort_keys=False, allow_unicode=True), nl=False)
        return
    click.echo(f"Derived implementation steps: {len(steps)}")


@implement.command("steps")
@click.option("--task", "task_id", required=True, help="Implementation task id")
@click.option("--approve", is_flag=True, help="Approve one or more derived steps")
@click.option("--step", "step_id", default=None, help="Step id for --approve")
@click.option("--all", "approve_all", is_flag=True, help="Approve all pending steps")
@click.option("--show-only", is_flag=True, help="Only show cached steps")
@click.option("--show-layer-breakdown", is_flag=True, help="Show explicit and inferred step groups")
@project_root_option("project_path")
def implement_steps_cmd(
    task_id: str,
    approve: bool,
    step_id: str | None,
    approve_all: bool,
    show_only: bool,
    show_layer_breakdown: bool,
    project_path: str,
):
    """Show or approve derived implementation steps."""
    from codd.llm.impl_step_deriver import approve_cached_impl_steps, impl_step_cache_path, read_impl_step_cache

    project_root = Path(project_path).resolve()
    cache_path = impl_step_cache_path(task_id, {"project_root": project_root})
    if approve:
        if not approve_all and not step_id:
            click.echo("Error: --approve requires --step or --all")
            raise SystemExit(2)
        try:
            changed = approve_cached_impl_steps(cache_path, step_id=step_id, approve_all=approve_all)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        click.echo(f"Approved implementation steps: {changed}")
        if not show_only:
            return

    record = read_impl_step_cache(cache_path)
    if record is None:
        click.echo("No derived implementation steps found")
        return
    if show_layer_breakdown:
        _echo_impl_step_layer_breakdown(record)
        return
    for step in record.steps:
        layer = "layer2" if step.inferred else "layer1"
        status = "approved" if step.approved else "pending"
        click.echo(f"{step.id}\t{status}\t{layer}\t{step.kind}\t{step.source_design_section}")


def _echo_impl_step_layer_breakdown(record: Any) -> None:
    layer_1 = [step for step in record.steps if not step.inferred]
    layer_2 = [step for step in record.steps if step.inferred]
    click.echo(f"[Layer 1 - Explicit, from design] (count={len(layer_1)})")
    for step in layer_1:
        click.echo(f"  - {step.kind}: {step.id} (rationale: {step.rationale})")

    avg_confidence = sum(float(step.confidence) for step in layer_2) / len(layer_2) if layer_2 else 0.0
    click.echo("")
    click.echo(f"[Layer 2 - Best Practice Augment] (count={len(layer_2)}, avg_confidence={avg_confidence:.2f})")
    for step in layer_2:
        category = step.best_practice_category or "uncategorized"
        click.echo(
            f"  - {step.kind}: {step.id} "
            f"(confidence={step.confidence:.2f}, category={category}, rationale: {step.rationale})"
        )


@implement.command("list-tasks")
@project_root_option("project_path")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format",
)
def implement_list_tasks_cmd(project_path: str, output_format: str) -> None:
    """List all implement tasks deterministically.

    Sources, in precedence order: configured targets
    (implement.default_output_paths / implement_targets in codd.yaml), then
    approved derived tasks (.codd/derived_tasks). Generalizes the implement-run
    auto-detection from "fail when multiple candidates" to "list every
    candidate" — useful for scripting (one task id per line).
    """
    from codd.implementer import list_implement_tasks

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    try:
        tasks = list_implement_tasks(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if output_format == "json":
        click.echo(json.dumps(tasks, ensure_ascii=False, indent=2))
        return
    if not tasks:
        click.echo(
            "No implement tasks found (configure implement.default_output_paths "
            "in codd.yaml or approve derived tasks via 'codd plan derive')."
        )
        return
    for task in tasks:
        click.echo(task["task_id"])


@implement.command("augment")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--design-doc", "design_docs", multiple=True, help="Design document path. May be repeated.")
@project_root_option("project_path")
@click.option("--provider", default=None, help="Best practice augmenter provider name")
@click.option("--ai-cmd", default=None, help="Override AI command")
def implement_augment_cmd(
    task_id: str,
    design_docs: tuple[str, ...],
    project_path: str,
    provider: str | None,
    ai_cmd: str | None,
):
    """Suggest inferred implementation steps and merge them into the task cache."""
    from codd.deployment.providers.ai_command_factory import get_ai_command
    from codd.llm.best_practice_augmenter import BEST_PRACTICE_AUGMENTERS
    from codd.llm.impl_step_deriver import (
        ImplStepCacheRecord,
        impl_step_cache_path,
        merge_impl_steps,
        read_impl_step_cache,
        utc_timestamp,
        write_impl_step_cache,
    )

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    config = _load_optional_project_config(project_root)
    cache_path = impl_step_cache_path(task_id, {"project_root": project_root})
    record = read_impl_step_cache(cache_path)
    if record is None:
        click.echo("Error: derive Layer 1 steps before augmenting")
        raise SystemExit(1)

    provider_name = provider or _best_practice_provider(config)
    augmenter_cls = BEST_PRACTICE_AUGMENTERS.get(provider_name)
    if augmenter_cls is None:
        click.echo(f"Error: best practice augmenter provider not found: {provider_name}")
        raise SystemExit(1)

    try:
        task_item = _implement_task_for_cli(project_root, config, task_id)
        docs = design_docs or tuple(record.design_docs)
        nodes = _plan_design_doc_nodes(project_root, docs)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    command = ai_cmd or _best_practice_command(config)
    augmenter = augmenter_cls(get_ai_command(config, project_root, command_override=command))
    explicit = [step for step in record.steps if not step.inferred]
    implicit = augmenter.suggest_implicit_steps(
        task_item,
        nodes,
        explicit,
        {"project_root": project_root, "config": config, "project_context": {"project": config.get("project", {})}},
    )
    merged = merge_impl_steps(explicit, implicit)
    write_impl_step_cache(
        cache_path,
        ImplStepCacheRecord(
            provider_id=record.provider_id,
            cache_key=f"{record.cache_key}:augmented",
            task_id=record.task_id,
            design_doc_sha=record.design_doc_sha,
            prompt_template_sha=record.prompt_template_sha,
            generated_at=utc_timestamp(),
            design_docs=record.design_docs,
            steps=merged,
        ),
    )
    click.echo(f"Augmented implementation steps: {len(implicit)}")


@implement.command("run")
@click.option("--task", "task_id", default=None, help="Generate only one task by task ID or title match")
@project_root_option("project_path")
@click.option("--ai-cmd", default=None, help="Override AI CLI command")
@click.option("--use-derived-steps", default="true", help="Inject derived implementation steps: true or false")
@click.option("--chunk-size", default=None, type=click.IntRange(min=1), help="Run derived steps in chunks of this size")
@click.option(
    "--timeout-per-chunk",
    default=600,
    type=click.IntRange(min=1),
    show_default=True,
    help="Seconds before one chunk is interrupted",
)
@click.option("--enable-typecheck-loop", is_flag=True, default=False, help="Run configured typecheck repair loop after implementation")
@click.option(
    "--language",
    default=None,
    help=(
        "Override project.language for this invocation only. Useful when "
        "`codd init --language js` was chosen but a downstream spec/design "
        "doc requires TypeScript (Issue #20). codd.yaml is not modified."
    ),
)
@click.option(
    "--no-coverage-gate",
    is_flag=True,
    default=False,
    help="Skip the verifiable-behavior (VB) coverage gate after test-related implementation",
)
def implement_run_cmd(
    task_id: str | None,
    project_path: str,
    ai_cmd: str | None,
    use_derived_steps: str,
    chunk_size: int | None,
    timeout_per_chunk: int,
    enable_typecheck_loop: bool,
    language: str | None,
    no_coverage_gate: bool,
):
    """Run implementation with optional derived step injection."""
    from codd.implementer import auto_detect_task, implement_tasks

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    if task_id is None:
        try:
            task_id = auto_detect_task(project_root)
        except ValueError as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        click.echo(f"Auto-detected task: {task_id}")

    if chunk_size is not None:
        try:
            result = _run_chunked_implementation(
                project_root=project_root,
                task_id=task_id,
                ai_cmd=ai_cmd,
                chunk_size=chunk_size,
                timeout_per_chunk=timeout_per_chunk,
                history=None,
                language=language,
            )
        except (FileNotFoundError, ValueError, CoddCLIError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        _echo_chunked_result(project_root, result)
        if result.status != "SUCCESS":
            raise SystemExit(1)
        try:
            _run_typecheck_loop_after_implement(
                project_root=project_root,
                modified_files=None,
                ai_cmd=ai_cmd,
                force_enabled=enable_typecheck_loop,
            )
        except (FileNotFoundError, ValueError, CoddCLIError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        # Chunked runs are gated audit-only (no automatic re-run: resume
        # semantics differ from a plain re-implementation).
        from codd.implementer import _configured_output_path_groups

        config = _load_optional_project_config(project_root)
        _enforce_implement_coverage_gate(
            project_root,
            design_node=task_id,
            output_paths=_configured_output_path_groups(config).get(task_id or "", []),
            opt_out=no_coverage_gate,
            rerun=None,
        )
        return

    try:
        results = implement_tasks(
            project_root,
            task=task_id,
            ai_command=ai_cmd,
            use_derived_steps=_optional_bool(use_derived_steps),
            language=language,
        )
    except (FileNotFoundError, ValueError, CoddCLIError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    failed = [result for result in results if result.error]
    for result in results:
        for generated_file in result.generated_files:
            click.echo(f"Generated: {generated_file.relative_to(project_root)} ({result.task_id})")
    click.echo(f"{sum(len(result.generated_files) for result in results)} files generated across {len(results) - len(failed)} task(s)")
    if failed:
        raise SystemExit(1)
    try:
        _run_typecheck_loop_after_implement(
            project_root=project_root,
            modified_files=[generated_file for result in results for generated_file in result.generated_files],
            ai_cmd=ai_cmd,
            force_enabled=enable_typecheck_loop,
        )
    except (FileNotFoundError, ValueError, CoddCLIError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    def _retry_with_feedback(feedback: str) -> None:
        implement_tasks(
            project_root,
            task=task_id,
            ai_command=ai_cmd,
            use_derived_steps=_optional_bool(use_derived_steps),
            language=language,
            feedback=feedback,
        )

    _enforce_implement_coverage_gate(
        project_root,
        design_node=task_id,
        results=results,
        opt_out=no_coverage_gate,
        rerun=_retry_with_feedback,
    )


@implement.command("resume")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--history", required=True, help="History id or path from a previous chunked run")
@project_root_option("project_path")
@click.option("--ai-cmd", default=None, help="Override AI CLI command")
@click.option("--chunk-size", default=5, type=click.IntRange(min=1), show_default=True, help="Chunk size")
@click.option(
    "--timeout-per-chunk",
    default=600,
    type=click.IntRange(min=1),
    show_default=True,
    help="Seconds before one chunk is interrupted",
)
def implement_resume_cmd(
    task_id: str,
    history: str,
    project_path: str,
    ai_cmd: str | None,
    chunk_size: int,
    timeout_per_chunk: int,
):
    """Resume a chunked implementation run."""
    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    try:
        result = _run_chunked_implementation(
            project_root=project_root,
            task_id=task_id,
            ai_cmd=ai_cmd,
            chunk_size=chunk_size,
            timeout_per_chunk=timeout_per_chunk,
            history=history,
        )
    except (FileNotFoundError, ValueError, CoddCLIError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    _echo_chunked_result(project_root, result)
    if result.status != "SUCCESS":
        raise SystemExit(1)


def _run_chunked_implementation(
    *,
    project_root: Path,
    task_id: str | None,
    ai_cmd: str | None,
    chunk_size: int,
    timeout_per_chunk: int,
    history: str | None,
    language: str | None = None,
):
    if not task_id:
        raise ValueError("--task is required when chunked execution is enabled")

    import codd.generator as generator_module
    from codd.implementer.chunked_runner import ChunkedRunner

    config = _load_optional_project_config(project_root)
    if language:
        # Per-invocation language override (Issue #20, v-kato): align with
        # implement_tasks() so both code paths honour the CLI flag without
        # mutating codd.yaml on disk.
        project_cfg = dict(config.get("project") or {})
        project_cfg["language"] = language
        config = {**config, "project": project_cfg}
    task_item, steps = _chunked_task_and_steps(project_root, config, task_id)
    resolved_ai_command = generator_module._resolve_ai_command(config, ai_cmd, command_name="implement")

    def progress(current: int, total: int) -> None:
        click.echo(f"Chunk {current}/{total} complete")

    runner = ChunkedRunner(
        chunk_size=chunk_size,
        timeout_per_chunk=timeout_per_chunk,
        progress_callback=progress,
    )
    if history is None:
        return runner.run_steps(task_item, steps, resolved_ai_command, project_root)
    return runner.resume_steps(task_item, steps, resolved_ai_command, project_root, history)


def _chunked_task_and_steps(project_root: Path, config: dict[str, Any], task_id: str):
    from codd.implementer import _filter_layer1_impl_steps, _filter_layer2_impl_steps
    from codd.llm.impl_step_deriver import impl_step_cache_path, read_impl_step_cache

    task_item = _implement_task_for_cli(project_root, config, task_id)
    context = {"project_root": project_root}
    cache_path = impl_step_cache_path(task_item, context)
    record = read_impl_step_cache(cache_path)
    if record is None:
        record = read_impl_step_cache(impl_step_cache_path(task_id, context))
    if record is None or not record.steps:
        raise ValueError("no derived implementation steps found; run 'codd implement plan' first")

    explicit = _filter_layer1_impl_steps([step for step in record.steps if not step.inferred], config)
    implicit = _filter_layer2_impl_steps([step for step in record.steps if step.inferred], config)
    steps = [*explicit, *implicit]
    if not steps:
        raise ValueError("no approved implementation steps found for chunked execution")
    return task_item, steps


def _echo_chunked_result(project_root: Path, result) -> None:
    try:
        history = result.history_path.relative_to(project_root)
    except ValueError:
        history = result.history_path
    click.echo(
        f"Chunked implementation {result.status}: "
        f"{len(result.completed_chunks)}/{result.total_chunks} chunks; history={history}"
    )


def _run_typecheck_loop_after_implement(
    *,
    project_root: Path,
    modified_files: list[Path] | None,
    ai_cmd: str | None,
    force_enabled: bool,
):
    config = _load_optional_project_config(project_root)
    if not force_enabled and not _typecheck_config_enabled(config):
        return None
    from codd.implementer import TypecheckRepairLoop

    loop = TypecheckRepairLoop.from_config(config, force_enabled=force_enabled)
    if not loop.enabled:
        return None

    result = loop.run_after_implement(
        project_root,
        modified_files if modified_files is not None else _git_modified_files(project_root),
        ai_cmd or _configured_ai_command(config),
    )
    click.echo(f"Typecheck loop {result.status}")
    if result.status == "REPAIR_EXHAUSTED":
        raise CoddCLIError("typecheck repair loop exhausted")
    return result


def _configured_ai_command(config: dict[str, Any]) -> str:
    command = config.get("ai_command")
    return command if isinstance(command, str) else ""


def _typecheck_config_enabled(config: dict[str, Any]) -> bool:
    typecheck = config.get("typecheck")
    return bool(typecheck.get("enabled")) if isinstance(typecheck, dict) else False


def _git_modified_files(project_root: Path) -> list[Path]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, ValueError):
        return []
    if completed.returncode != 0:
        return []
    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.rsplit(" -> ", 1)[1]
        if raw_path:
            paths.append(project_root / raw_path)
    return paths


@main.command()
@project_root_option()
@click.option("--output-dir", default=None, help="Output directory for assembled project (default: src/)")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
def assemble(path: str, output_dir: str | None, ai_cmd: str | None):
    """Assemble generated fragments into a working project."""
    from codd.assembler import assemble_project

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    try:
        result = assemble_project(project_root, output_dir=output_dir, ai_command=ai_cmd)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Assembled {result.files_written} files into {result.output_dir.relative_to(project_root)}/")


@main.command()
@project_root_option()
@click.option("--e2e", is_flag=True, default=False, help="Run E2E tests (CI-safe, excludes @cdp-only)")
@click.option("--deploy", is_flag=True, default=False, help="Run deploy/CDP-only E2E tests against deployed URL")
@click.option("--base-url", default=None, help="Override BASE_URL for E2E tests")
@click.option("--runtime", is_flag=True, default=False, help="Run Step 8 runtime smoke as the final verification gate")
@click.option("--runtime-base-url", default=None, help="Override dev server base URL for runtime smoke")
@click.option(
    "--runtime-skip",
    multiple=True,
    type=click.Choice(
        ["db", "dev-server", "connectivity", "e2e", "crud-flow", "action-outcome", "global-action", "verification-test"]
    ),
    help="Skip a runtime smoke check and record it as skipped in the report",
)
@click.option(
    "--design-md",
    is_flag=True,
    default=False,
    help="Run npx @google/design.md lint on DESIGN.md (skip if npx unavailable).",
)
@click.option("--auto-repair", is_flag=True, default=False, help="Run RepairLoop when verification fails")
@click.option("--max-attempts", default=None, type=click.IntRange(min=1), help="Maximum repair attempts")
@click.option(
    "--repair-mode",
    "repair_mode",
    type=click.Choice(["automatic", "hitl"]),
    default=None,
    help=(
        "Override repair.approval_mode for this run (requires --auto-repair): "
        "'automatic' auto-approves repair proposals (the flag is the explicit "
        "opt-in; oversized proposals still escalate), 'hitl' requires approval."
    ),
)
@click.option("--baseline-ref", default=None, help="Baseline git ref for repair classification")
@click.option("--engine", "engine_name", default=None, help="Repair engine name")
@click.option(
    "--no-contract-gate",
    is_flag=True,
    default=False,
    help="Skip the artifact-contract completion gate for the 'verify' stage",
)
def verify(
    path: str,
    e2e: bool,
    deploy: bool,
    base_url: str | None,
    runtime: bool,
    runtime_base_url: str | None,
    runtime_skip: tuple[str, ...],
    design_md: bool,
    auto_repair: bool,
    max_attempts: int | None,
    repair_mode: str | None,
    baseline_ref: str | None,
    engine_name: str | None,
    no_contract_gate: bool,
) -> None:
    """Run build + test verification and trace failures to design documents."""
    if repair_mode is not None and not auto_repair:
        raise click.BadOptionUsage("repair_mode", "--repair-mode requires --auto-repair")
    if design_md:
        _run_design_md_lint(Path(path).resolve())
        return
    if e2e or deploy:
        from codd.e2e_runner import run_e2e

        e2e_exit_code = run_e2e(path=path, deploy=deploy, base_url=base_url)
        if e2e_exit_code != 0:
            raise SystemExit(e2e_exit_code)
        if not runtime:
            return

    # Stack contract intake + lock enforcement (Contract Kernel v2.77a/v2.77b) —
    # bring the project's declared framework-stack contract into the live verify
    # run's trace (v2.77a, mirroring the greenfield intake), then ENFORCE the stack
    # lock as a gate (v2.77b): a stack project whose committed lock has drifted from
    # the resolved contract — or that has no committed lock at all — is RED. A
    # project with no `stack:` block is unaffected; a declared-but-broken stack
    # fails HONESTLY (anti-false-green), never a silent skip.
    _intake_stack_contract_for_verify(Path(path).resolve())

    verify_kwargs: dict[str, Any] = {"path": path, "prefer_standalone": True}
    if runtime_skip:
        verify_kwargs["runtime_skip"] = runtime_skip

    if not auto_repair:
        result = _run_verify_once(**verify_kwargs)
        _emit_verify_summary(result)
        if not result.passed:
            # FX3 honesty: a failed verification must fail the command. The
            # old behavior raised SystemExit only on the --runtime path, so a
            # plain `codd verify` with red failures printed a summary and
            # exited 0 — a false green on the CLI surface itself.
            raise SystemExit(result.exit_code)
        if runtime:
            _run_runtime_smoke_gate(
                path=path,
                runtime_base_url=runtime_base_url,
                runtime_skip=_runtime_smoke_skip(runtime_skip),
            )
        _enforce_stage_contract_gate(Path(path).resolve(), "verify", opt_out=no_contract_gate)
        return

    project_root = Path(path).resolve()
    result = _run_verify_once(**verify_kwargs)
    _emit_verify_summary(result)
    if result.passed:
        if runtime:
            _run_runtime_smoke_gate(
                path=path,
                runtime_base_url=runtime_base_url,
                runtime_skip=_runtime_smoke_skip(runtime_skip),
            )
        _enforce_stage_contract_gate(project_root, "verify", opt_out=no_contract_gate)
        return

    # Split on repair_mode so the per-run opt-in reaches the gate while plain
    # brownfield `codd verify --auto-repair` stays owner-gated:
    #   - repair_mode is None  → KEEP the brownfield gate (_load_required_repair_config
    #     rejects a missing/empty repair: section). The resolved config is the
    #     disk config; no per-run allow_auto.require_explicit_optin is injected, so
    #     `repair.approval_mode: auto` without that opt-in still fails the gate.
    #   - repair_mode set      → load the project config directly and apply the
    #     per-run override (apply_repair_mode), so `--repair-mode automatic` works
    #     end-to-end even with NO on-disk repair: section (the flag IS the opt-in).
    if repair_mode is None:
        repair_config = _load_required_repair_config(project_root)
        if repair_config is None:
            raise SystemExit(1)
    else:
        from codd.repair.approval_repair import apply_repair_mode

        try:
            base_config = load_project_config(project_root)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"WARN: codd.yaml is required for repair: {exc}")
            raise SystemExit(1)
        repair_config = apply_repair_mode(base_config, repair_mode)

    outcome = _run_repair_loop(
        project_root,
        result.failure,
        repair_config=repair_config,
        max_attempts=max_attempts,
        baseline_ref=baseline_ref,
        engine_name=engine_name,
        verify_callable=lambda: _run_verify_once(**verify_kwargs),
        initial_verify_result=result,
    )
    click.echo(f"Repair outcome: {outcome.status}")
    click.echo(f"Repair history: {_display_path(outcome.history_session_dir, project_root)}")
    raise SystemExit(_repair_exit_code(outcome.status))


def _run_runtime_smoke_gate(path: str, runtime_base_url: str | None, runtime_skip: tuple[str, ...]) -> None:
    from codd.runtime_smoke.runner import run_runtime_smoke

    try:
        smoke_result = run_runtime_smoke(Path(path).resolve(), skip_checks=runtime_skip, base_url_override=runtime_base_url)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"[FAIL] Step 8 runtime smoke configuration error: {exc}", err=True)
        raise SystemExit(3)

    click.echo(smoke_result.markdown_section.rstrip())
    if smoke_result.report_path is not None:
        click.echo(f"[codd verify] Runtime smoke report: {_display_path(smoke_result.report_path, Path(path).resolve())}")
    if not smoke_result.overall_passed:
        click.echo("[FAIL] Step 8 runtime smoke failed", err=True)
        raise SystemExit(1)


def _runtime_smoke_skip(runtime_skip: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in runtime_skip if item != "verification-test")


def _run_e2e_generate(path: str, base_url: str, output: str | None, framework: str, mode: str = "scenarios") -> None:
    """Generate E2E test files from saved or freshly extracted scenarios."""
    project_root = Path(path).resolve()
    if mode == "transitions":
        from codd.e2e_generator import TransitionTestGenerator

        output_path = None
        if output:
            output_path = Path(output).expanduser()
            if not output_path.is_absolute():
                output_path = project_root / output_path

        generator = TransitionTestGenerator(project_root)
        transition_count = len(generator.load_transitions())
        written_path = generator.write_tests(output_path=output_path)
        click.echo(f"Generated transition test file: {_display_path(written_path, project_root)} ({transition_count} transitions)")
        return

    from codd.e2e_extractor import ScenarioExtractor
    from codd.e2e_generator import TestGenerator, load_scenarios_from_markdown

    scenarios_path = (
        project_root / "docs" / "e2e" / "operational-scenarios.md"
        if mode == "operational"
        else project_root / "docs" / "e2e" / "scenarios.md"
    )
    if scenarios_path.exists():
        collection = load_scenarios_from_markdown(scenarios_path)
    elif mode == "operational":
        collection = ScenarioExtractor(project_root).extract_operational()
    else:
        collection = ScenarioExtractor(project_root).extract()

    if not collection.scenarios:
        click.echo(f"No scenarios found. Run `codd e2e extract --mode {mode}` first or check {_display_path(scenarios_path, project_root)}.")
        return

    output_dir = None
    if output:
        output_dir = Path(output).expanduser()
        if not output_dir.is_absolute():
            output_dir = project_root / output_dir

    generator = TestGenerator(project_root, base_url=base_url, framework=framework)
    generated = generator.generate(collection, output_dir=output_dir)

    click.echo(f"Generated {len(generated)} test file(s):")
    for test_file in generated:
        routes = ", ".join(test_file.routes[:3]) or "none"
        if len(test_file.routes) > 3:
            routes += "..."
        click.echo(f"  {test_file.file_name} ({test_file.steps_count} steps, routes: {routes})")


def _run_e2e_extract(path: str, output: str | None, mode: str = "scenarios") -> None:
    """Extract E2E scenario catalogs from project documents."""
    project_root = Path(path).resolve()
    from codd.e2e_extractor import ScenarioExtractor

    output_path = None
    if output:
        output_path = Path(output).expanduser()
        if not output_path.is_absolute():
            output_path = project_root / output_path

    extractor = ScenarioExtractor(project_root)
    if mode == "operational":
        collection = extractor.extract_operational()
        written_path = extractor.save_operational_scenarios(collection, output_path=output_path)
    else:
        collection = extractor.extract()
        written_path = extractor.save_scenarios(collection, output_path=output_path)

    click.echo(f"Extracted {len(collection.scenarios)} {mode} scenario(s): {_display_path(written_path, project_root)}")


def _run_e2e_audit(
    path: str,
    output: str | None,
    output_format: str,
    scenarios: str | None,
    test_dirs: tuple[str, ...],
    runner_backend: str,
) -> None:
    """Audit operational E2E coverage using adapter-neutral markers."""
    project_root = Path(path).resolve()
    from codd.operational_e2e_audit import build_operational_e2e_audit, write_operational_e2e_audit

    output_path = Path(output or f"docs/e2e/operational-audit.{output_format}").expanduser()
    if not output_path.is_absolute():
        output_path = project_root / output_path

    scenarios_path = Path(scenarios).expanduser() if scenarios else None
    if scenarios_path is not None and not scenarios_path.is_absolute():
        scenarios_path = project_root / scenarios_path

    report = build_operational_e2e_audit(
        project_root,
        scenarios_path=scenarios_path,
        test_dirs=test_dirs or None,
        runner_backend=runner_backend,
    )
    written_path = write_operational_e2e_audit(report, output_path, output_format=output_format)
    summary = report.summary
    click.echo(
        "Operational E2E audit: "
        f"{summary['scenario_count']} scenario(s), "
        f"{summary['covered_by_e2e']} covered by E2E, "
        f"{summary.get('covered_by_lower_test', 0)} lower-test only, "
        f"{summary.get('needs_trigger_evidence', 0)} need trigger evidence, "
        f"{summary.get('needs_dod_evidence', 0)} need DoD evidence, "
        f"{summary.get('needs_source_signal_variance', 0)} need source-signal variance, "
        f"{summary.get('blocked', 0)} blocked, "
        f"{summary['heuristic_matches']} heuristic, "
        f"{summary.get('not_covered_by_e2e', summary['uncovered'])} E2E gap(s), "
        f"{summary['uncovered']} strictly uncovered: "
        f"{_display_path(written_path, project_root)}"
    )


def _run_e2e_workflow_plan(
    path: str,
    output: str | None,
    output_format: str,
    scenarios: str | None,
    test_dirs: tuple[str, ...],
    runner_backend: str,
    max_scenarios_per_shard: int,
    claude_dangerously_skip_permissions: bool,
) -> None:
    """Create an adapter-neutral parallel E2E agent workflow plan."""
    project_root = Path(path).resolve()
    from codd.operational_e2e_audit import build_agent_workflow_plan, write_agent_workflow_plan

    output_path = Path(output or f"docs/e2e/agent-workflow-plan.{output_format}").expanduser()
    if not output_path.is_absolute():
        output_path = project_root / output_path

    scenarios_path = Path(scenarios).expanduser() if scenarios else None
    if scenarios_path is not None and not scenarios_path.is_absolute():
        scenarios_path = project_root / scenarios_path

    plan = build_agent_workflow_plan(
        project_root,
        scenarios_path=scenarios_path,
        test_dirs=test_dirs or None,
        runner_backend=runner_backend,
        max_scenarios_per_shard=max_scenarios_per_shard,
        claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
    )
    written_path = write_agent_workflow_plan(plan, output_path, output_format=output_format)
    click.echo(
        "Agent workflow E2E plan: "
        f"{plan.summary['workflow_candidate_scenarios']} candidate scenario(s), "
        f"{plan.summary['workflow_shards']} shard(s): "
        f"{_display_path(written_path, project_root)}"
    )


@main.group()
def e2e():
    """Generate and manage E2E test artifacts."""
    pass


@e2e.command("generate")
@project_root_option()
@click.option(
    "--base-url",
    default="http://localhost:3000",
    show_default=True,
    help="Base URL for generated E2E tests",
)
@click.option("--output", default=None, help="Output directory for scenario mode, or output file for transition mode")
@click.option(
    "--framework",
    default="playwright",
    show_default=True,
    type=click.Choice(["playwright", "cypress"]),
    help="E2E test framework",
)
@click.option(
    "--mode",
    default="scenarios",
    show_default=True,
    type=click.Choice(["scenarios", "transitions", "operational"]),
    help="Input source for generated E2E tests",
)
def e2e_generate(path: str, base_url: str, output: str | None, framework: str, mode: str) -> None:
    """Generate Playwright or Cypress test files from scenarios or screen transitions."""
    _run_e2e_generate(path=path, base_url=base_url, output=output, framework=framework, mode=mode)


@e2e.command("extract")
@project_root_option()
@click.option("--output", default=None, help="Output scenario catalog path")
@click.option(
    "--mode",
    default="scenarios",
    show_default=True,
    type=click.Choice(["scenarios", "operational"]),
    help="Scenario catalog to extract",
)
def e2e_extract(path: str, output: str | None, mode: str) -> None:
    """Extract E2E scenario catalogs without generating test files."""
    _run_e2e_extract(path=path, output=output, mode=mode)


@e2e.command("audit")
@project_root_option()
@click.option("--output", default=None, help="Output report path")
@click.option(
    "--format",
    "output_format",
    default="md",
    show_default=True,
    type=click.Choice(["md", "json"]),
    help="Output report format",
)
@click.option("--scenarios", default=None, help="Operational scenario catalog path")
@click.option("--test-dir", "test_dirs", multiple=True, help="Test directory or file to scan")
@click.option(
    "--runner-backend",
    default="local-playwright",
    show_default=True,
    type=click.Choice(["local-playwright", "ci-shard", "agent-workflow", "claude-dynamic-workflow"]),
    help="Runner backend contract to report against",
)
def e2e_audit(
    path: str,
    output: str | None,
    output_format: str,
    scenarios: str | None,
    test_dirs: tuple[str, ...],
    runner_backend: str,
) -> None:
    """Audit operational E2E coverage without requiring a specific agent backend."""
    _run_e2e_audit(
        path=path,
        output=output,
        output_format=output_format,
        scenarios=scenarios,
        test_dirs=test_dirs,
        runner_backend=runner_backend,
    )


@e2e.command("workflow-plan")
@project_root_option()
@click.option("--output", default=None, help="Output workflow plan path")
@click.option(
    "--format",
    "output_format",
    default="json",
    show_default=True,
    type=click.Choice(["json", "md"]),
    help="Output workflow plan format",
)
@click.option("--scenarios", default=None, help="Operational scenario catalog path")
@click.option("--test-dir", "test_dirs", multiple=True, help="Test directory or file to scan")
@click.option(
    "--runner-backend",
    default="agent-workflow",
    show_default=True,
    type=click.Choice(["agent-workflow", "claude-dynamic-workflow"]),
    help="Agent runner backend contract to report against",
)
@click.option(
    "--max-scenarios-per-shard",
    default=6,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum uncovered scenarios assigned to one worker shard",
)
@click.option(
    "--claude-dangerously-skip-permissions/--claude-safe-permissions",
    default=True,
    show_default=True,
    help=(
        "For --runner-backend claude-dynamic-workflow, include or suppress Claude CLI "
        "permission bypass flags in generated runner invocation metadata."
    ),
)
def e2e_workflow_plan(
    path: str,
    output: str | None,
    output_format: str,
    scenarios: str | None,
    test_dirs: tuple[str, ...],
    runner_backend: str,
    max_scenarios_per_shard: int,
    claude_dangerously_skip_permissions: bool,
) -> None:
    """Create parallel agent workflow shards from operational E2E gaps."""
    _run_e2e_workflow_plan(
        path=path,
        output=output,
        output_format=output_format,
        scenarios=scenarios,
        test_dirs=test_dirs,
        runner_backend=runner_backend,
        max_scenarios_per_shard=max_scenarios_per_shard,
        claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
    )


@main.command("e2e-generate")
@project_root_option()
@click.option(
    "--base-url",
    default="http://localhost:3000",
    show_default=True,
    help="Base URL for generated E2E tests",
)
@click.option("--output", default=None, help="Output directory for scenario mode, or output file for transition mode")
@click.option(
    "--framework",
    default="playwright",
    show_default=True,
    type=click.Choice(["playwright", "cypress"]),
    help="E2E test framework",
)
@click.option(
    "--mode",
    default="scenarios",
    show_default=True,
    type=click.Choice(["scenarios", "transitions", "operational"]),
    help="Input source for generated E2E tests",
)
def e2e_generate_legacy(path: str, base_url: str, output: str | None, framework: str, mode: str) -> None:
    """Generate Playwright or Cypress test files from scenarios or screen transitions."""
    _run_e2e_generate(path=path, base_url=base_url, output=output, framework=framework, mode=mode)


@main.group(invoke_without_command=True)
@project_root_option()
@click.option("--language", default=None, help="Override language detection (python/typescript/javascript/go — full support; java — symbols only)")
@click.option("--source-dirs", default=None, help="Comma-separated source directories (default: auto-detect)")
@click.option("--output", default=None, help="Output directory (default: <project-root>/.codd/extract/)")
@click.option("--init", "initialize", is_flag=True, help="Add brownfield init metadata to generated YAML/Markdown")
@click.option("--ai", is_flag=True, default=False, help="Use AI-powered extraction (6-layer MECE design docs)")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (default: codd.yaml ai_command or claude --print)",
)
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(exists=True),
    help="Custom extraction prompt file (overrides built-in baseline preset)",
)
@click.option(
    "--layer",
    default=None,
    type=click.Choice(["routes", "routes-edges"]),
    help="Extract specific layer (routes: filesystem routes, routes-edges: screen transition edges)",
)
@click.option(
    "--format",
    "output_format",
    default="mermaid",
    type=click.Choice(["mermaid"]),
    help="Output format for --layer extraction",
)
@click.option("--output-file", default=None, help="Output file for --layer routes (default: stdout)")
@click.pass_context
def extract(
    ctx: click.Context,
    path: str,
    language: str | None,
    source_dirs: str | None,
    output: str | None,
    initialize: bool,
    ai: bool,
    ai_cmd: str | None,
    prompt_file: str | None,
    layer: str | None,
    output_format: str,
    output_file: str | None,
):
    """Extract design documents from existing codebase (brownfield bootstrap).

    Default mode: static analysis (no AI, pure structural facts).
    With --ai: AI-powered 6-layer MECE extraction using claude --print.

    Output goes to `.codd/extract/` by default. Review and promote confirmed
    docs when ready.
    """
    if ctx.invoked_subcommand is not None:
        return

    project_root = Path(path).resolve()
    bootstrap_codd_dir = _resolve_bootstrap_codd_dir(project_root)
    dirs = [d.strip() for d in source_dirs.split(",") if d.strip()] if source_dirs else None
    from codd.extract_paths import default_extract_output_dir
    output_path = Path(output) if output else default_extract_output_dir(project_root)
    if output and not output_path.is_absolute():
        output_path = project_root / output_path
    init_metadata = None
    if initialize:
        from codd.extractor import build_extract_init_metadata
        init_metadata = build_extract_init_metadata(project_root)

    if layer == "routes":
        from codd.config import load_project_config
        from codd.routes_extractor import generate_mermaid_screen_flow

        config = load_project_config(project_root)
        route_configs = config.get("filesystem_routes", [])
        result = generate_mermaid_screen_flow(project_root, route_configs)
        content = f"```{output_format}\n{result.mermaid}\n```\n"
        if output_file:
            destination = Path(output_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            click.echo(f"Extracted {result.route_count} routes -> {output_file}")
        else:
            click.echo(content)
        return

    if layer == "routes-edges":
        from codd.screen_transition_extractor import extract_transitions, write_screen_transitions_yaml

        destination = Path(output_file or output) if (output_file or output) else project_root / "docs" / "extracted" / "screen-transitions.yaml"
        if not destination.is_absolute():
            destination = project_root / destination
        transitions = extract_transitions(project_root, dirs)
        write_screen_transitions_yaml(transitions, destination)
        click.echo(f"Extracted {len(transitions)} screen transitions -> {_display_path(destination, project_root)}")
        return

    if ai:
        from codd.extract_ai import run_extract_ai
        from codd.extractor import extract_facts
        from codd.config import load_project_config

        # Resolve AI command
        if ai_cmd is None:
            try:
                cfg = load_project_config(project_root)
                ai_cmd = cfg.get(
                    "ai_command",
                    'claude --print --permission-mode bypassPermissions --dangerously-skip-permissions --model claude-opus-4-8 --effort max --tools ""',
                )
            except FileNotFoundError:
                ai_cmd = (
                    'claude --print --permission-mode bypassPermissions '
                    '--dangerously-skip-permissions --model claude-opus-4-8 --effort max --tools ""'
                )

        preset_name = "custom" if prompt_file else "baseline"
        click.echo(f"CoDD AI Extract v3")
        click.echo(f"Using preset: {preset_name} ({'public' if not prompt_file else prompt_file})")
        click.echo(f"Project: {project_root}")
        click.echo(f"AI command: {ai_cmd}")
        click.echo(f"Scanning project...")

        try:
            result = run_extract_ai(project_root, ai_cmd, str(output_path), prompt_file=prompt_file)
        except Exception as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)
        if init_metadata is not None:
            from codd.extractor import add_extract_init_frontmatter
            add_extract_init_frontmatter(
                result.generated_files, init_metadata, output_dir=result.output_dir
            )

        facts = extract_facts(project_root, language, dirs)
        config_path, generated_config = _ensure_bootstrap_codd_yaml(
            project_root,
            codd_dir=bootstrap_codd_dir,
            language=facts.language,
            source_dirs=facts.source_dirs,
        )
        output_display = _display_path(result.output_dir, project_root)
        config_display = _display_path(config_path, project_root)

        click.echo(f"\nExtracted: {result.module_count} source files analyzed")
        click.echo(f"Output: {output_display}/")
        for f in result.generated_files:
            click.echo(f"  {f.name}")
        if generated_config:
            click.echo(f"Generated: {config_display} (minimal brownfield config)")

        click.echo(f"\nNext steps:")
        click.echo(f"  1. Review generated docs in {output_display}/")
        click.echo(f"  2. Promote confirmed docs: mv {output_display}/*.md docs/design/")
        click.echo(f"  3. Run: codd scan  (to build the dependency graph)")
    else:
        from codd.extractor import run_extract

        try:
            result = run_extract(project_root, language, dirs, str(output_path), init_metadata=init_metadata)
        except Exception as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        config_path, generated_config = _ensure_bootstrap_codd_yaml(
            project_root,
            codd_dir=bootstrap_codd_dir,
            language=result.language,
            source_dirs=result.source_dirs,
        )
        output_display = _display_path(result.output_dir, project_root)
        config_display = _display_path(config_path, project_root)

        click.echo(f"Extracted: {result.module_count} modules from {result.total_files} files ({result.total_lines:,} lines)")
        click.echo(f"Output: {output_display}/")
        for f in result.generated_files:
            click.echo(f"  {f.relative_to(result.output_dir)}")
        if generated_config:
            click.echo(f"Generated: {config_display} (minimal brownfield config)")

        click.echo(f"\nNext steps:")
        click.echo(f"  1. Review generated docs in {output_display}/")
        click.echo(f"  2. Promote confirmed docs: mv {output_display}/*.md docs/design/")
        click.echo(f"  3. Run: codd scan  (to build the dependency graph)")


@extract.command("design")
@project_root_option("project_path")
@click.option("--design-doc", required=True, type=click.Path(dir_okay=False, path_type=Path), help="Design document path")
@click.option("--force", is_flag=True, help="Ignore cached expected extraction and run extraction again")
def extract_design(project_path: str, design_doc: Path, force: bool):
    """Extract expected implementation coverage hints from one design document."""
    from codd.llm.design_doc_extractor import (
        expected_extraction_cache_path,
        extract_expected_artifacts_for_file,
    )

    project_root = Path(project_path).resolve()
    doc_path = design_doc.expanduser()
    if not doc_path.is_absolute():
        doc_path = project_root / doc_path
    doc_path = doc_path.resolve()

    if not doc_path.is_file():
        click.echo(f"Error: design document not found: {doc_path}")
        raise SystemExit(1)

    try:
        extraction = extract_expected_artifacts_for_file(
            doc_path,
            project_root,
            config=_load_optional_project_config(project_root),
            force=force,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    cache_path = expected_extraction_cache_path(project_root, doc_path)
    click.echo(
        "Extracted expected artifacts: "
        f"{len(extraction.expected_nodes)} node(s), "
        f"{len(extraction.expected_edges)} edge(s) -> "
        f"{_display_path(cache_path, project_root)}"
    )


@main.command("repair-slice")
@project_root_option()
@click.option("--files", required=True, help="Comma-separated list of located files to analyze")
@click.option("--issue", default=None, help="Issue/bug description text for relevance scoring")
@click.option("--issue-file", default=None, type=click.Path(exists=True), help="File containing issue text")
@click.option("--language", default=None, help="Override language detection")
@click.option("--source-dirs", default=None, help="Comma-separated source directories")
@click.option("--top-n", default=3, type=int, help="Top N functions per file (default: 3)")
def repair_slice_cmd(path, files, issue, issue_file, language, source_dirs, top_n):
    """Generate compact repair context for located files (patch generation pipeline)."""
    from codd.repair_slice import generate_repair_slices

    project_root = Path(path).resolve()
    file_list = [f.strip() for f in files.split(",") if f.strip()]

    issue_text = issue or ""
    if issue_file and not issue_text:
        issue_text = Path(issue_file).read_text(encoding="utf-8", errors="ignore")

    dirs = [d.strip() for d in source_dirs.split(",") if d.strip()] if source_dirs else None

    result = generate_repair_slices(
        project_root,
        file_list,
        issue_text=issue_text,
        language=language,
        source_dirs=dirs,
        top_n=top_n,
    )
    click.echo(result)


@main.command()
@click.option("--lexicon", is_flag=True, default=False, help="Validate against project_lexicon.yaml")
@click.option(
    "--design-tokens",
    is_flag=True,
    default=False,
    help="Check UI files for hardcoded #hex/px values not in DESIGN.md tokens.",
)
@click.option(
    "--screen-flow",
    "screen_flow",
    is_flag=True,
    default=False,
    help="Validate screen-flow.md routes against filesystem routes.",
)
@click.option(
    "--edges",
    is_flag=True,
    default=False,
    help="Also validate screen-flow route coverage by extracted transition edges.",
)
@project_root_option()
def validate(lexicon: bool, design_tokens: bool, screen_flow: bool, edges: bool, path: str):
    """Validate CoDD frontmatter and dependency references."""
    project_root = Path(path).resolve()
    if lexicon:
        from codd.validator import validate_with_lexicon

        violations = validate_with_lexicon(project_root)
        if violations:
            for violation in violations:
                click.echo(
                    f"[{violation['violation_type']}] {violation['node_id']}: {violation['message']}"
                )
            raise SystemExit(1)
        click.echo("Lexicon validation: OK (no violations)")
        raise SystemExit(0)

    if design_tokens:
        from codd.validator import validate_design_tokens

        violations = validate_design_tokens(project_root)
        if violations:
            click.echo(f"Design token violations found: {len(violations)}")
            for violation in violations:
                click.echo(
                    f"  {violation.file}:{violation.line} - hardcoded {violation.pattern} "
                    f"(suggest: {violation.suggestion})"
                )
            raise SystemExit(1)
        click.echo("No design token violations found.")
        raise SystemExit(0)

    if screen_flow or edges:
        from codd.coverage_metrics import check_edge_coverage_gate
        from codd.screen_flow_validator import (
            find_screen_flow_path,
            parse_screen_flow_routes,
            validate_screen_flow,
            validate_screen_flow_edges,
        )

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        drifts = validate_screen_flow(project_root, config)
        edge_result = None
        edge_ok = True
        strict_edges = _screen_flow_strict_edges(config)
        if edges:
            screen_flow_path = find_screen_flow_path(project_root)
            screen_flow_nodes = parse_screen_flow_routes(screen_flow_path) if screen_flow_path else []
            edge_result = validate_screen_flow_edges(project_root, screen_flow_nodes, config)
            edge_ok = check_edge_coverage_gate(edge_result, config)
        if drifts:
            click.echo(f"Screen-flow drift detected: {len(drifts)} route(s)")
            for drift in drifts:
                click.echo(f"  [{drift.source}] {drift.route}: {drift.detail}")
        else:
            click.echo("Screen-flow validation: OK (no drift)")
        if edge_result is not None:
            click.echo(
                "Screen-flow edge coverage: "
                f"{edge_result.coverage_ratio:.0%} "
                f"({len(edge_result.covered_nodes)} covered node(s), {edge_result.total_edges} edge(s))"
            )
            if edge_result.unreachable_nodes:
                click.echo("  Unreachable nodes: " + ", ".join(edge_result.unreachable_nodes))
            if edge_result.orphan_nodes:
                click.echo("  Orphan nodes: " + ", ".join(edge_result.orphan_nodes))
            if edge_result.dead_end_nodes:
                click.echo("  Dead-end nodes: " + ", ".join(edge_result.dead_end_nodes))
        if drifts or not edge_ok or (edge_result is not None and strict_edges and edge_result.orphan_nodes):
            raise SystemExit(1)
        raise SystemExit(0)

    from codd.validator import run_validate

    codd_dir = _require_codd_dir(project_root)
    raise SystemExit(run_validate(project_root, codd_dir))


@main.group(invoke_without_command=True)
@project_root_option()
@click.option(
    "--e2e-threshold",
    default=100.0,
    show_default=True,
    type=float,
    help="E2E coverage threshold percentage.",
)
@click.option(
    "--lexicon-threshold",
    default=100.0,
    show_default=True,
    type=float,
    help="Lexicon compliance threshold percentage.",
)
@click.option(
    "--screen-flow-threshold",
    default=100.0,
    show_default=True,
    type=float,
    help="Screen-flow coverage threshold percentage.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format",
)
@click.option("--json", "as_json", is_flag=True, hidden=True, help="Deprecated alias for --format json.")
@click.pass_context
def coverage(
    ctx: click.Context,
    path: str,
    e2e_threshold: float,
    lexicon_threshold: float,
    screen_flow_threshold: float,
    output_format: str,
    as_json: bool,
):
    """Coverage metrics merge gate: E2E, design tokens, and lexicon."""
    if ctx.invoked_subcommand is not None:
        return

    from codd.coverage_metrics import run_coverage

    as_json = _resolve_output_format(output_format, as_json, "codd coverage") == "json"
    project_root = Path(path).resolve()
    report = run_coverage(
        project_root,
        e2e_threshold=e2e_threshold,
        design_token_threshold=0.0,
        lexicon_threshold=lexicon_threshold,
        screen_flow_threshold=screen_flow_threshold,
    )

    if as_json:
        click.echo(
            json.dumps(
                {
                    "all_passed": report.all_passed,
                    "results": [
                        {
                            "metric": result.metric,
                            "total": result.total,
                            "covered": result.covered,
                            "uncovered": result.uncovered,
                            "pct": result.pct,
                            "threshold": result.threshold,
                            "passed": result.passed,
                            "details": result.details,
                        }
                        for result in report.results
                    ],
                },
                indent=2,
            )
        )
    else:
        for result in report.results:
            status = "PASS" if result.passed else "FAIL"
            click.echo(
                f"[{status}] {result.metric}: {result.pct:.0f}% "
                f"(threshold: {result.threshold:.0f}%, uncovered: {result.uncovered})"
            )
            for detail in result.details:
                click.echo(f"    {detail}")
        click.echo("Coverage gate PASSED" if report.all_passed else "Coverage gate FAILED")

    raise SystemExit(0 if report.all_passed else 1)


@coverage.command("report")
@click.option(
    "--lexicons",
    default="all",
    show_default=True,
    help="Lexicons to include: all or a comma-separated id list.",
)
@project_root_option("project_path")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "md", "html"]),
    default="md",
    show_default=True,
    help="Report output format.",
)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Write report to file.")
@click.option("--with-ai", is_flag=True, default=False, help="Use AI-backed elicit coverage mode.")
@click.option("--ai-cmd", default=None, help="Override AI CLI command for --with-ai.")
def coverage_report_cmd(
    lexicons: str,
    project_path: str,
    output_format: str,
    output: Path | None,
    with_ai: bool,
    ai_cmd: str | None,
) -> None:
    """Generate a lexicon coverage matrix report."""
    from codd.lexicon_cli.formatters.html import format_coverage_report_html
    from codd.lexicon_cli.formatters.json_fmt import to_json
    from codd.lexicon_cli.formatters.md import format_coverage_report_md
    from codd.lexicon_cli.reporter import CoverageReporter

    project_root = Path(project_path).resolve()
    try:
        report = CoverageReporter(project_root).build(lexicons, with_ai=with_ai, ai_command=ai_cmd)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if output_format == "json":
        rendered = to_json(report)
    elif output_format == "html":
        rendered = format_coverage_report_html(report)
    else:
        rendered = format_coverage_report_md(report)

    if output is None:
        click.echo(rendered, nl=False)
        return

    output_path = output.expanduser()
    if not output_path.is_absolute():
        output_path = project_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    click.echo(f"Report: {_display_path(output_path, project_root)}")


@coverage.command("check")
@click.option(
    "--lexicons",
    default="all",
    show_default=True,
    help="Lexicons to include: all or a comma-separated id list.",
)
@project_root_option("project_path")
@click.option(
    "--threshold",
    "global_threshold",
    type=float,
    default=None,
    help="Global covered_text_match_pct threshold override.",
)
@click.option(
    "--threshold-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="External threshold YAML file.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["human", "json", "md"]),
    default="human",
    show_default=True,
    help="Output format.",
)
@click.option("--with-ai", is_flag=True, default=False, help="Use AI-backed elicit coverage mode.")
@click.option("--ai-cmd", default=None, help="Override AI CLI command for --with-ai.")
@click.option("--exit-zero", is_flag=True, default=False, help="Always exit 0 for threshold violations.")
def coverage_check_cmd(
    lexicons: str,
    project_path: str,
    global_threshold: float | None,
    threshold_file: Path | None,
    output_format: str,
    with_ai: bool,
    ai_cmd: str | None,
    exit_zero: bool,
) -> None:
    """Run a lexicon coverage threshold gate."""
    from dataclasses import asdict

    from codd.lexicon_cli.formatters.json_fmt import to_json
    from codd.lexicon_cli.reporter import CoverageReporter
    from codd.lexicon_cli.threshold import ThresholdConfig, evaluate, load_thresholds

    project_root = Path(project_path).resolve()
    try:
        report = CoverageReporter(project_root).build(lexicons, with_ai=with_ai, ai_command=ai_cmd)
        config = load_thresholds(threshold_file or _default_threshold_path(project_root))
        if global_threshold is not None:
            config = ThresholdConfig(default_pct=global_threshold)
        violations = evaluate(report, config)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(2)

    payload = {
        "status": "fail" if violations else "pass",
        "thresholds": asdict(config),
        "totals": report.totals,
        "violations": [asdict(violation) for violation in violations],
    }
    if output_format == "json":
        click.echo(to_json(payload), nl=False)
    elif output_format == "md":
        click.echo(_format_coverage_check_md(payload), nl=False)
    else:
        click.echo(_format_coverage_check_human(payload), nl=False)

    raise SystemExit(0 if exit_zero or not violations else 1)


def _default_threshold_path(project_root: Path) -> Path | None:
    codd_dir = find_codd_dir(project_root)
    return codd_dir / "codd.yaml" if codd_dir is not None else None


def _format_coverage_check_human(payload: dict[str, Any]) -> str:
    status = str(payload["status"]).upper()
    totals = payload["totals"]
    lines = [
        f"Coverage check {status}",
        f"Axes: {totals['covered']}/{totals['axes']} covered ({totals['covered_pct']:.2f}%)",
    ]
    violations = payload["violations"]
    if violations:
        lines.append("Violations:")
        for violation in violations:
            axis = violation["axis"] or "<overall>"
            lines.append(
                f"  - {violation['lexicon_id']} {axis}: "
                f"{violation['observed_pct']:.2f}% < {violation['required_pct']:.2f}%"
            )
    return "\n".join(lines) + "\n"


def _format_coverage_check_md(payload: dict[str, Any]) -> str:
    totals = payload["totals"]
    lines = [
        "# Coverage Threshold Check",
        "",
        f"Status: **{str(payload['status']).upper()}**",
        f"Axes: {totals['covered']}/{totals['axes']} covered ({totals['covered_pct']:.2f}%)",
        "",
        "| Lexicon | Axis | Observed | Required |",
        "| --- | --- | ---: | ---: |",
    ]
    violations = payload["violations"]
    if violations:
        for violation in violations:
            axis = violation["axis"] or "<overall>"
            lines.append(
                f"| {violation['lexicon_id']} | {axis} | "
                f"{violation['observed_pct']:.2f}% | {violation['required_pct']:.2f}% |"
            )
    else:
        lines.append("| - | - | - | - |")
    return "\n".join(lines) + "\n"


@main.command("deploy")
@click.option("--target", default=None, help="Deploy target name from deploy.yaml")
@click.option(
    "--config",
    "config_file",
    default="deploy.yaml",
    show_default=True,
    help="Path to deploy config file",
)
@click.option(
    "--apply",
    "apply_mode",
    is_flag=True,
    default=False,
    help="Actually execute deploy (default: dry-run)",
)
@click.option("--rollback", is_flag=True, default=False, help="Rollback to last snapshot")
@click.option(
    "--healthcheck-timeout",
    default=60,
    show_default=True,
    help="Healthcheck timeout in seconds",
)
def deploy(target, config_file, apply_mode, rollback, healthcheck_timeout):
    """Deploy project to configured target.

    Dry-run by default — pass --apply to execute.
    Use in CI: codd deploy --target vps --apply
    """
    from codd.deployer import run_deploy

    project_root = Path(".").resolve()
    try:
        exit_code = run_deploy(
            project_root,
            target_name=target,
            config_path=project_root / config_file,
            dry_run=not apply_mode,
            rollback_flag=rollback,
            healthcheck_timeout=healthcheck_timeout,
            emit_output=True,
        )
    except CoddCLIError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    raise SystemExit(exit_code)


@main.command("fixup-drift")
@project_root_option()
@click.option(
    "--dry-run/--apply",
    default=True,
    show_default=True,
    help="Dry-run shows proposals; --apply writes via an isolated git worktree.",
)
@click.option(
    "--severity",
    type=click.Choice(["red", "amber", "green", "all"]),
    default="red",
    show_default=True,
    help="Severity to process.",
)
@click.option(
    "--kind",
    type=click.Choice(["url_drift", "design_token_drift", "lexicon_violation", "screen_flow_drift", "all"]),
    default="all",
    show_default=True,
    help="Drift kind to fix.",
)
def fixup_drift(path: str, dry_run: bool, severity: str, kind: str):
    """Detect drift and dispatch registered Coherence Engine fix strategies."""
    from codd.fixup_drift import run_fixup_drift

    project_root = Path(path).resolve()
    result = run_fixup_drift(
        project_root,
        dry_run=dry_run,
        severity_filter=severity,
        kind_filter=kind,
    )

    proposals = result.get("proposals", [])
    if dry_run:
        click.echo(f"[Dry-run] {len(proposals)} fix proposal(s) found.")
        for proposal in proposals:
            click.echo(f"  [{proposal.kind}] {proposal.file_path}: {proposal.description}")
            if proposal.diff:
                click.echo(proposal.diff[:500])
        return

    click.echo(
        f"Applied: {result.get('applied', 0)}, "
        f"HITL logged: {result.get('hitl_logged', 0)}"
    )
    for error in result.get("errors", []):
        click.echo(f"Error: {error}")


@main.command()
@click.argument("phenomenon", nargs=-1)
@project_root_option()
@click.option("--max-attempts", default=3, type=click.IntRange(min=1, max=10),
              help="Maximum fix attempts (default: 3)")
@click.option("--test-results", default=None, type=click.Path(exists=True),
              help="Path to test results directory or file")
@click.option("--ci-log", default=None, type=click.Path(exists=True),
              help="Path to CI failure log file")
@click.option("--ci", "ci_only", is_flag=True, help="Only check CI failures (skip local tests)")
@click.option("--local", "local_only", is_flag=True, help="Only run local tests (skip CI check)")
@click.option("--no-push", is_flag=True, help="Don't push fixes after successful fix")
@click.option("--dry-run", is_flag=True, help="Show what would be fixed without making changes")
@click.option("--ai-cmd", default=None, help="Override AI CLI command")
@click.option("--non-interactive", is_flag=True,
              help="PHENOMENON mode: disable interactive prompts (use defaults)")
@click.option("--on-ambiguity",
              type=click.Choice(["abort", "default", "top1"]),
              default="abort",
              show_default=True,
              help="PHENOMENON mode: behavior when candidates are ambiguous in --non-interactive")
@click.option("--allow-delete", is_flag=True,
              help="PHENOMENON mode: allow design_doc to lose existing acceptance criteria / user journeys")
@click.option("--strategy",
              type=click.Choice(["patch", "regenerate"]),
              default="patch",
              show_default=True,
              help="PHENOMENON mode: impl propagation strategy (regenerate is reserved/not implemented)")
@click.option("--propagate", "propagate_design", is_flag=True,
              help="PHENOMENON mode: after a verified fix, run `codd propagate --update` to reconcile dependent design docs")
@click.option("--no-propagate-impl", is_flag=True,
              help="PHENOMENON mode: stop at the design-doc update; do NOT propagate into implementation/tests")
def fix(phenomenon: tuple[str, ...], path: str, max_attempts: int,
        test_results: str | None, ci_log: str | None,
        ci_only: bool, local_only: bool, no_push: bool, dry_run: bool,
        ai_cmd: str | None, non_interactive: bool, on_ambiguity: str,
        allow_delete: bool, strategy: str, propagate_design: bool,
        no_propagate_impl: bool):
    """Fix test/build failures or a user-described PHENOMENON.

    \b
    Two modes:
      codd fix                              # legacy: auto-detect failures
      codd fix "ログインがわかりにくい"     # PHENOMENON mode

    \b
    Legacy mode (no positional argument) auto-detects failure source:
      1. Explicit --test-results / --ci-log files
      2. CI failures via `gh run view`
      3. Local test execution
    Maps failures to relevant design docs via the dependency graph,
    then invokes Claude Code to fix implementation code.

    \b
    PHENOMENON mode (with positional argument) drives the second entry
    point of CoDD's north star: starting from a phenomenon the user wants
    fixed, CoDD updates the design doc, propagates the change, and
    verifies — the user touches nothing. Argument-less invocation
    remains completely unchanged.

    \b
    Examples:
      codd fix                                          # auto-detect and fix
      codd fix --ci                                     # fix latest CI failure
      codd fix --local                                  # run tests locally and fix
      codd fix --dry-run                                # show plan without fixing
      codd fix --no-push                                # fix but don't push
      codd fix "ログインエラーをわかりやすくしたい"     # PHENOMENON mode
      codd fix "Button needs aria-label" --non-interactive
    """
    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    phenomenon_text = " ".join(phenomenon).strip()

    if phenomenon_text:
        _run_phenomenon_fix_cli(
            project_root,
            phenomenon_text,
            ai_cmd=ai_cmd,
            max_attempts=max_attempts,
            non_interactive=non_interactive,
            on_ambiguity=on_ambiguity,
            allow_delete=allow_delete,
            dry_run=dry_run,
            push=not no_push,
            strategy=strategy,
            propagate_design=propagate_design,
            propagate_impl=(False if no_propagate_impl else None),
        )
        return

    from codd.fixer import run_fix

    if ci_only and local_only:
        click.echo("Error: --ci and --local are mutually exclusive.")
        raise SystemExit(1)

    if dry_run:
        click.echo("🔍 Dry run — analyzing failures without making changes...")

    result = run_fix(
        project_root,
        ai_command=ai_cmd,
        max_attempts=max_attempts,
        test_results=test_results,
        ci_log=ci_log,
        ci_only=ci_only,
        local_only=local_only,
        push=not no_push,
        dry_run=dry_run,
    )

    if not result.attempts:
        click.echo("✅ All tests passed. Nothing to fix.")
        return

    if dry_run:
        click.echo(f"\n📊 Found {len(result.attempts[0].failures)} failure(s):")
        for f in result.attempts[0].failures:
            click.echo(f"  [{f.category}] {f.summary}")
            if f.failed_files:
                click.echo(f"    Files: {', '.join(f.failed_files)}")
        click.echo(f"\nSource: {result.source}")
        click.echo("Run without --dry-run to fix.")
        return

    for attempt in result.attempts:
        status = "✅" if attempt.fixed else "❌"
        click.echo(f"\n{status} Attempt {attempt.attempt}/{max_attempts}")
        click.echo(f"  Failures: {len(attempt.failures)}")

    if result.fixed:
        click.echo(f"\n✅ All failures fixed in {len(result.attempts)} attempt(s).")
        if result.pushed:
            click.echo("📤 Fixes pushed to remote.")
            if result.ci_passed is True:
                click.echo("✅ CI passed.")
            elif result.ci_passed is False:
                click.echo("❌ CI still failing after fix.")
            elif result.ci_passed is None:
                click.echo("👀 CI status unknown (gh CLI unavailable or timed out).")
        elif not no_push:
            click.echo("⚠️  Push failed. Run `git push` manually.")
    else:
        click.echo(f"\n❌ Could not fix all failures after {max_attempts} attempts.")
        click.echo("Review the errors above and fix manually.")
        raise SystemExit(1)


def _run_phenomenon_fix_cli(
    project_root: Path,
    phenomenon_text: str,
    *,
    ai_cmd: str | None,
    max_attempts: int,
    non_interactive: bool,
    on_ambiguity: str,
    allow_delete: bool,
    dry_run: bool,
    push: bool,
    strategy: str = "patch",
    propagate_design: bool = False,
    propagate_impl: bool | None = None,
) -> None:
    """CLI adapter for the PHENOMENON-mode fix pipeline."""
    from codd.fix import run_phenomenon_fix

    if dry_run:
        click.echo("🔍 Dry run — analyzing phenomenon without applying changes...")
    click.echo(f"🩺 Phenomenon: {phenomenon_text}")

    result = run_phenomenon_fix(
        project_root,
        phenomenon_text,
        ai_command=ai_cmd,
        non_interactive=non_interactive,
        on_ambiguity=on_ambiguity,
        max_attempts=max_attempts,
        dry_run=dry_run,
        push=push,
        allow_delete=allow_delete,
        strategy=strategy,
        propagate=propagate_design,
        propagate_impl=propagate_impl,
    )

    if result.analysis is not None:
        a = result.analysis
        click.echo(
            f"   intent={a.intent}, ambiguity={a.ambiguity_score:.2f}, "
            f"subject_terms={a.subject_terms[:4]}"
        )

    if result.aborted:
        click.echo(f"❌ Aborted: {result.abort_reason}")
        raise SystemExit(1)

    if result.selection is not None:
        cands = result.selection.candidates
        click.echo(f"   {len(cands)} candidate design doc(s) considered.")

    if not result.attempts:
        click.echo("ℹ️  No candidate produced a usable update.")
        raise SystemExit(1)

    applied_any = False
    for att in result.attempts:
        status = "✅" if att.applied else ("🔸" if att.aborted_reason == "dry_run: not applying" else "❌")
        target_id = att.target.node_id if att.target else "(no target)"
        click.echo(f"{status} Attempt {att.attempt} on {target_id}")
        if att.update is not None and att.update.diff:
            for line in att.update.diff.splitlines()[:40]:
                click.echo(f"  {line}")
            extra = len(att.update.diff.splitlines()) - 40
            if extra > 0:
                click.echo(f"  ... (+{extra} more diff lines)")
        if att.risk is not None and att.risk.risky:
            click.echo(f"  ⚠️ risk: {', '.join(att.risk.categories) or 'unspecified'} — {att.risk.summary}")
        if att.aborted_reason:
            click.echo(f"  reason: {att.aborted_reason}")
        if att.applied:
            applied_any = True

    if dry_run:
        if result.propagate_impl_enabled and (
            result.affected_impl_paths or result.affected_test_paths
        ):
            click.echo("\n🛠️  Implementation propagation would touch:")
            for p in result.affected_impl_paths:
                click.echo(f"   impl: {p}")
            for p in result.affected_test_paths:
                click.echo(f"   test: {p}")
        elif result.propagate_impl_enabled:
            click.echo("\nℹ️  No implementation files resolved from the DAG for propagation.")
        click.echo("\n🔎 Dry run complete — no files were modified.")
        return

    if applied_any:
        click.echo(f"\n✅ Updated {len(result.applied_paths)} design doc(s): "
                   f"{', '.join(result.applied_paths)}")
        _emit_impl_propagation_result(result)
        _emit_design_propagation_result(result)
        click.echo("   Review the diff above and commit when satisfied.")
    else:
        click.echo("\n❌ No design doc was updated.")
        raise SystemExit(1)


def _emit_impl_propagation_result(result) -> None:
    """Render the Stage-4 implementation propagation outcome."""
    prop = result.propagation
    if prop is None:
        if result.propagate_impl_enabled:
            click.echo("   (implementation propagation produced no changes)")
        return
    if prop.skipped_reason:
        click.echo(f"   ⏭️  Impl propagation skipped: {prop.skipped_reason}")
        return
    if prop.verified:
        click.echo(
            f"   ✅ Implementation propagated and verified "
            f"({len(prop.written_paths)} file(s)):"
        )
        for p in prop.written_paths:
            click.echo(f"      {p}")
        return
    # Not verified → targeted rollback already happened.
    click.echo("   ❌ Implementation propagation failed the verification gate.")
    if prop.rolled_back:
        click.echo(
            f"      Rolled back {len(prop.rolled_back_paths)} file(s) to pre-run state "
            "(design doc update kept; impl left unchanged)."
        )
    last = prop.attempts[-1] if prop.attempts else None
    if last is not None and last.failure_summary:
        click.echo(f"      Last failure: {last.failure_summary}")


def _emit_design_propagation_result(result) -> None:
    """Render the optional Stage-5 design propagation outcome."""
    if result.design_propagation_error:
        click.echo(
            f"   ⚠️  Design propagation (--propagate) could not run: "
            f"{result.design_propagation_error}"
        )
        return
    dp = result.design_propagation
    if dp is None:
        return
    updated = getattr(dp, "updated", None) or []
    affected = getattr(dp, "affected_docs", None) or []
    if affected:
        click.echo(
            f"   🔁 Design propagation: {len(updated)} updated / "
            f"{len(affected)} affected dependent design doc(s)."
        )


@main.command()
@project_root_option()
def policy(path: str):
    """Check source code against enterprise policy rules.

    Policies are defined in codd.yaml under the 'policies' key.
    Each rule specifies a regex pattern and whether it's forbidden or required.

    Exit code: 0 = all pass, 1 = critical violations found.
    """
    from codd.policy import run_policy, format_policy_text

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    try:
        result = run_policy(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(format_policy_text(result))
    raise SystemExit(0 if result.pass_ else 1)


def _screen_flow_drift_payload(result):
    return {
        "design_only": result.design_only,
        "impl_only": result.impl_only,
        "mismatch": result.mismatch,
        "total_design": result.total_design,
        "total_impl": result.total_impl,
    }


def _screen_flow_edge_source(edge: dict) -> str:
    source = edge.get("source") or edge.get("source_file") or ""
    source_line = edge.get("source_line")
    if source and source_line:
        return f"{source}:{source_line}"
    return str(source)


def _emit_screen_flow_drift_text(result) -> None:
    click.echo("=== Screen Flow Drift (Design vs Implementation) ===")
    click.echo(f"Total design transitions: {result.total_design}")
    click.echo(f"Total impl transitions: {result.total_impl}")

    click.echo("\nDesign-only (in design, missing from implementation):")
    if result.design_only:
        for edge in result.design_only:
            click.echo(f"  [AMBER] {edge['from']} -> {edge['to']} (trigger: {edge.get('trigger', '')})")
            source = _screen_flow_edge_source(edge)
            if source:
                click.echo(f"          Source: {source}")
            click.echo("          Status: No matching transition found in implementation code")
    else:
        click.echo("  none")

    click.echo("\nImpl-only (in implementation, missing from design):")
    if result.impl_only:
        for edge in result.impl_only:
            click.echo(f"  [AMBER] {edge['from']} -> {edge['to']} (trigger: {edge.get('trigger', '')})")
            source = _screen_flow_edge_source(edge)
            if source:
                click.echo(f"          Source: {source}")
    else:
        click.echo("  none")

    click.echo("\nMismatch (transition exists but trigger differs):")
    if result.mismatch:
        for mismatch in result.mismatch:
            edge = mismatch["edge"]
            click.echo(f"  [WARN]  {edge['from']} -> {edge['to']}")
            click.echo(f"          Design trigger: {mismatch.get('design_trigger', '')}")
            click.echo(f"          Impl trigger: {mismatch.get('impl_trigger', '')}")
    else:
        click.echo("  none")

    click.echo(
        "\nSummary: "
        f"{len(result.design_only)} design-only, "
        f"{len(result.impl_only)} impl-only, "
        f"{len(result.mismatch)} mismatch"
    )


@main.command("drift")
@project_root_option()
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "json"]),
    help="Output format",
)
@click.option("--e2e", is_flag=True, help="Compare screen-transitions.yaml routes with E2E URL assertions")
@click.option(
    "--screen-flow",
    "screen_flow",
    is_flag=True,
    help="Compare screen-transitions.yaml with implementation-extracted transition edges",
)
def drift(path: str, output_format: str, e2e: bool, screen_flow: bool):
    """Detect drift between design-referenced URLs and implementation endpoints.

    Exit code 0 = no drift. Exit code 1 = drift detected (use in CI).
    """
    from codd.drift import compute_screen_flow_drift, detect_screen_transition_drift, run_drift

    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    if e2e or screen_flow:
        config = load_project_config(project_root)
        exit_code = 0
        e2e_payload = None
        screen_flow_payload = None
        screen_flow_result = None

        if e2e:
            result = detect_screen_transition_drift(project_root, config)
            e2e_payload = {
                "missing_in_e2e": result.missing_in_e2e,
                "extra_in_e2e": result.extra_in_e2e,
                "coverage_ratio": result.coverage_ratio,
            }
            if result.missing_in_e2e:
                exit_code = 1

        if screen_flow:
            screen_flow_result = compute_screen_flow_drift(project_root, extractor_config=config)
            screen_flow_payload = _screen_flow_drift_payload(screen_flow_result)
            if screen_flow_result.design_only:
                exit_code = 1

        if output_format == "json":
            payload = (
                {"e2e": e2e_payload, "screen_flow": screen_flow_payload}
                if e2e and screen_flow
                else e2e_payload
                if e2e
                else screen_flow_payload
            )
            click.echo(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if e2e_payload is not None:
                for route in e2e_payload["missing_in_e2e"]:
                    click.echo(f"[drift e2e:missing_in_e2e] {route}  in screen-transitions.yaml")
                for route in e2e_payload["extra_in_e2e"]:
                    click.echo(f"[drift e2e:extra_in_e2e] {route}  in tests/e2e")
                click.echo(f"E2E route coverage: {e2e_payload['coverage_ratio']:.2%}")
                if not e2e_payload["missing_in_e2e"] and not e2e_payload["extra_in_e2e"]:
                    click.echo("No E2E drift detected.")
                else:
                    count = len(e2e_payload["missing_in_e2e"]) + len(e2e_payload["extra_in_e2e"])
                    click.echo(f"\n{count} E2E drift(s) found.")
            if screen_flow_payload is not None:
                if e2e_payload is not None:
                    click.echo("")
                _emit_screen_flow_drift_text(screen_flow_result)
        raise SystemExit(exit_code)

    result = run_drift(project_root, codd_dir)

    if output_format == "json":
        drift_entries = []
        for entry in result.drift:
            payload = {
                "kind": entry.kind,
                "url": entry.url,
                "source": entry.source,
                "closest_match": entry.closest_match,
            }
            if entry.status:
                payload["status"] = entry.status
            if entry.token:
                payload["token"] = entry.token
            drift_entries.append(payload)
        click.echo(
            json.dumps(
                {
                    "design_urls": result.design_urls,
                    "impl_urls": result.impl_urls,
                    "drift": drift_entries,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for entry in result.drift:
            status = f":{entry.status}" if entry.status else ""
            label = f"[drift {entry.kind}{status}]"
            value = entry.token or entry.url
            closest = f"  (closest: {entry.closest_match})" if entry.closest_match else ""
            source = f"  in {entry.source}" if entry.source else ""
            click.echo(f"{label} {value}{source}{closest}")
        if not result.drift:
            click.echo("No drift detected.")
        else:
            click.echo(f"\n{len(result.drift)} drift(s) found.")

    raise SystemExit(result.exit_code)


@main.group(invoke_without_command=True)
@project_root_option()
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format",
)
@click.option("--json", "as_json", is_flag=True, hidden=True, help="Deprecated alias for --format json.")
@click.option("--init", "initialize", is_flag=True, help="Generate wave_config from requirement docs")
@click.option(
    "--derive",
    is_flag=True,
    help="Derive required design artifacts from requirement documents using AI",
)
@click.option(
    "--regenerate-wave-config",
    is_flag=True,
    help="Regenerate wave_config from required_artifacts (WARNING: overwrites existing wave_config)",
)
@click.option("--force", is_flag=True, help="Overwrite existing wave_config during --init")
@click.option("--waves", is_flag=True, help="Output only the total wave count (for shell scripting)")
@click.option("--tasks", is_flag=True, help="Output only the total task count (for shell scripting)")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command for --init/--derive (defaults to codd.yaml ai_command or 'claude --print')",
)
@click.option(
    "--no-contract-gate",
    is_flag=True,
    default=False,
    help="Skip the artifact-contract completion gate for the 'plan' stage (applies to --init)",
)
@click.pass_context
def plan(
    ctx,
    path: str,
    output_format: str,
    as_json: bool,
    initialize: bool,
    derive: bool,
    regenerate_wave_config: bool,
    force: bool,
    waves: bool,
    tasks: bool,
    ai_cmd: str | None,
    no_contract_gate: bool,
):
    """Show wave execution status from configured artifacts.

    Lifecycle (subcommands): derive → show → approve (approve applies
    immediately; there is no separate apply step).
    """
    if ctx.invoked_subcommand is not None:
        return

    from codd.planner import (
        backup_codd_yaml,
        build_plan,
        generate_wave_config_from_artifacts,
        plan_init,
        plan_to_dict,
        render_plan_text,
    )

    as_json = _resolve_output_format(output_format, as_json, "codd plan") == "json"
    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    if derive:
        if initialize:
            raise click.BadOptionUsage("derive", "--derive cannot be used with --init")
        if force:
            raise click.BadOptionUsage("force", "--force requires --init")
        if waves:
            raise click.BadOptionUsage("waves", "--waves cannot be used with --derive")
        if tasks:
            raise click.BadOptionUsage("tasks", "--tasks cannot be used with --derive")
        if as_json and regenerate_wave_config:
            raise click.BadOptionUsage("format", "--format json cannot be used with --regenerate-wave-config")

        from codd import generator as generator_module
        from codd.required_artifacts_deriver import RequiredArtifactsDeriver

        config = load_project_config(project_root)
        resolved_ai_command = generator_module._resolve_ai_command(
            config,
            ai_cmd,
            command_name="plan_derive",
        )
        lexicon_path = project_root / LEXICON_FILENAME
        lexicon_data = _load_lexicon_data_for_update(lexicon_path)
        requirement_docs = _requirement_docs_from_lexicon_data(lexicon_data)

        try:
            from codd.lexicon import ProjectLexicon, validate_lexicon

            lexicon = ProjectLexicon(lexicon_data)
            artifacts = RequiredArtifactsDeriver(
                project_root,
                ai_command=resolved_ai_command,
            ).derive(requirement_docs, lexicon.coverage_decisions)
            lexicon.set_required_artifacts(artifacts)
            output = lexicon.as_dict()
            validate_lexicon(output)
            lexicon_path.write_text(
                yaml.safe_dump(output, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        if as_json:
            click.echo(json.dumps({"required_artifacts": artifacts}, ensure_ascii=False, indent=2))
            return

        click.echo(f"Derived {len(artifacts)} required artifact(s).")
        click.echo(f"Updated {LEXICON_FILENAME}:required_artifacts")
        if regenerate_wave_config:
            click.echo("Regenerating wave_config from required_artifacts...")
            backup_path = backup_codd_yaml(project_root)
            raw_config_path = codd_dir / "codd.yaml"
            raw_config = yaml.safe_load(raw_config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw_config, dict):
                click.echo("Error: codd.yaml must contain a YAML mapping")
                raise SystemExit(1)
            raw_config["wave_config"] = generate_wave_config_from_artifacts(artifacts)
            raw_config_path.write_text(
                yaml.safe_dump(raw_config, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            click.echo(f"Backed up codd.yaml to {backup_path.relative_to(project_root).as_posix()}")
            click.echo(f"Updated {raw_config_path.relative_to(project_root).as_posix()}:wave_config")
        for artifact in artifacts:
            click.echo(f"  - {artifact['id']}: {artifact['title']}")
        return

    if initialize:
        if as_json:
            raise click.BadOptionUsage("format", "--format json cannot be used with --init")

        try:
            result = plan_init(project_root, force=force, ai_command=ai_cmd)
        except FileExistsError:
            if not click.confirm("codd.yaml already contains wave_config. Overwrite it?", default=False):
                click.echo("Aborted: existing wave_config preserved.")
                raise SystemExit(1)
            result = plan_init(project_root, force=True, ai_command=ai_cmd)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        wave_count = len(result.wave_config)
        artifact_count = sum(len(entries) for entries in result.wave_config.values())
        config_rel_path = Path(result.config_path).relative_to(project_root).as_posix()

        if waves:
            click.echo(wave_count)
            return

        click.echo(
            f"Initialized wave_config in {config_rel_path} from {len(result.requirement_paths)} requirement document(s)."
        )
        click.echo(f"Generated {artifact_count} artifact(s) across {wave_count} wave(s).")
        _enforce_stage_contract_gate(project_root, "plan", opt_out=no_contract_gate)
        return

    if regenerate_wave_config:
        raise click.BadOptionUsage("regenerate_wave_config", "--regenerate-wave-config requires --derive")
    if force:
        raise click.BadOptionUsage("force", "--force requires --init")
    if ai_cmd is not None and not waves and not tasks:
        raise click.BadOptionUsage("ai_cmd", "--ai-cmd requires --init or --derive")

    if waves:
        from codd.generator import _load_project_config
        config = _load_project_config(project_root)
        wave_config = config.get("wave_config", {})
        click.echo(len(wave_config))
        return

    if tasks:
        from codd.implementer import get_valid_task_slugs
        click.echo(len(get_valid_task_slugs(project_root)))
        return

    try:
        result = build_plan(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(plan_to_dict(result), ensure_ascii=False, indent=2))
        return

    click.echo(render_plan_text(result))


@plan.command("derive")
@click.option("--design-doc", "design_docs", multiple=True, help="Design document path. May be repeated.")
@click.option(
    "--layer",
    "v_model_layer",
    default="detailed",
    type=click.Choice(["requirement", "basic", "detailed"]),
    show_default=True,
    help="Fallback V-model layer when the document does not declare one.",
)
@click.option("--force", is_flag=True, help="Bypass cached derived tasks")
@click.option("--dry-run", is_flag=True, help="Print derived tasks without writing cache")
@click.option("--merge-into-plan", is_flag=True, help="Append approved derived tasks to the implementation plan")
@project_root_option("project_path")
@click.option("--provider", default=None, help="Plan deriver provider name")
@click.option("--ai-cmd", default=None, help="Override AI command")
def plan_derive_cmd(
    design_docs: tuple[str, ...],
    v_model_layer: str,
    force: bool,
    dry_run: bool,
    merge_into_plan: bool,
    project_path: str,
    provider: str | None,
    ai_cmd: str | None,
):
    """Derive implementation tasks from design documents."""
    from codd.deployment.providers.ai_command_factory import get_ai_command
    from codd.llm.plan_deriver import PLAN_DERIVERS, merge_approved_tasks_into_plan

    project_root = Path(project_path).resolve()
    _require_codd_dir(project_root)
    config = _load_optional_project_config(project_root)
    provider_name = provider or _plan_derive_provider(config)
    deriver_cls = PLAN_DERIVERS.get(provider_name)
    if deriver_cls is None:
        click.echo(f"Error: plan deriver provider not found: {provider_name}")
        raise SystemExit(1)

    try:
        nodes = _plan_design_doc_nodes(project_root, design_docs)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    command = ai_cmd or _plan_derive_command(config)
    ai_command = get_ai_command(config, project_root, command_override=command)
    deriver = deriver_cls(ai_command)
    tasks = deriver.derive_tasks(
        nodes,
        v_model_layer,  # type: ignore[arg-type]
        {
            "project_root": project_root,
            "force": force,
            "dry_run": dry_run,
            "write_cache": not dry_run,
            "project_context": {"project": config.get("project", {})},
        },
    )

    if dry_run:
        click.echo(yaml.safe_dump([task.to_dict() for task in tasks], sort_keys=False, allow_unicode=True), nl=False)
    else:
        click.echo(f"Derived tasks: {len(tasks)}")

    if merge_into_plan:
        merged = merge_approved_tasks_into_plan(project_root, tasks)
        click.echo(f"Merged approved tasks: {merged}")


@plan.command("show")
@click.option("--design-doc", default=None, help="Filter by design document path")
@click.option(
    "--status",
    "status_filter",
    default="all",
    type=click.Choice(["approved", "pending", "all"]),
    show_default=True,
    help="Approval status filter",
)
@project_root_option("project_path")
def plan_show_cmd(design_doc: str | None, status_filter: str, project_path: str):
    """Show derived implementation tasks."""
    from codd.llm.plan_deriver import iter_derived_task_records

    project_root = Path(project_path).resolve()
    rows = []
    for cache_path, record in iter_derived_task_records(project_root, design_doc):
        for task in record.tasks:
            status = "approved" if task.approved else "pending"
            if status_filter != "all" and status != status_filter:
                continue
            rows.append((cache_path, status, task))

    if not rows:
        click.echo("No derived tasks found")
        return

    for cache_path, status, task in rows:
        click.echo(f"{task.id}\t{status}\t{task.v_model_layer}\t{task.source_design_doc}\t{task.title}")
        click.echo(f"  cache: {_display_path(cache_path, project_root)}")


@plan.command("approve")
@click.argument("design_doc")
@click.option("--task", "task_id", default=None, help="Approve one derived task id")
@click.option("--all", "approve_all", is_flag=True, help="Approve all pending tasks for the design document")
@project_root_option("project_path")
def plan_approve_cmd(design_doc: str, task_id: str | None, approve_all: bool, project_path: str):
    """Approve derived implementation tasks (applied immediately; no separate apply step)."""
    from codd.llm.plan_deriver import approve_cached_tasks, find_derived_task_cache

    if not approve_all and not task_id:
        click.echo("Error: --task or --all is required")
        raise SystemExit(2)

    project_root = Path(project_path).resolve()
    cache_path = find_derived_task_cache(project_root, design_doc)
    try:
        changed = approve_cached_tasks(cache_path, task_id=task_id, approve_all=approve_all)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    click.echo(f"Approved derived tasks: {changed}")


def _plan_derive_command(config: dict[str, Any]) -> str | None:
    value = _nested_config_value(config, ("ai_commands", "plan_derive"))
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) else None
    return None


def _plan_derive_provider(config: dict[str, Any]) -> str:
    value = _nested_config_value(config, ("ai_commands", "plan_derive"))
    if isinstance(value, dict) and isinstance(value.get("provider"), str):
        return value["provider"]
    value = _nested_config_value(config, ("ai_commands", "plan_derive_provider"))
    return value if isinstance(value, str) and value else "subprocess_ai_command"


def _impl_step_command(config: dict[str, Any]) -> str | None:
    value = _nested_config_value(config, ("ai_commands", "impl_step_derive"))
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) else None
    return None


def _impl_step_provider(config: dict[str, Any]) -> str:
    value = _nested_config_value(config, ("ai_commands", "impl_step_derive"))
    if isinstance(value, dict) and isinstance(value.get("provider"), str):
        return value["provider"]
    value = _nested_config_value(config, ("ai_commands", "impl_step_deriver_provider"))
    return value if isinstance(value, str) and value else "subprocess_ai_command"


def _best_practice_command(config: dict[str, Any]) -> str | None:
    value = _nested_config_value(config, ("ai_commands", "best_practice_augment"))
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) else None
    return None


def _best_practice_provider(config: dict[str, Any]) -> str:
    value = _nested_config_value(config, ("ai_commands", "best_practice_augment"))
    if isinstance(value, dict) and isinstance(value.get("provider"), str):
        return value["provider"]
    value = _nested_config_value(config, ("ai_commands", "best_practice_augmenter_provider"))
    return value if isinstance(value, str) and value else "subprocess_ai_command"


def _optional_bool(value: str | bool | None) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise click.BadParameter("expected true or false")


def _implement_task_for_cli(project_root: Path, config: dict[str, Any], task_id: str):
    from codd.implementer import ImplementSpec

    output_paths = _implement_output_paths_for_cli(config, task_id)
    return ImplementSpec(design_node=task_id, output_paths=output_paths)


def _implement_output_paths_for_cli(config: dict[str, Any], design_node: str) -> list[str]:
    implement = config.get("implement") if isinstance(config.get("implement"), dict) else {}
    for key in ("default_output_paths", "implement_targets"):
        mapping = implement.get(key) if isinstance(implement, dict) else None
        if not isinstance(mapping, dict) or design_node not in mapping:
            continue
        value = mapping[design_node]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            paths = [str(item) for item in value if str(item).strip()]
            if paths:
                return paths
    # No explicit mapping: fall back to ONE shared canonical source root so
    # that every unconfigured task (e.g. greenfield-derived tasks) amends the
    # same application layout. The old per-task default (src/<task_slug>/)
    # fragmented a greenfield build into N disjoint app copies — observed in
    # the 2026-06 real-AI dogfood where 15 derived tasks each produced their
    # own cli/core/storage under src/<task_id>/. Precedence:
    #   implement.output_root (explicit) > scan.source_dirs[0] > "src".
    output_root = implement.get("output_root") if isinstance(implement, dict) else None
    if isinstance(output_root, str) and output_root.strip():
        return [output_root.strip()]
    scan = config.get("scan") if isinstance(config.get("scan"), dict) else {}
    source_dirs = scan.get("source_dirs") if isinstance(scan, dict) else None
    if isinstance(source_dirs, list):
        for item in source_dirs:
            text = str(item).strip()
            if text:
                return [text]
    return ["src"]


def _nested_config_value(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _plan_design_doc_nodes(project_root: Path, design_docs: tuple[str, ...]):
    from codd.dag import Node
    from codd.dag.builder import build_dag
    from codd.dag.extractor import extract_design_doc_metadata

    if not design_docs:
        built_dag = build_dag(project_root)
        nodes = [node for node in built_dag.nodes.values() if node.kind == "design_doc"]
        if not nodes:
            raise ValueError("no design documents found")
        return nodes

    nodes = []
    for design_doc in design_docs:
        path = Path(design_doc).expanduser()
        if not path.is_absolute():
            path = project_root / path
        if not path.is_file():
            raise FileNotFoundError(f"design document not found: {design_doc}")
        rel_path = path.relative_to(project_root).as_posix()
        metadata = extract_design_doc_metadata(path)
        attributes = metadata.get("attributes") or {}
        nodes.append(
            Node(
                id=rel_path,
                kind="design_doc",
                path=rel_path,
                attributes={
                    "frontmatter": metadata["frontmatter"],
                    "depends_on": metadata["depends_on"],
                    "node_id": metadata.get("node_id"),
                    "body": metadata.get("body", ""),
                    **attributes,
                },
            )
        )
    return nodes


def _load_lexicon_data_for_update(lexicon_path: Path) -> dict[str, Any]:
    if not lexicon_path.exists():
        return {
            "node_vocabulary": [],
            "naming_conventions": [],
            "design_principles": [],
            "coverage_decisions": [],
            "required_artifacts": [],
        }
    data = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{LEXICON_FILENAME} must contain a YAML mapping")
    data.setdefault("node_vocabulary", [])
    data.setdefault("naming_conventions", [])
    data.setdefault("design_principles", [])
    data.setdefault("coverage_decisions", [])
    data.setdefault("required_artifacts", [])
    return data


def _requirement_docs_from_lexicon_data(data: dict[str, Any]) -> list[str]:
    value = (
        data.get("requirement_docs_path")
        or data.get("requirement_doc_paths")
        or data.get("requirement_docs")
    )
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


@main.command()
@project_root_option()
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format",
)
@click.option("--json", "as_json", is_flag=True, hidden=True, help="Deprecated alias for --format json.")
def measure(path: str, output_format: str, as_json: bool):
    """Show project metrics — graph health, coverage, quality, and health score.

    Collects metrics about the dependency graph, document coverage,
    validation status, and policy compliance. Useful for dashboards
    and tracking CoDD effectiveness over time.
    """
    from codd.measure import run_measure, format_measure_text, format_measure_json

    as_json = _resolve_output_format(output_format, as_json, "codd measure") == "json"
    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    try:
        result = run_measure(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(format_measure_json(result) if as_json else format_measure_text(result))


@main.command("mcp-server")
@click.option(
    "--path",
    "--project-path",
    "--project",
    "project",
    default=".",
    show_default=True,
    help="Project root directory",
)
def mcp_server(project: str):
    """Start MCP server for AI tool integration (stdio).

    Exposes CoDD tools (validate, impact, policy, audit, scan) via the
    Model Context Protocol. Compatible with Claude Code, Cursor, and
    other MCP clients.

    Configure in Claude Code:
        "mcpServers": {"codd": {"command": "codd", "args": ["mcp-server"]}}
    """
    from codd.mcp_server import run_stdio

    project_root = Path(project).resolve()
    _require_codd_dir(project_root)
    run_stdio(project_root)


@main.group("test", invoke_without_command=True)
@project_root_option("project_path")
@click.option("--related", multiple=True, help="Run only tests related to these files")
@click.option("--dry-run", is_flag=True, default=False, help="Print the related test command without running it")
@click.pass_context
def test_cmd(ctx, project_path: str, related: tuple[str, ...], dry_run: bool):
    """Run tests. Use --related <file> to run only related tests."""
    if ctx.invoked_subcommand is not None:
        return

    from codd.watch.test_runner import run_related_tests

    project_root = Path(project_path).resolve()
    if not related:
        click.echo("Use --related <file> to specify files. Full test run not supported via this command.")
        return

    result = run_related_tests(project_root, list(related), dry_run=dry_run)
    click.echo(f"Related tests: {result['related']}")
    if result.get("cmd"):
        click.echo(f"Command: {result['cmd']}")
    click.echo(f"Status: {result['status']}")
    if result.get("exit_code") not in (None, 0):
        raise SystemExit(1)


@test_cmd.command("audit")
@project_root_option()
@click.option("--output", default=None, help="Output report path")
@click.option(
    "--format",
    "output_format",
    default="md",
    show_default=True,
    type=click.Choice(["md", "json"]),
    help="Output report format",
)
@click.option(
    "--docs",
    "docs",
    multiple=True,
    help="Test document file or directory declaring VB traceability tables",
)
@click.option("--test-dir", "test_dirs", multiple=True, help="Test directory or file to scan")
def test_audit(
    path: str,
    output: str | None,
    output_format: str,
    docs: tuple[str, ...],
    test_dirs: tuple[str, ...],
) -> None:
    """Audit verifiable-behavior (VB) coverage from test-document traceability tables."""
    from codd.verifiable_behavior_audit import build_vb_coverage_audit, write_vb_coverage_audit

    project_root = Path(path).resolve()
    output_path = Path(output or f"docs/test/vb-coverage-audit.{output_format}").expanduser()
    if not output_path.is_absolute():
        output_path = project_root / output_path

    report = build_vb_coverage_audit(
        project_root,
        docs=docs or None,
        test_dirs=test_dirs or None,
    )
    written_path = write_vb_coverage_audit(report, output_path, output_format=output_format)
    summary = report.summary
    click.echo(
        "Verifiable behavior audit: "
        f"{summary['vb_count']} behavior(s), "
        f"{summary['covered']} covered, "
        f"{summary['blocked']} blocked, "
        f"{summary['uncovered']} uncovered, "
        f"{summary['orphan_vb_markers']} orphan vb marker(s): "
        f"{_display_path(written_path, project_root)}"
    )


@main.group(cls=_AliasedGroup, aliases={"list": "show"})
def llm():
    """Manage LLM-derived considerations.

    Lifecycle: derive → show → approve (approve applies immediately; alias:
    list deprecated). `skip` marks a consideration as skipped.
    """
    pass


@llm.command("derive")
@click.argument("design_doc", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@project_root_option("project_path")
@click.option("--ai-cmd", default=None, help="Override AI CLI command")
@click.option("--model", default=None, help="Override AI model")
@click.option("--force", is_flag=True, help="Bypass cached derived considerations")
def llm_derive(design_doc: Path, project_path: str, ai_cmd: str | None, model: str | None, force: bool):
    """Derive considerations for a design document."""
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    from codd.deployment.providers.llm_consideration import LlmConsiderationProvider
    from codd.llm.approval import notify_pending_considerations

    project_root = Path(project_path).resolve()
    config = _load_optional_project_config(project_root)
    provider = LlmConsiderationProvider(
        SubprocessAiCommand(command=ai_cmd, project_root=project_root, config=config),
        project_root=project_root,
        cache_dir=project_root / ".codd" / "consideration_cache",
        model=model,
        use_cache=not force,
    )
    result = provider.provide(design_doc.read_text(encoding="utf-8"), {"model": model} if model else {})
    notify_pending_considerations(result.considerations, config)
    click.echo(f"Derived considerations: {len(result.considerations)}")


@llm.command("approve")
@click.argument("consideration_id", required=False)
@click.option("--all", "approve_all", is_flag=True, help="Approve all pending considerations")
@project_root_option("project_path")
def llm_approve(consideration_id: str | None, approve_all: bool, project_path: str):
    """Approve one or all pending considerations."""
    from codd.llm.approval import ApprovalCache, consideration_status, load_cached_considerations

    project_root = Path(project_path).resolve()
    considerations = load_cached_considerations(project_root)
    by_id = {consideration.id: consideration for consideration in considerations}

    if approve_all:
        targets = [
            consideration
            for consideration in considerations
            if consideration_status(consideration, project_root) == "pending"
        ]
    else:
        if not consideration_id:
            click.echo("Error: consideration_id or --all is required")
            raise SystemExit(2)
        if consideration_id not in by_id:
            click.echo(f"Error: consideration not found: {consideration_id}")
            raise SystemExit(1)
        targets = [by_id[consideration_id]]

    for consideration in targets:
        ApprovalCache.save(consideration.id, "approved", project_root)
    click.echo(f"Approved considerations: {len(targets)}")


@llm.command("skip")
@click.argument("consideration_id")
@project_root_option("project_path")
def llm_skip(consideration_id: str, project_path: str):
    """Skip one consideration."""
    from codd.llm.approval import ApprovalCache, load_cached_considerations

    project_root = Path(project_path).resolve()
    known = {consideration.id for consideration in load_cached_considerations(project_root)}
    if consideration_id not in known:
        click.echo(f"Error: consideration not found: {consideration_id}")
        raise SystemExit(1)
    ApprovalCache.save(consideration_id, "skipped", project_root)
    click.echo(f"Skipped consideration: {consideration_id}")


@llm.command("show")
@project_root_option("project_path")
@click.option("--format", "output_format", default="text", type=click.Choice(["text", "json"]))
def llm_show(project_path: str, output_format: str):
    """Show generated considerations with approval status."""
    from codd.llm.approval import consideration_status, consideration_to_dict, load_cached_considerations

    project_root = Path(project_path).resolve()
    rows = []
    for consideration in sorted(load_cached_considerations(project_root), key=lambda item: item.id):
        row = consideration_to_dict(consideration)
        row["status"] = consideration_status(consideration, project_root)
        rows.append(row)

    if output_format == "json":
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not rows:
        click.echo("No considerations found")
        return
    for row in rows:
        description = str(row.get("description") or "")
        click.echo(f"{row['id']}\t{row['status']}\t{description}")


@main.group()
def qc():
    """Expand and evaluate task criteria."""
    pass


@qc.command("expand")
@click.option("--task", "task_id", required=True, help="Task id or task YAML path")
@project_root_option("project_path")
@click.option("--force", is_flag=True, help="Bypass cached expanded criteria")
@click.option(
    "--design-doc",
    "design_docs",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Design document path. Can be repeated.",
)
@click.option(
    "--expected-extraction",
    "expected_extractions",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Expected extraction YAML/JSON path. Can be repeated.",
)
@click.option("--ai-cmd", default=None, help="Override AI CLI command")
@click.option("--model", default=None, help="Override AI model")
def qc_expand(
    task_id: str,
    project_path: str,
    force: bool,
    design_docs: tuple[Path, ...],
    expected_extractions: tuple[Path, ...],
    ai_cmd: str | None,
    model: str | None,
):
    """Generate .codd/expanded_criteria/{task}.yaml."""
    from codd.llm.criteria_expander import (
        SubprocessAiCommandCriteriaExpander,
        expanded_criteria_cache_path,
        load_design_docs,
        load_expected_extractions,
        load_task_criteria,
    )

    project_root = Path(project_path).resolve()
    try:
        task_source = load_task_criteria(project_root, task_id)
        loaded_design_docs = load_design_docs(project_root, design_docs)
        loaded_expected_extractions = load_expected_extractions(expected_extractions)
        expander = SubprocessAiCommandCriteriaExpander(
            ai_command=ai_cmd,
            project_root=project_root,
            model=model,
            use_cache=not force,
        )
        expanded = expander.expand(
            task_source.task_id,
            task_source.static_criteria,
            loaded_design_docs,
            loaded_expected_extractions,
            {
                "project_root": project_root,
                "task_path": str(task_source.path) if task_source.path else "",
                "model": model or "",
                "use_cache": not force,
            },
        )
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    cache_path = expanded_criteria_cache_path(project_root, expanded.task_id)
    click.echo(
        "Expanded criteria: "
        f"static={len(expanded.static_items)} "
        f"dynamic={len(expanded.dynamic_items)} -> "
        f"{_display_path(cache_path, project_root)}"
    )


@qc.command("evaluate")
@click.option("--task", "task_id", required=True, help="Task id or task YAML path")
@project_root_option("project_path")
@click.option("--report-json", is_flag=True, help="Output a machine-readable report")
def qc_evaluate(task_id: str, project_path: str, report_json: bool):
    """Evaluate the saved expanded criteria file."""
    from codd.llm.criteria_expander import (
        evaluate_expanded_criteria,
        expanded_criteria_cache_path,
        load_task_criteria,
        read_expanded_criteria,
    )

    project_root = Path(project_path).resolve()
    try:
        task_source = load_task_criteria(project_root, task_id)
        cache_path = expanded_criteria_cache_path(project_root, task_source.task_id)
        expanded = read_expanded_criteria(cache_path)
        report = evaluate_expanded_criteria(expanded)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if report_json:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(
            "Criteria evaluation: "
            f"PASS={report['pass_count']} "
            f"FAIL={report['fail_count']} "
            f"TOTAL={report['total']} "
            f"(static={report['static_count']}, dynamic={report['dynamic_count']})"
        )
        for item in report["items"]:
            if item["status"] == "FAIL":
                click.echo(f"  FAIL {item['id']}: {', '.join(item['failures'])}")

    raise SystemExit(1 if report["fail_count"] else 0)


def _load_optional_project_config(project_root: Path) -> dict[str, Any]:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _intake_stack_contract_for_verify(project_root: Path, *, stack_command_executor=None) -> None:
    """Resolve the project's declared stack contract into the verify run trace.

    Contract Kernel v2.77a (intake only) — the verify-path mirror of the greenfield
    pipeline's :meth:`GreenfieldPipeline._intake_stack_contract`. Proves the
    framework-stack contract is LIVE-consumed by the verify path (the hash enters
    the run trace), WITHOUT enforcing any obligation or changing the verify verdict
    (obligation enforcement is v2.77b-e).

    ``stack_command_executor`` is the v2.77c materialization seam (default: the real
    subprocess executor): an injectable executor that invokes each composed stack
    command slot (exit-code only). Tests pass a recording/sentinel executor so the
    declared slots are provably invoked without real Next.js/Playwright.

    * No ``stack:`` block → no-op (the opt-in framework layer is unused; the vast
      majority of projects — emits a single trace line, no behaviour change).
    * A declared stack → its ``stack_contract_hash`` is echoed to the run trace.
    * A declared-but-BROKEN stack → honest failure (``SystemExit`` non-zero), never
      a silent skip (anti-false-green / no-silent-fallback).
    """
    from codd.stack.project import stack_contract_intake, stack_contract_trace

    try:
        contract = stack_contract_intake(project_root)
    except Exception as exc:  # noqa: BLE001 — a declared-but-broken stack must fail HONESTLY.
        click.echo(
            "stack contract intake failed: the project declares a `stack:` block in "
            f"codd.yaml but it could not be resolved ({type(exc).__name__}: {exc}). "
            "A declared-but-unresolvable stack is an honest error, never a silent skip.",
            err=True,
        )
        raise SystemExit(1) from exc

    if contract is None:
        # No `stack:` block → byte-identical, UNLESS a committed lock still exists
        # (a removed stack declaration on a still-pinned project = RED; closes the
        # "drop stack: to dodge the gate" bypass — v2.77b anti-false-green).
        from codd.stack.lock import orphan_stack_lock

        orphan = orphan_stack_lock(project_root)
        if orphan is not None and orphan.red:
            click.echo(f"[verify] stack lock gate: {orphan.message}", err=True)
            raise SystemExit(1)
        return
    trace = stack_contract_trace(contract)
    click.echo(
        f"[verify] stack contract intake: {trace['resolved_stack_id']} "
        f"stack_contract_hash={trace['stack_contract_hash']}"
    )

    # Stack lock ENFORCEMENT (Contract Kernel v2.77b) — verify is always an
    # enforcement context (it never CREATES a project), so it uses the strictly
    # READ-ONLY gate: a stack project with no committed lock, or a lock that has
    # drifted from (or cannot be parsed against) the resolved contract, is RED
    # (honest non-zero exit). The gate never writes or refreshes the lock, so a
    # drift cannot be silenced on the verify path.
    from codd.stack.lock import enforce_stack_lock

    gate = enforce_stack_lock(contract, project_root)
    click.echo(f"[verify] stack lock gate: {gate.message}", err=gate.red)
    if gate.red:
        raise SystemExit(1)

    # Stack command MATERIALIZATION (Contract Kernel v2.77c/d) — the verify-path mirror
    # of the greenfield pipeline's :meth:`_materialize_stack_commands`. (1) CONFLICT
    # GATE: a composition conflict (command collision / unproved replace / weakened
    # obligation / exclusive / deny) is RED (honest non-zero exit) — the composer
    # already refuses a silent last-wins merge by recording a Conflict; this makes it
    # a gate. (2) PLAN + EXECUTE: build a deterministic, contract-driven command plan
    # (NO framework literal) and INVOKE each composed slot by exit code, so a declared
    # framework_build/e2e_test is genuinely run on verify (not silently skipped while
    # the language verify greens alone). (3) AUTHENTICITY (v2.77d): exit 0 is necessary
    # but NOT sufficient — a no-op / observed-no-tests / missing-or-unreadable-report
    # slot is RED even on exit 0. The obligation-checker gate is v2.77e (out of lane).
    from codd.stack.command_authenticity import StackCommandAuthenticityError
    from codd.stack.command_plan import (
        StackCommandMaterializationError,
        StackContractConflictError,
        materialize_stack_command_plan,
    )

    try:
        plan, _result = materialize_stack_command_plan(
            contract, project_root, executor=stack_command_executor
        )
    except (
        StackContractConflictError,
        StackCommandMaterializationError,
        StackCommandAuthenticityError,
    ) as exc:
        click.echo(f"[verify] stack command materialization: {exc}", err=True)
        raise SystemExit(1) from exc
    click.echo(
        f"[verify] stack command materialization: {len(plan.slots)} slot(s) invoked "
        f"({', '.join(plan.command_ids)})"
    )

    # Stack obligation CHECKER gate (Contract Kernel v2.77e) — the verify-path mirror of
    # the greenfield pipeline's :meth:`GreenfieldPipeline._enforce_stack_obligations`.
    # AFTER materialization (v2.77c) + authenticity (v2.77d), CHECK the composed
    # framework/addon OBLIGATIONS as a red/green gate: the Next.js ignoreBuildErrors guard
    # reds a build that would pass with type errors; the Playwright e2e_actually_executed
    # obligation reds a 0-test run. Anti-false-green: a missing/disabled/faulting checker
    # or an unenforceable ERROR obligation is RED (honest non-zero exit), never a silent
    # pass. Uses the ALREADY-RESOLVED ``contract`` from intake (no re-resolution from disk
    # — avoids a TOCTOU skip) and the SAME current-run evidence the authenticity layer
    # blessed. A non-stack project never reaches here (byte-identical).
    from codd.stack.project import StackObligationGateError, enforce_stack_obligation_gate

    try:
        enforce_stack_obligation_gate(contract, project_root)
    except StackObligationGateError as exc:
        click.echo(f"[verify] stack obligation gate: {exc}", err=True)
        raise SystemExit(1) from exc
    click.echo(
        f"[verify] stack obligation gate: {len(contract.obligations)} obligation(s) checked "
        "— all enforced obligations satisfied"
    )


def _run_verify_once(
    path: str,
    *,
    prefer_standalone: bool = False,
    runtime_skip: tuple[str, ...] = (),
) -> _CliVerificationResult:
    handler = None if prefer_standalone else get_command_handler("verify")
    if handler is None:
        return _run_standalone_verify_once(path, runtime_skip=runtime_skip)

    try:
        result = handler(path=path)
    except SystemExit as exc:
        exit_code = _system_exit_code(exc)
        passed = exit_code == 0
        failure = None
        if not passed:
            failure = _verification_failure_report(
                "verify",
                [],
                [f"codd verify exited with code {exit_code}"],
                {},
            )
        return _CliVerificationResult(passed=passed, exit_code=exit_code, failure=failure)
    if isinstance(result, _CliVerificationResult):
        return result
    if type(result) is int:
        passed = result == 0
        failure = None
        if not passed:
            failure = _verification_failure_report(
                "verify",
                [],
                [f"codd verify exited with code {result}"],
                {},
            )
        return _CliVerificationResult(passed=passed, exit_code=result, failure=failure)
    return _CliVerificationResult(passed=True, exit_code=0, failure=None)


def _run_standalone_verify_once(path: str, *, runtime_skip: tuple[str, ...] = ()) -> _CliVerificationResult:
    from codd.repair.verify_runner import run_standalone_verify

    if runtime_skip:
        result = run_standalone_verify(Path(path).resolve(), runtime_skip=runtime_skip)
    else:
        result = run_standalone_verify(Path(path).resolve())
    _echo_verification_warnings(result)
    return _cli_result_from_standalone_verify(result)


def _echo_verification_warnings(result: Any) -> None:
    for warning in getattr(result, "warnings", []) or []:
        text = str(warning)
        if not text:
            continue
        prefix = "" if text.upper().startswith("WARNING:") else "WARNING: "
        click.echo(f"{prefix}{text}")


def _emit_verify_summary(result: _CliVerificationResult) -> None:
    check_results = list(result.check_results or [])
    runtime_results = list(result.runtime_results or [])

    dag_pass = sum(1 for item in check_results if _summary_passed(item))
    dag_fail = sum(1 for item in check_results if not _summary_passed(item) and _summary_severity(item) == "red")
    dag_warn = sum(1 for item in check_results if not _summary_passed(item) and _summary_severity(item) != "red")

    runtime_pass = sum(1 for item in runtime_results if _summary_passed(item) and not _summary_skipped(item))
    runtime_fail = sum(1 for item in runtime_results if _summary_value(item, "passed") is False and not _summary_skipped(item))
    runtime_skip = sum(1 for item in runtime_results if _summary_skipped(item))
    runtime_total = len(runtime_results)

    click.echo("[VERIFY SUMMARY]")
    click.echo(f"  DAG checks: {dag_pass} PASS / {dag_fail} FAIL (red) / {dag_warn} WARN (amber)")
    click.echo(
        "  Verification tests: "
        f"{runtime_pass} PASS / {runtime_fail} FAIL / {runtime_skip} SKIP"
        f"{_summary_skip_suffix(runtime_results)} / {runtime_total} total"
    )

    failed_items = [item for item in runtime_results if _summary_value(item, "passed") is False]
    if failed_items:
        click.echo("  Failed nodes:")
        for item in failed_items[:10]:
            click.echo(f"    - {_summary_node_id(item)} -> {_summary_output(item)}")

    skipped_items = [item for item in runtime_results if _summary_skipped(item)]
    if skipped_items:
        for line in _summary_skip_lines(runtime_results):
            click.echo(f"  {line}")
        click.echo("  Skipped nodes:")
        for item in skipped_items[:10]:
            click.echo(f"    - {_summary_node_id(item)} -> {_summary_output(item)}")

    _emit_verify_evidence(result)


_EVIDENCE_CHECK_NAMES = {"source_integrity", "test_command", "typecheck_command"}


def _emit_verify_evidence(result: _CliVerificationResult) -> None:
    """FX3: show WHAT the verification executed, and the evidence failures.

    The DAG/verification-test sections above cannot represent the new
    execution-evidence checks (source integrity, test command, typecheck) —
    without these lines a source-integrity failure would be invisible in the
    summary while the command exits nonzero.
    """
    # getattr with defaults: tests stub _run_verify_once with duck-typed
    # result objects that predate the evidence fields.
    tests_summary = str(getattr(result, "tests_summary", "") or "")
    tests = tests_summary or ("executed" if getattr(result, "tests_executed", False) else "not executed")
    test_command = getattr(result, "test_command", None)
    if test_command:
        tests = f"{tests} [{test_command}]"
    typecheck = "executed" if getattr(result, "typecheck_executed", False) else "not executed"
    integrity = str(getattr(result, "source_integrity", "") or "") or "not checked"
    click.echo(f"  Execution evidence: tests={tests}; typecheck={typecheck}; source integrity={integrity}")

    evidence_failures = [
        item
        for item in (getattr(result, "failures", None) or [])
        if str(_summary_value(item, "check_name") or "") in _EVIDENCE_CHECK_NAMES
    ]
    if evidence_failures:
        click.echo("  Evidence failures:")
        for item in evidence_failures[:10]:
            check_name = str(_summary_value(item, "check_name") or "check")
            click.echo(f"    - {check_name} -> {_summary_output(item)}")


def _summary_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _summary_passed(item: Any) -> bool:
    return _summary_value(item, "passed") is not False


def _summary_severity(item: Any) -> str:
    return str(_summary_value(item, "severity") or "red")


def _summary_skipped(item: Any) -> bool:
    return bool(_summary_value(item, "skipped"))


def _summary_node_id(item: Any) -> str:
    return str(_summary_value(item, "node_id") or _summary_value(item, "node") or "unknown")


def _summary_output(item: Any) -> str:
    output = str(_summary_value(item, "output") or _summary_value(item, "message") or "").strip()
    return output if len(output) <= 180 else f"{output[:177]}..."


def _summary_skip_suffix(runtime_results: list[Any]) -> str:
    reasons = _summary_skip_counts(runtime_results)
    if not reasons:
        return ""
    text = ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
    return f" ({text})"


def _summary_skip_lines(runtime_results: list[Any]) -> list[str]:
    lines: list[str] = []
    for reason, count in sorted(_summary_skip_counts(runtime_results).items()):
        if reason == "verification-test":
            lines.append(f"Skipped: verification-test ({count} nodes by user request)")
        else:
            lines.append(f"Skipped: {reason} ({count} nodes)")
    return lines


def _summary_skip_counts(runtime_results: list[Any]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for item in runtime_results:
        if not _summary_skipped(item):
            continue
        reason = str(_summary_value(item, "skip_reason") or "skipped")
        reasons[reason] = reasons.get(reason, 0) + 1
    return reasons


def _cli_result_from_standalone_verify(result: Any) -> _CliVerificationResult:
    return _CliVerificationResult(
        passed=bool(result.passed),
        exit_code=0 if result.passed else 1,
        failure=getattr(result, "failure", None),
        failures=list(getattr(result, "failures", []) or []),
        check_results=list(getattr(result, "check_results", []) or []),
        runtime_results=list(getattr(result, "runtime_results", []) or []),
        tests_executed=bool(getattr(result, "tests_executed", False)),
        test_command=getattr(result, "test_command", None),
        tests_summary=str(getattr(result, "tests_summary", "") or ""),
        typecheck_executed=bool(getattr(result, "typecheck_executed", False)),
        source_integrity=str(getattr(result, "source_integrity", "") or ""),
    )


def _system_exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    try:
        return int(code)
    except (TypeError, ValueError):
        return 1


def _load_required_repair_config(project_root: Path) -> dict[str, Any] | None:
    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"WARN: codd.yaml repair config is required for repair: {exc}")
        return None

    repair = config.get("repair")
    if not isinstance(repair, dict) or not repair:
        click.echo("WARN: codd.yaml [repair] section is required for repair.")
        return None
    return config


def _run_repair_loop(
    project_root: Path,
    failure: Any,
    *,
    repair_config: dict[str, Any],
    max_attempts: int | None,
    baseline_ref: str | None = None,
    engine_name: str | None,
    verify_callable,
    initial_verify_result: Any | None = None,
):
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    from codd.dag import DAG
    from codd.dag.builder import build_dag
    from codd.repair import RepairLoop, RepairLoopConfig

    try:
        dag = build_dag(project_root)
    except (FileNotFoundError, ValueError):
        dag = DAG()

    resolved_failure = failure if failure is not None else _verification_failure_report("verify", [], [], {})
    if getattr(resolved_failure, "dag_snapshot", None) in ({}, None):
        resolved_failure.dag_snapshot = _dag_snapshot(dag, project_root)

    repair = repair_config.get("repair") if isinstance(repair_config.get("repair"), dict) else {}
    config = RepairLoopConfig(
        max_attempts=_repair_max_attempts(repair, max_attempts),
        approval_mode=str(repair.get("approval_mode") or "required"),  # type: ignore[arg-type]
        history_dir=Path(str(repair.get("history_dir") or ".codd/repair_history")),
        engine_name=str(engine_name or repair.get("engine_name") or repair.get("engine") or "llm"),
        llm_client=SubprocessAiCommand(project_root=project_root, config=repair_config),
        repo_path=project_root,
        # Use the SAME resolved config for the approval gate. For --repair-mode
        # automatic this is the apply_repair_mode copy (carries the per-run
        # opt-in); for plain --auto-repair it is the disk config (owner-gated).
        codd_yaml=repair_config,
    )
    return RepairLoop(config, project_root).run(
        resolved_failure,
        dag,
        verify_callable=verify_callable,
        baseline_ref=baseline_ref,
        initial_verify_result=initial_verify_result,
    )


def _repair_max_attempts(repair: dict[str, Any], max_attempts: int | None) -> int:
    raw = max_attempts if max_attempts is not None else repair.get("max_attempts", 10)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 10


def _dag_snapshot(dag: Any, project_root: Path) -> dict[str, Any]:
    try:
        from codd.dag.builder import dag_to_dict

        return dag_to_dict(dag, project_root)
    except Exception:  # noqa: BLE001 - repair reports should survive DAG serialization failures.
        return {}


def _verification_failure_report(
    check_name: str,
    failed_nodes: list[str],
    error_messages: list[str],
    dag_snapshot: dict[str, Any],
):
    from codd.repair.schema import VerificationFailureReport

    return VerificationFailureReport(
        check_name=check_name,
        failed_nodes=failed_nodes,
        error_messages=error_messages,
        dag_snapshot=dag_snapshot,
        timestamp=_utc_timestamp(),
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repair_exit_code(status: str) -> int:
    return {
        "REPAIR_SUCCESS": 0,
        "PARTIAL_SUCCESS": 2,
        "MAX_ATTEMPTS_REACHED": 2,
        "REPAIR_REJECTED_BY_HITL": 1,
        "REPAIR_EXHAUSTED": 2,
        "REPAIR_FAILED": 3,
    }.get(str(status), 3)


def _load_failure_report(path: Path) -> Any:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("failure report must contain a YAML mapping")
    return _verification_failure_report(
        str(payload.get("check_name") or payload.get("name") or "verify"),
        _repair_string_list(payload.get("failed_nodes") or payload.get("nodes")),
        _repair_string_list(payload.get("error_messages") or payload.get("errors") or payload.get("messages")),
        payload.get("dag_snapshot") if isinstance(payload.get("dag_snapshot"), dict) else {},
    )


def _repair_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if value is None:
        return []
    return [str(value)]


def _repair_history_dir(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    repair = config.get("repair") if isinstance(config, dict) and isinstance(config.get("repair"), dict) else {}
    history_dir = Path(str(repair.get("history_dir") or ".codd/repair_history"))
    if history_dir.is_absolute():
        return history_dir
    return project_root / history_dir


def _session_attempt_dirs(session_dir: Path) -> list[Path]:
    return sorted(
        [path for path in session_dir.glob("attempt_*") if path.is_dir()],
        key=lambda path: _attempt_number(path.name),
    )


def _attempt_number(name: str) -> int:
    try:
        return int(name.removeprefix("attempt_"))
    except ValueError:
        return 10**9


def _read_repair_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _repair_session_summary(session_dir: Path) -> dict[str, Any]:
    final_status = _read_repair_yaml(session_dir / "final_status.yaml") or {}
    attempts = _session_attempt_dirs(session_dir)
    return {
        "history_id": session_dir.name,
        "timestamp": final_status.get("timestamp") or session_dir.name,
        "status": final_status.get("outcome") or final_status.get("status") or "IN_PROGRESS",
        "attempts": len(attempts),
        "path": str(session_dir),
    }


def _session_matches_design_doc(session_dir: Path, design_doc: str | None) -> bool:
    if not design_doc:
        return True
    needle = str(design_doc)
    for path in session_dir.glob("**/*.yaml"):
        try:
            if needle in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def _resolve_history_session(project_root: Path, history_id: str, config: dict[str, Any] | None = None) -> Path:
    raw = Path(history_id).expanduser()
    if raw.is_absolute() and raw.is_dir():
        return raw
    if raw.is_dir():
        return raw.resolve()
    session_dir = _repair_history_dir(project_root, config) / history_id
    if session_dir.is_dir():
        return session_dir
    raise FileNotFoundError(f"repair history session not found: {history_id}")


def _load_repair_proposal(path: Path) -> Any:
    from codd.repair.schema import RepairProposal

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("repair proposal must contain a YAML mapping")
    return RepairProposal(**payload)


@main.group(invoke_without_command=True)
@click.option("--from-report", "from_report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@project_root_option("project_path")
@click.option("--max-attempts", default=None, type=click.IntRange(min=1), help="Maximum repair attempts")
@click.option("--baseline-ref", default=None, help="Baseline git ref for repair classification")
@click.option("--engine", "engine_name", default=None, help="Repair engine name")
@click.pass_context
def repair(
    ctx,
    from_report: Path | None,
    project_path: str,
    max_attempts: int | None,
    baseline_ref: str | None,
    engine_name: str | None,
):
    """Run and inspect repair sessions."""
    if ctx.invoked_subcommand is not None:
        return
    if from_report is None:
        click.echo(ctx.get_help())
        return

    project_root = Path(project_path).resolve()
    repair_config = _load_required_repair_config(project_root)
    if repair_config is None:
        raise SystemExit(1)

    try:
        failure = _load_failure_report(from_report)
        outcome = _run_repair_loop(
            project_root,
            failure,
            repair_config=repair_config,
            max_attempts=max_attempts,
            baseline_ref=baseline_ref,
            engine_name=engine_name,
            verify_callable=lambda: _run_verify_once(path=str(project_root)),
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Repair outcome: {outcome.status}")
    click.echo(f"Repair history: {_display_path(outcome.history_session_dir, project_root)}")
    raise SystemExit(_repair_exit_code(outcome.status))


@repair.command("history")
@project_root_option("project_path")
@click.option("--last", "last", default=10, type=click.IntRange(min=1), show_default=True, help="Number of sessions")
@click.option("--design-doc", "design_doc", default=None, help="Filter sessions containing a design doc path")
def repair_history(project_path: str, last: int, design_doc: str | None):
    """List repair history sessions."""
    from codd.repair.history import RepairHistory

    project_root = Path(project_path).resolve()
    config = _load_optional_project_config(project_root)
    sessions = [
        session
        for session in RepairHistory().list_sessions(_repair_history_dir(project_root, config))
        if _session_matches_design_doc(session, design_doc)
    ][:last]

    if not sessions:
        click.echo("No repair history found.")
        return

    for session_dir in sessions:
        summary = _repair_session_summary(session_dir)
        click.echo(
            f"{summary['history_id']}\t{summary['status']}\t"
            f"attempts={summary['attempts']}\t{summary['timestamp']}"
        )


@repair.command("approve")
@click.argument("history_id")
@click.option("--attempt", "attempt", default=None, type=click.IntRange(min=0), help="Attempt number")
@project_root_option("project_path")
def repair_approve(history_id: str, attempt: int | None, project_path: str):
    """Approve a repair proposal in history."""
    from codd.repair.approval_repair import approve_repair_proposal

    project_root = Path(project_path).resolve()
    config = _load_optional_project_config(project_root)
    try:
        session_dir = _resolve_history_session(project_root, history_id, config)
        attempt_dirs = _session_attempt_dirs(session_dir)
        if not attempt_dirs:
            raise FileNotFoundError("repair attempt not found")
        attempt_dir = session_dir / f"attempt_{attempt}" if attempt is not None else attempt_dirs[-1]
        proposal = _load_repair_proposal(attempt_dir / "repair_proposal.yaml")
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    approval_config = dict(config)
    repair_config = dict(approval_config.get("repair") if isinstance(approval_config.get("repair"), dict) else {})
    repair_config["approval_decision"] = "approved"
    approval_config["repair"] = repair_config
    approved = approve_repair_proposal(
        proposal,
        approval_mode="required",
        codd_yaml=approval_config,
        notify_callable=click.echo,
    )
    if not approved:
        click.echo("Repair proposal not approved.")
        raise SystemExit(1)

    (attempt_dir / "approval.yaml").write_text(
        yaml.safe_dump({"status": "approved", "timestamp": _utc_timestamp()}, sort_keys=False),
        encoding="utf-8",
    )
    click.echo(f"Approved repair proposal: {session_dir.name} attempt={attempt_dir.name.removeprefix('attempt_')}")


@repair.command("status")
@click.argument("history_id", required=False)
@project_root_option("project_path")
def repair_status(history_id: str | None, project_path: str):
    """Show repair session status."""
    from codd.repair.history import RepairHistory

    project_root = Path(project_path).resolve()
    config = _load_optional_project_config(project_root)
    try:
        if history_id is None:
            sessions = RepairHistory().list_sessions(_repair_history_dir(project_root, config))
            if not sessions:
                click.echo("No repair history found.")
                raise SystemExit(1)
            session_dir = sessions[0]
        else:
            session_dir = _resolve_history_session(project_root, history_id, config)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    summary = _repair_session_summary(session_dir)
    click.echo(f"history_id: {summary['history_id']}")
    click.echo(f"status: {summary['status']}")
    click.echo(f"attempts: {summary['attempts']}")
    click.echo(f"timestamp: {summary['timestamp']}")


@main.group()
def dag():
    """DAG Completeness Gate commands."""
    pass


@dag.command("build")
@project_root_option("project_path")
@click.option(
    "--format",
    "output_format",
    default="json",
    type=click.Choice(["json", "mermaid"]),
    help="Output format",
)
@click.option("--cache", is_flag=True, help="Use cached DAG if output exists")
@click.option("--output", default=None, help="Output file (default: .codd/dag.json or .codd/dag.mmd)")
def dag_build(project_path: str, output_format: str, cache: bool, output: str | None):
    """Build the project DAG and output it under .codd/."""
    from codd.dag.builder import (
        build_dag,
        default_dag_json_path,
        default_dag_mermaid_path,
        write_dag_json,
        write_dag_mermaid,
    )

    project_root = Path(project_path).resolve()
    default_output = default_dag_json_path(project_root) if output_format == "json" else default_dag_mermaid_path(project_root)
    output_path = Path(output).expanduser() if output else default_output
    if not output_path.is_absolute():
        output_path = project_root / output_path

    if cache and output_path.exists():
        click.echo(f"Using cached DAG: {_display_path(output_path, project_root)}")
        return

    try:
        built_dag = build_dag(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if output_format == "json":
        if output_path != default_dag_json_path(project_root):
            write_dag_json(built_dag, project_root, output_path)
    else:
        write_dag_mermaid(built_dag, output_path)

    click.echo(
        "Built DAG: "
        f"{len(built_dag.nodes)} nodes, "
        f"{len(built_dag.edges)} edges, "
        f"{len(built_dag.detect_cycles())} cycles -> "
        f"{_display_path(output_path, project_root)}"
    )


@dag.command("verify")
@project_root_option("project_path")
@click.option("--check", "check_names", multiple=True, help="Run specific check(s) only")
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "json"]),
    help="Output format",
)
@click.option(
    "--auto-repair",
    is_flag=True,
    default=False,
    help="Apply violation-output suggestions mechanically (e.g. append "
    "suggested_lexicon_entry to project_lexicon.yaml). Implies a dry-run "
    "preview unless --apply is also supplied.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Write changes to disk when --auto-repair is set. Default: dry run.",
)
def dag_verify(
    project_path: str,
    check_names: tuple[str, ...],
    output_format: str,
    auto_repair: bool,
    apply_changes: bool,
):
    """Run DAG completeness checks."""
    from codd.dag.runner import run_all_checks, unselected_check_names

    project_root = Path(project_path).resolve()
    try:
        results = run_all_checks(project_root, check_names=list(check_names) or None)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    # enabled_checks is an explicit allowlist; surface registered checks it
    # leaves out so a newly shipped check never becomes a silent no-op.
    # Skipped when the user already narrowed the run with --check.
    unselected: list[str] = []
    if not check_names:
        try:
            unselected = unselected_check_names(project_root)
        except Exception:  # visibility aid must never break verification
            unselected = []

    opt_out_results = [result for result in results if _dag_result_status(result) == "opt_out"]
    failed_red = [
        result
        for result in results
        if not _dag_result_passed(result)
        and _dag_result_severity(result) == "red"
        and _dag_result_status(result) != "opt_out"
    ]
    amber_findings = [
        result
        for result in results
        if _dag_result_severity(result) == "amber" and _dag_result_has_findings(result)
    ]

    if output_format == "json":
        click.echo(json.dumps([_dag_result_to_dict(result) for result in results], indent=2, default=str))
        if unselected:
            # stderr keeps the stdout JSON array shape intact for consumers.
            click.echo(
                f"note: {len(unselected)} registered check(s) not selected by enabled_checks: "
                + ", ".join(unselected),
                err=True,
            )
    else:
        for result in results:
            severity = _dag_result_severity(result)
            status_value = _dag_result_status(result)
            if status_value == "opt_out":
                status = "OPT_OUT"
            elif _dag_result_passed(result):
                status = "PASS"
            else:
                status = "WARN" if severity == "amber" else "FAIL"
            click.echo(f"  {status}  {_dag_result_name(result)} [{severity}]")
            for detail in _dag_result_details(result):
                click.echo(f"    {detail}")

        if failed_red:
            click.echo(f"\n{len(failed_red)} check(s) FAILED (severity=red)")
        elif amber_findings:
            click.echo(f"\n{len(amber_findings)} check(s) WARN (severity=amber, deploy allowed)")
        if opt_out_results:
            click.echo(f"\n{len(opt_out_results)} active opt-out(s) (deploy allowed):")
            for result in opt_out_results:
                click.echo(f"  - {_dag_result_name(result)}: {_dag_result_message(result)}")
        if unselected:
            click.echo(
                f"\nnote: {len(unselected)} registered check(s) not selected by enabled_checks "
                f"and therefore not run: {', '.join(unselected)} — add them to dag.enabled_checks "
                "in codd.yaml (or remove the list to run all registered checks)."
            )

    if auto_repair:
        from codd.dag.auto_repair import apply_auto_repair

        outcome = apply_auto_repair(project_root, results, dry_run=not apply_changes)
        click.echo("")
        verb = "Would apply" if not apply_changes else "Applied"
        click.echo(f"{verb} {len(outcome.applied)} repair(s):")
        for action in outcome.applied:
            click.echo(f"  - {action.description}")
        if outcome.skipped:
            click.echo(f"\nSkipped {len(outcome.skipped)} non-repairable violation(s):")
            for action in outcome.skipped:
                click.echo(f"  - {action.description}")

    raise SystemExit(1 if failed_red else 0)


@dag.command("visualize")
@project_root_option("project_path")
def dag_visualize(project_path: str):
    """Build and print the project DAG as Mermaid."""
    from codd.dag.builder import build_dag, render_mermaid

    project_root = Path(project_path).resolve()
    try:
        built_dag = build_dag(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)
    click.echo(render_mermaid(built_dag), nl=False)


@dag.command("journeys")
@project_root_option("project_path")
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "json"]),
    help="Output format",
)
def dag_journeys(project_path: str, output_format: str):
    """List user_journeys declared on design_doc DAG nodes."""
    from codd.dag.builder import build_dag

    project_root = Path(project_path).resolve()
    try:
        built_dag = build_dag(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    journeys = _collect_dag_journeys(built_dag, project_root, _load_optional_project_config(project_root))
    if output_format == "json":
        click.echo(json.dumps(journeys, ensure_ascii=False, indent=2))
        return

    current_doc: str | None = None
    for journey in journeys:
        design_doc = journey["design_doc"]
        if design_doc != current_doc:
            if current_doc is not None:
                click.echo()
            click.echo(design_doc)
            current_doc = design_doc
        required = journey["required_capabilities"]
        requires = ", ".join(required) if required else "-"
        click.echo(f"  {journey['name']} [{journey['criticality']}]  requires: {requires}")


@dag.command("run-journey")
@click.argument("journey_name")
@project_root_option("project_path")
@click.option(
    "--axis",
    "axis_overrides",
    multiple=True,
    metavar="TYPE=VARIANT",
    help="Runtime axis override. Repeat for multiple axes.",
)
@click.option(
    "--config-section",
    default="cdp_browser",
    show_default=True,
    help="verification.templates section used for browser config",
)
def dag_run_journey(
    journey_name: str,
    project_path: str,
    axis_overrides: tuple[str, ...],
    config_section: str,
):
    """Run one declared user_journey with the CDP browser template."""
    from codd.dag.builder import build_dag
    from codd.deployment.providers.verification.cdp_browser import CdpBrowser

    project_root = Path(project_path).resolve()
    try:
        parsed_axes = _parse_axis_overrides(axis_overrides)
        config = load_project_config(project_root)
        template_config = _journey_template_config(config, config_section)
        built_dag = build_dag(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(2)

    journey_record = _find_dag_journey(built_dag, journey_name, project_root, config)
    if journey_record is None:
        click.echo(f"Error: user_journey not found: {journey_name}")
        raise SystemExit(2)

    command = json.dumps(
        _journey_execution_plan(project_root, journey_record, template_config, parsed_axes),
        sort_keys=True,
    )
    result = CdpBrowser(config=template_config).execute(command)
    if result.output:
        click.echo(result.output)
    raise SystemExit(0 if result.passed else 1)


def _journey_template_config(config: dict[str, Any], config_section: str) -> dict[str, Any]:
    verification = config.get("verification")
    templates = verification.get("templates") if isinstance(verification, dict) else None
    if not isinstance(templates, dict) or config_section not in templates:
        raise ValueError(f"verification.templates.{config_section} config not found")

    section = templates[config_section]
    if not isinstance(section, dict):
        raise ValueError(f"verification.templates.{config_section} must be a mapping")
    return dict(section)


def _parse_axis_overrides(axis_overrides: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in axis_overrides:
        if "=" not in raw:
            raise ValueError("--axis must use TYPE=VARIANT")
        axis_type, variant_id = raw.split("=", 1)
        axis = axis_type.strip()
        variant = variant_id.strip()
        if not axis or not variant:
            raise ValueError("--axis must use non-empty TYPE=VARIANT")
        parsed[axis] = variant
    return parsed


def _find_dag_journey(
    dag: Any,
    journey_name: str,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind != "design_doc":
            continue
        for journey in _node_user_journeys(node, project_root, settings):
            if str(journey.get("name") or "") == journey_name:
                return {
                    "design_doc": node.path or node.id,
                    "journey": dict(journey),
                }
    return None


def _journey_execution_plan(
    project_root: Path,
    journey_record: dict[str, Any],
    template_config: dict[str, Any],
    axis_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    journey = dict(journey_record["journey"])
    journey_name = str(journey.get("name") or "")
    steps = journey.get("steps")
    plan = {
        "template": "cdp_browser",
        "test_kind": "e2e",
        "target": _journey_target(journey),
        "identifier": f"journey:{journey_name}",
        "journey": journey_name,
        "steps": steps if isinstance(steps, list) else [],
        "project_root": str(project_root),
        "design_doc": journey_record["design_doc"],
        "config": template_config,
    }
    if axis_overrides:
        plan["axis_overrides"] = dict(axis_overrides)
    return plan


def _journey_target(journey: dict[str, Any]) -> str:
    steps = journey.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("action") == "navigate":
                target = step.get("target") or step.get("url")
                if target:
                    return str(target)

    target = journey.get("target") or journey.get("url")
    return str(target or "")


def _collect_dag_journeys(
    dag: Any,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    journeys: list[dict[str, Any]] = []
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind != "design_doc":
            continue
        for journey in _node_user_journeys(node, project_root, settings):
            journeys.append(
                {
                    "design_doc": node.path or node.id,
                    "name": str(journey.get("name") or ""),
                    "criticality": str(journey.get("criticality") or ""),
                    "required_capabilities": _string_list(journey.get("required_capabilities")),
                }
            )
    return journeys


def _node_user_journeys(
    node: Any,
    project_root: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if project_root is not None:
        from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck

        return UserJourneyCoherenceCheck(project_root=project_root, settings=settings or {})._journey_entries(node)

    attributes = getattr(node, "attributes", {}) or {}
    value = attributes.get("user_journeys")
    if not isinstance(value, list):
        return []
    return [journey for journey in value if isinstance(journey, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _dag_result_to_dict(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        return asdict(result)
    if isinstance(result, dict):
        return result
    return dict(vars(result))


def _dag_result_name(result: Any) -> str:
    return str(_dag_result_value(result, "check_name") or result.__class__.__name__)


def _dag_result_severity(result: Any) -> str:
    return str(_dag_result_value(result, "severity") or "red")


def _dag_result_passed(result: Any) -> bool:
    return _dag_result_value(result, "passed") is not False


def _dag_result_status(result: Any) -> str:
    return str(_dag_result_value(result, "status") or "")


def _dag_result_message(result: Any) -> str:
    return str(_dag_result_value(result, "message") or "")


def _dag_result_has_findings(result: Any) -> bool:
    for key in (
        "violations",
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "incomplete_tasks",
        "unreachable_nodes",
    ):
        value = _dag_result_value(result, key)
        if value:
            return True
    return False


def _dag_result_details(result: Any) -> list[str]:
    details: list[str] = []
    for key in (
        "message",
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "violations",
        "incomplete_tasks",
        "unreachable_nodes",
        "warnings",
    ):
        value = _dag_result_value(result, key)
        if not value:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value[:5])
            if len(value) > 5:
                rendered += f", ... {len(value) - 5} more"
            details.append(f"{key}: {rendered}")
        else:
            details.append(f"{key}: {value}")
    common_count = _dag_result_value(result, "common_node_count")
    if isinstance(common_count, int) and common_count > 0:
        details.append(f"common_node_count: {common_count}")
    return details


def _dag_result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


@main.group()
def hooks():
    """Manage Git hook integration."""
    pass


@hooks.command("install")
@project_root_option()
def hooks_install(path: str):
    """Install the CoDD pre-commit hook into .git/hooks."""
    from codd.hooks import install_pre_commit_hook

    project_root = Path(path).resolve()

    try:
        hook_path, installed = install_pre_commit_hook(project_root)
    except (FileNotFoundError, FileExistsError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if installed:
        click.echo(f"Installed pre-commit hook: {hook_path}")
    else:
        click.echo(f"Pre-commit hook already installed: {hook_path}")


@hooks.command("run-pre-commit", hidden=True)
@project_root_option()
def hooks_run_pre_commit(path: str):
    """Run CoDD pre-commit checks."""
    from codd.hooks import run_pre_commit

    project_root = Path(path).resolve()
    raise SystemExit(run_pre_commit(project_root))


def _render_template(template_name: str, dest: Path, variables: dict):
    """Simple template rendering (replace {{key}} with value)."""
    tmpl_path = TEMPLATES_DIR / template_name
    if not tmpl_path.exists():
        # Create empty file if template doesn't exist yet
        dest.write_text(f"# TODO: template {template_name} not yet created\n", encoding="utf-8")
        return

    content = tmpl_path.read_text(encoding="utf-8")
    for key, value in variables.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    dest.write_text(content, encoding="utf-8")


def _import_requirements(project_root: Path, source: Path, project_name: str) -> Path:
    """Import a requirements file, adding CoDD frontmatter if missing."""
    import re

    content = source.read_text(encoding="utf-8")

    # Check if it already has CoDD frontmatter
    has_frontmatter = content.strip().startswith("---") and "codd:" in content.split("---", 2)[1] if content.strip().startswith("---") and content.count("---") >= 2 else False

    if not has_frontmatter:
        # Derive node_id from project name
        slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
        frontmatter = (
            "---\n"
            "codd:\n"
            f'  node_id: "req:{slug}-requirements"\n'
            "  type: requirement\n"
            "  status: approved\n"
            "  confidence: 0.95\n"
            "---\n\n"
        )
        content = frontmatter + content

    # Place in docs/requirements/
    req_dir = project_root / "docs" / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    dest = req_dir / "requirements.md"
    dest.write_text(content, encoding="utf-8")
    return dest


if __name__ == "__main__":
    main()

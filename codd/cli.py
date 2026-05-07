"""CoDD CLI — codd init / scan / impact / require / plan."""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click
import yaml

from codd.bridge import PRO_COMMAND_INSTALL_MESSAGE, get_command_handler
from codd.config import find_codd_dir, load_project_config
from codd.lexicon import LEXICON_FILENAME, load_lexicon

TEMPLATES_DIR = Path(__file__).parent / "templates"


class CoddCLIError(RuntimeError):
    """Error raised for CLI-facing validation failures."""


@dataclass
class _CliVerificationResult:
    passed: bool
    exit_code: int
    failure: Any | None = None
    failures: list[Any] | None = None


@dataclass(frozen=True)
class _VersionCheckResult:
    installed_version: str
    required_spec: str
    satisfied: bool
    strict: bool
    message: str


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


def _run_pro_command(name: str, **kwargs):
    """Dispatch a Pro-only command when the bridge plugin is installed."""
    handler = get_command_handler(name)
    if handler is None:
        click.echo(PRO_COMMAND_INSTALL_MESSAGE)
        raise SystemExit(1)

    result = handler(**kwargs)
    if type(result) is int:
        raise SystemExit(result)


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
    default_dir = project_root / "codd"
    if hidden_dir.exists():
        return hidden_dir
    if default_dir.exists():
        # Avoid writing config into projects whose source package is already named codd/.
        return hidden_dir
    return default_dir


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
    return config_path, True


@click.group()
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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


@main.command("gungi")
@click.argument("task_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--path", "project_path", default=".", help="Project root directory")
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


def _format_preflight_ntfy(result: Any) -> str:
    failed = [check.name for check in result.checks if check.status in {"FAIL", "WARN"}]
    suffix = f" ({', '.join(failed)})" if failed else ""
    return f"CoDD preflight: {result.task_id} severity={result.severity}{suffix}"


@main.command()
@click.option("--project-name", prompt="Project name", help="Name of the project")
@click.option("--language", prompt="Primary language", help="Primary language (python/typescript/javascript/go — full support; java — symbols only)")
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
def init(project_name: str, language: str, dest: str, requirements: str | None, config_dir: str):
    """Initialize CoDD in a project directory."""
    dest_path = Path(dest).resolve()
    codd_dir = dest_path / config_dir

    if codd_dir.exists():
        if requirements:
            # Import requirements into existing CoDD project
            req_path = _import_requirements(dest_path, Path(requirements), project_name)
            rel_req = req_path.relative_to(dest_path).as_posix()
            click.echo(f"Requirements imported: {rel_req} (frontmatter added)")
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


@main.command()
@click.option("--path", default=".", help="Project root directory")
def scan(path: str):
    """Scan codebase and update dependency graph (Stage 1)."""
    from codd.scanner import run_scan
    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    run_scan(project_root, codd_dir)


@main.command()
@click.option("--diff", default="HEAD", help="Git diff target (default: HEAD, shows uncommitted changes)")
@click.option("--path", default=".", help="Project root directory")
@click.option("--output", default=None, help="Output file (default: stdout)")
def impact(diff: str, path: str, output: str):
    """Analyze change impact from git diff."""
    from codd.propagate import run_impact
    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    run_impact(project_root, codd_dir, diff, output)


@main.command("watch")
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--wave", required=True, type=click.IntRange(min=1), help="Wave number to generate")
@click.option("--path", default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
@click.option("--feedback", default=None, help="Review feedback to address in this generation (from codd review)")
def generate(wave: int, path: str, force: bool, ai_cmd: str | None, feedback: str | None):
    """Generate CoDD documents for a specific wave."""
    from codd.generator import generate_wave, _load_project_config

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

    try:
        results = generate_wave(project_root, wave, force=force, ai_command=ai_cmd, feedback=feedback)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    generated = 0
    skipped = 0

    for result in results:
        rel_path = result.path.relative_to(project_root).as_posix()
        click.echo(f"{result.status.capitalize()}: {rel_path} ({result.node_id})")
        if result.status == "generated":
            generated += 1
        else:
            skipped += 1

    click.echo(f"Wave {wave}: {generated} generated, {skipped} skipped")


@main.command()
@click.option("--wave", required=True, type=click.IntRange(min=1), help="Wave number to restore")
@click.option("--path", default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
@click.option("--feedback", default=None, help="Review feedback to address in this restoration (from codd review)")
def restore(wave: int, path: str, force: bool, ai_cmd: str | None, feedback: str | None):
    """Restore design documents from extracted codebase facts (brownfield).

    Unlike 'generate' which creates design docs from requirements (greenfield),
    'restore' reconstructs design documents from extracted code analysis.
    The AI infers design intent from the actual codebase structure.

    Run 'codd extract' first, then 'codd plan --init' to create wave_config,
    then 'codd restore --wave N' to reconstruct design docs.
    """
    from codd.restore import restore_wave

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

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
@click.option("--path", default=".", help="Project root directory")
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
    "format_name",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Discovery output format.",
)
@click.option("--lexicon", "lexicon_path", default=None, help="Lexicon directory, manifest path, or plug-in name.")
@click.option("--path", "project_path", default=".", help="Project root directory")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or CODD_AI_COMMAND).",
)
@click.pass_context
def elicit(
    ctx: click.Context,
    interactive: bool,
    format_name: str,
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
        lexicon_config = _load_elicit_lexicon(project_root, lexicon_path) if lexicon_path else None
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

    if format_name == "json":
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
    "format_name",
    type=click.Choice(["auto", "md", "json"]),
    default="auto",
    show_default=True,
    help="Input format. Auto uses the file extension.",
)
@click.option("--path", "project_path", default=".", help="Project root directory")
def elicit_apply_cmd(input_file: Path, format_name: str, project_path: str) -> None:
    """Apply approved elicit findings to project state."""
    from codd.elicit.apply import ElicitApplyEngine, load_findings_from_file

    project_root = Path(project_path).resolve()
    try:
        findings = load_findings_from_file(input_file, None if format_name == "auto" else format_name)
        result = ElicitApplyEngine(project_root).apply(findings)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Elicit apply complete: applied={result.applied_count}, skipped={result.skipped_count}")
    for file_path in result.files_updated:
        click.echo(f"Updated: {file_path}")


@main.group("diff", invoke_without_command=True)
@click.option("--extract-input", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--requirements", "requirements_path", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option(
    "--format",
    "format_name",
    type=click.Choice(["json", "md"]),
    default="md",
    show_default=True,
    help="Discovery output format.",
)
@click.option("--interactive", is_flag=True, default=False, help="Review findings inline and apply approved items.")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--path", "project_path", default=".", help="Project root directory")
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
    format_name: str,
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
    extract_path = _project_file(project_root, extract_input, "codd/extracted.md")
    req_path = _project_file(project_root, requirements_path, "docs/requirements/requirements.md")
    output_path = _project_file(project_root, output, "drift_findings.md") if output is not None or format_name == "md" else None

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

    formatted = JsonFormatter().format(findings) if format_name == "json" else MdFormatter().format(findings)
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
    "format_name",
    type=click.Choice(["auto", "md", "json"]),
    default="auto",
    show_default=True,
    help="Input format. Auto uses the file extension.",
)
@click.option("--path", "project_path", default=".", help="Project root directory")
def diff_apply_cmd(input_file: Path, format_name: str, project_path: str) -> None:
    """Apply approved comparison findings to project artifacts."""
    from codd.diff.apply import DiffApplyEngine, load_findings_from_file

    project_root = Path(project_path).resolve()
    try:
        findings = load_findings_from_file(input_file, None if format_name == "auto" else format_name)
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
@click.option("--path", default=".", help="Project root directory")
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
@click.option("--project-path", default=".", show_default=True, help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
@click.option("--task", default=None, help="Generate only one task by task ID or title match")
@click.option("--clean", is_flag=True, default=False, help="Remove existing generated output before re-generating")
@click.option(
    "--max-tasks",
    default=30,
    type=click.IntRange(min=1),
    show_default=True,
    help="Maximum number of tasks to process per session. Abort if plan exceeds this limit.",
)
@click.option(
    "--wave",
    default=None,
    type=click.IntRange(min=1),
    help="Execute only tasks belonging to this wave number.",
)
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or merged CoDD defaults)",
)
@click.option("--use-derived-steps", default=None, help="Inject derived implementation steps: true or false")
@click.pass_context
def implement(
    ctx,
    path: str,
    task: str | None,
    clean: bool,
    max_tasks: int,
    wave: int | None,
    ai_cmd: str | None,
    use_derived_steps: str | None,
):
    """Generate implementation code from the implementation plan."""
    if ctx.invoked_subcommand is not None:
        return

    from codd.implementer import implement_tasks

    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    if clean:
        click.echo("Cleaning src/generated/ ...")

    try:
        implement_kwargs = {
            "task": task,
            "ai_command": ai_cmd,
            "clean": clean,
            "max_tasks": max_tasks,
            "wave": wave,
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


@implement.command("plan")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--design-doc", "design_docs", multiple=True, help="Design document path. May be repeated.")
@click.option("--force", is_flag=True, help="Bypass cached implementation steps")
@click.option("--dry-run", is_flag=True, help="Print derived steps without writing cache")
@click.option("--path", "project_path", default=".", help="Project root directory")
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
    from codd.deployment.providers.ai_command import SubprocessAiCommand
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
    deriver = deriver_cls(SubprocessAiCommand(command=command, project_root=project_root, config=config))
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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


@implement.command("augment")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--design-doc", "design_docs", multiple=True, help="Design document path. May be repeated.")
@click.option("--path", "project_path", default=".", help="Project root directory")
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
    from codd.deployment.providers.ai_command import SubprocessAiCommand
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
    augmenter = augmenter_cls(SubprocessAiCommand(command=command, project_root=project_root, config=config))
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
def implement_run_cmd(
    task_id: str | None,
    project_path: str,
    ai_cmd: str | None,
    use_derived_steps: str,
    chunk_size: int | None,
    timeout_per_chunk: int,
    enable_typecheck_loop: bool,
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
        return

    try:
        results = implement_tasks(
            project_root,
            task=task_id,
            ai_command=ai_cmd,
            use_derived_steps=_optional_bool(use_derived_steps),
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


@implement.command("resume")
@click.option("--task", "task_id", required=True, help="Implementation task id or title match")
@click.option("--history", required=True, help="History id or path from a previous chunked run")
@click.option("--path", "project_path", default=".", help="Project root directory")
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
):
    if not task_id:
        raise ValueError("--task is required when chunked execution is enabled")

    import codd.generator as generator_module
    from codd.implementer.chunked_runner import ChunkedRunner

    config = _load_optional_project_config(project_root)
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
@click.option("--path", default=".", help="Project root directory")
@click.option("--output-dir", default=None, help="Output directory for assembled project (default: src/)")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
def assemble(path: str, output_dir: str | None, ai_cmd: str | None):
    """Assemble generated sprint fragments into a working project."""
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
@click.option("--path", default=".", help="Project root directory")
@click.option("--sprint", default=None, type=click.IntRange(min=1), help="(deprecated, ignored) Sprint number", hidden=True)
@click.option("--e2e", is_flag=True, default=False, help="Run E2E tests (CI-safe, excludes @cdp-only)")
@click.option("--deploy", is_flag=True, default=False, help="Run deploy/CDP-only E2E tests against deployed URL")
@click.option("--base-url", default=None, help="Override BASE_URL for E2E tests")
@click.option(
    "--design-md",
    is_flag=True,
    default=False,
    help="Run npx @google/design.md lint on DESIGN.md (skip if npx unavailable).",
)
@click.option("--auto-repair", is_flag=True, default=False, help="Run RepairLoop when verification fails")
@click.option("--max-attempts", default=None, type=click.IntRange(min=1), help="Maximum repair attempts")
@click.option("--baseline-ref", default=None, help="Baseline git ref for repair classification")
@click.option("--engine", "engine_name", default=None, help="Repair engine name")
def verify(
    path: str,
    sprint: int | None,
    e2e: bool,
    deploy: bool,
    base_url: str | None,
    design_md: bool,
    auto_repair: bool,
    max_attempts: int | None,
    baseline_ref: str | None,
    engine_name: str | None,
) -> None:
    """Run build + test verification and trace failures to design documents."""
    if design_md:
        _run_design_md_lint(Path(path).resolve())
        return
    if e2e or deploy:
        from codd.e2e_runner import run_e2e
        run_e2e(path=path, deploy=deploy, base_url=base_url)
        return

    if not auto_repair:
        _run_pro_command("verify", path=path, sprint=sprint)
        return

    project_root = Path(path).resolve()
    result = _run_verify_once(path=path, sprint=sprint, prefer_standalone=True)
    if result.passed:
        return

    repair_config = _load_required_repair_config(project_root)
    if repair_config is None:
        raise SystemExit(1)

    outcome = _run_repair_loop(
        project_root,
        result.failure,
        repair_config=repair_config,
        max_attempts=max_attempts,
        baseline_ref=baseline_ref,
        engine_name=engine_name,
        verify_callable=lambda: _run_verify_once(path=path, sprint=sprint, prefer_standalone=True),
        initial_verify_result=result,
    )
    click.echo(f"Repair outcome: {outcome.status}")
    click.echo(f"Repair history: {_display_path(outcome.history_session_dir, project_root)}")
    raise SystemExit(_repair_exit_code(outcome.status))


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

    scenarios_path = project_root / "docs" / "e2e" / "scenarios.md"
    if scenarios_path.exists():
        collection = load_scenarios_from_markdown(scenarios_path)
    else:
        collection = ScenarioExtractor(project_root).extract()

    if not collection.scenarios:
        click.echo("No scenarios found. Run `codd e2e extract` first or check docs/e2e/scenarios.md.")
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


@main.group()
def e2e():
    """Generate and manage E2E test artifacts."""
    pass


@e2e.command("generate")
@click.option("--path", default=".", show_default=True, help="Project root")
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
    type=click.Choice(["scenarios", "transitions"]),
    help="Input source for generated E2E tests",
)
def e2e_generate(path: str, base_url: str, output: str | None, framework: str, mode: str) -> None:
    """Generate Playwright or Cypress test files from scenarios or screen transitions."""
    _run_e2e_generate(path=path, base_url=base_url, output=output, framework=framework, mode=mode)


@main.command("e2e-generate")
@click.option("--path", default=".", show_default=True, help="Project root")
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
    type=click.Choice(["scenarios", "transitions"]),
    help="Input source for generated E2E tests",
)
def e2e_generate_legacy(path: str, base_url: str, output: str | None, framework: str, mode: str) -> None:
    """Generate Playwright or Cypress test files from scenarios or screen transitions."""
    _run_e2e_generate(path=path, base_url=base_url, output=output, framework=framework, mode=mode)


@main.group(invoke_without_command=True)
@click.option("--path", default=".", help="Project root directory")
@click.option("--language", default=None, help="Override language detection (python/typescript/javascript/go — full support; java — symbols only)")
@click.option("--source-dirs", default=None, help="Comma-separated source directories (default: auto-detect)")
@click.option("--output", default=None, help="Output directory (default: <config-dir>/extracted/)")
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

    Output goes to the discovered CoDD config dir as draft documents
    (`codd/extracted/` or `.codd/extracted/`). Review and promote
    confirmed docs when ready.
    """
    if ctx.invoked_subcommand is not None:
        return

    project_root = Path(path).resolve()
    bootstrap_codd_dir = _resolve_bootstrap_codd_dir(project_root)
    dirs = [d.strip() for d in source_dirs.split(",") if d.strip()] if source_dirs else None
    output_path = Path(output) if output else bootstrap_codd_dir / "extracted"

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
                ai_cmd = cfg.get("ai_command", 'claude --print --model claude-opus-4-6 --tools ""')
            except FileNotFoundError:
                ai_cmd = 'claude --print --model claude-opus-4-6 --tools ""'

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
            result = run_extract(project_root, language, dirs, str(output_path))
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
@click.option("--path", "project_path", default=".", show_default=True, help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
@click.option("--scope", default=None, help="Review a single document by node_id")
@click.option("--json", "as_json", is_flag=True, help="Output results as JSON")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command)",
)
def review(path: str, scope: str | None, as_json: bool, ai_cmd: str | None):
    """Review design documents for content quality using AI.

    Evaluates artifacts against type-specific criteria (architecture soundness,
    completeness, consistency with upstream docs, etc.) and returns PASS/FAIL
    with a score and detailed feedback.

    Without --scope: reviews all documents.
    With --scope: reviews a single document by node_id.
    """
    _run_pro_command("review", path=path, scope=scope, as_json=as_json, ai_cmd=ai_cmd)


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
@click.option("--path", default=".", help="Project root directory")
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


@main.command()
@click.option("--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def coverage(
    path: str,
    e2e_threshold: float,
    lexicon_threshold: float,
    screen_flow_threshold: float,
    as_json: bool,
):
    """Coverage metrics merge gate: E2E, design tokens, and lexicon."""
    from codd.coverage_metrics import run_coverage

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
@click.option("--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--diff", default="HEAD", help="Git diff target (default: HEAD)")
@click.option("--path", default=".", help="Project root directory")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--skip-review", is_flag=True, help="Skip AI review (faster, no AI cost)")
@click.option("--output", default=None, help="Output file (default: stdout)")
@click.option("--ai-cmd", default=None, help="Override AI command for review phase")
def audit(diff: str, path: str, as_json: bool, skip_review: bool, output: str | None, ai_cmd: str | None):
    """Change review pack — validate + impact + policy + review in one report.

    Produces a consolidated audit report for PM/QA to make merge/release
    decisions. Runs four phases: structural validation, impact analysis,
    policy check (enterprise rules from codd.yaml), and (optionally) AI
    quality review.

    Exit code: 0 = APPROVE, 1 = CONDITIONAL or REJECT.
    """
    _run_pro_command(
        "audit",
        diff=diff,
        path=path,
        as_json=as_json,
        skip_review=skip_review,
        output=output,
        ai_cmd=ai_cmd,
    )


@main.command()
@click.option("--path", default=".", help="Project root directory")
def risk(path: str):
    """Analyze change risk using the codd-pro extension pack."""
    _run_pro_command("risk", path=path)


@main.command()
@click.option("--path", default=".", help="Project root directory")
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
def fix(path: str, max_attempts: int, test_results: str | None, ci_log: str | None,
        ci_only: bool, local_only: bool, no_push: bool, dry_run: bool, ai_cmd: str | None):
    """Fix test/build failures using AI guided by design documents.

    \b
    Auto-detects failure source (in order):
      1. Explicit --test-results / --ci-log files
      2. CI failures via `gh run view`
      3. Local test execution

    Maps failures to relevant design documents via the dependency graph,
    then invokes Claude Code to fix implementation code.

    \b
    Examples:
      codd fix                     # auto-detect and fix
      codd fix --ci                # fix latest CI failure
      codd fix --local             # run tests locally and fix
      codd fix --dry-run           # show plan without fixing
      codd fix --no-push           # fix but don't push
    """
    from codd.fixer import run_fix

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

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


@main.command()
@click.option("--path", default=".", help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
@click.option("--json", "as_json", is_flag=True, help="Output plan as JSON")
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
@click.pass_context
def plan(
    ctx,
    path: str,
    as_json: bool,
    initialize: bool,
    derive: bool,
    regenerate_wave_config: bool,
    force: bool,
    waves: bool,
    tasks: bool,
    ai_cmd: str | None,
):
    """Show wave execution status from configured artifacts."""
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
            raise click.BadOptionUsage("json", "--json cannot be used with --regenerate-wave-config")

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
            raise click.BadOptionUsage("json", "--json cannot be used with --init")

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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
    from codd.deployment.providers.ai_command import SubprocessAiCommand
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
    ai_command = SubprocessAiCommand(command=command, project_root=project_root, config=config)
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
def plan_approve_cmd(design_doc: str, task_id: str | None, approve_all: bool, project_path: str):
    """Approve derived implementation tasks."""
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
    from codd.implementer import _extract_all_tasks, _filter_tasks, _load_implementation_plan

    plan = _load_implementation_plan(project_root, config)
    matches = _filter_tasks(_extract_all_tasks(plan), task_id)
    if not matches:
        raise ValueError(f"no implementation task matched {task_id!r}")
    return matches[0]


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
@click.option("--path", default=".", help="Project root directory")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def measure(path: str, as_json: bool):
    """Show project metrics — graph health, coverage, quality, and health score.

    Collects metrics about the dependency graph, document coverage,
    validation status, and policy compliance. Useful for dashboards
    and tracking CoDD effectiveness over time.
    """
    from codd.measure import run_measure, format_measure_text, format_measure_json

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

    try:
        result = run_measure(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(format_measure_json(result) if as_json else format_measure_text(result))


@main.command("mcp-server")
@click.option("--project", default=".", help="Project root directory")
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


@main.command("test")
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
@click.option("--related", multiple=True, help="Run only tests related to these files")
@click.option("--dry-run", is_flag=True, default=False, help="Print the related test command without running it")
def test_cmd(project_path: str, related: tuple[str, ...], dry_run: bool):
    """Run tests. Use --related <file> to run only related tests."""
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


@main.group()
def llm():
    """Manage LLM-derived considerations."""
    pass


@llm.command("derive")
@click.argument("design_doc", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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


@llm.command("list")
@click.option("--path", "project_path", default=".", help="Project root directory")
@click.option("--format", "output_format", default="text", type=click.Choice(["text", "json"]))
def llm_list(project_path: str, output_format: str):
    """List generated considerations with approval status."""
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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


def _run_verify_once(
    path: str,
    sprint: int | None = None,
    *,
    prefer_standalone: bool = False,
) -> _CliVerificationResult:
    if prefer_standalone or get_command_handler("verify") is None:
        return _run_standalone_verify_once(path)

    try:
        _run_pro_command("verify", path=path, sprint=sprint)
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
    return _CliVerificationResult(passed=True, exit_code=0, failure=None)


def _run_standalone_verify_once(path: str) -> _CliVerificationResult:
    from codd.repair.verify_runner import run_standalone_verify

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


def _cli_result_from_standalone_verify(result: Any) -> _CliVerificationResult:
    return _CliVerificationResult(
        passed=bool(result.passed),
        exit_code=0 if result.passed else 1,
        failure=getattr(result, "failure", None),
        failures=list(getattr(result, "failures", []) or []),
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
            verify_callable=lambda: _run_verify_once(path=str(project_root), sprint=None),
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Repair outcome: {outcome.status}")
    click.echo(f"Repair history: {_display_path(outcome.history_session_dir, project_root)}")
    raise SystemExit(_repair_exit_code(outcome.status))


@repair.command("history")
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--path", "project_path", default=".", help="Project root directory")
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
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
@click.option("--check", "check_names", multiple=True, help="Run specific check(s) only")
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "json"]),
    help="Output format",
)
def dag_verify(project_path: str, check_names: tuple[str, ...], output_format: str):
    """Run DAG completeness checks."""
    from codd.dag.runner import run_all_checks

    project_root = Path(project_path).resolve()
    try:
        results = run_all_checks(project_root, check_names=list(check_names) or None)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    failed_red = [
        result
        for result in results
        if not _dag_result_passed(result) and _dag_result_severity(result) == "red"
    ]
    amber_findings = [
        result
        for result in results
        if _dag_result_severity(result) == "amber" and _dag_result_has_findings(result)
    ]

    if output_format == "json":
        click.echo(json.dumps([_dag_result_to_dict(result) for result in results], indent=2, default=str))
    else:
        for result in results:
            severity = _dag_result_severity(result)
            if _dag_result_passed(result):
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

    raise SystemExit(1 if failed_red else 0)


@dag.command("visualize")
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--project-path", "--path", default=".", show_default=True, help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
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
@click.option("--path", default=".", help="Project root directory")
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

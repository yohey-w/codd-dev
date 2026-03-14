"""CoDD CLI — codd init / scan / impact / plan."""

import click
import json
import os
import shutil
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


@click.group()
@click.version_option(package_name="shogun-codd")
def main():
    """CoDD: Coherence-Driven Development."""
    pass


@main.command()
@click.option("--project-name", prompt="Project name", help="Name of the project")
@click.option("--language", prompt="Primary language", help="Primary language (python/typescript/java/go)")
@click.option("--dest", default=".", help="Destination directory (default: current dir)")
def init(project_name: str, language: str, dest: str):
    """Initialize CoDD in a project directory."""
    dest_path = Path(dest).resolve()
    codd_dir = dest_path / "codd"

    if codd_dir.exists():
        click.echo(f"Error: {codd_dir} already exists.")
        raise SystemExit(1)

    # Create directory structure
    (codd_dir / "reports").mkdir(parents=True)
    (codd_dir / "scan").mkdir(exist_ok=True)

    # Copy templates
    _render_template("codd.yaml.tmpl", codd_dir / "codd.yaml", {
        "project_name": project_name,
        "language": language,
    })
    _render_template("gitignore.tmpl", codd_dir / ".gitignore", {})

    # Version file
    (dest_path / ".codd_version").write_text("0.2.0\n")

    click.echo(f"CoDD initialized in {codd_dir}")
    click.echo(f"  codd.yaml    — project config")
    click.echo(f"  scan/        — JSONL scan output (nodes.jsonl, edges.jsonl)")
    click.echo(f"")
    click.echo(f"Next: Add codd frontmatter to your documents (docs/*.md)")
    click.echo(f"Then: codd scan → builds scan/*.jsonl from all frontmatter")


@main.command()
@click.option("--path", default=".", help="Project root directory")
def scan(path: str):
    """Scan codebase and update dependency graph (Stage 1)."""
    from codd.scanner import run_scan
    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    run_scan(project_root, codd_dir)


@main.command()
@click.option("--diff", default="HEAD~1", help="Git diff target (default: HEAD~1)")
@click.option("--path", default=".", help="Project root directory")
@click.option("--output", default=None, help="Output file (default: stdout)")
def impact(diff: str, path: str, output: str):
    """Analyze change impact from git diff."""
    from codd.propagate import run_impact
    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    run_impact(project_root, codd_dir, diff, output)


@main.command()
@click.option("--wave", required=True, type=click.IntRange(min=1), help="Wave number to generate")
@click.option("--path", default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
def generate(wave: int, path: str, force: bool, ai_cmd: str | None):
    """Generate CoDD documents for a specific wave."""
    from codd.generator import generate_wave

    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    try:
        results = generate_wave(project_root, wave, force=force, ai_command=ai_cmd)
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
@click.option("--sprint", required=True, type=click.IntRange(min=1), help="Sprint number to implement")
@click.option("--path", default=".", help="Project root directory")
@click.option("--task", default=None, help="Generate only one task by task ID or title match")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or merged CoDD defaults)",
)
def implement(sprint: int, path: str, task: str | None, ai_cmd: str | None):
    """Generate implementation code for a specific sprint."""
    from codd.implementer import implement_sprint

    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    try:
        results = implement_sprint(project_root, sprint, task=task, ai_command=ai_cmd)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    generated_files = 0
    for result in results:
        for generated_file in result.generated_files:
            rel_path = generated_file.relative_to(project_root)
            click.echo(f"Generated: {rel_path} ({result.task_id})")
            generated_files += 1

    click.echo(f"Sprint {sprint}: {generated_files} files generated across {len(results)} task(s)")


@main.command()
@click.option("--path", default=".", help="Project root directory")
@click.option("--sprint", default=None, type=click.IntRange(min=1), help="Sprint number to verify")
def verify(path: str, sprint: int | None) -> None:
    """Run build + test verification and trace failures to design documents."""
    from codd.verifier import VerifyPreflightError, run_verify

    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    try:
        result = run_verify(project_root, sprint=sprint)
    except VerifyPreflightError as exc:
        click.echo(f"Preflight check failed: {exc}")
        raise SystemExit(1)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if result.typecheck.success:
        click.echo("Typecheck: PASS")
    else:
        click.echo(f"Typecheck: FAIL ({result.typecheck.error_count} errors)")

    if result.tests.success:
        click.echo(f"Tests: PASS ({result.tests.passed}/{result.tests.total})")
    else:
        click.echo(f"Tests: FAIL ({result.tests.failed} failed, {result.tests.passed} passed)")

    if result.design_refs:
        click.echo("\nDesign documents to review:")
        for ref in result.design_refs:
            click.echo(f"  {ref.node_id} -> {ref.doc_path} (from {ref.source_file})")
        propagate_targets = tuple(dict.fromkeys(ref.node_id for ref in result.design_refs))
        if propagate_targets:
            click.echo("\nSuggested propagate targets:")
            for target in propagate_targets:
                click.echo(f"  {target}")

    for warning in result.warnings:
        click.echo(f"Warning: {warning}")

    click.echo(f"\nReport: {result.report_path}")
    raise SystemExit(0 if result.success else 1)


@main.command()
@click.option("--path", default=".", help="Project root directory")
def validate(path: str):
    """Validate CoDD frontmatter and dependency references."""
    from codd.validator import run_validate

    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

    raise SystemExit(run_validate(project_root, codd_dir))


@main.command()
@click.option("--path", default=".", help="Project root directory")
@click.option("--json", "as_json", is_flag=True, help="Output plan as JSON")
@click.option("--init", "initialize", is_flag=True, help="Generate wave_config from requirement docs")
@click.option("--force", is_flag=True, help="Overwrite existing wave_config during --init")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command for --init (defaults to codd.yaml ai_command or 'claude --print')",
)
def plan(path: str, as_json: bool, initialize: bool, force: bool, ai_cmd: str | None):
    """Show wave execution status from configured artifacts."""
    from codd.planner import build_plan, plan_init, plan_to_dict, render_plan_text

    project_root = Path(path).resolve()
    codd_dir = project_root / "codd"

    if not codd_dir.exists():
        click.echo("Error: codd/ not found. Run 'codd init' first.")
        raise SystemExit(1)

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

        artifact_count = sum(len(entries) for entries in result.wave_config.values())
        config_rel_path = Path(result.config_path).relative_to(project_root).as_posix()
        click.echo(
            f"Initialized wave_config in {config_rel_path} from {len(result.requirement_paths)} requirement document(s)."
        )
        click.echo(f"Generated {artifact_count} artifact(s) across {len(result.wave_config)} wave(s).")
        return

    if force:
        raise click.BadOptionUsage("force", "--force requires --init")
    if ai_cmd is not None:
        raise click.BadOptionUsage("ai_cmd", "--ai-cmd requires --init")

    try:
        result = build_plan(project_root)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(plan_to_dict(result), ensure_ascii=False, indent=2))
        return

    click.echo(render_plan_text(result))


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
        dest.write_text(f"# TODO: template {template_name} not yet created\n")
        return

    content = tmpl_path.read_text()
    for key, value in variables.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    dest.write_text(content)


    # _init_graph_db removed — JSONL files are created on first scan


if __name__ == "__main__":
    main()

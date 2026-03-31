"""CoDD CLI — codd init / scan / impact / plan."""

import click
import json
import os
import shutil
from pathlib import Path

from codd.config import find_codd_dir

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _require_codd_dir(project_root: Path) -> Path:
    """Return the CoDD config dir or exit with a helpful message."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        click.echo("Error: CoDD config dir not found (looked for codd/ and .codd/). Run 'codd init' first.")
        raise SystemExit(1)
    return codd_dir


@click.group()
@click.version_option(package_name="codd-dev")
def main():
    """CoDD: Coherence-Driven Development."""
    pass


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
    })
    _render_template("gitignore.tmpl", codd_dir / ".gitignore", {})

    # Version file
    (dest_path / ".codd_version").write_text("0.2.0\n")

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
@click.option("--wave", required=True, type=click.IntRange(min=1), help="Wave number to restore")
@click.option("--path", default=".", help="Project root directory")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command (defaults to codd.yaml ai_command or 'claude --print')",
)
def restore(wave: int, path: str, force: bool, ai_cmd: str | None):
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
        results = restore_wave(project_root, wave, force=force, ai_command=ai_cmd)
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
    codd_dir = _require_codd_dir(project_root)

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
    codd_dir = _require_codd_dir(project_root)

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
@click.option("--language", default=None, help="Override language detection (python/typescript/javascript/go — full support; java — symbols only)")
@click.option("--source-dirs", default=None, help="Comma-separated source directories (default: auto-detect)")
@click.option("--output", default=None, help="Output directory (default: codd/extracted/)")
def extract(path: str, language: str | None, source_dirs: str | None, output: str | None):
    """Extract design documents from existing codebase (brownfield bootstrap).

    Reverse-engineers CoDD design docs from source code using static analysis.
    No AI required — pure structural fact extraction.

    Output goes to codd/extracted/ as draft documents. Review and promote
    to codd/ when confirmed.
    """
    from codd.extractor import run_extract

    project_root = Path(path).resolve()
    dirs = [d.strip() for d in source_dirs.split(",")] if source_dirs else None

    try:
        result = run_extract(project_root, language, dirs, output)
    except Exception as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    click.echo(f"Extracted: {result.module_count} modules from {result.total_files} files ({result.total_lines:,} lines)")
    click.echo(f"Output: {result.output_dir}/")
    for f in result.generated_files:
        click.echo(f"  {f.relative_to(result.output_dir)}")

    click.echo(f"\nNext steps:")
    click.echo(f"  1. Review generated docs in {result.output_dir}/")
    click.echo(f"  2. Promote confirmed docs: mv codd/extracted/*.md docs/design/")
    click.echo(f"  3. Run: codd scan  (to build the dependency graph)")


@main.command()
@click.option("--path", default=".", help="Project root directory")
def validate(path: str):
    """Validate CoDD frontmatter and dependency references."""
    from codd.validator import run_validate

    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

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
    codd_dir = _require_codd_dir(project_root)

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

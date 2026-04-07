"""CoDD CLI — codd init / scan / impact / require / plan."""

import click
import json
import os
import shutil
from pathlib import Path

from codd.bridge import PRO_COMMAND_INSTALL_MESSAGE, get_command_handler
from codd.config import find_codd_dir

TEMPLATES_DIR = Path(__file__).parent / "templates"


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
        "source_dirs": _format_yaml_list(["src/"]),
        "graph_path": f"{config_dir}/scan",
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
def require(path: str, output: str, scope: str | None, ai_cmd: str | None, force: bool, feedback: str | None):
    """Infer requirements from extracted codebase facts (brownfield).

    Unlike 'restore' which reconstructs design docs from extracted facts,
    'require' reverse-engineers requirements documents from the same
    extracted code analysis. Run 'codd extract' first.
    """
    from codd.require import run_require

    project_root = Path(path).resolve()
    _require_codd_dir(project_root)

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
def propagate(diff: str, path: str, update: bool, verify: bool, do_commit: bool,
              reason: str | None, reason_file: str | None,
              ai_cmd: str | None, feedback: str | None):
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
            result = run_verify(project_root, diff, ai_command=ai_cmd, feedback=feedback)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1)

        if not result.changed_files:
            click.echo("No changed files detected.")
            return

        click.echo(f"Changed files: {len(result.changed_files)}")
        if not result.file_module_map:
            click.echo("No source files changed (only non-source files in diff).")
            return

        click.echo(f"\nSource changes → modules:")
        for f, m in sorted(result.file_module_map.items()):
            click.echo(f"  {f} → {m}")

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

        if not result.auto_applied and not result.needs_hitl:
            click.echo("\nNo design docs found covering changed modules.")
        return

    # Default / --update mode (existing behavior)
    from codd.propagator import run_propagate

    try:
        result = run_propagate(project_root, diff, update=update, ai_command=ai_cmd, feedback=feedback)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1)

    if not result.changed_files:
        click.echo("No changed files detected.")
        return

    click.echo(f"Changed files: {len(result.changed_files)}")
    if not result.file_module_map:
        click.echo("No source files changed (only non-source files in diff).")
        return

    click.echo(f"\nSource changes → modules:")
    for f, m in sorted(result.file_module_map.items()):
        click.echo(f"  {f} → {m}")

    if not result.affected_docs:
        click.echo("\nNo design docs found covering changed modules.")
        click.echo("(Design docs need a 'modules' field in frontmatter to be tracked.)")
        return

    click.echo(f"\nAffected design docs: {len(result.affected_docs)}")
    for doc in result.affected_docs:
        status = "UPDATED" if doc.node_id in result.updated else "needs review"
        click.echo(f"  [{status}] {doc.path} ({doc.node_id})")
        click.echo(f"    modules: {', '.join(doc.matched_modules)}")

    if not update and result.affected_docs:
        click.echo(f"\nRun with --update to update these docs via AI.")


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
@click.option("--sprint", default=None, type=click.IntRange(min=1), help="Sprint number to verify")
def verify(path: str, sprint: int | None) -> None:
    """Run build + test verification and trace failures to design documents."""
    _run_pro_command("verify", path=path, sprint=sprint)


@main.command()
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
def extract(path: str, language: str | None, source_dirs: str | None, output: str | None, ai: bool, ai_cmd: str | None, prompt_file: str | None):
    """Extract design documents from existing codebase (brownfield bootstrap).

    Default mode: static analysis (no AI, pure structural facts).
    With --ai: AI-powered 6-layer MECE extraction using claude --print.

    Output goes to the discovered CoDD config dir as draft documents
    (`codd/extracted/` or `.codd/extracted/`). Review and promote
    confirmed docs when ready.
    """
    project_root = Path(path).resolve()
    bootstrap_codd_dir = _resolve_bootstrap_codd_dir(project_root)
    dirs = [d.strip() for d in source_dirs.split(",") if d.strip()] if source_dirs else None
    output_path = Path(output) if output else bootstrap_codd_dir / "extracted"

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
@click.option("--path", default=".", help="Project root directory")
def validate(path: str):
    """Validate CoDD frontmatter and dependency references."""
    from codd.validator import run_validate

    project_root = Path(path).resolve()
    codd_dir = _require_codd_dir(project_root)

    raise SystemExit(run_validate(project_root, codd_dir))


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


@main.command()
@click.option("--path", default=".", help="Project root directory")
@click.option("--json", "as_json", is_flag=True, help="Output plan as JSON")
@click.option("--init", "initialize", is_flag=True, help="Generate wave_config from requirement docs")
@click.option("--force", is_flag=True, help="Overwrite existing wave_config during --init")
@click.option("--waves", is_flag=True, help="Output only the total wave count (for shell scripting)")
@click.option("--sprints", is_flag=True, help="Output only the total sprint count (for shell scripting)")
@click.option(
    "--ai-cmd",
    default=None,
    help="Override AI CLI command for --init (defaults to codd.yaml ai_command or 'claude --print')",
)
def plan(path: str, as_json: bool, initialize: bool, force: bool, waves: bool, sprints: bool, ai_cmd: str | None):
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

    if force:
        raise click.BadOptionUsage("force", "--force requires --init")
    if ai_cmd is not None and not waves and not sprints:
        raise click.BadOptionUsage("ai_cmd", "--ai-cmd requires --init")

    if waves:
        from codd.generator import _load_project_config
        config = _load_project_config(project_root)
        wave_config = config.get("wave_config", {})
        click.echo(len(wave_config))
        return

    if sprints:
        from codd.implementer import count_sprints
        click.echo(count_sprints(project_root))
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

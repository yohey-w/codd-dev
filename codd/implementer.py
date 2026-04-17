"""CoDD implementer — design-to-code generation from implementation plans."""

from __future__ import annotations

from collections import deque
import concurrent.futures
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any

import codd.generator as generator_module
from codd.generator import DependencyDocument, _load_project_config, _normalize_conventions
from codd.scanner import _extract_frontmatter, build_document_node_path_map


DEFAULT_IMPLEMENT_NODE_ID = "plan:implementation-plan"
FILE_BLOCK_RE = re.compile(r"^=== FILE: (?P<path>.+?) ===\s*$", re.MULTILINE)

SPRINT_HEADING_RE = re.compile(
    r"^####\s+Sprint\s+(?P<number>\d+)(?:（(?P<window>[^）]+)）)?(?:\s*:\s*(?P<title>.+))?\s*$",
    re.MULTILINE,
)
SECTION_HEADING_RE = re.compile(r"^##\s+\d+\.\s+(?P<title>.+?)\s*$", re.MULTILINE)
MILESTONE_HEADING_RE = re.compile(
    r"^###\s+(?:Milestone\s+)?(?P<number>\d+)\s*(?:[—–-]\s*(?P<title>.+?))?\s*$",
    re.MULTILINE,
)
PHASE_MILESTONE_RE = re.compile(
    r"^####\s+M(?P<phase>\d+)\.(?P<milestone>\d+)\s+(?P<title>.+?)(?:\s*（[^）]+）)?\s*$",
    re.MULTILINE,
)
DURATION_RE = re.compile(r"\*\*Duration:\*\*\s*(?P<period>.+?)$", re.MULTILINE)

EXPORT_TYPE_RE = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:type|interface|enum)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_CLASS_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_FUNCTION_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_VALUE_RE = re.compile(
    r"^\s*export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
EXPORT_NAMED_BLOCK_RE = re.compile(
    r"^\s*export\s+(?P<type_prefix>type\s+)?{\s*(?P<body>[^}]+)\s*}(?:\s+from\s+['\"].+['\"])?\s*;?",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ImplementationPlan:
    """Implementation plan document and its metadata."""

    node_id: str
    path: Path
    content: str
    depends_on: list[dict[str, Any]]
    conventions: list[dict[str, Any]]


@dataclass(frozen=True)
class ImplementationTask:
    """Concrete implementation task extracted from the plan."""

    task_id: str
    title: str
    summary: str
    module_hint: str
    deliverable: str
    output_dir: str
    dependency_node_ids: list[str]
    task_context: str
    blocked_by_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImplementationResult:
    """Result of generating code for one implementation task."""

    task_id: str
    task_title: str
    output_dir: Path
    generated_files: list[Path]
    error: str | None = None


def get_valid_task_slugs(project_root: Path) -> set[str]:
    """Return set of valid task directory names under src/generated/.

    Used by assembler to detect orphan fragments.
    Returns empty set if implementation plan is not found.
    """
    config = _load_project_config(project_root)
    try:
        plan = _load_implementation_plan(project_root, config)
    except (FileNotFoundError, ValueError):
        return set()
    tasks = _extract_all_tasks(plan)
    return {PurePosixPath(t.output_dir).name for t in tasks}


def implement_tasks(
    project_root: Path,
    *,
    task: str | None = None,
    ai_command: str | None = None,
    clean: bool = False,
) -> list[ImplementationResult]:
    """Generate code for tasks from implementation plan."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)

    if clean:
        _clean_generated_output(project_root, config)

    plan = _load_implementation_plan(project_root, config)
    selected_tasks = _extract_all_tasks(plan)

    if task:
        selected_tasks = _filter_tasks(selected_tasks, task)

    if not selected_tasks:
        if task:
            raise ValueError(f"no implementation task matched {task!r}")
        raise ValueError("implementation plan does not define any tasks")

    resolved_ai_command = generator_module._resolve_ai_command(config, ai_command, command_name="implement")
    global_conventions = _normalize_conventions(config.get("conventions", []))
    coding_principles = _load_coding_principles(project_root, config)
    node_paths = build_document_node_path_map(project_root, config)
    detailed_design_node_ids = _select_detailed_design_dependency_node_ids(plan.depends_on, node_paths)

    phase_groups = _group_tasks_by_phase(selected_tasks)
    phase_groups = _resolve_task_dependencies(phase_groups)
    use_worktree = generator_module._is_file_writing_agent(
        __import__("shlex").split(resolved_ai_command),
    )

    results: list[ImplementationResult] = []
    prior_task_outputs: list[dict[str, Any]] = []

    for phase_tasks in phase_groups:
        executable: list[ImplementationTask] = []
        for t in phase_tasks:
            blocker_error = _check_blockers(t, results)
            if blocker_error:
                results.append(ImplementationResult(
                    task_id=t.task_id,
                    task_title=t.title,
                    output_dir=Path(t.output_dir),
                    generated_files=[],
                    error=blocker_error,
                ))
            else:
                executable.append(t)

        if not executable:
            continue

        if len(executable) == 1:
            result, summary = _execute_task(
                config, plan, executable[0], resolved_ai_command,
                global_conventions, coding_principles, node_paths,
                detailed_design_node_ids, prior_task_outputs, project_root,
            )
            results.append(result)
            prior_task_outputs.append(summary)
        else:
            phase_results = _execute_phase_parallel(
                config, plan, executable, resolved_ai_command,
                global_conventions, coding_principles, node_paths,
                detailed_design_node_ids, prior_task_outputs, project_root,
                use_worktree=use_worktree,
            )
            for result, summary in phase_results:
                results.append(result)
                prior_task_outputs.append(summary)

    failed = [r for r in results if r.error]
    if failed:
        import sys
        print(
            f"\n[codd] WARNING: {len(failed)} of {len(results)} task(s) failed to generate files:",
            file=sys.stderr,
        )
        for r in failed:
            print(f"  - {r.task_id} ({r.task_title}): {r.error}", file=sys.stderr)

    expected = len(selected_tasks)
    actual = len(results)
    if actual < expected:
        import sys
        print(
            f"\n[codd] WARNING: expected {expected} task results but got {actual} "
            f"({expected - actual} task(s) lost)",
            file=sys.stderr,
        )

    return results


def _group_tasks_by_phase(
    tasks: list[ImplementationTask],
) -> list[list[ImplementationTask]]:
    """Group tasks by phase number. Same phase = independent, can run in parallel."""
    phase_map: dict[str, list[ImplementationTask]] = {}
    for t in tasks:
        match = re.match(r"m(\d+)\.", t.task_id)
        phase = match.group(1) if match else "0"
        phase_map.setdefault(phase, []).append(t)
    return [phase_map[k] for k in sorted(phase_map.keys())]


def _resolve_task_dependencies(
    phase_groups: list[list[ImplementationTask]],
) -> list[list[ImplementationTask]]:
    """Assign blocked_by_task_ids: each task is blocked by all tasks in prior phases."""
    resolved: list[list[ImplementationTask]] = []
    prior_task_ids: tuple[str, ...] = ()
    for group in phase_groups:
        resolved_group = []
        for t in group:
            if t.blocked_by_task_ids:
                resolved_group.append(t)
            else:
                resolved_group.append(
                    ImplementationTask(
                        task_id=t.task_id,
                        title=t.title,
                        summary=t.summary,
                        module_hint=t.module_hint,
                        deliverable=t.deliverable,
                        output_dir=t.output_dir,
                        dependency_node_ids=t.dependency_node_ids,
                        task_context=t.task_context,
                        blocked_by_task_ids=prior_task_ids,
                    )
                )
        resolved.append(resolved_group)
        prior_task_ids = prior_task_ids + tuple(t.task_id for t in group)
    return resolved


def _check_blockers(
    task: ImplementationTask,
    results: list[ImplementationResult],
) -> str | None:
    """Return error message if any blocker task failed, else None."""
    if not task.blocked_by_task_ids:
        return None
    result_map = {r.task_id: r for r in results}
    failed_blockers = []
    for blocker_id in task.blocked_by_task_ids:
        result = result_map.get(blocker_id)
        if result is not None and result.error:
            failed_blockers.append(blocker_id)
    if failed_blockers:
        return f"skipped: blocked by failed task(s) {', '.join(failed_blockers)}"
    return None


def _execute_task(
    config: dict[str, Any],
    plan: ImplementationPlan,
    task_item: ImplementationTask,
    resolved_ai_command: str,
    global_conventions: list[dict[str, Any]],
    coding_principles: str,
    node_paths: dict[str, Path],
    detailed_design_node_ids: list[str],
    prior_task_outputs: list[dict[str, Any]],
    project_root: Path,
) -> tuple[ImplementationResult, dict[str, Any]]:
    """Execute a single implementation task. Returns (result, summary)."""
    dependency_node_ids = _ordered_unique(
        task_item.dependency_node_ids + detailed_design_node_ids,
    )
    dependency_documents, document_conventions = _collect_dependency_documents(
        project_root, dependency_node_ids, node_paths,
    )
    combined_conventions = _merge_conventions(
        global_conventions, plan.conventions, document_conventions,
    )
    prompt = _build_implementation_prompt(
        config=config,
        plan=plan,
        task=task_item,
        dependency_documents=dependency_documents,
        conventions=combined_conventions,
        coding_principles=coding_principles,
        prior_task_outputs=prior_task_outputs,
    )
    raw_output = generator_module._invoke_ai_command(
        resolved_ai_command, prompt, project_root=project_root,
    )
    generated_files = _write_generated_files(
        project_root=project_root,
        plan=plan,
        task=task_item,
        dependency_documents=dependency_documents,
        output_dir=task_item.output_dir,
        raw_output=raw_output,
    )
    summary = _summarize_generated_task_output(project_root, task_item, generated_files)
    result = ImplementationResult(
        task_id=task_item.task_id,
        task_title=task_item.title,
        output_dir=project_root / task_item.output_dir,
        generated_files=generated_files,
    )
    return result, summary


def _create_worktree(project_root: Path) -> tuple[Path, str]:
    """Create a temporary git worktree for isolated parallel execution."""
    worktree_dir = Path(tempfile.mkdtemp(prefix="codd-wt-"))
    branch = f"codd-wt-{os.getpid()}-{id(worktree_dir)}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir), "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        check=True,
    )
    return worktree_dir, branch


def _remove_worktree(project_root: Path, worktree_dir: Path, branch: str) -> None:
    """Remove a temporary git worktree and its branch."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        cwd=str(project_root),
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=str(project_root),
        capture_output=True,
    )
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)


def _execute_task_in_worktree(
    config: dict[str, Any],
    plan: ImplementationPlan,
    task_item: ImplementationTask,
    resolved_ai_command: str,
    global_conventions: list[dict[str, Any]],
    coding_principles: str,
    node_paths: dict[str, Path],
    detailed_design_node_ids: list[str],
    prior_task_outputs: list[dict[str, Any]],
    project_root: Path,
) -> tuple[ImplementationResult, dict[str, Any]]:
    """Execute task in a git worktree, copy output back to main project."""
    worktree_dir, branch = _create_worktree(project_root)
    try:
        result, summary = _execute_task(
            config, plan, task_item, resolved_ai_command,
            global_conventions, coding_principles, node_paths,
            detailed_design_node_ids, prior_task_outputs, worktree_dir,
        )
        output_dir = worktree_dir / task_item.output_dir
        target_dir = project_root / task_item.output_dir
        if output_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            for src_file in output_dir.iterdir():
                if src_file.is_file():
                    shutil.copy2(src_file, target_dir / src_file.name)
        return ImplementationResult(
            task_id=result.task_id,
            task_title=result.task_title,
            output_dir=target_dir,
            generated_files=[
                project_root / f.relative_to(worktree_dir)
                for f in result.generated_files
            ],
        ), summary
    finally:
        _remove_worktree(project_root, worktree_dir, branch)


def _execute_phase_parallel(
    config: dict[str, Any],
    plan: ImplementationPlan,
    phase_tasks: list[ImplementationTask],
    resolved_ai_command: str,
    global_conventions: list[dict[str, Any]],
    coding_principles: str,
    node_paths: dict[str, Path],
    detailed_design_node_ids: list[str],
    prior_task_outputs: list[dict[str, Any]],
    project_root: Path,
    *,
    use_worktree: bool = False,
) -> list[tuple[ImplementationResult, dict[str, Any]]]:
    """Execute all tasks in a phase concurrently."""
    import sys

    executor_fn = _execute_task_in_worktree if use_worktree else _execute_task
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(phase_tasks), 4),
    ) as executor:
        futures = {
            executor.submit(
                executor_fn,
                config, plan, t, resolved_ai_command,
                global_conventions, coding_principles, node_paths,
                detailed_design_node_ids, prior_task_outputs, project_root,
            ): t
            for t in phase_tasks
        }
        phase_results: list[tuple[int, tuple[ImplementationResult, dict[str, Any]]]] = []
        for future in concurrent.futures.as_completed(futures):
            t = futures[future]
            idx = phase_tasks.index(t)
            try:
                phase_results.append((idx, future.result()))
            except Exception as exc:
                print(
                    f"[codd] task {t.task_id} failed: {exc}",
                    file=sys.stderr,
                )
                error_result = ImplementationResult(
                    task_id=t.task_id,
                    task_title=t.title,
                    output_dir=project_root / t.output_dir,
                    generated_files=[],
                    error=str(exc),
                )
                error_summary = {
                    "task_id": t.task_id,
                    "task_title": t.title,
                    "directory": t.output_dir,
                    "files": [],
                    "exported_types": [],
                    "exported_functions": [],
                    "exported_classes": [],
                    "exported_values": [],
                    "error": str(exc),
                }
                phase_results.append((idx, (error_result, error_summary)))
    phase_results.sort(key=lambda x: x[0])
    return [r for _, r in phase_results]


def _load_implementation_plan(project_root: Path, config: dict[str, Any]) -> ImplementationPlan:
    node_paths = build_document_node_path_map(project_root, config)
    rel_path = node_paths.get(DEFAULT_IMPLEMENT_NODE_ID, Path("docs/plan/implementation_plan.md"))
    plan_path = project_root / rel_path
    if not plan_path.exists():
        raise FileNotFoundError(f"implementation plan not found: {rel_path.as_posix()}")

    codd = _extract_frontmatter(plan_path) or {}
    content = plan_path.read_text(encoding="utf-8")
    return ImplementationPlan(
        node_id=str(codd.get("node_id") or DEFAULT_IMPLEMENT_NODE_ID),
        path=rel_path,
        content=content,
        depends_on=generator_module._normalize_dependencies(codd.get("depends_on", [])),
        conventions=_normalize_conventions(codd.get("conventions", [])),
    )


def _load_coding_principles(project_root: Path, config: dict[str, Any]) -> str | None:
    raw_path = config.get("coding_principles")
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("coding_principles must be a non-empty project-relative path when configured")

    principles_path = project_root / raw_path
    if not principles_path.exists():
        raise FileNotFoundError(f"coding_principles file not found: {raw_path}")

    return principles_path.read_text(encoding="utf-8")


def _extract_all_tasks(plan: ImplementationPlan) -> list[ImplementationTask]:
    """Extract all implementation tasks from the plan.

    Supports phase milestones (M1.1), Sprint headings, and milestone tables.
    Phase milestones are tried first (most specific format).
    """
    tasks = _extract_tasks_from_phase_milestones(plan)
    if not tasks:
        tasks = _extract_tasks_from_sprint_headings(plan)
    if not tasks:
        tasks = _extract_tasks_from_milestones(plan)
    return _deduplicate_slugs(tasks)


def _extract_tasks_from_sprint_headings(plan: ImplementationPlan) -> list[ImplementationTask]:
    """Extract tasks from all #### Sprint N sections."""
    matches = list(SPRINT_HEADING_RE.finditer(plan.content))
    if not matches:
        return []

    tasks: list[ImplementationTask] = []
    for index, match in enumerate(matches):
        section_start = match.end()
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(plan.content)
        section_text = plan.content[section_start:section_end]
        table_rows = _parse_markdown_table(section_text)

        sprint_num = int(match.group("number"))
        for row in table_rows:
            if len(row) < 4:
                continue
            task_id = row[0] or f"{sprint_num}-{len(tasks) + 1}"
            title = row[1] or f"Task {len(tasks) + 1}"
            module_hint = row[2]
            deliverable = row[3]
            slug = _derive_task_slug(title, module_hint, task_id)
            tasks.append(
                ImplementationTask(
                    task_id=task_id,
                    title=title,
                    summary=title,
                    module_hint=module_hint,
                    deliverable=deliverable,
                    output_dir=f"src/generated/{slug}",
                    dependency_node_ids=_infer_dependency_node_ids(plan, title, module_hint, deliverable),
                    task_context=_clean_text_block(section_text),
                )
            )
    return tasks


def _extract_tasks_from_phase_milestones(plan: ImplementationPlan) -> list[ImplementationTask]:
    """Extract tasks from #### M<phase>.<milestone> headings (e.g., #### M1.1 DB Schema)."""
    milestones_match = re.search(
        r"^##\s+\d+\.\s+Milestones",
        plan.content,
        re.MULTILINE,
    )
    if not milestones_match:
        return []

    section_start = milestones_match.end()
    next_section = SECTION_HEADING_RE.search(plan.content, section_start)
    section_end = next_section.start() if next_section else len(plan.content)
    milestones_text = plan.content[section_start:section_end]

    matches = list(PHASE_MILESTONE_RE.finditer(milestones_text))
    if not matches:
        return []

    tasks: list[ImplementationTask] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(milestones_text)
        body = milestones_text[start:end]

        phase = match.group("phase")
        milestone = match.group("milestone")
        title = match.group("title").strip()
        task_id = f"m{phase}.{milestone}"

        table_rows = _parse_markdown_table(body)
        deliverables = []
        for row in table_rows:
            if len(row) >= 2 and row[1].strip() and row[0] != "タスク":
                deliverables.append(row[1].strip())

        slug = _slug_from_text(f"m{phase}_{milestone}_{title}")
        tasks.append(
            ImplementationTask(
                task_id=task_id,
                title=f"M{phase}.{milestone} {title}",
                summary=f"M{phase}.{milestone} {title}",
                module_hint="",
                deliverable="; ".join(deliverables[:6]),
                output_dir=f"src/generated/{slug}",
                dependency_node_ids=_infer_dependency_node_ids(
                    plan, title, "", "; ".join(deliverables[:3]),
                ),
                task_context=_clean_text_block(body),
            )
        )
    return tasks


def _extract_tasks_from_milestones(plan: ImplementationPlan) -> list[ImplementationTask]:
    """Extract tasks from milestone table when no Sprint headings exist."""
    milestones = _parse_milestone_rows(plan.content)
    if not milestones:
        return []

    tasks: list[ImplementationTask] = []
    for ms_index, milestone in enumerate(milestones, start=1):
        task_context = (
            f"Milestone: {milestone['title']}\n"
            f"Period: {milestone['period']}\n"
            f"Deliverables: {milestone['deliverables']}"
        )
        summary_chunks = [
            chunk for chunk in _split_deliverable_chunks(milestone["deliverables"]) if chunk
        ][:4]
        if not summary_chunks:
            summary_chunks = [milestone["deliverables"] or milestone["title"]]

        for chunk_index, chunk in enumerate(summary_chunks, start=1):
            task_id = f"{ms_index}-{chunk_index}"
            slug = _derive_task_slug(chunk, "", task_id)
            tasks.append(
                ImplementationTask(
                    task_id=task_id,
                    title=chunk,
                    summary=chunk,
                    module_hint=f"src/generated/{slug}",
                    deliverable=milestone["deliverables"],
                    output_dir=f"src/generated/{slug}",
                    dependency_node_ids=[entry["id"] for entry in plan.depends_on] or ["design:system-design"],
                    task_context=task_context,
                )
            )
    return tasks


def _deduplicate_slugs(tasks: list[ImplementationTask]) -> list[ImplementationTask]:
    """Ensure output_dir slugs are unique by appending task_id on collision."""
    slug_counts: dict[str, int] = {}
    for t in tasks:
        slug = PurePosixPath(t.output_dir).name
        slug_counts[slug] = slug_counts.get(slug, 0) + 1

    duplicated = {slug for slug, count in slug_counts.items() if count > 1}
    if not duplicated:
        return tasks

    result: list[ImplementationTask] = []
    for t in tasks:
        slug = PurePosixPath(t.output_dir).name
        if slug in duplicated:
            id_suffix = _slug_from_text(t.task_id)
            new_output_dir = f"src/generated/{slug}_{id_suffix}"
            result.append(
                ImplementationTask(
                    task_id=t.task_id,
                    title=t.title,
                    summary=t.summary,
                    module_hint=t.module_hint,
                    deliverable=t.deliverable,
                    output_dir=new_output_dir,
                    dependency_node_ids=t.dependency_node_ids,
                    task_context=t.task_context,
                )
            )
        else:
            result.append(t)
    return result


def _filter_tasks(tasks: list[ImplementationTask], task_filter: str) -> list[ImplementationTask]:
    """Filter tasks by task_id, slug, or title match."""
    needle = task_filter.strip().casefold()
    return [
        task
        for task in tasks
        if needle in {
            task.task_id.casefold(),
            _slug_from_text(task.title).casefold(),
            _slug_from_text(task.output_dir).casefold(),
        }
        or needle in task.title.casefold()
    ]


def _clean_generated_output(project_root: Path, config: dict[str, Any]) -> None:
    """Remove all generated files before re-generation."""
    import shutil

    source_dirs = config.get("scan", {}).get("source_dirs", ["src/"])
    for src_dir in source_dirs:
        generated_dir = project_root / src_dir / "generated"
        if generated_dir.is_dir():
            shutil.rmtree(generated_dir)
            return

    generated_dir = project_root / "src" / "generated"
    if generated_dir.is_dir():
        shutil.rmtree(generated_dir)


def _parse_markdown_table(section_text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 4:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r"-{3,}", cell.replace(" ", "")) for cell in cells):
            continue
        if cells and cells[0] == "#":
            continue
        rows.append(cells)
    return rows


def _parse_milestone_rows(content: str) -> list[dict[str, str]]:
    match = re.search(
        r"^##\s+\d+\.\s+Milestones(?:（マイルストーン）)?\s*$",
        content,
        re.MULTILINE,
    )
    if not match:
        return []

    remaining = content[match.end():]
    next_heading = SECTION_HEADING_RE.search(remaining)
    section_text = remaining[: next_heading.start()] if next_heading else remaining

    rows = _parse_markdown_table(section_text)
    milestones: list[dict[str, str]] = []
    for row in rows:
        if len(row) < 3:
            continue
        milestones.append(
            {
                "period": row[0],
                "title": row[1],
                "deliverables": row[2],
            }
        )
    if milestones:
        return milestones

    heading_matches = list(MILESTONE_HEADING_RE.finditer(section_text))
    for idx, h_match in enumerate(heading_matches):
        start = h_match.end()
        end = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else len(section_text)
        body = section_text[start:end]
        dur_match = DURATION_RE.search(body)
        period = dur_match.group("period").strip() if dur_match else ""
        sub_headings = re.findall(r"^####\s+.+$", body, re.MULTILINE)
        deliverables = "; ".join(h.lstrip("#").strip() for h in sub_headings[:6])
        milestones.append(
            {
                "period": period,
                "title": (h_match.group("title") or "").strip(),
                "deliverables": deliverables,
            }
        )
    return milestones


def _infer_dependency_node_ids(
    plan: ImplementationPlan,
    title: str,
    module_hint: str,
    deliverable: str,
) -> list[str]:
    plan_dependencies = [entry["id"] for entry in plan.depends_on]
    keyword_text = " ".join([title, module_hint, deliverable]).casefold()
    selected: list[str] = []

    dependency_keywords = {
        "design:system-design": ["system", "architecture", "基盤", "bootstrap", "project", "middleware"],
        "design:database-design": ["database", "db", "prisma", "schema", "rls", "sql"],
        "design:api-design": ["api", "endpoint", "request", "middleware", "route"],
        "design:auth-authorization-design": ["auth", "jwt", "rbac", "oauth", "session", "認証", "認可"],
        "design:ux-design": ["ui", "layout", "screen", "component", "ux", "frontend"],
        "design:integration-design": ["integration", "stripe", "line", "sendgrid", "4ms", "bunny"],
    }

    for node_id in plan_dependencies:
        for keyword in dependency_keywords.get(node_id, []):
            if keyword in keyword_text:
                selected.append(node_id)
                break

    if "design:system-design" in plan_dependencies and "design:system-design" not in selected:
        selected.insert(0, "design:system-design")

    return _ordered_unique(selected or plan_dependencies)


def _select_detailed_design_dependency_node_ids(
    dependencies: list[dict[str, Any]],
    node_paths: dict[str, Path],
) -> list[str]:
    selected: list[str] = []
    for dependency in dependencies:
        node_id = dependency["id"]
        rel_path = node_paths.get(node_id)
        if rel_path is None:
            continue
        if _is_detailed_design_path(rel_path):
            selected.append(node_id)
    return _ordered_unique(selected)


def _collect_dependency_documents(
    project_root: Path,
    initial_node_ids: list[str],
    node_paths: dict[str, Path],
) -> tuple[list[DependencyDocument], list[dict[str, Any]]]:
    documents: list[DependencyDocument] = []
    conventions: list[dict[str, Any]] = []
    queue: deque[str] = deque(node_id for node_id in initial_node_ids if node_id)
    required_node_ids = set(initial_node_ids)
    seen: set[str] = set()
    missing: list[str] = []

    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)

        rel_path = node_paths.get(node_id)
        if rel_path is None:
            if node_id in required_node_ids:
                missing.append(node_id)
            continue

        doc_path = project_root / rel_path
        if not doc_path.exists():
            if node_id in required_node_ids:
                raise ValueError(
                    f"dependency document {node_id!r} maps to {rel_path.as_posix()}, but the file does not exist"
                )
            continue

        content = doc_path.read_text(encoding="utf-8")
        documents.append(DependencyDocument(node_id=node_id, path=rel_path, content=content))

        codd = _extract_frontmatter(doc_path) or {}
        conventions.extend(_normalize_conventions(codd.get("conventions", [])))
        for dependency in generator_module._normalize_dependencies(codd.get("depends_on", [])):
            if dependency["id"] not in seen:
                queue.append(dependency["id"])

    if missing:
        raise ValueError(f"unable to resolve dependency document paths for: {', '.join(sorted(set(missing)))}")

    documents.sort(key=lambda document: document.path.as_posix())
    return documents, conventions


def _merge_conventions(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for convention in group:
            normalized = {
                "targets": [target for target in convention.get("targets", []) if isinstance(target, str)],
                "reason": str(convention.get("reason") or "").strip(),
            }
            key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged


def _build_implementation_prompt(
    *,
    config: dict[str, Any],
    plan: ImplementationPlan,
    task: ImplementationTask,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    coding_principles: str | None,
    prior_task_outputs: list[dict[str, Any]] | None = None,
) -> str:
    project = config.get("project") or {}
    frameworks = project.get("frameworks") or []
    language = project.get("language") or "typescript"
    framework_text = ", ".join(str(item) for item in frameworks) if frameworks else "(unspecified)"

    lines = [
        "You are generating implementation code from CoDD design documents.",
        f"Project name: {project.get('name') or '(unknown)'}",
        f"Primary language: {language}",
        f"Framework stack: {framework_text}",
        f"Implementation plan: {plan.path.as_posix()} ({plan.node_id})",
        f"Task ID: {task.task_id}",
        f"Task title: {task.title}",
        f"Task summary: {task.summary}",
        f"Module hint: {task.module_hint}",
        f"Deliverable: {task.deliverable}",
        f"Output directory: {task.output_dir}",
        "",
        "Mandatory instructions:",
        "- Generate concrete production-oriented TypeScript / TSX source files.",
        "- Use Next.js App Router, TypeScript, and Prisma-compatible patterns when relevant.",
        "- Reflect tenant isolation, RLS context propagation, authentication, authorization, and auditability explicitly where the design requires them.",
        "- The tool will prepend traceability comments to each generated file; do not emit separate metadata files.",
        "- Do not emit prose, explanations, Markdown headings, YAML, TODOs, placeholders, or file descriptions outside the required FILE blocks.",
        "- Every generated file path must stay under the output directory shown above.",
        "- If a React component is needed, emit .tsx files. Otherwise prefer .ts files.",
        "- Favor small coherent modules rather than one monolithic file.",
        "- Cross-file imports may use relative imports or '@/generated/...' style aliases, but keep the task internally coherent.",
        "",
        "Required output format (repeat this block for each file and output nothing else):",
        f"=== FILE: {task.output_dir}/<filename>.ts ===",
        "```ts",
        "// code",
        "```",
        "",
        "ABSOLUTE PROHIBITION: Outputting prose, planning notes, TODO markers, or files outside the output directory is a CRITICAL ERROR.",
        "",
        "Task context:",
        task.task_context,
    ]

    if coding_principles:
        lines.extend(
            [
                "",
                "Project coding principles (treat these as source-of-truth implementation rules):",
                coding_principles.rstrip(),
            ]
        )

    if conventions:
        lines.extend(
            [
                "",
                "Non-negotiable conventions:",
                "- These are release-blocking constraints. The code must embody them explicitly.",
                "- If a convention concerns security, RLS, tenant boundaries, or auth, implement a concrete control rather than only comments.",
            ]
        )
        for index, convention in enumerate(conventions, start=1):
            targets = ", ".join(target for target in convention.get("targets", []) if isinstance(target, str))
            reason = convention.get("reason") or "(no reason provided)"
            lines.append(f"{index}. Targets: {targets or '(no explicit targets)'}")
            lines.append(f"   Reason: {reason}")

    if prior_task_outputs:
        lines.extend(
            [
                "",
                "Prior implementations (earlier tasks):",
                "- The following summaries describe code that was already generated for earlier tasks.",
                "- ABSOLUTE PROHIBITION: Re-implementing the same type definitions, utility functions, classes, guards, middleware, or helpers is a CRITICAL ERROR and a release-blocking violation.",
                "- Reuse these implementations via imports. If a needed symbol already exists below, import it instead of redefining it.",
            ]
        )
        for summary in prior_task_outputs:
            lines.extend(_format_prior_task_summary(summary))

    lines.extend(
        [
            "",
            "Dependency documents:",
        ]
    )
    for document in dependency_documents:
        lines.extend(
            [
                f"--- BEGIN DEPENDENCY {document.path.as_posix()} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END DEPENDENCY {document.path.as_posix()} ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _write_generated_files(
    *,
    project_root: Path,
    plan: ImplementationPlan,
    task: ImplementationTask,
    dependency_documents: list[DependencyDocument],
    output_dir: str,
    raw_output: str,
) -> list[Path]:
    file_payloads = _parse_file_payloads(raw_output, output_dir)
    traceability_comment = _build_traceability_comment(plan, task, dependency_documents)
    generated_paths: list[Path] = []
    for relative_path, content in file_payloads:
        destination = project_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_prepend_traceability_comment(relative_path, traceability_comment, content), encoding="utf-8")
        generated_paths.append(destination)
    return generated_paths


def _parse_file_payloads(raw_output: str, output_dir: str) -> list[tuple[str, str]]:
    cleaned_output = raw_output.strip()
    matches = list(FILE_BLOCK_RE.finditer(cleaned_output))
    if not matches:
        fallback_content = _strip_code_fence(cleaned_output).strip()
        if not fallback_content:
            raise ValueError("AI command returned empty implementation output")
        extension = ".tsx" if _looks_like_tsx(fallback_content) else ".ts"
        return [(f"{output_dir}/index{extension}", fallback_content.rstrip() + "\n")]

    payloads: list[tuple[str, str]] = []
    skipped: list[str] = []
    output_prefix = PurePosixPath(output_dir)
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned_output)
        block = cleaned_output[start:end].strip()
        path_text = match.group("path").strip()
        path = PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts:
            skipped.append(f"{path_text!r}: path traversal")
            continue
        if not path.parts or path.parts[0] != "src":
            skipped.append(f"{path_text!r}: outside src/")
            continue
        if tuple(path.parts[: len(output_prefix.parts)]) != output_prefix.parts:
            skipped.append(f"{path_text!r}: outside output directory {output_dir!r}")
            continue

        content = _strip_code_fence(block).strip()
        if not content:
            skipped.append(f"{path_text!r}: empty content")
            continue
        payloads.append((path.as_posix(), content.rstrip() + "\n"))

    if skipped:
        import sys
        for reason in skipped:
            print(f"Warning: skipped generated file — {reason}", file=sys.stderr)

    if not payloads:
        raise ValueError(
            f"AI produced {len(matches)} file block(s) but all were invalid: {'; '.join(skipped)}"
        )

    return payloads


def _summarize_generated_task_output(
    project_root: Path,
    task: ImplementationTask,
    generated_files: list[Path],
) -> dict[str, Any]:
    exported_types: list[str] = []
    exported_functions: list[str] = []
    exported_classes: list[str] = []
    exported_values: list[str] = []
    relative_files: list[str] = []

    for file_path in generated_files:
        relative_files.append(file_path.relative_to(project_root).as_posix())
        summary = _extract_export_summary(file_path.read_text(encoding="utf-8"))
        exported_types.extend(summary["exported_types"])
        exported_functions.extend(summary["exported_functions"])
        exported_classes.extend(summary["exported_classes"])
        exported_values.extend(summary["exported_values"])

    return {
        "task_id": task.task_id,
        "task_title": task.title,
        "directory": task.output_dir,
        "files": relative_files,
        "exported_types": _ordered_unique(exported_types),
        "exported_functions": _ordered_unique(exported_functions),
        "exported_classes": _ordered_unique(exported_classes),
        "exported_values": _ordered_unique(exported_values),
    }


def _extract_export_summary(content: str) -> dict[str, list[str]]:
    summary = {
        "exported_types": [match.group("name") for match in EXPORT_TYPE_RE.finditer(content)],
        "exported_functions": [match.group("name") for match in EXPORT_FUNCTION_RE.finditer(content)],
        "exported_classes": [match.group("name") for match in EXPORT_CLASS_RE.finditer(content)],
        "exported_values": [match.group("name") for match in EXPORT_VALUE_RE.finditer(content)],
    }

    for match in EXPORT_NAMED_BLOCK_RE.finditer(content):
        body = match.group("body")
        block_is_type = bool(match.group("type_prefix"))
        for raw_item in body.split(","):
            item = raw_item.strip()
            if not item:
                continue
            item_is_type = block_is_type
            if item.startswith("type "):
                item_is_type = True
                item = item[5:].strip()
            exported_name = item.split(" as ")[-1].strip()
            if not exported_name:
                continue
            bucket = "exported_types" if item_is_type else "exported_values"
            summary[bucket].append(exported_name)

    return {key: _ordered_unique(values) for key, values in summary.items()}


def _format_prior_task_summary(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"- Task {summary.get('task_id') or '(unknown)'}: {summary.get('task_title') or '(untitled task)'}",
        f"  Directory: {summary.get('directory') or '(unknown directory)'}",
    ]

    files = [str(item) for item in summary.get("files", []) if str(item).strip()]
    if files:
        lines.append(f"  Files: {', '.join(files)}")

    for label, key in (
        ("Exported types", "exported_types"),
        ("Exported functions", "exported_functions"),
        ("Exported classes", "exported_classes"),
        ("Other exported values", "exported_values"),
    ):
        items = [str(item) for item in summary.get(key, []) if str(item).strip()]
        if items:
            lines.append(f"  {label}: {', '.join(items)}")

    return lines


def _build_traceability_comment(
    plan: ImplementationPlan,
    task: ImplementationTask,
    dependency_documents: list[DependencyDocument],
) -> str:
    lines = [
        "@generated-by: codd implement",
        f"@generated-from: {plan.path.as_posix()} ({plan.node_id})",
        f"@task-id: {task.task_id}",
        f"@task-title: {task.title}",
    ]
    for document in dependency_documents:
        lines.append(f"@generated-from: {document.path.as_posix()} ({document.node_id})")
    return "\n".join(lines)


def _prepend_traceability_comment(relative_path: str, comment_block: str, content: str) -> str:
    suffix = PurePosixPath(relative_path).suffix
    if suffix not in {".ts", ".tsx", ".js", ".jsx"}:
        return content

    formatted_comment = "\n".join(f"// {line}" for line in comment_block.splitlines())
    stripped_content = content.lstrip()
    if stripped_content.startswith("// @generated-by: codd implement"):
        return content
    return f"{formatted_comment}\n\n{content.lstrip()}"


def _strip_code_fence(block: str) -> str:
    stripped = block.strip()
    fenced = re.match(r"^```(?:[a-zA-Z0-9_+-]+)?\s*\n(?P<body>.*)\n```$", stripped, re.DOTALL)
    if fenced:
        return fenced.group("body")
    return stripped


def _looks_like_tsx(content: str) -> bool:
    return bool(re.search(r"</?[A-Z][A-Za-z0-9]*|return\s*\(\s*<", content))


def _split_deliverable_chunks(text: str) -> list[str]:
    chunks = re.split(r"[、/]", text or "")
    return [re.sub(r"\s+", " ", chunk).strip(" ・") for chunk in chunks if chunk.strip(" ・")]


def _derive_task_slug(title: str, module_hint: str, task_id: str) -> str:
    keyword_text = " ".join([title, module_hint]).casefold()
    keyword_map = {
        "project_initialization": ["bootstrap", "project", "基盤", "初期化"],
        "database_foundation": ["database", "db", "prisma", "schema", "rls", "sql"],
        "authentication": ["auth", "oauth", "jwt", "session", "認証", "認可", "rbac", "google"],
        "common_middleware": ["middleware", "request", "tenant", "監査", "role"],
        "ui_foundation": ["ui", "layout", "screen", "component", "ux"],
        "integration": ["integration", "stripe", "line", "sendgrid", "bunny", "4ms"],
        "testing": ["test", "lint", "eslint", "quality"],
    }
    for slug, keywords in keyword_map.items():
        if any(keyword in keyword_text for keyword in keywords):
            return slug

    generic_slug = _slug_from_text(title) or _slug_from_text(module_hint)
    if generic_slug:
        return generic_slug
    return f"task_{_slug_from_text(task_id)}"


def _slug_from_text(text: str) -> str:
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    ascii_text = re.sub(r"_+", "_", ascii_text)
    return ascii_text


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _clean_text_block(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _is_detailed_design_path(path: Path | str) -> bool:
    path_text = path.as_posix() if isinstance(path, Path) else str(path)
    parts = PurePosixPath(path_text).parts
    return len(parts) >= 2 and parts[0] == "docs" and parts[1] == "detailed_design"

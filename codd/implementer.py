"""CoDD implementer - direct design document to output path generation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import warnings
from typing import Any

import codd.generator as generator_module
from codd.generator import DependencyDocument, _load_project_config, _normalize_conventions
from codd.scanner import _extract_frontmatter, build_document_node_path_map


FILE_BLOCK_RE = re.compile(r"^=== FILE: (?P<path>.+?) ===\s*$", re.MULTILINE)
LANGUAGE_EXT_MAP: dict[str, tuple[str, ...]] = {
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx"),
    "python": (".py",),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "kotlin": (".kt",),
    "swift": (".swift",),
    "cpp": (".cpp", ".cc", ".h"),
    "c": (".c", ".h"),
    "csharp": (".cs",),
    "ruby": (".rb",),
}
LANGUAGE_ALIASES = {
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "py": "python",
    "rs": "rust",
    "golang": "go",
    "c++": "cpp",
    "cc": "cpp",
    "c#": "csharp",
    "cs": "csharp",
}
LANGUAGE_DISPLAY_NAMES = {
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "python": "Python",
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "cpp": "C++",
    "c": "C",
    "csharp": "C#",
    "ruby": "Ruby",
}
LANGUAGE_CODE_FENCE_MAP = {
    "typescript": "ts",
    "javascript": "js",
    "python": "python",
    "rust": "rust",
    "go": "go",
    "java": "java",
    "kotlin": "kotlin",
    "swift": "swift",
    "cpp": "cpp",
    "c": "c",
    "csharp": "csharp",
    "ruby": "ruby",
}
COMMENT_PREFIX_BY_SUFFIX = {
    ".ts": "//",
    ".tsx": "//",
    ".js": "//",
    ".jsx": "//",
    ".py": "#",
    ".rs": "//",
    ".go": "//",
    ".java": "//",
    ".kt": "//",
    ".swift": "//",
    ".cpp": "//",
    ".cc": "//",
    ".h": "//",
    ".c": "//",
    ".cs": "//",
    ".rb": "#",
}
UI_FILE_EXTENSIONS = {".tsx", ".jsx", ".vue", ".svelte", ".swift", ".kt", ".dart"}
SCREEN_FLOW_PROMPT_LIMIT = 8000
_DEFAULT_GUARD_FILES = ["middleware.ts", "middleware.js"]
_SKIP_GENERATION_RE = re.compile(
    r"(?mi)^\s*(?:[-*]\s*)?skip_generation\s*:\s*true\s*$",
)
_ROUTE_TOKEN_RE = re.compile(r"(?<![:\w])/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_ROUTE_HOME_KEYWORDS = {"home", "homepage", "landing", "root", "top", "top page"}
_ROUTE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_FRAMEWORK_KEYWORDS = {
    "angular",
    "astro",
    "next",
    "next.js",
    "nextjs",
    "nuxt",
    "react",
    "remix",
    "svelte",
    "vue",
}
_UI_TASK_KEYWORDS = frozenset(
    {
        "component",
        "frontend",
        "layout",
        "login",
        "page",
        "route",
        "screen",
        "signup",
        "ui",
        "ux",
        "view",
        "widget",
        "ログイン",
        "画面",
    }
    - _FRAMEWORK_KEYWORDS
)
UI_TASK_KEYWORDS = _UI_TASK_KEYWORDS
_WRAPPER_TASK_KEYWORDS = frozenset(
    {
        "page wrapper",
        "root page",
        "thin wrapper",
        "wrapper",
        "ページラッパー",
        "ラッパー",
    }
)
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
class ImplementSpec:
    design_node: str
    output_paths: list[str]
    dependency_design_nodes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        design_node = str(self.design_node).strip()
        if not design_node:
            raise ValueError("design_node is required")
        output_paths = _ordered_unique([str(item).strip() for item in self.output_paths if str(item).strip()])
        if not output_paths:
            raise ValueError("output_paths must contain at least one path")
        dependencies = _ordered_unique(
            [str(item).strip() for item in self.dependency_design_nodes if str(item).strip()]
        )
        object.__setattr__(self, "design_node", design_node)
        object.__setattr__(self, "output_paths", output_paths)
        object.__setattr__(self, "dependency_design_nodes", dependencies)

    @property
    def task_id(self) -> str:
        return self.design_node

    @property
    def title(self) -> str:
        return Path(self.design_node).stem.replace("_", " ").replace("-", " ").strip() or self.design_node

    @property
    def summary(self) -> str:
        return f"Implement {self.design_node}"

    @property
    def module_hint(self) -> str:
        return ", ".join(self.output_paths)

    @property
    def deliverable(self) -> str:
        return ", ".join(self.output_paths)

    @property
    def output_dir(self) -> str:
        return self.output_paths[0]

    @property
    def dependency_node_ids(self) -> list[str]:
        return list(self.dependency_design_nodes)

    @property
    def task_context(self) -> str:
        return ""


@dataclass(frozen=True)
class DesignContext:
    node_id: str
    path: Path
    content: str
    depends_on: list[dict[str, Any]] = field(default_factory=list)
    conventions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ImplementationResult:
    design_node: str
    output_paths: list[Path]
    generated_files: list[Path]
    error: str | None = None

    @property
    def task_id(self) -> str:
        return self.design_node

    @property
    def task_title(self) -> str:
        return Path(self.design_node).stem.replace("_", " ").replace("-", " ").strip() or self.design_node

    @property
    def output_dir(self) -> Path:
        return self.output_paths[0]


class Implementer:
    def __init__(
        self,
        project_root: Path,
        *,
        config: dict[str, Any] | None = None,
        ai_command: str | None = None,
        use_derived_steps: bool | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config if config is not None else _load_project_config(self.project_root)
        self.ai_command = ai_command
        self.use_derived_steps = _use_derived_steps_enabled(self.config, use_derived_steps)

    def run_implement(self, spec: ImplementSpec) -> ImplementationResult:
        """Read ``spec.design_node`` and generate files under ``spec.output_paths``."""
        spec = _normalize_spec_paths(spec)
        _check_guard_files_uniqueness(self.project_root, self.config)
        _create_output_paths(self.project_root, spec.output_paths)

        design_context = _load_design_context(self.project_root, self.config, spec.design_node)
        node_paths = build_document_node_path_map(self.project_root, self.config)
        node_paths[design_context.node_id] = design_context.path
        node_paths[design_context.path.as_posix()] = design_context.path

        explicit_dependencies = list(spec.dependency_design_nodes)
        design_dependencies = [entry["id"] for entry in design_context.depends_on if isinstance(entry.get("id"), str)]
        dependency_documents, document_conventions = _collect_dependency_documents(
            self.project_root,
            _ordered_unique([*explicit_dependencies, *design_dependencies]),
            node_paths,
        )
        design_document = DependencyDocument(
            node_id=design_context.node_id,
            path=design_context.path,
            content=design_context.content,
        )
        all_documents = _dedupe_documents([design_document, *dependency_documents])

        combined_conventions = _merge_conventions(
            _normalize_conventions(self.config.get("conventions", [])),
            design_context.conventions,
            document_conventions,
        )
        coding_principles = _load_coding_principles(self.project_root, self.config)
        design_md_content = (
            _load_design_md_content(self.project_root)
            if _spec_generates_ui_file(spec, (self.config.get("project") or {}).get("language"), design_context.content)
            else None
        )
        screen_flow_content = (
            _load_screen_flow_for_implementation(self.project_root)
            if _spec_looks_ui_facing(spec, design_context.content)
            else None
        )
        screen_flow_routes = (
            _select_screen_flow_routes_for_spec(spec, design_context.content, screen_flow_content)
            if screen_flow_content
            else []
        )
        impl_steps_context = (
            _implementation_steps_context(
                config=self.config,
                spec=spec,
                dependency_documents=all_documents,
                project_root=self.project_root,
            )
            if self.use_derived_steps
            else None
        )
        prompt = _build_implementation_prompt(
            config=self.config,
            design_context=design_context,
            spec=spec,
            dependency_documents=dependency_documents,
            conventions=combined_conventions,
            coding_principles=coding_principles,
            design_md_content=design_md_content,
            screen_flow_content=screen_flow_content,
            screen_flow_routes=screen_flow_routes,
            impl_steps_context=impl_steps_context,
        )
        prompt = generator_module._inject_lexicon(prompt, self.project_root)
        resolved_ai_command = generator_module._resolve_ai_command(
            self.config,
            self.ai_command,
            command_name="implement",
        )
        try:
            raw_output = generator_module._invoke_ai_command(
                resolved_ai_command,
                prompt,
                project_root=self.project_root,
            )
        except ValueError as exc:
            if "empty output" not in str(exc).casefold():
                raise
            if _skip_generation_enabled(design_context.content):
                raw_output = ""
            else:
                raise _zero_generated_files_error(spec) from exc

        language = _normalize_implementation_language((self.config.get("project") or {}).get("language"))
        if _skip_generation_enabled(design_context.content) and not raw_output.strip():
            generated_files: list[Path] = []
        else:
            try:
                generated_files = _write_generated_files(
                    project_root=self.project_root,
                    design_context=design_context,
                    spec=spec,
                    dependency_documents=dependency_documents,
                    language=language,
                    raw_output=raw_output,
                )
            except ValueError as exc:
                raise _zero_generated_files_error(spec) from exc

        if len(generated_files) == 0 and not _skip_generation_enabled(design_context.content):
            raise _zero_generated_files_error(spec)
        return ImplementationResult(
            design_node=spec.design_node,
            output_paths=[_resolve_output_path(self.project_root, item) for item in spec.output_paths],
            generated_files=generated_files,
        )


def implement_tasks(
    project_root: Path,
    *,
    design: str | None = None,
    output_paths: list[str] | tuple[str, ...] | None = None,
    dependency_design_nodes: list[str] | tuple[str, ...] | None = None,
    ai_command: str | None = None,
    clean: bool = False,
    use_derived_steps: bool | None = None,
    task: str | None = None,
    language: str | None = None,
    **_ignored: Any,
) -> list[ImplementationResult]:
    project_root = Path(project_root).resolve()
    config = _load_project_config(project_root)
    if language:
        # Per-invocation language override (Issue #20, v-kato): mismatched
        # codd init --language doesn't force a full re-init; spec authors can
        # ship an implement run with --language typescript and the project's
        # codd.yaml stays untouched.
        project_cfg = dict(config.get("project") or {})
        project_cfg["language"] = language
        config = {**config, "project": project_cfg}
    design_node = design or task
    if not design_node:
        raise ValueError("--design is required")
    outputs = list(output_paths or _default_output_paths_for_design(config, design_node))
    spec = ImplementSpec(
        design_node=design_node,
        output_paths=outputs,
        dependency_design_nodes=list(dependency_design_nodes or ()),
    )
    spec = _normalize_spec_paths(spec)
    if clean:
        _clean_output_paths(project_root, spec.output_paths)
    result = Implementer(
        project_root,
        config=config,
        ai_command=ai_command,
        use_derived_steps=use_derived_steps,
    ).run_implement(spec)
    return [result]


def get_valid_task_slugs(project_root: Path) -> set[str]:
    config = _load_project_config(Path(project_root).resolve())
    values: set[str] = set()
    for paths in _configured_output_path_groups(config).values():
        for item in paths:
            name = PurePosixPath(item).name
            if name:
                values.add(name)
    return values


def auto_detect_task(project_root: Path) -> str:
    project_root = Path(project_root).resolve()
    config = _load_project_config(project_root)
    configured = _configured_output_path_groups(config)
    if len(configured) == 1:
        return next(iter(configured.keys()))

    candidates = _auto_detect_approved_derived_task_candidates(project_root)
    candidates = _ordered_unique(candidates)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("could not auto-detect a design node; pass --design")
    raise ValueError(
        "multiple implementation task candidates found "
        f"({', '.join(candidates)}); pass --design"
    )


def _auto_detect_approved_derived_task_candidates(project_root: Path) -> list[str]:
    from codd.llm.plan_deriver import iter_derived_task_records

    records = iter_derived_task_records(project_root)
    records.sort(key=lambda item: _path_mtime(item[0]), reverse=True)
    for _cache_path, record in records:
        approved = [task.id for task in record.tasks if task.approved]
        if approved:
            return approved
    return []


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _default_output_paths_for_design(config: dict[str, Any], design: str) -> list[str]:
    configured = _configured_output_path_groups(config)
    if design in configured:
        return configured[design]
    raise ValueError("--output is required unless codd.yaml maps the design node to output paths")


def _configured_output_path_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    implement = config.get("implement") if isinstance(config.get("implement"), dict) else {}
    groups: dict[str, list[str]] = {}
    for key in ("default_output_paths", "implement_targets"):
        payload = implement.get(key) if isinstance(implement, dict) else None
        if not isinstance(payload, dict):
            continue
        for design, paths in payload.items():
            if isinstance(paths, str):
                groups[str(design)] = [paths]
            elif isinstance(paths, list):
                groups[str(design)] = [str(item) for item in paths if str(item).strip()]
    return groups


def _normalize_spec_paths(spec: ImplementSpec) -> ImplementSpec:
    output_paths = [_normalize_project_path(item) for item in spec.output_paths]
    return ImplementSpec(
        design_node=spec.design_node,
        output_paths=output_paths,
        dependency_design_nodes=spec.dependency_design_nodes,
    )


def _normalize_project_path(path_text: str) -> str:
    path = PurePosixPath(str(path_text).strip())
    if path.is_absolute():
        return path.as_posix()
    if not path.parts or any(part == ".." for part in path.parts):
        raise ValueError(f"output path must stay within the project: {path_text}")
    return path.as_posix().rstrip("/")


def _resolve_output_path(project_root: Path, output_path: str) -> Path:
    path = Path(output_path)
    resolved = path if path.is_absolute() else project_root / path
    return resolved.resolve(strict=False)


def _create_output_paths(project_root: Path, output_paths: list[str]) -> None:
    for output_path in output_paths:
        destination = _resolve_output_path(project_root, output_path)
        _ensure_inside_project(project_root, destination, "output path")
        destination.mkdir(parents=True, exist_ok=True)


def _clean_output_paths(project_root: Path, output_paths: list[str]) -> None:
    for output_path in output_paths:
        destination = _resolve_output_path(project_root, output_path)
        _ensure_inside_project(project_root, destination, "output path")
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()


def _ensure_inside_project(project_root: Path, path: Path, label: str) -> None:
    root = project_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay within the project: {path}") from exc


def _load_design_context(project_root: Path, config: dict[str, Any], design_node: str) -> DesignContext:
    path = _resolve_design_path(project_root, config, design_node)
    codd = _extract_frontmatter(path) or {}
    content = path.read_text(encoding="utf-8")
    return DesignContext(
        node_id=str(codd.get("node_id") or _relative_path(project_root, path).as_posix()),
        path=_relative_path(project_root, path),
        content=content,
        depends_on=generator_module._normalize_dependencies(codd.get("depends_on", [])),
        conventions=_normalize_conventions(codd.get("conventions", [])),
    )


def _resolve_design_path(project_root: Path, config: dict[str, Any], design_node: str) -> Path:
    candidate = Path(design_node)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise FileNotFoundError(f"design document not found: {design_node}")
        _ensure_inside_project(project_root, candidate, "design document")
        return candidate

    project_relative = project_root / candidate
    if project_relative.is_file():
        return project_relative

    node_paths = build_document_node_path_map(project_root, config)
    mapped = node_paths.get(design_node)
    if mapped is not None and (project_root / mapped).is_file():
        return project_root / mapped

    raise FileNotFoundError(f"design document not found: {design_node}")


def _relative_path(project_root: Path, path: Path) -> Path:
    return path.resolve(strict=False).relative_to(project_root.resolve(strict=False))


def _dedupe_documents(documents: list[DependencyDocument]) -> list[DependencyDocument]:
    seen: set[str] = set()
    result: list[DependencyDocument] = []
    for document in documents:
        key = document.path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        result.append(document)
    return result


def _use_derived_steps_enabled(config: dict[str, Any], override: bool | None) -> bool:
    if override is not None:
        return bool(override)
    implementer_config = config.get("implementer")
    if isinstance(implementer_config, dict) and "use_derived_steps" in implementer_config:
        return bool(implementer_config.get("use_derived_steps"))
    return False


def _implementation_steps_context(
    *,
    config: dict[str, Any],
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    project_root: Path,
) -> str | None:
    from codd.llm.impl_step_deriver import render_impl_steps_for_prompt

    steps = _load_or_derive_implementation_steps(config, spec, dependency_documents, project_root)
    explicit = _filter_layer1_impl_steps([step for step in steps if not step.inferred], config)
    implicit = _filter_layer2_impl_steps([step for step in steps if step.inferred], config)
    if not explicit and not implicit:
        return None

    lines: list[str] = []
    if explicit:
        lines.extend(
            [
                "[Layer 1 - Explicit, from design]",
                render_impl_steps_for_prompt(explicit),
            ]
        )
    if implicit:
        if lines:
            lines.append("")
        lines.extend(
            [
                "[Layer 2 - Inferred, best-practice augment]",
                render_impl_steps_for_prompt(implicit),
            ]
        )
    return "\n".join(lines)


def _load_or_derive_implementation_steps(
    config: dict[str, Any],
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    project_root: Path,
) -> list[Any]:
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    from codd.llm.best_practice_augmenter import SubprocessAiCommandBestPracticeAugmenter
    from codd.llm.impl_step_deriver import (
        ImplStepCacheRecord,
        SubprocessAiCommandImplStepDeriver,
        impl_step_cache_path,
        merge_impl_steps,
        read_impl_step_cache,
        utc_timestamp,
        write_impl_step_cache,
    )

    context = {"project_root": project_root, "config": config, "project_context": {"project": config.get("project", {})}}
    cache_path = impl_step_cache_path(spec, context)
    record = read_impl_step_cache(cache_path)
    steps = list(record.steps) if record is not None else []
    explicit = [step for step in steps if not step.inferred]
    implicit = [step for step in steps if step.inferred]
    nodes = _dependency_documents_as_nodes(dependency_documents)

    derive_command = _ai_command_from_config(config, "impl_step_derive")
    if not explicit and derive_command and nodes:
        deriver = SubprocessAiCommandImplStepDeriver(
            SubprocessAiCommand(command=derive_command, project_root=project_root, config=config),
        )
        explicit = deriver.derive_steps(spec, nodes, context)
        record = read_impl_step_cache(cache_path)
        steps = list(record.steps) if record is not None else explicit
    elif not explicit and not derive_command and nodes:
        # K-2 cmd_345: detect silent fail of operation_flow_hint injection
        from codd.llm.criteria_expander import warn_if_operation_flow_unused

        warn_if_operation_flow_unused(config, nodes)

    augment_command = _ai_command_from_config(config, "best_practice_augment")
    if explicit and not implicit and augment_command and _best_practice_augment_enabled(config):
        augmenter = SubprocessAiCommandBestPracticeAugmenter(
            SubprocessAiCommand(command=augment_command, project_root=project_root, config=config),
        )
        implicit = augmenter.suggest_implicit_steps(spec, nodes, explicit, context)
        if implicit:
            merged = merge_impl_steps(explicit, implicit)
            base_record = read_impl_step_cache(cache_path)
            write_impl_step_cache(
                cache_path,
                ImplStepCacheRecord(
                    provider_id=(base_record.provider_id if base_record else "subprocess_ai_command"),
                    cache_key=((base_record.cache_key if base_record else spec.task_id) + ":augmented"),
                    task_id=(base_record.task_id if base_record else spec.task_id),
                    design_doc_sha=(base_record.design_doc_sha if base_record else ""),
                    prompt_template_sha=(base_record.prompt_template_sha if base_record else ""),
                    generated_at=utc_timestamp(),
                    design_docs=(base_record.design_docs if base_record else [node.path or node.id for node in nodes]),
                    steps=merged,
                ),
            )
            steps = merged

    return steps


def _dependency_documents_as_nodes(dependency_documents: list[DependencyDocument]):
    from codd.dag import Node

    return [
        Node(
            id=document.node_id,
            kind="design_doc",
            path=document.path.as_posix(),
            attributes={"content": document.content},
        )
        for document in dependency_documents
    ]


def _filter_layer1_impl_steps(steps: list[Any], config: dict[str, Any]) -> list[Any]:
    implementer_config = config.get("implementer") if isinstance(config.get("implementer"), dict) else {}
    per_kind = implementer_config.get("approval_mode_per_step_kind") if isinstance(implementer_config, dict) else {}
    if not isinstance(per_kind, dict):
        per_kind = {}
    approved: list[Any] = []
    for step in steps:
        mode = str(per_kind.get(step.kind, "required"))
        if mode == "auto" or bool(getattr(step, "approved", False)):
            approved.append(step)
    return approved


def _filter_layer2_impl_steps(steps: list[Any], config: dict[str, Any]) -> list[Any]:
    from codd.llm.approval import filter_layer_2_impl_steps

    return filter_layer_2_impl_steps(steps, config)


def _ai_command_from_config(config: dict[str, Any], name: str) -> str | None:
    ai_commands = config.get("ai_commands")
    if not isinstance(ai_commands, dict):
        return None
    value = ai_commands.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("command"), str):
        return value["command"]
    return None


def _best_practice_augment_enabled(config: dict[str, Any]) -> bool:
    implementer_config = config.get("implementer")
    if not isinstance(implementer_config, dict):
        return True
    return bool(implementer_config.get("use_best_practice_augmenter", True))


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


def _load_design_md_content(project_root: Path) -> str | None:
    try:
        from codd.design_md import DesignMdExtractor
    except ImportError:
        return None

    design_md_path = project_root / "DESIGN.md"
    if not design_md_path.exists():
        warnings.warn(
            "DESIGN.md not found. UI file generation will proceed without design tokens. "
            "Consider creating DESIGN.md (https://github.com/google-labs-code/design.md)",
            UserWarning,
            stacklevel=3,
        )
        return None

    result = DesignMdExtractor().extract(design_md_path)
    if result.error:
        warnings.warn(f"DESIGN.md parse error: {result.error}", UserWarning, stacklevel=3)
        return None

    lines = ["# DESIGN.md tokens (W3C Design Tokens spec)"]
    for token in result.tokens:
        lines.append(f"- {token.id} ({token.category}): {token.value}")
    return "\n".join(lines)


def _load_screen_flow_for_implementation(project_root: Path) -> str | None:
    try:
        from codd.screen_flow_validator import find_screen_flow_path

        screen_flow_path = find_screen_flow_path(project_root)
    except ImportError:
        default_path = project_root / "docs" / "extracted" / "screen-flow.md"
        screen_flow_path = default_path if default_path.exists() else None

    if screen_flow_path is None:
        warnings.warn(
            "screen-flow.md not found. UI file generation will proceed without "
            "route definitions. Consider creating docs/extracted/screen-flow.md.",
            UserWarning,
            stacklevel=3,
        )
        return None
    return screen_flow_path.read_text(encoding="utf-8")


def _is_ui_task(task_title: str, task_description: str = "") -> bool:
    text = f"{task_title} {task_description}".casefold()
    for keyword in _UI_TASK_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", text):
            return True
    return False


def _is_wrapper_task(task_title: str, task_description: str = "") -> bool:
    text = f"{task_title} {task_description}".casefold()
    for keyword in _WRAPPER_TASK_KEYWORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", text):
            return True
    return _is_ui_task(task_title, task_description)


def _check_guard_files_uniqueness(project_root: Path, config: dict[str, Any] | None = None) -> None:
    guard_files = list(_DEFAULT_GUARD_FILES)
    if config:
        implementer_config = config.get("implementer") or {}
        override = (
            implementer_config.get("guard_files")
            if isinstance(implementer_config, dict)
            else None
        )
        if isinstance(override, str) and override.strip():
            guard_files = [override.strip()]
        elif isinstance(override, list):
            configured = [str(item).strip() for item in override if str(item).strip()]
            if configured:
                guard_files = configured

    for filename in guard_files:
        candidates = sorted(project_root.rglob(filename))
        if len(candidates) > 1:
            warnings.warn(
                f"Multiple '{filename}' detected: {[str(p.relative_to(project_root)) for p in candidates]}. "
                f"Keep only ONE (usually the root-level file). "
                f"Remove duplicates to avoid dead code. "
                f"Override this check via codd.yaml [implementer] guard_files.",
                UserWarning,
                stacklevel=3,
            )


def _select_screen_flow_routes_for_spec(
    spec: ImplementSpec,
    design_content: str,
    screen_flow_content: str | None,
) -> list[str]:
    if not screen_flow_content:
        return []

    routes = _parse_screen_flow_routes_from_text(screen_flow_content)
    if not routes:
        return []

    relevant = [route for route in routes if _route_matches_spec(route, spec, design_content)]
    return relevant or routes[:20]


def _parse_screen_flow_routes_from_text(screen_flow_content: str) -> list[str]:
    routes: list[str] = []
    for match in _ROUTE_TOKEN_RE.finditer(screen_flow_content):
        route = _normalize_screen_flow_route(match.group(0))
        if route and route not in routes:
            routes.append(route)
    return routes


def _normalize_screen_flow_route(route: str) -> str:
    normalized = route.strip().strip("`\"'")
    normalized = normalized.rstrip(".,;。、)")
    if not normalized.startswith("/") or normalized.startswith("//"):
        return ""
    return normalized.rstrip("/") or "/"


def _route_matches_spec(route: str, spec: ImplementSpec, design_content: str) -> bool:
    task_text = " ".join(
        [
            spec.design_node,
            " ".join(spec.output_paths),
            " ".join(spec.dependency_design_nodes),
            design_content,
        ]
    ).casefold()
    if route.casefold() in task_text:
        return True

    if route == "/":
        return any(keyword in task_text for keyword in _ROUTE_HOME_KEYWORDS)

    segments = [_normalize_route_segment(segment) for segment in route.split("/") if segment]
    return any(segment and segment in task_text for segment in segments)


def _normalize_route_segment(segment: str) -> str:
    return _ROUTE_NORMALIZE_RE.sub("", segment.casefold())


def _skip_generation_enabled(design_content: str) -> bool:
    return bool(_SKIP_GENERATION_RE.search(design_content))


def _zero_generated_files_error(spec: ImplementSpec) -> Exception:
    from codd.cli import CoddCLIError

    return CoddCLIError(
        f"Design '{spec.design_node}' produced 0 generated files. "
        "If this is intentional, add 'skip_generation: true' to the design document. "
        "Otherwise, verify the design document contains sufficient implementation details."
    )


def _spec_generates_ui_file(spec: ImplementSpec, language: Any, design_content: str) -> bool:
    for path in _candidate_generated_paths(spec, language, design_content):
        if path.suffix.lower() in UI_FILE_EXTENSIONS:
            return True
    return False


def _candidate_generated_paths(spec: ImplementSpec, language: Any, design_content: str) -> list[PurePosixPath]:
    candidates: list[PurePosixPath] = []
    fields = [
        spec.design_node,
        " ".join(spec.output_paths),
        " ".join(spec.dependency_design_nodes),
        design_content,
    ]
    for field in fields:
        for match in re.findall(r"[\w@./-]+\.(?:tsx|jsx|vue|svelte|swift|kt|dart)\b", field or "", re.IGNORECASE):
            candidates.append(PurePosixPath(match))

    default_extension = _default_generated_extension(language)
    for output_path in spec.output_paths:
        candidates.append(PurePosixPath(output_path) / f"index{default_extension}")

    normalized_language = _normalize_implementation_language(language)
    if normalized_language in {"typescript", "javascript"} and _spec_looks_ui_facing(spec, design_content):
        extensions = _implementation_language_extensions(normalized_language)
        if len(extensions) > 1:
            for output_path in spec.output_paths:
                candidates.append(PurePosixPath(output_path) / f"index{extensions[1]}")

    return candidates


def _spec_looks_ui_facing(spec: ImplementSpec, design_content: str) -> bool:
    return _is_ui_task(
        spec.design_node,
        " ".join([*spec.output_paths, *spec.dependency_design_nodes, design_content]),
    )


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
        resolved_node_id = node_id
        if rel_path is None:
            try:
                context = _load_design_context(project_root, {}, node_id)
            except (FileNotFoundError, ValueError):
                if node_id in required_node_ids:
                    missing.append(node_id)
                continue
            rel_path = context.path
            resolved_node_id = context.node_id
            node_paths[context.node_id] = context.path
            node_paths[context.path.as_posix()] = context.path

        doc_path = project_root / rel_path
        if not doc_path.exists():
            if node_id in required_node_ids:
                raise ValueError(
                    f"dependency document {node_id!r} maps to {rel_path.as_posix()}, but the file does not exist"
                )
            continue

        content = doc_path.read_text(encoding="utf-8")
        documents.append(DependencyDocument(node_id=resolved_node_id, path=rel_path, content=content))

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
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    coding_principles: str | None,
    prior_task_outputs: list[dict[str, Any]] | None = None,
    design_md_content: str | None = None,
    screen_flow_content: str | None = None,
    screen_flow_routes: list[str] | None = None,
    impl_steps_context: str | None = None,
) -> str:
    project = config.get("project") or {}
    frameworks = project.get("frameworks") or []
    language = _normalize_implementation_language(project.get("language"))
    language_name = LANGUAGE_DISPLAY_NAMES.get(language, language)
    preferred_extensions = _implementation_language_extensions(language)
    default_extension = _default_generated_extension(language)
    code_fence_language = LANGUAGE_CODE_FENCE_MAP.get(language, language)
    framework_text = ", ".join(str(item) for item in frameworks) if frameworks else "(unspecified)"
    if frameworks:
        framework_guidance = f"- Honor the configured framework stack ({framework_text}) when relevant."
    else:
        framework_guidance = f"- Use idiomatic {language_name} patterns for the target project."

    if language in {"typescript", "javascript"} and len(preferred_extensions) > 1:
        jsx_extension = preferred_extensions[1]
        extension_guidance = (
            f"- If the work needs JSX-style UI components, emit {jsx_extension} files. "
            f"Otherwise prefer {default_extension} files."
        )
    elif len(preferred_extensions) > 1:
        extension_guidance = (
            f"- Use {default_extension} files by default. "
            f"Additional allowed extensions for this language family: {', '.join(preferred_extensions)}."
        )
    else:
        extension_guidance = f"- Use {default_extension} files for generated source unless the design explicitly requires another file type."

    prior_task_outputs = prior_task_outputs or []
    output_text = ", ".join(spec.output_paths)
    example_output = spec.output_paths[0]
    lines = [
        "You are generating implementation code from CoDD design documents.",
        f"Project name: {project.get('name') or '(unknown)'}",
        f"Primary language: {language}",
        f"Framework stack: {framework_text}",
        f"Design node: {design_context.path.as_posix()} ({design_context.node_id})",
        f"Requested design: {spec.design_node}",
        f"Output paths: {output_text}",
        "",
        "Mandatory instructions:",
        f"- Generate concrete production-oriented {language_name} source files.",
        framework_guidance,
        "- Reflect security, data boundaries, authentication, authorization, and auditability explicitly where the design requires them.",
        "- The tool will prepend traceability comments to each generated file; do not emit separate metadata files.",
        "- Do not emit prose, explanations, Markdown headings, YAML, TODOs, placeholders, or file descriptions outside the required FILE blocks.",
        "- Every generated file path must stay under one of the output paths shown above.",
        extension_guidance,
        "- Favor small coherent modules rather than one monolithic file.",
        "- Cross-file imports may use relative imports or project-local aliases, but keep the output internally coherent.",
        "",
        "Required output format (repeat this block for each file and output nothing else):",
        f"=== FILE: {example_output}/<filename>{default_extension} ===",
        f"```{code_fence_language}",
        "# code" if default_extension in {".py", ".rb"} else "// code",
        "```",
        "",
        "ABSOLUTE PROHIBITION: Outputting prose, planning notes, TODO markers, or files outside the requested output paths is a CRITICAL ERROR.",
        "",
        "Design document content:",
        design_context.content.rstrip(),
    ]

    if spec.dependency_design_nodes:
        lines.extend(["", "Explicit dependency design nodes:"])
        lines.extend(f"- {item}" for item in spec.dependency_design_nodes)

    if impl_steps_context:
        lines.extend(
            [
                "",
                "Implementation steps to follow (LLM-derived, project-approved):",
                impl_steps_context.rstrip(),
            ]
        )

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
                "- If a convention concerns security, data boundaries, or auth, implement a concrete control rather than only comments.",
            ]
        )
        for index, convention in enumerate(conventions, start=1):
            targets = ", ".join(target for target in convention.get("targets", []) if isinstance(target, str))
            reason = convention.get("reason") or "(no reason provided)"
            lines.append(f"{index}. Targets: {targets or '(no explicit targets)'}")
            lines.append(f"   Reason: {reason}")

    successful_prior_outputs = [s for s in prior_task_outputs if not s.get("error")]
    if successful_prior_outputs:
        lines.extend(
            [
                "",
                "Prior implementations:",
                "- The following summaries describe code that was already generated.",
                "- ABSOLUTE PROHIBITION: Re-implementing the same type definitions, utility functions, classes, guards, middleware, or helpers is a CRITICAL ERROR.",
                "- Reuse these implementations via imports. If a needed symbol already exists below, import it instead of redefining it.",
            ]
        )
        for summary in successful_prior_outputs:
            lines.extend(_format_prior_task_summary(summary))

    lines.extend(["", "Dependency documents:"])
    for document in dependency_documents:
        lines.extend(
            [
                f"--- BEGIN DEPENDENCY {document.path.as_posix()} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END DEPENDENCY {document.path.as_posix()} ---",
                "",
            ]
        )

    if design_md_content:
        lines.extend(
            [
                "DESIGN.md design token context:",
                "- Apply these W3C-style design tokens when generating UI files.",
                design_md_content.rstrip(),
                "",
            ]
        )

    if screen_flow_content:
        route_lines = list(screen_flow_routes or [])
        lines.extend(["--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---"])
        if route_lines:
            lines.append("This UI work must implement the relevant route(s):")
            for route in route_lines:
                lines.append(f"- {route}")
            lines.append("")
        lines.append(screen_flow_content[:SCREEN_FLOW_PROMPT_LIMIT].rstrip())
        lines.extend(["--- END SCREEN-FLOW ---", ""])

    if _is_wrapper_task(spec.design_node, " ".join([*spec.output_paths, design_context.content])):
        lines.extend(
            [
                "--- WRAPPER COMPONENT RULES ---",
                "When generating a UI page wrapper that wraps a form, screen, or route component:",
                "1. Identify the component name from screen-flow.md or design docs. Do not rename it.",
                "2. Wire all callbacks the component requires.",
                "3. Pass required props from router, session, context, or equivalent platform services as needed.",
                "4. Do not generate a thin wrapper that ignores required component props.",
                "--- END WRAPPER RULES ---",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _write_generated_files(
    *,
    project_root: Path,
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
    language: str,
    raw_output: str,
) -> list[Path]:
    file_payloads = _parse_file_payloads(raw_output, spec.output_paths, language)
    traceability_comment = _build_traceability_comment(design_context, spec, dependency_documents)
    generated_paths: list[Path] = []
    for relative_path, content in file_payloads:
        destination = project_root / relative_path
        _ensure_inside_project(project_root, destination, "generated file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_prepend_traceability_comment(relative_path, traceability_comment, content), encoding="utf-8")
        generated_paths.append(destination)
    return generated_paths


def _parse_file_payloads(raw_output: str, output_paths: list[str], language: str) -> list[tuple[str, str]]:
    cleaned_output = raw_output.strip()
    output_prefixes = [PurePosixPath(item) for item in output_paths]
    matches = list(FILE_BLOCK_RE.finditer(cleaned_output))
    if not matches:
        fallback_content = _strip_code_fence(cleaned_output).strip()
        if not fallback_content:
            raise ValueError("AI command returned empty implementation output")
        extension = _default_generated_extension(language, fallback_content)
        return [(f"{output_paths[0]}/index{extension}", fallback_content.rstrip() + "\n")]

    payloads: list[tuple[str, str]] = []
    skipped: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned_output)
        block = cleaned_output[start:end].strip()
        path_text = match.group("path").strip()
        path = PurePosixPath(path_text)
        if path.is_absolute() or ".." in path.parts:
            skipped.append(f"{path_text!r}: path traversal")
            continue
        if not any(_path_starts_with(path, prefix) for prefix in output_prefixes):
            skipped.append(f"{path_text!r}: outside output paths {output_paths!r}")
            continue

        content = _strip_code_fence(block).strip()
        if not content:
            skipped.append(f"{path_text!r}: empty content")
            continue
        payloads.append((path.as_posix(), content.rstrip() + "\n"))

    if skipped:
        import sys
        for reason in skipped:
            print(f"Warning: skipped generated file - {reason}", file=sys.stderr)

    if not payloads:
        raise ValueError(
            f"AI produced {len(matches)} file block(s) but all were invalid: {'; '.join(skipped)}"
        )

    return payloads


def _path_starts_with(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return tuple(path.parts[: len(prefix.parts)]) == prefix.parts


def _summarize_generated_task_output(
    project_root: Path,
    spec: ImplementSpec,
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
        "task_id": spec.task_id,
        "task_title": spec.title,
        "directory": ", ".join(spec.output_paths),
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
    design_context: DesignContext,
    spec: ImplementSpec,
    dependency_documents: list[DependencyDocument],
) -> str:
    lines = [
        "@generated-by: codd implement",
        f"@generated-from: {design_context.path.as_posix()} ({design_context.node_id})",
        f"@design-node: {spec.design_node}",
        f"@output-paths: {', '.join(spec.output_paths)}",
    ]
    for document in dependency_documents:
        lines.append(f"@generated-from: {document.path.as_posix()} ({document.node_id})")
    return "\n".join(lines)


def _prepend_traceability_comment(relative_path: str, comment_block: str, content: str) -> str:
    prefix = COMMENT_PREFIX_BY_SUFFIX.get(PurePosixPath(relative_path).suffix.lower())
    if prefix is None:
        return content

    formatted_comment = "\n".join(f"{prefix} {line}" for line in comment_block.splitlines())
    stripped_content = content.lstrip()
    if stripped_content.startswith(f"{prefix} @generated-by: codd implement"):
        return content
    return f"{formatted_comment}\n\n{content.lstrip()}"


def _strip_code_fence(block: str) -> str:
    stripped = block.strip()
    # Non-greedy `.*?` captures up to the FIRST closing fence; any trailing
    # prose/markdown after the fence is discarded (Issue #22, v-kato).
    # Drop the `$` end-of-string anchor so the match still wins when the
    # LLM ignored the "no commentary" instruction and appended explanations.
    fenced = re.match(r"^```(?:[a-zA-Z0-9_+-]+)?\s*\n(?P<body>.*?)\n```", stripped, re.DOTALL)
    if fenced:
        return fenced.group("body")
    return stripped


def _looks_like_tsx(content: str) -> bool:
    return bool(re.search(r"</?[A-Z][A-Za-z0-9]*|return\s*\(\s*<", content))


def _normalize_implementation_language(language: Any) -> str:
    normalized = str(language or "").strip().lower()
    if not normalized:
        return "typescript"
    return LANGUAGE_ALIASES.get(normalized, normalized)


def _implementation_language_extensions(language: Any) -> tuple[str, ...]:
    normalized = _normalize_implementation_language(language)
    return LANGUAGE_EXT_MAP.get(normalized, LANGUAGE_EXT_MAP["typescript"])


def _default_generated_extension(language: Any, content: str | None = None) -> str:
    normalized = _normalize_implementation_language(language)
    extensions = _implementation_language_extensions(normalized)
    if normalized in {"typescript", "javascript"} and len(extensions) > 1 and content and _looks_like_tsx(content):
        return extensions[1]
    return extensions[0]


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

"""Brownfield pipeline orchestration for extract, diff, and elicit."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from codd.elicit.finding import ElicitResult, Finding


ExtractRunner = Callable[..., Any]
DiffEngineFactory = Callable[[Path], Any]
ElicitEngineFactory = Callable[[], Any]
LexiconLoader = Callable[[Path], Any]


@dataclass
class BrownfieldResult:
    extract_output: Path
    diff_findings: list[Finding]
    elicit_findings: list[Finding]
    merged_findings: list[Finding]
    extract_input: Path | None = None
    requirements_path: Path | None = None
    lexicon_path: Path | None = None
    extract_result: Any | None = None
    elicit_result: ElicitResult | Any | None = None
    stage_status: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "extract_output": _path_text(self.extract_output),
            "extract_input": _path_text(self.extract_input),
            "requirements_path": _path_text(self.requirements_path),
            "lexicon_path": _path_text(self.lexicon_path),
            "diff_findings": [finding.to_dict() for finding in self.diff_findings],
            "elicit_findings": [finding.to_dict() for finding in self.elicit_findings],
            "merged_findings": [finding.to_dict() for finding in self.merged_findings],
            "summary": {
                "diff_findings": len(self.diff_findings),
                "elicit_findings": len(self.elicit_findings),
                "merged_findings": len(self.merged_findings),
            },
        }
        # Machine-readable honesty signal: when a stage was skipped/partial/empty
        # the counts above are not the whole story. Only emitted for real
        # pipeline runs (directly-constructed results stay byte-stable / legacy).
        if self.stage_status is not None:
            payload["stage_status"] = self.stage_status
        if isinstance(self.elicit_result, ElicitResult):
            payload["elicit"] = {
                "all_covered": self.elicit_result.all_covered,
                "lexicon_coverage_report": self.elicit_result.lexicon_coverage_report,
                "metadata": self.elicit_result.metadata,
            }
        return payload


class BrownfieldPipeline:
    """Run the generic brownfield discovery pipeline.

    The pipeline intentionally delegates extraction, comparison, and elicitation
    logic to the existing engines. It only coordinates paths, optional inputs,
    and merged reporting.
    """

    def __init__(
        self,
        *,
        extract_runner: ExtractRunner | None = None,
        diff_engine_factory: DiffEngineFactory | None = None,
        elicit_engine_factory: ElicitEngineFactory | None = None,
        lexicon_loader: LexiconLoader | None = None,
        ai_command: str | None = None,
    ) -> None:
        self.extract_runner = extract_runner
        self.diff_engine_factory = diff_engine_factory
        self.elicit_engine_factory = elicit_engine_factory
        self.lexicon_loader = lexicon_loader
        self.ai_command = ai_command

    def run(
        self,
        target_path: Path | str,
        requirements_path: Path | str | None = None,
        lexicon_path: Path | str | None = None,
    ) -> BrownfieldResult:
        project_root = _resolve_project_root(target_path)
        extract_output = project_root / ".codd" / "extract"
        extract_result = self._run_extract(project_root, extract_output)
        result_output = Path(getattr(extract_result, "output_dir", extract_output))
        extract_input, extract_status = _ensure_aggregate_extract(
            result_output, getattr(extract_result, "generated_files", [])
        )

        requirements = _resolve_optional_file(
            project_root,
            requirements_path,
            default=".codd/requirements.md",
            label="requirements",
        )
        diff_findings: list[Finding] = []
        if requirements is not None:
            diff_findings = list(self._run_diff(project_root, extract_input, requirements))
        diff_status: dict[str, Any] = (
            {"status": "executed", "reason": None}
            if requirements is not None
            else {"status": "skipped", "reason": "no requirements.md"}
        )

        lexicon = _resolve_optional_path(
            project_root,
            lexicon_path,
            default=".codd/lexicon",
            label="lexicon",
        )
        lexicon_config = self._load_lexicon(lexicon) if lexicon is not None else None
        elicit_result = self._run_elicit(project_root, lexicon_config)
        elicit_findings = _findings_from_elicit_result(elicit_result)
        elicit_status = {
            "status": "ok",
            "mode": "lexicon" if lexicon is not None else "discovery",
        }

        stage_status = {
            "extract": extract_status,
            "diff": diff_status,
            "elicit": elicit_status,
        }

        merged_findings = merge_findings(diff_findings, elicit_findings)
        return BrownfieldResult(
            extract_output=result_output,
            extract_input=extract_input,
            requirements_path=requirements,
            lexicon_path=lexicon,
            diff_findings=diff_findings,
            elicit_findings=elicit_findings,
            merged_findings=merged_findings,
            extract_result=extract_result,
            elicit_result=elicit_result,
            stage_status=stage_status,
        )

    def _run_extract(self, project_root: Path, output_dir: Path) -> Any:
        if self.extract_runner is not None:
            return self.extract_runner(project_root, output=str(output_dir))
        from codd.extractor import run_extract

        return run_extract(project_root, output=str(output_dir))

    def _run_diff(self, project_root: Path, extract_input: Path, requirements_path: Path) -> Iterable[Finding]:
        engine = self._build_diff_engine(project_root)
        try:
            return engine.run_diff(
                extract_input=extract_input,
                requirements_path=requirements_path,
                ignored_findings=None,
            )
        except TypeError:
            return engine.run_diff(extract_input, requirements_path)

    def _run_elicit(self, project_root: Path, lexicon_config: Any | None) -> Any:
        engine = self._build_elicit_engine()
        return engine.run(project_root, lexicon_config=lexicon_config)

    def _build_diff_engine(self, project_root: Path) -> Any:
        if self.diff_engine_factory is not None:
            return self.diff_engine_factory(project_root)
        from codd.diff.engine import DiffEngine

        return DiffEngine(llm_client=self.ai_command, project_root=project_root)

    def _build_elicit_engine(self) -> Any:
        if self.elicit_engine_factory is not None:
            return self.elicit_engine_factory()
        from codd.elicit.engine import ElicitEngine

        return ElicitEngine(ai_command=self.ai_command)

    def _load_lexicon(self, lexicon_path: Path) -> Any:
        if self.lexicon_loader is not None:
            return self.lexicon_loader(lexicon_path)
        from codd.elicit.lexicon_loader import load_lexicon

        return load_lexicon(lexicon_path)


def merge_findings(*groups: Iterable[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    seen: set[str] = set()
    for group in groups:
        for finding in group:
            if finding.id in seen:
                continue
            seen.add(finding.id)
            merged.append(finding)
    return merged


def format_brownfield_result(result: BrownfieldResult, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    if format_name != "md":
        raise ValueError(f"unsupported brownfield format: {format_name}")
    return _format_brownfield_markdown(result)


def _format_brownfield_markdown(result: BrownfieldResult) -> str:
    from codd.elicit.formatters.md import MdFormatter

    stage_status = result.stage_status
    diff_stage = (stage_status or {}).get("diff") or {}
    diff_skipped = diff_stage.get("status") == "skipped"

    lines = [
        "# Brownfield Report",
        "",
        "## Summary",
        "",
        f"- extract_output: `{_path_text(result.extract_output)}`",
        f"- extract_input: `{_path_text(result.extract_input)}`",
        f"- requirements_path: `{_path_text(result.requirements_path) or 'skipped'}`",
        f"- lexicon_path: `{_path_text(result.lexicon_path) or 'discovery mode'}`",
    ]
    if diff_skipped:
        reason = diff_stage.get("reason") or "no requirements.md"
        # Honest headline: a skipped diff must not read as "0 findings" (pass).
        lines.append(f"- diff: SKIPPED ({reason})")
    else:
        lines.append(f"- diff_findings: {len(result.diff_findings)}")
    lines.extend(
        [
            f"- elicit_findings: {len(result.elicit_findings)}",
            f"- merged_findings: {len(result.merged_findings)}",
            "",
        ]
    )
    if stage_status:
        lines.extend(_format_stage_status_markdown(stage_status))
    findings_report = MdFormatter().format(result.merged_findings).strip()
    lines.append(findings_report)
    lines.append("")
    return "\n".join(lines)


def _format_stage_status_markdown(stage_status: dict[str, Any]) -> list[str]:
    lines = ["## Stage status", ""]

    extract = stage_status.get("extract") or {}
    failed = extract.get("files_failed") or []
    lines.append(
        f"- extract: {extract.get('status', 'unknown')} "
        f"(discovered={extract.get('files_discovered', 0)}, "
        f"aggregated={extract.get('files_aggregated', 0)}, "
        f"failed={len(failed)})"
    )
    for entry in failed:
        path, reason = entry[0], entry[1]
        lines.append(f"  - failed: `{path}` — {reason}")

    diff = stage_status.get("diff") or {}
    if diff.get("status") == "skipped":
        reason = diff.get("reason") or "no requirements.md"
        lines.append(f"- diff: SKIPPED ({reason})")
    else:
        lines.append(f"- diff: {diff.get('status', 'unknown')}")

    elicit = stage_status.get("elicit") or {}
    lines.append(f"- elicit: {elicit.get('status', 'unknown')} (mode={elicit.get('mode', 'unknown')})")
    lines.append("")
    return lines


def _resolve_project_root(value: Path | str) -> Path:
    project_root = Path(value).expanduser().resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"target path not found: {project_root}")
    if not project_root.is_dir():
        raise NotADirectoryError(f"target path is not a directory: {project_root}")
    return project_root


def _resolve_optional_file(
    project_root: Path,
    value: Path | str | None,
    *,
    default: str,
    label: str,
) -> Path | None:
    path = _resolve_optional_path(project_root, value, default=default, label=label)
    if path is None:
        return None
    if not path.is_file():
        if value is not None:
            raise FileNotFoundError(f"{label} file not found: {path}")
        return None
    return path


def _resolve_optional_path(
    project_root: Path,
    value: Path | str | None,
    *,
    default: str,
    label: str,
) -> Path | None:
    explicit = value is not None
    raw_path = Path(default) if value is None else Path(value).expanduser()
    path = raw_path if raw_path.is_absolute() else project_root / raw_path
    if path.exists():
        return path
    if explicit:
        raise FileNotFoundError(f"{label} path not found: {path}")
    return None


def _ensure_aggregate_extract(
    output_dir: Path, generated_files: Iterable[Path]
) -> tuple[Path, dict[str, Any]]:
    output_dir = Path(output_dir)
    aggregate_path = output_dir / "extracted.md"
    paths = _extract_markdown_paths(output_dir, generated_files)
    if aggregate_path.is_file():
        # A prior run already built the aggregate; treat it as usable content.
        status = {
            "status": "ok",
            "files_discovered": len(paths),
            "files_aggregated": len(paths),
            "files_failed": [],
        }
        return aggregate_path, status

    sections: list[str] = []
    files_failed: list[tuple[str, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            # Do NOT silently drop read failures: a partial aggregate that hides
            # unreadable inputs is a false-green. Collect and surface them.
            files_failed.append((_relative_to(path, output_dir), str(exc) or exc.__class__.__name__))
            continue
        if not text:
            continue
        sections.append(f"<!-- source: {_relative_to(path, output_dir)} -->\n\n{text}")

    body = "\n\n---\n\n".join(sections) if sections else "No extracted documents generated."
    output_dir.mkdir(parents=True, exist_ok=True)
    aggregate_path.write_text(f"# Extracted Brownfield Facts\n\n{body}\n", encoding="utf-8")

    if not sections:
        # A stub body (currently written unconditionally) is NOT an "ok" extract:
        # nothing usable was aggregated.
        status_name = "empty"
    elif files_failed:
        status_name = "partial"
    else:
        status_name = "ok"
    status = {
        "status": status_name,
        "files_discovered": len(paths),
        "files_aggregated": len(sections),
        "files_failed": files_failed,
    }
    return aggregate_path, status


def _extract_markdown_paths(output_dir: Path, generated_files: Iterable[Path]) -> list[Path]:
    collected = [
        Path(path)
        for path in generated_files
        if Path(path).suffix.lower() in {".md", ".markdown"} and Path(path).name != "extracted.md"
    ]
    if not collected and output_dir.is_dir():
        collected = [
            path
            for path in sorted(output_dir.rglob("*.md"))
            if path.name != "extracted.md"
        ]
    return sorted(collected, key=lambda path: path.as_posix())


def _findings_from_elicit_result(result: Any) -> list[Finding]:
    if isinstance(result, ElicitResult):
        return list(result.findings)
    findings = getattr(result, "findings", result)
    return list(findings)


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _path_text(path: Path | None) -> str | None:
    return path.as_posix() if path is not None else None

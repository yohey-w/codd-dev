"""Related test discovery and execution for CDAP."""

from __future__ import annotations

import shlex
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config
from codd.dag.builder import build_dag

DEFAULT_TEST_FRAMEWORKS_PATH = Path(__file__).parents[1] / "dag" / "defaults" / "test_frameworks.yaml"

FRAMEWORK_RUNNERS = {
    "pytest": "python -m pytest {files} -q",
    "jest": "npx jest {files}",
    "vitest": "npx vitest run {files}",
    "bats": "bats {files}",
    "go_test": "go test {files}",
}


def find_related_tests(project_root: Path, changed_files: list[str]) -> list[str]:
    """Find test files related to changed implementation files via tested_by edges."""

    root = Path(project_root).resolve()
    dag = build_dag(root)
    changed = {_normalize_changed_path(root, file_name) for file_name in changed_files}
    related: set[str] = set()

    for node_id, node in dag.nodes.items():
        if not node.path or not _matches_changed_path(str(node.path), changed):
            continue
        for edge in dag.edges:
            if edge.from_id != node_id or edge.kind != "tested_by":
                continue
            test_node = dag.nodes.get(edge.to_id)
            if test_node and test_node.path:
                related.add(str(test_node.path))

    return sorted(related)


def detect_test_framework(project_root: Path, settings: dict[str, Any] | None = None) -> str:
    """Detect the test framework from explicit config or project structure."""

    root = Path(project_root).resolve()
    explicit = _explicit_test_framework(settings)
    if explicit:
        return explicit

    try:
        explicit = _explicit_test_framework(load_project_config(root))
    except (FileNotFoundError, ValueError):
        explicit = None
    if explicit:
        return explicit

    if (root / "pyproject.toml").exists():
        return "pytest"
    if (root / "package.json").exists():
        pkg = (root / "package.json").read_text(encoding="utf-8", errors="ignore")
        if "vitest" in pkg:
            return "vitest"
        if "jest" in pkg:
            return "jest"
    if list(root.glob("**/*.bats")):
        return "bats"
    if (root / "go.mod").exists():
        return "go_test"

    return str(_load_test_defaults().get("test_framework") or "pytest")


def run_related_tests(
    project_root: Path,
    changed_files: list[str],
    settings: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run only tests related to changed files."""

    root = Path(project_root).resolve()
    related = find_related_tests(root, changed_files)
    if not related:
        return {"status": "no_tests_found", "related": [], "exit_code": None}

    framework = detect_test_framework(root, settings)
    runner_tmpl = _test_runners(root, settings).get(framework, FRAMEWORK_RUNNERS["pytest"])
    cmd = runner_tmpl.format(files=" ".join(shlex.quote(path) for path in related))
    if dry_run:
        return {"status": "dry_run", "related": related, "cmd": cmd, "exit_code": None}

    proc = subprocess.run(cmd, shell=True, cwd=str(root))
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "related": related,
        "cmd": cmd,
        "exit_code": proc.returncode,
    }


def _test_runners(project_root: Path, settings: dict[str, Any] | None = None) -> dict[str, str]:
    config = _load_test_defaults()
    try:
        config = _merge_test_settings(config, _test_config_section(load_project_config(project_root)))
    except (FileNotFoundError, ValueError):
        pass
    config = _merge_test_settings(config, _test_config_section(settings or {}))

    runners = config.get("test_runners", {})
    if not isinstance(runners, dict):
        runners = {}
    merged = {**FRAMEWORK_RUNNERS, **{str(name): str(template) for name, template in runners.items()}}
    return merged


def _load_test_defaults() -> dict[str, Any]:
    if not DEFAULT_TEST_FRAMEWORKS_PATH.is_file():
        return {"test_framework": "pytest", "test_runners": deepcopy(FRAMEWORK_RUNNERS)}
    payload = yaml.safe_load(DEFAULT_TEST_FRAMEWORKS_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {"test_framework": "pytest", "test_runners": deepcopy(FRAMEWORK_RUNNERS)}
    return _merge_test_settings({"test_framework": "pytest", "test_runners": deepcopy(FRAMEWORK_RUNNERS)}, payload)


def _merge_test_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if key == "test_runners" and isinstance(value, dict):
            runners = merged.get("test_runners", {})
            merged["test_runners"] = {**(runners if isinstance(runners, dict) else {}), **deepcopy(value)}
        else:
            merged[key] = deepcopy(value)
    return merged


def _test_config_section(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("test_framework", "test_runners"):
        if key in config:
            result[key] = config[key]

    section = config.get("test", {})
    if isinstance(section, dict):
        if "framework" in section:
            result["test_framework"] = section["framework"]
        if "test_framework" in section:
            result["test_framework"] = section["test_framework"]
        if "runners" in section:
            result["test_runners"] = section["runners"]
        if "test_runners" in section:
            result["test_runners"] = section["test_runners"]
    return result


def _explicit_test_framework(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    section = _test_config_section(config)
    value = section.get("test_framework")
    return str(value) if value else None


def _normalize_changed_path(project_root: Path, file_name: str) -> str:
    path = Path(file_name).expanduser()
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(project_root)
        except ValueError:
            return path.resolve().as_posix()
    return path.as_posix().lstrip("./")


def _matches_changed_path(node_path: str, changed_paths: set[str]) -> bool:
    normalized = node_path.lstrip("./")
    return any(
        normalized == changed
        or normalized.endswith(f"/{changed}")
        or changed.endswith(f"/{normalized}")
        or changed in normalized
        for changed in changed_paths
    )

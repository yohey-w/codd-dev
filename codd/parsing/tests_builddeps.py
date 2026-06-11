"""Test-evidence and build-dependency extraction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from codd.parsing._shared import BuildDepsInfo, TestInfo, _dedupe, _iter_project_files

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from codd.extractor import CallEdge, Symbol


class BuildDepsExtractor:
    """Extract build/runtime dependencies from common project manifests."""

    file_names = ("pyproject.toml", "package.json", "go.mod")

    def detect_build_files(self, project_root: Path) -> list[Path]:
        return [project_root / name for name in self.file_names if (project_root / name).exists()]

    def extract_deps(self, content: str, file_type: str, file_path: str = "") -> BuildDepsInfo:
        normalized = file_type.lower()
        if normalized == "pyproject.toml":
            return self._extract_pyproject(content, file_path)
        if normalized == "package.json":
            return self._extract_package_json(content, file_path)
        if normalized == "go.mod":
            return self._extract_go_mod(content, file_path)
        return BuildDepsInfo(file_path=file_path)

    def merge(self, infos: list[BuildDepsInfo]) -> BuildDepsInfo | None:
        if not infos:
            return None
        if len(infos) == 1:
            return infos[0]

        merged = BuildDepsInfo(
            file_path=", ".join(info.file_path for info in infos if info.file_path),
            runtime=[],
            dev=[],
            scripts={},
        )
        for info in infos:
            merged.runtime.extend(info.runtime)
            merged.dev.extend(info.dev)
            merged.scripts.update(info.scripts)
        merged.runtime = _dedupe(merged.runtime)
        merged.dev = _dedupe(merged.dev)
        return merged

    def _extract_pyproject(self, content: str, file_path: str) -> BuildDepsInfo:
        if tomllib is None:
            return BuildDepsInfo(file_path=file_path)

        try:
            payload = tomllib.loads(content)
        except Exception:
            return BuildDepsInfo(file_path=file_path)

        project = payload.get("project") or {}
        runtime = [str(dep) for dep in project.get("dependencies", []) or []]
        dev: list[str] = []
        for deps in (project.get("optional-dependencies") or {}).values():
            if isinstance(deps, list):
                dev.extend(str(dep) for dep in deps)

        scripts = {
            str(name): str(target)
            for name, target in (project.get("scripts") or {}).items()
        }
        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=_dedupe(dev),
            scripts=scripts,
        )

    def _extract_package_json(self, content: str, file_path: str) -> BuildDepsInfo:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return BuildDepsInfo(file_path=file_path)

        return BuildDepsInfo(
            file_path=file_path,
            runtime=sorted((payload.get("dependencies") or {}).keys()),
            dev=sorted((payload.get("devDependencies") or {}).keys()),
            scripts={
                str(name): str(command)
                for name, command in (payload.get("scripts") or {}).items()
            },
        )

    def _extract_go_mod(self, content: str, file_path: str) -> BuildDepsInfo:
        runtime: list[str] = []
        scripts: dict[str, str] = {}
        in_require_block = False

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            if line == "require (":
                in_require_block = True
                continue
            if line == ")" and in_require_block:
                in_require_block = False
                continue
            if line.startswith("require "):
                runtime.append(line.removeprefix("require ").split()[0])
                continue
            if in_require_block:
                runtime.append(line.split()[0])
                continue
            if line.startswith("replace "):
                left, _, right = line.removeprefix("replace ").partition("=>")
                scripts[f"replace:{left.strip()}"] = right.strip()

        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=[],
            scripts=scripts,
        )

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []

class TestExtractor:
    """Extract test metadata from test files."""

    def __init__(self, language: str):
        self.language = language.lower()

    def detect_test_files(self, project_root: Path) -> list[Path]:
        suffixes = {
            "python": {".py"},
            "typescript": {".ts", ".tsx"},
            "javascript": {".js", ".jsx"},
            "go": {".go"},
        }.get(self.language, set())
        if not suffixes:
            return []

        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, suffixes):
            if self._is_test_file(file_path.name):
                matches.append(file_path)
        return matches

    def extract_test_info(self, content: str, file_path: str) -> TestInfo:
        if self.language == "python":
            return self._extract_python(content, file_path)
        if self.language in {"typescript", "javascript"}:
            return self._extract_javascript(content, file_path)
        if self.language == "go":
            return self._extract_go(content, file_path)
        return TestInfo(file_path=file_path)

    def _is_test_file(self, filename: str) -> bool:
        if self.language == "python":
            return filename.startswith("test_") or filename.endswith("_test.py")
        if self.language in {"typescript", "javascript"}:
            return any(
                filename.endswith(suffix)
                for suffix in (
                    ".test.ts",
                    ".spec.ts",
                    ".test.tsx",
                    ".spec.tsx",
                    ".test.js",
                    ".spec.js",
                )
            )
        if self.language == "go":
            return filename.endswith("_test.go")
        return False

    def _extract_python(self, content: str, file_path: str) -> TestInfo:
        tests: list[str] = []
        fixtures: list[str] = []
        pending_fixture = False

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("@pytest.fixture"):
                pending_fixture = True
                continue

            match = re.match(r"^\s*def\s+(\w+)\s*\(", line)
            if not match:
                continue

            name = match.group(1)
            if pending_fixture:
                fixtures.append(name)
                pending_fixture = False
                continue
            if name.startswith("test_"):
                tests.append(name)
            elif name in {"setUp", "tearDown", "setup_method", "teardown_method"}:
                fixtures.append(name)

        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_javascript(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"\b(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]", content)
        fixtures = re.findall(r"\b(beforeEach|afterEach|beforeAll|afterAll)\s*\(", content)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_go(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"^\s*func\s+(Test\w+)\s*\(", content, re.MULTILINE)
        fixtures = re.findall(r"^\s*func\s+(TestMain)\s*\(", content, re.MULTILINE)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []

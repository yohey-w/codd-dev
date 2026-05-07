"""Generic stack signal detection from common project manifests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Python 3.10 fallback.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class StackDetection:
    detected_signals: list[str]
    stack_hints: list[str]


class StackDetector:
    """Detect package-manager signals without encoding product knowledge."""

    def detect(self, project_root: Path) -> StackDetection:
        root = Path(project_root)
        signals: list[str] = []
        hints: list[str] = []
        for name, extractor in _MANIFEST_EXTRACTORS.items():
            path = root / name
            if not path.is_file():
                continue
            signals.append(name)
            hints.extend(extractor(path))
        return StackDetection(detected_signals=signals, stack_hints=_unique(hints))


def _package_json(path: Path) -> Iterable[str]:
    data = _load_json_mapping(path)
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        yield from _mapping_keys(data.get(key))


def _composer_json(path: Path) -> Iterable[str]:
    data = _load_json_mapping(path)
    for key in ("require", "require-dev"):
        yield from _mapping_keys(data.get(key))


def _requirements_txt(path: Path) -> Iterable[str]:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            egg = re.search(r"(?:^|[&#])egg=([A-Za-z0-9_.-]+)", raw_line)
            if egg:
                yield egg.group(1)
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            yield match.group(1)


def _pyproject_toml(path: Path) -> Iterable[str]:
    data = _load_toml_mapping(path)
    project = data.get("project", {})
    if isinstance(project, Mapping):
        for item in _as_list(project.get("dependencies")):
            yield from _requirement_name(item)
        for group in _mapping_values(project.get("optional-dependencies")):
            for item in _as_list(group):
                yield from _requirement_name(item)
    tool = data.get("tool", {})
    if isinstance(tool, Mapping):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, Mapping):
            yield from _mapping_keys(poetry.get("dependencies"), exclude={"python"})
            yield from _mapping_keys(poetry.get("dev-dependencies"), exclude={"python"})
    for group in _mapping_values(data.get("dependency-groups")):
        for item in _as_list(group):
            yield from _requirement_name(item)


def _go_mod(path: Path) -> Iterable[str]:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "module ", ")")):
            continue
        if stripped.startswith("require "):
            stripped = stripped.removeprefix("require ").strip()
        name = stripped.split()[0] if stripped.split() else ""
        if "/" in name:
            yield name


def _cargo_toml(path: Path) -> Iterable[str]:
    data = _load_toml_mapping(path)
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        yield from _mapping_keys(data.get(key))
    workspace = data.get("workspace", {})
    if isinstance(workspace, Mapping):
        yield from _mapping_keys(workspace.get("dependencies"))


def _gemfile(path: Path) -> Iterable[str]:
    for match in re.finditer(r"\bgem\s+['\"]([^'\"]+)['\"]", path.read_text(encoding="utf-8")):
        yield match.group(1)


def _pom_xml(path: Path) -> Iterable[str]:
    for match in re.finditer(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", path.read_text(encoding="utf-8")):
        yield match.group(1)


def _load_json_mapping(path: Path) -> Mapping[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else {}


def _load_toml_mapping(path: Path) -> Mapping[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else {}


def _mapping_keys(value: Any, *, exclude: set[str] | None = None) -> Iterable[str]:
    if not isinstance(value, Mapping):
        return []
    skipped = exclude or set()
    return [str(key) for key in value if str(key) not in skipped]


def _mapping_values(value: Any) -> Iterable[Any]:
    return value.values() if isinstance(value, Mapping) else []


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _requirement_name(value: Any) -> Iterable[str]:
    if not isinstance(value, str):
        return []
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", value)
    return [match.group(1)] if match else []


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        hint = value.strip().lower()
        if not hint or hint in seen:
            continue
        seen.add(hint)
        result.append(hint)
    return result


_MANIFEST_EXTRACTORS: dict[str, Callable[[Path], Iterable[str]]] = {
    "package.json": _package_json,
    "requirements.txt": _requirements_txt,
    "pyproject.toml": _pyproject_toml,
    "go.mod": _go_mod,
    "Cargo.toml": _cargo_toml,
    "Gemfile": _gemfile,
    "pom.xml": _pom_xml,
    "composer.json": _composer_json,
}


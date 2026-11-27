"""Read a test strategy's environment declaration and check profile-owned assets."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from codd.design_consistency import StrictDesignLoader, _contract_blocks, _planned_entries
from codd.discovery import iter_source_files
from codd.frontmatter import parse_frontmatter
from codd.languages.profile import TestAssetSpec
from codd.path_safety import PathEscapeError, require_project_path, resolve_project_path
from codd.verifiable_behavior_audit import (
    _CANONICAL_VB_OUTPUT_PATH,
    canonical_vb_wave_entries,
    discover_vb_documents,
)


_FENCE = "codd-test-environment"
_FENCE_BYTES = _FENCE.encode("ascii")


@dataclass(frozen=True)
class TestEnvironmentDeclaration:
    __test__ = False

    location: str
    packages: tuple[str, ...]
    assets: tuple[str, ...]


def read_test_environment(project_root: Path, config: dict) -> TestEnvironmentDeclaration | None:
    """Detect opt-in before interpreting the settings that locate the strategy."""
    project_root = project_root.resolve()
    documents = set(discover_vb_documents(project_root))
    marked = {doc: block for doc in sorted(documents) if (block := _probe_block(project_root, doc))}
    # A pin or plan can name a Markdown document outside the default test tree.
    # Keep the opt-in probe independent of those settings, including invalid pins.
    if not marked and not any(
        _probe_block(project_root, doc) for doc in iter_source_files(project_root, extensions=(".md",))
        if doc not in documents
    ):
        return None

    waves = config.get("wave_config")
    strategy_output = next((
        output for entry in canonical_vb_wave_entries(waves, node_only=True)
        if (output := entry.get("output")) is not None
        and (not isinstance(output, str) or output.strip())
    ), None)
    coverage = config.get("test_coverage")
    pins = coverage.get("docs") if isinstance(coverage, dict) else None
    pin_paths: list[Path] = []
    errors: list[str] = []
    if coverage is not None and not isinstance(coverage, dict):
        errors.append("test_coverage must be a mapping")
    if pins is not None and not isinstance(pins, list):
        errors.append("test_coverage.docs must be a list of paths")
    if isinstance(pins, list):
        for pin in pins:
            if not isinstance(pin, str) or not pin.strip():
                errors.append(f"test_coverage.docs entry must be a nonempty path string: {pin!r}")
                continue
            try:
                pin_paths.append(require_project_path(project_root, pin, context="test_coverage.docs entry"))
            except PathEscapeError as exc:
                errors.append(str(exc))
    if pin_paths:
        documents.update(discover_vb_documents(project_root, docs=pin_paths))

    for entry in _planned_entries(config):
        node_id = entry.get("node_id") or entry.get("id")
        output = entry.get("output")
        if (
            isinstance(node_id, str) and node_id.strip().casefold().startswith("test:")
            and isinstance(output, str)
        ):
            if path := resolve_project_path(project_root, output.strip()):
                documents.add(path)

    if waves is not None and not isinstance(waves, dict):
        errors.append("wave_config must be a mapping")
    if strategy_output is not None:
        canonical = (
            resolve_project_path(project_root, strategy_output.strip())
            if isinstance(strategy_output, str) else None
        )
        if canonical is None:
            errors.append("wave_config test:test-strategy output must be a path inside the project")
    elif len(pin_paths) == 1 and not pin_paths[0].is_dir():
        canonical = pin_paths[0]
    else:
        canonical = resolve_project_path(project_root, _CANONICAL_VB_OUTPUT_PATH)
    if canonical is not None:
        documents.add(canonical)

    for doc in sorted(documents - marked.keys()):
        if block := _probe_block(project_root, doc):
            marked[doc] = block
    if not marked:
        return None
    if errors:
        raise ValueError(f"invalid test strategy configuration: {', '.join(errors)}")
    for doc, block in marked.items():
        if doc != canonical:
            location = str(block) if isinstance(block, ValueError) else f"{doc.relative_to(project_root)}:{block[0]}"
            raise ValueError(f"{location}: {_FENCE} declaration is in a noncanonical test document")
    block = marked[canonical]
    if isinstance(block, ValueError):
        raise block
    return _parse_declaration(canonical.relative_to(project_root).as_posix(), *block)


def _probe_block(project_root: Path, doc: Path) -> tuple[int, str] | ValueError | None:
    """Find the declaration and retain malformed declarations as opt-in evidence."""
    try:
        raw = doc.read_bytes()
    except OSError:
        return None
    if _FENCE_BYTES not in raw:
        return None
    path = doc.relative_to(project_root).as_posix()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = raw.count(b"\n", 0, exc.start) + 1
        return ValueError(f"{path}:{line}: cannot read {_FENCE} declaration: document is not UTF-8")
    body = parse_frontmatter(text, yaml_loader=yaml.BaseLoader).body
    offset = len(text.splitlines()) - len(body.splitlines())
    try:
        blocks = _contract_blocks(body, name=_FENCE, line_offset=offset)
    except ValueError as exc:
        return ValueError(f"{path}:{exc}")
    if not blocks:
        return None
    if len(blocks) > 1:
        return ValueError(f"{path}:{offset + blocks[1][0] + 1}: only one {_FENCE} block is allowed")
    first, last = blocks[0]
    return offset + first + 1, "\n".join(body.splitlines()[first + 1:last])


class _TestEnvironmentLoader(StrictDesignLoader):
    def construct_object(self, node, deep=False):
        try:
            return super().construct_object(node, deep=deep)
        except yaml.YAMLError:
            raise
        except Exception as exc:
            # Explicit YAML tags can raise native conversion errors in PyYAML.
            raise yaml.constructor.ConstructorError(None, None, str(exc), node.start_mark) from exc


def _parse_declaration(path: str, start_line: int, source: str) -> TestEnvironmentDeclaration | None:
    location = f"{path}:{start_line}"
    try:
        values = yaml.load(source, Loader=_TestEnvironmentLoader)
    except Exception as exc:
        mark = getattr(exc, "problem_mark", None)
        line = start_line + 1 + mark.line if mark is not None else start_line
        problem = getattr(exc, "problem", None) or str(exc)
        raise ValueError(f"{path}:{line}: invalid {_FENCE} YAML: {problem}") from exc
    if not isinstance(values, dict):
        raise ValueError(f"{location}: {_FENCE} must be a YAML mapping")
    required = {"version", "packages", "assets"}
    if unknown := values.keys() - required:
        raise ValueError(f"{location}: unknown {_FENCE} field(s): {', '.join(sorted(map(repr, unknown)))}")
    if missing := required - values.keys():
        raise ValueError(f"{location}: missing {_FENCE} field(s): {', '.join(sorted(missing))}")
    if type(values["version"]) is not int or values["version"] != 1:
        raise ValueError(f"{location}: {_FENCE} version must be the integer 1")
    for key in ("packages", "assets"):
        if not isinstance(values[key], list) or not all(
            isinstance(item, str) and item.strip() for item in values[key]
        ):
            raise ValueError(f"{location}: {key} must be a list of nonempty strings")
    if not values["packages"] and not values["assets"]:
        return None
    return TestEnvironmentDeclaration(location, tuple(values["packages"]), tuple(values["assets"]))


def _run_asset_command(
    argv: tuple[str, ...], project_root: Path, env: dict[str, str], timeout: float,
) -> tuple[str | None, str]:
    """Return a bounded diagnostic on failure and captured stdout on success."""
    try:
        result = subprocess.run(
            argv, cwd=project_root, env=env, timeout=timeout, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return f"timed out after {timeout:g}s\n{output.strip()[-2000:]}".rstrip(), ""
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not start: {exc}", ""
    if result.returncode:
        output = (result.stderr or result.stdout or "").strip()[-2000:]
        return f"exited {result.returncode}\n{output}".rstrip(), ""
    return None, (result.stdout or "").strip()[-2000:]


@dataclass(frozen=True)
class AssetCheck:
    failure: str | None = None
    next_step: str | None = None
    notice: str | None = None


def check_test_asset(
    project_root: Path, name: str, spec: TestAssetSpec, *, env: dict[str, str],
    acquire: bool, install_timeout: float,
) -> AssetCheck:
    failure, _ = _run_asset_command(spec.check, project_root, env, spec.check_timeout_seconds)
    if failure is None:
        return AssetCheck()
    if acquire:
        acquisition_failure, output = _run_asset_command(spec.acquire, project_root, env, install_timeout)
        if acquisition_failure is None:
            failure, location = _run_asset_command(spec.check, project_root, env, spec.check_timeout_seconds)
            if failure is None:
                return AssetCheck(notice=f"acquired test asset {name!r} at {location or output}")
        else:
            failure = f"{failure}\nAcquisition failed: {acquisition_failure}"
    manual = f"In the provisioned environment, run: {shlex.join(spec.acquire)}."
    if spec.manual_hint:
        manual = f"{manual} {spec.manual_hint}"
    return AssetCheck(f"test asset {name!r} check failed: {failure}", manual)

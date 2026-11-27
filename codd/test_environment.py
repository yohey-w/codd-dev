"""Read a test strategy's environment declaration and check profile-owned assets."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from codd.design_consistency import StrictDesignLoader, _contract_blocks, _planned_entries
from codd.frontmatter import parse_frontmatter
from codd.languages.profile import TestAssetSpec
from codd.path_safety import PathEscapeError, resolve_project_path
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


class MissingTestEnvironment(ValueError):
    """The generated strategy omitted its environment declaration."""


def read_test_environment(project_root: Path, config: dict) -> TestEnvironmentDeclaration | None:
    """Detect opt-in before interpreting the settings that locate the strategy."""
    project_root = project_root.resolve()
    documents = set(discover_vb_documents(project_root))
    pin_paths = _coverage_pin_paths(project_root, config)
    for path in pin_paths.values():
        try:
            documents.update(discover_vb_documents(project_root, docs=[path]))
        except (OSError, PathEscapeError, RuntimeError):
            continue

    for entry in _planned_entries(config):
        node_id = entry.get("node_id") or entry.get("id")
        if isinstance(node_id, str) and node_id.strip().casefold().startswith("test:"):
            if path := _document_path(project_root, entry.get("output"), strip=True):
                documents.add(path)

    default_doc = _document_path(project_root, _CANONICAL_VB_OUTPUT_PATH)
    if default_doc is not None:
        documents.add(default_doc)
    marked = {doc: block for doc in sorted(documents) if (block := _probe_block(project_root, doc))}
    if not marked:
        return None
    # An empty declaration asks for nothing, so it behaves exactly as no
    # declaration, whatever the surrounding configuration looks like. Malformed
    # blocks still stop (``_parse_declaration`` raises for them).
    if all(not isinstance(block, ValueError) for block in marked.values()) and all(
        _parse_declaration(doc.relative_to(project_root).as_posix(), *block) is None
        for doc, block in marked.items()
    ):
        return None

    canonical, error = resolve_test_strategy_path(project_root, config, pin_paths=pin_paths)
    if error:
        raise ValueError(error)
    for doc, block in marked.items():
        if doc != canonical:
            raise _noncanonical_declaration(doc.relative_to(project_root), block)
    block = marked[canonical]
    if isinstance(block, ValueError):
        raise block
    return _parse_declaration(canonical.relative_to(project_root).as_posix(), *block)


def _coverage_pin_paths(project_root: Path, config: dict) -> dict[int, Path]:
    coverage = config.get("test_coverage")
    pins = coverage.get("docs") if isinstance(coverage, dict) else None
    pin_paths: dict[int, Path] = {}
    if isinstance(pins, list):
        for index, pin in enumerate(pins):
            if path := _document_path(project_root, pin):
                pin_paths[index] = path
    return pin_paths


def resolve_test_strategy_path(
    project_root: Path, config: dict, *, pin_paths: dict[int, Path] | None = None,
) -> tuple[Path | None, str | None]:
    """Select the strategy without reading documents, reporting configuration errors separately."""
    waves = config.get("wave_config")
    coverage = config.get("test_coverage")
    pins = coverage.get("docs") if isinstance(coverage, dict) else None
    if pin_paths is None:
        pin_paths = _coverage_pin_paths(project_root, config)
    errors: list[str] = []
    if coverage is not None and not isinstance(coverage, dict):
        errors.append("test_coverage must be a mapping")
    if pins is not None and not isinstance(pins, list):
        errors.append("test_coverage.docs must be a list of paths")
    if isinstance(pins, list):
        for index, pin in enumerate(pins):
            if not isinstance(pin, str) or not pin.strip():
                errors.append(f"test_coverage.docs entry must be a nonempty path string: {pin!r}")
            elif index not in pin_paths:
                errors.append(f"test_coverage.docs entry must be a path inside the project: {pin!r}")
    if "wave_config" in config:
        if not isinstance(waves, dict):
            errors.append("wave_config must be a mapping")
        else:
            for wave, entries in waves.items():
                if not isinstance(entries, list):
                    errors.append(f"wave_config[{wave!r}] must be a list of entries")
                elif any(not isinstance(entry, dict) for entry in entries):
                    errors.append(f"wave_config[{wave!r}] entries must be mappings")

    strategy_output = next((
        output for entry in canonical_vb_wave_entries(waves, node_only=True)
        if (output := entry.get("output")) is not None
        and (not isinstance(output, str) or output.strip())
    ), None)
    if strategy_output is None:
        pin_path = next(iter(pin_paths.values())) if len(pin_paths) == 1 else None
        canonical = (
            pin_path if pin_path is not None and not pin_path.is_dir()
            else _document_path(project_root, _CANONICAL_VB_OUTPUT_PATH)
        )
    else:
        canonical = _document_path(project_root, strategy_output, strip=True)
        if canonical is None:
            errors.append("wave_config test:test-strategy output must be a path inside the project")
    error = f"invalid test strategy configuration: {', '.join(errors)}" if errors else None
    return canonical, error


def _noncanonical_declaration(path: Path, block: tuple[int, str] | ValueError) -> ValueError:
    location = str(block) if isinstance(block, ValueError) else f"{path}:{block[0]}"
    return ValueError(f"{location}: {_FENCE} declaration is in a noncanonical test document")


def _document_path(project_root: Path, value: object, *, strip: bool = False) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return resolve_project_path(project_root, value.strip() if strip else value)
    except RuntimeError:
        # Python versions before 3.13 can raise on a symlink loop.
        return None


def _probe_block(project_root: Path, doc: Path) -> tuple[int, str] | ValueError | None:
    """Find the declaration and retain malformed declarations as opt-in evidence."""
    try:
        if not doc.is_file():
            return None
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
    return _find_declaration_block(path, text)


def _find_declaration_block(path: str, text: str) -> tuple[int, str] | ValueError | None:
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


def parse_generated_test_environment(
    path: Path, text: str, *, canonical: bool, strategy_error: str | None,
) -> TestEnvironmentDeclaration | None:
    """Check the declaration in a generated document before it is written."""
    block = _find_declaration_block(path.as_posix(), text)
    if block is None:
        if canonical:
            raise MissingTestEnvironment(f"{path}: write exactly one {_FENCE} block")
        return None
    declaration = None if isinstance(block, ValueError) else _parse_declaration(path.as_posix(), *block)
    if strategy_error and (isinstance(block, ValueError) or declaration is not None):
        raise ValueError(strategy_error)
    if not canonical:
        raise _noncanonical_declaration(path, block)
    if isinstance(block, ValueError):
        raise block
    return declaration


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

"""Read a test strategy's environment declaration and check profile-owned assets."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from codd.design_consistency import StrictDesignLoader, _contract_blocks
from codd.frontmatter import parse_frontmatter
from codd.languages.profile import TestAssetSpec
from codd.path_safety import PathEscapeError
from codd.verifiable_behavior_audit import discover_vb_documents, is_canonical_vb_doc


_FENCE = "codd-test-environment"


@dataclass(frozen=True)
class TestEnvironmentDeclaration:
    __test__ = False

    location: str
    packages: tuple[str, ...]
    assets: tuple[str, ...]


def read_test_environment(project_root: Path, config: dict) -> TestEnvironmentDeclaration | None:
    """Read only the same canonical document that owns the VB registry."""
    project_root = project_root.resolve()
    # A coverage pin may include other evidence documents or select a strategy
    # at a custom in-project path. Fall back to the registry's ordinary discovery.
    try:
        documents = discover_vb_documents(project_root, config=config)
    except PathEscapeError:
        # Keep the coverage gate responsible for diagnosing an invalid pin.
        documents = []
    documents.extend(doc for doc in discover_vb_documents(project_root) if doc not in documents)
    canonical = next((doc for doc in documents if is_canonical_vb_doc(
        output_path=doc.relative_to(project_root).as_posix()
    )), None)
    if canonical is None:
        return None
    try:
        text = canonical.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None  # Match the VB document reader for unreadable legacy files.
    if _FENCE not in text:
        return None
    path = canonical.relative_to(project_root).as_posix()
    body = parse_frontmatter(text).body
    offset = len(text.splitlines()) - len(body.splitlines())
    try:
        blocks = _contract_blocks(body, name=_FENCE, line_offset=offset)
    except ValueError as exc:
        raise ValueError(f"{path}:{exc}") from exc
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ValueError(f"{path}:{offset + blocks[1][0] + 1}: only one {_FENCE} block is allowed")
    first, last = blocks[0]
    location = f"{path}:{offset + first + 1}"
    try:
        values = yaml.load("\n".join(body.splitlines()[first + 1:last]), Loader=StrictDesignLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = offset + first + 2 + mark.line if mark is not None else offset + first + 1
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

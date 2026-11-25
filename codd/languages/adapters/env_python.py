"""Editable-install test-environment realizer for the language profile."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from codd.project_types import EnvProvisionResult, LayoutProfile, _parse_pyproject_toml


# Keep the verifier-only toolchain aligned with the profile CI setup.
_PYTHON_TEST_TOOLCHAIN_PIN = "pytest>=8,<10"

#: Where the provisioner records its realized environment. ``.codd/**`` is
#: harness_owned (python.yaml), so this never trips the orphan/propagate gates. The
#: NAME + SHAPE are language-neutral on purpose (a plain list of dirs) — the verify
#: runner that reads it stays free of any venv/interpreter knowledge.
_ENV_PROVISION_STATE_RELPATH = ".codd/verify/exec_env.json"

#: Bounded budget for the venv build + editable install (a cold pip resolve can be
#: slow but must not hang the gate forever). Mirrors the node install preflight cap.
_ENV_PROVISION_TIMEOUT_SECONDS = 900.0


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _venv_interpreter(venv_dir: Path) -> Path:
    return _venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")


_PYTHON_ENV_PROBE = """
import importlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

package, runner, distribution, source_root, trust_declarations, *modules = sys.argv[1:]
source_root = Path(source_root)


def comes_from_source(spec):
    if spec is None:
        return False
    locations = [spec.origin] if spec.origin else list(spec.submodule_search_locations or ())
    return bool(locations) and all(Path(path).resolve().is_relative_to(source_root) for path in locations)


importlib.import_module(runner)
specs = {module: importlib.util.find_spec(module) for module in modules}
remaining = [module for module, spec in specs.items() if not comes_from_source(spec)]
if remaining:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    # An editable backend can expose a module through distribution metadata
    # even when its import location is outside the configured source root.
    allowed = {canonicalize_name(distribution)}
    if trust_declarations == "1":
        requirements = map(Requirement, importlib.metadata.requires(distribution) or ())
        allowed.update(
            canonicalize_name(req.name) for req in requirements
            if req.marker is None or req.marker.evaluate({"extra": ""})
        )
    providers = importlib.metadata.packages_distributions()
    missing = [
        module for module in remaining
        if specs[module] is None
        or allowed.isdisjoint(map(canonicalize_name, providers.get(module, ())))
    ]
    if missing:
        print(json.dumps(missing))
        sys.exit(42)
importlib.import_module(package)
"""


def _probe_python_env(
    interpreter: Path,
    profile: LayoutProfile,
    distribution: str,
    source_root: Path,
    imports: dict[str, str],
    *,
    trust_declarations: bool = True,
) -> EnvProvisionResult:
    """Prove imports resolve to the project or its directly declared dependencies."""
    next_step = "Inspect the import traceback and correct the generated source or dependency manifest."
    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv (harness), shell=False
            [
                str(interpreter), "-I", "-c", _PYTHON_ENV_PROBE,
                profile.package_name, profile.runner, distribution, str(source_root),
                "1" if trust_declarations else "0", *imports,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _env_build_error(
            f"could not run the venv import probe: {exc}",
            "Check that the venv interpreter starts and that the generated package can be imported.",
        )
    if completed.returncode == 0:
        return EnvProvisionResult(ok=True, action="up_to_date", detail="")
    if completed.returncode == 42:
        try:
            report = json.loads(completed.stdout.splitlines()[-1])
        except (IndexError, TypeError, ValueError):
            report = []
        missing = tuple(module for module in imports if isinstance(report, list) and module in report)
        if missing:
            evidence = ", ".join(f"{module} ({imports[module]})" for module in missing)
            return EnvProvisionResult(
                ok=False,
                action="failed",
                detail=f"runtime imports lack an installed direct dependency in pyproject.toml: {evidence}",
                next_step=(
                    "Declare the distributions providing these imports in pyproject.toml "
                    "[project].dependencies or correct the imports."
                ),
                undeclared_imports=missing,
            )
    return _env_build_error(
        f"venv import probe exited {completed.returncode}: "
        f"{(completed.stderr or completed.stdout or '').strip()[-2000:]}",
        next_step,
    )


def _python_runtime_imports(project_root: Path, profile: LayoutProfile) -> dict[str, str]:
    """Collect non-ambient runtime imports for ownership checks in the venv."""
    from codd.import_coherence import _collect_test_imports, _iter_py_files, _resolve_ambient_modules

    local = _resolve_ambient_modules(profile) | {profile.package_name}
    imports: dict[str, str] = {}
    for path in _iter_py_files(project_root / profile.package_root, project_root):
        try:
            tree = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            continue  # The implementation oracle diagnoses unreadable or invalid source.
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and (
                getattr(node.test, "id", None) == "TYPE_CHECKING"
                or getattr(node.test, "attr", None) == "TYPE_CHECKING"
            ):
                node.body.clear()
            if isinstance(node, ast.Try) and any(
                isinstance(handler.type, ast.Name)
                and handler.type.id in {"ImportError", "ModuleNotFoundError"}
                for handler in node.handlers
            ):
                node.body.clear()
        static, dynamic = _collect_test_imports(tree)
        for module in sorted((static | dynamic) - local):
            imports.setdefault(module, path.relative_to(project_root).as_posix())
    return imports


def _python_dependency_edit(original: str, output: str) -> str:
    """Add requirements to the manifest while preserving all unrelated text."""
    from codd.llm.plan_deriver import strip_json_fence

    new = json.loads(strip_json_fence(output))
    parsed = _parse_pyproject_toml(original)
    if parsed is None or not isinstance(parsed.get("project"), dict):
        raise ValueError("the manifest has no readable [project] table")
    project = parsed["project"]
    old = project.get("dependencies", [])
    if not isinstance(old, list) or "dependencies" in (project.get("dynamic") or ()):
        raise ValueError("the manifest does not use a static [project].dependencies array")
    if (
        not isinstance(new, list) or not new
        or any(not isinstance(req, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.<>=!~+*,\[\] -]*", req
        ) for req in new)
    ):
        raise ValueError("expected a nonempty JSON array of registry requirement strings")
    additions = list(dict.fromkeys(req for req in new if req not in old))
    if not additions:
        raise ValueError("the proposal adds no new declarations")
    # Test candidate splices semantically so similarly named keys in other tables
    # and array-like text in comments/strings cannot redirect the edit.
    expected = [*additions, *old]
    rendered = ", ".join(json.dumps(req) for req in additions)
    if "dependencies" in project:
        pattern = r"""(?m)^\s*(?:dependencies|"dependencies"|'dependencies')\s*=\s*\["""
        insertion = rendered + ", "
    else:
        pattern = r"(?m)^\s*\[project\][ \t]*(?:#[^\r\n]*)?(?=\r?$)"
        newline = "\r\n" if "\r\n" in original else "\n"
        insertion = f"{newline}dependencies = [{rendered}]"
    project["dependencies"] = expected
    for match in re.finditer(pattern, original):
        offset = match.end()
        candidate = original[:offset] + insertion + original[offset:]
        if _parse_pyproject_toml(candidate) == parsed:
            return candidate
    raise ValueError("could not safely update [project].dependencies")


def _replace_manifest(path: Path, before: str, after: str) -> bool:
    """Publish a complete manifest only while the expected contents still match."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(after.encode("utf-8"))
            handle.close()
            if path.read_bytes() != before.encode("utf-8"):
                return False
            temporary.chmod(path.stat().st_mode)
            temporary.replace(path)
            return True
        finally:
            temporary.unlink(missing_ok=True)


def _record_env_provision_state(
    project_root: Path, venv_dir: Path, *, action: str, detail: str, manifest_digest: str = "",
) -> EnvProvisionResult:
    """Write the REAL absolute bin dir + interpreter path to the state artifact."""
    bin_dir = _venv_bin_dir(venv_dir)
    interpreter = _venv_interpreter(venv_dir)
    state = {
        # A list of REAL absolute dirs for the verify executor to prepend to PATH.
        "path_prepend_dirs": [str(bin_dir.resolve())],
        "interpreter": str(interpreter.resolve()) if interpreter.exists() else str(interpreter),
    }
    if manifest_digest:
        state["manifest_digest"] = manifest_digest
    state_file = project_root / _ENV_PROVISION_STATE_RELPATH
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return EnvProvisionResult(
        ok=True, action=action, detail=detail, state_path=_ENV_PROVISION_STATE_RELPATH
    )


def _env_build_error(detail: str, next_step: str) -> EnvProvisionResult:
    return EnvProvisionResult(ok=False, action="failed", detail=detail, next_step=next_step)


def provision_python_env(
    project_root: Path, *, profile: LayoutProfile,
    propose_dependencies: Callable[[str], str] | None = None,
) -> EnvProvisionResult:
    """Build from the manifest and try one scoped declaration repair if needed."""
    from codd.path_safety import resolve_project_path

    manifest = resolve_project_path(project_root, "pyproject.toml")
    inspect_manifest = "Inspect pyproject.toml and correct the project's packaging configuration."
    if manifest is None:
        return _env_build_error("the dependency manifest escapes the project", inspect_manifest)
    try:
        original = manifest.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return _env_build_error(f"could not read the dependency manifest: {exc}", inspect_manifest)
    imports = _python_runtime_imports(project_root, profile)
    result = _build_python_env(project_root, profile, original, imports)
    if not result.undeclared_imports or propose_dependencies is None:
        return result

    prompt = (
        "Repair missing runtime dependency declarations in pyproject.toml. "
        "Identify the distribution names that actually provide the imports in the diagnostic. "
        "Do not infer a distribution name by spelling alone. Follow the project requirements. "
        "Return only a JSON array of new registry requirements (names with optional versions or extras). "
        "Return an empty array if the mapping cannot be justified. Do not edit files.\n\n"
        f"Diagnostic: {result.detail}\n\nCurrent manifest:\n{original}"
    )
    try:
        updated = _python_dependency_edit(original, propose_dependencies(prompt))
        if not _replace_manifest(manifest, original, updated):
            raise ValueError("the manifest changed while the repair was being proposed")
    except Exception as exc:  # AI transport, validation and filesystem failures are operator-actionable.
        return _env_build_error(
            f"{result.detail}. Declaration repair could not be applied: {exc}",
            result.next_step or inspect_manifest,
        )
    repaired = None
    try:
        repaired = _build_python_env(project_root, profile, updated, imports)
    finally:
        if repaired is None or not repaired.ok:
            _replace_manifest(manifest, updated, original)
    if repaired.ok:
        return repaired
    return _env_build_error(
        f"{result.detail}. Declaration repair failed to provision the environment: {repaired.detail}",
        repaired.next_step or inspect_manifest,
    )


def _build_python_env(
    project_root: Path, profile: LayoutProfile, manifest: str, imports: dict[str, str],
) -> EnvProvisionResult:
    """Install the test toolchain and editable project, then prove the result."""
    from codd.path_safety import require_project_path

    venv_dir = project_root / ".venv"
    interpreter = _venv_interpreter(venv_dir)
    source_root = require_project_path(project_root, project_root / profile.source_root)
    package_name = profile.package_name
    metadata = (_parse_pyproject_toml(manifest) or {}).get("project", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    distribution = str(metadata.get("name") or package_name)
    digest = ""
    cached_digest = ""
    state_path = project_root / _ENV_PROVISION_STATE_RELPATH
    dynamic = metadata.get("dynamic")
    declares_dependencies = bool(metadata.get("dependencies")) or (
        isinstance(dynamic, list) and "dependencies" in dynamic
    )
    if imports or declares_dependencies:
        digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cached_digest = state.get("manifest_digest", "") if isinstance(state, dict) else ""
        except (OSError, ValueError):
            pass

    current_manifest = cached_digest == digest
    # Reuse older venvs for projects with only first-party imports without
    # accepting external dependencies from unverifiable installed metadata.
    legacy_env = not cached_digest and not declares_dependencies
    if interpreter.exists() and (current_manifest or legacy_env) and _probe_python_env(
        interpreter, profile, distribution, source_root, imports, trust_declarations=current_manifest,
    ).ok:
        return _record_env_provision_state(
            project_root, venv_dir, action="up_to_date",
            detail="existing venv already imports the package + pytest",
            manifest_digest=digest if current_manifest else "",
        )

    state_path.unlink(missing_ok=True)
    venv_next_step = "Check venv support in the interpreter running CoDD and write access to .venv."
    if not interpreter.exists():
        try:
            created = subprocess.run(  # noqa: S603 — trusted argv, shell=False
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=_ENV_PROVISION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _env_build_error(f"could not create virtualenv at {venv_dir}: {exc}", venv_next_step)
        if created.returncode != 0:
            return _env_build_error(
                f"virtualenv creation exited {created.returncode}: "
                f"{(created.stderr or created.stdout or '').strip()[-2000:]}",
                venv_next_step,
            )

    install_next_step = (
        "Check the editable install diagnostic, the pyproject.toml requirements and package index access."
    )
    try:
        installed = subprocess.run(  # noqa: S603 — trusted argv, shell=False
            [
                str(interpreter), "-m", "pip", "install",
                "--disable-pip-version-check",
                _PYTHON_TEST_TOOLCHAIN_PIN, "-e", ".",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=_ENV_PROVISION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _env_build_error(
            f"editable install exceeded {_ENV_PROVISION_TIMEOUT_SECONDS:g}s",
            install_next_step,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _env_build_error(f"editable install could not run: {exc}", install_next_step)
    if installed.returncode != 0:
        return _env_build_error(
            f"editable install exited {installed.returncode}: "
            f"{(installed.stderr or installed.stdout or '').strip()[-2000:]}",
            install_next_step,
        )

    probe = _probe_python_env(interpreter, profile, distribution, source_root, imports)
    if not probe.ok:
        return probe
    return _record_env_provision_state(
        project_root, venv_dir, action="provisioned",
        detail=f"provisioned .venv (editable install + pinned pytest) for {package_name}",
        manifest_digest=digest,
    )

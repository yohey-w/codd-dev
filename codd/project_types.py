"""Central, extensible registry of CoDD project types and their capabilities.

This module is the single source of truth for "what project types does CoDD
support". Historically the supported set was a hardcoded tuple duplicated across
``required_artifacts_deriver.py``, ``requirement_completeness_auditor.py`` and
``preflight/__init__.py``; unknown configured types silently fell back to
``web`` (wrong: a ``library`` project would be handed web artifacts).

Design goals:

* **Discovery over enumeration.** Supported types are discovered by scanning the
  shipped ``required_artifacts/defaults/*.yaml`` filenames, so dropping a new
  ``<type>.yaml`` registers the type with no core edit.
* **Extensibility without forking.** A project may add its own types by placing
  ``<codd-dir>/required_artifacts_defaults/<name>.yaml`` (project-local override
  dir) or by pointing ``project.type_defaults_dir`` in ``codd.yaml`` at a
  directory of ``<name>.yaml`` profiles. Project-local types are checked first.
* **No silent web fallback.** Unknown configured types resolve to the
  conservative ``generic`` baseline (plus a caller-emitted warning), never web.
* **Capability model.** Each profile may declare a small, orthogonal
  ``capabilities:`` block that the generation pipeline (a later step) consults to
  adapt output (UI vs none, network surface, e2e modality, long-running server).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config


GENERIC_PROJECT_TYPE = "generic"
CUSTOM_PROJECT_TYPE = "custom"

# Shipped per-type artifact profiles. Filenames here define the built-in types.
SHIPPED_DEFAULTS_DIR = Path(__file__).parent / "required_artifacts" / "defaults"

# Project-local override directory (relative to a project root). A project can
# register or override a type by dropping ``<type>.yaml`` here.
PROJECT_LOCAL_DEFAULTS_SUBDIR = Path("required_artifacts_defaults")


@dataclass(frozen=True)
class ProjectCapabilities:
    """Orthogonal capability flags a profile may declare for generation.

    Defaults are deliberately conservative — they match the ``generic`` baseline
    so that a profile which omits ``capabilities:`` behaves like a plain,
    non-UI, no-network, CLI-tested, non-server project. The generation pipeline
    consults these to decide whether to emit UI/UX artifacts, route e2e tests,
    derive operations runbooks, etc.
    """

    user_interface: bool = False
    network_surface: str = "none"  # "http" | "none"
    e2e_modality: str = "cli"  # "browser" | "cli" | "device" | "none"
    long_running_service: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_interface": self.user_interface,
            "network_surface": self.network_surface,
            "e2e_modality": self.e2e_modality,
            "long_running_service": self.long_running_service,
        }


def _project_local_defaults_dir(project_root: Path | None) -> Path | None:
    """Resolve a project's local type-defaults directory, if configured/present.

    Precedence:
      1. ``project.type_defaults_dir`` in ``codd.yaml`` (explicit pointer).
      2. ``<project_root>/required_artifacts_defaults/`` (convention).
    """

    if project_root is None:
        return None
    root = Path(project_root)
    try:
        config = load_project_config(root)
    except (FileNotFoundError, ValueError):
        config = {}

    project_section = config.get("project", {})
    if isinstance(project_section, dict):
        configured = project_section.get("type_defaults_dir")
        if configured:
            configured_path = Path(str(configured))
            if not configured_path.is_absolute():
                configured_path = root / configured_path
            return configured_path

    convention = root / PROJECT_LOCAL_DEFAULTS_SUBDIR
    return convention


def _discover_types_in_dir(directory: Path | None) -> set[str]:
    if directory is None or not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.yaml") if path.stem}


def supported_project_types(project_root: Path | None = None) -> list[str]:
    """Return the sorted set of known project types.

    Discovered from shipped ``required_artifacts/defaults/*.yaml`` plus any
    project-local override profiles. ``generic`` is always included. ``custom``
    is a reserved sentinel (empty artifacts) and is intentionally NOT listed as a
    profile here; callers handle it explicitly where supported.
    """

    types: set[str] = _discover_types_in_dir(SHIPPED_DEFAULTS_DIR)
    types |= _discover_types_in_dir(_project_local_defaults_dir(project_root))
    types.add(GENERIC_PROJECT_TYPE)
    return sorted(types)


def is_known_project_type(project_type: str | None, project_root: Path | None = None) -> bool:
    if not project_type:
        return False
    return project_type.lower() in set(supported_project_types(project_root))


def resolve_project_type(
    configured: str | None,
    detected: str | None = None,
    project_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve the effective project type and a human-readable reason.

    Precedence:
      1. explicit ``configured`` when it is a known type → use it.
      2. ``configured`` set but unknown → ``generic`` + reason naming the unknown
         type (caller is expected to warn). NEVER falls back to ``web``.
      3. ``detected`` when known → use it.
      4. otherwise → ``generic``.

    Note: ``custom`` is passed through as-is (callers treat it as the
    empty-artifacts sentinel); it is not coerced to generic.
    """

    known = set(supported_project_types(project_root))
    configured_norm = (configured or "").strip().lower()
    detected_norm = (detected or "").strip().lower()

    if configured_norm == CUSTOM_PROJECT_TYPE:
        return CUSTOM_PROJECT_TYPE, "configured project_type 'custom' (empty-artifacts sentinel)"

    if configured_norm and configured_norm in known:
        return configured_norm, f"configured project_type '{configured_norm}'"

    if configured_norm and configured_norm not in known:
        reason = (
            f"project_type '{configured_norm}' is not a known profile; "
            f"using '{GENERIC_PROJECT_TYPE}' baseline. Add "
            f"codd/required_artifacts/defaults/{configured_norm}.yaml or a "
            f"project-local override to define it."
        )
        return GENERIC_PROJECT_TYPE, reason

    if detected_norm and detected_norm in known:
        return detected_norm, f"detected project_type '{detected_norm}'"

    return GENERIC_PROJECT_TYPE, f"no known project_type configured or detected; using '{GENERIC_PROJECT_TYPE}' baseline"


def _profile_path(project_type: str, project_root: Path | None) -> Path | None:
    """Return the profile YAML path for a type (project-local first, then shipped)."""

    filename = f"{project_type}.yaml"
    local_dir = _project_local_defaults_dir(project_root)
    if local_dir is not None:
        local_path = local_dir / filename
        if local_path.is_file():
            return local_path
    shipped = SHIPPED_DEFAULTS_DIR / filename
    if shipped.is_file():
        return shipped
    return None


def load_capabilities(
    project_type: str,
    project_root: Path | None = None,
) -> ProjectCapabilities:
    """Load the ``capabilities:`` block for a type with conservative defaults.

    Missing profile or missing/invalid keys fall back to the conservative
    generic capability values defined on ``ProjectCapabilities``.
    """

    path = _profile_path((project_type or "").strip().lower(), project_root)
    if path is None:
        return ProjectCapabilities()

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ProjectCapabilities()

    block = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return ProjectCapabilities()

    defaults = ProjectCapabilities()
    return ProjectCapabilities(
        user_interface=_as_bool(block.get("user_interface"), defaults.user_interface),
        network_surface=_as_choice(
            block.get("network_surface"), {"http", "none"}, defaults.network_surface
        ),
        e2e_modality=_as_choice(
            block.get("e2e_modality"),
            {"browser", "cli", "device", "none"},
            defaults.e2e_modality,
        ),
        long_running_service=_as_bool(
            block.get("long_running_service"), defaults.long_running_service
        ),
    )


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _as_choice(value: Any, allowed: set[str], fallback: str) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return fallback


# ═══════════════════════════════════════════════════════════
# Stack-general test-runner configuration (the "runnable tests" guarantee)
# ═══════════════════════════════════════════════════════════
#
# A greenfield build must be VERIFIABLE: by the time the autopilot reaches the
# verify stage, ``codd.test_detection.detect_test_command`` has to resolve a
# runnable test command for the project's stack. That detection keys off a
# stack-specific config file (Python: a ``pyproject.toml`` carrying
# ``[tool.pytest.ini_options]``; Node: a ``package.json`` ``test`` script; etc.).
# Whether that file exists is otherwise left to chance — the generating AI may or
# may not emit one (observed in the 2026-06 real-AI dogfood: one CLI emitted a
# pyproject, another did not, so verify "executed nothing" and the build could
# not be certified). This module makes the test-runner config DETERMINISTIC per
# stack, centralized here in the capability layer rather than scattered as path
# literals across the pipeline.
#
# Design:
#   * One ENSURER per language/stack, registered in ``_TEST_RUNNER_ENSURERS``.
#     Adding a stack = adding one function (node→package.json, go→go.mod, …); no
#     core edit elsewhere.
#   * The goal is RUNNABLE, not merely DETECTABLE. For Python a *bare*
#     ``pyproject.toml`` is already detectable (``detect_test_command`` rule 5
#     → pytest), yet src-layout tests still fail to import without a
#     ``pythonpath``. So each ensurer owns its own "already runnable?" check
#     against a STRONG marker for its stack — it does not defer to the coarse
#     detectability probe (that probe only guards stacks WITHOUT an ensurer, so a
#     non-native setup an AI wired up is respected).
#   * Every ensurer is IDEMPOTENT and NON-CLOBBERING: it leaves an existing
#     strong config untouched (an AI/user-provided one is authoritative), only
#     AUGMENTS a partial config file in place (preserving the rest byte-for-byte)
#     when it lacks the test-runner section, and derives every path from the
#     project's configured ``scan.source_dirs`` / ``scan.test_dirs`` — never
#     hardcoded "src"/"tests".
#   * It makes the runner RUNNABLE only. It never weakens verification: tests
#     still actually execute and still fail honestly (anti-false-green is owned by
#     the verify layer, not here).

_PYTEST_INI_SECTION = "[tool.pytest.ini_options]"
_PYPROJECT_FILENAME = "pyproject.toml"


@dataclass(frozen=True)
class EnsureTestRunnerResult:
    """Outcome of ensuring a stack's test-runner config exists.

    ``action`` is one of:
      * ``"created"``   — a new config file was written.
      * ``"augmented"`` — an existing config file gained the test-runner section.
      * ``"present"``   — a runnable test setup already existed (left untouched).
      * ``"unsupported"`` — no ensurer for this language/stack (no-op).
    """

    language: str
    action: str
    path: Path | None = None
    detail: str = ""


def _normalize_dirs(dirs: Any) -> list[str]:
    """Normalize a ``scan.*_dirs`` value to clean, slash-free relative roots."""
    if not isinstance(dirs, (list, tuple)):
        return []
    roots: list[str] = []
    for item in dirs:
        text = str(item).strip().replace("\\", "/").strip("/")
        if text and text not in roots:
            roots.append(text)
    return roots


def _toml_str_array(values: list[str]) -> str:
    """Render a list of strings as a TOML inline array (deterministic order)."""
    inner = ", ".join('"' + value.replace('"', '\\"') + '"' for value in values)
    return "[" + inner + "]"


def _render_pytest_ini_section(*, testpaths: list[str], pythonpath: list[str]) -> str:
    """Build a minimal, valid ``[tool.pytest.ini_options]`` TOML block.

    ``pythonpath`` lets pytest import a ``src``-layout package (and flat-layout
    modules via ``"."``) without an installed/editable package — the missing
    piece that makes generated tests both DETECTABLE and importable. ``addopts``
    disables the cache plugin so a read-only / sandboxed checkout never fails on
    ``.pytest_cache`` creation.
    """
    lines = [_PYTEST_INI_SECTION]
    if testpaths:
        lines.append(f"testpaths = {_toml_str_array(testpaths)}")
    if pythonpath:
        lines.append(f"pythonpath = {_toml_str_array(pythonpath)}")
    lines.append('addopts = "-p no:cacheprovider"')
    return "\n".join(lines) + "\n"


def _python_pythonpath(source_dirs: list[str]) -> list[str]:
    """Resolve the pytest ``pythonpath`` for a Python project.

    Covers every configured source root (so a ``src/`` layout is importable) and
    always appends ``"."`` so flat-layout modules at the repo root import too.
    """
    roots = list(source_dirs)
    if "." not in roots:
        roots.append(".")
    return roots


def _ensure_python_test_runner(
    project_root: Path,
    *,
    source_dirs: list[str],
    test_dirs: list[str],
) -> EnsureTestRunnerResult:
    """Ensure a RUNNABLE, pytest-detectable ``pyproject.toml`` for a Python project.

    "Runnable" means more than "detectable": the marker checked here is the
    strong ``[tool.pytest`` section (which carries ``pythonpath``), NOT mere
    presence of a ``pyproject.toml`` (a bare one is detectable as pytest but
    leaves src-layout tests unimportable). Behaviour:

      * a ``pyproject.toml`` that already carries a ``[tool.pytest`` section is
        left untouched (author/AI intent is authoritative);
      * a ``pyproject.toml`` WITHOUT one (e.g. a bare ``[project]`` table) gets
        the section APPENDED — the rest of the file is preserved byte-for-byte,
        and the added ``pythonpath`` is what finally makes the tests importable;
      * otherwise a minimal new file is written.
    """
    from codd.test_detection import _has_strong_pytest_config, detect_test_command

    # Strong, runnable pytest config already present (pytest.ini / setup.cfg /
    # pyproject [tool.pytest]) → author intent is authoritative, do nothing.
    if _has_strong_pytest_config(project_root):
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail="a strong pytest config already exists; left untouched",
        )

    # A DIFFERENT, non-pytest test command is already wired up (a Makefile
    # target, a package.json script, cargo, …). Respect the author's chosen
    # runner instead of forcing pytest on top of it. The lone exception is the
    # WEAK "a bare pyproject.toml exists" rule: that is detectable as pytest but
    # not necessarily runnable (no pythonpath), so we fall through to upgrade it.
    detected = detect_test_command(project_root)
    pyproject = project_root / _PYPROJECT_FILENAME
    bare_pyproject_only = pyproject.exists() and "[tool.pytest" not in _read_text_or_empty(pyproject)
    if detected is not None and not bare_pyproject_only:
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail=f"a non-pytest test command is already detectable ({detected}); left untouched",
        )

    section = _render_pytest_ini_section(
        testpaths=test_dirs,
        pythonpath=_python_pythonpath(source_dirs),
    )

    if pyproject.exists():
        existing = _read_text_or_empty(pyproject)
        separator = "" if existing.endswith("\n") or not existing else "\n"
        pyproject.write_text(existing + separator + "\n" + section, encoding="utf-8")
        return EnsureTestRunnerResult(
            language="python",
            action="augmented",
            path=pyproject,
            detail=f"appended {_PYTEST_INI_SECTION} to existing pyproject.toml",
        )

    pyproject.write_text(section, encoding="utf-8")
    return EnsureTestRunnerResult(
        language="python",
        action="created",
        path=pyproject,
        detail=f"wrote pyproject.toml with {_PYTEST_INI_SECTION}",
    )


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Language → ensurer. Add a stack here (and only here) to make its greenfield
# builds deterministically verifiable: node → a package.json test script, go →
# go.mod, rust → Cargo.toml, etc. The dispatch, non-clobber guard and CLI-
# agnostic wiring all stay unchanged.
_TestRunnerEnsurer = Callable[..., EnsureTestRunnerResult]
_TEST_RUNNER_ENSURERS: dict[str, _TestRunnerEnsurer] = {
    "python": _ensure_python_test_runner,
}


def supported_test_runner_languages() -> list[str]:
    """Languages for which greenfield can deterministically ensure a test runner."""
    return sorted(_TEST_RUNNER_ENSURERS)


def ensure_test_runner_config(
    project_root: Path | str,
    *,
    language: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
) -> EnsureTestRunnerResult:
    """Guarantee a RUNNABLE, detectable test setup for ``language``'s stack.

    Stack-general entry point. For a language WITH a registered ensurer, the
    ensurer owns the present/augment/create decision (it checks its own STRONG
    marker — e.g. a ``[tool.pytest`` section, not mere ``pyproject.toml``
    presence — so it can upgrade a bare config that is detectable but not
    runnable). For a language WITHOUT an ensurer, this returns an
    ``"unsupported"`` no-op UNLESS a test command is already detectable, in which
    case a provided (possibly non-native) setup is respected.

    Either way an AI/user-provided setup is never clobbered, and the verify layer
    remains the authority that refuses to certify an unexecuted build. All path
    inputs default to the conventional roots only when not provided; normally the
    caller passes the project's configured ``scan.source_dirs`` /
    ``scan.test_dirs`` so nothing is hardcoded.
    """
    root = Path(project_root)
    lang = (language or "").strip().lower()

    ensurer = _TEST_RUNNER_ENSURERS.get(lang)
    if ensurer is None:
        # No native ensurer for this stack. Respect any test command an AI/user
        # already wired up (stack-agnostic), otherwise it is an advisory no-op:
        # the verify honesty gate still refuses to certify an unexecuted build.
        from codd.test_detection import detect_test_command

        if detect_test_command(root) is not None:
            return EnsureTestRunnerResult(
                language=lang or "unknown",
                action="present",
                detail="a test command is already detectable; left untouched",
            )
        return EnsureTestRunnerResult(
            language=lang or "unknown",
            action="unsupported",
            detail=(
                f"no deterministic test-runner ensurer for language {lang!r}; "
                "relying on the generated project to provide a detectable setup"
            ),
        )

    source_roots = _normalize_dirs(source_dirs) or ["src"]
    test_roots = _normalize_dirs(test_dirs) or ["tests"]
    return ensurer(root, source_dirs=source_roots, test_dirs=test_roots)

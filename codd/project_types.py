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
# Stack layout profiles (the harness OWNS repo topology / module resolution)
# ═══════════════════════════════════════════════════════════
#
# Model-independence principle (A-core): the harness REMOVES the degrees of
# freedom the model should NOT vary. The repository TOPOLOGY and the
# MODULE-RESOLUTION contract are harness-owned; the model only fills the
# CONTENTS (domain logic, behavior, test behavior, messages). A greenfield build
# that lets the model invent project structure produces source + tests that
# DISAGREE on package/import context (observed 2026-06 cross-vendor: source uses
# package-relative ``from .todo_store import X`` while tests flat-import
# ``import todo_store`` — a real import failure masked only by an accidental
# ``pythonpath="."``, an environment-dependent FALSE GREEN).
#
# A ``LayoutProfile`` is the single, stack-specific declaration of that
# topology: the package name (derived deterministically from the project name),
# the source/package/test roots (derived from ``scan.*_dirs``), the test runner,
# the install mode, and the test IMPORT POLICY the coherence gate enforces. One
# profile per stack, centralized here in the registry — Python is implemented
# now; node/go/rust are future profiles added as one entry each, with NO
# scattered "src"/"tests"/"<package>" literals anywhere in the pipeline.

_VALID_TEST_IMPORT_POLICIES = {"package_absolute", "flat"}


@dataclass(frozen=True)
class LayoutProfile:
    """Harness-owned repository topology + module-resolution contract for a stack.

    Every path is DERIVED (from the project name + ``scan.source_dirs`` /
    ``scan.test_dirs``), never hardcoded. The generation pipeline writes INTO
    ``package_root`` so source lands inside the package; the coherence gate
    enforces ``test_import_policy`` (Python: ``package_absolute`` — a test must
    import a generated source module as ``from <package_name>.<mod> import ...``,
    never by bare basename); the test-runner ensurer + scaffold realize
    ``runner`` / ``install_mode`` so tests run against the REAL installed package
    (anti-false-green: an accidental flat import no longer resolves).
    """

    language: str
    package_name: str
    source_root: str
    package_root: str
    test_root: str
    runner: str = "pytest"
    install_mode: str = "editable"  # "editable" | "none"
    test_import_policy: str = "package_absolute"  # "package_absolute" | "flat"
    requires_package_init: bool = True
    requires_test_init: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "package_name": self.package_name,
            "source_root": self.source_root,
            "package_root": self.package_root,
            "test_root": self.test_root,
            "runner": self.runner,
            "install_mode": self.install_mode,
            "test_import_policy": self.test_import_policy,
        }


def normalize_package_name(project_name: str | None, *, fallback: str = "app") -> str:
    """Derive a valid Python package identifier from a project name.

    ``todo-cli`` → ``todo_cli``; ``2048 Game`` → ``_2048_game``; empty/garbage →
    ``fallback``. Deterministic and pure so the same project name always yields
    the same package, which is what makes source + tests + pyproject agree.
    """
    raw = str(project_name or "").strip().lower()
    chars: list[str] = []
    for ch in raw:
        chars.append(ch if (ch.isalnum() or ch == "_") else "_")
    collapsed = "".join(chars).strip("_")
    while "__" in collapsed:
        collapsed = collapsed.replace("__", "_")
    if not collapsed:
        return fallback
    if collapsed[0].isdigit():
        collapsed = "_" + collapsed
    return collapsed


def _first_clean_dir(dirs: Any, default: str) -> str:
    """First normalized (slash-free) root from a ``scan.*_dirs`` value, or default."""
    normalized = _normalize_dirs(dirs)
    return normalized[0] if normalized else default


def _python_layout_profile(
    *,
    project_name: str | None,
    source_dirs: Any,
    test_dirs: Any,
) -> LayoutProfile:
    """Python ``python_src_package`` profile: a src-layout, installed package.

    * ``package_name`` derives from the project name (normalized identifier).
    * ``source_root`` from ``scan.source_dirs`` (default ``src``).
    * ``package_root`` = ``<source_root>/<package_name>`` — source lives in a
      named package, so package-absolute imports work both in tests (installed)
      and at runtime (``python -m <package_name>``).
    * ``test_root`` from ``scan.test_dirs`` (default ``tests``).
    * runner=pytest, install_mode=editable, policy=package_absolute.
    """
    package_name = normalize_package_name(project_name)
    source_root = _first_clean_dir(source_dirs, "src")
    test_root = _first_clean_dir(test_dirs, "tests")
    return LayoutProfile(
        language="python",
        package_name=package_name,
        source_root=source_root,
        package_root=f"{source_root}/{package_name}",
        test_root=test_root,
        runner="pytest",
        install_mode="editable",
        test_import_policy="package_absolute",
        requires_package_init=True,
        requires_test_init=True,
    )


# Language → layout-profile builder. ONE entry per stack (the only place a stack
# registers its topology). node/go/rust extend here with a single function each.
_LayoutProfileBuilder = Callable[..., LayoutProfile]
_LAYOUT_PROFILE_BUILDERS: dict[str, _LayoutProfileBuilder] = {
    "python": _python_layout_profile,
}


def supported_layout_profile_languages() -> list[str]:
    """Languages with a harness-owned layout profile (deterministic topology)."""
    return sorted(_LAYOUT_PROFILE_BUILDERS)


def resolve_layout_profile(
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
) -> LayoutProfile | None:
    """Resolve the :class:`LayoutProfile` for a stack, or ``None`` if unsupported.

    Stack-general dispatch through :data:`_LAYOUT_PROFILE_BUILDERS`. Every path
    is derived from ``project_name`` + the configured ``scan.*_dirs`` — there
    are NO hardcoded ``src``/``tests``/``<package>`` literals outside the
    per-stack builder's documented defaults.
    """
    builder = _LAYOUT_PROFILE_BUILDERS.get((language or "").strip().lower())
    if builder is None:
        return None
    return builder(project_name=project_name, source_dirs=source_dirs, test_dirs=test_dirs)


# ═══════════════════════════════════════════════════════════
# Deterministic scaffold (harness creates topology; model fills contents)
# ═══════════════════════════════════════════════════════════
#
# The scaffold realizes a :class:`LayoutProfile` on disk: pyproject (package
# metadata + pytest config, NO ``pythonpath="."``), ``<package_root>/__init__``,
# ``<package_root>/__main__``, and the test ``__init__`` the profile requires.
# It is CREATE-ONLY and IDEMPOTENT: it never moves or rewrites model-authored
# files (that would violate "harness owns structure, not contents" and could
# corrupt author intent — an EXISTING incoherent build must instead FAIL the
# coherence gate honestly and be REGENERATED, not silently healed). A valid
# Claude-consistent layout is therefore left byte-for-byte alone; a second call
# is a no-op.

_PYTEST_INI_SECTION = "[tool.pytest.ini_options]"
_PYPROJECT_FILENAME = "pyproject.toml"

#: Package-init marker so a created ``__init__.py`` is recognised as scaffold
#: (idempotent) and never an author file we might clobber on re-augment.
_SCAFFOLD_INIT_DOC = '"""Package root (scaffolded by codd greenfield)."""\n'


@dataclass(frozen=True)
class ScaffoldResult:
    """Outcome of realizing a layout profile on disk."""

    language: str
    created: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    detail: str = ""


def scaffold_layout(
    project_root: Path | str,
    profile: LayoutProfile,
) -> ScaffoldResult:
    """Create the profile's topology (create-only, idempotent, non-clobbering).

    Returns the relative paths created vs. skipped (already present). Only the
    Python profile is realized today; an unknown ``profile.language`` is a no-op.
    """
    if profile.language == "python":
        return _scaffold_python(Path(project_root), profile)
    return ScaffoldResult(language=profile.language, detail="no scaffolder for stack")


def _scaffold_python(project_root: Path, profile: LayoutProfile) -> ScaffoldResult:
    created: list[str] = []
    skipped: list[str] = []

    def _ensure_file(rel: str, content: str) -> None:
        target = project_root / rel
        if target.exists():
            skipped.append(rel)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(rel)

    package_dir = profile.package_root
    # __init__ makes <source_root>/<package_name> an importable package; __main__
    # gives ``python -m <package_name>`` an entry point. Both package-relative.
    if profile.requires_package_init:
        _ensure_file(
            f"{package_dir}/__init__.py",
            _SCAFFOLD_INIT_DOC,
        )
        _ensure_file(
            f"{package_dir}/__main__.py",
            (
                '"""Console entry point (scaffolded by codd greenfield)."""\n\n'
                "def main() -> int:\n"
                "    raise NotImplementedError\n\n\n"
                'if __name__ == "__main__":\n'
                "    raise SystemExit(main())\n"
            ),
        )
    if profile.requires_test_init:
        _ensure_file(f"{profile.test_root}/__init__.py", "")

    runner_result = _ensure_python_test_runner(
        project_root,
        profile=profile,
    )
    if runner_result.action in ("created", "augmented") and runner_result.path is not None:
        created.append(_PYPROJECT_FILENAME)
    elif runner_result.action == "present":
        skipped.append(_PYPROJECT_FILENAME)

    detail = (
        f"package={profile.package_root}, test_root={profile.test_root}, "
        f"runner={runner_result.action}"
    )
    return ScaffoldResult(
        language="python",
        created=tuple(created),
        skipped=tuple(skipped),
        detail=detail,
    )


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


def _render_pytest_ini_section(*, testpaths: list[str], source_root: str) -> str:
    """Build a minimal, valid ``[tool.pytest.ini_options]`` TOML block.

    ANTI-FALSE-GREEN (A-core): ``pythonpath`` is the SOURCE ROOT ONLY — never
    ``"."``. The prior fix put ``pythonpath = [<src>, "."]`` so tests ran without
    an installed package, but ``"."`` (plus a flat ``src`` layout) let a test
    resolve a source module by BARE BASENAME (``import todo_store``) even when the
    source uses package-relative imports — an environment-dependent FALSE GREEN.
    With the harness-owned src-layout PACKAGE (``<source_root>/<package_name>/``),
    a source-root-only ``pythonpath`` makes the package-absolute import
    ``from <package_name>.<mod> import ...`` resolve while a bare ``import <mod>``
    does NOT (there is no top-level ``<source_root>/<mod>.py``). Combined with
    ``--import-mode=importlib`` (no ``sys.path[0]`` insertion of the test's own
    dir), an accidental flat import stays a real failure. The package metadata
    (see :func:`_python_editable_metadata`) additionally makes ``pip install -e .``
    work for real deployment, but is not required for tests to run. ``addopts``
    also disables the cache plugin so a read-only checkout never fails on
    ``.pytest_cache``.
    """
    lines = [_PYTEST_INI_SECTION]
    if testpaths:
        lines.append(f"testpaths = {_toml_str_array(testpaths)}")
    clean_root = source_root.strip().replace("\\", "/").strip("/")
    if clean_root:
        lines.append(f"pythonpath = {_toml_str_array([clean_root])}")
    lines.append('addopts = "-p no:cacheprovider --import-mode=importlib"')
    return "\n".join(lines) + "\n"


def _python_editable_metadata(profile: LayoutProfile) -> str:
    """A ``[project]`` + setuptools src-layout table for an editable install.

    Makes ``pip install -e .`` install ``<package_name>`` from
    ``<source_root>``, so tests import the REAL package (package-absolute) rather
    than relying on PYTHONPATH. Only written when the file does not already carry
    a ``[project]`` table (we never clobber author metadata).
    """
    pkg = profile.package_name
    src = profile.source_root
    return (
        "[build-system]\n"
        'requires = ["setuptools>=61"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{pkg}"\n'
        'version = "0.0.0"\n\n'
        "[tool.setuptools.packages.find]\n"
        f'where = ["{src}"]\n'
    )


def _ensure_python_test_runner(
    project_root: Path,
    *,
    profile: LayoutProfile,
) -> EnsureTestRunnerResult:
    """Ensure a RUNNABLE, pytest-detectable ``pyproject.toml`` for a Python project.

    A-core: "runnable" now means "the installed package is importable and tests
    run against it" — NOT "src is on PYTHONPATH". The emitted pyproject carries
    (a) package metadata for an editable install and (b) a ``[tool.pytest``
    section with ``--import-mode=importlib`` and NO ``pythonpath``. Behaviour:

      * a ``pyproject.toml`` already carrying a ``[tool.pytest`` section is left
        untouched (author/AI intent is authoritative);
      * a ``pyproject.toml`` WITHOUT one (e.g. a bare ``[project]`` table) gets
        the pytest section APPENDED — the rest preserved byte-for-byte; package
        metadata is only added when no ``[project]`` table exists;
      * otherwise a minimal new file (metadata + pytest section) is written.
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
    # not runnable as an installed package, so we fall through to upgrade it.
    detected = detect_test_command(project_root)
    pyproject = project_root / _PYPROJECT_FILENAME
    pyproject_text = _read_text_or_empty(pyproject) if pyproject.exists() else ""
    bare_pyproject_only = pyproject.exists() and "[tool.pytest" not in pyproject_text
    if detected is not None and not bare_pyproject_only:
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail=f"a non-pytest test command is already detectable ({detected}); left untouched",
        )

    section = _render_pytest_ini_section(
        testpaths=[profile.test_root], source_root=profile.source_root
    )

    if pyproject.exists():
        existing = pyproject_text
        addition = section
        # Add package metadata only when the file has no [project] table, so an
        # editable install can resolve the package; never clobber author metadata.
        if "[project]" not in existing and "[build-system]" not in existing:
            addition = _python_editable_metadata(profile) + "\n" + section
        separator = "" if existing.endswith("\n") or not existing else "\n"
        pyproject.write_text(existing + separator + "\n" + addition, encoding="utf-8")
        return EnsureTestRunnerResult(
            language="python",
            action="augmented",
            path=pyproject,
            detail=f"appended {_PYTEST_INI_SECTION} (importlib mode) to existing pyproject.toml",
        )

    pyproject.write_text(
        _python_editable_metadata(profile) + "\n" + section,
        encoding="utf-8",
    )
    return EnsureTestRunnerResult(
        language="python",
        action="created",
        path=pyproject,
        detail=f"wrote pyproject.toml (editable package + {_PYTEST_INI_SECTION}, importlib mode)",
    )


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Language → ensurer. Add a stack here (and only here) to make its greenfield
# builds deterministically verifiable: node → a package.json test script, go →
# go.mod, rust → Cargo.toml, etc. Each ensurer drives off the resolved
# :class:`LayoutProfile` for its stack, so topology lives in ONE place.
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
    project_name: str | None = None,
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
    remains the authority that refuses to certify an unexecuted build. All paths
    derive from the resolved :class:`LayoutProfile` (``project_name`` +
    ``scan.source_dirs`` / ``scan.test_dirs``); nothing is hardcoded.
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

    profile = resolve_layout_profile(
        language=lang,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
    )
    if profile is not None:
        return ensurer(root, profile=profile)

    # Fallback (no profile builder for an ensurer-having stack — shouldn't
    # happen, but keep the path total): synthesize a minimal profile from dirs.
    source_root = _first_clean_dir(source_dirs, "src")
    test_root = _first_clean_dir(test_dirs, "tests")
    package_name = normalize_package_name(project_name)
    fallback_profile = LayoutProfile(
        language=lang,
        package_name=package_name,
        source_root=source_root,
        package_root=f"{source_root}/{package_name}",
        test_root=test_root,
    )
    return ensurer(root, profile=fallback_profile)

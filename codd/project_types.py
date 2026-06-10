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

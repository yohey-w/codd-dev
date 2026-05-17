"""Discover bundled or explicitly provided CoDD skill sources."""

from __future__ import annotations

from importlib import resources
from pathlib import Path


class SkillNotFoundError(ValueError):
    """Raised when a skill source cannot be found."""


def find_skill_source(skill_name: str, skill_dir: str | Path | None = None) -> Path:
    """Find a skill source directory.

    Search order:
    1. Explicit --dir path.
    2. Bundled wheel resources under codd._skills.
    3. Editable checkout fallback at repository top-level skills/.
    """
    name = _validate_skill_name(skill_name)

    if skill_dir is not None:
        source = _resolve_explicit_source(name, Path(skill_dir).expanduser())
        if source is not None:
            return source
        raise SkillNotFoundError(f"Skill {name!r} was not found under --dir {skill_dir!s}")

    source = _find_bundled_skill(name)
    if source is not None:
        return source

    source = _find_editable_skill(name)
    if source is not None:
        return source

    raise SkillNotFoundError(
        f"Skill {name!r} was not found. Use --dir to install from an explicit skill directory."
    )


def _validate_skill_name(skill_name: str) -> str:
    name = str(skill_name).strip()
    if not name:
        raise SkillNotFoundError("Skill name must be non-empty.")
    if Path(name).name != name or name in {".", ".."}:
        raise SkillNotFoundError(f"Invalid skill name: {skill_name!r}")
    return name


def _resolve_explicit_source(skill_name: str, root: Path) -> Path | None:
    root = root.resolve()
    if _is_skill_dir(root):
        return root
    candidate = root / skill_name
    if _is_skill_dir(candidate):
        return candidate.resolve()
    return None


def _find_bundled_skill(skill_name: str) -> Path | None:
    try:
        root = resources.files("codd._skills")
    except (AttributeError, ModuleNotFoundError, TypeError):
        return None

    candidate = root.joinpath(skill_name)
    try:
        if not candidate.is_dir() or not candidate.joinpath("SKILL.md").is_file():
            return None
    except OSError:
        return None

    return _traversable_to_path(candidate)


def _find_editable_skill(skill_name: str) -> Path | None:
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "skills" / skill_name
    return candidate.resolve() if _is_skill_dir(candidate) else None


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _traversable_to_path(candidate: object) -> Path | None:
    if isinstance(candidate, Path):
        return candidate.resolve()
    try:
        path = Path(candidate)  # type: ignore[arg-type]
    except TypeError:
        return None
    return path.resolve() if path.exists() else None

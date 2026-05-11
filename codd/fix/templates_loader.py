"""Template loading helpers for codd.fix prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def template_dir() -> Path:
    return _TEMPLATE_DIR


@lru_cache(maxsize=32)
def _read_packaged(name: str) -> str:
    path = _TEMPLATE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"codd.fix template not found: {name}")
    return path.read_text(encoding="utf-8")


def load_template(name: str, *, override: Path | None = None) -> str:
    """Return the template text, allowing an explicit override for tests."""
    if override is not None:
        return Path(override).read_text(encoding="utf-8")
    return _read_packaged(name)


def render_template(template: str, /, **kwargs: object) -> str:
    """Substitute ``{key}`` placeholders without disturbing literal braces.

    str.format() is unsafe for these templates because the prompts contain
    JSON examples with literal `{` / `}`. Plain string replacement keeps
    those examples intact.
    """
    rendered = template
    for key, value in kwargs.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered

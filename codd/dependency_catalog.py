"""Dependency manifest catalogs for brownfield extraction."""

from __future__ import annotations


_PYTHON_FRAMEWORKS = {
    "aiohttp": "aiohttp",
    "django": "Django",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "starlette": "Starlette",
    "tornado": "Tornado",
}

_PYTHON_ORMS = {
    "django": "Django ORM",
    "peewee": "Peewee",
    "prisma": "Prisma",
    "sqlalchemy": "SQLAlchemy",
    "sqlmodel": "SQLModel",
    "tortoise-orm": "Tortoise ORM",
}

_PYTHON_TEST_FRAMEWORKS = {
    "nose": "nose2",
    "pytest": "pytest",
    "unittest": "unittest",
}


def detect_python_manifest_patterns(content: str) -> tuple[list[str], str, str]:
    """Return framework, ORM, and test framework names found in manifest text."""
    normalized = content.lower()
    frameworks = [
        name for key, name in _PYTHON_FRAMEWORKS.items()
        if key in normalized
    ]
    orm = next(
        (name for key, name in _PYTHON_ORMS.items() if key in normalized),
        "",
    )
    test_framework = next(
        (name for key, name in _PYTHON_TEST_FRAMEWORKS.items() if key in normalized),
        "",
    )
    return frameworks, orm, test_framework

"""Pytest launch command from the Python profile."""

from __future__ import annotations

from pathlib import Path

from codd.languages.profile import LanguageProfile


def pytest_launcher(
    profile: LanguageProfile | None = None, *, interpreter: str | None = None,
) -> tuple[str, ...]:
    """Resolve the Python runner and optionally pin a direct module launch."""
    launcher = profile.test_runner_argv("pytest") if profile is not None else ()
    if not launcher:
        from codd.languages.registry import default_registry

        launcher = default_registry.resolve("python").test_runner_argv("pytest")
    if not launcher:
        raise ValueError("The Python profile does not declare a pytest launcher")
    if interpreter and launcher[-2:] == ("-m", "pytest") and (
        launcher[1] == "-m" or Path(launcher[0]).name.startswith(("python", "pypy"))
    ):
        return (interpreter, *launcher[1:])
    return launcher

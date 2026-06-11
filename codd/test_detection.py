"""Single source of truth for "what command runs this project's tests".

Before this module existed, two independent detectors answered the same
question with different heuristics and different precedence:

* ``codd/fixer.py`` ``_detect_test_command`` — package.json scripts first
  (``test:unit`` > ``test`` > ``test:e2e``), then bare ``pyproject.toml``
  → pytest, then ``Makefile`` with a ``test:`` target.
* ``codd/watch/test_runner.py`` ``detect_test_framework`` — explicit config
  first, then ``pyproject.toml`` → pytest BEFORE package.json (vitest/jest
  by dependency sniffing), then ``*.bats``, then ``go.mod``.

If ``codd fix`` and the watch layer disagree on the project's test command,
the wrong suite can gate a change. This module is the union of both
heuristic sets with one documented precedence.

Precedence (first hit wins):

1. Explicit config ``fix.test_command``   (codd.yaml — author intent)
2. Explicit config ``verify.test_command`` (codd.yaml — author intent)
3. Strong pytest configuration: ``pytest.ini``, or ``pyproject.toml``
   containing ``[tool.pytest``, or ``setup.cfg`` containing
   ``[tool:pytest]``  → ``pytest --tb=short -q``
4. ``package.json`` scripts: ``test:unit`` > ``test`` > ``test:e2e``
   → ``npm run <key>``  (unit preferred — E2E needs a full-stack
   environment that is usually unavailable locally)
5. Bare ``pyproject.toml``  → ``pytest --tb=short -q``  (weak signal,
   kept from the fixer heuristics; ranked below explicit npm scripts)
6. ``package.json`` declaring vitest / jest without a test script
   → ``npx vitest run`` / ``npx jest``  (from the watch-layer framework
   sniffing, expressed as a run-all command)
7. ``Cargo.toml``  → ``cargo test``
8. ``go.mod``      → ``go test ./...``
9. Any ``*.bats`` file → ``bats -r .``  (bats-core recursive run-all)
10. ``Makefile`` with a ``test:`` target → ``make test``  (most generic
    wrapper; last so language-native runners win)

Returns ``None`` when nothing is detected — callers MUST treat that as
"unverified", never as "tests passed".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["detect_test_command"]


def detect_test_command(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Detect the project's test command. Explicit config beats detection."""
    root = Path(project_root)

    explicit = _explicit_test_command(config)
    if explicit:
        return explicit

    # 3. Strong pytest configuration
    if _has_strong_pytest_config(root):
        return "pytest --tb=short -q"

    # 4. package.json test scripts (author-declared)
    scripts = _package_json_scripts(root)
    for key in ("test:unit", "test", "test:e2e"):
        if key in scripts:
            return f"npm run {key}"

    # 5. Bare pyproject.toml → pytest (weak but long-standing fixer rule)
    if (root / "pyproject.toml").exists():
        return "pytest --tb=short -q"

    # 6. JS test framework declared as a dependency, no test script
    pkg_text = _read_text(root / "package.json")
    if pkg_text:
        if "vitest" in pkg_text:
            return "npx vitest run"
        if "jest" in pkg_text:
            return "npx jest"

    # 7-8. Language-native runners
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."

    # 9. bats suites
    if any(root.glob("**/*.bats")):
        return "bats -r ."

    # 10. Makefile test target (generic wrapper, last)
    makefile_text = _read_text(root / "Makefile")
    if makefile_text and "test:" in makefile_text:
        return "make test"

    return None


def _explicit_test_command(config: dict[str, Any] | None) -> str | None:
    """Read ``fix.test_command`` then ``verify.test_command`` from config."""
    if not isinstance(config, dict):
        return None
    for section_name in ("fix", "verify"):
        section = config.get(section_name)
        if isinstance(section, dict):
            value = section.get("test_command")
            if isinstance(value, str) and value.strip():
                return value
    return None


def _has_strong_pytest_config(root: Path) -> bool:
    if (root / "pytest.ini").exists():
        return True
    pyproject = _read_text(root / "pyproject.toml")
    if pyproject and "[tool.pytest" in pyproject:
        return True
    setup_cfg = _read_text(root / "setup.cfg")
    if setup_cfg and "[tool:pytest]" in setup_cfg:
        return True
    return False


def _package_json_scripts(root: Path) -> dict[str, Any]:
    text = _read_text(root / "package.json")
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

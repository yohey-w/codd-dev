"""Regression tests for cmd_471 Issue #23 — codd/fix/templates wheel packaging.

Background: v2.16.0 (cmd_468) introduced ``codd/fix/templates/*.txt`` prompt
files but the hatch include glob in ``pyproject.toml`` only listed ``*.py``,
``*.md`` and ``*.yaml`` under ``codd/**``. Built wheels therefore did not
contain the .txt files and any user running ``codd fix [PHENOMENON]`` after
``pip install codd-dev`` got ``FileNotFoundError: codd.fix template not
found: design_update.txt``.

These tests pin the v2.17.1+ behaviour: every shipped template file is
both present in the source tree and loadable via ``templates_loader``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.fix.templates_loader import load_template, template_dir

EXPECTED_TEMPLATES = (
    "clarification_question.txt",
    "design_update.txt",
    "phenomenon_parse.txt",
    "risk_assessment.txt",
)


def test_template_directory_exists():
    assert template_dir().is_dir(), (
        f"codd/fix/templates/ must exist at {template_dir()}"
    )


@pytest.mark.parametrize("name", EXPECTED_TEMPLATES)
def test_each_template_is_loadable(name):
    text = load_template(name)
    assert text.strip(), f"template {name!r} is empty or whitespace-only"


def test_pyproject_include_covers_txt_under_codd():
    """The hatch include glob must cover .txt files under codd/.

    Without ``codd/**/*.txt`` (or an equivalent explicit listing of the
    fix/templates directory), the wheel build silently drops the prompt
    files. The full integration verification — actually building a wheel
    and inspecting its contents — is performed in CI/release scripts; this
    unit test just guards the include declaration so regressions are
    caught at PR time.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert (
        '"codd/**/*.txt"' in text
        or "'codd/**/*.txt'" in text
        or '"codd/fix/templates/' in text
        or "'codd/fix/templates/" in text
    ), (
        "pyproject.toml [tool.hatch.build] include must list codd/**/*.txt "
        "(or an explicit codd/fix/templates/ pattern) so that prompt "
        "templates ship in the wheel — see cmd_471 Issue #23."
    )


def test_all_expected_templates_live_in_codd_package():
    """Each expected template must physically exist under codd/fix/templates/.

    This catches the case where a template is renamed or moved without
    updating EXPECTED_TEMPLATES or the loader callers — equally fatal at
    runtime, equally invisible until a user hits the missing path.
    """
    base = template_dir()
    missing = [name for name in EXPECTED_TEMPLATES if not (base / name).is_file()]
    assert not missing, f"missing template files: {missing}"

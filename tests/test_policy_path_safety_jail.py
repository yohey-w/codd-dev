"""Path-escape jail coverage for ``codd policy`` source reads.

``_run_policy_oss`` reads source files and scans their contents against policy
rules. Two user-controllable path surfaces feed that evidence:

* ``scan.source_dirs`` (codd.yaml), walked by ``_collect_source_files``;
* the ``changed_files`` argument, each entry joined under the project root.

A path that is absolute, ``../`` traversal, or an in-root symlink whose target
escapes the project root must NOT be read — otherwise an out-of-root file is
scanned and its matches reported as in-project policy violations (a path-escape
false result). These tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject, plus an in-root
regression (anti-false-red). Escapes are *excluded* (skipped, with a stderr
diagnostic) rather than crashed on. ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.policy import run_policy

# A pattern the FORBIDDEN rule below matches. If an out-of-root file is scanned,
# this token surfaces as a violation.
SECRET = "FORBIDDEN_SECRET_TOKEN"

_FORBIDDEN_CONFIG = {
    "scan": {"source_dirs": ["src"]},
    "policies": [
        {
            "id": "no-secret",
            "description": "secret token banned",
            "severity": "CRITICAL",
            "kind": "forbidden",
            "pattern": SECRET,
            "glob": "*.py",
        }
    ],
}


def _make_project(tmp_path: Path, config: dict) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return project


def _seed_outside_source(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    leak = outside / "leak.py"
    leak.write_text(f"value = '{SECRET}'\n", encoding="utf-8")
    return outside


def _violation_ids(result) -> set[str]:
    return {v.rule_id for v in result.violations}


# --- source_dirs escape -------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_source_dir_out_of_root_not_scanned(tmp_path, escape):
    outside = _seed_outside_source(tmp_path)
    raw = "../outside" if escape == "parent" else str(outside)
    config = {**_FORBIDDEN_CONFIG, "scan": {"source_dirs": [raw]}}
    project = _make_project(tmp_path, config)

    result = run_policy(project)

    assert "no-secret" not in _violation_ids(result), (
        "out-of-root source dir was scanned and reported a policy violation"
    )
    assert result.files_checked == 0


def test_source_dir_in_root_symlink_escape_not_scanned(tmp_path):
    outside = _seed_outside_source(tmp_path)
    config = {**_FORBIDDEN_CONFIG, "scan": {"source_dirs": ["linked_src"]}}
    project = _make_project(tmp_path, config)
    (project / "linked_src").symlink_to(outside, target_is_directory=True)

    result = run_policy(project)

    assert "no-secret" not in _violation_ids(result), (
        "in-root symlink escaping the root was scanned for policy violations"
    )


# --- changed_files escape -----------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_changed_file_out_of_root_not_scanned(tmp_path, escape):
    outside = _seed_outside_source(tmp_path)
    project = _make_project(tmp_path, _FORBIDDEN_CONFIG)
    raw = "../outside/leak.py" if escape == "parent" else str(outside / "leak.py")

    result = run_policy(project, changed_files=[raw])

    assert "no-secret" not in _violation_ids(result), (
        "out-of-root changed_file was scanned and reported a policy violation"
    )
    assert result.files_checked == 0


def test_changed_file_in_root_symlink_escape_not_scanned(tmp_path):
    outside = _seed_outside_source(tmp_path)
    project = _make_project(tmp_path, _FORBIDDEN_CONFIG)
    (project / "leak_link.py").symlink_to(outside / "leak.py")

    result = run_policy(project, changed_files=["leak_link.py"])

    assert "no-secret" not in _violation_ids(result), (
        "in-root symlink (changed_file) escaping the root was scanned"
    )


# --- anti-false-red: in-root evidence is unchanged ----------------------------


def test_in_root_source_still_scanned_and_flagged(tmp_path):
    project = _make_project(tmp_path, _FORBIDDEN_CONFIG)
    src = project / "src"
    src.mkdir()
    (src / "bad.py").write_text(f"value = '{SECRET}'\n", encoding="utf-8")

    result = run_policy(project)

    assert "no-secret" in _violation_ids(result), (
        "in-root source file with forbidden pattern was not flagged (false-red)"
    )
    assert result.files_checked == 1


def test_in_root_changed_file_still_scanned(tmp_path):
    project = _make_project(tmp_path, _FORBIDDEN_CONFIG)
    src = project / "src"
    src.mkdir()
    (src / "bad.py").write_text(f"value = '{SECRET}'\n", encoding="utf-8")

    result = run_policy(project, changed_files=["src/bad.py"])

    assert "no-secret" in _violation_ids(result)
    assert result.files_checked == 1


def test_changed_file_escape_emits_visibility_warning(tmp_path, capsys):
    """Visibility: an excluded out-of-root changed_file is reported on stderr."""
    outside = _seed_outside_source(tmp_path)
    project = _make_project(tmp_path, _FORBIDDEN_CONFIG)

    run_policy(project, changed_files=[str(outside / "leak.py")])

    err = capsys.readouterr().err
    assert "outside the project root" in err
    assert "excluded" in err

"""Path-escape jail tests for ``codd drift`` evidence readers.

``codd drift`` reads several filesystem paths that originate from *external,
user-controllable* config (``codd.yaml``): ``e2e.test_dir``,
``e2e.screen_transitions_path`` / ``screen_flow_drift.screen_transitions_path``,
the ``compute_screen_flow_drift`` path argument, and the ``scan.doc_dirs`` roots
read for document-URL drift. Drift is *evidence* consumed by check / coverage /
gate, so an out-of-root read (absolute, ``../`` traversal, or an in-root symlink
whose target escapes the tree) is a path-escape false-green: an off-root file's
contents are consumed as drift evidence.

These tests pin the fail-closed contract (raise :class:`PathEscapeError`, never
silently read an off-root file and never silently substitute empty evidence) and
the anti-false-red invariant (in-root evidence is read exactly as before).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from codd.drift import (
    ScreenFlowDriftResult,
    ScreenTransitionDrift,
    compute_screen_flow_drift,
    detect_screen_transition_drift,
    extract_e2e_have_url_assertions,
    run_drift,
)
from codd.path_safety import PathEscapeError

_SECRET_ROUTE = "/SECRET_EXTERNAL"


def _supports_symlinks(tmp_path: Path) -> bool:
    probe = tmp_path / "__symlink_probe__"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def _write_e2e_spec(directory: Path, route: str, name: str = "navigation.spec.ts") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    spec = directory / name
    spec.write_text(f"await expect(page).toHaveURL('{route}')\n", encoding="utf-8")
    return spec


def _write_transitions(path: Path, routes: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"edges": [{"from": "/", "to": route, "trigger": "click"} for route in routes]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# ── e2e.test_dir (extract_e2e_have_url_assertions) ───────────────────────────


def test_e2e_test_dir_absolute_out_of_root_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    _write_e2e_spec(outside / "e2e", _SECRET_ROUTE)
    config = {"e2e": {"test_dir": str(outside / "e2e")}}

    with pytest.raises(PathEscapeError):
        extract_e2e_have_url_assertions(project, config)


def test_e2e_test_dir_dotdot_traversal_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_e2e_spec(tmp_path / "e2e", _SECRET_ROUTE)
    config = {"e2e": {"test_dir": "../e2e"}}

    with pytest.raises(PathEscapeError):
        extract_e2e_have_url_assertions(project, config)


def test_e2e_test_dir_symlink_escape_fails_closed(tmp_path):
    if not _supports_symlinks(tmp_path):
        pytest.skip("symlinks unsupported on this filesystem")
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external_e2e"
    _write_e2e_spec(external, _SECRET_ROUTE)
    (project / "tests").mkdir()
    (project / "tests" / "e2e").symlink_to(external, target_is_directory=True)

    with pytest.raises(PathEscapeError):
        extract_e2e_have_url_assertions(project)


def test_e2e_test_dir_in_root_unchanged(tmp_path):
    project = tmp_path / "project"
    _write_e2e_spec(project / "tests" / "e2e", "/login")

    assert extract_e2e_have_url_assertions(project) == ["/login"]


def test_e2e_test_dir_in_root_missing_is_benign(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert extract_e2e_have_url_assertions(project) == []


# ── screen_transitions_path (detect_screen_transition_drift) ─────────────────


def test_screen_transitions_absolute_out_of_root_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = _write_transitions(tmp_path / "outside" / "screen-transitions.yaml", [_SECRET_ROUTE])
    config = {"e2e": {"screen_transitions_path": str(external)}}

    with pytest.raises(PathEscapeError):
        detect_screen_transition_drift(project, config)


def test_screen_transitions_dotdot_traversal_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_transitions(tmp_path / "screen-transitions.yaml", [_SECRET_ROUTE])
    config = {"e2e": {"screen_transitions_path": "../screen-transitions.yaml"}}

    with pytest.raises(PathEscapeError):
        detect_screen_transition_drift(project, config)


def test_screen_transitions_symlink_escape_fails_closed(tmp_path):
    if not _supports_symlinks(tmp_path):
        pytest.skip("symlinks unsupported on this filesystem")
    project = tmp_path / "project"
    project.mkdir()
    external = _write_transitions(tmp_path / "external.yaml", [_SECRET_ROUTE])
    link_dir = project / "docs" / "extracted"
    link_dir.mkdir(parents=True)
    (link_dir / "screen-transitions.yaml").symlink_to(external)

    with pytest.raises(PathEscapeError):
        detect_screen_transition_drift(project)


def test_screen_transitions_in_root_unchanged(tmp_path):
    project = tmp_path / "project"
    _write_transitions(project / "docs" / "extracted" / "screen-transitions.yaml", ["/dashboard"])

    result = detect_screen_transition_drift(project)

    assert result.missing_in_e2e == ["/dashboard"]
    assert result.extra_in_e2e == []
    assert result.coverage_ratio == 0.0


def test_screen_transitions_in_root_missing_is_benign(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert detect_screen_transition_drift(project) == ScreenTransitionDrift(
        missing_in_e2e=[], extra_in_e2e=[], coverage_ratio=1.0
    )


# ── compute_screen_flow_drift (path arg + config) ────────────────────────────


def test_screen_flow_path_arg_absolute_out_of_root_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = _write_transitions(tmp_path / "outside" / "screen-transitions.yaml", [_SECRET_ROUTE])

    with pytest.raises(PathEscapeError):
        compute_screen_flow_drift(project, screen_transitions_yaml_path=str(external))


def test_screen_flow_config_path_dotdot_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_transitions(tmp_path / "screen-transitions.yaml", [_SECRET_ROUTE])
    config = {"screen_flow_drift": {"screen_transitions_path": "../screen-transitions.yaml"}}

    with pytest.raises(PathEscapeError):
        compute_screen_flow_drift(project, extractor_config=config)


def test_screen_flow_in_root_missing_is_benign(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert compute_screen_flow_drift(project) == ScreenFlowDriftResult(
        design_only=[], impl_only=[], mismatch=[], total_design=0, total_impl=0
    )


# ── document-URL drift evidence (scan.doc_dirs in run_drift) ─────────────────


def _write_codd_yaml(project: Path, config: dict) -> Path:
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return codd_dir


def _doc_url_config(doc_dir: str) -> dict:
    return {
        "filesystem_routes": [],
        "document_url_linking": {"enabled": True},
        "scan": {"doc_dirs": [doc_dir]},
    }


def test_doc_dirs_dotdot_traversal_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external_docs = tmp_path / "external_docs"
    external_docs.mkdir()
    (external_docs / "spec.md").write_text(f"See `{_SECRET_ROUTE}` endpoint.\n", encoding="utf-8")
    codd_dir = _write_codd_yaml(project, _doc_url_config("../external_docs"))

    with pytest.raises(PathEscapeError):
        run_drift(project, codd_dir)


def test_doc_dirs_absolute_out_of_root_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external_docs = tmp_path / "external_docs"
    external_docs.mkdir()
    (external_docs / "spec.md").write_text(f"See `{_SECRET_ROUTE}` endpoint.\n", encoding="utf-8")
    codd_dir = _write_codd_yaml(project, _doc_url_config(str(external_docs)))

    with pytest.raises(PathEscapeError):
        run_drift(project, codd_dir)


def test_doc_dirs_in_root_unchanged(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    docs = project / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("See `/in-root-route` endpoint.\n", encoding="utf-8")
    codd_dir = _write_codd_yaml(project, _doc_url_config("docs"))

    result = run_drift(project, codd_dir)

    design_urls = set(result.design_urls)
    assert "/in-root-route" in design_urls
    assert _SECRET_ROUTE not in design_urls


def test_doc_dirs_in_root_symlink_file_dropped(tmp_path):
    if not _supports_symlinks(tmp_path):
        pytest.skip("symlinks unsupported on this filesystem")
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external.md"
    external.write_text(f"See `{_SECRET_ROUTE}` endpoint.\n", encoding="utf-8")
    docs = project / "docs"
    docs.mkdir()
    (docs / "in_root.md").write_text("See `/in-root-route` endpoint.\n", encoding="utf-8")
    (docs / "smuggled.md").symlink_to(external)
    codd_dir = _write_codd_yaml(project, _doc_url_config("docs"))

    result = run_drift(project, codd_dir)

    design_urls = set(result.design_urls)
    assert "/in-root-route" in design_urls
    assert _SECRET_ROUTE not in design_urls


# ── CLI boundary: fail-closed (non-zero exit, no off-root leak) ──────────────


def test_cli_drift_e2e_escaping_config_fails_closed(tmp_path):
    from click.testing import CliRunner

    from codd.cli import main

    project = tmp_path / "project"
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True)
    codd_dir.joinpath("codd.yaml").write_text(
        yaml.safe_dump(
            {"filesystem_routes": [], "e2e": {"screen_transitions_path": "../external.yaml"}}
        ),
        encoding="utf-8",
    )
    _write_transitions(tmp_path / "external.yaml", [_SECRET_ROUTE])

    result = CliRunner().invoke(main, ["drift", "--e2e", "--path", str(project)])

    assert result.exit_code != 0
    assert _SECRET_ROUTE not in result.output


def test_cli_drift_doc_dirs_escaping_config_fails_closed(tmp_path):
    from click.testing import CliRunner

    from codd.cli import main

    project = tmp_path / "project"
    codd_dir = _write_codd_yaml(project, _doc_url_config("../external_docs"))
    external_docs = tmp_path / "external_docs"
    external_docs.mkdir()
    external_docs.joinpath("spec.md").write_text(f"See `{_SECRET_ROUTE}` endpoint.\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["drift", "--path", str(project)])

    assert result.exit_code != 0
    assert _SECRET_ROUTE not in result.output


# ── generality / no-bespoke-resolver audit ───────────────────────────────────


def test_drift_uses_shared_path_safety_jail():
    """drift.py routes user-path evidence through the shared jail, not a bespoke resolver."""
    source = (Path(__file__).resolve().parents[1] / "codd" / "drift.py").read_text(encoding="utf-8")

    # The bespoke local resolver (root-confine-free) must be gone.
    assert "_resolve_project_path" not in source
    # The shared closure must be imported.
    assert "from codd.path_safety import" in source

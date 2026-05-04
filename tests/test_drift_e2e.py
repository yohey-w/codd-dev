"""Tests for E2E screen-transition drift detection."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.config import load_project_config
from codd.drift import (
    ScreenTransitionDrift,
    detect_screen_transition_drift,
    extract_e2e_have_url_assertions,
)


def _write_e2e_spec(tmp_path: Path, body: str, name: str = "navigation.spec.ts") -> Path:
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    spec_path = e2e_dir / name
    spec_path.write_text(body, encoding="utf-8")
    return spec_path


def _write_screen_transitions(tmp_path: Path, routes: list[str]) -> Path:
    output_path = tmp_path / "docs" / "extracted" / "screen-transitions.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "edges": [
            {"from": "/", "to": route, "trigger": "click", "type": "navigate"}
            for route in routes
        ]
    }
    output_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return output_path


def _write_codd_yaml(tmp_path: Path, text: str = "") -> Path:
    codd_dir = tmp_path / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    config_path = codd_dir / "codd.yaml"
    config_path.write_text(text or "filesystem_routes: []\n", encoding="utf-8")
    return config_path


def test_extract_e2e_have_url_no_e2e_dir(tmp_path):
    assert extract_e2e_have_url_assertions(tmp_path) == []


def test_extract_e2e_have_url_basic(tmp_path):
    _write_e2e_spec(tmp_path, "await expect(page).toHaveURL('/login')")

    assert extract_e2e_have_url_assertions(tmp_path) == ["/login"]


def test_extract_e2e_have_url_multiple(tmp_path):
    _write_e2e_spec(
        tmp_path,
        """
        await expect(page).toHaveURL('/login')
        await expect(page).toHaveURL("/dashboard")
        await expect(page).toHaveURL('/login')
        """,
    )

    assert extract_e2e_have_url_assertions(tmp_path) == ["/login", "/dashboard"]


def test_extract_e2e_codd_yaml_pattern_override(tmp_path):
    _write_codd_yaml(
        tmp_path,
        """
e2e:
  assertion_pattern: toBeURL
""",
    )
    _write_e2e_spec(tmp_path, "await expect(page).toBeURL('/custom')")

    assert extract_e2e_have_url_assertions(tmp_path, load_project_config(tmp_path)) == ["/custom"]


def test_detect_drift_no_transitions_yaml(tmp_path):
    _write_e2e_spec(tmp_path, "await expect(page).toHaveURL('/extra')")

    result = detect_screen_transition_drift(tmp_path)

    assert result == ScreenTransitionDrift(missing_in_e2e=[], extra_in_e2e=[], coverage_ratio=1.0)


def test_detect_drift_missing_in_e2e(tmp_path):
    _write_screen_transitions(tmp_path, ["/dashboard"])

    result = detect_screen_transition_drift(tmp_path)

    assert result.missing_in_e2e == ["/dashboard"]
    assert result.extra_in_e2e == []
    assert result.coverage_ratio == 0.0


def test_detect_drift_extra_in_e2e(tmp_path):
    _write_screen_transitions(tmp_path, ["/dashboard"])
    _write_e2e_spec(
        tmp_path,
        """
        await expect(page).toHaveURL('/dashboard')
        await expect(page).toHaveURL('/extra')
        """,
    )

    result = detect_screen_transition_drift(tmp_path)

    assert result.missing_in_e2e == []
    assert result.extra_in_e2e == ["/extra"]
    assert result.coverage_ratio == 1.0


def test_detect_drift_full_coverage(tmp_path):
    _write_screen_transitions(tmp_path, ["/login", "/dashboard"])
    _write_e2e_spec(
        tmp_path,
        """
        await expect(page).toHaveURL('/login')
        await expect(page).toHaveURL('/dashboard')
        """,
    )

    result = detect_screen_transition_drift(tmp_path)

    assert result.missing_in_e2e == []
    assert result.extra_in_e2e == []
    assert result.coverage_ratio == 1.0


def test_generality_no_playwright_hardcode():
    drift_source = (Path(__file__).resolve().parents[1] / "codd" / "drift.py").read_text(encoding="utf-8")

    assert "playwright" not in drift_source.lower()


def test_cli_drift_e2e_missing_routes_exit_one(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_screen_transitions(tmp_path, ["/dashboard"])

    result = CliRunner().invoke(main, ["drift", "--e2e", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "[drift e2e:missing_in_e2e] /dashboard" in result.output


def test_cli_drift_e2e_json_full_coverage_exit_zero(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_screen_transitions(tmp_path, ["/dashboard"])
    _write_e2e_spec(tmp_path, "await expect(page).toHaveURL('/dashboard')")

    result = CliRunner().invoke(main, ["drift", "--e2e", "--path", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0
    assert '"missing_in_e2e": []' in result.output
    assert '"coverage_ratio": 1.0' in result.output

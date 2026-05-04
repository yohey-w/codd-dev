"""Tests for DESIGN.md design token drift detection."""

from __future__ import annotations

from codd.drift import DesignTokenDriftLinker, run_drift


def _write_design_md(tmp_path, front_matter: str) -> None:
    (tmp_path / "DESIGN.md").write_text(f"---\n{front_matter.rstrip()}\n---\n", encoding="utf-8")


def _write_ui_file(tmp_path, text: str, name: str = "Button.tsx") -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / name).write_text(text, encoding="utf-8")


def _token_statuses(drifts: list[dict[str, str]]) -> set[tuple[str, str]]:
    return {(drift["token"], drift["status"]) for drift in drifts}


def test_detect_drift_missing_token(tmp_path):
    _write_design_md(tmp_path, 'version: "1.0"')
    _write_ui_file(tmp_path, 'const color = "{colors.Primary}";')

    drifts = DesignTokenDriftLinker(tmp_path).detect_drift()

    assert ("colors.Primary", "missing_in_design_md") in _token_statuses(drifts)
    assert all(drift["kind"] == "design_token" for drift in drifts)


def test_detect_drift_unused_token(tmp_path):
    _write_design_md(
        tmp_path,
        """
colors:
  Secondary: "#34A853"
""",
    )

    drifts = DesignTokenDriftLinker(tmp_path).detect_drift()

    assert drifts == [
        {"token": "colors.Secondary", "status": "unused_design_token", "kind": "design_token"}
    ]


def test_no_drift_all_defined(tmp_path):
    _write_design_md(
        tmp_path,
        """
colors:
  Primary: "#1A73E8"
spacing:
  sm: "8px"
""",
    )
    _write_ui_file(tmp_path, 'style={{ color: "{colors.Primary}", gap: "{spacing.sm}" }}')

    assert DesignTokenDriftLinker(tmp_path).detect_drift() == []


def test_no_design_md_returns_empty(tmp_path):
    _write_ui_file(tmp_path, 'const color = "{colors.Primary}";')

    assert DesignTokenDriftLinker(tmp_path).detect_drift() == []


def test_run_drift_includes_design_token_drift(tmp_path):
    codd_dir = tmp_path / ".codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text("filesystem_routes: []\n", encoding="utf-8")
    _write_design_md(tmp_path, 'version: "1.0"')
    _write_ui_file(tmp_path, 'const color = "{colors.Primary}";')

    result = run_drift(tmp_path, codd_dir)

    assert result.exit_code == 1
    assert [(entry.kind, entry.token, entry.status) for entry in result.drift] == [
        ("design_token", "colors.Primary", "missing_in_design_md")
    ]

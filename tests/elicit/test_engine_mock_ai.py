"""Mock AI sentinel for elicit (cmd_466 #7)."""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.elicit.engine import ElicitEngine, _is_mock_ai_command


def test_mock_sentinel_recognition() -> None:
    for value in ("true", ":", "none", "mock", "TRUE", " true "):
        assert _is_mock_ai_command(value) is True
    for value in ("claude --print", "codex exec", "", None, 0, [], {}):
        assert _is_mock_ai_command(value) is False


def _scaffold(project: Path) -> None:
    (project / "docs" / "requirements").mkdir(parents=True)
    (project / "docs" / "requirements" / "req.md").write_text("# req", encoding="utf-8")
    (project / "docs" / "design").mkdir()
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "project": "test",
                "scope": "system_implementation",
                "phase": "mvp",
                "node_vocabulary": [
                    {"id": "x", "naming_convention": "snake_case", "provenance": "human"}
                ],
                "naming_conventions": [{"id": "snake_case", "regex": "^[a-z_]+$"}],
                "design_principles": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_mock_ai_returns_empty_findings(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    engine = ElicitEngine(ai_command="true")
    result = engine.run(tmp_path)
    assert result.findings == []


def test_explicit_ai_path_unchanged_for_string_callable(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    captured = {}

    def fake_ai(prompt: str) -> str:
        captured["called"] = True
        return '{"findings": [], "lexicon_coverage_report": {}}'

    engine = ElicitEngine(ai_command=fake_ai)
    engine.run(tmp_path)
    assert captured.get("called") is True

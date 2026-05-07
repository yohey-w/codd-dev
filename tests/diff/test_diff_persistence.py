from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.diff.persistence import DiffPersistence, append_history, load_ignored, save_ignored


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_load_ignored_returns_empty_set_when_file_missing(tmp_path: Path) -> None:
    assert load_ignored(tmp_path) == set()


def test_load_ignored_reads_mapping_string_and_nested_entries(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".codd" / "diff" / "ignored_findings.yaml",
        {"ignored": [{"id": "DIFF-1"}, "DIFF-2", {"finding": {"id": "DIFF-3"}}]},
    )

    assert DiffPersistence(tmp_path).load_ignored() == {"DIFF-1", "DIFF-2", "DIFF-3"}


def test_save_ignored_writes_schema(tmp_path: Path) -> None:
    save_ignored(tmp_path, "DIFF-1", "Intentional behavior")

    payload = yaml.safe_load((tmp_path / ".codd" / "diff" / "ignored_findings.yaml").read_text(encoding="utf-8"))
    assert payload["ignored"][0]["id"] == "DIFF-1"
    assert payload["ignored"][0]["ignored_at"]
    assert payload["ignored"][0]["reason"] == "Intentional behavior"


def test_save_ignored_does_not_duplicate_existing_ids(tmp_path: Path) -> None:
    persistence = DiffPersistence(tmp_path)

    persistence.save_ignored("DIFF-1", "first")
    persistence.save_ignored("DIFF-1", "second")

    payload = yaml.safe_load((tmp_path / ".codd" / "diff" / "ignored_findings.yaml").read_text(encoding="utf-8"))
    assert len(payload["ignored"]) == 1
    assert payload["ignored"][0]["reason"] == "first"


def test_append_history_creates_and_appends_sessions(tmp_path: Path) -> None:
    append_history(tmp_path, {"timestamp": "t1", "findings_total": 1})
    append_history(tmp_path, {"timestamp": "t2", "findings_total": 2})

    payload = yaml.safe_load((tmp_path / ".codd" / "diff" / "diff_history.yaml").read_text(encoding="utf-8"))
    assert [session["timestamp"] for session in payload["sessions"]] == ["t1", "t2"]


def test_invalid_yaml_shape_raises_value_error(tmp_path: Path) -> None:
    _write_yaml(tmp_path / ".codd" / "diff" / "ignored_findings.yaml", {"ignored": {}})

    with pytest.raises(ValueError, match="ignored"):
        DiffPersistence(tmp_path).load_ignored()


def test_non_mapping_yaml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / ".codd" / "diff" / "ignored_findings.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("- invalid\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_ignored(tmp_path)

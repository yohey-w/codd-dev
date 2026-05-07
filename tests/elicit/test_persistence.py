from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.elicit.finding import Finding
from codd.elicit.persistence import (
    ElicitPersistence,
    append_history,
    filter_known_findings,
    load_ignored,
    load_pending,
    save_pending,
)


def _finding(finding_id: str = "F-1") -> Finding:
    return Finding(id=finding_id, kind="gap", severity="medium", rationale="Reason")


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_load_ignored_returns_empty_set_when_file_missing(tmp_path: Path) -> None:
    assert ElicitPersistence(tmp_path).load_ignored() == set()


def test_load_ignored_reads_mapping_and_string_entries(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".codd" / "elicit" / "ignored_findings.yaml",
        {"ignored": [{"id": "F-1"}, "F-2", {"finding": {"id": "F-3"}}]},
    )

    assert load_ignored(tmp_path) == {"F-1", "F-2", "F-3"}


def test_load_pending_returns_findings(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".codd" / "elicit" / "pending_findings.yaml",
        {"pending": [{"finding": _finding("F-1").to_dict()}]},
    )

    assert load_pending(tmp_path) == [_finding("F-1")]


def test_load_pending_accepts_raw_finding_entries(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".codd" / "elicit" / "pending_findings.yaml",
        {"pending": [_finding("F-1").to_dict()]},
    )

    assert ElicitPersistence(tmp_path).load_pending() == [_finding("F-1")]


def test_save_pending_writes_schema(tmp_path: Path) -> None:
    save_pending(tmp_path, [_finding("F-1"), _finding("F-2")])

    payload = yaml.safe_load(
        (tmp_path / ".codd" / "elicit" / "pending_findings.yaml").read_text(encoding="utf-8")
    )
    assert [entry["finding"]["id"] for entry in payload["pending"]] == ["F-1", "F-2"]
    assert payload["pending"][0]["discovered_at"]
    assert payload["pending"][0]["last_review_at"] is None


def test_save_pending_overwrites_previous_pending(tmp_path: Path) -> None:
    persistence = ElicitPersistence(tmp_path)
    persistence.save_pending([_finding("F-1")])
    persistence.save_pending([_finding("F-2")])

    assert persistence.load_pending() == [_finding("F-2")]


def test_append_history_creates_and_appends_sessions(tmp_path: Path) -> None:
    append_history(tmp_path, {"timestamp": "t1", "findings_total": 1})
    append_history(tmp_path, {"timestamp": "t2", "findings_total": 2})

    payload = yaml.safe_load(
        (tmp_path / ".codd" / "elicit" / "elicit_history.yaml").read_text(encoding="utf-8")
    )
    assert [session["timestamp"] for session in payload["sessions"]] == ["t1", "t2"]


def test_filter_known_removes_ignored_and_pending_ids(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".codd" / "elicit" / "ignored_findings.yaml",
        {"ignored": [{"id": "F-1"}]},
    )
    save_pending(tmp_path, [_finding("F-2")])

    assert filter_known_findings(tmp_path, [_finding("F-1"), _finding("F-2"), _finding("F-3")]) == [
        _finding("F-3")
    ]


def test_invalid_yaml_shape_raises_value_error(tmp_path: Path) -> None:
    _write_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml", {"pending": {}})

    with pytest.raises(ValueError, match="pending"):
        ElicitPersistence(tmp_path).load_pending()


def test_non_mapping_yaml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / ".codd" / "elicit" / "ignored_findings.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("- invalid\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        ElicitPersistence(tmp_path).load_ignored()

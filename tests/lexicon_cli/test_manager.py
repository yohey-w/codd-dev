from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.lexicon import validate_lexicon
from codd.lexicon_cli.manager import LexiconManager


REPO_ROOT = Path(__file__).parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons"


def _first_record(manager: LexiconManager):
    records = manager.available()
    assert records
    return records[0]


def test_available_lists_bundled_manifest_directories(tmp_path: Path) -> None:
    records = LexiconManager(tmp_path, LEXICON_ROOT).available()

    assert records
    assert all(record.id for record in records)
    assert all(record.lexicon_name for record in records)


def test_available_marks_installed_suggested_lexicons(tmp_path: Path) -> None:
    first = _first_record(LexiconManager(tmp_path, LEXICON_ROOT))
    (tmp_path / "project_lexicon.yaml").write_text(
        yaml.safe_dump(
            {
                "node_vocabulary": [],
                "naming_conventions": [],
                "design_principles": [],
                "suggested_lexicons": [first.id],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    records = LexiconManager(tmp_path, LEXICON_ROOT).available()

    assert any(record.id == first.id and record.installed for record in records)


def test_installed_ids_support_mapping_entries(tmp_path: Path) -> None:
    (tmp_path / "project_lexicon.yaml").write_text(
        yaml.safe_dump(
            {
                "node_vocabulary": [],
                "naming_conventions": [],
                "design_principles": [],
                "suggested_lexicons": [{"id": "one"}, {"lexicon_name": "two"}, "one"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert LexiconManager(tmp_path, LEXICON_ROOT).installed_ids() == ["one", "two"]


def test_record_reads_manifest_description_and_dimensions(tmp_path: Path) -> None:
    record = _first_record(LexiconManager(tmp_path, LEXICON_ROOT))

    assert record.description
    assert record.observation_dimensions >= 0


def test_record_reads_recommended_kinds_when_file_exists(tmp_path: Path) -> None:
    record = next(record for record in LexiconManager(tmp_path, LEXICON_ROOT).available() if record.recommended_kinds)

    assert record.recommended_kinds


def test_install_creates_valid_project_lexicon(tmp_path: Path) -> None:
    first = _first_record(LexiconManager(tmp_path, LEXICON_ROOT))

    result = LexiconManager(tmp_path, LEXICON_ROOT).install([first.id])

    data = yaml.safe_load(result.project_lexicon_path.read_text(encoding="utf-8"))
    validate_lexicon(data)
    assert result.installed == (first.id,)
    assert data["suggested_lexicons"] == [first.id]


def test_install_skips_existing_lexicon(tmp_path: Path) -> None:
    manager = LexiconManager(tmp_path, LEXICON_ROOT)
    first = _first_record(manager)
    manager.install([first.id])

    result = LexiconManager(tmp_path, LEXICON_ROOT).install([first.id])

    assert result.installed == ()
    assert result.skipped == (first.id,)


def test_resolve_accepts_manifest_lexicon_name(tmp_path: Path) -> None:
    manager = LexiconManager(tmp_path, LEXICON_ROOT)
    first = _first_record(manager)

    resolved = manager.resolve(first.lexicon_name)

    assert resolved.id == first.id


def test_resolve_unknown_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown lexicon"):
        LexiconManager(tmp_path, LEXICON_ROOT).resolve("missing")


def test_available_ignores_directories_without_manifest(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    (root / "without_manifest").mkdir(parents=True)

    assert LexiconManager(tmp_path, root).available() == []


def test_available_handles_missing_root(tmp_path: Path) -> None:
    assert LexiconManager(tmp_path, tmp_path / "missing").available() == []

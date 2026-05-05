from codd.watch.events import FileChangeEvent
from codd.watch.propagation_log import (
    MAX_LOG_ENTRIES,
    append_propagation_log,
    read_propagation_log,
)


def test_file_change_event_creation():
    event = FileChangeEvent(
        files=["app/api/users/route.ts"],
        source="editor_hook",
        editor="codex",
    )

    assert event.files == ["app/api/users/route.ts"]
    assert event.source == "editor_hook"
    assert event.editor == "codex"
    assert event.timestamp
    assert event.event_id.startswith("fce-")
    assert len(event.event_id) == 12


def test_file_change_event_to_dict():
    event = FileChangeEvent(
        files=["docs/design/api.md"],
        source="watch",
        timestamp="2026-05-05T10:00:00+00:00",
        event_id="fce-12345678",
    )

    assert event.to_dict() == {
        "timestamp": "2026-05-05T10:00:00+00:00",
        "files": ["docs/design/api.md"],
        "source": "watch",
        "editor": None,
        "event_id": "fce-12345678",
    }


def test_file_change_event_from_dict():
    event = FileChangeEvent.from_dict(
        {
            "timestamp": "2026-05-05T10:00:00+00:00",
            "files": ["codd/cli.py"],
            "source": "git_hook",
            "editor": "manual",
            "event_id": "fce-87654321",
        }
    )

    assert event.files == ["codd/cli.py"]
    assert event.source == "git_hook"
    assert event.editor == "manual"
    assert event.timestamp == "2026-05-05T10:00:00+00:00"
    assert event.event_id == "fce-87654321"


def test_propagation_log_append(tmp_path):
    event = FileChangeEvent(
        files=["codd/propagator.py"],
        source="editor_hook",
        editor="claude",
        event_id="fce-aaaaaaaa",
    )

    append_propagation_log(tmp_path, event, {"impacted_nodes": ["docs/design/api.md"]})

    entries = read_propagation_log(tmp_path)
    assert entries == [
        {
            **event.to_dict(),
            "propagation_result": {"impacted_nodes": ["docs/design/api.md"]},
        }
    ]


def test_propagation_log_ring_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr("codd.watch.propagation_log.MAX_LOG_ENTRIES", 3)

    for index in range(5):
        append_propagation_log(
            tmp_path,
            FileChangeEvent(
                files=[f"file_{index}.py"],
                source="watch",
                event_id=f"fce-{index:08d}",
            ),
            {"index": index},
        )

    entries = read_propagation_log(tmp_path)
    assert len(entries) == 3
    assert [entry["event_id"] for entry in entries] == [
        "fce-00000002",
        "fce-00000003",
        "fce-00000004",
    ]
    assert len(entries) <= MAX_LOG_ENTRIES


def test_propagation_log_read_empty(tmp_path):
    assert read_propagation_log(tmp_path) == []

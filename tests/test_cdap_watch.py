import threading

import pytest
from click.testing import CliRunner

pytest.importorskip("watchdog")

from codd.cli import main
from codd.watch.events import FileChangeEvent
from codd.watch.watcher import CoDDWatchHandler, start_watch


class DummyEvent:
    def __init__(self, src_path, is_directory=False):
        self.src_path = str(src_path)
        self.is_directory = is_directory


class FakeTimer:
    instances = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.started = False
        self.daemon = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture(autouse=True)
def fake_timer(monkeypatch):
    FakeTimer.instances = []
    monkeypatch.setattr(threading, "Timer", FakeTimer)


def test_watch_handler_created(tmp_path):
    events = []
    handler = CoDDWatchHandler(tmp_path, events.append)

    assert handler.project_root == tmp_path.resolve()
    assert callable(handler.callback)
    assert handler.debounce_ms == 500


def test_watch_handler_on_modified_schedules(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler.on_modified(DummyEvent(tmp_path / "app.py"))

    assert handler._pending == ["app.py"]
    assert FakeTimer.instances[-1].started is True


def test_watch_handler_on_created_schedules(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler.on_created(DummyEvent(tmp_path / "new.py"))

    assert handler._pending == ["new.py"]


def test_watch_handler_debounce(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler.on_modified(DummyEvent(tmp_path / "a.py"))
    first_timer = FakeTimer.instances[-1]
    handler.on_modified(DummyEvent(tmp_path / "b.py"))

    assert first_timer.cancelled is True
    assert handler._pending == ["a.py", "b.py"]
    assert len(FakeTimer.instances) == 2


def test_watch_handler_flush_calls_callback(tmp_path):
    events = []
    handler = CoDDWatchHandler(tmp_path, events.append)
    handler.on_modified(DummyEvent(tmp_path / "a.py"))

    handler._flush()

    assert len(events) == 1
    assert events[0].files == ["a.py"]
    assert events[0].source == "watch"
    assert events[0].editor == "manual"


def test_watch_handler_ignores_directory_events(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler.on_modified(DummyEvent(tmp_path / "pkg", is_directory=True))
    handler.on_created(DummyEvent(tmp_path / "pkg", is_directory=True))

    assert handler._pending == []
    assert FakeTimer.instances == []


def test_file_change_event_source_watch():
    event = FileChangeEvent(files=["a.py"], source="watch", editor="manual")

    assert event.source == "watch"


def test_watch_handler_relative_path(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler._schedule(str(tmp_path / "src" / "app.py"))

    assert handler._pending == ["src/app.py"]


def test_watch_handler_dedup_pending(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)

    handler._schedule(str(tmp_path / "src" / "app.py"))
    handler._schedule(str(tmp_path / "src" / "app.py"))

    assert handler._pending == ["src/app.py"]


def test_start_watch_returns_observer(tmp_path, monkeypatch):
    class FakeObserver:
        def __init__(self):
            self.scheduled = []
            self.started = False

        def schedule(self, handler, path, recursive):
            self.scheduled.append((handler, path, recursive))

        def start(self):
            self.started = True

    fake_observer = FakeObserver()
    monkeypatch.setattr("codd.watch.watcher.Observer", lambda: fake_observer)

    observer = start_watch(tmp_path, lambda event: None, background=True)

    assert observer is fake_observer
    assert fake_observer.started is True
    assert fake_observer.scheduled[0][1] == str(tmp_path.resolve())
    assert fake_observer.scheduled[0][2] is True


def test_cli_watch_status_not_running(tmp_path):
    result = CliRunner().invoke(main, ["watch", "--status", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Watcher not running" in result.output


def test_cli_watch_status_running(tmp_path):
    pid_file = tmp_path / ".codd" / "watch.pid"
    pid_file.parent.mkdir()
    pid_file.write_text("12345\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["watch", "--status", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Watcher running (PID: 12345)" in result.output


def test_cli_watch_help():
    result = CliRunner().invoke(main, ["watch", "--help"])

    assert result.exit_code == 0
    assert "Watch for file changes" in result.output


def test_watch_cmd_registered():
    assert "watch" in main.commands


def test_debounce_default_500ms(tmp_path, monkeypatch):
    calls = []

    def fake_start(project_root, callback, debounce_ms, background):
        calls.append((project_root, debounce_ms, background))

        class FakeObserver:
            def join(self, timeout=None):
                return None

        return FakeObserver()

    monkeypatch.setattr("codd.watch.watcher.start_watch", fake_start)

    result = CliRunner().invoke(main, ["watch", "--project-path", str(tmp_path), "--background"])

    assert result.exit_code == 0
    assert calls[0][1] == 500


def test_watch_handler_flush_clears_pending(tmp_path):
    handler = CoDDWatchHandler(tmp_path, lambda event: None)
    handler._schedule(str(tmp_path / "a.py"))

    handler._flush()

    assert handler._pending == []
    assert handler._timer is None


def test_multiple_files_batched(tmp_path):
    events = []
    handler = CoDDWatchHandler(tmp_path, events.append)
    handler._schedule(str(tmp_path / "a.py"))
    handler._schedule(str(tmp_path / "b.py"))

    handler._flush()

    assert events[0].files == ["a.py", "b.py"]


def test_background_mode_flag():
    result = CliRunner().invoke(main, ["watch", "--help"])

    assert result.exit_code == 0
    assert "--background" in result.output

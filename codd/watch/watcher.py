"""File watcher for Change-Driven Auto-Propagation Pipeline."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from codd.watch.events import FileChangeEvent


class CoDDWatchHandler(FileSystemEventHandler):
    """Batch file system changes into debounced FileChangeEvent callbacks."""

    def __init__(
        self,
        project_root: Path,
        callback: Callable[[FileChangeEvent], None],
        debounce_ms: int = 500,
    ) -> None:
        super().__init__()
        self.project_root = project_root.resolve()
        self.callback = callback
        self.debounce_ms = debounce_ms
        self._pending: list[str] = []
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._schedule(str(event.src_path))

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._schedule(str(event.src_path))

    def _schedule(self, path: str) -> None:
        rel_path = self._relative_path(path)
        with self._lock:
            if rel_path not in self._pending:
                self._pending.append(rel_path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_ms / 1000, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            files = list(self._pending)
            self._pending.clear()
            self._timer = None

        if files:
            self.callback(
                FileChangeEvent(
                    files=files,
                    source="watch",
                    editor="manual",
                )
            )

    def cancel(self) -> None:
        """Cancel any pending debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _relative_path(self, path: str) -> str:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            return resolved.as_posix()


def start_watch(
    project_root: Path,
    callback: Callable[[FileChangeEvent], None],
    debounce_ms: int = 500,
    background: bool = False,
):
    """Start a recursive project watcher and return the watchdog observer."""
    root = project_root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Project path is not a directory: {root}")

    observer = Observer()
    handler = CoDDWatchHandler(root, callback, debounce_ms)
    observer.schedule(handler, str(root), recursive=True)
    observer.start()

    if background:
        return observer

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    finally:
        handler.cancel()
        observer.join()

    return observer

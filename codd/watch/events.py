"""File-change event primitives for CDAP."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

ChangeSource = Literal["watch", "git_hook", "editor_hook"]
EditorKind = Literal["claude", "codex", "manual"]


@dataclass
class FileChangeEvent:
    """Represents a file change event from any source."""

    files: list[str]
    source: ChangeSource
    editor: EditorKind | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_id: str = field(default_factory=lambda: f"fce-{uuid.uuid4().hex[:8]}")

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "files": self.files,
            "source": self.source,
            "editor": self.editor,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileChangeEvent":
        return cls(
            files=list(data["files"]),
            source=data["source"],
            editor=data.get("editor"),
            timestamp=data.get("timestamp", ""),
            event_id=data.get("event_id", ""),
        )

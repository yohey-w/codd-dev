"""Propagation log storage for CDAP."""

from __future__ import annotations

import json
from pathlib import Path

from codd.json_safe import json_default
from codd.watch.events import FileChangeEvent

PROPAGATION_LOG_PATH = ".codd/propagation_log.jsonl"
MAX_LOG_ENTRIES = 1000


def append_propagation_log(
    project_root: Path,
    event: FileChangeEvent,
    result: dict,
) -> None:
    """Append a propagation event to the ring buffer log."""

    log_path = Path(project_root) / PROPAGATION_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {**event.to_dict(), "propagation_result": result}
    entries = (*read_propagation_log(project_root), entry)[-MAX_LOG_ENTRIES:]

    with log_path.open("w", encoding="utf-8") as handle:
        for item in entries:
            # default=json_default: a propagated design doc's `date:` frontmatter
            # parses to datetime.date, which raw json.dumps rejects (issue #28).
            handle.write(json.dumps(item, ensure_ascii=False, default=json_default) + "\n")


def read_propagation_log(project_root: Path) -> list[dict]:
    """Read all entries from the propagation log."""

    log_path = Path(project_root) / PROPAGATION_LOG_PATH
    if not log_path.exists():
        return []

    with log_path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]

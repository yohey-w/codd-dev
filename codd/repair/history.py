"""Repair history persistence under project-local ``.codd`` state."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codd.repair.schema import (
    ApplyResult,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


class RepairHistory:
    DEFAULT_HISTORY_DIR = Path(".codd/repair_history")

    def new_session(self, history_dir: Path | None = None) -> Path:
        """Create and return a new timestamped repair history directory."""

        root = self._history_dir(history_dir)
        root.mkdir(parents=True, exist_ok=True)
        session_dir = root / _timestamp()
        suffix = 1
        while session_dir.exists():
            session_dir = root / f"{_timestamp()}-{suffix:03d}"
            suffix += 1
        session_dir.mkdir()
        return session_dir

    def record_attempt(
        self,
        session_dir: Path,
        attempt: int,
        failure: VerificationFailureReport,
        rca: RootCauseAnalysis,
        proposal: RepairProposal,
        apply_result: ApplyResult,
        post_verify: dict | None,
    ) -> None:
        """Write one attempt directory with the repair inputs and results."""

        attempt_dir = Path(session_dir) / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        _write_yaml(attempt_dir / "failure_report.yaml", failure)
        _write_yaml(attempt_dir / "root_cause_analysis.yaml", rca)
        _write_yaml(attempt_dir / "repair_proposal.yaml", proposal)
        _write_yaml(attempt_dir / "apply_result.yaml", apply_result)
        _write_yaml(attempt_dir / "post_repair_verify.yaml", post_verify)

    def finalize(self, session_dir: Path, outcome: str) -> None:
        """Write the final repair session outcome."""

        allowed = {"REPAIR_SUCCESS", "REPAIR_EXHAUSTED", "REPAIR_REJECTED_BY_HITL", "REPAIR_FAILED"}
        if outcome not in allowed:
            raise ValueError(f"outcome must be one of {sorted(allowed)}")
        _write_yaml(
            Path(session_dir) / "final_status.yaml",
            {"outcome": outcome, "timestamp": _timestamp()},
        )

    def load_session(self, session_dir: Path) -> dict:
        """Load all YAML files from a repair session."""

        root = Path(session_dir)
        session: dict[str, Any] = {
            "session_dir": str(root),
            "attempts": {},
            "final_status": None,
        }
        for attempt_dir in sorted(root.glob("attempt_*"), key=_attempt_sort_key):
            if not attempt_dir.is_dir():
                continue
            session["attempts"][attempt_dir.name] = {
                path.stem: _read_yaml(path) for path in sorted(attempt_dir.glob("*.yaml"))
            }

        final_status = root / "final_status.yaml"
        if final_status.exists():
            session["final_status"] = _read_yaml(final_status)
        return session

    def list_sessions(self, history_dir: Path | None = None) -> list[Path]:
        """Return timestamped repair session directories, newest first."""

        root = self._history_dir(history_dir)
        if not root.is_dir():
            return []
        return sorted([path for path in root.iterdir() if path.is_dir()], key=lambda path: path.name, reverse=True)

    def _history_dir(self, history_dir: Path | None) -> Path:
        return history_dir if history_dir is not None else self.DEFAULT_HISTORY_DIR


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_yaml(path: Path, value: Any) -> None:
    path.write_text(yaml.safe_dump(_to_plain_data(value), sort_keys=False, allow_unicode=False), encoding="utf-8")


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _to_plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _attempt_sort_key(path: Path) -> tuple[int, str]:
    try:
        return int(path.name.removeprefix("attempt_")), path.name
    except ValueError:
        return 10**9, path.name


__all__ = ["RepairHistory"]

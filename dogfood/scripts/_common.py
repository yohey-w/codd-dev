"""Shared helpers for the dogfood harness scripts (axis runners + loop tick).

Pure-stdlib (+ PyYAML, already a CoDD dependency). Every helper is LLM-free and
side-effect-free except the explicit ledger writer. Named with a leading
underscore and NOT ``run_*`` so pytest never collects it.

A "Finding" is the harness's atom of discovery: a deterministic axis run that
crashes, escapes the tree, or loses coverage produces one. Findings are what
reset an axis's saturation counter and what (when triaged) spawn derived cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

import yaml


# Repo root = two levels up from this file: dogfood/scripts/_common.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_DIR = REPO_ROOT / "dogfood"
LEDGER_PATH = DOGFOOD_DIR / "ledger.yaml"
FIXTURES_DIR = DOGFOOD_DIR / "fixtures"


def ensure_repo_on_path() -> None:
    """Make ``import codd`` work no matter the current working directory."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


@dataclass
class Finding:
    """One deterministic discovery from an axis run."""

    axis: str
    symptom: str
    detail: str = ""
    subject: str = ""  # which repo / fixture / input surfaced it

    def as_line(self) -> str:
        where = f" [{self.subject}]" if self.subject else ""
        extra = f" — {self.detail}" if self.detail else ""
        return f"{self.axis}{where}: {self.symptom}{extra}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "symptom": self.symptom,
            "detail": self.detail,
            "subject": self.subject,
        }


@dataclass
class AxisResult:
    """The outcome of one axis run: findings + a short human summary + stats."""

    axis: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    skipped: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def dry(self) -> bool:
        """A run is dry (saturation-advancing) IFF it surfaced zero findings."""
        return not self.findings

    def print_report(self) -> None:
        head = f"[{self.axis}] {self.summary}".rstrip()
        print(head)
        for note in self.skipped:
            print(f"  SKIP: {note}")
        if self.findings:
            for f in self.findings:
                print(f"  FINDING: {f.as_line()}")
        else:
            print("  (no findings — dry run)")


def load_ledger() -> dict[str, Any]:
    """Load the ledger, or a minimal skeleton if it is missing."""
    if not LEDGER_PATH.exists():
        return {"protocol_version": "1.1", "saturation_k": 2, "axes": {},
                "findings": [], "pending_cases": []}
    data = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("axes", {})
    data.setdefault("findings", [])
    data.setdefault("pending_cases", [])
    data.setdefault("saturation_k", 2)
    return data


def save_ledger(data: dict[str, Any]) -> None:
    """Write the ledger back, preserving the documentation header comment."""
    header_lines: list[str] = []
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.startswith("#") or line.strip() == "":
                header_lines.append(line)
            else:
                break
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    LEDGER_PATH.write_text("".join(header_lines) + body, encoding="utf-8")


def read_fixture_text(*parts: str) -> str:
    return (FIXTURES_DIR.joinpath(*parts)).read_text(encoding="utf-8")

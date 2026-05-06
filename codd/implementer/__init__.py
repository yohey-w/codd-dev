"""Compatibility package for implementation helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_LEGACY_PATH = Path(__file__).resolve().parent.parent / "implementer.py"
_LEGACY_NAME = "codd._implementer_legacy"
_spec = importlib.util.spec_from_file_location(_LEGACY_NAME, _LEGACY_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise ImportError(f"cannot load implementer module from {_LEGACY_PATH}")

_legacy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_LEGACY_NAME, _legacy)
_spec.loader.exec_module(_legacy)

for _name, _value in vars(_legacy).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from .chunked_runner import ChunkedExecution, ChunkedRunner, ChunkedRunResult

__all__ = [
    *list(getattr(_legacy, "__all__", [])),
    "ChunkedExecution",
    "ChunkedRunner",
    "ChunkedRunResult",
]

"""R5.3 — Runtime wiring detection for codd extract.

Detects framework-specific implicit dependencies that don't appear
in import graphs or call graphs: DI injection, middleware chains,
signal handlers, and decorator-based routing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class RuntimeWire:
    """An implicit runtime dependency detected from framework patterns."""
    kind: str           # "depends" | "middleware" | "signal" | "decorator" | "task"
    source: str         # file:line
    target: str         # the function/class/module wired
    framework: str      # "fastapi" | "django" | "flask" | "celery" | "generic"


# ── Detection patterns ──────────────────────────────────

# FastAPI Depends()
_FASTAPI_DEPENDS_RE = re.compile(
    r"""Depends\(\s*(\w[\w.]*)\s*\)""",
)

# Django MIDDLEWARE list
_DJANGO_MIDDLEWARE_RE = re.compile(
    r"""MIDDLEWARE\s*=\s*\[([^\]]*)\]""",
    re.DOTALL,
)

# Django signals: signal.connect(handler)
_DJANGO_SIGNAL_RE = re.compile(
    r"""(\w+)\s*\.\s*connect\(\s*(\w[\w.]*)\s*""",
)

# Flask before/after request
_FLASK_HOOK_RE = re.compile(
    r"""@\s*(?:\w+\.)\s*(before_request|after_request|before_first_request|teardown_request|teardown_appcontext)\b""",
)

# Celery task
_CELERY_TASK_RE = re.compile(
    r"""@\s*(?:\w+\.)\s*task\b""",
)

# Generic event handler registration: on_event, add_event_handler, register
_GENERIC_HANDLER_RE = re.compile(
    r"""(?:on_event|add_event_handler|register_handler|subscribe)\(\s*['"](\w+)['"]\s*,\s*(\w[\w.]*)""",
)


def detect_runtime_wires(content: str, file_path: str) -> list[RuntimeWire]:
    """Detect runtime wiring patterns in source code."""
    wires: list[RuntimeWire] = []
    lines = content.splitlines()

    for line_no, line in enumerate(lines, 1):
        source = f"{file_path}:{line_no}"

        # FastAPI Depends()
        for m in _FASTAPI_DEPENDS_RE.finditer(line):
            wires.append(RuntimeWire(
                kind="depends",
                source=source,
                target=m.group(1),
                framework="fastapi",
            ))

        # Django signals
        m = _DJANGO_SIGNAL_RE.search(line)
        if m and m.group(1) in ("post_save", "pre_save", "post_delete",
                                 "pre_delete", "m2m_changed", "post_init",
                                 "pre_init", "request_started", "request_finished"):
            wires.append(RuntimeWire(
                kind="signal",
                source=source,
                target=m.group(2),
                framework="django",
            ))

        # Flask hooks
        m = _FLASK_HOOK_RE.search(line)
        if m:
            wires.append(RuntimeWire(
                kind="decorator",
                source=source,
                target=m.group(1),
                framework="flask",
            ))

        # Celery task
        if _CELERY_TASK_RE.search(line):
            wires.append(RuntimeWire(
                kind="task",
                source=source,
                target="celery_task",
                framework="celery",
            ))

        # Generic event handlers
        m = _GENERIC_HANDLER_RE.search(line)
        if m:
            wires.append(RuntimeWire(
                kind="signal",
                source=source,
                target=m.group(2),
                framework="generic",
            ))

    # Django MIDDLEWARE (multiline)
    for m in _DJANGO_MIDDLEWARE_RE.finditer(content):
        raw = m.group(1)
        for mw in re.findall(r"""['"]([^'"]+)['"]""", raw):
            wires.append(RuntimeWire(
                kind="middleware",
                source=f"{file_path}:{content[:m.start()].count(chr(10)) + 1}",
                target=mw,
                framework="django",
            ))

    return wires


def build_runtime_wires(facts: ProjectFacts, project_root: Path) -> None:
    """Populate ``runtime_wires`` on every module in *facts*."""
    for mod in facts.modules.values():
        all_wires: list[RuntimeWire] = []
        for rel_file in mod.files:
            full = project_root / rel_file
            try:
                content = full.read_text(errors="ignore")
            except Exception:
                continue
            wires = detect_runtime_wires(content, rel_file)
            all_wires.extend(wires)
        mod.runtime_wires = all_wires

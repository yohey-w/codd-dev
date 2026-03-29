"""R8 — Environment variable & config dependency detection for codd extract.

Detects references to environment variables and config keys that indicate
runtime configuration dependencies not visible in import or call graphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class EnvRef:
    """A reference to an environment variable or config key."""
    key: str           # "DB_HOST", "DATABASE_URL"
    kind: str          # "env" | "config"
    file: str          # relative file path
    line: int          # line number
    has_default: bool  # True if default value provided


# ── Python env patterns ──────────────────────────────────────────────────────

# os.getenv("KEY") or os.getenv("KEY", default)
_PY_GETENV_RE = re.compile(
    r"""os\.getenv\(\s*["']([^"']+)["']""",
)

# os.environ["KEY"]
_PY_ENVIRON_BRACKET_RE = re.compile(
    r"""os\.environ\[["']([^"']+)["']\]""",
)

# os.environ.get("KEY") or os.environ.get("KEY", default)
_PY_ENVIRON_GET_RE = re.compile(
    r"""os\.environ\.get\(\s*["']([^"']+)["']""",
)

# os.environ.pop("KEY") or os.environ.pop("KEY", default)
_PY_ENVIRON_POP_RE = re.compile(
    r"""os\.environ\.pop\(\s*["']([^"']+)["']""",
)

# ── TS/JS env patterns ───────────────────────────────────────────────────────

# process.env.KEY (dotted access — uppercase identifier only)
_JS_ENV_DOT_RE = re.compile(
    r"""process\.env\.([A-Z_][A-Z0-9_]*)""",
)

# process.env["KEY"] or process.env['KEY']
_JS_ENV_BRACKET_RE = re.compile(
    r"""process\.env\[["']([^"']+)["']\]""",
)

# ── Config patterns (Python) ─────────────────────────────────────────────────

# config["KEY"], settings["KEY"], cfg["KEY"], etc.
_PY_CONFIG_BRACKET_RE = re.compile(
    r"""(?:config|settings|cfg|conf|app\.config|current_app\.config)\[["']([^"']+)["']\]""",
)

# settings.UPPER_CASE_ATTR  (only UPPER_CASE to avoid false positives)
_PY_SETTINGS_ATTR_RE = re.compile(
    r"""settings\.([A-Z_][A-Z0-9_]*)""",
)


def _has_second_arg(line: str, match_end: int) -> bool:
    """Return True if the call at *match_end* has a second argument.

    Checks whether the substring from the closing quote of the first
    string arg to the closing paren contains a comma, indicating a
    default value was supplied.

    E.g.:
        os.getenv("KEY", "fallback")  → True
        os.getenv("KEY")              → False
    """
    rest = line[match_end:]
    # rest starts right after the first argument's closing quote.
    # Look for a comma before the matching close-paren (shallow scan).
    depth = 0
    for ch in rest:
        if ch in ("(", "[", "{"):
            depth += 1
        elif ch in (")", "]", "}"):
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            return True
    return False


def detect_env_refs(content: str, file_path: str) -> list[EnvRef]:
    """Detect environment variable and config references in source code."""
    refs: list[EnvRef] = []
    lines = content.splitlines()

    for line_no, line in enumerate(lines, 1):

        # ── Python: os.getenv ───────────────────────────────────────────────
        for m in _PY_GETENV_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=_has_second_arg(line, m.end()),
            ))

        # ── Python: os.environ["KEY"] ───────────────────────────────────────
        for m in _PY_ENVIRON_BRACKET_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=False,
            ))

        # ── Python: os.environ.get ──────────────────────────────────────────
        for m in _PY_ENVIRON_GET_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=_has_second_arg(line, m.end()),
            ))

        # ── Python: os.environ.pop ──────────────────────────────────────────
        for m in _PY_ENVIRON_POP_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=_has_second_arg(line, m.end()),
            ))

        # ── TS/JS: process.env.KEY ──────────────────────────────────────────
        for m in _JS_ENV_DOT_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=False,
            ))

        # ── TS/JS: process.env["KEY"] ───────────────────────────────────────
        for m in _JS_ENV_BRACKET_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="env",
                file=file_path,
                line=line_no,
                has_default=False,
            ))

        # ── Python config bracket access ────────────────────────────────────
        for m in _PY_CONFIG_BRACKET_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="config",
                file=file_path,
                line=line_no,
                has_default=False,
            ))

        # ── Python settings.UPPER_CASE ──────────────────────────────────────
        for m in _PY_SETTINGS_ATTR_RE.finditer(line):
            refs.append(EnvRef(
                key=m.group(1),
                kind="config",
                file=file_path,
                line=line_no,
                has_default=False,
            ))

    return refs


def build_env_refs(facts: "ProjectFacts", project_root: Path) -> None:
    """Populate ``env_refs`` on every module in *facts*."""
    for mod in facts.modules.values():
        all_refs: list[EnvRef] = []
        for rel_file in getattr(mod, "files", []):
            full = project_root / rel_file
            try:
                content = full.read_text(errors="ignore")
            except Exception:
                continue
            refs = detect_env_refs(content, rel_file)
            all_refs.extend(refs)
        mod.env_refs = all_refs
